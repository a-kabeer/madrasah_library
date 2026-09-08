from django.test import SimpleTestCase, TestCase
from django.urls import resolve, reverse

from library.tests.helpers import make_borrower, make_loan, make_user


class BorrowerModalRoutingTests(SimpleTestCase):
    def test_borrower_crud_routes_use_modal_views(self):
        cases = (
            ("borrower_list", "borrower_list_modal"),
            ("borrower_add", "borrower_add_modal"),
            ("borrower_detail", "borrower_detail_modal"),
            ("borrower_edit", "borrower_edit_modal"),
            ("borrower_delete", "borrower_delete_modal"),
        )
        for name, expected in cases:
            kwargs = {} if name in {"borrower_list", "borrower_add"} else {"borrower_id": 1}
            match = resolve(reverse(name, kwargs=kwargs))
            self.assertEqual(match.func.__name__, expected)

    def test_borrower_routes_keep_stable_names(self):
        self.assertEqual(reverse("borrower_list"), "/library/borrowers/")
        self.assertEqual(reverse("borrower_add"), "/library/borrowers/add/")
        self.assertEqual(reverse("borrower_detail", kwargs={"borrower_id": 7}), "/library/borrowers/7/")
        self.assertEqual(reverse("borrower_edit", kwargs={"borrower_id": 7}), "/library/borrowers/7/edit/")
        self.assertEqual(reverse("borrower_delete", kwargs={"borrower_id": 7}), "/library/borrowers/7/delete/")


class BorrowerModalCrudTests(TestCase):
    def setUp(self):
        user = make_user(username="borrower_modal_admin", password="pass12345", role="Admin")
        self.client.login(username=user.username, password="pass12345")

    def test_add_rejects_duplicate_phone_and_preserves_modal_form(self):
        make_borrower(name="Existing Borrower", phone="03001234567")
        response = self.client.post(reverse("borrower_add"), {
            "name": "Another Borrower",
            "phone": "03001234567",
            "borrower_type": "Student",
        }, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertContains(response, "Another Borrower")

    def test_add_success_returns_htmx_redirect_instead_of_swapping_page(self):
        response = self.client.post(reverse("borrower_add"), {
            "name": "New Borrower",
            "phone": "03009998877",
            "borrower_type": "Student",
            "registration_no": "ST-100",
        }, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], reverse("borrower_list"))
        self.assertTrue(response)

    def test_edit_rejects_duplicate_registration_number(self):
        first = make_borrower(name="First", phone="03001111111")
        second = make_borrower(name="Second", phone="03002222222")
        first.registration_no = "REG-1"
        first.save()

        response = self.client.post(reverse("borrower_edit", kwargs={"borrower_id": second.id}), {
            "name": "Second",
            "phone": "03002222222",
            "borrower_type": "Student",
            "registration_no": "REG-1",
            "is_active": "True",
        }, HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already belongs to another borrower")

    def test_delete_with_loan_history_is_blocked(self):
        borrower = make_borrower(name="Borrower With History", phone="03003333333")
        make_loan(borrower=borrower)

        response = self.client.post(reverse("borrower_delete", kwargs={"borrower_id": borrower.id}), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "loan history")
        self.assertTrue(type(borrower).objects.filter(id=borrower.id).exists())
