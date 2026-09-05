"""The issue and return workflow: lookup, review, commit, and who may.

The issue form used to render every available copy in the library and had no
borrower control at all, so it could not complete through the UI. Both the
borrower and the chosen copies now live in the query string, which is what
lets a search add to the selection instead of replacing it.
"""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library.models import BookCopy, Loan
from library.views import COPY_LOOKUP_LIMIT, issuable_copies
from library.tests.helpers import (
    make_author,
    make_book,
    make_borrower,
    make_copy,
    make_loan,
    make_shelf,
    make_user,
    make_volume,
)


class CirculationTestCase(TestCase):

    def setUp(self):
        self.librarian = make_user(
            username="librarian_u", password="pass12345", role="Librarian"
        )
        self.client.login(username="librarian_u", password="pass12345")

        self.shelf = make_shelf()
        self.book = make_book(title="Kitab al-Kharaj", author=make_author("Abu Yusuf"))
        self.volume = make_volume(book=self.book, volume_number=1)
        self.copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-900001"
        )
        self.borrower = make_borrower(name="Hafsa Rahmani", phone="03450000001")

        self.url = reverse("loan_add")

    def get(self, **params):
        return self.client.get(self.url, params)

    def issue(self, **overrides):
        today = timezone.now().date()
        data = {
            "borrower": self.borrower.id,
            "copies": [str(self.copy.id)],
            "issue_date": today.isoformat(),
            "due_date": (today + timedelta(days=14)).isoformat(),
            "notes": "",
        }
        data.update(overrides)
        return self.client.post(self.url, data)


class IssueFormTests(CirculationTestCase):

    def test_the_borrower_control_exists(self):
        # It did not exist at all, so the form always answered "please
        # select a borrower" and no loan could be made through the page.
        # It is now the searchable dropdown, which still posts an id in a
        # field named `borrower`.
        response = self.get()

        self.assertContains(response, 'name="borrower"')
        self.assertContains(response, "data-combobox")
        self.assertContains(response, reverse("borrower_list"))

    def test_a_chosen_borrower_is_shown_back(self):
        response = self.get(borrower=self.borrower.id)

        self.assertEqual(response.context["borrower_name"], "Hafsa Rahmani")
        self.assertContains(response, "Hafsa Rahmani")

    def test_a_nonsense_borrower_in_the_url_does_not_break_the_page(self):
        response = self.get(borrower="abc")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["borrower_name"], "")

    def test_the_borrowers_are_not_all_loaded_into_the_page(self):
        for number in range(15):
            make_borrower(
                name="Filler %02d" % number, phone="0345800%04d" % number
            )

        response = self.get()

        self.assertNotIn("borrowers", response.context)
        self.assertNotContains(response, "Filler 07")

    def test_nothing_is_listed_until_something_is_searched(self):
        response = self.get()

        self.assertEqual(list(response.context["matches"]), [])
        self.assertNotContains(response, "LIB-900001")
        self.assertContains(response, "added to the list straight away")

    def test_a_whole_copy_code_is_added_rather_than_listed(self):
        # What a scanner sends. It used to come back as a table with one row
        # in it; it now comes back with the copy in the basket - see
        # ScanOutcome in views.py.
        response = self.get(q="LIB-900001")

        self.assertEqual(response.status_code, 302)
        self.assertIn("copies=%d" % self.copy.id, response["Location"])

        # And the code is gone from the box, ready for the next one.
        self.assertNotIn("q=", response["Location"])

    def test_a_copy_is_found_by_part_of_its_code(self):
        self.assertEqual(
            [c.id for c in self.get(q="900001").context["matches"]],
            [self.copy.id],
        )

    def test_a_copy_is_found_by_book_title(self):
        self.assertEqual(
            [c.id for c in self.get(q="Kharaj").context["matches"]],
            [self.copy.id],
        )

    def test_a_copy_is_found_by_author(self):
        self.assertEqual(
            [c.id for c in self.get(q="Abu Yusuf").context["matches"]],
            [self.copy.id],
        )

    def test_the_row_shows_enough_to_pick_the_right_copy(self):
        # A partial code still searches, so this is where a row is seen.
        response = self.get(q="900001")

        self.assertContains(response, "Kitab al-Kharaj")
        self.assertContains(response, "Abu Yusuf")
        self.assertContains(response, self.shelf.shelf_code)

    def test_no_match_says_why_it_might_be_missing(self):
        response = self.get(q="LIB-999999")

        self.assertEqual(list(response.context["matches"]), [])
        self.assertContains(response, "withdrawn")

    def test_the_results_are_capped(self):
        for number in range(COPY_LOOKUP_LIMIT + 5):
            make_copy(
                volume=self.volume, shelf=self.shelf,
                copy_code="LIB-95%04d" % number,
            )

        response = self.get(q="LIB-95")

        self.assertEqual(len(response.context["matches"]), COPY_LOOKUP_LIMIT)
        self.assertTrue(response.context["more_matches"])
        self.assertContains(response, "narrow the search")


