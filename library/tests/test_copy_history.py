"""A copy's history, read from the records that already hold it.

Two things are being pinned. First, that every kind of event reaches the
timeline from its own authoritative source and appears exactly once - a
loan is never copied into a second table, so there is nothing that can
disagree with it. Second, that the events which had no history at all
before this task now get one going forward, and that nothing invents
history for the changes made before that.
"""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library import history
from library.models import (
    ActivityLog,
    BookCopy,
    InventoryScan,
    InventorySession,
    Loan,
)

from .helpers import (
    main_content,
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


class HistoryTestCase(TestCase):

    def setUp(self):
        self.librarian = make_user(
            username="librarian_u", password="pass12345", role="Librarian"
        )
        self.client.login(username="librarian_u", password="pass12345")

        self.hall = make_location(name="Main Hall")
        self.shelf_a = make_shelf(location=self.hall, shelf_code="A-1")
        self.shelf_b = make_shelf(location=self.hall, shelf_code="B-1")

        self.author = make_author("Abu Yusuf")
        self.book = make_book(title="Kitab al-Kharaj", author=self.author)
        self.volume = make_volume(book=self.book, volume_number=1, title="")

        self.copy = make_copy(
            volume=self.volume, shelf=self.shelf_a, copy_code="HIS-0001"
        )

        self.borrower = make_borrower(name="Hafsa", phone="0355-1")

        self.today = timezone.now().date()

    def dialog(self, copy=None, **params):
        """A copy's details, the way the browser asks for them.

        `?modal=1` and the HX-Request header together - `is_modal_request`
        wants both, so one URL never answers with two different bodies.
        There is no copy page any more; the dialog is the whole of it.
        """

        # The query is built into the path, not passed as `data`: the
        # test client replaces a path's query string when both are given,
        # which silently dropped `modal=1` and turned the dialog into a
        # redirect.
        query = "&".join(
            ["modal=1"]
            + ["%s=%s" % (key, value) for key, value in params.items()]
        )

        return self.client.get(
            reverse("book_copy_detail", args=[(copy or self.copy).id])
            + "?" + query,
            headers={"HX-Request": "true"},
        )

    def page(self, copy=None, **params):
        # The copy's details are a dialog now; there is no page to fetch.
        return self.dialog(copy, **params)

    def timeline(self, copy=None):
        return history.copy_timeline(copy or self.copy)

    def kinds(self, copy=None):
        return [event.kind for event in self.timeline(copy)]

    def edit(self, **overrides):
        """Save the copy's edit form, changing only what is named."""

        self.copy.refresh_from_db()

        data = {
            "location": (
                self.copy.shelf.location_id if self.copy.shelf_id else ""
            ),
            "shelf": self.copy.shelf_id or "",
            "status": self.copy.status,
            "acquisition_date": "",
            "notes": self.copy.notes or "",
        }
        data.update(overrides)

        return self.client.post(
            reverse("book_copy_edit", args=[self.copy.id]) + "?modal=1",
            data,
            headers={"HX-Request": "true"},
        )


class TimelineSourceTests(HistoryTestCase):
    """Each event comes from the record that owns it, and appears once."""

    def test_a_new_copy_has_an_empty_history(self):
        # `make_copy` writes no log, so this copy has genuinely no events.
        self.assertEqual(self.timeline(), [])
        self.assertContains(self.page(), "Nothing has happened to this copy")

    def test_an_issue_appears_from_the_loan(self):
        make_loan(copy=self.copy, borrower=self.borrower)

        self.assertEqual(self.kinds(), ["issued"])

        event = self.timeline()[0]
        self.assertEqual(event.borrower, self.borrower)

    def test_a_return_appears_alongside_its_issue(self):
        make_loan(
            copy=self.copy,
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=10),
            due_date=self.today - timedelta(days=3),
            return_date=self.today,
        )

        self.assertEqual(self.kinds(), ["returned", "issued"])

    def test_no_loan_is_duplicated_into_a_second_table(self):
        # The whole reason there is no movement table.
        make_loan(copy=self.copy, borrower=self.borrower)

        issues = [k for k in self.kinds() if k == "issued"]

        self.assertEqual(len(issues), 1)
        self.assertEqual(Loan.objects.filter(copy=self.copy).count(), 1)

    def test_a_renewal_appears_once(self):
        loan = make_loan(
            copy=self.copy,
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=5),
            due_date=self.today + timedelta(days=3),
        )

        self.client.post(reverse("loan_renew", args=[loan.id]))

        self.assertEqual(self.kinds().count("renewed"), 1)

    def test_a_renewal_is_found_through_the_copys_own_loan(self):
        # It is logged against the loan, not the copy.
        loan = make_loan(
            copy=self.copy,
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=5),
            due_date=self.today + timedelta(days=3),
        )
        self.client.post(reverse("loan_renew", args=[loan.id]))

        logged = ActivityLog.objects.get(action="RENEW")

        self.assertEqual(logged.entity_type, "Loan")
        self.assertEqual(logged.entity_id, loan.id)
        self.assertIn("renewed", self.kinds())

    def test_another_copys_renewal_is_not_in_this_copys_history(self):
        other = make_copy(
            volume=self.volume, shelf=self.shelf_a, copy_code="HIS-0002"
        )
        loan = make_loan(
            copy=other,
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=5),
            due_date=self.today + timedelta(days=3),
        )
        self.client.post(reverse("loan_renew", args=[loan.id]))

        self.assertNotIn("renewed", self.kinds())
        self.assertIn("renewed", self.kinds(other))

    def test_a_withdrawal_appears_once(self):
        self.client.post(
            reverse("book_copy_withdraw", args=[self.copy.id])
        )

        self.assertEqual(self.kinds().count("withdrawn"), 1)

    def test_a_stock_check_finding_appears(self):
        session = InventorySession.objects.create(
            name="A-1 sweep",
            scope=InventorySession.SCOPE_LIBRARY,
            status=InventorySession.STATUS_IN_PROGRESS,
            started_by=self.librarian,
            started_at=timezone.now(),
        )
        self.client.post(
            reverse("inventory_session_scan", args=[session.id]),
            {"copy_code": "HIS-0001"},
        )

        self.assertIn("counted", self.kinds())
        self.assertIn("A-1 sweep", [e.detail for e in self.timeline()])

    def test_a_scan_that_found_nothing_is_not_in_any_copys_history(self):
        session = InventorySession.objects.create(
            name="Sweep",
            scope=InventorySession.SCOPE_SHELF,
            shelf=self.shelf_b,
            status=InventorySession.STATUS_IN_PROGRESS,
            started_by=self.librarian,
            started_at=timezone.now(),
        )

        # This copy is on A-1, so scanning it into a B-1 check is an
        # outside-scope read, not a finding.
        self.client.post(
            reverse("inventory_session_scan", args=[session.id]),
            {"copy_code": "HIS-0001"},
        )

        self.assertEqual(
            InventoryScan.objects.get().outcome,
            InventoryScan.OUTCOME_OUTSIDE,
        )
        self.assertNotIn("counted", self.kinds())

    def test_events_are_scoped_to_the_copy(self):
        other = make_copy(
            volume=self.volume, shelf=self.shelf_a, copy_code="HIS-0003"
        )
        make_loan(copy=other, borrower=self.borrower)

        self.assertEqual(self.timeline(), [])
        self.assertEqual(self.kinds(other), ["issued"])

    def test_the_page_shows_them(self):
        make_loan(copy=self.copy, borrower=self.borrower)

        response = self.page()

        self.assertContains(response, "History")
        self.assertContains(response, "Issued")
        self.assertContains(response, "Hafsa")


