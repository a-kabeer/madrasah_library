"""Analytics: what the library's records say, and what they do not.

Four properties carry these tests.

Every figure is computed from the live rows. There is no stored number, no
snapshot and no counter, so the tests set up records and read the page -
never a cache to prime or a job to run.

Two scopes, kept apart on purpose. "Issued" and "Returned" are about the
selected period; "out now" and "overdue now" are the position today. That
is Task 15's split, and the tests below pin both halves so a later change
cannot quietly merge them.

Never borrowed is not "not borrowed lately". A book lent once years ago is
not in that list however quiet this term was, and a book with no copies is
in neither it nor the underused list - it has not been ignored, there is
nothing to ignore.

And the cost is fixed. Every ranking is limited in SQL and every tally is a
conditional aggregate, so the query count is compared between a small
library and one ten times the size rather than merely asserted to be small.
"""

from datetime import date, timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library import analytics
from library.models import (
    ActivityLog,
    Book,
    InventoryScan,
    InventorySession,
    Loan,
    Notification,
)

from .helpers import (
    main_content,
    make_author,
    make_book,
    make_borrower,
    make_category,
    make_copy,
    make_location,
    make_shelf,
    make_user,
    make_volume,
)


class AnalyticsTestCase(TestCase):
    """One shelf, one author, and whatever each test needs on it."""

    # Every copy code in a test comes off this, so no two collide.
    made = 0

    def setUp(self):
        self.url = reverse("analytics")

        self.today = timezone.localdate()

        self.location = make_location(name="Main Hall")
        self.shelf = make_shelf(location=self.location, shelf_code="A1")

        self.author = make_author("Ibn Hajar")
        self.hadith = make_category("Hadith")

        self.admin = make_user(
            username="admin_a", password="pass12345", role="Admin"
        )
        self.librarian = make_user(
            username="librarian_a", password="pass12345", role="Librarian"
        )
        self.assistant = make_user(
            username="assistant_a", password="pass12345", role="Assistant"
        )

        self.sign_in("librarian_a")

    def sign_in(self, username):
        self.client.login(username=username, password="pass12345")

    def a_book(self, title, category=None, copies=1, author=None):
        """A book with a volume and `copies` copies on the shelf."""

        book = make_book(
            title=title,
            author=author or self.author,
            category=category if category is not None else self.hadith,
        )
        volume = make_volume(book=book, volume_number=1, title="")

        # Codes off a running counter, not off the title: two books called
        # "Book 00" and "Book 01" share their first six characters, and a
        # copy code is unique across the whole library.
        book.copy_list = []

        for _ in range(copies):
            self.made += 1

            book.copy_list.append(
                make_copy(
                    volume=volume,
                    shelf=self.shelf,
                    copy_code="CP-%05d" % self.made,
                )
            )

        return book

    def a_loan(self, book, borrower, issued_days_ago=1, returned_days_ago=None,
               due_days_ago=None, copy=None):
        """One loan, dated relative to today so periods are deterministic."""

        issue = self.today - timedelta(days=issued_days_ago)

        due = (
            self.today - timedelta(days=due_days_ago)
            if due_days_ago is not None
            else issue + timedelta(days=14)
        )

        returned = (
            self.today - timedelta(days=returned_days_ago)
            if returned_days_ago is not None
            else None
        )

        if copy is None:
            self.made += 1
            copy = make_copy(
                volume=book.bookvolume_set.first(),
                shelf=self.shelf,
                copy_code="CP-%05d" % self.made,
            )

        return Loan.objects.create(
            copy=copy,
            borrower=borrower,
            issue_date=issue,
            due_date=due,
            return_date=returned,
        )

    def page(self, **params):
        return self.client.get(self.url, params)

    def context(self, key, **params):
        return self.page(**params).context[key]


# ==========================================================================
# PERIODS AND FILTERS
# ==========================================================================


class PeriodTests(AnalyticsTestCase):

    def test_the_default_is_ninety_days(self):
        period = self.context("period")

        self.assertEqual(period.key, "90d")
        self.assertEqual(period.start, self.today - timedelta(days=90))
        self.assertEqual(period.end, self.today)

    def test_each_period_has_the_boundary_it_says(self):
        for key, days in (("30d", 30), ("90d", 90), ("365d", 365)):
            with self.subTest(period=key):

                period = self.context("period", period=key)

                self.assertEqual(period.key, key)
                self.assertEqual(period.start, self.today - timedelta(days=days))
                self.assertEqual(period.end, self.today)

    def test_all_time_has_no_start(self):
        period = self.context("period", period="all")

        self.assertTrue(period.is_all_time)
        self.assertIsNone(period.start)

    def test_an_unknown_period_falls_back_to_the_default(self):
        for bad in ("junk", "7d", "", "30", "'; DROP TABLE loans; --"):
            with self.subTest(period=bad):

                self.assertEqual(self.context("period", period=bad).key, "90d")

    def test_the_fallback_is_not_silent(self):
        # The page states which window it is describing, so a typo cannot
        # be mistaken for the period that was asked for.
        response = self.page(period="junk")

        self.assertContains(response, "Last 90 days")

    def test_each_period_has_its_own_grouping(self):
        for key, grain in (
            ("30d", "day"), ("90d", "week"), ("365d", "month"),
            ("all", "month"),
        ):
            with self.subTest(period=key):
                self.assertEqual(self.context("period", period=key).grain, grain)

    def test_a_known_category_is_kept(self):
        self.assertEqual(
            self.context("period", category=self.hadith.id).category_id,
            str(self.hadith.id),
        )

    def test_an_unknown_or_invalid_category_is_dropped(self):
        for bad in ("999999", "abc", "", "-1", "1.5"):
            with self.subTest(category=bad):
                self.assertEqual(
                    self.context("period", category=bad).category_id, ""
                )

    def test_boundary_dates_are_inclusive_at_both_ends(self):
        book = self.a_book("Boundary")
        borrower = make_borrower(name="Hafsa", phone="1")

        # Exactly on the start, and today.
        self.a_loan(book, borrower, issued_days_ago=30)
        self.a_loan(book, borrower, issued_days_ago=0)

        # One day outside.
        self.a_loan(book, borrower, issued_days_ago=31)

        self.assertEqual(self.context("loans", period="30d")["issued"], 2)

    def test_the_category_filter_narrows_the_loans(self):
        fiqh = make_category("Fiqh")

        borrower = make_borrower(name="Hafsa", phone="1")

        self.a_loan(self.a_book("Hadith book"), borrower)
        self.a_loan(self.a_book("Fiqh book", category=fiqh), borrower)

        self.assertEqual(self.context("loans")["issued"], 2)
        self.assertEqual(
            self.context("loans", category=self.hadith.id)["issued"], 1
        )


