"""The borrowing rules: where they are configured, and that they hold.

The rules are one thing and their enforcement is another, so both are
tested. The first half is library/policy.py answering questions in
isolation; the second half is the issue and renewal views refusing real
requests, including requests that never went near the form.

`OrganizationSettings` is a single row with a CHECK pinning it to id 1, so
each test that needs a rule writes that row rather than passing a policy
object around - which is also what proves the columns are read.
"""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library import policy
from library.models import (
    ActivityLog,
    BookCopy,
    Loan,
    OrganizationSettings,
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


def configure(**rules):
    """Write the policy row, the way the settings screen would."""

    row = OrganizationSettings.load()

    for field, value in rules.items():
        setattr(row, field, value)

    row.save()

    return row


class PolicyBase(TestCase):

    def setUp(self):
        self.admin = make_user(
            username="admin_u", password="pass12345", role="Admin"
        )
        self.client.login(username="admin_u", password="pass12345")

        self.book = make_book(
            title="Sahih al-Bukhari", author=make_author(name="Al-Bukhari")
        )
        self.volume = make_volume(book=self.book, volume_number=1, title="")
        self.shelf = make_shelf(location=make_location(name="Hall"))

        self.borrower = make_borrower(name="Yusuf", phone="0311-1")
        self.today = timezone.now().date()

    def a_copy(self, code, status="Available"):
        return make_copy(
            volume=self.volume,
            shelf=self.shelf,
            copy_code=code,
            status=status,
        )

    def issue(self, copies, borrower=None, **extra):
        """Post the issue form directly - no GET, no form state."""

        payload = {
            "borrower": (borrower or self.borrower).id,
            "copies": [copy.id for copy in copies],
            "issue_date": self.today.isoformat(),
            "due_date": (self.today + timedelta(days=7)).isoformat(),
        }
        payload.update(extra)

        return self.client.post(reverse("circulation_issue"), payload)

    def hold(self, count, *, overdue=0, prefix="H"):
        """Put `count` books in this borrower's hands, `overdue` of them late."""

        loans = []

        for index in range(count):
            late = index < overdue

            loans.append(
                make_loan(
                    copy=self.a_copy("%s-%d" % (prefix, index), "Issued"),
                    borrower=self.borrower,
                    issue_date=self.today - timedelta(days=30),
                    due_date=(
                        self.today - timedelta(days=3)
                        if late
                        else self.today + timedelta(days=7)
                    ),
                )
            )

        return loans


class PolicyResolutionTests(PolicyBase):
    """What the rules are, before anything is enforced with them."""

    def test_an_unconfigured_library_lends_as_it_always_did(self):
        # The behaviour before there was anywhere to configure it: fourteen
        # days, no limits, overdue items no obstacle.
        rules = policy.load()

        self.assertEqual(rules.loan_period_days, 14)
        self.assertEqual(rules.max_active_loans, 0)
        self.assertEqual(rules.max_renewals, 0)
        self.assertFalse(rules.block_when_overdue)
        self.assertFalse(rules.limits_active_loans)
        self.assertFalse(rules.limits_renewals)

    def test_configured_values_are_read_back(self):
        configure(
            loan_period_days=21,
            max_active_loans=3,
            max_renewals=2,
            block_when_overdue=True,
        )

        rules = policy.load()

        self.assertEqual(rules.loan_period_days, 21)
        self.assertEqual(rules.max_active_loans, 3)
        self.assertEqual(rules.max_renewals, 2)
        self.assertTrue(rules.block_when_overdue)

    def test_zero_means_no_limit_and_null_means_unset(self):
        configure(max_active_loans=0, max_renewals=None)

        rules = policy.load()

        self.assertFalse(rules.limits_active_loans)
        self.assertFalse(rules.limits_renewals)

    def test_the_due_date_comes_from_the_period(self):
        configure(loan_period_days=10)

        self.assertEqual(
            policy.load().due_date_for(self.today),
            self.today + timedelta(days=10),
        )

    def test_the_rules_cannot_be_adjusted_in_flight(self):
        # Frozen, so nothing downstream can quietly relax a limit.
        with self.assertRaises(Exception):
            policy.load().max_active_loans = 99

    def test_counting_ignores_returned_loans(self):
        self.hold(2)

        returned = make_loan(
            copy=self.a_copy("R-1"),
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=20),
            due_date=self.today - timedelta(days=6),
            return_date=self.today - timedelta(days=5),
        )

        counts = policy.loan_counts(self.borrower, self.today)

        self.assertEqual(counts["active"], 2)
        self.assertEqual(counts["overdue"], 0)
        self.assertIsNotNone(returned.return_date)