class TimelineOrderTests(HistoryTestCase):

    def test_newest_first(self):
        make_loan(
            copy=self.copy,
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=30),
            due_date=self.today - timedelta(days=16),
            return_date=self.today - timedelta(days=20),
        )
        make_loan(
            copy=self.copy,
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=2),
            due_date=self.today + timedelta(days=12),
        )

        when = [event.when for event in self.timeline()]

        self.assertEqual(when, sorted(when, reverse=True))

    def test_a_return_and_a_re_issue_on_one_day_read_in_order(self):
        # The dates are equal, so only the tiebreak decides. A book comes
        # back before it goes out again.
        make_loan(
            copy=self.copy,
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=10),
            due_date=self.today,
            return_date=self.today,
        )
        make_loan(
            copy=self.copy,
            borrower=self.borrower,
            issue_date=self.today,
            due_date=self.today + timedelta(days=14),
        )

        same_day = [
            event.kind for event in self.timeline()
            if event.when.date() == self.today
        ]

        self.assertEqual(same_day, ["issued", "returned"])

    def test_the_order_is_stable_across_reads(self):
        for number in range(4):
            make_loan(
                copy=self.copy,
                borrower=self.borrower,
                issue_date=self.today - timedelta(days=20),
                due_date=self.today - timedelta(days=6),
                return_date=self.today - timedelta(days=10),
            )

        first = [(e.kind, e.key) for e in self.timeline()]
        second = [(e.kind, e.key) for e in self.timeline()]

        self.assertEqual(first, second)

    def test_it_paginates(self):
        from library.views import PAGE_SIZE

        for number in range(PAGE_SIZE + 4):
            make_loan(
                copy=self.copy,
                borrower=self.borrower,
                issue_date=self.today - timedelta(days=40 + number),
                due_date=self.today - timedelta(days=26 + number),
                return_date=self.today - timedelta(days=30 + number),
            )

        first = self.page()

        self.assertEqual(len(first.context["history"]), PAGE_SIZE)
        self.assertTrue(first.context["history"].has_next)

        second = self.page(history=2)

        self.assertEqual(second.context["history"].number, 2)

    def test_paging_the_history_leaves_the_loan_history_alone(self):
        # Its own parameter, so the two tables on this page do not fight.
        make_loan(copy=self.copy, borrower=self.borrower)

        response = self.page(history=1)

        self.assertEqual(len(response.context["loan_history"]), 1)