class UnissuableCopiesTests(CirculationTestCase):

    def test_a_copy_already_out_is_not_offered(self):
        make_loan(copy=self.copy)

        self.assertEqual(list(self.get(q="LIB-900001").context["matches"]), [])

    def test_a_withdrawn_copy_is_not_offered(self):
        self.copy.status = BookCopy.STATUS_TRANSFERRED
        self.copy.save(update_fields=["status"])

        self.assertEqual(list(self.get(q="LIB-900001").context["matches"]), [])

    def test_a_copy_of_an_archived_book_is_not_offered(self):
        self.book.archived_at = timezone.now()
        self.book.save(update_fields=["archived_at"])

        self.assertEqual(list(self.get(q="LIB-900001").context["matches"]), [])

    def test_a_withdrawn_copy_cannot_be_issued_by_posting_it(self):
        self.copy.status = BookCopy.STATUS_TRANSFERRED
        self.copy.save(update_fields=["status"])
        before = Loan.objects.count()

        response = self.issue()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Loan.objects.count(), before)
        self.assertContains(response, "no longer available")

    def test_a_copy_of_an_archived_book_cannot_be_issued_by_posting_it(self):
        # The status column still says Available, so only the archived
        # check stops this one - it was reachable before.
        self.book.archived_at = timezone.now()
        self.book.save(update_fields=["archived_at"])
        before = Loan.objects.count()

        response = self.issue()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Loan.objects.count(), before)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Available")

    def test_the_lendable_rule_is_one_definition(self):
        self.assertIn(self.copy, issuable_copies())

        self.book.archived_at = timezone.now()
        self.book.save(update_fields=["archived_at"])

        self.assertNotIn(self.copy, issuable_copies())

    def test_an_inactive_borrower_cannot_be_issued_to(self):
        self.borrower.is_active = False
        self.borrower.save(update_fields=["is_active"])
        before = Loan.objects.count()

        response = self.issue()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Loan.objects.count(), before)
        self.assertContains(response, "Invalid or inactive borrower")

    def test_a_nonsense_borrower_is_handled_cleanly(self):
        for value in ("abc", "999999", ""):
            with self.subTest(value=value):
                response = self.issue(borrower=value)

                self.assertEqual(response.status_code, 200)
                self.assertFalse(Loan.objects.exists())

    def test_a_nonsense_copy_id_is_handled_cleanly(self):
        for value in ("abc", "999999"):
            with self.subTest(value=value):
                response = self.issue(copies=[value])

                self.assertEqual(response.status_code, 200)
                self.assertFalse(Loan.objects.exists())


