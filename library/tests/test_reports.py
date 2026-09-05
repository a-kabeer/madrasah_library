"""Operational reports, and that they agree with the pages they summarise.

Every number here has an authority somewhere else in the application - the
loan list, the copy list, the overdue rule - and the point of most of these
tests is that the report and that authority say the same thing. A report
that quietly disagreed with the screen a librarian was looking at would be
worse than no report.

The other half is the filtering: a period that cannot be read is refused
rather than silently replaced, and the CSV is the same records as the page.
"""

import csv
import io
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library import reports
from library.models import BookCopy, Loan

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


class ReportTestCase(TestCase):

    def setUp(self):
        self.librarian = make_user(
            username="librarian_u", password="pass12345", role="Librarian"
        )
        self.client.login(username="librarian_u", password="pass12345")

        self.hall = make_location(name="Main Hall")
        self.annex = make_location(name="Annex")
        self.shelf_a = make_shelf(location=self.hall, shelf_code="A-1")
        self.shelf_z = make_shelf(location=self.annex, shelf_code="Z-1")

        self.author = make_author("Abu Yusuf")
        self.book = make_book(title="Kitab al-Kharaj", author=self.author)
        self.volume = make_volume(book=self.book, volume_number=1, title="")

        self.borrower = make_borrower(name="Hafsa", phone="0366-1")
        self.borrower.registration_no = "REG-7"
        self.borrower.save()

        self.today = timezone.now().date()

    # An unshelved copy is a real thing, so `None` cannot also mean
    # "not specified".
    DEFAULT_SHELF = object()

    def a_copy(self, code, shelf=DEFAULT_SHELF, status="Available",
               volume=None):
        return make_copy(
            volume=volume or self.volume,
            shelf=self.shelf_a if shelf is self.DEFAULT_SHELF else shelf,
            copy_code=code,
            status=status,
        )

    def a_loan(self, code, *, days_ago=5, due_in=9, returned=None,
               borrower=None, shelf=DEFAULT_SHELF, volume=None):
        return make_loan(
            copy=self.a_copy(
                code, shelf=shelf, status="Issued", volume=volume
            ),
            borrower=borrower or self.borrower,
            issue_date=self.today - timedelta(days=days_ago),
            due_date=self.today + timedelta(days=due_in),
            return_date=returned,
        )

    def get(self, name, **params):
        return self.client.get(reverse(name), params)

    def rows_of(self, response):
        """A CSV response, parsed back into rows."""

        text = response.content.decode("utf-8-sig")

        return list(csv.reader(io.StringIO(text)))

    def as_role(self, role):
        self.client.logout()
        make_user(username="r_%s" % role, password="pass12345", role=role)
        self.client.login(username="r_%s" % role, password="pass12345")


class ReportsHomeTests(ReportTestCase):

    def test_it_lists_every_report(self):
        response = self.get("reports_home")

        self.assertEqual(response.status_code, 200)

        for name, title, _icon, _blurb in reports.REPORTS:
            with self.subTest(report=title):
                self.assertContains(response, reverse(name))
                self.assertContains(response, title)

    def test_the_sidebar_offers_one_entry_not_six(self):
        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn(reverse("reports_home"), body)

        for name, _title, _icon, _blurb in reports.REPORTS:
            with self.subTest(report=name):
                self.assertNotIn(reverse(name), body)


