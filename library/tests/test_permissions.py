from django.test import TestCase
from django.urls import reverse

from library.tests.helpers import make_user, make_category


class RolePermissionTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.librarian = make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.assistant = make_user(username="assistant_u", password="pass12345", role="Assistant")

        self.category = make_category()

    def login_as(self, user):
        self.client.logout()
        self.client.login(username=user.username, password="pass12345")

    # --- User management: Admin only ---

    def test_admin_can_access_user_list(self):
        self.login_as(self.admin)
        response = self.client.get(reverse("user_list"))
        self.assertEqual(response.status_code, 200)

    def test_librarian_blocked_from_user_list(self):
        self.login_as(self.librarian)
        response = self.client.get(reverse("user_list"))
        self.assertEqual(response.status_code, 403)

    def test_assistant_blocked_from_user_list(self):
        self.login_as(self.assistant)
        response = self.client.get(reverse("user_list"))
        self.assertEqual(response.status_code, 403)

    # --- Catalog add/edit: Admin + Librarian, not Assistant ---

    def test_librarian_can_add_book(self):
        self.login_as(self.librarian)
        response = self.client.get(reverse("book_add"))
        self.assertEqual(response.status_code, 200)

    def test_assistant_blocked_from_adding_book(self):
        self.login_as(self.assistant)
        response = self.client.get(reverse("book_add"))
        self.assertEqual(response.status_code, 403)

    def test_assistant_can_still_view_book_list(self):
        self.login_as(self.assistant)
        response = self.client.get(reverse("book_list"))
        self.assertEqual(response.status_code, 200)

    # --- Deletes: Admin + Librarian, not Assistant ---

    def test_assistant_blocked_from_category_delete(self):
        self.login_as(self.assistant)
        response = self.client.get(
            reverse("category_delete", args=[self.category.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_librarian_can_reach_category_delete(self):
        # Deleting a category is a dialog now, not a page: a plain GET has
        # no page to answer with and goes to the list. What this asserts is
        # what it always did - a Librarian is not refused, where an
        # Assistant is - so it checks both shapes of the request.
        self.login_as(self.librarian)

        plain = self.client.get(
            reverse("category_delete", args=[self.category.id])
        )
        self.assertEqual(plain.status_code, 302)

        dialog = self.client.get(
            reverse("category_delete", args=[self.category.id]),
            {"modal": "1"},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(dialog.status_code, 200)

    # --- Borrower management: all three roles allowed ---

    def test_assistant_can_add_borrower(self):
        self.login_as(self.assistant)
        response = self.client.get(reverse("borrower_add"))
        self.assertEqual(response.status_code, 200)

    def test_assistant_can_add_loan(self):
        self.login_as(self.assistant)
        response = self.client.get(reverse("loan_add"))
        self.assertEqual(response.status_code, 200)

    # --- Loan delete: Admin + Librarian, not Assistant ---

    def test_assistant_blocked_from_loan_delete_view(self):
        # loan_delete requires a real loan id in the URL; use a nonexistent
        # one and confirm the permission check fires before the 404 lookup
        # would — role_required runs first, so this should be 403, not 404.
        self.login_as(self.assistant)
        response = self.client.get(reverse("loan_delete", args=[999999]))
        self.assertEqual(response.status_code, 403)