class SelectionTests(CirculationTestCase):

    def setUp(self):
        super().setUp()
        self.second = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-900002"
        )

    def test_a_chosen_copy_appears_in_the_review(self):
        response = self.get(copies=self.copy.id)

        self.assertEqual(
            [c.id for c in response.context["chosen"]], [self.copy.id]
        )

    def test_a_second_search_keeps_the_first_choice(self):
        # A partial code, because a whole one is added rather than listed.
        response = self.client.get(
            self.url,
            {"copies": [str(self.copy.id)], "q": "900002"},
        )

        self.assertEqual(
            [c.id for c in response.context["chosen"]], [self.copy.id]
        )
        self.assertEqual(
            [c.id for c in response.context["matches"]], [self.second.id]
        )

    def test_a_chosen_copy_is_not_offered_again(self):
        response = self.client.get(
            self.url, {"copies": [str(self.copy.id)], "q": "LIB-9000"}
        )

        ids = [c.id for c in response.context["matches"]]
        self.assertNotIn(self.copy.id, ids)
        self.assertIn(self.second.id, ids)

    def test_the_add_link_carries_the_existing_choice(self):
        response = self.client.get(
            self.url, {"copies": [str(self.copy.id)], "q": "900002"}
        )

        add_url = response.context["matches"][0].add_url

        self.assertIn("copies=%d" % self.copy.id, add_url)
        self.assertIn("copies=%d" % self.second.id, add_url)

    def test_the_remove_link_drops_only_that_copy(self):
        response = self.client.get(
            self.url,
            {"copies": [str(self.copy.id), str(self.second.id)]},
        )

        first = next(
            c for c in response.context["chosen"] if c.id == self.copy.id
        )

        self.assertNotIn("copies=%d" % self.copy.id, first.remove_url)
        self.assertIn("copies=%d" % self.second.id, first.remove_url)

    def test_a_choice_that_went_out_meanwhile_drops_off(self):
        make_loan(copy=self.copy)

        response = self.client.get(
            self.url, {"copies": [str(self.copy.id), str(self.second.id)]}
        )

        self.assertEqual(
            [c.id for c in response.context["chosen"]], [self.second.id]
        )

    def test_the_borrower_survives_a_copy_search(self):
        response = self.client.get(
            self.url, {"borrower": str(self.borrower.id), "q": "900001"}
        )

        self.assertEqual(response.context["borrower_id"], str(self.borrower.id))
        self.assertContains(response, "Hafsa Rahmani")

    def test_the_issue_button_waits_for_both(self):
        self.assertContains(self.get(), "disabled")

        ready = self.client.get(
            self.url,
            {"borrower": str(self.borrower.id), "copies": [str(self.copy.id)]},
        )

        self.assertNotContains(ready, "disabled")


class IssueAndReturnTests(CirculationTestCase):

    def test_a_valid_issue_succeeds(self):
        response = self.issue()

        self.assertEqual(response.status_code, 302)
        loan = Loan.objects.get(copy=self.copy, return_date__isnull=True)
        self.assertEqual(loan.borrower_id, self.borrower.id)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Issued")

    def test_the_signed_in_user_is_recorded_as_issuing_it(self):
        other = make_user(username="someone_else", password="pass12345",
                          role="Librarian")

        self.issue(issued_by=str(other.id))

        loan = Loan.objects.get(copy=self.copy)
        self.assertEqual(loan.issued_by_id, self.librarian.id)

    def test_a_second_issue_of_the_same_copy_is_refused(self):
        self.issue()
        before = Loan.objects.count()

        response = self.issue()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Loan.objects.count(), before)
        self.assertEqual(
            Loan.objects.filter(
                copy=self.copy, return_date__isnull=True
            ).count(),
            1,
        )

    def test_the_lookup_finds_the_active_loan(self):
        self.issue()

        response = self.client.get(
            reverse("circulation_return_lookup"), {"copy_code": "LIB-900001"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["loan"])
        self.assertContains(response, "Hafsa Rahmani")
        self.assertContains(response, "Kitab al-Kharaj")

    def test_the_lookup_is_case_insensitive(self):
        self.issue()

        response = self.client.get(
            reverse("circulation_return_lookup"), {"copy_code": "lib-900001"}
        )

        self.assertIsNotNone(response.context["loan"])

    def test_an_unknown_code_says_so(self):
        response = self.client.get(
            reverse("circulation_return_lookup"), {"copy_code": "LIB-000000"}
        )

        self.assertIsNone(response.context["loan"])
        self.assertContains(response, "No copy found with that code")

    def test_a_copy_that_is_not_out_says_so(self):
        response = self.client.get(
            reverse("circulation_return_lookup"), {"copy_code": "LIB-900001"}
        )

        self.assertIsNone(response.context["loan"])
        self.assertContains(response, "not out on loan")

    def test_a_returned_copy_is_no_longer_found_by_the_lookup(self):
        self.issue()
        loan = Loan.objects.get(copy=self.copy)
        today = timezone.now().date()
        self.client.post(
            reverse("loan_return", args=[loan.id]),
            {"return_date": today.isoformat(), "notes": ""},
        )

        response = self.client.get(
            reverse("circulation_return_lookup"), {"copy_code": "LIB-900001"}
        )

        self.assertIsNone(response.context["loan"])

    def test_a_valid_return_succeeds_and_records_the_signed_in_user(self):
        self.issue()
        loan = Loan.objects.get(copy=self.copy)
        today = timezone.now().date()

        response = self.client.post(
            reverse("loan_return", args=[loan.id]),
            {"return_date": today.isoformat(), "returned_to": "999999",
             "notes": ""},
        )

        self.assertEqual(response.status_code, 302)
        loan.refresh_from_db()
        self.assertEqual(loan.return_date, today)
        self.assertEqual(loan.returned_to_id, self.librarian.id)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Available")

    def test_returning_twice_does_not_move_the_first_date(self):
        self.issue()
        loan = Loan.objects.get(copy=self.copy)
        today = timezone.now().date()
        self.client.post(
            reverse("loan_return", args=[loan.id]),
            {"return_date": today.isoformat(), "notes": ""},
        )

        self.client.post(
            reverse("loan_return", args=[loan.id]),
            {"return_date": (today - timedelta(days=1)).isoformat(),
             "notes": ""},
        )

        loan.refresh_from_db()
        self.assertEqual(loan.return_date, today)

    def test_the_history_survives_a_return(self):
        self.issue()
        loan = Loan.objects.get(copy=self.copy)
        today = timezone.now().date()

        self.client.post(
            reverse("loan_return", args=[loan.id]),
            {"return_date": today.isoformat(), "notes": ""},
        )

        self.assertTrue(Loan.objects.filter(id=loan.id).exists())
        self.assertEqual(
            Loan.objects.get(id=loan.id).borrower_id, self.borrower.id
        )

    def test_a_returned_copy_can_be_issued_again(self):
        self.issue()
        loan = Loan.objects.get(copy=self.copy)
        today = timezone.now().date()
        self.client.post(
            reverse("loan_return", args=[loan.id]),
            {"return_date": today.isoformat(), "notes": ""},
        )

        response = self.issue()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Loan.objects.filter(copy=self.copy).count(), 2)