class DueDateTests(PolicyBase):
    """The configured period fills the form in; it does not overrule anyone."""

    def test_the_issue_form_offers_the_configured_due_date(self):
        configure(loan_period_days=30)

        response = self.client.get(reverse("circulation_issue"))

        self.assertEqual(
            response.context["default_due_date"],
            (self.today + timedelta(days=30)).isoformat(),
        )

    def test_an_unconfigured_library_still_offers_fourteen_days(self):
        response = self.client.get(reverse("circulation_issue"))

        self.assertEqual(
            response.context["default_due_date"],
            (self.today + timedelta(days=14)).isoformat(),
        )

    def test_an_explicit_due_date_is_honoured(self):
        # Choosing the date is behaviour that predates the policy. The
        # period is a default, not a ceiling.
        configure(loan_period_days=7)

        chosen = self.today + timedelta(days=45)

        response = self.issue(
            [self.a_copy("DD-1")], due_date=chosen.isoformat()
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Loan.objects.get().due_date, chosen)

    def test_the_existing_one_year_ceiling_is_untouched(self):
        response = self.issue(
            [self.a_copy("DD-2")],
            due_date=(self.today + timedelta(days=400)).isoformat(),
        )

        self.assertContains(response, "more than one year")
        self.assertFalse(Loan.objects.exists())


class ActiveLoanLimitTests(PolicyBase):

    def test_with_no_limit_a_borrower_may_hold_many(self):
        self.hold(6)

        response = self.issue([self.a_copy("N-1")])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Loan.objects.filter(
                borrower=self.borrower, return_date__isnull=True
            ).count(),
            7,
        )

    def test_the_limit_refuses_the_one_that_would_exceed_it(self):
        configure(max_active_loans=3)
        self.hold(3)

        response = self.issue([self.a_copy("L-1")])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "which is the limit of 3")
        self.assertFalse(
            Loan.objects.filter(copy__copy_code="L-1").exists()
        )

    def test_up_to_the_limit_is_allowed(self):
        configure(max_active_loans=3)
        self.hold(2)

        response = self.issue([self.a_copy("L-2")])

        self.assertEqual(response.status_code, 302)

    def test_a_basket_is_counted_whole_not_copy_by_copy(self):
        # Each of these three is under the limit on its own. The submission
        # is not.
        configure(max_active_loans=4)
        self.hold(2)

        response = self.issue(
            [self.a_copy("B-1"), self.a_copy("B-2"), self.a_copy("B-3")]
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "only 2 more can be issued")
        self.assertEqual(
            Loan.objects.filter(borrower=self.borrower).count(), 2
        )

    def test_a_refused_basket_leaves_no_copy_issued(self):
        configure(max_active_loans=2)
        self.hold(2)

        copies = [self.a_copy("B-4"), self.a_copy("B-5")]

        self.issue(copies)

        for copy in copies:
            with self.subTest(copy=copy.copy_code):
                copy.refresh_from_db()
                self.assertEqual(copy.status, "Available")

    def test_a_basket_that_exactly_fills_the_allowance_is_allowed(self):
        configure(max_active_loans=4)
        self.hold(2)

        response = self.issue([self.a_copy("B-6"), self.a_copy("B-7")])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Loan.objects.filter(
                borrower=self.borrower, return_date__isnull=True
            ).count(),
            4,
        )

    def test_returning_a_book_makes_room_again(self):
        configure(max_active_loans=2)
        loans = self.hold(2)

        self.assertContains(self.issue([self.a_copy("RM-1")]), "the limit")

        # Given back through the existing return workflow, not by hand.
        returned = self.client.post(
            reverse("loan_return", args=[loans[0].id]),
            {"return_date": self.today.isoformat()},
        )

        self.assertEqual(returned.status_code, 302)
        self.assertEqual(self.issue([self.a_copy("RM-2")]).status_code, 302)

    def test_the_limit_is_per_borrower(self):
        configure(max_active_loans=1)
        self.hold(1)

        other = make_borrower(name="Someone Else", phone="0311-2")

        self.assertEqual(
            self.issue([self.a_copy("PB-1")], borrower=other).status_code,
            302,
        )