# ==========================================================================
# KPIs
# ==========================================================================


class LoanSummaryTests(AnalyticsTestCase):

    def setUp(self):
        super().setUp()

        self.book = self.a_book("Fath al-Bari")
        self.borrower = make_borrower(name="Hafsa", phone="1")

    def summary(self, **params):
        return self.context("loans", **params)

    def test_issued_counts_the_period(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=5)
        self.a_loan(self.book, self.borrower, issued_days_ago=200)

        self.assertEqual(self.summary(period="30d")["issued"], 1)
        self.assertEqual(self.summary(period="365d")["issued"], 2)
        self.assertEqual(self.summary(period="all")["issued"], 2)

    def test_returned_counts_the_period_by_return_date(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=5,
                    returned_days_ago=2)
        self.a_loan(self.book, self.borrower, issued_days_ago=200,
                    returned_days_ago=190)

        self.assertEqual(self.summary(period="30d")["returned"], 1)
        self.assertEqual(self.summary(period="365d")["returned"], 2)

    def test_active_is_the_position_today_not_the_period(self):
        # Issued long before the window and still out: it is out *now*, so
        # a thirty-day page must still say so.
        self.a_loan(self.book, self.borrower, issued_days_ago=200)

        self.assertEqual(self.summary(period="30d")["issued"], 0)
        self.assertEqual(self.summary(period="30d")["active"], 1)

    def test_a_returned_loan_is_not_active(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=5,
                    returned_days_ago=1)

        self.assertEqual(self.summary()["active"], 0)

    def test_overdue_counts_what_is_late_now(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=30,
                    due_days_ago=5)

        self.assertEqual(self.summary()["overdue"], 1)

    def test_a_book_due_today_is_not_overdue(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=14,
                    due_days_ago=0)

        self.assertEqual(self.summary()["active"], 1)
        self.assertEqual(self.summary()["overdue"], 0)
        self.assertEqual(self.summary()["overdue_percent"], 0.0)

    def test_a_returned_late_loan_is_not_overdue_now(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=30,
                    due_days_ago=10, returned_days_ago=1)

        self.assertEqual(self.summary()["overdue"], 0)

    def test_the_overdue_percentage(self):
        # Four out and still in time - the default due date is two weeks
        # after issue, which for a loan issued twenty days ago is already
        # past, so these say when they are due explicitly.
        for index in range(4):
            self.a_loan(self.book, self.borrower, issued_days_ago=20,
                        due_days_ago=-10)

        self.a_loan(self.book, self.borrower, issued_days_ago=30,
                    due_days_ago=3)

        summary = self.summary()

        self.assertEqual(summary["active"], 5)
        self.assertEqual(summary["overdue"], 1)
        self.assertEqual(summary["overdue_percent"], 20.0)

    def test_nothing_out_is_zero_percent_not_a_crash(self):
        summary = self.summary()

        self.assertEqual(summary["active"], 0)
        self.assertEqual(summary["overdue"], 0)
        self.assertEqual(summary["overdue_percent"], 0.0)

    def test_everything_out_is_a_hundred_percent(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=30,
                    due_days_ago=3)

        self.assertEqual(self.summary()["overdue_percent"], 100.0)

    def test_unique_borrowers_counts_people_not_loans(self):
        other = make_borrower(name="Bilal", phone="2")

        for _ in range(3):
            self.a_loan(self.book, self.borrower, issued_days_ago=5)

        self.a_loan(self.book, other, issued_days_ago=5)

        summary = self.summary()

        self.assertEqual(summary["issued"], 4)
        self.assertEqual(summary["borrowers"], 2)
        self.assertEqual(summary["loans_per_borrower"], 2.0)

    def test_no_borrowers_is_zero_not_a_crash(self):
        summary = self.summary()

        self.assertEqual(summary["borrowers"], 0)
        self.assertEqual(summary["loans_per_borrower"], 0.0)

    def test_a_borrower_outside_the_period_is_not_counted(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=200)

        self.assertEqual(self.summary(period="30d")["borrowers"], 0)


class CollectionSummaryTests(AnalyticsTestCase):

    def test_total_counts_books_still_in_the_catalogue(self):
        self.a_book("One")
        self.a_book("Two")

        archived = self.a_book("Archived")
        archived.archived_at = timezone.now()
        archived.save()

        self.assertEqual(self.context("collection")["total_books"], 2)

    def test_borrowed_counts_distinct_books_in_the_period(self):
        borrower = make_borrower(name="Hafsa", phone="1")

        book = self.a_book("Borrowed twice")

        self.a_loan(book, borrower, issued_days_ago=3)
        self.a_loan(book, borrower, issued_days_ago=5)

        self.a_book("Untouched")

        collection = self.context("collection")

        self.assertEqual(collection["total_books"], 2)
        self.assertEqual(collection["borrowed_books"], 1)

    def test_never_borrowed_ignores_the_period_entirely(self):
        borrower = make_borrower(name="Hafsa", phone="1")

        # Borrowed once, long ago. Quiet this term, but not never.
        old = self.a_book("Borrowed in 2019")
        self.a_loan(old, borrower, issued_days_ago=900, returned_days_ago=890)

        self.a_book("Truly untouched")

        collection = self.context("collection", period="30d")

        self.assertEqual(collection["borrowed_books"], 0)
        self.assertEqual(collection["never_borrowed"], 1)

    def test_a_book_with_no_copies_is_not_never_borrowed(self):
        # Nothing on the shelf is not the same as nothing borrowed.
        book = make_book(title="No copies", author=self.author)
        make_volume(book=book, volume_number=1, title="")

        self.a_book("Has a copy")

        self.assertEqual(self.context("collection")["never_borrowed"], 1)

    def test_an_archived_book_is_not_never_borrowed(self):
        # It has already been dealt with; a list of decisions to make must
        # not contain a decision already made.
        book = self.a_book("Put away")
        book.archived_at = timezone.now()
        book.save()

        self.assertEqual(self.context("collection")["never_borrowed"], 0)


