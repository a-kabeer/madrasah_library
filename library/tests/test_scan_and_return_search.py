"""Keyboard scanning on Issue, and one search field on Return.

A USB or Bluetooth barcode scanner is a keyboard: it types the copy code
and presses Enter. So there is nothing device-specific to test here, and
nothing about these paths that a librarian typing the same characters does
not also get. What is worth pinning is that a whole code is treated as a
whole code - added on Issue, resolved to one loan on Return - and that
neither route relaxes a single rule the ordinary form applies.

`copy_code` is the only identifier involved. It is unique in the database
(`book_copies_copy_code_key`), generated once from the row's own id, and
never rewritten, which is what makes it usable as the value on a label.
"""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from library.models import BookCopy, Loan
from library.views import RETURN_LOOKUP_LIMIT

from .helpers import (
    make_author,
    make_book,
    make_borrower,
    make_copy,
    make_loan,
    make_location,
    make_shelf,
    make_user,
    make_volume,
)


class ScanTestCase(TestCase):

    def setUp(self):
        self.librarian = make_user(
            username="librarian_u", password="pass12345", role="Librarian"
        )
        self.client.login(username="librarian_u", password="pass12345")

        self.shelf = make_shelf(location=make_location(name="Main Hall"))

        self.author = make_author("Abu Yusuf")
        self.book = make_book(title="Kitab al-Kharaj", author=self.author)
        self.volume = make_volume(book=self.book, volume_number=1, title="")

        self.borrower = make_borrower(
            name="Hafsa Rahmani", phone="03450000001"
        )

        self.today = timezone.now().date()

    def a_copy(self, code, status="Available", volume=None):
        return make_copy(
            volume=volume or self.volume,
            shelf=self.shelf,
            copy_code=code,
            status=status,
        )

    def scan(self, code, **params):
        """Type a code into the issue form and press Enter."""

        params["q"] = code

        return self.client.get(reverse("circulation_issue"), params)

    def basket(self, response):
        """The copy ids the redirect leaves in the query string."""

        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(response["Location"]).query)

        return [int(value) for value in query.get("copies", [])]


class IssueScanTests(ScanTestCase):

    def test_a_whole_code_goes_straight_into_the_basket(self):
        copy = self.a_copy("LIB-900001")

        response = self.scan("LIB-900001")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.basket(response), [copy.id])

    def test_the_search_box_is_emptied_for_the_next_scan(self):
        self.a_copy("LIB-900002")

        response = self.scan("LIB-900002")

        self.assertNotIn("q=", response["Location"])

    def test_a_code_is_matched_whatever_its_case(self):
        copy = self.a_copy("LIB-900003")

        self.assertEqual(
            self.basket(self.scan("lib-900003")), [copy.id]
        )

    def test_surrounding_space_from_the_scanner_is_ignored(self):
        copy = self.a_copy("LIB-900004")

        self.assertEqual(
            self.basket(self.scan("  LIB-900004  ")), [copy.id]
        )

    def test_scanning_several_builds_the_basket_up(self):
        first = self.a_copy("LIB-900005")
        second = self.a_copy("LIB-900006")
        third = self.a_copy("LIB-900007")

        response = self.scan("LIB-900005")
        self.assertEqual(self.basket(response), [first.id])

        response = self.scan("LIB-900006", copies=[first.id])
        self.assertEqual(self.basket(response), [first.id, second.id])

        response = self.scan(
            "LIB-900007", copies=[first.id, second.id]
        )
        self.assertEqual(
            self.basket(response), [first.id, second.id, third.id]
        )

    def test_the_same_code_twice_does_not_duplicate_it(self):
        copy = self.a_copy("LIB-900008")

        # A repeat stays on the page - nothing was added, so there is no
        # new basket to put in the URL - and the basket is unchanged.
        response = self.scan("LIB-900008", copies=[copy.id])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [c.id for c in response.context["chosen"]], [copy.id]
        )

    def test_a_repeated_scan_says_it_is_already_there(self):
        copy = self.a_copy("LIB-900009")

        response = self.client.get(
            reverse("circulation_issue"),
            {"q": "LIB-900009", "copies": [copy.id]},
            follow=True,
        )

        self.assertContains(response, "already in the list")

    def test_the_borrower_is_not_lost_by_a_scan(self):
        copy = self.a_copy("LIB-900010")

        response = self.scan("LIB-900010", borrower=self.borrower.id)

        self.assertIn("borrower=%d" % self.borrower.id, response["Location"])
        self.assertEqual(self.basket(response), [copy.id])

    def test_an_unknown_code_is_searched_for_instead(self):
        # Not an error: the same field takes a book title, and the librarian
        # may have meant one.
        self.a_copy("LIB-900011")

        response = self.scan("LIB-999999")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["matches"]), [])

    def test_a_title_still_searches(self):
        copy = self.a_copy("LIB-900012")

        response = self.scan("Kharaj")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [c.id for c in response.context["matches"]], [copy.id]
        )

    def test_an_author_still_searches(self):
        copy = self.a_copy("LIB-900013")

        self.assertEqual(
            [c.id for c in self.scan("Abu Yusuf").context["matches"]],
            [copy.id],
        )


