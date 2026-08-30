from django.contrib.auth import authenticate
from django.test import TestCase
from django.urls import reverse

from library.models import User, Loan
from library.tests.helpers import make_user, make_loan


class UserEditTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")
        self.target = make_user(username="target_u", password="OldPass123", role="Assistant")

    def test_edit_without_new_password_keeps_old_password_working(self):
        response = self.client.post(reverse("user_edit", args=[self.target.id]), {
            "username": "target_u",
            "full_name": "Target Updated",
            "new_password": "",
            "role": "Assistant",
            "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)

        self.target.refresh_from_db()
        self.assertEqual(self.target.full_name, "Target Updated")
        self.assertIsNotNone(authenticate(username="target_u", password="OldPass123"))

    def test_setting_new_password_actually_changes_it(self):
        response = self.client.post(reverse("user_edit", args=[self.target.id]), {
            "username": "target_u",
            "full_name": "Target User",
            "new_password": "BrandNewPass1",
            "role": "Assistant",
            "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)

        self.assertIsNone(authenticate(username="target_u", password="OldPass123"))
        self.assertIsNotNone(authenticate(username="target_u", password="BrandNewPass1"))

    def test_too_short_new_password_is_rejected(self):
        response = self.client.post(reverse("user_edit", args=[self.target.id]), {
            "username": "target_u",
            "full_name": "Target User",
            "new_password": "short",
            "role": "Assistant",
        })
        self.assertContains(response, "at least 8 characters")
        # original password must still work
        self.assertIsNotNone(authenticate(username="target_u", password="OldPass123"))


class UserDeleteTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_user_delete_blocked_when_they_issued_a_loan(self):
        staff = make_user(username="staff_u", password="pass12345", role="Librarian")
        make_loan(issued_by=staff)

        response = self.client.post(reverse("user_delete", args=[staff.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(User.objects.filter(id=staff.id).exists())

    def test_user_delete_succeeds_with_no_related_records(self):
        staff = make_user(username="staff_u", password="pass12345", role="Librarian")

        response = self.client.post(reverse("user_delete", args=[staff.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(id=staff.id).exists())