# ==========================================================================
# TREND
# ==========================================================================


class TrendTests(AnalyticsTestCase):

    def setUp(self):
        super().setUp()

        self.book = self.a_book("Fath al-Bari")
        self.borrower = make_borrower(name="Hafsa", phone="1")

    def test_thirty_days_is_bucketed_daily_and_complete(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=1)

        trend = self.context("trend", period="30d")

        # start..today inclusive.
        self.assertEqual(len(trend), 31)
        self.assertEqual(sum(row["loans"] for row in trend), 1)

    def test_zero_activity_buckets_are_included(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=1)

        trend = self.context("trend", period="30d")

        self.assertTrue(any(row["loans"] == 0 for row in trend))

    def test_buckets_are_in_order(self):
        trend = self.context("trend", period="30d")

        buckets = [row["bucket"] for row in trend]

        self.assertEqual(buckets, sorted(buckets))

    def test_a_loan_lands_in_its_own_day(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=3)

        trend = self.context("trend", period="30d")

        target = self.today - timedelta(days=3)

        for row in trend:
            expected = 1 if row["bucket"] == target else 0

            self.assertEqual(row["loans"], expected, msg=str(row["bucket"]))

    def test_ninety_days_is_bucketed_weekly(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=1)

        trend = self.context("trend", period="90d")

        self.assertEqual(sum(row["loans"] for row in trend), 1)
        self.assertLess(len(trend), 31)
        self.assertGreaterEqual(len(trend), 13)

    def test_a_year_is_bucketed_monthly(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=1)

        trend = self.context("trend", period="365d")

        self.assertEqual(len(trend), 13)
        self.assertEqual(sum(row["loans"] for row in trend), 1)

    def test_all_time_shows_only_the_months_with_activity(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=1)

        trend = self.context("trend", period="all")

        self.assertEqual(len(trend), 1)
        self.assertEqual(trend[0]["loans"], 1)

    def test_an_empty_period_still_renders(self):
        response = self.page(period="30d")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sum(row["loans"] for row in response.context["trend"]), 0
        )

    def test_the_trend_respects_the_category_filter(self):
        fiqh = make_category("Fiqh")

        self.a_loan(self.book, self.borrower, issued_days_ago=1)
        self.a_loan(self.a_book("Fiqh book", category=fiqh), self.borrower,
                    issued_days_ago=1)

        trend = self.context("trend", period="30d", category=fiqh.id)

        self.assertEqual(sum(row["loans"] for row in trend), 1)


# ==========================================================================
# BOOK METRICS
# ==========================================================================


class BookMetricTests(AnalyticsTestCase):

    def setUp(self):
        super().setUp()

        self.borrower = make_borrower(name="Hafsa", phone="1")

    def titles(self, key, **params):
        return [book.title for book in self.context(key, **params)]

    def test_most_borrowed_ranks_by_loan_count(self):
        busy = self.a_book("Busy")
        quiet = self.a_book("Quiet")

        for _ in range(3):
            self.a_loan(busy, self.borrower, issued_days_ago=2)

        self.a_loan(quiet, self.borrower, issued_days_ago=2)

        self.assertEqual(self.titles("most_borrowed"), ["Busy", "Quiet"])

    def test_a_book_with_many_copies_is_counted_once(self):
        book = self.a_book("Three copies", copies=3)

        for copy in book.copy_list:
            self.a_loan(book, self.borrower, issued_days_ago=2, copy=copy)

        rows = list(self.context("most_borrowed"))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].loans, 3)

    def test_loans_outside_the_period_are_not_ranked(self):
        book = self.a_book("Old favourite")

        self.a_loan(book, self.borrower, issued_days_ago=200)

        self.assertEqual(self.titles("most_borrowed", period="30d"), [])
        self.assertEqual(
            self.titles("most_borrowed", period="365d"), ["Old favourite"]
        )

    def test_ties_break_by_title_then_id(self):
        for title in ("Zulu", "Alpha", "Mike"):
            self.a_loan(self.a_book(title), self.borrower, issued_days_ago=2)

        self.assertEqual(
            self.titles("most_borrowed"), ["Alpha", "Mike", "Zulu"]
        )

    def test_most_borrowed_is_capped_at_ten(self):
        for index in range(14):
            self.a_loan(
                self.a_book("Book %02d" % index), self.borrower,
                issued_days_ago=2,
            )

        self.assertEqual(len(self.titles("most_borrowed")), 10)

    def test_out_now_is_the_position_today(self):
        book = self.a_book("Fath", copies=2)

        self.a_loan(book, self.borrower, issued_days_ago=2,
                    copy=book.copy_list[0])
        self.a_loan(book, self.borrower, issued_days_ago=3,
                    returned_days_ago=1, copy=book.copy_list[1])

        row = list(self.context("most_borrowed"))[0]

        self.assertEqual(row.loans, 2)
        self.assertEqual(row.out_now, 1)

    def test_an_archived_book_still_shows_in_usage(self):
        # Archiving does not unlend: a history that dropped last term's
        # most borrowed title would mislead.
        book = self.a_book("Archived favourite")

        self.a_loan(book, self.borrower, issued_days_ago=2)

        book.archived_at = timezone.now()
        book.save()

        self.assertEqual(
            self.titles("most_borrowed"), ["Archived favourite"]
        )

    # --- underused ------------------------------------------------------

    def test_underused_ranks_the_quietest_first(self):
        busy = self.a_book("Busy")
        quiet = self.a_book("Quiet")

        for _ in range(3):
            self.a_loan(busy, self.borrower, issued_days_ago=2)

        self.a_loan(quiet, self.borrower, issued_days_ago=2)

        self.assertEqual(self.titles("underused"), ["Quiet", "Busy"])

    def test_underused_includes_books_with_no_loans_this_period(self):
        self.a_book("Untouched")

        self.assertEqual(self.titles("underused"), ["Untouched"])

    def test_underused_excludes_books_with_no_copies(self):
        book = make_book(title="No copies", author=self.author)
        make_volume(book=book, volume_number=1, title="")

        self.a_book("Has a copy")

        self.assertEqual(self.titles("underused"), ["Has a copy"])

    def test_underused_excludes_archived_books(self):
        book = self.a_book("Put away")
        book.archived_at = timezone.now()
        book.save()

        self.a_book("Still here")

        self.assertEqual(self.titles("underused"), ["Still here"])

    def test_underused_ties_break_by_title(self):
        for title in ("Zulu", "Alpha", "Mike"):
            self.a_book(title)

        self.assertEqual(
            self.titles("underused"), ["Alpha", "Mike", "Zulu"]
        )

    def test_underused_is_capped_at_ten(self):
        for index in range(14):
            self.a_book("Book %02d" % index)

        self.assertEqual(len(self.titles("underused")), 10)

    # --- never borrowed -------------------------------------------------

    def test_never_borrowed_means_never(self):
        old = self.a_book("Borrowed once, long ago")
        self.a_loan(old, self.borrower, issued_days_ago=900,
                    returned_days_ago=890)

        self.a_book("Never at all")

        self.assertEqual(
            self.titles("never_borrowed", period="30d"), ["Never at all"]
        )

    def test_never_borrowed_excludes_books_with_no_copies(self):
        book = make_book(title="No copies", author=self.author)
        make_volume(book=book, volume_number=1, title="")

        self.a_book("Has a copy")

        self.assertEqual(self.titles("never_borrowed"), ["Has a copy"])

    def test_never_borrowed_excludes_archived_books(self):
        book = self.a_book("Put away")
        book.archived_at = timezone.now()
        book.save()

        self.assertEqual(self.titles("never_borrowed"), [])

    def test_never_borrowed_shows_the_copy_count(self):
        self.a_book("Three copies", copies=3)

        row = list(self.context("never_borrowed"))[0]

        self.assertEqual(row.copies, 3)

    def test_never_borrowed_is_capped_at_ten(self):
        for index in range(14):
            self.a_book("Book %02d" % index)

        self.assertEqual(len(self.titles("never_borrowed")), 10)

    def test_a_returned_loan_still_counts_as_ever_borrowed(self):
        book = self.a_book("Returned long ago")
        self.a_loan(book, self.borrower, issued_days_ago=500,
                    returned_days_ago=495)

        self.assertEqual(self.titles("never_borrowed"), [])