class FutureLoggingTests(HistoryTestCase):
    """Shelf and status changes, recorded from here on."""

    def descriptions(self):
        return [
            log.description
            for log in ActivityLog.objects.filter(
                entity_type="BookCopy", entity_id=self.copy.id
            )
        ]

    def test_a_shelf_change_records_where_it_came_from(self):
        self.edit(location=self.hall.id, shelf=self.shelf_b.id)

        self.assertEqual(
            self.descriptions(),
            ["HIS-0001 moved from %s to %s" % (self.shelf_a, self.shelf_b)],
        )

    def test_a_status_change_records_what_it_was(self):
        self.edit(status="Damaged")

        self.assertEqual(
            self.descriptions(),
            ["HIS-0001 status changed from Available to Damaged"],
        )

    def test_both_at_once_are_two_events(self):
        self.edit(
            location=self.hall.id, shelf=self.shelf_b.id, status="Damaged"
        )

        self.assertEqual(len(self.descriptions()), 2)

    def test_clearing_the_shelf_is_recorded(self):
        self.edit(location="", shelf="")

        self.assertEqual(
            self.descriptions(),
            ["HIS-0001 moved from %s to no shelf" % self.shelf_a],
        )

    def test_a_note_is_not_recorded_as_a_move_or_a_status_change(self):
        # It is an edit, so it is recorded and attributed - but the shelf
        # and the status did not move, and the history must not say they
        # did.
        self.edit(notes="A note.")

        self.assertEqual(self.descriptions(), ["HIS-0001 details updated"])

    def test_an_unchanged_shelf_and_status_are_not_logged(self):
        self.edit(notes="A note.")

        for description in self.descriptions():
            with self.subTest(logged=description):
                self.assertNotIn("moved from", description)
                self.assertNotIn("status changed", description)

    def test_saving_the_form_untouched_records_nothing(self):
        self.edit()

        self.assertEqual(self.descriptions(), [])
        self.assertEqual(self.timeline(), [])

    def test_the_changes_reach_the_timeline(self):
        self.edit(status="Damaged")

        self.assertEqual(self.kinds(), ["updated"])
        self.assertTrue(any(
            "status changed from Available to Damaged" in event.detail
            for event in self.timeline()
        ))

    def test_no_history_is_invented_for_what_came_before(self):
        # A copy that existed before any of this has no events, and this
        # does not manufacture an "added" or a "moved" for it.
        old = make_copy(
            volume=self.volume, shelf=self.shelf_a, copy_code="HIS-0009"
        )

        self.assertEqual(history.copy_timeline(old), [])

    def test_a_move_through_the_move_dialog_is_one_event_not_two(self):
        response = self.client.post(
            reverse("book_copy_move", args=[self.copy.id]) + "?modal=1",
            {"location": self.hall.id, "shelf": self.shelf_b.id},
            headers={"HX-Request": "true"},
        )

        # A dialog answers a save with 204 and an event, not a redirect.
        self.assertEqual(response.status_code, 204)

        self.assertEqual(len(self.descriptions()), 1)
        self.assertEqual(self.kinds().count("updated"), 1)

    def test_the_same_move_without_the_dialog_is_still_one_event(self):
        """There is no Move page left, so a scriptless post redirects -
        but it moves the copy and logs it exactly once, as before."""

        response = self.client.post(
            reverse("book_copy_move", args=[self.copy.id]),
            {"location": self.hall.id, "shelf": self.shelf_b.id},
        )

        self.assertEqual(response.status_code, 302)

        self.copy.refresh_from_db()
        self.assertEqual(self.copy.shelf_id, self.shelf_b.id)
        self.assertEqual(len(self.descriptions()), 1)

    def test_a_withdrawal_is_not_also_logged_as_a_status_change(self):
        # The dedicated workflow writes its own event; the edit view is
        # what the copy went through, and it is not involved.
        self.client.post(
            reverse("book_copy_withdraw", args=[self.copy.id])
        )

        self.assertEqual(self.kinds(), ["withdrawn"])

    def test_issuing_does_not_add_a_status_change_event(self):
        # Circulation moves the status too, and it has its own events.
        make_loan(copy=self.copy, borrower=self.borrower)
        self.copy.status = "Issued"
        self.copy.save(update_fields=["status"])

        self.assertEqual(self.kinds(), ["issued"])