class OverdueBlockTests(PolicyBase):

    def test_off_by_default_an_overdue_item_does_not_block(self):
        self.hold(2, overdue=1)

        self.assertEqual(self.issue([self.a_copy("O-1")]).status_code, 302)

    def test_on_it_blocks_and_says_why(self):
        configure(block_when_overdue=True)
        self.hold(2, overdue=1)

        response = self.issue([self.a_copy("O-2")])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "overdue item")
        self.assertFalse(
            Loan.objects.filter(copy__copy_code="O-2").exists()
        )

    def test_a_borrower_with_nothing_late_is_unaffected(self):
        configure(block_when_overdue=True)
        self.hold(2)

        self.assertEqual(self.issue([self.a_copy("O-3")]).status_code, 302)

    def test_a_loan_due_today_is_not_overdue(self):
        # The existing rule: overdue means past the due date, not on it.
        configure(block_when_overdue=True)

        make_loan(
            copy=self.a_copy("O-4", "Issued"),
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=14),
            due_date=self.today,
        )

        self.assertEqual(self.issue([self.a_copy("O-5")]).status_code, 302)

    def test_returning_the_late_book_clears_the_block(self):
        configure(block_when_overdue=True)
        loans = self.hold(1, overdue=1)

        self.assertContains(self.issue([self.a_copy("O-6")]), "overdue")

        self.client.post(
            reverse("loan_return", args=[loans[0].id]),
            {"return_date": self.today.isoformat()},
        )

        self.assertEqual(self.issue([self.a_copy("O-7")]).status_code, 302)