class DateRangeTests(ReportTestCase):

    def test_no_dates_means_a_sensible_default(self):
        response = self.get("report_circulation")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["dates"].end, self.today)
        self.assertEqual(
            response.context["dates"].start,
            self.today - timedelta(days=reports.DEFAULT_DAYS),
        )

    def test_the_dates_asked_for_are_used(self):
        response = self.get(
            "report_circulation", start="2026-01-01", end="2026-01-31"
        )

        self.assertEqual(
            response.context["dates"].start.isoformat(), "2026-01-01"
        )
        self.assertEqual(
            response.context["dates"].end.isoformat(), "2026-01-31"
        )

    def test_an_unreadable_date_is_refused_not_ignored(self):
        response = self.get("report_circulation", start="last tuesday")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not a date the report can read")
        self.assertIsNone(response.context["tallies"])

    def test_a_start_after_the_end_is_refused(self):
        response = self.get(
            "report_circulation", start="2026-03-01", end="2026-02-01"
        )

        self.assertContains(response, "after the end date")
        self.assertIsNone(response.context["tallies"])

    def test_a_refused_period_reports_nothing_at_all(self):
        # Rather than reporting on a different period than the one asked
        # for, which is the failure that gets acted on.
        self.a_loan("REP-0001")

        response = self.get("report_circulation", end="nonsense")

        self.assertEqual(list(response.context["loans"]), [])

    def test_the_filters_survive_in_the_url(self):
        response = self.get(
            "report_circulation", start="2026-01-01", end="2026-01-31"
        )

        self.assertContains(response, 'value="2026-01-01"')
        self.assertContains(response, 'value="2026-01-31"')

    def test_every_dated_report_reads_the_same_way(self):
        for name in (
            "report_circulation", "report_borrowers", "report_popular"
        ):
            with self.subTest(report=name):
                self.assertContains(
                    self.get(name, start="rubbish"),
                    "not a date the report can read",
                )


class CirculationReportTests(ReportTestCase):

    def test_the_counts_are_right(self):
        self.a_loan("REP-1001")
        self.a_loan("REP-1002", returned=self.today)
        self.a_loan("REP-1003", days_ago=30, due_in=-3)

        tallies = self.get("report_circulation").context["tallies"]

        self.assertEqual(tallies["issued"], 3)
        self.assertEqual(tallies["returned"], 1)
        self.assertEqual(tallies["out"], 2)
        self.assertEqual(tallies["overdue"], 1)

    def test_a_loan_outside_the_period_is_not_counted(self):
        self.a_loan("REP-1010", days_ago=200, due_in=-180)

        tallies = self.get("report_circulation").context["tallies"]

        self.assertEqual(tallies["issued"], 0)
        # Still out, though, because that is a fact about today.
        self.assertEqual(tallies["out"], 1)

    def test_renewals_are_counted_from_the_loans_in_scope(self):
        loan = self.a_loan("REP-1020", due_in=3)

        self.client.post(reverse("loan_renew", args=[loan.id]))

        self.assertEqual(
            self.get("report_circulation").context["tallies"]["renewals"], 1
        )

    def test_the_location_filter_narrows_it(self):
        self.a_loan("REP-1030", shelf=self.shelf_a)
        self.a_loan("REP-1031", shelf=self.shelf_z)

        tallies = self.get(
            "report_circulation", location=self.hall.id
        ).context["tallies"]

        self.assertEqual(tallies["issued"], 1)

    def test_the_borrower_filter_narrows_it(self):
        other = make_borrower(name="Bilal", phone="0366-2")

        self.a_loan("REP-1040")
        self.a_loan("REP-1041", borrower=other)

        tallies = self.get(
            "report_circulation", borrower=other.id
        ).context["tallies"]

        self.assertEqual(tallies["issued"], 1)

    def test_the_title_filter_narrows_it(self):
        other_book = make_book(title="Al-Muwatta", author=make_author("Malik"))
        other_volume = make_volume(
            book=other_book, volume_number=1, title=""
        )

        self.a_loan("REP-1050")
        self.a_loan("REP-1051", volume=other_volume)

        tallies = self.get(
            "report_circulation", title="Muwatta"
        ).context["tallies"]

        self.assertEqual(tallies["issued"], 1)

    def test_the_detail_table_holds_the_same_loans(self):
        wanted = self.a_loan("REP-1060")
        self.a_loan("REP-1061", days_ago=300, due_in=-280)

        listed = [
            loan.id
            for loan in self.get("report_circulation").context["loans"]
        ]

        self.assertEqual(listed, [wanted.id])