# ==========================================================================
# BORROWER METRICS
# ==========================================================================


class BorrowerMetricTests(AnalyticsTestCase):

    def setUp(self):
        super().setUp()

        self.book = self.a_book("Fath al-Bari")

    def names(self, **params):
        return [row.name for row in self.context("top_borrowers", **params)]

    def test_it_ranks_by_loans_in_the_period(self):
        busy = make_borrower(name="Busy", phone="1")
        quiet = make_borrower(name="Quiet", phone="2")

        for _ in range(3):
            self.a_loan(self.book, busy, issued_days_ago=2)

        self.a_loan(self.book, quiet, issued_days_ago=2)

        self.assertEqual(self.names(), ["Busy", "Quiet"])

    def test_a_borrower_with_no_loans_in_the_period_is_absent(self):
        old = make_borrower(name="Old", phone="1")

        self.a_loan(self.book, old, issued_days_ago=200)

        self.assertEqual(self.names(period="30d"), [])
        self.assertEqual(self.names(period="365d"), ["Old"])

    def test_an_inactive_borrower_stays_historically_visible(self):
        borrower = make_borrower(name="Departed", phone="1", is_active=False)

        self.a_loan(self.book, borrower, issued_days_ago=2)

        self.assertEqual(self.names(), ["Departed"])

    def test_out_and_overdue_are_the_position_today(self):
        borrower = make_borrower(name="Hafsa", phone="1")

        self.a_loan(self.book, borrower, issued_days_ago=2)
        self.a_loan(self.book, borrower, issued_days_ago=30, due_days_ago=5)
        self.a_loan(self.book, borrower, issued_days_ago=3,
                    returned_days_ago=1)

        row = list(self.context("top_borrowers"))[0]

        self.assertEqual(row.loans, 3)
        self.assertEqual(row.out_now, 2)
        self.assertEqual(row.overdue_now, 1)

    def test_current_figures_include_loans_from_outside_the_period(self):
        borrower = make_borrower(name="Hafsa", phone="1")

        self.a_loan(self.book, borrower, issued_days_ago=2)
        self.a_loan(self.book, borrower, issued_days_ago=200)

        row = list(self.context("top_borrowers", period="30d"))[0]

        self.assertEqual(row.loans, 1)
        self.assertEqual(row.out_now, 2)

    def test_ties_break_by_name_then_id(self):
        for name in ("Zaid", "Aisha", "Musa"):
            self.a_loan(
                self.book, make_borrower(name=name, phone=name),
                issued_days_ago=2,
            )

        self.assertEqual(self.names(), ["Aisha", "Musa", "Zaid"])

    def test_it_is_capped_at_ten(self):
        for index in range(14):
            self.a_loan(
                self.book,
                make_borrower(name="B%02d" % index, phone="p%02d" % index),
                issued_days_ago=2,
            )

        self.assertEqual(len(self.names()), 10)

    def test_the_registration_number_is_shown_when_there_is_one(self):
        borrower = make_borrower(name="Hafsa", phone="1")
        borrower.registration_no = "REG-4242"
        borrower.save()

        self.a_loan(self.book, borrower, issued_days_ago=2)

        self.assertContains(self.page(), "REG-4242")


# ==========================================================================
# CATEGORY METRICS
# ==========================================================================


