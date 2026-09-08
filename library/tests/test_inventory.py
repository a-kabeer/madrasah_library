"""Stock checks: what a session expects, what it counts, what it reports.

The rule these all circle is that a stock check changes nothing. It reads
the shelves, records what a person picked up, and reports the difference.
A copy that does not turn up keeps the status it had; deciding it is
Missing stays the librarian's action on the copy's own page.

The other rule is that counting the same book twice cannot inflate the
count, and that is held by the database - `unique_found_copy_per_session`
- not by a check in Python that two requests could both pass.
"""

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library import inventory
from library.models import (
    BookCopy,
    InventoryScan,
    InventorySession,
)

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


class StockCheckTestCase(TestCase):

    def setUp(self):
        self.librarian = make_user(
            username="librarian_u", password="pass12345", role="Librarian"
        )
        self.client.login(username="librarian_u", password="pass12345")

        self.hall = make_location(name="Main Hall")
        self.annex = make_location(name="Annex")

        self.shelf_a = make_shelf(location=self.hall, shelf_code="A-1")
        self.shelf_b = make_shelf(location=self.hall, shelf_code="B-1")
        self.shelf_c = make_shelf(location=self.annex, shelf_code="C-1")

        self.author = make_author("Abu Yusuf")
        self.book = make_book(title="Kitab al-Kharaj", author=self.author)
        self.volume = make_volume(book=self.book, volume_number=1, title="")

        self.today = timezone.now().date()

    # `None` is a real answer for a shelf - an unshelved copy - so "not
    # specified" needs a value of its own.
    DEFAULT_SHELF = object()

    def a_copy(self, code, shelf=DEFAULT_SHELF, status="Available",
               volume=None):
        return make_copy(
            volume=volume or self.volume,
            shelf=self.shelf_a if shelf is self.DEFAULT_SHELF else shelf,
            copy_code=code,
            status=status,
        )

    def a_session(self, scope="library", location=None, shelf=None,
                  name="September count", completed=False):
        session = InventorySession.objects.create(
            name=name,
            scope=scope,
            location=location,
            shelf=shelf,
            status=InventorySession.STATUS_IN_PROGRESS,
            started_by=self.librarian,
            started_at=timezone.now(),
        )

        if completed:
            session.status = InventorySession.STATUS_COMPLETED
            session.completed_at = timezone.now()
            session.save(update_fields=["status", "completed_at"])

        return session

    def scan(self, session, code):
        return self.client.post(
            reverse("inventory_session_scan", args=[session.id]),
            {"copy_code": code},
            follow=True,
        )

    def detail(self, session):
        return self.client.get(
            reverse("inventory_session_detail", args=[session.id])
        )

    def as_role(self, role):
        self.client.logout()
        make_user(username="s_%s" % role, password="pass12345", role=role)
        self.client.login(username="s_%s" % role, password="pass12345")