class OverdueReportTests(ReportTestCase):

    def test_it_uses_the_applications_own_overdue_rule(self):
        late = self.a_loan("REP-2001", days_ago=30, due_in=-4)
        self.a_loan("REP-2002")
        self.a_loan("REP-2003", days_ago=20, due_in=0)   # due today

        listed = [
            loan.id for loan in self.get("report_overdue").context["loans"]
        ]

        self.assertEqual(listed, [late.id])

    def test_it_agrees_with_the_loan_list(self):
        self.a_loan("REP-2010", days_ago=30, due_in=-4)
        self.a_loan("REP-2011", days_ago=30, due_in=-9)

        from_report = {
            loan.id for loan in self.get("report_overdue").context["loans"]
        }
        from_list = {
            loan.id
            for loan in self.client.get(
                reverse("loan_list"), {"status": "overdue"}
            ).context["loans"]
        }

        self.assertEqual(from_report, from_list)

    def test_the_longest_overdue_comes_first(self):
        recent = self.a_loan("REP-2020", days_ago=30, due_in=-2)
        ancient = self.a_loan("REP-2021", days_ago=60, due_in=-40)

        listed = [
            loan.id for loan in self.get("report_overdue").context["loans"]
        ]

        self.assertEqual(listed, [ancient.id, recent.id])

    def test_a_returned_loan_is_never_overdue(self):
        self.a_loan(
            "REP-2030", days_ago=30, due_in=-10, returned=self.today
        )

        self.assertEqual(list(self.get("report_overdue").context["loans"]), [])

    def test_it_shows_what_is_needed_to_chase_it(self):
        self.a_loan("REP-2040", days_ago=30, due_in=-6)

        response = self.get("report_overdue")

        for expected in (
            "Hafsa", "REG-7", "0366-1",
            "Kitab al-Kharaj", "Abu Yusuf", "REP-2040",
        ):
            with self.subTest(shows=expected):
                self.assertContains(response, expected)

    def test_the_days_overdue_are_counted(self):
        self.a_loan("REP-2050", days_ago=30, due_in=-6)

        loan = self.get("report_overdue").context["loans"][0]

        self.assertEqual(loan.days_overdue, 6)


class InventoryReportTests(ReportTestCase):

    def test_the_counts_match_the_copy_list(self):
        self.a_copy("REP-3001")
        self.a_copy("REP-3002", status="Lost")
        self.a_copy("REP-3003", status="Damaged")
        self.a_copy("REP-3004", shelf=None)

        tallies = self.get("report_inventory").context["tallies"]

        for state in ("lost", "damaged"):
            with self.subTest(state=state):
                from_list = self.client.get(
                    reverse("book_copy_list"), {"status": state}
                ).context["paginator"].count

                self.assertEqual(tallies[state], from_list)

    def test_the_total_is_every_copy_in_scope(self):
        for number in range(4):
            self.a_copy("REP-31%02d" % number)

        self.assertEqual(
            self.get("report_inventory").context["tallies"]["total"], 4
        )

    def test_the_location_filter_narrows_the_counts(self):
        self.a_copy("REP-3201", shelf=self.shelf_a)
        self.a_copy("REP-3202", shelf=self.shelf_z)

        tallies = self.get(
            "report_inventory", location=self.hall.id
        ).context["tallies"]

        self.assertEqual(tallies["total"], 1)

    def test_the_shelf_filter_narrows_them_too(self):
        self.a_copy("REP-3301", shelf=self.shelf_a)
        self.a_copy("REP-3302", shelf=self.shelf_z)

        tallies = self.get(
            "report_inventory", shelf=self.shelf_z.id
        ).context["tallies"]

        self.assertEqual(tallies["total"], 1)

    def test_a_status_filter_lists_only_those_copies(self):
        self.a_copy("REP-3401")
        lost = self.a_copy("REP-3402", status="Lost")

        listed = [
            copy.id
            for copy in self.get(
                "report_inventory", status="lost"
            ).context["copies"]
        ]

        self.assertEqual(listed, [lost.id])

    def test_the_condition_report_shows_only_the_unusable(self):
        self.a_copy("REP-3501")
        lost = self.a_copy("REP-3502", status="Lost")
        damaged = self.a_copy("REP-3503", status="Damaged")
        withdrawn = self.a_copy("REP-3504", status="Transferred")

        listed = {
            copy.id
            for copy in self.get("report_condition").context["copies"]
        }

        self.assertEqual(listed, {lost.id, damaged.id, withdrawn.id})

    def test_withdrawn_uses_the_existing_status(self):
        # Not a new state: withdrawal has always written Transferred.
        self.assertIn("transferred", reports.CONDITION_STATES)

    def test_unshelved_is_counted_only_because_it_already_exists(self):
        self.a_copy("REP-3601", shelf=None)

        self.assertEqual(
            self.get("report_inventory").context["tallies"]["unshelved"], 1
        )