class CategoryMetricTests(AnalyticsTestCase):

    def setUp(self):
        super().setUp()

        self.fiqh = make_category("Fiqh")
        self.borrower = make_borrower(name="Hafsa", phone="1")

    def rows(self, **params):
        return list(self.context("category_usage", **params))

    def test_it_counts_loans_and_distinct_books(self):
        first = self.a_book("Hadith one")
        second = self.a_book("Hadith two")

        for _ in range(2):
            self.a_loan(first, self.borrower, issued_days_ago=2)

        self.a_loan(second, self.borrower, issued_days_ago=2)

        row = self.rows()[0]

        self.assertEqual(row.name, "Hadith")
        self.assertEqual(row.loans, 3)
        self.assertEqual(row.books, 2)

    def test_many_copies_of_one_book_count_as_one_book(self):
        book = self.a_book("Three copies", copies=3)

        for copy in book.copy_list:
            self.a_loan(book, self.borrower, issued_days_ago=2, copy=copy)

        row = self.rows()[0]

        self.assertEqual(row.loans, 3)
        self.assertEqual(row.books, 1)

    def test_it_ranks_by_loans(self):
        for _ in range(3):
            self.a_loan(self.a_book("Fiqh book", category=self.fiqh),
                        self.borrower, issued_days_ago=2)

        self.a_loan(self.a_book("Hadith book"), self.borrower,
                    issued_days_ago=2)

        self.assertEqual([row.name for row in self.rows()], ["Fiqh", "Hadith"])

    def test_a_category_nobody_borrowed_from_is_omitted(self):
        self.a_loan(self.a_book("Hadith book"), self.borrower,
                    issued_days_ago=2)

        self.a_book("Fiqh book", category=self.fiqh)

        self.assertEqual([row.name for row in self.rows()], ["Hadith"])

    def test_zero_usage_everywhere_is_an_empty_ranking(self):
        self.a_book("Untouched")

        self.assertEqual(self.rows(), [])

    def test_ties_break_by_name(self):
        for name in ("Zoology", "Arabic", "Mantiq"):
            category = make_category(name)

            self.a_loan(self.a_book("%s book" % name, category=category),
                        self.borrower, issued_days_ago=2)

        self.assertEqual(
            [row.name for row in self.rows()], ["Arabic", "Mantiq", "Zoology"]
        )

    def test_loans_outside_the_period_are_not_counted(self):
        self.a_loan(self.a_book("Hadith book"), self.borrower,
                    issued_days_ago=200)

        self.assertEqual(self.rows(period="30d"), [])

    def test_the_category_filter_narrows_it_to_one_row(self):
        self.a_loan(self.a_book("Hadith book"), self.borrower,
                    issued_days_ago=2)
        self.a_loan(self.a_book("Fiqh book", category=self.fiqh),
                    self.borrower, issued_days_ago=2)

        rows = self.rows(category=self.fiqh.id)

        self.assertEqual([row.name for row in rows], ["Fiqh"])


# ==========================================================================
# LOAN DURATION
# ==========================================================================


class DurationTests(AnalyticsTestCase):

    def setUp(self):
        super().setUp()

        self.book = self.a_book("Fath al-Bari")
        self.borrower = make_borrower(name="Hafsa", phone="1")

    def duration(self, **params):
        return self.context("duration", **params)

    def test_one_returned_loan(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=10,
                    returned_days_ago=3)

        duration = self.duration()

        self.assertEqual(duration["returned"], 1)
        self.assertEqual(duration["average"], 7.0)
        self.assertEqual(duration["shortest"], 7.0)
        self.assertEqual(duration["longest"], 7.0)

    def test_several_returned_loans(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=10,
                    returned_days_ago=8)     # 2
        self.a_loan(self.book, self.borrower, issued_days_ago=20,
                    returned_days_ago=10)    # 10
        self.a_loan(self.book, self.borrower, issued_days_ago=30,
                    returned_days_ago=24)    # 6

        duration = self.duration()

        self.assertEqual(duration["returned"], 3)
        self.assertEqual(duration["average"], 6.0)
        self.assertEqual(duration["shortest"], 2.0)
        self.assertEqual(duration["longest"], 10.0)

    def test_a_same_day_return_is_nought_not_missing(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=3,
                    returned_days_ago=3)

        duration = self.duration()

        self.assertEqual(duration["returned"], 1)
        self.assertEqual(duration["shortest"], 0.0)
        self.assertEqual(duration["average"], 0.0)

    def test_active_loans_are_excluded(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=100)

        duration = self.duration()

        self.assertEqual(duration["returned"], 0)
        self.assertIsNone(duration["average"])

    def test_an_active_loan_does_not_drag_the_average(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=10,
                    returned_days_ago=6)     # 4
        self.a_loan(self.book, self.borrower, issued_days_ago=300)

        self.assertEqual(self.duration()["average"], 4.0)

    def test_nothing_returned_is_an_empty_state(self):
        duration = self.duration()

        self.assertEqual(duration["returned"], 0)
        self.assertIsNone(duration["average"])
        self.assertIsNone(duration["shortest"])
        self.assertIsNone(duration["longest"])

    def test_the_page_shows_a_dash_when_there_is_nothing_to_measure(self):
        response = self.page()

        self.assertContains(response, "nothing to measure")
        self.assertContains(response, "&mdash;")

    def test_only_loans_returned_inside_the_period_are_measured(self):
        self.a_loan(self.book, self.borrower, issued_days_ago=300,
                    returned_days_ago=290)

        self.assertEqual(self.duration(period="30d")["returned"], 0)
        self.assertEqual(self.duration(period="365d")["returned"], 1)


# ==========================================================================
# COLLECTION ATTENTION
# ==========================================================================


