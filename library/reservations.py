"""Who is waiting for which book, and in what order.

Reservations are on the book, not on a copy. Nothing is set aside
physically: no particular copy is held back and no shelf is marked. What
the queue decides is *who*, not *which* - while somebody is waiting for a
book, the next copy of it goes to them, and `refuse_issue` is what makes
that true rather than merely advertised.

The rule is enforced where the loan is written, not where the form is
drawn, so a request posted straight at the view meets it too.

Everything that could be decided automatically is deliberately not. There
is no expiry, no notification, no priority and no auto-assignment when a
copy comes back - each of those is a policy a library should choose, and
none of them is needed for the queue to be useful.

Two rules are held by the database rather than by anything here, because
two simultaneous requests could pass any check written in Python:

  * one active reservation per borrower per book
    (`unique_active_reservation`);
  * a reservation is active exactly when it has no closing time
    (`check_reservation_closed`).

Ordering is `(created_at, id)` throughout. The timestamp alone would leave
two reservations made in the same millisecond in an order the database
picks freshly each time, and a queue that reorders itself between two page
loads is not a queue.
"""

from django.db import IntegrityError, models, transaction
from django.db.models import F, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from .models import Reservation


# The order a queue is read in, everywhere. Named once so no caller can
# quietly use a different one.
QUEUE_ORDER = ("created_at", "id")


def active_for_book(book):
    """The queue for one book, front first, with each position worked out.

    `position` is a window function rather than a counter in Python: the
    page needs the number beside every row, and counting the ones in front
    of each would be a query per row.

    Cancelled and fulfilled reservations are absent, so they cannot shift
    anybody's place - somebody who withdraws leaves the queue rather than
    leaving a gap in it.
    """

    return Reservation.objects.filter(
        book=book,
        status=Reservation.STATUS_ACTIVE,
    ).annotate(
        position=Window(
            expression=RowNumber(),
            order_by=[F("created_at").asc(), F("id").asc()],
        ),
    ).select_related("borrower").order_by(*QUEUE_ORDER)


def active_for_borrower(borrower):
    """What one borrower is waiting for, and where they are in each queue.

    The position has to be counted per book rather than partitioned over
    this borrower's own rows, so it is a correlated count: how many active
    reservations for the same book were made before this one. One query,
    and a borrower waits for a handful of books, not thousands.
    """

    earlier = Reservation.objects.filter(
        book_id=models.OuterRef("book_id"),
        status=Reservation.STATUS_ACTIVE,
    ).filter(
        models.Q(created_at__lt=models.OuterRef("created_at"))
        | models.Q(
            created_at=models.OuterRef("created_at"),
            id__lt=models.OuterRef("id"),
        )
    ).values("book_id").annotate(
        total=models.Count("*")
    ).values("total")

    return Reservation.objects.filter(
        borrower=borrower,
    ).annotate(
        ahead=models.functions.Coalesce(
            models.Subquery(earlier, output_field=models.IntegerField()), 0
        ),
    ).select_related("book__author").order_by("-created_at", "-id")


def queue_front(book):
    """The reservation at the head of this book's queue, or None."""

    return Reservation.objects.filter(
        book=book,
        status=Reservation.STATUS_ACTIVE,
    ).select_related("borrower", "book").order_by(*QUEUE_ORDER).first()


def active_count(book):
    return Reservation.objects.filter(
        book=book, status=Reservation.STATUS_ACTIVE
    ).count()


def refuse_reservation(book, borrower):
    """Why this borrower cannot join this book's queue, or "".

    Two refusals, both of which say something true about the library
    rather than about the queue:

      * an archived book is out of the catalogue, so there is nothing to
        wait for;
      * an inactive borrower cannot be issued a book, so a place in the
        queue would only lead somewhere they cannot go.

    A book with copies on the shelf is *not* refused: someone may
    reasonably want to be next in line for a book that is in today, and
    blocking that would mean the queue only worked once it was too late to
    join it. The book page says how many are available instead.
    """

    if book.archived_at is not None:
        return (
            "%s has been archived, so it cannot be reserved. Restore it "
            "first if it is back in the catalogue." % book.title
        )

    if not borrower.is_active:
        return (
            "%s is inactive and cannot be issued books, so there is "
            "nothing to wait for. Reactivate them first." % borrower.name
        )

    return ""


