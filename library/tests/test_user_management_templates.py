from django.test import TestCase
from django.urls import reverse

from library.tests.helpers import make_user


class UserManagementTemplateTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin_templates", role="Admin")
        self.target = make_user("target_templates", role="Assistant")
        self.client.force_login(self.admin)

    def test_add_edit_and_delete_pages_render(self):
        self.assertEqual(self.client.get(reverse("user_add")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("user_edit", args=[self.target.id])).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("user_delete", args=[self.target.id])).status_code,
            200,
        )