class CollectionAttentionTests(AnalyticsTestCase):

    def a_copy_in(self, status, code):
        book = self.a_book("Book %s" % code, copies=0)

        return make_copy(
            volume=book.bookvolume_set.first(),
            shelf=self.shelf,
            copy_code=code,
            status=status,
        )

    def attention(self, **params):
        return self.context("attention", **params)

    def test_each_condition_is_counted(self):
        for status in ("Lost", "Damaged", "Missing", "Transferred"):
            self.a_copy_in(status, "C-%s" % status[:4])

        attention = self.attention()

        self.assertEqual(attention["lost"], 1)
        self.assertEqual(attention["damaged"], 1)
        self.assertEqual(attention["missing"], 1)
        self.assertEqual(attention["transferred"], 1)

    def test_an_available_copy_is_in_none_of_them(self):
        self.a_book("Fine", copies=2)

        attention = self.attention()

        for state in ("lost", "damaged", "missing", "transferred"):
            with self.subTest(state=state):
                self.assertEqual(attention[state], 0)

    def test_the_counts_are_not_scoped_to_the_period(self):
        # A copy lost in March is still lost on a thirty-day page.
        self.a_copy_in("Lost", "C-OLD")

        self.assertEqual(self.attention(period="30d")["lost"], 1)

    def test_each_count_links_to_the_records_behind_it(self):
        response = self.page()

        for state in ("lost", "damaged", "missing", "transferred"):
            with self.subTest(state=state):
                self.assertContains(
                    response,
                    "%s?status=%s" % (reverse("book_copy_list"), state),
                )

    def test_no_risk_score_is_invented(self):
        body = self.page().content.decode().lower()

        for absent in ("risk score", "health score", "risk level"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, body)


class StockCheckFindingTests(AnalyticsTestCase):

    def setUp(self):
        super().setUp()

        self.book = self.a_book("Fath al-Bari", copies=2)

    def a_session(self, name, status, scope=InventorySession.SCOPE_SHELF,
                  shelf=None, location=None):
        completed = (
            timezone.now() if status == InventorySession.STATUS_COMPLETED
            else None
        )

        return InventorySession.objects.create(
            name=name,
            scope=scope,
            shelf=shelf if scope == InventorySession.SCOPE_SHELF else None,
            location=location if scope == InventorySession.SCOPE_LOCATION else None,
            status=status,
            started_by=self.librarian,
            started_at=timezone.now(),
            completed_at=completed,
        )

    def found(self, session, copy):
        return InventoryScan.objects.create(
            session=session,
            copy=copy,
            copy_code=copy.copy_code,
            outcome=InventoryScan.OUTCOME_FOUND,
            scanned_by=self.librarian,
            scanned_at=timezone.now(),
        )

    def findings(self):
        return self.context("stock_checks")

    def test_a_completed_check_reports_what_it_did_not_find(self):
        session = self.a_session(
            "Autumn", InventorySession.STATUS_COMPLETED, shelf=self.shelf
        )

        self.found(session, self.book.copy_list[0])

        rows = self.findings()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["missing"], 1)

    def test_a_check_that_found_everything_reports_nought(self):
        session = self.a_session(
            "Autumn", InventorySession.STATUS_COMPLETED, shelf=self.shelf
        )

        for copy in self.book.copy_list:
            self.found(session, copy)

        self.assertEqual(self.findings()[0]["missing"], 0)

    def test_an_open_check_is_not_reported(self):
        self.a_session(
            "Still counting", InventorySession.STATUS_IN_PROGRESS,
            shelf=self.shelf,
        )

        self.assertEqual(list(self.findings()), [])

    def test_an_out_of_scope_scan_is_never_expected(self):
        # A copy on another shelf was not in this session's expected set,
        # so it cannot appear as one it failed to find.
        other_shelf = make_shelf(location=self.location, shelf_code="B9")

        elsewhere = make_copy(
            volume=self.book.bookvolume_set.first(),
            shelf=other_shelf,
            copy_code="ELSEWHERE-1",
        )

        session = self.a_session(
            "Shelf A1", InventorySession.STATUS_COMPLETED, shelf=self.shelf
        )

        for copy in self.book.copy_list:
            self.found(session, copy)

        InventoryScan.objects.create(
            session=session,
            copy=elsewhere,
            copy_code=elsewhere.copy_code,
            outcome=InventoryScan.OUTCOME_OUTSIDE,
            scanned_by=self.librarian,
            scanned_at=timezone.now(),
        )

        self.assertEqual(self.findings()[0]["missing"], 0)

    def test_a_copy_unfound_twice_is_two_findings(self):
        # Counted honestly, and the page says these are findings rather
        # than distinct copies.
        for name in ("First check", "Second check"):
            self.a_session(
                name, InventorySession.STATUS_COMPLETED, shelf=self.shelf
            )

        rows = self.findings()

        self.assertEqual(len(rows), 2)
        self.assertEqual([row["missing"] for row in rows], [2, 2])

        self.assertContains(self.page(), "findings, not distinct copies")

    def test_only_the_most_recent_few_are_shown(self):
        for index in range(8):
            self.a_session(
                "Check %02d" % index, InventorySession.STATUS_COMPLETED,
                shelf=self.shelf,
            )

        self.assertEqual(len(self.findings()), analytics.RECENT_SESSIONS)

    def test_each_finding_links_to_its_session(self):
        session = self.a_session(
            "Autumn", InventorySession.STATUS_COMPLETED, shelf=self.shelf
        )

        self.assertContains(
            self.page(),
            reverse("inventory_session_detail", args=[session.id]),
        )

    def test_no_copy_status_is_changed(self):
        session = self.a_session(
            "Autumn", InventorySession.STATUS_COMPLETED, shelf=self.shelf
        )

        self.page()

        for copy in self.book.copy_list:
            copy.refresh_from_db()

            self.assertEqual(copy.status, "Available")

        session.refresh_from_db()

        self.assertEqual(session.status, InventorySession.STATUS_COMPLETED)


# ==========================================================================
# PERMISSIONS AND NAVIGATION
# ==========================================================================


