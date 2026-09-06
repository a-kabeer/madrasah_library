from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library.models import ActivityLog
from library.views import activity_log_target, lookup_books_url

from .helpers import (
    make_book,
    make_borrower,
    make_category,
    make_copy,
    make_loan,
    make_location,
    make_shelf,
    make_user,
    make_volume,
)


def make_log(action="CREATE", entity_type="Book", entity_id=1, description=""):
    return ActivityLog.objects.create(
        user=None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        description=description,
        created_at=timezone.now(),
    )


class ActivityLogTargetTests(TestCase):
    """Which activity entries get a link, and where it points."""

    def test_entities_with_a_detail_page_are_linked(self):
        expected = {
            "Book": "book_detail",
            "BookContent": "book_content_detail",
            "BookCopy": "book_copy_detail",
            "BookVolume": "book_volume_detail",
            "Borrower": "borrower_detail",
            "Loan": "loan_detail",
            "Location": "location_detail",
            "Shelf": "shelf_detail",
        }

        for entity_type, route in expected.items():
            with self.subTest(entity_type=entity_type):
                log = make_log(entity_type=entity_type, entity_id=7)

                self.assertEqual(
                    activity_log_target(log),
                    reverse(route, args=[7]),
                )

    def test_the_three_lookups_point_at_their_books_instead(self):
        # None of them has a page of its own any more. Their name in the
        # list opens the book list filtered to them, and so does this.
        for entity_type, kind in (
            ("Author", "author"),
            ("Category", "category"),
            ("Publisher", "publisher"),
        ):
            with self.subTest(entity_type=entity_type):
                log = make_log(entity_type=entity_type, entity_id=7)

                self.assertEqual(
                    activity_log_target(log), lookup_books_url(kind, 7)
                )

    def test_deletions_are_not_linked(self):
        # The record is gone, so its detail page would only 404.
        log = make_log(action="DELETE", entity_type="Book", entity_id=7)

        self.assertEqual(activity_log_target(log), "")

    def test_entities_without_a_detail_page_are_not_linked(self):
        for entity_type in ("User", "OrganizationSettings", "Mystery"):
            with self.subTest(entity_type=entity_type):
                log = make_log(entity_type=entity_type, entity_id=7)

                self.assertEqual(activity_log_target(log), "")

    def test_missing_entity_details_are_not_linked(self):
        self.assertEqual(activity_log_target(make_log(entity_type="")), "")
        self.assertEqual(activity_log_target(make_log(entity_id=None)), "")


class DashboardRecentActivityTests(TestCase):
    """The whole row should be clickable, matching Recent Loans."""

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def rows(self, response):
        """(tag, href) for each Recent Activity row, in page order."""

        import re

        html = response.content.decode()
        start = html.find("Recent Activity")
        block = html[start:]

        return re.findall(
            r"<(a|div)\s[^>]*?list-group-item[^>]*?>", block, re.S
        )

    def test_linkable_entry_wraps_the_whole_row_in_a_link(self):
        book = make_book()
        make_log(entity_type="Book", entity_id=book.id)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        # The anchor carries the row classes, so the entire row is the target.
        self.assertContains(
            response,
            f'href="{reverse("book_detail", args=[book.id])}"',
        )
        self.assertIn("a", self.rows(response))

    def test_linked_row_uses_the_same_affordance_as_recent_loans(self):
        book = make_book()
        make_log(entity_type="Book", entity_id=book.id)

        response = self.client.get(reverse("dashboard"))
        html = response.content.decode()

        start = html.find("Recent Activity")

        self.assertIn("list-group-item-action", html[start:])

    def test_unlinkable_entry_renders_a_plain_row(self):
        make_log(action="DELETE", entity_type="Book", entity_id=99999)

        response = self.client.get(reverse("dashboard"))

        # No link to the deleted record.
        self.assertNotContains(
            response,
            reverse("book_detail", args=[99999]),
        )

    def test_target_url_is_attached_to_each_entry(self):
        category = make_category()
        make_log(entity_type="Category", entity_id=category.id)
        make_log(action="DELETE", entity_type="Book", entity_id=42)

        response = self.client.get(reverse("dashboard"))

        targets = {
            log.entity_type: log.target_url
            for log in response.context["recent_logs"]
        }

        self.assertEqual(
            targets["Category"],
            lookup_books_url("category", category.id),
        )
        self.assertEqual(targets["Book"], "")

    def test_dashboard_renders_with_no_activity(self):
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No recent activity found.")