class IssueScanRefusalTests(ScanTestCase):
    """Scanning refuses exactly what picking from the results refuses."""

    def follow(self, code, **params):
        params["q"] = code

        return self.client.get(
            reverse("circulation_issue"), params, follow=True
        )

    def test_a_copy_already_out_is_refused_with_the_reason(self):
        copy = self.a_copy("LIB-910001")
        make_loan(copy=copy, borrower=self.borrower)
        copy.status = "Issued"
        copy.save(update_fields=["status"])

        response = self.follow("LIB-910001")

        self.assertContains(response, "already out on loan")
        self.assertEqual(list(response.context["chosen"]), [])

    def test_a_withdrawn_copy_is_refused_with_the_reason(self):
        self.a_copy("LIB-910002", status="Transferred")

        response = self.follow("LIB-910002")

        self.assertContains(response, "out of circulation")
        self.assertEqual(list(response.context["chosen"]), [])

    def test_a_copy_of_an_archived_book_is_refused_with_the_reason(self):
        self.a_copy("LIB-910003")

        self.book.archived_at = timezone.now()
        self.book.save(update_fields=["archived_at"])

        response = self.follow("LIB-910003")

        self.assertContains(response, "archived")
        self.assertEqual(list(response.context["chosen"]), [])

    def test_a_refusal_keeps_the_borrower_and_the_basket(self):
        kept = self.a_copy("LIB-910004")
        self.a_copy("LIB-910005", status="Damaged")

        response = self.follow(
            "LIB-910005", borrower=self.borrower.id, copies=[kept.id]
        )

        self.assertContains(response, "out of circulation")
        self.assertEqual(
            [c.id for c in response.context["chosen"]], [kept.id]
        )
        self.assertEqual(
            response.context["borrower_id"], str(self.borrower.id)
        )

    def test_a_scan_never_writes_anything(self):
        self.a_copy("LIB-910006")

        before = Loan.objects.count()

        self.scan("LIB-910006")

        self.assertEqual(Loan.objects.count(), before)
        self.assertEqual(
            BookCopy.objects.get(copy_code="LIB-910006").status, "Available"
        )

    def test_the_post_is_still_the_authority(self):
        # Scanning is a convenience on the way to the form. The rules are
        # applied again when the loan is written, so a basket assembled by
        # any means is re-checked.
        copy = self.a_copy("LIB-910007", status="Transferred")

        response = self.client.post(
            reverse("circulation_issue"),
            {
                "borrower": self.borrower.id,
                "copies": [copy.id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Loan.objects.filter(copy=copy).exists())

    def test_a_borrowing_policy_still_applies_to_a_scanned_basket(self):
        from library.models import OrganizationSettings

        row = OrganizationSettings.load()
        row.max_active_loans = 1
        row.save()

        make_loan(
            copy=self.a_copy("LIB-910008", status="Issued"),
            borrower=self.borrower,
        )

        copy = self.a_copy("LIB-910009")

        # Scanning it in is allowed - the basket is not a loan.
        self.assertEqual(
            self.basket(
                self.scan("LIB-910009", borrower=self.borrower.id)
            ),
            [copy.id],
        )

        # Issuing it is not.
        response = self.client.post(
            reverse("circulation_issue"),
            {
                "borrower": self.borrower.id,
                "copies": [copy.id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        self.assertContains(response, "the limit")
        self.assertFalse(Loan.objects.filter(copy=copy).exists())


class IssueScanQueryTests(ScanTestCase):

    def test_a_scan_does_not_read_the_whole_copies_table(self):
        for number in range(30):
            self.a_copy("LIB-92%04d" % number)

        with CaptureQueriesContext(connection) as queries:
            self.scan("LIB-920005")

        for query in queries.captured_queries:
            with self.subTest(sql=query["sql"][:80]):
                # Every copy read is either the one code or the basket.
                if 'FROM "book_copies"' not in query["sql"]:
                    continue

                self.assertTrue(
                    "copy_code" in query["sql"]
                    or '"id" IN' in query["sql"]
                    or '"id" =' in query["sql"],
                    query["sql"],
                )

    def test_the_cost_of_a_scan_does_not_grow_with_the_catalogue(self):
        self.a_copy("LIB-930001")

        with CaptureQueriesContext(connection) as few:
            self.scan("LIB-930001")

        for number in range(30):
            self.a_copy("LIB-93%04d" % (number + 100))

        with CaptureQueriesContext(connection) as many:
            self.scan("LIB-930001")

        self.assertEqual(len(few), len(many))


class ReturnCodeLookupTests(ScanTestCase):
    """The scan path: one code, one targeted lookup, no searching."""

    def setUp(self):
        super().setUp()

        self.copy = self.a_copy("LIB-940001", status="Issued")
        self.loan = make_loan(
            copy=self.copy,
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=10),
            due_date=self.today + timedelta(days=4),
        )

    def look(self, term, **extra):
        params = {"copy_code": term}
        params.update(extra)

        return self.client.get(
            reverse("circulation_return_lookup"), params
        )

    def test_a_scanned_code_finds_the_active_loan(self):
        response = self.look("LIB-940001")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["loan"].id, self.loan.id)

    def test_a_typed_code_behaves_identically(self):
        # A scanner is a keyboard; there is one path, not two.
        scanned = self.look("LIB-940001")
        typed = self.look("LIB-940001")

        self.assertEqual(
            scanned.context["loan"].id, typed.context["loan"].id
        )

    def test_the_code_is_matched_whatever_its_case(self):
        self.assertEqual(
            self.look("lib-940001").context["loan"].id, self.loan.id
        )

    def test_surrounding_space_is_ignored(self):
        self.assertEqual(
            self.look("  LIB-940001  ").context["loan"].id, self.loan.id
        )

    def test_an_exact_hit_does_not_run_the_search(self):
        response = self.look("LIB-940001")

        self.assertEqual(list(response.context["matches"]), [])

    def test_it_leads_into_the_existing_return_workflow(self):
        # By way of the return list, which is where the return happens
        # now - one book or five, it is the same one submission.
        response = self.look("LIB-940001")

        self.assertContains(response, "loans=%d" % self.loan.id)

    def test_adding_the_hit_and_submitting_returns_it(self):
        self.client.get(
            reverse("circulation_return_lookup"),
            {"copy_code": "LIB-940001", "loans": str(self.loan.id)},
        )

        response = self.client.post(
            reverse("circulation_return_lookup"),
            {
                "loans": str(self.loan.id),
                "return_date": self.today.isoformat(),
                "notes": "",
            },
        )

        self.assertEqual(response.status_code, 302)

        self.loan.refresh_from_db()
        self.copy.refresh_from_db()

        self.assertEqual(self.loan.return_date, self.today)
        self.assertEqual(self.copy.status, "Available")

    def test_a_returned_loan_is_no_longer_found(self):
        self.loan.return_date = self.today
        self.loan.save(update_fields=["return_date"])
        self.copy.status = "Available"
        self.copy.save(update_fields=["status"])

        response = self.look("LIB-940001")

        self.assertIsNone(response.context["loan"])
        self.assertContains(response, "not out on loan")

    def test_a_copy_that_was_never_lent_says_so(self):
        self.a_copy("LIB-940002")

        response = self.look("LIB-940002")

        self.assertIsNone(response.context["loan"])
        self.assertContains(response, "not out on loan")

    def test_an_unknown_code_says_so(self):
        response = self.look("LIB-999999")

        self.assertIsNone(response.context["loan"])
        self.assertEqual(list(response.context["matches"]), [])
        self.assertContains(response, "No copy found with that code")

    def test_the_lookup_is_one_indexed_query_not_a_search(self):
        for number in range(30):
            copy = self.a_copy("LIB-95%04d" % number, status="Issued")
            make_loan(copy=copy, borrower=self.borrower)

        with CaptureQueriesContext(connection) as queries:
            self.look("LIB-940001")

        loan_reads = [
            q["sql"] for q in queries.captured_queries
            if 'FROM "loans"' in q["sql"]
        ]

        self.assertEqual(len(loan_reads), 1)
        self.assertIn("copy_code", loan_reads[0])
        self.assertNotIn("LIKE", loan_reads[0].upper())


class ReturnSearchTests(ScanTestCase):
    """The other half of the field: a book in hand and no readable label."""

    def setUp(self):
        super().setUp()

        # Out on loan.
        self.out = self.a_copy("LIB-960001", status="Issued")
        self.loan = make_loan(
            copy=self.out,
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=20),
            due_date=self.today - timedelta(days=5),
        )

        # Same book, still on the shelf.
        self.shelved = self.a_copy("LIB-960002")

        # Same book, borrowed and brought back.
        self.history_copy = self.a_copy("LIB-960003")
        self.history = make_loan(
            copy=self.history_copy,
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=60),
            due_date=self.today - timedelta(days=46),
            return_date=self.today - timedelta(days=50),
        )

        # A different book, out, by a different author.
        self.other_book = make_book(
            title="Al-Muwatta", author=make_author("Malik ibn Anas")
        )
        self.other_volume = make_volume(
            book=self.other_book, volume_number=1, title=""
        )
        self.other_out = self.a_copy(
            "LIB-960004", status="Issued", volume=self.other_volume
        )
        self.other_loan = make_loan(
            copy=self.other_out,
            borrower=make_borrower(name="Bilal", phone="03450000002"),
            issue_date=self.today - timedelta(days=3),
            due_date=self.today + timedelta(days=11),
        )

    def look(self, term):
        return self.client.get(
            reverse("circulation_return_lookup"), {"copy_code": term}
        )

    def ids(self, response):
        return [loan.id for loan in response.context["matches"]]

    def test_a_full_book_title_finds_what_is_out(self):
        self.assertEqual(self.ids(self.look("Kitab al-Kharaj")), [self.loan.id])

    def test_part_of_a_book_title_finds_it_too(self):
        self.assertEqual(self.ids(self.look("Kharaj")), [self.loan.id])

    def test_the_title_search_ignores_case(self):
        self.assertEqual(self.ids(self.look("kharaj")), [self.loan.id])

    def test_a_full_author_name_finds_what_is_out(self):
        self.assertEqual(self.ids(self.look("Abu Yusuf")), [self.loan.id])

    def test_part_of_an_author_name_finds_it_too(self):
        self.assertEqual(self.ids(self.look("Yusuf")), [self.loan.id])

    def test_a_shelved_copy_is_not_a_result(self):
        listed = self.ids(self.look("Kharaj"))

        self.assertNotIn(self.shelved.id, [
            loan.copy_id for loan in self.look("Kharaj").context["matches"]
        ])
        self.assertEqual(listed, [self.loan.id])

    def test_a_returned_loan_is_not_a_result(self):
        self.assertNotIn(self.history.id, self.ids(self.look("Kharaj")))

    def test_another_book_is_not_a_result(self):
        self.assertNotIn(self.other_loan.id, self.ids(self.look("Kharaj")))

    def test_a_search_matching_two_books_lists_both(self):
        # Both authors share nothing, so search on something the titles do:
        # a term that reaches both books through their authors' loans.
        listed = set(self.ids(self.look("a")))

        self.assertIn(self.loan.id, listed)
        self.assertIn(self.other_loan.id, listed)

    def test_no_loan_appears_twice(self):
        # A book whose title and author both match the term would join
        # twice if this were built carelessly.
        book = make_book(title="Malik", author=make_author("Malik"))
        volume = make_volume(book=book, volume_number=1, title="")
        copy = self.a_copy("LIB-960005", status="Issued", volume=volume)
        loan = make_loan(copy=copy, borrower=self.borrower)

        listed = self.ids(self.look("Malik"))

        self.assertEqual(listed.count(loan.id), 1)

    def test_a_row_carries_what_is_needed_to_pick_it(self):
        response = self.look("Kharaj")

        for expected in (
            "LIB-960001",              # copy code
            "Kitab al-Kharaj",         # book
            "Abu Yusuf",               # author
            "Hafsa Rahmani",           # borrower
            # As the circulation UI prints dates - the loan list has
            # always used `j M Y`, and this page now agrees with it.
            date_format(self.loan.due_date, "j M Y"),
            "5 days late",             # overdue, in days
        ):
            with self.subTest(shows=expected):
                self.assertContains(response, expected)

    def test_an_overdue_row_says_how_late_it_is(self):
        response = self.look("Kharaj")

        match = response.context["matches"][0]

        self.assertEqual(match.days_overdue, 5)
        self.assertContains(response, "5 days late")

    def test_a_volume_is_named_when_there_is_one(self):
        volume = make_volume(
            book=self.book, volume_number=2, title="Land Tax"
        )
        copy = self.a_copy("LIB-960006", status="Issued", volume=volume)
        make_loan(copy=copy, borrower=self.borrower)

        response = self.look("Kharaj")

        self.assertContains(response, "Volume 2")
        self.assertContains(response, "Land Tax")

    def test_every_row_leads_into_the_existing_return_workflow(self):
        # By way of the return list, which is what lets a stack of books
        # be taken back in one action. The row adds itself; the list does
        # the returning.
        response = self.look("Kharaj")

        for match in response.context["matches"]:
            with self.subTest(loan=match.id):
                self.assertContains(response, "loans=%d" % match.id)

    def test_adding_a_row_puts_that_loan_in_the_return_list(self):
        response = self.look("Kharaj")
        match = response.context["matches"][0]

        added = self.client.get(
            reverse("circulation_return_lookup"),
            {"copy_code": "Kharaj", "loans": str(match.id)},
        )

        self.assertEqual(
            [item.id for item in added.context["chosen"]], [match.id]
        )

        # And it is not offered a second time.
        self.assertNotIn(
            match.id, [item.id for item in added.context["matches"]]
        )

    def test_the_list_is_what_actually_returns_the_book(self):
        match = self.look("Kharaj").context["matches"][0]

        response = self.client.post(
            reverse("circulation_return_lookup"),
            {
                "loans": str(match.id),
                "return_date": self.today.isoformat(),
                "notes": "",
            },
        )

        self.assertEqual(response.status_code, 302)

        match.refresh_from_db()
        match.copy.refresh_from_db()

        self.assertEqual(match.return_date, self.today)
        self.assertEqual(match.copy.status, "Available")

    def test_nothing_matching_says_so_without_blaming_the_label(self):
        response = self.look("Ibn Kathir")

        self.assertEqual(list(response.context["matches"]), [])
        self.assertContains(response, "nothing on loan matches")

    def test_the_results_are_capped(self):
        for number in range(RETURN_LOOKUP_LIMIT + 5):
            copy = self.a_copy("LIB-97%04d" % number, status="Issued")
            make_loan(copy=copy, borrower=self.borrower)

        response = self.look("Kharaj")

        self.assertEqual(
            len(response.context["matches"]), RETURN_LOOKUP_LIMIT
        )
        self.assertTrue(response.context["more_matches"])
        self.assertContains(response, "narrow the search")

    def test_the_search_costs_the_same_however_long_the_history(self):
        with CaptureQueriesContext(connection) as few:
            self.look("Kharaj")

        # Fifty loans, all returned, all on this book.
        for number in range(50):
            copy = self.a_copy("LIB-98%04d" % number)
            make_loan(
                copy=copy,
                borrower=self.borrower,
                issue_date=self.today - timedelta(days=90),
                due_date=self.today - timedelta(days=76),
                return_date=self.today - timedelta(days=80),
            )

        with CaptureQueriesContext(connection) as many:
            self.look("Kharaj")

        self.assertEqual(len(few), len(many))

    def test_the_rows_render_without_a_query_each(self):
        # One matching row.
        with CaptureQueriesContext(connection) as one_row:
            body = self.look("Kharaj").content.decode()

        self.assertIn("Abu Yusuf", body)
        self.assertIn("Hafsa Rahmani", body)

        # Seven.
        for number in range(6):
            copy = self.a_copy("LIB-99%04d" % number, status="Issued")
            make_loan(copy=copy, borrower=self.borrower)

        with CaptureQueriesContext(connection) as many_rows:
            body = self.look("Kharaj").content.decode()

        # Every one of the six is on the page. Counted by presence
        # rather than by occurrences: a row prints its code in the cell
        # and again as the add button's accessible name, the same as the
        # issue form's matches do.
        for number in range(6):
            self.assertIn("LIB-99%04d" % number, body)

        self.assertEqual(len(one_row), len(many_rows))

        # Two reads of `loans` either way: the code lookup that misses, then
        # the search. Never one per row - the book, the author and the
        # borrower all arrive with the loan.
        self.assertEqual(
            len([
                q for q in many_rows.captured_queries
                if 'FROM "loans"' in q["sql"]
            ]),
            2,
        )


class ScanPermissionTests(ScanTestCase):
    """The same permissions as the workflows they belong to. Nothing new."""

    def as_role(self, role):
        self.client.logout()
        make_user(
            username="s_%s" % role, password="pass12345", role=role
        )
        self.client.login(username="s_%s" % role, password="pass12345")

    def test_every_role_may_scan_on_the_issue_form(self):
        copy = self.a_copy("LIB-800001")

        for role in ("Admin", "Librarian", "Assistant"):
            with self.subTest(role=role):

                self.as_role(role)

                response = self.scan("LIB-800001")

                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.basket(response), [copy.id])

    def test_every_role_may_search_on_the_return_form(self):
        copy = self.a_copy("LIB-800002", status="Issued")
        make_loan(copy=copy, borrower=self.borrower)

        for role in ("Admin", "Librarian", "Assistant"):
            with self.subTest(role=role):

                self.as_role(role)

                response = self.client.get(
                    reverse("circulation_return_lookup"),
                    {"copy_code": "Kharaj"},
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [loan.copy_id for loan in response.context["matches"]],
                    [copy.id],
                )

    def test_a_signed_out_visitor_gets_neither(self):
        self.a_copy("LIB-800003")
        self.client.logout()

        for name, params in (
            ("circulation_issue", {"q": "LIB-800003"}),
            ("circulation_return_lookup", {"copy_code": "Kharaj"}),
        ):
            with self.subTest(page=name):

                response = self.client.get(reverse(name), params)

                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])

    def test_neither_path_is_a_way_round_a_role_check(self):
        # Nothing here is a new privilege: the pages are the ones all three
        # roles could already open, and the restricted parts of circulation
        # stay restricted.
        copy = self.a_copy("LIB-800004", status="Issued")
        loan = make_loan(copy=copy, borrower=self.borrower)

        self.as_role("Assistant")

        self.assertEqual(
            self.client.get(
                reverse("circulation_return_lookup"),
                {"copy_code": "LIB-800004"},
            ).status_code,
            200,
        )

        for name in ("loan_renew", "loan_delete"):
            with self.subTest(view=name):
                self.assertEqual(
                    self.client.get(
                        reverse(name, args=[loan.id])
                    ).status_code,
                    403,
                )