class PermissionTests(AnalyticsTestCase):

    def test_admin_may_open_it(self):
        self.sign_in("admin_a")

        self.assertEqual(self.page().status_code, 200)

    def test_a_librarian_may_open_it(self):
        self.sign_in("librarian_a")

        self.assertEqual(self.page().status_code, 200)

    def test_an_assistant_is_refused(self):
        self.sign_in("assistant_a")

        self.assertEqual(self.page().status_code, 403)

    def test_an_assistant_is_refused_a_direct_url_with_filters(self):
        self.sign_in("assistant_a")

        self.assertEqual(
            self.client.get(
                self.url, {"period": "all", "category": self.hadith.id}
            ).status_code,
            403,
        )

    def test_an_assistant_is_refused_a_post_too(self):
        self.sign_in("assistant_a")

        self.assertEqual(self.client.post(self.url, {}).status_code, 403)

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.client.logout()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_the_page_is_read_only(self):
        # A GET of every filter combination writes nothing at all.
        logs = ActivityLog.objects.count()
        notes = Notification.objects.count()
        books = Book.objects.count()
        loans = Loan.objects.count()

        for period in analytics.PERIODS:
            self.page(period=period)

        self.assertEqual(ActivityLog.objects.count(), logs)
        self.assertEqual(Notification.objects.count(), notes)
        self.assertEqual(Book.objects.count(), books)
        self.assertEqual(Loan.objects.count(), loans)

    def test_there_is_no_form_that_writes(self):
        # The analytics page itself writes nothing. The shell around it
        # does contain one POST form - the topbar language switcher, which
        # is on every staff page - so the question is asked of the report,
        # which is what this test is about.
        body = main_content(self.page().content.decode())

        self.assertNotIn("csrfmiddlewaretoken", body)
        self.assertNotIn('method="post"', body.lower())


class NavigationTests(AnalyticsTestCase):

    def shell(self, username):
        self.sign_in(username)

        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_sidebar_offers_it_to_admin_and_librarian(self):
        for username in ("admin_a", "librarian_a"):
            with self.subTest(user=username):
                self.assertIn(self.url, self.shell(username))

    def test_the_sidebar_hides_it_from_an_assistant(self):
        self.assertNotIn(self.url, self.shell("assistant_a"))

    def test_it_sits_beside_reports(self):
        body = self.shell("admin_a")

        self.assertLess(body.index(self.url), body.index(reverse("reports_home")))
        self.assertIn("ADMINISTRATION", body)

    def test_it_is_a_plain_link_like_every_other_entry(self):
        import re

        link = re.search(
            r'<a[^>]*href="%s"[^>]*>' % self.url, self.shell("admin_a"), re.S
        )

        self.assertIsNotNone(link)
        self.assertIn("nav-link-custom", link.group(0))
        self.assertNotIn("hx-", link.group(0))

    def test_its_path_cannot_be_confused_with_another_entry(self):
        # The sidebar lights the longest matching prefix, so the property
        # that matters is that no other entry is a prefix of this one.
        for other in (reverse("reports_home"), reverse("book_list"),
                      reverse("activity_log_list")):
            with self.subTest(other=other):
                self.assertFalse(self.url.startswith(other))
                self.assertFalse(other.startswith(self.url))

    def test_it_answers_a_navigation_with_the_region_alone(self):
        body = self.client.get(
            self.url,
            headers={"hx-request": "true", "hx-target": "mainContent"},
        ).content.decode()

        self.assertNotIn("<!DOCTYPE", body.upper())
        self.assertIn('id="pageHeading"', body)

    def test_it_is_still_a_whole_page_without_the_headers(self):
        body = self.page().content.decode()

        self.assertIn("<!DOCTYPE", body.upper())
        self.assertIn("sidebar", body)

    def test_a_navigation_keeps_the_filters(self):
        response = self.client.get(
            self.url,
            {"period": "365d"},
            headers={"hx-request": "true", "hx-target": "mainContent"},
        )

        self.assertEqual(response.context["period"].key, "365d")


# ==========================================================================
# LAYOUT
# ==========================================================================


class LayoutTests(AnalyticsTestCase):

    def test_a_library_with_no_data_renders_every_section(self):
        response = self.page()

        self.assertEqual(response.status_code, 200)

        for expected in (
            "Library Insights",
            "Borrowing trend",
            "Most borrowed",
            "Underused",
            "Never borrowed",
            "Most active borrowers",
            "Category usage",
            "How long a loan lasts",
            "Collection attention",
        ):
            with self.subTest(section=expected):
                self.assertContains(response, expected)

    def test_the_empty_states_say_something_useful(self):
        response = self.page()

        for expected in (
            "No loans in this period",
            "Nothing was borrowed in this period",
            "Nobody borrowed anything in this period",
            "No category saw a loan in this period",
            "No stock check has been completed yet",
        ):
            with self.subTest(message=expected):
                self.assertContains(response, expected)

    def test_it_carries_no_chart_library(self):
        # The project has none, and a page of counts is not a reason to
        # add one.
        body = self.page().content.decode()

        for absent in ("chart.js", "chartjs", "d3.min.js", "plotly",
                       "highcharts", "apexcharts"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, body.lower())

    def test_the_layout_uses_fluid_columns_rather_than_fixed_widths(self):
        # What makes 375px work without a breakpoint per section: the tile
        # grid fits as many as will fit and wraps the rest.
        import io
        import os

        from django.conf import settings

        css = io.open(
            os.path.join(
                settings.BASE_DIR, "library", "static", "library", "css",
                "style.css",
            ),
            encoding="utf-8",
        ).read()

        self.assertIn("auto-fit", css)
        self.assertIn(".analytics-kpis", css)

    def test_it_points_at_reports_rather_than_replacing_them(self):
        self.assertContains(self.page(), reverse("reports_home"))


# ==========================================================================
# QUERY EFFICIENCY
# ==========================================================================


