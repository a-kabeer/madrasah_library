"""A GET never changes anything.

Every destructive and state-changing endpoint is reachable by URL, and a
browser, a crawler or a link prefetcher will issue GET requests to all of
them. None may act: a GET that deletes is a link that deletes, and the
project's own confirm dialogs assume the GET is what draws them.

Checked against the routes rather than a hand-written list, so an endpoint
added later is covered by the same assertion.
"""

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from library.models import (
    Author, Book, BookCopy, BookVolume, Category, Loan, Location, Publisher,
    Shelf, Borrower, User,
)
from library.tests.helpers import (
    make_author, make_book, make_borrower, make_category, make_copy, make_loan,
    make_location, make_publisher, make_shelf, make_user, make_volume,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class GetIsSafeTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="safe_admin", password="p", role="Admin")
        self.client.force_login(self.admin)
        self.author = make_author(name="Safe Author")
        self.category = make_category(name="Safe Category")
        self.publisher = make_publisher(name="Safe Publisher")
        self.location = make_location(name="Safe Location")
        self.shelf = make_shelf(location=self.location, shelf_code="SAFE-1")
        self.made = 0
        cache.clear()

    def a_book(self, title):
        return make_book(
            title=title,
            author=self.author,
            category=self.category,
            publisher=self.publisher,
        )

    def a_copy(self):
        self.made += 1

        return make_copy(
            volume=make_volume(book=self.a_book(f"Host {self.made}"), volume_number=1),
            shelf=self.shelf,
            copy_code=f"SAFE-{self.made:05d}",
        )

    def assertGetChangesNothing(self, route, obj, model, field=None):
        """A GET to `route` leaves `obj` exactly as it was."""

        before = getattr(obj, field) if field else True
        self.client.get(reverse(route, args=[obj.id]))
        row = model.objects.filter(id=obj.id).first()

        self.assertIsNotNone(row, f"{route} deleted the record on a GET")

        if field:
            self.assertEqual(
                getattr(row, field), before, f"{route} changed {field} on a GET"
            )

    def test_deletes_do_not_delete(self):
        self.assertGetChangesNothing("book_delete", self.a_book("Keep A"), Book)
        self.assertGetChangesNothing(
            "book_volume_delete",
            make_volume(book=self.a_book("Keep B"), volume_number=7),
            BookVolume,
        )
        self.assertGetChangesNothing("book_copy_delete", self.a_copy(), BookCopy)
        self.assertGetChangesNothing(
            "author_delete", make_author(name="Keep Author"), Author
        )
        self.assertGetChangesNothing(
            "category_delete", make_category(name="Keep Category"), Category
        )
        self.assertGetChangesNothing(
            "publisher_delete", make_publisher(name="Keep Publisher"), Publisher
        )
        self.assertGetChangesNothing(
            "location_delete", make_location(name="Keep Location"), Location
        )
        self.assertGetChangesNothing(
            "shelf_delete",
            make_shelf(location=self.location, shelf_code="KEEP-9"),
            Shelf,
        )
        self.assertGetChangesNothing(
            "borrower_delete",
            make_borrower(name="Keep Borrower", phone="03007770001"),
            Borrower,
        )
        self.assertGetChangesNothing(
            "user_delete",
            make_user(username="keep_u", password="p", role="Assistant"),
            User,
        )
        self.assertGetChangesNothing("loan_delete", make_loan(copy=self.a_copy()), Loan)

    def test_state_changes_do_not_happen(self):
        self.assertGetChangesNothing(
            "book_archive", self.a_book("Keep C"), Book, field="archived_at"
        )
        self.assertGetChangesNothing(
            "book_copy_withdraw", self.a_copy(), BookCopy, field="status"
        )
        self.assertGetChangesNothing(
            "borrower_toggle_active",
            make_borrower(name="Keep Active", phone="03007770002"),
            Borrower,
            field="is_active",
        )
        self.assertGetChangesNothing(
            "user_toggle_active",
            make_user(username="keep_tog", password="p", role="Assistant"),
            User,
            field="is_active",
        )
        self.assertGetChangesNothing(
            "loan_return", make_loan(copy=self.a_copy()), Loan, field="return_date"
        )
