"""The queue of borrowers waiting for a book.

The views only. The queue's rules - who is next, who may be issued a copy -
are in library/reservations.py, and the enforcement is in the issue view,
not here.
"""

from django.contrib import messages
from django.db import models
from django.utils.translation import gettext
from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from ..permissions import feature_required

from .. import queries

from ..models import (
    Book,
    Borrower,
    Reservation,
)

from .. import notifications
from .. import reservations

from .common import (
    PAGE_SIZE,
    create_activity_log,
    is_form_modal_request,
    query_with,
    safe_redirect_target,
)


# ==========================================================================
# RESERVATIONS
#
# A queue of borrowers waiting for a book. Nothing here holds a copy back,
# and nothing happens automatically when one is returned - see
# library/reservations.py.
# ==========================================================================


def describe_availability(page, status):
    """Attach "how many are in" and "can this one be handed over" to a page.

    One query for the whole page rather than one per row, and the count is
    `annotate_copy_counts`' own - the figure the book list and the
    catalogue already show - so "2 on the shelf" here means what it means
    there. A copy that is out, or withdrawn, or shelved nowhere is not on
    the shelf.

    `ready` is the pair of conditions `announce_ready` uses to decide the
    same thing for the notification: somebody is at the front *and* there
    is a copy to give them. Neither alone is news.

    Nothing is attached to a closed reservation. What was on the shelf on
    the day somebody cancelled is not a question the list is asked.
    """

    if status != Reservation.STATUS_ACTIVE:
        return

    book_ids = {reservation.book_id for reservation in page}

    on_shelf = {
        book.id: book.available_copies
        for book in queries.annotate_copy_counts(
            Book.objects.filter(id__in=book_ids)
        )
    } if book_ids else {}

    for reservation in page:

        reservation.available = on_shelf.get(reservation.book_id, 0)

        reservation.ready = (
            getattr(reservation, "position", None) == 1
            and reservation.available > 0
        )


