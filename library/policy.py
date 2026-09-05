"""The borrowing rules, in one place.

Four questions decide whether a book may leave the building and how long it
may stay out:

  * how long a loan runs, which sets the due date offered;
  * how many books one borrower may hold at once;
  * whether anything overdue stops them borrowing more;
  * how many times a loan may be renewed.

They lived in two places before this module: a `DEFAULT_LOAN_PERIOD_DAYS`
constant in views.py, and nowhere - the other three were not rules the
library could state at all. Both problems have the same fix, so all four
are answered here and read from `OrganizationSettings`, the single-row
table branding already uses. Nothing else in the project may decide a
borrowing rule; views ask this module and report what it says.

Deliberately not a rule engine. There is one policy for the whole library,
not one per borrower type or per shelf: a madrasah library lends the same
way to everyone, and per-type rules would need a second screen to manage
and a second set of answers to explain. No fines, no penalties, no
suspensions - the existing `is_active` flag is how a borrower is stopped,
and it is enforced already.

The refusal functions return a sentence for the librarian, or "" for "no
objection". They are the enforcement, not a preflight for it: the issue and
renewal views call them inside the transaction that writes the loan, with
the borrower row locked, so two requests cannot each be told yes.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.db import models

from .models import ActivityLog, Loan, OrganizationSettings


# What an unconfigured library does: exactly what it did before there was
# anywhere to configure it. Fourteen days was the constant in views.py. The
# other three are off, because a limit nobody chose is a limit nobody
# expects - a library that upgrades should not discover that its borrowers
# have suddenly been capped.
DEFAULT_LOAN_PERIOD_DAYS = 14
DEFAULT_MAX_ACTIVE_LOANS = 0
DEFAULT_MAX_RENEWALS = 0
DEFAULT_BLOCK_WHEN_OVERDUE = False

# Zero means "no limit" for both counts. A separate nullable column per rule
# would say the same thing in a way every reader has to check for None.
NO_LIMIT = 0

# What the settings form will accept. The loan period has to be at least a
# day - a due date on the issue date is a loan that is overdue the moment it
# is made - and the upper bounds are the existing one-year ceiling on a due
# date and a count nobody will reach by accident.
LOAN_PERIOD_MIN = 1
LOAN_PERIOD_MAX = 365
LIMIT_MIN = 0
LIMIT_MAX = 99


@dataclass(frozen=True)
class BorrowingPolicy:
    """The four rules, resolved. Frozen: nothing may adjust them in flight."""

    loan_period_days: int = DEFAULT_LOAN_PERIOD_DAYS
    max_active_loans: int = DEFAULT_MAX_ACTIVE_LOANS
    block_when_overdue: bool = DEFAULT_BLOCK_WHEN_OVERDUE
    max_renewals: int = DEFAULT_MAX_RENEWALS

    @property
    def limits_active_loans(self):
        return self.max_active_loans > NO_LIMIT

    @property
    def limits_renewals(self):
        return self.max_renewals > NO_LIMIT

    def due_date_for(self, issue_date):
        """The due date this policy offers for a loan starting `issue_date`.

        Offered, not imposed: the issue form fills the date field with this
        and the librarian may change it, which is behaviour that predates
        the policy and is left alone.
        """

        return issue_date + timedelta(days=self.loan_period_days)


def load(settings_row=None):
    """The configured policy, falling back to the defaults above.

    `settings_row` is accepted so a caller that already has the settings in
    hand - the settings page itself - does not read the row twice.

    A NULL column means "not configured", which is what an install that
    predates these columns has in every one of them. That reads as the
    default rather than as zero, so upgrading changes nothing until someone
    chooses to.
    """

    row = (
        settings_row
        if settings_row is not None
        else OrganizationSettings.load()
    )

    def number(value, default):
        return default if value is None else int(value)

    return BorrowingPolicy(
        loan_period_days=number(
            row.loan_period_days, DEFAULT_LOAN_PERIOD_DAYS
        ),
        max_active_loans=number(
            row.max_active_loans, DEFAULT_MAX_ACTIVE_LOANS
        ),
        block_when_overdue=(
            DEFAULT_BLOCK_WHEN_OVERDUE
            if row.block_when_overdue is None
            else bool(row.block_when_overdue)
        ),
        max_renewals=number(row.max_renewals, DEFAULT_MAX_RENEWALS),
    )


def loan_counts(borrower, today):
    """How much this borrower is holding, and how much of it is late.

    One query for both, and both derived from the loans themselves - the
    same rule the loan list, the copy list and the borrower profile apply.
    Overdue is a fact about a due date; a stored count of it could only ever
    disagree.
    """

    return Loan.objects.filter(
        borrower=borrower,
        return_date__isnull=True,
    ).aggregate(
        active=models.Count("id"),
        overdue=models.Count("id", filter=models.Q(due_date__lt=today)),
    )


def refuse_issue(policy, borrower, wanted, today):
    """Why `wanted` more copies cannot go to `borrower`, or "".

    Called with the borrower's row locked, inside the transaction that
    creates the loans, so the counts it reads cannot change under it and two
    simultaneous requests cannot both be allowed. `wanted` is the whole
    basket, not one copy at a time: a limit that let five copies through in
    one submission because each of them was under it on its own would not be
    a limit.

    Returned loans are absent by construction - `loan_counts` only looks at
    `return_date IS NULL`, so giving a book back always makes room.
    """

    if borrower is None or not borrower.is_active:
        return (
            "This borrower is inactive and cannot be issued books. "
            "Reactivate the borrower first."
        )

    if not (policy.limits_active_loans or policy.block_when_overdue):
        # Nothing configured that needs counting, so nothing is counted.
        return ""

    counts = loan_counts(borrower, today)

    if policy.block_when_overdue and counts["overdue"]:
        return (
            "%s has %d overdue item%s and cannot borrow again until "
            "%s returned. Take the overdue book%s back first."
            % (
                borrower.name,
                counts["overdue"],
                "" if counts["overdue"] == 1 else "s",
                "it is" if counts["overdue"] == 1 else "they are",
                "" if counts["overdue"] == 1 else "s",
            )
        )

    if policy.limits_active_loans:

        allowance = policy.max_active_loans - counts["active"]

        if allowance <= 0:
            return (
                "%s already has %d book%s out, which is the limit of %d. "
                "One has to come back before another goes out."
                % (
                    borrower.name,
                    counts["active"],
                    "" if counts["active"] == 1 else "s",
                    policy.max_active_loans,
                )
            )

        if wanted > allowance:
            return (
                "%s has %d book%s out and the limit is %d, so only %d more "
                "can be issued - you selected %d."
                % (
                    borrower.name,
                    counts["active"],
                    "" if counts["active"] == 1 else "s",
                    policy.max_active_loans,
                    allowance,
                    wanted,
                )
            )

    return ""


def renewals_used(loan):
    """How many times this loan has been renewed.

    Counted from the activity log, which has recorded every renewal as a
    RENEW row against the loan since before this policy existed. That means
    the limit applies to loans made before it was configured, and it means
    no new column: the log is append-only here, nothing in the application
    deletes from it, and the renewal view writes its row inside the same
    transaction that moves the due date - so the count and the act it counts
    cannot come apart.
    """

    return ActivityLog.objects.filter(
        action="RENEW",
        entity_type="Loan",
        entity_id=loan.id,
    ).count()


def refuse_renewal(policy, loan):
    """Why this loan cannot be renewed, or "".

    Called with the loan's row locked, for the same reason `refuse_issue` is.
    """

    if loan.return_date is not None:
        return (
            "This loan has already been returned and cannot be renewed."
        )

    if not policy.limits_renewals:
        return ""

    used = renewals_used(loan)

    if used >= policy.max_renewals:
        return (
            "This loan has been renewed %d time%s, which is the limit of "
            "%d. It has to be returned rather than renewed again."
            % (used, "" if used == 1 else "s", policy.max_renewals)
        )

    return ""