class BorrowerActivityTests(ReportTestCase):

    def test_the_counts_are_right(self):
        self.a_loan("REP-4001")
        self.a_loan("REP-4002", returned=self.today)

        tallies = self.get("report_borrowers").context["tallies"]

        self.assertEqual(tallies["issued"], 2)
        self.assertEqual(tallies["returned"], 1)
        self.assertEqual(tallies["out"], 1)

    def test_one_borrower_can_be_singled_out(self):
        other = make_borrower(name="Bilal", phone="0366-3")

        self.a_loan("REP-4010")
        self.a_loan("REP-4011", borrower=other)

        response = self.get("report_borrowers", borrower=other.id)

        self.assertEqual(response.context["tallies"]["issued"], 1)
        self.assertEqual(
            [row.id for row in response.context["rows"]], [other.id]
        )

    def test_every_borrower_appears_when_none_is_chosen(self):
        other = make_borrower(name="Bilal", phone="0366-4")

        self.a_loan("REP-4020")
        self.a_loan("REP-4021", borrower=other)

        listed = {
            row.id for row in self.get("report_borrowers").context["rows"]
        }

        self.assertEqual(listed, {self.borrower.id, other.id})

    def test_a_borrower_who_did_nothing_is_not_listed(self):
        make_borrower(name="Quiet", phone="0366-5")

        self.a_loan("REP-4030")

        listed = [
            row.name for row in self.get("report_borrowers").context["rows"]
        ]

        self.assertNotIn("Quiet", listed)

    def test_there_is_no_score_or_ranking(self):
        # Counts only. The report says what happened, not who is good.
        self.a_loan("REP-4040")

        row = self.get("report_borrowers").context["rows"][0]

        self.assertFalse(hasattr(row, "score"))
        self.assertFalse(hasattr(row, "rank"))


class PopularBooksTests(ReportTestCase):

    def other_book(self, title, author):
        book = make_book(title=title, author=make_author(author))
        return book, make_volume(book=book, volume_number=1, title="")

    def test_books_are_ranked_by_how_often_they_were_lent(self):
        second, second_volume = self.other_book("Al-Muwatta", "Malik")

        for number in range(3):
            self.a_loan("REP-50%02d" % number)

        self.a_loan("REP-5100", volume=second_volume)

        books = list(self.get("report_popular").context["books"])

        self.assertEqual(books[0].id, self.book.id)
        self.assertEqual(books[0].times_issued, 3)
        self.assertEqual(books[1].id, second.id)
        self.assertEqual(books[1].times_issued, 1)

    def test_it_counts_loans_not_log_entries(self):
        self.a_loan("REP-5200")

        self.assertEqual(
            self.get("report_popular").context["books"][0].times_issued,
            Loan.objects.filter(
                copy__volume__book=self.book
            ).count(),
        )

    def test_ties_break_the_same_way_every_time(self):
        beta, beta_volume = self.other_book("Beta", "B")
        alpha, alpha_volume = self.other_book("Alpha", "A")

        self.a_loan("REP-5300", volume=beta_volume)
        self.a_loan("REP-5301", volume=alpha_volume)

        first = [b.id for b in self.get("report_popular").context["books"]]
        second = [b.id for b in self.get("report_popular").context["books"]]

        self.assertEqual(first, second)
        # Equal counts, so the title decides: Alpha before Beta.
        self.assertEqual(first, [alpha.id, beta.id])

    def test_an_archived_book_still_counts_in_the_history(self):
        self.a_loan("REP-5400")

        self.book.archived_at = timezone.now()
        self.book.save(update_fields=["archived_at"])

        books = [b.id for b in self.get("report_popular").context["books"]]

        self.assertIn(self.book.id, books)

    def test_a_book_lent_outside_the_period_is_not_ranked(self):
        self.a_loan("REP-5500", days_ago=500, due_in=-480)

        self.assertEqual(
            list(self.get("report_popular").context["books"]), []
        )

    def test_availability_comes_along_when_it_is_cheap(self):
        self.a_loan("REP-5600")
        self.a_copy("REP-5601")

        book = self.get("report_popular").context["books"][0]

        self.assertEqual(book.copies_available, 1)


