"""Operational reports, built from the records that already answer them.

Six questions a librarian actually asks, and nothing else. Every one of
them is answered from `Loan`, `BookCopy` and the statuses that already
exist - there is no reporting table, no nightly rollup and no second
definition of "overdue". If a number here ever disagreed with the loan
list, the loan list would be right, so the numbers are taken from the same
place it takes them from.

Filtering happens before aggregating, always. A report over a month should
cost what that month costs, not what the whole history costs and then a
filter in Python.

The date range is one pattern shared by all of them, in the query string,
so a report is a URL: bookmarkable, shareable, printable, and the exact
same thing the CSV export reads.
"""

from collections import namedtuple
from datetime import date, timedelta

from django.db import models
from django.utils import timezone

from .models import ActivityLog, Book, BookCopy, Borrower, Loan


# A month back, which is the period most of these questions are about when
# nobody says otherwise.
DEFAULT_DAYS = 30

# What the reports home offers. Kept here rather than in the template so
# the page and the URLs cannot drift apart.
REPORTS = (
    (
        "report_circulation",
        "Circulation",
        "bi-arrow-left-right",
        "What went out and came back over a period.",
    ),
    (
        "report_overdue",
        "Overdue",
        "bi-clock-history",
        "Everything out past its due date, oldest first.",
    ),
    (
        "report_inventory",
        "Inventory Status",
        "bi-boxes",
        "Where the copies are and what state they are in.",
    ),
    (
        "report_condition",
        "Lost / Damaged / Withdrawn",
        "bi-exclamation-octagon",
        "Copies that are no longer on the shelf in usable condition.",
    ),
    (
        "report_borrowers",
        "Borrower Activity",
        "bi-people",
        "What a borrower, or everyone, did over a period.",
    ),
    (
        "report_popular",
        "Popular Books",
        "bi-star",
        "Ranked by how often they were actually lent.",
    ),
)


DateRange = namedtuple("DateRange", "start end error")


def parse_range(request, days=DEFAULT_DAYS):
    """The start and end dates asked for, or the reason they are unusable.

    One pattern for every report. Blank means the default period rather
    than "everything", because a report with no range quietly covering ten
    years is the kind of answer that gets acted on before anyone notices.

    A bad date is an error, not an ignored parameter: silently reporting on
    a different period than the one asked for is worse than refusing.
    """

    today = timezone.now().date()

    raw_start = (request.GET.get("start") or "").strip()
    raw_end = (request.GET.get("end") or "").strip()

    def read(value, fallback, label):
        if not value:
            return fallback, ""

        try:
            return date.fromisoformat(value), ""

        except ValueError:
            return None, (
                "%s is not a date the report can read. Use the date "
                "picker, or the form YYYY-MM-DD." % label
            )

    start, error = read(raw_start, today - timedelta(days=days), "Start date")

    if error:
        return DateRange(None, None, error)

    end, error = read(raw_end, today, "End date")

    if error:
        return DateRange(None, None, error)

    if start > end:
        return DateRange(
            None,
            None,
            "The start date is after the end date, so the period is empty. "
            "Swap them round.",
        )

    return DateRange(start, end, "")


def circulation(dates, location_id=None, borrower_id=None, title=""):
    """What went out and came back, and what is still out.

    `issued` and `returned` are counted over the period; `out` and
    `overdue` are the position today, because "how many are out" is not a
    question about a date range. Both are said plainly on the page rather
    than left for the reader to work out.
    """

    today = timezone.now().date()

    loans = Loan.objects.all()

    if location_id:
        loans = loans.filter(copy__shelf__location_id=location_id)

    if borrower_id:
        loans = loans.filter(borrower_id=borrower_id)

    if title:
        loans = loans.filter(copy__volume__book__title__icontains=title)

    # Filtered first, then counted, and all five in one pass.
    tallies = loans.aggregate(
        issued=models.Count(
            "id",
            filter=models.Q(
                issue_date__gte=dates.start, issue_date__lte=dates.end
            ),
        ),
        returned=models.Count(
            "id",
            filter=models.Q(
                return_date__gte=dates.start, return_date__lte=dates.end
            ),
        ),
        out=models.Count("id", filter=models.Q(return_date__isnull=True)),
        overdue=models.Count(
            "id",
            filter=models.Q(
                return_date__isnull=True, due_date__lt=today
            ),
        ),
    )

    # Renewals are recorded against the loan they extended, so they are
    # counted through the loans this report is about - not from a second
    # idea of what a renewal is.
    tallies["renewals"] = ActivityLog.objects.filter(
        action="RENEW",
        entity_type="Loan",
        entity_id__in=loans.values("id"),
        created_at__date__gte=dates.start,
        created_at__date__lte=dates.end,
    ).count()

    detail = loans.filter(
        models.Q(issue_date__gte=dates.start, issue_date__lte=dates.end)
        | models.Q(return_date__gte=dates.start, return_date__lte=dates.end)
    ).select_related(
        "copy__volume__book__author",
        "borrower",
    ).order_by("-issue_date", "-id")

    return tallies, detail


def overdue(location_id=None):
    """Everything out past its due date, by the rule the rest of the app uses.

    Longest overdue first: the report is a worklist, and the book that has
    been out since March is the one to chase.
    """

    today = timezone.now().date()

    loans = Loan.objects.filter(
        return_date__isnull=True,
        due_date__lt=today,
    )

    if location_id:
        loans = loans.filter(copy__shelf__location_id=location_id)

    return loans.select_related(
        "copy__volume__book__author",
        "borrower",
    ).order_by("due_date", "copy__copy_code")