class DashboardBase(TestCase):

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        self.book = make_book(title="Riyad as-Salihin")
        self.volume = make_volume(book=self.book, volume_number=1, title="")
        self.shelf = make_shelf(location=make_location(name="Hall"))
        self.borrower = make_borrower(name="Yusuf", phone="0321-1")

        self.today = timezone.now().date()

    def a_copy(self, code, status="Issued"):
        return make_copy(
            volume=self.volume,
            shelf=self.shelf,
            copy_code=code,
            status=status,
        )

    def a_loan(self, code, *, due_in=7, returned=None):
        # Issued a month back, so a test can hand a book in several days
        # ago without tripping `check_return_date_not_before_issue`.
        return make_loan(
            copy=self.a_copy(code),
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=30),
            due_date=self.today + timedelta(days=due_in),
            return_date=returned,
        )

    def get(self):
        return self.client.get(reverse("dashboard"))

    def as_role(self, role):
        self.client.logout()
        make_user(
            username="d_%s" % role, password="pass12345", role=role
        )
        self.client.login(username="d_%s" % role, password="pass12345")


class ActionAlertTests(DashboardBase):
    """The three counts a librarian opens this screen for."""

    def setUp(self):
        super().setUp()

        self.overdue = self.a_loan("AL-1", due_in=-4)
        self.due_today = self.a_loan("AL-2", due_in=0)
        self.later = self.a_loan("AL-3", due_in=9)
        self.given_back = self.a_loan(
            "AL-4", due_in=-1, returned=self.today
        )

    def test_the_counts_are_right(self):
        context = self.get().context

        # Three out, one of them past its date, one due on it. The returned
        # one counts for none of them.
        self.assertEqual(context["active_loans"], 3)
        self.assertEqual(context["overdue_loans"], 1)
        self.assertEqual(context["due_today_loans"], 1)

    def test_due_today_is_not_counted_as_overdue(self):
        # The existing rule: overdue is past the due date, not on it.
        self.assertEqual(self.get().context["overdue_loans"], 1)

    def test_each_alert_links_to_the_matching_records(self):
        body = self.get().content.decode()

        for status, expected in (
            ("overdue", {self.overdue.id}),
            ("due_today", {self.due_today.id}),
            ("active", {self.overdue.id, self.due_today.id, self.later.id}),
        ):
            with self.subTest(status=status):

                link = "%s?status=%s" % (reverse("loan_list"), status)

                self.assertIn(link, body)

                # And the link actually shows those records - the filter is
                # the loan list's own, not a second implementation.
                listed = self.client.get(
                    reverse("loan_list"), {"status": status}
                )

                self.assertEqual(
                    {loan.id for loan in listed.context["loans"]}, expected
                )

    def test_the_alerts_come_before_the_catalogue_totals(self):
        # The point of the change: today's work is read first.
        body = self.get().content.decode()

        self.assertLess(
            body.index("Overdue"), body.index("Total Books")
        )

    def test_a_quiet_day_says_nothing_extra(self):
        Loan = self.overdue.__class__
        Loan.objects.all().delete()

        body = self.get().content.decode()

        self.assertNotIn("Chase these first", body)