class CsvExportTests(ReportTestCase):

    def test_the_export_is_a_csv_download(self):
        response = self.get("report_overdue", format="csv")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn(".csv", response["Content-Disposition"])

    def test_the_filename_is_safe(self):
        response = self.get("report_condition", format="csv")

        disposition = response["Content-Disposition"]

        for character in ("/", "\\\\", "..", " "):
            with self.subTest(character=character):
                self.assertNotIn(
                    character, disposition.split('filename="')[1]
                )

    def test_it_holds_the_same_records_the_page_showed(self):
        self.a_loan("REP-6001", days_ago=30, due_in=-5)
        self.a_loan("REP-6002", days_ago=30, due_in=-2)
        self.a_loan("REP-6003")     # not overdue

        page = self.get("report_overdue")
        exported = self.rows_of(self.get("report_overdue", format="csv"))

        self.assertEqual(len(exported) - 1, len(page.context["loans"]))

        codes = {row[5] for row in exported[1:]}
        self.assertEqual(codes, {"REP-6001", "REP-6002"})

    def test_it_honours_the_filters(self):
        self.a_loan("REP-6010", days_ago=30, due_in=-5, shelf=self.shelf_a)
        self.a_loan("REP-6011", days_ago=30, due_in=-5, shelf=self.shelf_z)

        exported = self.rows_of(
            self.get("report_overdue", format="csv", location=self.hall.id)
        )

        self.assertEqual(len(exported) - 1, 1)
        self.assertEqual(exported[1][5], "REP-6010")

    def test_it_honours_the_dates(self):
        self.a_loan("REP-6020")

        exported = self.rows_of(
            self.get(
                "report_circulation",
                format="csv",
                start="2020-01-01",
                end="2020-01-31",
            )
        )

        self.assertEqual(len(exported), 1)      # the header alone

    def test_an_unreadable_date_exports_nothing_rather_than_everything(self):
        self.a_loan("REP-6030")

        response = self.get("report_circulation", format="csv", start="soon")

        # Refused, so it renders the page with the error rather than
        # handing over a file that answers a different question.
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("text/csv", response["Content-Type"])
        self.assertContains(response, "not a date the report can read")

    def test_it_carries_a_bom_so_excel_reads_the_titles(self):
        response = self.get("report_overdue", format="csv")

        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))

    def test_every_report_exports(self):
        for name in (
            "report_circulation", "report_overdue", "report_inventory",
            "report_condition", "report_borrowers", "report_popular",
        ):
            with self.subTest(report=name):
                response = self.get(name, format="csv")

                self.assertIn("text/csv", response["Content-Type"])
                self.assertGreaterEqual(len(self.rows_of(response)), 1)


class PrintSupportTests(ReportTestCase):

    def test_the_controls_are_marked_to_be_hidden(self):
        response = self.get("report_overdue")

        self.assertContains(response, "print-hidden")

    def test_the_paper_keeps_the_context(self):
        self.a_loan("REP-7001", days_ago=30, due_in=-3)

        response = self.get("report_circulation", start="2026-01-01")

        # Title, applied period, and when it was generated.
        self.assertContains(response, "Circulation")
        self.assertContains(response, "2026-01-01")
        self.assertContains(response, "generated")
        self.assertContains(response, "print-only")

    def test_every_report_offers_printing_and_export(self):
        for name in (
            "report_circulation", "report_overdue", "report_inventory",
            "report_condition", "report_borrowers", "report_popular",
        ):
            with self.subTest(report=name):
                response = self.get(name)

                self.assertContains(response, "window.print()")
                self.assertContains(response, "format=csv")