class SessionLifecycleTests(StockCheckTestCase):

    def test_a_session_can_be_started(self):
        response = self.client.post(
            reverse("inventory_session_start"),
            {"name": "Hall sweep", "scope": "library"},
        )

        self.assertEqual(response.status_code, 302)

        session = InventorySession.objects.get()

        self.assertEqual(session.name, "Hall sweep")
        self.assertTrue(session.is_open)
        self.assertEqual(session.started_by, self.librarian)
        self.assertIsNone(session.completed_at)

    def test_it_needs_a_name(self):
        response = self.client.post(
            reverse("inventory_session_start"),
            {"name": "  ", "scope": "library"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Give the stock check a name")
        self.assertFalse(InventorySession.objects.exists())

    def test_a_location_check_needs_a_location(self):
        response = self.client.post(
            reverse("inventory_session_start"),
            {"name": "Hall", "scope": "location"},
        )

        self.assertContains(response, "Choose the location")
        self.assertFalse(InventorySession.objects.exists())

    def test_a_shelf_check_needs_a_shelf(self):
        response = self.client.post(
            reverse("inventory_session_start"),
            {"name": "A-1", "scope": "shelf"},
        )

        self.assertContains(response, "Choose the shelf")
        self.assertFalse(InventorySession.objects.exists())

    def test_an_open_session_can_be_resumed(self):
        session = self.a_session()

        response = self.detail(session)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Scan or type a copy code")

    def test_it_appears_in_the_list(self):
        session = self.a_session(name="Findable")

        response = self.client.get(reverse("inventory_session_list"))

        self.assertContains(response, "Findable")
        self.assertContains(
            response, reverse("inventory_session_detail", args=[session.id])
        )

    def test_completing_records_when(self):
        session = self.a_session()

        response = self.client.post(
            reverse("inventory_session_complete", args=[session.id])
        )

        self.assertEqual(response.status_code, 302)

        session.refresh_from_db()

        self.assertEqual(session.status, InventorySession.STATUS_COMPLETED)
        self.assertIsNotNone(session.completed_at)

    def test_completing_twice_is_safe(self):
        session = self.a_session()

        self.client.post(
            reverse("inventory_session_complete", args=[session.id])
        )
        session.refresh_from_db()
        first = session.completed_at

        self.client.post(
            reverse("inventory_session_complete", args=[session.id])
        )
        session.refresh_from_db()

        self.assertEqual(session.completed_at, first)

    def test_a_completed_session_shows_its_report_not_a_scan_box(self):
        session = self.a_session(completed=True)

        response = self.detail(session)

        self.assertNotContains(response, "Scan or type a copy code")
        self.assertContains(response, "Not found")

    def test_the_database_refuses_a_completed_session_with_no_time(self):
        # The rule is a CHECK, not a convention.
        session = self.a_session()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InventorySession.objects.filter(id=session.id).update(
                    status=InventorySession.STATUS_COMPLETED
                )

    def test_the_database_refuses_an_incoherent_scope(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InventorySession.objects.create(
                    name="Nonsense",
                    scope=InventorySession.SCOPE_SHELF,
                    location=None,
                    shelf=None,
                    status=InventorySession.STATUS_IN_PROGRESS,
                    started_at=timezone.now(),
                )


class ScopeTests(StockCheckTestCase):

    def setUp(self):
        super().setUp()

        self.on_a = self.a_copy("INV-0001", shelf=self.shelf_a)
        self.on_b = self.a_copy("INV-0002", shelf=self.shelf_b)
        self.in_annex = self.a_copy("INV-0003", shelf=self.shelf_c)
        self.unshelved = self.a_copy("INV-0004", shelf=None)

    def expected(self, session):
        return set(
            inventory.expected_copies(session).values_list("id", flat=True)
        )

    def test_a_library_check_expects_everything(self):
        session = self.a_session(scope="library")

        self.assertEqual(
            self.expected(session),
            {self.on_a.id, self.on_b.id, self.in_annex.id,
             self.unshelved.id},
        )

    def test_a_location_check_expects_its_shelves(self):
        session = self.a_session(scope="location", location=self.hall)

        self.assertEqual(
            self.expected(session), {self.on_a.id, self.on_b.id}
        )

    def test_a_shelf_check_expects_that_shelf(self):
        session = self.a_session(scope="shelf", shelf=self.shelf_a)

        self.assertEqual(self.expected(session), {self.on_a.id})

    def test_an_archived_book_is_still_on_the_shelf(self):
        # Archiving takes a book out of the catalogue, not the building.
        self.book.archived_at = timezone.now()
        self.book.save(update_fields=["archived_at"])

        session = self.a_session(scope="shelf", shelf=self.shelf_a)

        self.assertIn(self.on_a.id, self.expected(session))

    def test_copies_of_every_status_are_expected(self):
        # A stock check is exactly when a Lost copy turns up again, and a
        # copy out on loan is reported as not found with the status that
        # says why. Neither is filtered out in advance.
        lost = self.a_copy("INV-0005", shelf=self.shelf_a, status="Lost")
        issued = self.a_copy("INV-0006", shelf=self.shelf_a, status="Issued")

        session = self.a_session(scope="shelf", shelf=self.shelf_a)

        self.assertIn(lost.id, self.expected(session))
        self.assertIn(issued.id, self.expected(session))

    def test_the_expected_count_is_counted_not_loaded(self):
        session = self.a_session(scope="library")

        for number in range(30):
            self.a_copy("INV-1%03d" % number)

        with CaptureQueriesContext(connection) as queries:
            counts = inventory.session_counts(session)

        self.assertEqual(counts["expected"], 34)

        # Two queries whatever the size of the library: the copies in
        # scope, and this session's scans.
        self.assertEqual(len(queries), 2)


class ScanningTests(StockCheckTestCase):

    def setUp(self):
        super().setUp()

        self.session = self.a_session(scope="shelf", shelf=self.shelf_a)
        self.copy = self.a_copy("INV-2001", shelf=self.shelf_a)

    def counts(self):
        return inventory.session_counts(self.session)

    def test_a_valid_copy_is_recorded_as_found(self):
        response = self.scan(self.session, "INV-2001")

        self.assertContains(response, "found. Scan the next one")
        self.assertEqual(self.counts()["found"], 1)

    def test_the_code_is_matched_the_way_the_rest_of_the_app_matches_it(self):
        # Task 11's lookup: case-insensitive, surrounding space ignored.
        self.scan(self.session, "  inv-2001  ")

        self.assertEqual(self.counts()["found"], 1)

    def test_several_copies_count_up(self):
        second = self.a_copy("INV-2002", shelf=self.shelf_a)
        third = self.a_copy("INV-2003", shelf=self.shelf_a)

        for code in ("INV-2001", "INV-2002", "INV-2003"):
            self.scan(self.session, code)

        self.assertEqual(self.counts()["found"], 3)
        self.assertEqual(self.counts()["remaining"], 0)
        self.assertTrue(second.id and third.id)

    def test_a_repeated_scan_does_not_count_twice(self):
        self.scan(self.session, "INV-2001")
        response = self.scan(self.session, "INV-2001")

        self.assertContains(response, "already been counted")
        self.assertEqual(self.counts()["found"], 1)
        self.assertEqual(self.counts()["duplicates"], 1)

    def test_the_database_is_what_stops_the_second_one(self):
        # Not the check above it: two requests could both pass that.
        inventory.record_scan(
            self.session, self.copy, "INV-2001", self.librarian
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InventoryScan.objects.create(
                    session=self.session,
                    copy=self.copy,
                    copy_code="INV-2001",
                    outcome=InventoryScan.OUTCOME_FOUND,
                    scanned_at=timezone.now(),
                )

    def test_a_race_gives_one_found_and_one_duplicate(self):
        # What two people scanning the same shelf amounts to: the second
        # write is refused and recorded as a repeat instead.
        first, _ = inventory.record_scan(
            self.session, self.copy, "INV-2001", self.librarian
        )
        second, _ = inventory.record_scan(
            self.session, self.copy, "INV-2001", self.librarian
        )

        self.assertEqual(first, InventoryScan.OUTCOME_FOUND)
        self.assertEqual(second, InventoryScan.OUTCOME_DUPLICATE)
        self.assertEqual(self.counts()["found"], 1)

    def test_an_unknown_code_is_reported_and_recorded(self):
        response = self.scan(self.session, "NOT-A-CODE")

        self.assertContains(response, "No copy carries the code")
        self.assertEqual(self.counts()["found"], 0)
        self.assertEqual(self.counts()["unknown"], 1)

        scan = InventoryScan.objects.get(outcome="unknown")
        self.assertEqual(scan.copy_code, "NOT-A-CODE")
        self.assertIsNone(scan.copy_id)

    def test_a_copy_from_another_shelf_is_reported_as_outside(self):
        elsewhere = self.a_copy("INV-2010", shelf=self.shelf_b)

        response = self.scan(self.session, "INV-2010")

        self.assertContains(response, "not part of this check")
        self.assertContains(response, "B-1")
        self.assertEqual(self.counts()["found"], 0)
        self.assertEqual(self.counts()["outside"], 1)

        # And nothing about it has been touched.
        elsewhere.refresh_from_db()
        self.assertEqual(elsewhere.shelf_id, self.shelf_b.id)
        self.assertEqual(elsewhere.status, "Available")

    def test_scanning_never_changes_the_copy(self):
        before = BookCopy.objects.get(id=self.copy.id)

        self.scan(self.session, "INV-2001")

        after = BookCopy.objects.get(id=self.copy.id)

        self.assertEqual(after.status, before.status)
        self.assertEqual(after.shelf_id, before.shelf_id)

    def test_an_empty_code_does_nothing(self):
        self.client.post(
            reverse("inventory_session_scan", args=[self.session.id]),
            {"copy_code": "   "},
        )

        self.assertFalse(InventoryScan.objects.exists())

    def test_the_remaining_count_falls_as_things_are_found(self):
        self.a_copy("INV-2020", shelf=self.shelf_a)

        self.assertEqual(self.counts()["remaining"], 2)

        self.scan(self.session, "INV-2001")
        self.assertEqual(self.counts()["remaining"], 1)

        self.scan(self.session, "INV-2020")
        self.assertEqual(self.counts()["remaining"], 0)

    def test_progress_is_a_percentage_of_what_is_expected(self):
        self.a_copy("INV-2030", shelf=self.shelf_a)
        self.a_copy("INV-2031", shelf=self.shelf_a)
        self.a_copy("INV-2032", shelf=self.shelf_a)

        self.scan(self.session, "INV-2001")

        self.assertEqual(self.counts()["progress"], 25)

    def test_an_empty_shelf_is_fully_checked(self):
        empty = self.a_session(scope="shelf", shelf=self.shelf_c)

        counts = inventory.session_counts(empty)

        self.assertEqual(counts["expected"], 0)
        self.assertEqual(counts["progress"], 100)


class CompletedSessionTests(StockCheckTestCase):

    def setUp(self):
        super().setUp()

        self.session = self.a_session(scope="shelf", shelf=self.shelf_a)

        self.found = self.a_copy("INV-3001", shelf=self.shelf_a)
        self.absent = self.a_copy("INV-3002", shelf=self.shelf_a)

        self.scan(self.session, "INV-3001")

    def complete(self):
        self.client.post(
            reverse("inventory_session_complete", args=[self.session.id])
        )
        self.session.refresh_from_db()

    def test_what_was_not_found_is_listed(self):
        self.complete()

        response = self.detail(self.session)

        self.assertEqual(
            [c.id for c in response.context["missing"]], [self.absent.id]
        )
        self.assertContains(response, "INV-3002")

    def test_what_was_found_is_not_in_that_list(self):
        self.complete()

        listed = [
            c.id for c in self.detail(self.session).context["missing"]
        ]

        self.assertNotIn(self.found.id, listed)

    def test_nothing_is_marked_missing_by_completing(self):
        self.complete()

        self.absent.refresh_from_db()

        self.assertEqual(self.absent.status, "Available")
        self.assertNotEqual(self.absent.status, "Missing")

    def test_the_report_says_so_in_as_many_words(self):
        self.complete()

        self.assertContains(
            self.detail(self.session), "has been marked Missing"
        )

    def test_a_copy_out_on_loan_is_reported_with_the_status_that_explains_it(self):
        on_loan = self.a_copy("INV-3010", shelf=self.shelf_a, status="Issued")
        make_loan(copy=on_loan, borrower=make_borrower(phone="0399-1"))

        self.complete()

        response = self.detail(self.session)

        self.assertIn(on_loan.id, [c.id for c in response.context["missing"]])
        self.assertContains(response, "Issued")

    def test_the_summary_holds(self):
        self.complete()

        counts = self.detail(self.session).context["counts"]

        self.assertEqual(counts["expected"], 2)
        self.assertEqual(counts["found"], 1)
        self.assertEqual(counts["remaining"], 1)

    def test_it_says_who_ran_it_and_when(self):
        self.complete()

        response = self.detail(self.session)

        self.assertContains(response, "librarian_u")
        self.assertContains(response, "Completed")

    def test_every_missing_copy_links_to_its_own_page(self):
        self.complete()

        response = self.detail(self.session)

        for copy in response.context["missing"]:
            with self.subTest(copy=copy.copy_code):
                self.assertContains(
                    response, reverse("book_copy_detail", args=[copy.id])
                )

    def test_the_outside_scope_scans_are_listed(self):
        elsewhere = self.a_copy("INV-3020", shelf=self.shelf_b)
        self.scan(self.session, "INV-3020")

        self.complete()

        response = self.detail(self.session)

        self.assertEqual(
            [s.copy_id for s in response.context["outside"]], [elsewhere.id]
        )
        self.assertContains(response, "Found somewhere else")

    def test_the_repeat_scans_are_listed(self):
        self.scan(self.session, "INV-3001")

        self.complete()

        response = self.detail(self.session)

        self.assertEqual(len(response.context["duplicates"]), 1)


class ImmutabilityTests(StockCheckTestCase):
    """A finished check is finished, whatever arrives at the URL."""

    def setUp(self):
        super().setUp()

        self.session = self.a_session(scope="library", completed=True)
        self.copy = self.a_copy("INV-4001")

    def test_a_completed_session_cannot_be_scanned_into(self):
        response = self.scan(self.session, "INV-4001")

        self.assertContains(response, "This stock check is complete")
        self.assertFalse(InventoryScan.objects.exists())

    def test_a_hand_made_post_cannot_reopen_it_either(self):
        # No GET does anything, and the POST is the only writer.
        self.client.get(
            reverse("inventory_session_scan", args=[self.session.id])
        )

        self.assertFalse(InventoryScan.objects.exists())

    def test_completing_an_already_completed_session_changes_nothing(self):
        was = self.session.completed_at

        self.client.post(
            reverse("inventory_session_complete", args=[self.session.id])
        )

        self.session.refresh_from_db()

        self.assertEqual(self.session.completed_at, was)
        self.assertEqual(self.session.status, "Completed")

    def test_the_page_offers_no_way_to_carry_on(self):
        response = self.detail(self.session)

        self.assertNotContains(
            response,
            reverse("inventory_session_scan", args=[self.session.id]),
        )
        self.assertNotContains(
            response,
            reverse("inventory_session_complete", args=[self.session.id]),
        )


class CopyDetailIntegrationTests(StockCheckTestCase):

    def setUp(self):
        super().setUp()

        self.copy = self.a_copy("INV-5001", shelf=self.shelf_a)

    def page(self):
        # A copy's details are a dialog now; there is no page to fetch.
        return self.client.get(
            reverse("book_copy_detail", args=[self.copy.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )

    def test_a_copy_never_counted_says_so(self):
        response = self.page()

        self.assertIsNone(response.context["last_check"])
        self.assertContains(response, "Never counted")

    def test_it_names_the_last_completed_check_and_the_outcome(self):
        session = self.a_session(
            scope="shelf", shelf=self.shelf_a, name="A-1 sweep"
        )
        self.scan(session, "INV-5001")
        self.client.post(
            reverse("inventory_session_complete", args=[session.id])
        )

        response = self.page()

        self.assertEqual(response.context["last_check"].id, session.id)
        self.assertTrue(response.context["last_check_found"])
        self.assertContains(response, "A-1 sweep")
        self.assertContains(response, "Found")

    def test_a_copy_not_found_in_the_last_check_says_that(self):
        session = self.a_session(scope="shelf", shelf=self.shelf_a)
        self.client.post(
            reverse("inventory_session_complete", args=[session.id])
        )

        response = self.page()

        self.assertFalse(response.context["last_check_found"])
        self.assertContains(response, "Not found")

    def test_an_open_check_is_not_the_last_completed_one(self):
        self.a_session(scope="library")

        self.assertIsNone(self.page().context["last_check"])

    def test_a_check_of_another_shelf_says_nothing_about_this_copy(self):
        # Reporting it as "not found" would be a lie by omission.
        session = self.a_session(scope="shelf", shelf=self.shelf_b)
        self.client.post(
            reverse("inventory_session_complete", args=[session.id])
        )

        self.assertIsNone(self.page().context["last_check"])

    def test_a_library_wide_check_does_cover_it(self):
        session = self.a_session(scope="library")
        self.client.post(
            reverse("inventory_session_complete", args=[session.id])
        )

        self.assertEqual(self.page().context["last_check"].id, session.id)

    def test_the_copy_list_is_not_made_to_pay_for_this(self):
        for number in range(20):
            self.a_copy("INV-6%03d" % number)

        session = self.a_session(scope="library")
        self.client.post(
            reverse("inventory_session_complete", args=[session.id])
        )

        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("book_copy_list"))

        self.assertFalse([
            q for q in queries.captured_queries
            if "inventory_" in q["sql"]
        ])

    def test_the_copy_page_costs_a_fixed_amount(self):
        session = self.a_session(scope="library")
        self.scan(session, "INV-5001")
        self.client.post(
            reverse("inventory_session_complete", args=[session.id])
        )

        with CaptureQueriesContext(connection) as few:
            self.page()

        for number in range(10):
            later = self.a_session(scope="library", name="Check %d" % number)
            self.client.post(
                reverse("inventory_session_complete", args=[later.id])
            )

        with CaptureQueriesContext(connection) as many:
            self.page()

        self.assertEqual(len(few), len(many))


class StockCheckPermissionTests(StockCheckTestCase):

    def setUp(self):
        super().setUp()
        self.session = self.a_session()
        self.a_copy("INV-7001")

    def pages(self):
        return (
            ("list", reverse("inventory_session_list")),
            ("start", reverse("inventory_session_start")),
            (
                "detail",
                reverse("inventory_session_detail", args=[self.session.id]),
            ),
        )

    def test_the_roles_that_manage_copies_may_run_a_check(self):
        for role in ("Admin", "Librarian"):
            self.as_role(role)

            for name, url in self.pages():
                with self.subTest(role=role, page=name):
                    self.assertEqual(
                        self.client.get(url).status_code, 200
                    )

    def test_an_assistant_may_not(self):
        self.as_role("Assistant")

        for name, url in self.pages():
            with self.subTest(page=name):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_an_assistant_cannot_scan_or_complete_either(self):
        self.as_role("Assistant")

        for name, url in (
            (
                "scan",
                reverse("inventory_session_scan", args=[self.session.id]),
            ),
            (
                "complete",
                reverse(
                    "inventory_session_complete", args=[self.session.id]
                ),
            ),
        ):
            with self.subTest(view=name):
                self.assertEqual(
                    self.client.post(url, {"copy_code": "INV-7001"}).status_code,
                    403,
                )

        self.assertFalse(InventoryScan.objects.exists())
        self.session.refresh_from_db()
        self.assertTrue(self.session.is_open)

    def test_an_assistant_is_not_offered_it_in_the_sidebar(self):
        self.as_role("Assistant")

        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, reverse("inventory_session_list"))

    def test_a_librarian_is(self):
        self.as_role("Librarian")

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, reverse("inventory_session_list"))

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        self.client.logout()

        for name, url in self.pages():
            with self.subTest(page=name):
                response = self.client.get(url)

                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])


class StockCheckQueryTests(StockCheckTestCase):

    def test_the_session_list_does_not_query_per_row(self):
        # One row against fifteen. Against none it would be comparing a
        # page that renders a table with one that renders an empty state.
        self.a_session(name="Check 0")

        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("inventory_session_list"))

        for number in range(1, 15):
            self.a_session(name="Check %d" % number)

        with CaptureQueriesContext(connection) as many:
            response = self.client.get(reverse("inventory_session_list"))

        self.assertEqual(len(response.context["sessions"]), 15)
        self.assertEqual(len(few), len(many))

    def test_the_dashboard_does_not_grow_with_the_scans(self):
        session = self.a_session(scope="library")

        for number in range(5):
            self.a_copy("INV-8%03d" % number)

        with CaptureQueriesContext(connection) as few:
            self.detail(session)

        for number in range(5):
            self.scan(session, "INV-8%03d" % number)

        with CaptureQueriesContext(connection) as many:
            self.detail(session)

        self.assertEqual(len(few), len(many))

    def test_the_missing_report_does_not_query_per_copy(self):
        session = self.a_session(scope="library")

        for number in range(3):
            self.a_copy("INV-9%03d" % number)

        self.client.post(
            reverse("inventory_session_complete", args=[session.id])
        )

        with CaptureQueriesContext(connection) as few:
            body = self.detail(session).content.decode()

        self.assertIn("Kitab al-Kharaj", body)
        self.assertIn("Abu Yusuf", body)

        for number in range(20):
            self.a_copy("INV-95%02d" % number)

        with CaptureQueriesContext(connection) as many:
            self.detail(session)

        self.assertEqual(len(few), len(many))
