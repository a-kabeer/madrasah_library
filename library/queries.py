"""The standard ways of asking for things, kept in one place.

Four questions that more than one part of the application asks, and that
must be answered the same way every time: which books are still in the
catalogue, which copies are out, which are overdue, and how many copies a
book has.

They live here rather than in the views because the reports and the
analytics need them too. While they were in views.py those two modules had
to import them from inside a function - views.py imports both of them, so
the other direction could not be taken at the top of the file. Now nothing
imports upwards and the imports are ordinary ones.
"""

from django.db import models

from .models import (
    Book,
    BookCopy,
    Loan,
)


def active_loan_copies():
    """Copy ids with a loan still out. A subquery, so nothing is fetched.

    `loans` has a partial unique index on copy_id WHERE return_date IS
    NULL, so there is at most one of these per copy — which is what lets
    the list treat "has an active loan" as a state rather than a count.
    """

    return Loan.objects.filter(
        return_date__isnull=True
    ).values("copy_id")


def overdue_loan_copies(today):
    """Copy ids whose active loan is past its due date."""

    return Loan.objects.filter(
        return_date__isnull=True,
        due_date__lt=today,
    ).values("copy_id")


def annotate_copy_counts(books):
    """Total, available and issued copy counts, in the list's own query.

    One pass over the join that is already there rather than a count per
    row, so a page of results costs the same whatever the catalogue holds.
    `distinct=True` on each because the three aggregates share one join and
    would otherwise multiply each other.

    The definitions are the copy list's, not new ones: a copy is out when a
    loan says so, and available only when it is on a shelf, marked
    Available, and not out. Withdrawn copies - Lost, Damaged, Missing,
    Transferred - fall into neither, which is why the two can be less than
    the total.
    """

    out = active_loan_copies()

    return books.annotate(
        total_copies=models.Count(
            "bookvolume__bookcopy",
            distinct=True,
        ),
        issued_copies=models.Count(
            "bookvolume__bookcopy",
            filter=models.Q(bookvolume__bookcopy__id__in=out),
            distinct=True,
        ),
        available_copies=models.Count(
            "bookvolume__bookcopy",
            filter=models.Q(
                bookvolume__bookcopy__status=BookCopy.STATUS_AVAILABLE,
                bookvolume__bookcopy__shelf__isnull=False,
            ) & ~models.Q(bookvolume__bookcopy__id__in=out),
            distinct=True,
        ),
    )


def active_books():
    """The catalogue, without the books that have been archived.

    Every screen that means "the library's books" goes through this, so
    excluding archived ones is one decision in one place rather than a
    filter each caller has to remember. `idx_books_archived_at` is a
    partial index over the archived rows, which is the small side.
    """

    return Book.objects.filter(archived_at__isnull=True)