def refuse_issue(front, borrower_id):
    """Why a copy cannot go to this borrower, given the queue front, or "".

    A queue that anyone could be served past is not a queue - it is a note
    about who once asked. So while somebody is waiting, only they may be
    handed a copy, and this is checked on the server: hiding the button
    would leave the rule to the browser, and a form posted straight at the
    view would walk round it.

    The cancelled and the fulfilled are already absent from the queue, so
    they cannot block anybody: whoever is front is somebody still waiting.

    An empty queue refuses nothing. Reservations only constrain a book that
    somebody is actually waiting for.
    """

    if front is None:
        return ""

    if str(front.borrower_id) == str(borrower_id):
        return ""

    return (
        "%s is first in the queue for %s, waiting since %s. That copy has "
        "to go to them - cancel their reservation first if it should not."
        % (
            front.borrower.name,
            front.book.title,
            front.created_at.date().isoformat(),
        )
    )


def refuse_issue_for_copies(copies, borrower_id):
    """The first reservation refusal among `copies`, or "".

    One query for the whole basket, through `queues_for_copies`, rather
    than one per copy - a librarian issuing eight books should not pay
    eight round trips to find out that nobody is waiting for any of them.
    """

    queues = queues_for_copies(copies)

    for copy in copies:

        front, _count = queues.get(copy.volume.book_id, (None, 0))

        refusal = refuse_issue(front, borrower_id)

        if refusal:
            return refusal

    return ""


def reserve(book, borrower):
    """Put `borrower` in `book`'s queue.

    Returns `(reservation, error)`. The duplicate is caught from the
    database rather than checked beforehand: a check would be a read
    followed by a write, and two requests could both read "not queued" and
    both write.
    """

    refusal = refuse_reservation(book, borrower)

    if refusal:
        return None, refusal

    try:
        with transaction.atomic():
            reservation = Reservation.objects.create(
                book=book,
                borrower=borrower,
                status=Reservation.STATUS_ACTIVE,
                created_at=timezone.now(),
            )

    except IntegrityError:
        return None, (
            "%s is already waiting for %s." % (borrower.name, book.title)
        )

    return reservation, ""


def close(reservation, status, user=None):
    """End a reservation, once.

    Re-read under a lock and re-checked, so the second of two simultaneous
    cancellations - or a cancel racing a fulfilment - finds it already
    closed and changes nothing rather than moving the closing time or
    overwriting how it ended.

    Returns True when this call is the one that closed it.
    """

    with transaction.atomic():

        locked = Reservation.objects.select_for_update().filter(
            id=reservation.id,
            status=Reservation.STATUS_ACTIVE,
        ).first()

        if locked is None:
            return False

        locked.status = status
        locked.closed_at = timezone.now()
        locked.save(update_fields=["status", "closed_at"])

    return True


def fulfil_for(book_id, borrower_id, user=None):
    """Close this borrower's reservation for this book, if they have one.

    Called from inside the transaction that writes a loan, with the
    borrower already locked by the issue view - so this adds no new lock
    order and cannot deadlock against it.

    Only their own, which under `refuse_issue` means only the front's -
    nobody behind them can be issued this book while they are waiting. The
    narrower rule is kept anyway: it is the true one, it does not depend on
    the caller having checked the queue first, and a reservation that was
    cancelled a moment ago must not be closed as fulfilled by a loan that
    had nothing to do with it.

    Returns the reservation it closed, or None.
    """

    reservation = Reservation.objects.select_for_update().filter(
        book_id=book_id,
        borrower_id=borrower_id,
        status=Reservation.STATUS_ACTIVE,
    ).order_by(*QUEUE_ORDER).first()

    if reservation is None:
        return None

    reservation.status = Reservation.STATUS_FULFILLED
    reservation.closed_at = timezone.now()
    reservation.save(update_fields=["status", "closed_at"])

    return reservation


def queues_for_copies(copies):
    """The reservation position for each book among `copies`.

    For the issue form: the librarian is about to hand over these copies
    and should be told, before they do, that somebody is waiting for one
    of them. One query for the whole basket rather than one per copy.

    Returns `{book_id: (front_reservation, active_count)}`.
    """

    book_ids = {copy.volume.book_id for copy in copies}

    if not book_ids:
        return {}

    waiting = Reservation.objects.filter(
        book_id__in=book_ids,
        status=Reservation.STATUS_ACTIVE,
    ).select_related("borrower", "book").order_by(*QUEUE_ORDER)

    queues = {}

    for reservation in waiting:

        front, count = queues.get(reservation.book_id, (None, 0))

        queues[reservation.book_id] = (
            front or reservation,      # ordered, so the first seen is the front
            count + 1,
        )

    return queues