class InactiveBorrowerTests(PolicyBase):

    def test_an_inactive_borrower_is_refused_whatever_the_policy_says(self):
        suspended = make_borrower(
            name="Suspended", phone="0311-3", is_active=False
        )

        response = self.issue([self.a_copy("I-1")], borrower=suspended)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "inactive")
        self.assertFalse(Loan.objects.exists())

    def test_a_nonexistent_borrower_is_refused(self):
        response = self.client.post(
            reverse("circulation_issue"),
            {
                "borrower": 999999,
                "copies": [self.a_copy("I-2").id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Loan.objects.exists())


class DirectPostTests(PolicyBase):
    """The rules are not in the form, so the form cannot be gone around."""

    def test_a_hand_made_post_is_held_to_the_limit(self):
        configure(max_active_loans=1)
        self.hold(1)

        # No GET first, no form state, ids supplied directly.
        response = self.client.post(
            reverse("circulation_issue"),
            {
                "borrower": self.borrower.id,
                "copies": [self.a_copy("DP-1").id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "the limit")

    def test_repeating_a_copy_id_does_not_multiply_the_allowance(self):
        configure(max_active_loans=2)

        copy = self.a_copy("DP-2")

        response = self.client.post(
            reverse("circulation_issue"),
            {
                "borrower": self.borrower.id,
                "copies": [copy.id, copy.id, copy.id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        # Whatever the form was told, one copy can only be lent once - the
        # partial unique index on loans(copy_id) sees to that - and nothing
        # about repeating an id gets a borrower past their limit.
        self.assertLessEqual(
            Loan.objects.filter(
                borrower=self.borrower, return_date__isnull=True
            ).count(),
            2,
        )
        self.assertLessEqual(
            Loan.objects.filter(copy=copy, return_date__isnull=True).count(),
            1,
        )
        self.assertIn(response.status_code, (200, 302))

    def test_a_crafted_copy_id_is_still_checked_for_availability(self):
        configure(max_active_loans=5)

        issued_elsewhere = self.a_copy("DP-3", "Issued")

        response = self.issue([issued_elsewhere])

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Loan.objects.filter(copy=issued_elsewhere).exists()
        )


class LockingTests(PolicyBase):
    """The counts are read under a lock, not merely before the write."""

    def test_the_writing_transaction_takes_the_borrower_before_a_copy(self):
        # Two transactions run per issue: the first locks each copy to check
        # it is still available and lets go again, the second is the one
        # that writes. What matters is the order inside the second - the
        # borrower first, always, so two baskets sharing a borrower cannot
        # end up holding half of each other's rows.
        configure(max_active_loans=3)
        self.hold(1)

        with CaptureQueriesContext(connection) as queries:
            self.issue([self.a_copy("LK-1")])

        locks = [
            q["sql"] for q in queries.captured_queries
            if "FOR UPDATE" in q["sql"]
        ]

        borrower_locks = [
            index for index, sql in enumerate(locks)
            if "borrowers" in sql
        ]

        self.assertTrue(borrower_locks, "the borrower row was never locked")

        # A copy is locked after it: that pair is the writing transaction.
        self.assertTrue(
            any(
                "book_copies" in sql
                for sql in locks[borrower_locks[0] + 1:]
            ),
            "no copy was locked after the borrower",
        )

    def test_the_check_happens_inside_the_writing_transaction(self):
        # A check made before the transaction is advice: the answer can
        # change between asking and writing. This pins that the refusal and
        # the rollback are the same event - a refused basket leaves no loan,
        # no status change and no log entry behind.
        configure(max_active_loans=1)
        self.hold(1)

        before = ActivityLog.objects.count()

        self.issue([self.a_copy("LK-2")])

        self.assertEqual(ActivityLog.objects.count(), before)
        self.assertEqual(
            BookCopy.objects.filter(copy_code="LK-2").get().status,
            "Available",
        )


class RenewalTests(PolicyBase):

    def setUp(self):
        super().setUp()

        self.loan = make_loan(
            copy=self.a_copy("RN-1", "Issued"),
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=10),
            due_date=self.today + timedelta(days=4),
        )

    def renew(self):
        return self.client.post(
            reverse("loan_renew", args=[self.loan.id])
        )

    def test_a_renewal_extends_by_the_configured_period(self):
        configure(loan_period_days=21)

        was = self.loan.due_date

        self.assertEqual(self.renew().status_code, 302)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.due_date, was + timedelta(days=21))

    def test_without_configuration_it_extends_by_fourteen(self):
        was = self.loan.due_date

        self.renew()

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.due_date, was + timedelta(days=14))

    def test_with_no_limit_it_can_be_renewed_repeatedly(self):
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                self.assertEqual(self.renew().status_code, 302)

    def test_the_limit_stops_it_and_says_why(self):
        configure(max_renewals=2)

        self.assertEqual(self.renew().status_code, 302)
        self.assertEqual(self.renew().status_code, 302)

        response = self.renew()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "which is the limit of 2")

    def test_a_refused_renewal_does_not_move_the_due_date(self):
        configure(max_renewals=1)

        self.renew()

        self.loan.refresh_from_db()
        reached = self.loan.due_date

        self.renew()

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.due_date, reached)

    def test_the_page_says_how_many_renewals_are_left(self):
        configure(max_renewals=2)
        self.renew()

        response = self.client.get(
            reverse("loan_renew", args=[self.loan.id])
        )

        self.assertEqual(response.context["renewals_used"], 1)
        self.assertEqual(response.context["renewal_limit"], 2)

    def test_the_count_comes_from_the_activity_log(self):
        configure(max_renewals=3)
        self.renew()
        self.renew()

        self.assertEqual(policy.renewals_used(self.loan), 2)
        self.assertEqual(
            ActivityLog.objects.filter(
                action="RENEW", entity_type="Loan", entity_id=self.loan.id
            ).count(),
            2,
        )

    def test_the_limit_applies_to_a_loan_made_before_it_was_set(self):
        # Counted from the log, which has recorded renewals all along, so
        # turning the rule on is not a fresh start for loans already out.
        self.renew()
        self.renew()

        configure(max_renewals=1)

        self.assertContains(self.renew(), "the limit of 1")

    def test_a_returned_loan_cannot_be_renewed(self):
        self.loan.return_date = self.today
        self.loan.save(update_fields=["return_date"])

        response = self.renew()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already been returned")

    def test_a_returned_loan_is_refused_even_with_renewals_left(self):
        configure(max_renewals=5)

        self.loan.return_date = self.today
        self.loan.save(update_fields=["return_date"])

        was = self.loan.due_date

        self.renew()

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.due_date, was)

    def test_renewal_is_re_checked_under_a_lock(self):
        configure(max_renewals=1)

        with CaptureQueriesContext(connection) as queries:
            self.renew()

        self.assertTrue([
            q["sql"] for q in queries.captured_queries
            if "FOR UPDATE" in q["sql"] and "loans" in q["sql"]
        ])

    def test_who_may_renew_is_unchanged(self):
        for role, expected in (
            ("Admin", 302),
            ("Librarian", 302),
            ("Assistant", 403),
        ):
            with self.subTest(role=role):

                self.client.logout()
                make_user(
                    username="r_%s" % role,
                    password="pass12345",
                    role=role,
                )
                self.client.login(
                    username="r_%s" % role, password="pass12345"
                )

                self.assertEqual(self.renew().status_code, expected)