class RecentCirculationTests(DashboardBase):

    def test_recently_issued_is_newest_first(self):
        old = self.a_loan("RI-1")
        old.issue_date = self.today - timedelta(days=30)
        old.save(update_fields=["issue_date"])

        new = self.a_loan("RI-2")
        new.issue_date = self.today
        new.save(update_fields=["issue_date"])

        listed = [loan.id for loan in self.get().context["recent_loans"]]

        self.assertEqual(listed[0], new.id)
        self.assertIn(old.id, listed)

    def test_same_day_issues_are_ordered_not_arbitrary(self):
        # `issue_date` is a date, so without a tiebreaker everything issued
        # today came back in whatever order the database chose.
        first = self.a_loan("RI-3")
        second = self.a_loan("RI-4")

        for loan in (first, second):
            loan.issue_date = self.today
            loan.save(update_fields=["issue_date"])

        listed = [loan.id for loan in self.get().context["recent_loans"]]

        self.assertEqual(listed[:2], [second.id, first.id])

    def test_recently_returned_holds_only_returned_loans(self):
        out = self.a_loan("RR-1")
        back = self.a_loan("RR-2", returned=self.today)

        returned = self.get().context["recent_returns"]

        self.assertEqual({loan.id for loan in returned}, {back.id})
        self.assertNotIn(out.id, {loan.id for loan in returned})

    def test_recently_returned_is_newest_first(self):
        older = self.a_loan(
            "RR-3", returned=self.today - timedelta(days=5)
        )
        newer = self.a_loan("RR-4", returned=self.today)

        listed = [loan.id for loan in self.get().context["recent_returns"]]

        self.assertEqual(listed, [newer.id, older.id])

    def test_both_lists_are_capped(self):
        for index in range(8):
            self.a_loan("RC-%d" % index)
            self.a_loan("RD-%d" % index, returned=self.today)

        context = self.get().context

        self.assertEqual(len(context["recent_loans"]), 5)
        self.assertEqual(len(context["recent_returns"]), 5)

    def test_the_returned_list_names_the_book_and_who_took_it_back(self):
        self.a_loan("RR-5", returned=self.today)

        body = self.get().content.decode()

        self.assertIn("Recently Returned", body)
        self.assertIn("RR-5", body)
        self.assertIn("Riyad as-Salihin", body)

    def test_an_empty_dashboard_still_renders(self):
        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nothing has been returned yet.")


class QuickActionTests(DashboardBase):

    def test_the_four_workflows_are_linked_for_an_admin(self):
        body = self.get().content.decode()

        for name in (
            "circulation_issue",
            "circulation_return_lookup",
            "book_add",
            "borrower_add",
        ):
            with self.subTest(action=name):
                self.assertIn(reverse(name), body)

    def test_every_offered_action_answers_for_the_role_it_is_offered_to(self):
        # The failure this guards is a dashboard button that leads to 403.
        for role in ("Admin", "Librarian", "Assistant"):
            self.as_role(role)

            body = self.get().content.decode()

            for name in (
                "circulation_issue",
                "circulation_return_lookup",
                "book_add",
                "borrower_add",
            ):
                if reverse(name) not in body:
                    continue

                with self.subTest(role=role, action=name):
                    self.assertEqual(
                        self.client.get(reverse(name)).status_code, 200
                    )

    def test_an_assistant_is_not_offered_adding_a_book(self):
        self.as_role("Assistant")

        response = self.get()

        self.assertFalse(response.context["can_edit"])
        self.assertNotContains(response, reverse("book_add"))

        # Which matches what the view would have said.
        self.assertEqual(
            self.client.get(reverse("book_add")).status_code, 403
        )

    def test_an_assistant_still_gets_the_circulation_actions(self):
        self.as_role("Assistant")

        body = self.get().content.decode()

        for name in (
            "circulation_issue",
            "circulation_return_lookup",
            "borrower_add",
        ):
            with self.subTest(action=name):
                self.assertIn(reverse(name), body)

    def test_a_librarian_is_offered_all_four(self):
        self.as_role("Librarian")

        response = self.get()

        self.assertTrue(response.context["can_edit"])
        self.assertContains(response, reverse("book_add"))


class DashboardQueryTests(DashboardBase):

    def test_the_page_costs_the_same_however_much_has_happened(self):
        # Five rows each, with the joins the rows name. Nothing per-row.
        with CaptureQueriesContext(connection) as few:
            self.get()

        for index in range(20):
            self.a_loan("QQ-%d" % index)
            self.a_loan("QR-%d" % index, returned=self.today)
            make_log(entity_type="Book", entity_id=self.book.id)

        with CaptureQueriesContext(connection) as many:
            self.get()

        self.assertEqual(len(few), len(many))

    def test_the_recent_lists_need_no_further_query_to_render(self):
        for index in range(5):
            self.a_loan("QS-%d" % index)
            self.a_loan("QT-%d" % index, returned=self.today)

        with CaptureQueriesContext(connection) as queries:
            body = self.get().content.decode()

        # The rows name the book, the borrower and the member of staff; all
        # three arrive with the loan.
        self.assertIn("Riyad as-Salihin", body)
        self.assertIn("Yusuf", body)

        loan_queries = [
            q["sql"] for q in queries.captured_queries
            if 'FROM "loans"' in q["sql"]
        ]

        # One for each list, plus the three counts. Never one per row.
        self.assertLess(len(loan_queries), 8)
