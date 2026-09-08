"""Small, cache-backed protections for authentication endpoints."""

from django.conf import settings
from django.core.cache import cache


class LoginRateLimiter:
    """Throttle repeated failed login attempts by IP and username.

    The limiter deliberately keys both dimensions: an attacker should not be
    able to brute-force many accounts from one address, and a single account
    should not be brute-forced by many attempts from the same address.

    This uses Django's configured cache. For a multi-worker deployment the
    cache must therefore be shared (for example Redis); LocMemCache is only
    suitable for the current single-process/demo deployment.
    """

    @property
    def enabled(self):
        return getattr(settings, "LOGIN_RATE_LIMIT_ENABLED", True)

    @property
    def window(self):
        return getattr(settings, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 15 * 60)

    @property
    def ip_limit(self):
        return getattr(settings, "LOGIN_RATE_LIMIT_IP_MAX_FAILURES", 20)

    @property
    def username_limit(self):
        return getattr(settings, "LOGIN_RATE_LIMIT_USERNAME_MAX_FAILURES", 5)

    def _ip_key(self, request):
        # Do not trust X-Forwarded-For here. Django is only configured to
        # trust that header for HTTPS scheme detection, not client identity.
        ip = request.META.get("REMOTE_ADDR") or "unknown"
        return f"login-fail:ip:{ip}"

    def _username_key(self, username):
        normalized = username.strip().casefold()
        return f"login-fail:user:{normalized or '<empty>'}"

    def _increment(self, key):
        if cache.add(key, 1, timeout=self.window):
            return 1

        try:
            return cache.incr(key)
        except ValueError:
            # A cache backend may evict the key between add/incr. Start a new
            # window rather than allowing the exception to reach the login.
            cache.set(key, 1, timeout=self.window)
            return 1

    def is_blocked(self, request, username):
        if not self.enabled:
            return False

        ip_count = cache.get(self._ip_key(request), 0)
        username_count = cache.get(self._username_key(username), 0)

        return (
            ip_count >= self.ip_limit
            or username_count >= self.username_limit
        )

    def record_failure(self, request, username):
        if not self.enabled:
            return

        self._increment(self._ip_key(request))
        self._increment(self._username_key(username))

    def clear_username_failures(self, username):
        if not self.enabled:
            return

        cache.delete(self._username_key(username))


login_rate_limiter = LoginRateLimiter()