class CirculationPermissionTests(TestCase):
    """One policy across the workflow: all three roles issue and return."""

    def setUp(self):
        self.copy = make_copy(copy_code="PERM-9001", shelf=make_shelf())
        self.loan = make_loan(copy=self.copy)

    def as_role(self, role):
        make_user(username="%s_u" % role.lower(), password="pass12345",
                  role=role)
        client = self.client
        client.login(username="%s_u" % role.lower(), password="pass12345")
        return client

    def test_every_role_can_reach_the_whole_issue_and_return_workflow(self):
        for role in ("Admin", "Librarian", "Assistant"):
            client = self.as_role(role)

            for name, url in (
                ("dashboard", reverse("circulation_dashboard")),
                ("issue", reverse("loan_add")),
                ("return lookup", reverse("circulation_return_lookup")),
                ("loan list", reverse("loan_list")),
                ("return", reverse("loan_return", args=[self.loan.id])),
            ):
                with self.subTest(role=role, page=name):
                    self.assertEqual(client.get(url).status_code, 200)

    def test_deleting_a_loan_stays_restricted(self):
        client = self.as_role("Assistant")

        self.assertEqual(
            client.get(reverse("loan_delete", args=[self.loan.id])).status_code,
            403,
        )

    def test_renewing_stays_restricted(self):
        client = self.as_role("Assistant")

        self.assertEqual(
            client.get(reverse("loan_renew", args=[self.loan.id])).status_code,
            403,
        )

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        self.client.logout()

        response = self.client.get(reverse("loan_add"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])


class QueryCostTests(CirculationTestCase):

    def test_the_form_does_not_grow_with_the_number_of_copies(self):
        with CaptureQueriesContext(connection) as few:
            self.get(q="LIB-9")

        for number in range(40):
            book = make_book(
                title="Filler %02d" % number,
                author=make_author("Filler Author %d" % number),
            )
            volume = make_volume(book=book, volume_number=1)
            make_copy(
                volume=volume, shelf=self.shelf,
                copy_code="LIB-96%04d" % number,
            )

        with CaptureQueriesContext(connection) as many:
            self.get(q="LIB-9")

        self.assertEqual(len(many), len(few))

    def test_the_empty_form_does_not_read_the_copies_table(self):
        with CaptureQueriesContext(connection) as ctx:
            self.get()

        reads = [
            q for q in ctx.captured_queries
            if 'FROM "book_copies"' in q["sql"]
        ]

        self.assertEqual(reads, [])

    def test_the_review_does_not_cost_a_query_per_copy(self):
        extra = [
            make_copy(
                volume=self.volume, shelf=self.shelf,
                copy_code="LIB-97%04d" % number,
            )
            for number in range(6)
        ]

        with CaptureQueriesContext(connection) as one:
            self.client.get(self.url, {"copies": [str(self.copy.id)]})

        with CaptureQueriesContext(connection) as several:
            self.client.get(
                self.url,
                {"copies": [str(c.id) for c in [self.copy] + extra]},
            )

        self.assertEqual(len(several), len(one))
