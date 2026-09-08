from django.test import TestCase

from library.models import User
from library.tests.helpers import make_user


class UserCreationSecurityTests(TestCase):
    def setUp(self):
        self.admin = make_user("creator", role="Admin")
        self.client.force_login(self.admin)

    def test_new_user_password_is_hashed_and_authenticatable(self):
        response = self.client.post(
            "/library/users/add/",
            {
                "username": "newassistant",
                "full_name": "New Assistant",
                "password": "SafePass123!",
                "confirm_password": "SafePass123!",
                "role": "Assistant",
            },
        )

        self.assertRedirects(response, "/library/users/")

        created = User.objects.get(username="newassistant")
        self.assertNotEqual(created.password, "SafePass123!")
        self.assertTrue(created.password.startswith("pbkdf2_"))
        self.assertTrue(created.check_password("SafePass123!"))

    def test_old_raw_password_field_cannot_create_an_account(self):
        response = self.client.post(
            "/library/users/add/",
            {
                "username": "rawattempt",
                "full_name": "Raw Attempt",
                "password_hash": "SafePass123!",
                "role": "Assistant",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="rawattempt").exists())
