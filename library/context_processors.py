"""Template context shared by every page.

`branding` has to be available on templates rendered for anonymous users
(the login page most importantly), so nothing here may touch
`request.user`. It also has to work before anything has been configured, so
`OrganizationSettings.load()` returns an instance carrying defaults rather
than None.
"""

from django.core.cache import cache

from .models import OrganizationSettings


BRANDING_CACHE_KEY = "organization_branding"
BRANDING_CACHE_TIMEOUT = 300


def get_branding():
    """The organisation's branding, cached.

    Follows the same read/write/invalidate pattern the views use for the
    catalogue caches: read through the cache, and let the writer call
    `clear_branding_cache()`.
    """

    branding = cache.get(BRANDING_CACHE_KEY)

    if branding is None:
        branding = OrganizationSettings.load()

        cache.set(
            BRANDING_CACHE_KEY,
            branding,
            timeout=BRANDING_CACHE_TIMEOUT,
        )

    return branding


def clear_branding_cache():
    """Call after saving branding, so the next render picks it up."""

    cache.delete(BRANDING_CACHE_KEY)


def get_theme_preference(request):
    """The signed-in user's appearance choice, or "" for anonymous visitors.

    The empty string matters: it tells the layout's pre-paint script that
    there is no server-side choice to honour, so it should fall back to the
    localStorage mirror and then the OS setting. Returning "system" instead
    would wrongly override a choice the browser already remembers.
    """

    user = getattr(request, "user", None)

    if user is None or not user.is_authenticated:
        return ""

    return user.theme


def branding(request):
    return {
        "branding": get_branding(),
        "theme_preference": get_theme_preference(request),
    }
