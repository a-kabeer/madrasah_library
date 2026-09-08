from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from library.tests.helpers import make_user


class LoginTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = make_user(username="alice", password="CorrectHorse1", role="Admin")

    def tearDown(self):
        cache.clear()

    def test_login_page_reachable_when_anonymous(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)

    def test_protected_page_redirects_anonymous_user_to_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_correct_credentials_log_in(self):
        response = self.client.post(reverse("login"), {
            "username": "alice",
            "password": "CorrectHorse1",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard"))

    def test_wrong_password_shows_error_and_stays_anonymous(self):
        response = self.client.post(reverse("login"), {
            "username": "alice",
            "password": "wrong-password",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid username or password")

        # still anonymous — protected page should still redirect
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)

    @override_settings(
        LOGIN_RATE_LIMIT_USERNAME_MAX_FAILURES=3,
        LOGIN_RATE_LIMIT_IP_MAX_FAILURES=100,
        LOGIN_RATE_LIMIT_WINDOW_SECONDS=900,
    )
    def test_repeated_failures_are_rate_limited(self):
        payload = {"username": "alice", "password": "wrong-password"}

        for _ in range(3):
            response = self.client.post(reverse("login"), payload)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Invalid username or password")

        response = self.client.post(reverse("login"), payload)

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many failed login attempts")
        self.assertEqual(response["Retry-After"], "900")

    @override_settings(
        LOGIN_RATE_LIMIT_USERNAME_MAX_FAILURES=100,
        LOGIN_RATE_LIMIT_IP_MAX_FAILURES=2,
        LOGIN_RATE_LIMIT_WINDOW_SECONDS=900,
    )
    def test_ip_limit_protects_against_many_usernames(self):
        for username in ("alice", "unknown-one"):
            response = self.client.post(reverse("login"), {
                "username": username,
                "password": "wrong-password",
            })
            self.assertEqual(response.status_code, 200)

        response = self.client.post(reverse("login"), {
            "username": "another-unknown",
            "password": "wrong-password",
        })

        self.assertEqual(response.status_code, 429)

    @override_settings(
        LOGIN_RATE_LIMIT_USERNAME_MAX_FAILURES=3,
        LOGIN_RATE_LIMIT_IP_MAX_FAILURES=100,
        LOGIN_RATE_LIMIT_WINDOW_SECONDS=900,
    )
    def test_successful_login_clears_username_failures(self):
        self.client.post(reverse("login"), {
            "username": "alice",
            "password": "wrong-password",
        })

        response = self.client.post(reverse("login"), {
            "username": "alice",
            "password": "CorrectHorse1",
        })
        self.assertEqual(response.status_code, 302)

        self.client.get(reverse("logout"))

        # The previous failed attempt was cleared by the successful login.
        # Three new failures therefore still fit inside the threshold.
        for _ in range(3):
            response = self.client.post(reverse("login"), {
                "username": "alice",
                "password": "wrong-password",
            })
            self.assertEqual(response.status_code, 200)

        response = self.client.post(reverse("login"), {
            "username": "alice",
            "password": "wrong-password",
        })
        self.assertEqual(response.status_code, 429)

    def test_authenticated_user_can_reach_protected_page(self):
        self.client.login(username="alice", password="CorrectHorse1")
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_logout_clears_session(self):
        self.client.login(username="alice", password="CorrectHorse1")
        self.client.get(reverse("logout"))

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_inactive_user_cannot_log_in(self):
        make_user(username="bob", password="CorrectHorse1", role="Assistant", is_active=False)

        response = self.client.post(reverse("login"), {
            "username": "bob",
            "password": "CorrectHorse1",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid username or password")