class CopyCodeIdentityTests(ScanTestCase):
    """One identifier, and the properties a label will depend on."""

    def test_the_code_is_unique_in_the_database(self):
        from django.db import IntegrityError, transaction

        self.a_copy("LIB-700001")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.a_copy("LIB-700001")

    def test_the_code_survives_being_issued_and_returned(self):
        copy = self.a_copy("LIB-700002")
        loan = make_loan(copy=copy, borrower=self.borrower)

        self.client.post(
            reverse("loan_return", args=[loan.id]),
            {"return_date": self.today.isoformat()},
        )

        copy.refresh_from_db()
        self.assertEqual(copy.copy_code, "LIB-700002")

    def test_the_same_code_serves_both_workflows(self):
        # Issue finds it, and after it is out, Return finds it - with no
        # second identifier anywhere between them.
        copy = self.a_copy("LIB-700003")

        self.assertEqual(self.basket(self.scan("LIB-700003")), [copy.id])

        make_loan(copy=copy, borrower=self.borrower)
        copy.status = "Issued"
        copy.save(update_fields=["status"])

        response = self.client.get(
            reverse("circulation_return_lookup"),
            {"copy_code": "LIB-700003"},
        )

        self.assertEqual(response.context["loan"].copy_id, copy.id)


class MultiBookReturnTests(ScanTestCase):
    """A stack of books, one submission - the issue basket's shape.

    Issue has held more than one copy since it grew a basket; this is the
    same idea on the way back, and the reason it is worth having is the
    common case at the desk: somebody arrives with everything they
    borrowed.
    """

    def setUp(self):
        super().setUp()

        # Three books out to one person, and one out to somebody else.
        self.first = self.a_copy("LIB-910001", status="Issued")
        self.second = self.a_copy("LIB-910002", status="Issued")
        self.third = self.a_copy("LIB-910003", status="Issued")

        self.loans = [
            make_loan(
                copy=copy,
                borrower=self.borrower,
                issue_date=self.today - timedelta(days=10),
                due_date=self.today + timedelta(days=4),
            )
            for copy in (self.first, self.second, self.third)
        ]

        self.someone_else = make_borrower(
            name="Bilal Qureshi", phone="03450000009"
        )
        self.theirs_copy = self.a_copy("LIB-910004", status="Issued")
        self.theirs = make_loan(
            copy=self.theirs_copy,
            borrower=self.someone_else,
            issue_date=self.today - timedelta(days=2),
            due_date=self.today + timedelta(days=12),
        )

    def look(self, term, loans=()):
        params = {"copy_code": term}

        if loans:
            params["loans"] = [str(value) for value in loans]

        return self.client.get(
            reverse("circulation_return_lookup"), params
        )

    def take_back(self, loans, when=None, notes=""):
        return self.client.post(
            reverse("circulation_return_lookup"),
            {
                "loans": [str(value) for value in loans],
                "return_date": (when or self.today).isoformat(),
                "notes": notes,
            },
        )

    # ------------------------------------------------ finding the stack

    def test_a_borrower_name_lists_everything_they_have_out(self):
        # The search the multi-return exists for: one name, three books.
        found = self.look("Hafsa").context["matches"]

        self.assertEqual(
            sorted(item.id for item in found),
            sorted(loan.id for loan in self.loans),
        )

    def test_somebody_elses_book_is_not_in_that_list(self):
        found = self.look("Hafsa").context["matches"]

        self.assertNotIn(self.theirs.id, [item.id for item in found])

    def test_searching_again_keeps_what_was_already_added(self):
        # The whole point of the basket: finding the second book must not
        # lose the first.
        response = self.look("Al-Muwatta", loans=[self.loans[0].id])

        self.assertEqual(
            [item.id for item in response.context["chosen"]],
            [self.loans[0].id],
        )

    def test_a_loan_returned_at_another_desk_drops_out_of_the_list(self):
        # Held in the query string, so it is re-read rather than trusted.
        self.loans[0].return_date = self.today
        self.loans[0].save(update_fields=["return_date"])

        response = self.look("", loans=[loan.id for loan in self.loans])

        self.assertEqual(
            [item.id for item in response.context["chosen"]],
            [self.loans[1].id, self.loans[2].id],
        )

    def test_the_list_may_hold_more_than_one_borrower(self):
        # A drop-box shelf is not one person's, and nothing needs the
        # borrowers to match.
        response = self.look(
            "", loans=[self.loans[0].id, self.theirs.id]
        )

        self.assertEqual(
            sorted(item.id for item in response.context["chosen"]),
            sorted([self.loans[0].id, self.theirs.id]),
        )

    # ------------------------------------------------ taking them back

    def test_three_books_come_back_in_one_submission(self):
        response = self.take_back([loan.id for loan in self.loans])

        self.assertEqual(response.status_code, 302)

        for loan, copy in zip(self.loans, (self.first, self.second, self.third)):
            with self.subTest(copy=copy.copy_code):
                loan.refresh_from_db()
                copy.refresh_from_db()

                self.assertEqual(loan.return_date, self.today)
                self.assertEqual(loan.returned_to_id, self.librarian.id)
                self.assertEqual(copy.status, "Available")

    def test_one_book_still_comes_back_on_its_own(self):
        self.take_back([self.loans[0].id])

        self.loans[0].refresh_from_db()
        self.first.refresh_from_db()

        self.assertEqual(self.loans[0].return_date, self.today)
        self.assertEqual(self.first.status, "Available")

        # And the others are untouched.
        for loan in self.loans[1:]:
            loan.refresh_from_db()
            self.assertIsNone(loan.return_date)

    def test_the_note_and_the_date_apply_to_every_book(self):
        when = self.today - timedelta(days=1)

        self.take_back(
            [loan.id for loan in self.loans[:2]],
            when=when,
            notes="Water damage to both",
        )

        for loan in self.loans[:2]:
            loan.refresh_from_db()

            self.assertEqual(loan.return_date, when)
            self.assertEqual(loan.notes, "Water damage to both")

    def test_the_returns_are_logged_one_entry_each(self):
        from library.models import ActivityLog

        before = ActivityLog.objects.filter(action="RETURN").count()

        self.take_back([loan.id for loan in self.loans])

        self.assertEqual(
            ActivityLog.objects.filter(action="RETURN").count(),
            before + 3,
        )

    # ------------------------------------------- all of it, or none of it

    def test_a_book_already_returned_rolls_the_whole_stack_back(self):
        # Somebody else took one back between the search and the submit.
        # A partial answer would leave the librarian holding two books and
        # knowing only that something went wrong.
        self.loans[1].return_date = self.today
        self.loans[1].save(update_fields=["return_date"])

        response = self.take_back([loan.id for loan in self.loans])

        self.assertEqual(response.status_code, 200)

        # Said by name, so the librarian knows which row to take out.
        self.assertContains(response, "LIB-910002")
        self.assertContains(response, "already been returned")

        for loan, copy in ((self.loans[0], self.first), (self.loans[2], self.third)):
            with self.subTest(copy=copy.copy_code):
                loan.refresh_from_db()
                copy.refresh_from_db()

                self.assertIsNone(loan.return_date)
                self.assertEqual(copy.status, "Issued")

    def test_a_date_before_one_book_went_out_refuses_the_whole_stack(self):
        # The rule `loan_return` already applied, held to per book and
        # naming the one that could not take the date.
        late_copy = self.a_copy("LIB-910005", status="Issued")
        late = make_loan(
            copy=late_copy,
            borrower=self.borrower,
            issue_date=self.today,
            due_date=self.today + timedelta(days=14),
        )

        response = self.take_back(
            [self.loans[0].id, late.id],
            when=self.today - timedelta(days=5),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "LIB-910005")
        self.assertContains(response, "earlier than issue date")

        for loan in (self.loans[0], late):
            loan.refresh_from_db()
            self.assertIsNone(loan.return_date)

    def test_an_unparseable_date_records_nothing(self):
        response = self.client.post(
            reverse("circulation_return_lookup"),
            {
                "loans": [str(self.loans[0].id)],
                "return_date": "not-a-date",
                "notes": "",
            },
        )

        self.assertEqual(response.status_code, 200)

        self.loans[0].refresh_from_db()
        self.assertIsNone(self.loans[0].return_date)

    def test_an_empty_list_is_refused_rather_than_silently_accepted(self):
        response = self.client.post(
            reverse("circulation_return_lookup"),
            {"return_date": self.today.isoformat(), "notes": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nothing is in the return list")

    def test_the_same_book_twice_in_the_list_is_returned_once(self):
        response = self.take_back(
            [self.loans[0].id, self.loans[0].id]
        )

        self.assertEqual(response.status_code, 302)

        self.loans[0].refresh_from_db()
        self.assertEqual(self.loans[0].return_date, self.today)

    # ------------------------------------------------------ permissions

    def test_every_role_that_could_return_one_can_return_several(self):
        # No new privilege: the page and the workflow are the ones all
        # three roles already had.
        for role in ("Admin", "Librarian", "Assistant"):
            with self.subTest(role=role):

                copy = self.a_copy(
                    "LIB-92%04d" % ("Admin", "Librarian", "Assistant").index(role),
                    status="Issued",
                )
                loan = make_loan(
                    copy=copy,
                    borrower=self.borrower,
                    issue_date=self.today - timedelta(days=3),
                    due_date=self.today + timedelta(days=11),
                )

                user = make_user(
                    username="multi_%s" % role.lower(),
                    password="pass12345",
                    role=role,
                )
                self.client.force_login(user)

                self.take_back([loan.id])

                loan.refresh_from_db()
                self.assertEqual(loan.return_date, self.today)

    def test_a_signed_out_visitor_cannot_return_anything(self):
        self.client.logout()

        response = self.take_back([self.loans[0].id])

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

        self.loans[0].refresh_from_db()
        self.assertIsNone(self.loans[0].return_date)