class HistoryPermissionTests(HistoryTestCase):

    def as_role(self, role):
        self.client.logout()
        make_user(username="h_%s" % role, password="pass12345", role=role)
        self.client.login(username="h_%s" % role, password="pass12345")

    def test_history_follows_the_copy_page_it_is_on(self):
        make_loan(copy=self.copy, borrower=self.borrower)

        for role in ("Admin", "Librarian", "Assistant"):
            with self.subTest(role=role):
                self.as_role(role)

                response = self.page()

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "History")

    def test_it_is_read_only_for_everyone(self):
        # There is no writer: no URL, no form, no button anywhere on it.
        make_loan(copy=self.copy, borrower=self.borrower)

        # Scoped to the page's own content: `bi-clock-history` is also
        # the sidebar's Activity Log icon, so slicing from its first
        # occurrence started in the shell and swept in the topbar.
        section = main_content(self.page().content.decode())

        self.assertIn("bi-clock-history", section)
        self.assertNotIn("<form", section)

    def test_an_assistant_still_cannot_edit_the_copy(self):
        self.as_role("Assistant")

        self.assertEqual(
            self.client.get(
                reverse("book_copy_edit", args=[self.copy.id])
            ).status_code,
            403,
        )

    def test_a_signed_out_visitor_sees_none_of_it(self):
        self.client.logout()

        response = self.page()

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])


