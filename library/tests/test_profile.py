from django.test import TestCase
from django.urls import reverse

from library.tests.helpers import make_user


class ProfileTests(TestCase):

    def setUp(self):
        self.user = make_user(username="carol", password="OldPassword1", role="Librarian")
        self.client.login(username="carol", password="OldPassword1")

    def test_profile_page_shows_account_info(self):
        response = self.client.get(reverse("profile"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "carol")
        self.assertContains(response, "Librarian")

    def test_wrong_current_password_is_rejected(self):
        response = self.client.post(reverse("profile"), {
            "current_password": "not-the-real-password",
            "new_password": "NewPassword1",
            "confirm_password": "NewPassword1",
        })
        self.assertContains(response, "incorrect")

        # password must not have changed
        self.client.logout()
        self.assertFalse(
            self.client.login(username="carol", password="NewPassword1")
        )

    def test_mismatched_confirmation_is_rejected(self):
        response = self.client.post(reverse("profile"), {
            "current_password": "OldPassword1",
            "new_password": "NewPassword1",
            "confirm_password": "SomethingElse2",
        })
        self.assertContains(response, "do not match")

    def test_too_short_password_is_rejected(self):
        response = self.client.post(reverse("profile"), {
            "current_password": "OldPassword1",
            "new_password": "short",
            "confirm_password": "short",
        })
        self.assertContains(response, "at least 8 characters")

    def test_successful_change_updates_password_and_keeps_session(self):
        response = self.client.post(reverse("profile"), {
            "current_password": "OldPassword1",
            "new_password": "NewPassword1",
            "confirm_password": "NewPassword1",
        })
        self.assertContains(response, "updated successfully")

        # session should still be valid (update_session_auth_hash worked)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

        # new password now works for a fresh login
        self.client.logout()
        self.assertTrue(
            self.client.login(username="carol", password="NewPassword1")
        )
