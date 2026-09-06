"""Suggested purchases: what to look for, and what came of each one.

A suggestion is a note, not a record of a book. Somebody at the desk writes
down a title, perhaps an author, perhaps an ISBN off a cover they saw, and
somebody senior decides whether the library should go and find it. Nothing
in this module creates an `Author`, a `Publisher`, a `Category`, a `Book`
or a `BookCopy` - the catalogue is entered through `book_add`, with its own
validation, its own duplicate check and its own permissions, and a
suggestion at most fills that form in for you.

Nor is any of this procurement. There is no supplier, no quotation, no
price, no invoice and no receiving step: those are an accounting system,
and what a small library actually needs is a list of books worth looking
for and a record of which ones were found.


The workflow
------------

    Pending ──▶ Approved ──▶ Acquired
        └────▶ Rejected

Four states, three moves, and no way back. A rejected suggestion is not
reopened and an acquired one is not un-acquired: both are a record of a
decision somebody made, and the way to revisit either is a new suggestion.

`AcquisitionSuggestion.TRANSITIONS` is the one statement of which move is
legal, and the detail page's buttons are drawn from it. That is not the
enforcement. `advance` below is, and it enforces it in the
database: a conditional UPDATE whose WHERE clause names the state the row
has to be in. So the second of two clicks, a retried POST, a refreshed
confirmation and a URL typed by hand all match nothing and change nothing,
rather than moving a review timestamp or reopening a decision. There is no
read-then-write anywhere here, and so nothing that two simultaneous
requests could both pass.
"""

from django.db import models
from django.utils import timezone

from .models import AcquisitionSuggestion


# The list's own order, everywhere: what still needs a decision first, then
# newest. `id` breaks ties because `created_at` alone would leave two
# suggestions made in the same millisecond in whatever order the database
# felt like, and a list that reshuffles between two page loads is not a
# list.
#
# Ordered in the database, never in Python - `Case`/`When` puts the
# pending-first rule into the ORDER BY so pagination can be applied to it.
PENDING_FIRST = models.Case(
    models.When(status=AcquisitionSuggestion.STATUS_PENDING, then=0),
    default=1,
    output_field=models.IntegerField(),
)

QUEUE_ORDER = (PENDING_FIRST, "-created_at", "-id")


def all_suggestions():
    """Every suggestion, in the order the list shows them.

    `select_related` on both people, because every row renders who
    suggested it and - once decided - who decided. Without it a page of
    twenty-five costs fifty extra queries, which is the whole of the N+1
    this list could have had.
    """

    return AcquisitionSuggestion.objects.select_related(
        "suggested_by", "reviewed_by"
    ).order_by(*QUEUE_ORDER)


def filtered(queryset, status="", search=""):
    """Narrow a list by state and by title, in the database.

    Both are optional and both are applied as SQL. `icontains` on the title
    is the same shape the rest of this application searches with, and it is
    the only field searched: the author and publisher on a suggestion are
    free text that nobody has checked, so offering to search them would be
    offering a search whose misses mean nothing.
    """

    if status in dict(AcquisitionSuggestion.STATUS_CHOICES):
        queryset = queryset.filter(status=status)

    if search:
        queryset = queryset.filter(title__icontains=search)

    return queryset


def create(*, user, title, author_name="", publisher_name="", isbn="",
           notes=""):
    """Write one suggestion, attributed to the signed-in user.

    `user` is `request.user` and nothing else. There is no parameter here
    for a suggester id, so a posted one cannot be honoured even by mistake
    - the caller has nowhere to put it.

    Everything but the title is optional and stored as it was typed. None
    of it is resolved against the catalogue: see the module docstring.
    """

    now = timezone.now()

    return AcquisitionSuggestion.objects.create(
        title=title,
        author_name=author_name,
        publisher_name=publisher_name,
        isbn=isbn,
        notes=notes,
        status=AcquisitionSuggestion.STATUS_PENDING,
        suggested_by=user,
        created_at=now,
        updated_at=now,
    )


def advance(suggestion_id, to_status, *, user):
    """Move one suggestion to `to_status`, once. Returns whether it moved.

    The state it has to be coming *from* is worked out from `TRANSITIONS` -
    the same table the detail page's buttons are drawn from - and then put
    in the WHERE clause, so the database is what refuses an illegal move
    rather than a check above it. One statement, no lock, and nothing
    that two requests could both pass:

      * an arbitrary jump (Pending straight to Acquired) matches no row;
      * reopening a Rejected or Acquired suggestion matches no row, because
        neither has a legal move out of it and the filter can never be
        satisfied;
      * the second of two identical POSTs matches no row, because the first
        one already left that state.

    In each case this returns False and nothing was written - including the
    review timestamp, which is what would otherwise creep on every refresh.
    """

    sources = [
        state
        for state, allowed in AcquisitionSuggestion.TRANSITIONS.items()
        if to_status in allowed
    ]

    if not sources:
        return False

    now = timezone.now()

    return AcquisitionSuggestion.objects.filter(
        id=suggestion_id,
        status__in=sources,
    ).update(
        status=to_status,
        reviewed_by=user,
        reviewed_at=now,
        updated_at=now,
    ) > 0


def mark_acquired(suggestion_id, *, user):
    """Record that an approved suggestion has been added to the catalogue.

    Called from `book_add`, and only when a book was actually created on a
    request that came through this suggestion's own shortcut. It is the
    same conditional UPDATE as any other move, so an approved suggestion
    becomes Acquired exactly once however many books are added afterwards,
    and a Pending or Rejected one never does - `Approved` is the only state
    with a legal move to `Acquired`, and it is in the WHERE clause.

    Adding a book the ordinary way marks nothing: nothing identifies a
    suggestion on that request, so this is never reached.
    """

    return advance(
        suggestion_id, AcquisitionSuggestion.STATUS_ACQUIRED, user=user
    )


def open_for_catalogue(suggestion_id):
    """The approved suggestion behind a catalogue shortcut, or None.

    The single gate for both halves of the integration - the prefill on the
    way in and the Acquired mark on the way out - so the two cannot
    disagree about which suggestions may use it. Approved and nothing else:
    a Pending one has not been decided, and a Rejected or Acquired one is
    finished.
    """

    if not suggestion_id:
        return None

    return AcquisitionSuggestion.objects.filter(
        id=suggestion_id,
        status=AcquisitionSuggestion.STATUS_APPROVED,
    ).first()
