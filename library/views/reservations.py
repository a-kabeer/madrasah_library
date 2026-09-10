"""The queue of borrowers waiting for a book.

The views only. The queue's rules - who is next, who may be issued a copy -
are in library/reservations.py, and the enforcement is in the issue view,
not here.
"""

from django.contrib import messages
from django.utils.translation import gettext
from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from ..permissions import feature_required

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


@feature_required("reservations")
def reservation_list(request):
    """Everyone currently waiting, and what for.

    Grouped by nothing: it is one list, oldest first, because the question
    a librarian brings to it is "who has been waiting longest".
    """

    status = (request.GET.get("status") or "").strip()

    if status not in dict(Reservation.STATUS_CHOICES):
        status = Reservation.STATUS_ACTIVE

    waiting = Reservation.objects.filter(
        status=status
    ).select_related(
        "borrower", "book__author"
    ).order_by("created_at", "id")

    paginator = Paginator(waiting, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/reservation_list.html",
        {
            "reservations": page,
            "paginator": paginator,
            "pagination_query": query_with(request, page=None),
            "status": status,
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