class ReportPermissionTests(ReportTestCase):

    def all_reports(self):
        return ["reports_home"] + [name for name, *_ in reports.REPORTS]

    def test_every_role_that_can_see_the_data_can_see_the_report(self):
        # These summarise the loan list, the copy list and the borrower
        # pages, all of which every role already reaches.
        for role in ("Admin", "Librarian", "Assistant"):
            self.as_role(role)

            for name in self.all_reports():
                with self.subTest(role=role, report=name):
                    self.assertEqual(
                        self.client.get(reverse(name)).status_code, 200
                    )

    def test_no_borrower_detail_beyond_what_is_already_shown(self):
        # The phone number is on the borrower's own page for every role,
        # so the overdue report showing it exposes nothing new.
        self.a_loan("REP-8001", days_ago=30, due_in=-3)
        self.as_role("Assistant")

        self.assertContains(
            self.client.get(
                reverse("borrower_detail", args=[self.borrower.id])
            ),
            "0366-1",
        )
        self.assertContains(self.get("report_overdue"), "0366-1")

    def test_a_signed_out_visitor_gets_none_of_them(self):
        self.client.logout()

        for name in self.all_reports():
            with self.subTest(report=name):
                response = self.client.get(reverse(name))

                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])

    def test_a_signed_out_visitor_cannot_export_either(self):
        self.client.logout()

        response = self.get("report_overdue", format="csv")

        self.assertEqual(response.status_code, 302)


class ReportQueryTests(ReportTestCase):

    def test_the_overdue_report_does_not_query_per_row(self):
        self.a_loan("REP-9001", days_ago=30, due_in=-3)

        with CaptureQueriesContext(connection) as few:
            self.get("report_overdue")

        for number in range(20):
            self.a_loan("REP-91%02d" % number, days_ago=30, due_in=-3)

        with CaptureQueriesContext(connection) as many:
            response = self.get("report_overdue")

        self.assertEqual(response.context["total"], 21)
        self.assertEqual(len(few), len(many))

    def test_the_circulation_counts_are_one_aggregate(self):
        for number in range(10):
            self.a_loan("REP-92%02d" % number)

        with CaptureQueriesContext(connection) as queries:
            reports.circulation(reports.parse_range(self.get_request()))

        # One aggregate over the loans, one count of the renewals.
        self.assertEqual(len(queries), 2)

    def get_request(self):
        from django.test import RequestFactory

        return RequestFactory().get("/")

    def test_the_inventory_counts_are_one_query(self):
        for number in range(10):
            self.a_copy("REP-93%02d" % number)

        with CaptureQueriesContext(connection) as queries:
            reports.inventory_counts(BookCopy.objects.all(), self.today)

        self.assertEqual(len(queries), 1)

    def test_borrower_activity_does_not_query_per_borrower(self):
        self.a_loan("REP-9400")

        with CaptureQueriesContext(connection) as few:
            self.get("report_borrowers")

        for number in range(10):
            self.a_loan(
                "REP-94%02d" % (number + 1),
                borrower=make_borrower(
                    name="Person %d" % number, phone="0377-%d" % number
                ),
            )

        with CaptureQueriesContext(connection) as many:
            self.get("report_borrowers")

        self.assertEqual(len(few), len(many))

    def test_popular_books_does_not_query_per_book(self):
        self.a_loan("REP-9500")

        with CaptureQueriesContext(connection) as few:
            self.get("report_popular")

        for number in range(10):
            book = make_book(
                title="Book %d" % number, author=make_author("A %d" % number)
            )
            volume = make_volume(book=book, volume_number=1, title="")
            self.a_loan("REP-96%02d" % number, volume=volume)

        with CaptureQueriesContext(connection) as many:
            response = self.get("report_popular")

        self.assertEqual(len(response.context["books"]), 11)
        self.assertEqual(len(few), len(many))