@feature_required("reservations")
def reservation_list(request):
    """Everyone currently waiting, and what for.

    Grouped by nothing: it is one list, oldest first, because the question
    a librarian brings to it is "who has been waiting longest".

    Three things are worked out for the active list, all of them readings
    of what is already there rather than anything new the queue obeys:
    each row's place in its own book's queue, whether a copy of that book
    is on the shelf, and - the pair of those - whether the person at the
    front can be handed one right now.
    """

    status = (request.GET.get("status") or "").strip()

    if status not in dict(Reservation.STATUS_CHOICES):
        status = Reservation.STATUS_ACTIVE

    # Who is waiting and what for, which is what the desk is asked at the
    # counter: a name, a number half-remembered from a phone, or a title.
    search = (request.GET.get("search") or "").strip()

    waiting = Reservation.objects.filter(status=status)

    if search:
        waiting = waiting.filter(
            models.Q(borrower__name__icontains=search)
            | models.Q(borrower__phone__icontains=search)
            | models.Q(book__title__icontains=search)
        )

    waiting = waiting.select_related(
        "borrower", "book__author"
    ).order_by("created_at", "id")

    # Counted over every active reservation, not over the ones the search
    # left behind, so #1 means the front of the queue rather than the
    # first row on screen. Only for the active list: a cancelled
    # reservation has no place in a queue it is no longer in.
    if status == Reservation.STATUS_ACTIVE:
        waiting = reservations.with_position(waiting)

    paginator = Paginator(waiting, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    describe_availability(page, status)

    return render(
        request,
        "library/reservation_list.html",
        {
            "reservations": page,
            "paginator": paginator,
            "pagination_query": query_with(request, page=None),
            "status": status,
            "search": search,
            # The active list is a queue and shows one; the other two are
            # records of queues that have ended and show the other.
            "is_active_list": status == Reservation.STATUS_ACTIVE,
            "statuses": Reservation.STATUS_CHOICES,
            # Shaped exactly like the loan list's `status_options`, so this
            # page can use that page's pill markup unchanged rather than a
            # second filter control that looks like nothing else. Same keys,
            # same `query_with` so a status keeps the rest of the query and
            # returns to page 1; the labels are still the model's own.
            "status_options": [
                {
                    "value": value,
                    "label": label,
                    "icon": icon,
                    "active": status == value,
                    "url": "?" + query_with(
                        request, status=value, page=None
                    ),
                }
                for (value, label), icon in zip(
                    Reservation.STATUS_CHOICES,
                    (
                        "bi-hourglass-split",
                        "bi-check2-circle",
                        "bi-x-circle",
                    ),
                )
            ],
            "active_total": Reservation.objects.filter(
                status=Reservation.STATUS_ACTIVE
            ).count(),
        },
    )


@feature_required("reservations")
def reservation_add(request):
    """Put a borrower in a book's queue.

    A POST from the book's own page, which is where the queue is on
    screen. The borrower comes from the same searchable control the issue
    form uses, so there is one way of naming a borrower in this
    application.
    """

    book_id = request.POST.get("book", "") if request.method == "POST" else ""

    book = get_object_or_404(Book, id=book_id) if book_id.isdigit() else None

    if request.method != "POST" or book is None:
        return redirect("book_list")

    landing = redirect(
        "%s?tab=reservations" % reverse("book_detail", args=[book.id])
    )

    borrower_id = request.POST.get("borrower", "").strip()

    borrower = Borrower.objects.filter(
        id=borrower_id
    ).first() if borrower_id.isdigit() else None

    if borrower is None:
        messages.error(request, gettext("Choose who is waiting for this book."))
        return landing

    reservation, error = reservations.reserve(book, borrower)

    if error:
        messages.warning(request, error)
        return landing

    create_activity_log(
        user=request.user,
        action="RESERVE",
        entity_type="Reservation",
        entity_id=book.id,
        description=(
            "%s reserved %s" % (borrower.name, book.title)
        ),
    )

    messages.success(
        request,
        gettext("%s is in the queue for %s.") % (borrower.name, book.title),
    )

    return landing


@feature_required("reservations")
def reservation_cancel(request, reservation_id):
    """Take a borrower out of a queue.

    Cancelling closes that reservation and nothing else: the people behind
    move up because they were always behind, not because anything was
    renumbered.
    """

    reservation = get_object_or_404(
        Reservation.objects.select_related("borrower", "book"),
        id=reservation_id,
    )

    landing = redirect(
        safe_redirect_target(
            request, reverse("borrower_detail", args=[reservation.borrower_id])
        )
    )

    if request.method != "POST":

        # Ask first. Cancelling is one click in a table of near-identical
        # rows and it cannot be undone - the queue has no "put them back",
        # because rejoining it would put them at the end. So the question
        # goes into #formModal, where Delete and Withdraw already ask
        # theirs, and it names the borrower, the book and their place in
        # the queue.
        #
        # A plain GET is unchanged: without the dialog there is nothing to
        # confirm with, and this URL has always answered one by sending
        # the reader back where they came from.
        if is_form_modal_request(request):
            return render(
                request,
                "library/partials/reservation_cancel_modal.html",
                {
                    "reservation": reservation,
                    "action": reverse(
                        "reservation_cancel", args=[reservation.id]
                    ),
                    "next": safe_redirect_target(
                        request, reverse("reservation_list")
                    ),
                    "position": reservations.position_of(reservation),
                },
            )

        return landing

    if reservations.close(
        reservation, Reservation.STATUS_CANCELLED, request.user
    ):
        create_activity_log(
            user=request.user,
            action="CANCEL",
            entity_type="Reservation",
            entity_id=reservation.book_id,
            description=(
                "%s cancelled their reservation for %s"
                % (reservation.borrower.name, reservation.book.title)
            ),
        )

        # Cancelling advances the queue, so the person behind may now be
        # at the front with a copy on the shelf. Nothing is sent about the
        # cancellation itself: the only person it is news to is the
        # borrower, who has no account here, and it is already on the
        # reservation list and in the activity log for the desk.
        notifications.announce_ready(reservation.book)

        messages.success(
            request,
            gettext("%s is no longer waiting for %s.")
            % (reservation.borrower.name, reservation.book.title),
        )

    else:
        messages.info(
            request, gettext("That reservation had already been closed.")
        )

    return landing