# The states the inventory report counts, in the order they are shown. The
# same names the copy list's own filter uses, so a count here and a click
# through to that list agree.
INVENTORY_STATES = (
    "available",
    "issued",
    "overdue",
    "unshelved",
    "lost",
    "damaged",
    "missing",
    "transferred",
)

# The three that mean a copy is not in usable circulation. `Transferred` is
# what withdrawal has always written - see the archive work - so this is
# the existing vocabulary, not a new one.
CONDITION_STATES = ("lost", "damaged", "missing", "transferred")


def inventory_counts(copies, today, states=INVENTORY_STATES):
    """How many copies are in each state, without eight queries.

    Each state is a condition rather than a column - `Available` means
    shelved and not out, `Overdue` is worked out from a due date - so they
    are counted as conditional aggregates over the filtered set in one
    pass, using the same definitions `filter_copies_by_state` applies.
    """

    # Imported here rather than at the top: views.py imports this module,
    # so the other direction can only be taken at call time. These two are
    # the definitions the copy list itself filters by, and writing them out
    # again here is exactly the second definition this module exists to
    # avoid.
    from .views import active_loan_copies, overdue_loan_copies

    conditions = {
        "available": models.Q(
            status=BookCopy.STATUS_AVAILABLE,
            shelf__isnull=False,
        ) & ~models.Q(id__in=active_loan_copies()),
        "issued": models.Q(id__in=active_loan_copies()),
        "overdue": models.Q(id__in=overdue_loan_copies(today)),
        "unshelved": models.Q(shelf__isnull=True),
        "lost": models.Q(status__iexact="lost"),
        "damaged": models.Q(status__iexact="damaged"),
        "missing": models.Q(status__iexact="missing"),
        "transferred": models.Q(status__iexact="transferred"),
    }

    tallies = copies.aggregate(
        total=models.Count("id"),
        **{
            state: models.Count("id", filter=conditions[state])
            for state in states
        }
    )

    return tallies


def borrower_activity(dates, borrower_id=None):
    """What one borrower, or everyone, did over a period.

    Counts, not scores. There is no ranking here and no notion of a good or
    a bad borrower: the library wants to know what happened.
    """

    today = timezone.now().date()

    loans = Loan.objects.all()

    if borrower_id:
        loans = loans.filter(borrower_id=borrower_id)

    tallies = loans.aggregate(
        issued=models.Count(
            "id",
            filter=models.Q(
                issue_date__gte=dates.start, issue_date__lte=dates.end
            ),
        ),
        returned=models.Count(
            "id",
            filter=models.Q(
                return_date__gte=dates.start, return_date__lte=dates.end
            ),
        ),
        out=models.Count("id", filter=models.Q(return_date__isnull=True)),
        overdue=models.Count(
            "id",
            filter=models.Q(
                return_date__isnull=True, due_date__lt=today
            ),
        ),
    )

    tallies["renewals"] = ActivityLog.objects.filter(
        action="RENEW",
        entity_type="Loan",
        entity_id__in=loans.values("id"),
        created_at__date__gte=dates.start,
        created_at__date__lte=dates.end,
    ).count()

    # Per borrower, when no single one was asked for. Annotated, so a
    # hundred borrowers is one query and not a hundred.
    rows = Borrower.objects.annotate(
        issued=models.Count(
            "loan",
            filter=models.Q(
                loan__issue_date__gte=dates.start,
                loan__issue_date__lte=dates.end,
            ),
            distinct=True,
        ),
        returned=models.Count(
            "loan",
            filter=models.Q(
                loan__return_date__gte=dates.start,
                loan__return_date__lte=dates.end,
            ),
            distinct=True,
        ),
        out=models.Count(
            "loan",
            filter=models.Q(loan__return_date__isnull=True),
            distinct=True,
        ),
        overdue=models.Count(
            "loan",
            filter=models.Q(
                loan__return_date__isnull=True,
                loan__due_date__lt=today,
            ),
            distinct=True,
        ),
    ).filter(
        models.Q(issued__gt=0) | models.Q(returned__gt=0) | models.Q(out__gt=0)
    ).order_by("-issued", "-out", "name", "id")

    if borrower_id:
        rows = rows.filter(id=borrower_id)

    return tallies, rows


def popular(dates, limit=50):
    """Books ranked by how often they were actually lent.

    From `Loan`, which is the record of a book leaving the building. The
    activity log also mentions issues, but it is a description of an event
    rather than the event, and counting it would be counting the receipt
    instead of the transaction.

    Archived books are included. Taking a book out of the catalogue does
    not unlend it, and a history that quietly dropped last year's most
    borrowed title the day it was archived would be misleading.

    Ties break by title and then id, so two books lent the same number of
    times come back in the same order every time the report is run.
    """

    return Book.objects.annotate(
        times_issued=models.Count(
            "bookvolume__bookcopy__loan",
            filter=models.Q(
                bookvolume__bookcopy__loan__issue_date__gte=dates.start,
                bookvolume__bookcopy__loan__issue_date__lte=dates.end,
            ),
            distinct=True,
        ),
        copies_available=models.Count(
            "bookvolume__bookcopy",
            filter=models.Q(
                bookvolume__bookcopy__status=BookCopy.STATUS_AVAILABLE
            ),
            distinct=True,
        ),
    ).filter(
        times_issued__gt=0
    ).select_related(
        "author"
    ).order_by("-times_issued", "title", "id")[:limit]