class QueryCountTests(AnalyticsTestCase):
    """The page must cost the same on a big library as on a small one."""

    def populate(self, books, borrowers, loans_each):
        made = []

        for index in range(books):
            made.append(self.a_book("Book %04d" % index))

        people = [
            make_borrower(name="B%04d" % index, phone="p%04d" % index)
            for index in range(borrowers)
        ]

        for index, book in enumerate(made):
            for step in range(loans_each):
                self.a_loan(
                    book,
                    people[(index + step) % len(people)],
                    issued_days_ago=(step % 25) + 1,
                    returned_days_ago=(step % 3) if step % 2 else None,
                )

        return made

    def count_for(self, **params):
        with CaptureQueriesContext(connection) as captured:
            self.client.get(self.url, params)

        return len(captured.captured_queries)

    def test_an_empty_library_and_a_populated_one_cost_the_same(self):
        empty = self.count_for()

        self.populate(books=20, borrowers=8, loans_each=3)

        populated = self.count_for()

        self.assertEqual(empty, populated)

    def test_a_small_and_a_much_larger_library_cost_the_same(self):
        self.populate(books=5, borrowers=3, loans_each=2)

        small = self.count_for()

        self.populate(books=60, borrowers=20, loans_each=4)

        large = self.count_for()

        self.assertEqual(small, large)

    def test_every_period_costs_the_same(self):
        self.populate(books=30, borrowers=10, loans_each=3)

        counts = {
            period: self.count_for(period=period)
            for period in analytics.PERIODS
        }

        self.assertEqual(len(set(counts.values())), 1, msg=str(counts))

    def test_a_filtered_page_costs_no_more_than_a_handful_extra(self):
        self.populate(books=30, borrowers=10, loans_each=3)

        plain = self.count_for()
        filtered = self.count_for(category=self.hadith.id)

        # The category filter is validated against the table, which is one
        # lookup - and nothing else.
        self.assertLessEqual(filtered - plain, 1)

    def test_the_whole_page_is_a_bounded_number_of_queries(self):
        self.populate(books=60, borrowers=20, loans_each=4)

        self.assertLessEqual(self.count_for(), 25)

    def test_the_rankings_are_limited_in_the_database(self):
        # Not "fetch everything and slice": the row count coming back is
        # the limit, whatever the catalogue holds.
        self.populate(books=60, borrowers=20, loans_each=3)

        response = self.page()

        for key in ("most_borrowed", "underused", "never_borrowed",
                    "top_borrowers"):
            with self.subTest(section=key):
                self.assertLessEqual(
                    len(list(response.context[key])), analytics.TOP_N
                )

    def test_never_borrowed_does_not_query_per_book(self):
        self.populate(books=40, borrowers=5, loans_each=1)

        for index in range(20):
            self.a_book("Untouched %02d" % index)

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(self.url)

            list(response.context["never_borrowed"])

        book_queries = [
            query["sql"]
            for query in captured.captured_queries
            if 'FROM "books"' in query["sql"]
        ]

        # Total, borrowed, never-borrowed count, the three rankings - a
        # fixed handful, not one per book.
        self.assertLessEqual(len(book_queries), 8)

    def test_the_stock_check_section_is_bounded_by_its_own_limit(self):
        for index in range(12):
            InventorySession.objects.create(
                name="Check %02d" % index,
                scope=InventorySession.SCOPE_SHELF,
                shelf=self.shelf,
                status=InventorySession.STATUS_COMPLETED,
                started_by=self.librarian,
                started_at=timezone.now(),
                completed_at=timezone.now(),
            )

        few = self.count_for()

        for index in range(12, 40):
            InventorySession.objects.create(
                name="Check %02d" % index,
                scope=InventorySession.SCOPE_SHELF,
                shelf=self.shelf,
                status=InventorySession.STATUS_COMPLETED,
                started_by=self.librarian,
                started_at=timezone.now(),
                completed_at=timezone.now(),
            )

        many = self.count_for()

        self.assertEqual(few, many)

    def test_the_trend_returns_buckets_not_loans(self):
        self.populate(books=20, borrowers=5, loans_each=6)

        trend = self.context("trend", period="30d")

        # One row per day in the window, however many loans landed in them.
        self.assertEqual(len(trend), 31)
        self.assertGreater(sum(row["loans"] for row in trend), 31)


# ==========================================================================
# THE PERIOD HELPER ITSELF
# ==========================================================================


class PeriodHelperTests(TestCase):
    """The window object every section is handed, checked directly."""

    def test_resolve_period_accepts_only_what_it_offers(self):
        for good in analytics.PERIODS:
            self.assertEqual(analytics.resolve_period(good), good)

        for bad in (None, "", "  ", "60d", "day", "ALL"):
            self.assertEqual(
                analytics.resolve_period(bad), analytics.DEFAULT_PERIOD
            )

    def test_a_period_knows_its_own_window(self):
        today = date(2026, 6, 15)

        period = analytics.Period("30d", today)

        self.assertEqual(period.start, date(2026, 5, 16))
        self.assertEqual(period.end, today)
        self.assertFalse(period.is_all_time)

    def test_all_time_has_no_lower_bound(self):
        period = analytics.Period("all", date(2026, 6, 15))

        self.assertTrue(period.is_all_time)
        self.assertIsNone(period.start)

    def test_month_buckets_cross_a_year_end(self):
        period = analytics.Period("365d", date(2026, 2, 10))

        buckets = analytics.trend_buckets(period)

        self.assertEqual(buckets[0], date(2025, 2, 1))
        self.assertEqual(buckets[-1], date(2026, 2, 1))
        self.assertEqual(len(buckets), 13)

    def test_week_buckets_start_on_a_monday(self):
        period = analytics.Period("90d", date(2026, 6, 15))

        for bucket in analytics.trend_buckets(period):
            self.assertEqual(bucket.weekday(), 0)

    def test_day_buckets_cover_the_window_inclusively(self):
        period = analytics.Period("30d", date(2026, 6, 15))

        buckets = analytics.trend_buckets(period)

        self.assertEqual(len(buckets), 31)
        self.assertEqual(buckets[0], date(2026, 5, 16))
        self.assertEqual(buckets[-1], date(2026, 6, 15))

    def test_as_days_rounds_and_passes_none_through(self):
        self.assertIsNone(analytics.as_days(None))
        self.assertEqual(analytics.as_days(timedelta(0)), 0.0)
        self.assertEqual(analytics.as_days(timedelta(days=3)), 3.0)
        self.assertEqual(
            analytics.as_days(timedelta(days=2, hours=12)), 2.5
        )