class PolicySettingsTests(PolicyBase):
    """Who may change the rules, and what the screen will accept."""

    url = reverse("branding_settings")

    def save(self, **fields):
        payload = {"section": "policy"}
        payload.update(fields)

        return self.client.post(self.url, payload)

    def test_an_admin_can_set_the_rules(self):
        response = self.save(
            loan_period_days="21",
            max_active_loans="4",
            max_renewals="2",
            block_when_overdue="on",
        )

        self.assertEqual(response.status_code, 302)

        rules = policy.load()

        self.assertEqual(rules.loan_period_days, 21)
        self.assertEqual(rules.max_active_loans, 4)
        self.assertEqual(rules.max_renewals, 2)
        self.assertTrue(rules.block_when_overdue)

    def test_blank_means_back_to_the_default(self):
        configure(loan_period_days=30, max_active_loans=2)

        self.save(
            loan_period_days="", max_active_loans="", max_renewals=""
        )

        row = OrganizationSettings.load()

        self.assertIsNone(row.loan_period_days)
        self.assertIsNone(row.max_active_loans)
        self.assertEqual(policy.load().loan_period_days, 14)

    def test_the_checkbox_off_turns_the_overdue_block_off(self):
        configure(block_when_overdue=True)

        self.save(loan_period_days="14")

        self.assertFalse(policy.load().block_when_overdue)

    def test_nonsense_is_refused_and_nothing_is_saved(self):
        for field, value, says in (
            ("loan_period_days", "abc", "whole number"),
            ("loan_period_days", "0", "between"),
            ("loan_period_days", "999", "between"),
            ("max_active_loans", "-1", "whole number"),
            ("max_renewals", "500", "between"),
        ):
            with self.subTest(field=field, value=value):

                response = self.save(**{field: value})

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, says)
                self.assertIsNone(
                    getattr(OrganizationSettings.load(), field)
                )

    def test_saving_the_policy_does_not_blank_the_branding(self):
        # Two forms, two pieces of state. One must not clear the other.
        configure(name="Al Noor Library", primary_color="#ff8800")

        self.save(loan_period_days="20")

        row = OrganizationSettings.load()

        self.assertEqual(row.name, "Al Noor Library")
        self.assertEqual(row.primary_color, "#ff8800")
        self.assertEqual(row.loan_period_days, 20)

    def test_saving_the_branding_does_not_blank_the_policy(self):
        configure(loan_period_days=20, max_active_loans=3)

        self.client.post(
            self.url,
            {
                "name": "Al Noor Library",
                "primary_color": "",
                "secondary_color": "",
                "accent_color": "",
            },
        )

        row = OrganizationSettings.load()

        self.assertEqual(row.name, "Al Noor Library")
        self.assertEqual(row.loan_period_days, 20)
        self.assertEqual(row.max_active_loans, 3)

    def test_the_screen_shows_the_rules_in_force(self):
        configure(loan_period_days=21, max_active_loans=4)

        response = self.client.get(self.url)

        self.assertContains(response, 'name="loan_period_days"')
        self.assertContains(response, 'value="21"')
        self.assertContains(response, 'name="max_active_loans"')
        self.assertContains(response, 'name="block_when_overdue"')

    def test_nobody_but_an_admin_may_change_them(self):
        for role in ("Librarian", "Assistant"):
            with self.subTest(role=role):

                self.client.logout()
                make_user(
                    username="p_%s" % role,
                    password="pass12345",
                    role=role,
                )
                self.client.login(
                    username="p_%s" % role, password="pass12345"
                )

                self.assertEqual(
                    self.client.get(self.url).status_code, 403
                )
                self.assertEqual(
                    self.save(loan_period_days="99").status_code, 403
                )
                self.assertIsNone(
                    OrganizationSettings.load().loan_period_days
                )
