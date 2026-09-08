from django.test import SimpleTestCase
from django.urls import resolve, reverse


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
