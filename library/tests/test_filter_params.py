"""A filter nobody can see is not worth a 500.

Every list page reads its filters straight from the query string. Five of
them handed the value to `filter()` untouched, so `?location=abc` on the
shelf list, `?borrower=abc` or `?issue_date=abc` on the loans list,
`?volume=abc` on the contents list, `?book=abc` on the volumes list and
`?user=abc` or `?date=abc` on the activity log each raised out of the view
and became a 500 - reachable by editing the address bar, or by following a
bookmark made before a filter's values changed.

`numeric_param` already existed for exactly this, with a docstring saying
so; those call sites simply did not use it. `date_param` is its sibling for
the three date filters, and a well-formed but impossible date
(2026-02-30) has to be refused too, not just a non-date.

The rule these tests hold: an unreadable filter value is dropped and the
page answers without it, the same way an unknown sort field or status
already is. A readable one still filters.
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from library.tests.helpers import (
    make_user, make_book, make_volume, make_content, make_location,
    make_shelf, make_copy, make_borrower, make_loan,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# The values that used to crash, plus the neighbours worth keeping honest:
# an out-of-range id, a huge one, and a date that parses but does not exist.
UNREADABLE = ("abc", "-1", "1.5", "99999999999999999999", "", "%", "2026-02-30")


@override_settings(CACHES=LOCMEM)
class UnreadableFilterValues(TestCase):

    def setUp(self):
        self.admin = make_user(username="filter_admin", password="pass12345",
                               role="Admin")
        self.client.force_login(self.admin)

        self.location = make_location(name="Filter Location")
        self.shelf = make_shelf(location=self.location, shelf_code="FL-1")
        self.book = make_book(title="Filter Book")
        self.volume = make_volume(book=self.book, volume_number=1, title="Juz 1")
        make_content(volume=self.volume, title="Bab al-Awwal")
        self.copy = make_copy(volume=self.volume, shelf=self.shelf,
                              copy_code="FL-COPY-1")
        self.borrower = make_borrower(name="Filter Borrower")
        self.loan = make_loan(copy=self.copy, borrower=self.borrower,
                              issue_date=timezone.now().date() - timedelta(days=3))

    # --- the eight parameters that used to raise ---

    def test_no_list_filter_can_crash_its_page(self):
        cases = (
            ("shelf_list", "location"),
            ("loan_list", "borrower"),
            ("loan_list", "issue_date"),
            ("loan_list", "due_date"),
            ("book_content_list", "volume"),
            ("book_volume_list", "book"),
            ("activity_log_list", "user"),
            ("activity_log_list", "date"),
        )

        for page, param in cases:
            for value in UNREADABLE:
                with self.subTest(page=page, param=param, value=value):
                    response = self.client.get(reverse(page), {param: value})
                    self.assertEqual(response.status_code, 200)

    # --- and the value is dropped, not silently reinterpreted ---

    def test_an_unreadable_id_filter_shows_the_unfiltered_list(self):
        response = self.client.get(reverse("shelf_list"), {"location": "abc"})

        self.assertContains(response, "FL-1")

    def test_a_readable_id_filter_still_filters(self):
        other = make_location(name="Other Location")
        make_shelf(location=other, shelf_code="OL-9")

        response = self.client.get(reverse("shelf_list"),
                                   {"location": self.location.id})

        self.assertContains(response, "FL-1")
        self.assertNotContains(response, "OL-9")

    def test_a_readable_date_filter_still_filters(self):
        on = self.loan.issue_date.isoformat()
        off = (self.loan.issue_date - timedelta(days=10)).isoformat()

        self.assertContains(
            self.client.get(reverse("loan_list"), {"issue_date": on}),
            "Filter Borrower",
        )
        self.assertNotContains(
            self.client.get(reverse("loan_list"), {"issue_date": off}),
            "Filter Borrower",
        )

    def test_an_unreadable_date_leaves_the_input_empty(self):
        """The form must not echo a value that is not being applied."""

        response = self.client.get(reverse("activity_log_list"), {"date": "abc"})

        self.assertNotContains(response, 'value="abc"')

    def test_a_readable_date_is_echoed_back_in_iso_form(self):
        """`<input type="date">` only accepts YYYY-MM-DD, so the context has
        to carry the string, not a formatted date."""

        response = self.client.get(reverse("activity_log_list"),
                                   {"date": "2026-01-05"})

        self.assertContains(response, 'value="2026-01-05"')
