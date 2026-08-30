from django.test import TestCase
from django.urls import reverse

from library.tests.helpers import make_user


class LoginTests(TestCase):

    def setUp(self):
        self.user = make_user(username="alice", password="CorrectHorse1", role="Admin")

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