class HistoryQueryTests(HistoryTestCase):

    def test_a_long_history_costs_no_more_than_a_short_one(self):
        make_loan(copy=self.copy, borrower=self.borrower)

        with CaptureQueriesContext(connection) as few:
            self.page()

        for number in range(15):
            make_loan(
                copy=self.copy,
                borrower=self.borrower,
                issue_date=self.today - timedelta(days=40 + number),
                due_date=self.today - timedelta(days=26 + number),
                return_date=self.today - timedelta(days=30 + number),
            )
            self.client.post(
                reverse("book_copy_edit", args=[self.copy.id]),
                {
                    "location": self.hall.id,
                    "shelf": (
                        self.shelf_b.id if number % 2 else self.shelf_a.id
                    ),
                    "status": "Available",
                    "acquisition_date": "",
                    "notes": "",
                },
            )

        with CaptureQueriesContext(connection) as many:
            response = self.page()

        self.assertGreater(response.context["history_total"], 20)
        self.assertEqual(len(few), len(many))

    def test_the_timeline_itself_is_four_queries(self):
        make_loan(copy=self.copy, borrower=self.borrower)

        with CaptureQueriesContext(connection) as queries:
            self.timeline()

        # The loans, the copy's log entries, the renewals of those loans,
        # and the stock checks.
        self.assertEqual(len(queries), 4)

    def test_rendering_it_needs_no_further_query(self):
        for number in range(5):
            make_loan(
                copy=self.copy,
                borrower=self.borrower,
                issue_date=self.today - timedelta(days=40 + number),
                due_date=self.today - timedelta(days=26 + number),
                return_date=self.today - timedelta(days=30 + number),
            )

        with CaptureQueriesContext(connection) as queries:
            body = self.page().content.decode()

        self.assertIn("Hafsa", body)

        # None: the borrowers arrive joined to the loans, so rendering
        # five rows reads the table zero further times - let alone once
        # per row.
        self.assertEqual(
            len([
                q for q in queries.captured_queries
                if q["sql"].startswith('SELECT') and 'FROM "borrowers"' in q["sql"]
            ]),
            0,
        )

    def test_the_copy_list_is_not_made_to_pay_for_history(self):
        for number in range(10):
            copy = make_copy(
                volume=self.volume,
                shelf=self.shelf_a,
                copy_code="HIS-1%03d" % number,
            )
            make_loan(copy=copy, borrower=self.borrower)

        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("book_copy_list"))

        self.assertFalse([
            q for q in queries.captured_queries
            if 'FROM "activity_logs"' in q["sql"]
        ])


class CurrentStateSeparateTests(HistoryTestCase):
    """The summary above still says what is true now."""

    def test_the_summary_still_shows_the_current_status_and_shelf(self):
        self.edit(location=self.hall.id, shelf=self.shelf_b.id,
                  status="Damaged")

        response = self.page()

        self.assertEqual(response.context["copy"].status, "Damaged")
        self.assertEqual(response.context["copy"].shelf_id, self.shelf_b.id)
        self.assertContains(response, "B-1")

    def test_the_current_borrower_still_shows_when_issued(self):
        make_loan(copy=self.copy, borrower=self.borrower)

        response = self.page()

        self.assertIsNotNone(response.context["active_loan"])

    def test_history_does_not_decide_the_status(self):
        # An event saying it was withdrawn does not make the copy
        # withdrawn: the column does.
        self.client.post(
            reverse("book_copy_withdraw", args=[self.copy.id])
        )

        self.copy.refresh_from_db()

        self.assertEqual(self.copy.status, "Transferred")
        self.assertEqual(
            BookCopy.objects.get(id=self.copy.id).status, "Transferred"
        )
