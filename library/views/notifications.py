"""The bell, its panel, and the page behind it.

Everything here is scoped to the signed-in person by the views themselves:
no recipient appears in any of these URLs.
"""

from django.core.paginator import Paginator
from django.shortcuts import render, redirect
from django.urls import reverse

from .. import notifications

from .common import (
    PAGE_SIZE,
    query_with,
    safe_redirect_target,
)


# ==========================================================================
# NOTIFICATIONS
#
# Everything below reads and writes exactly one person's notifications: the
# signed-in one. There is no view here that takes a recipient from the
# request, and no query that does not start from `request.user`, so a
# forged id addresses nothing rather than somebody else's row - and an id
# that belongs to another user is indistinguishable from one that does not
# exist at all, which is the point.
#
# Reading a notification is not access to what it points at. The link is an
# ordinary link, and the view behind it still applies its own
# `role_required` - so a destination that has since become inaccessible
# answers 403 exactly as it would if the link had been typed.
# ==========================================================================


def notification_panel_response(request):
    """The panel fragment, with the shell's badge refreshed alongside it.

    One template for every answer this section gives - opening the panel,
    marking one read, marking them all read - so the panel a user is
    looking at is always the panel the server just decided on, and there is
    no second rendering path to keep in step.
    """

    return render(
        request,
        "library/partials/notification_panel.html",
        {
            "notifications": notifications.recent(request.user),
            "unread_count": notifications.unread_count(request.user),
            "unread_cap": notifications.UNREAD_CAP,
        },
    )


def notification_panel(request):
    """What the bell opens: the newest few, and nothing more.

    Bounded by `PANEL_LIMIT` in the database, not sliced in Python, so a
    user with ten thousand notifications pays the same as one with three.
    Nothing is joined: a notification carries its own title, message and
    destination, so there is no linked record to fetch per row and no N+1
    to avoid.
    """

    return notification_panel_response(request)


def notification_read(request, notification_id):
    """Mark one notification read.

    POST only. A GET does nothing at all - not because marking one read is
    dangerous, but because a state change on a GET is a state change any
    prefetch, crawler or Back button can make on the user's behalf.

    Harmless to repeat: `mark_read` is a conditional UPDATE scoped to this
    recipient and to what is still unread, so the second request matches
    nothing. The answer is the same either way, so nothing about it says
    whether the id existed, was already read, or belongs to somebody else.
    """

    if request.method != "POST":
        return redirect("notification_list")

    notifications.mark_read(request.user, notification_id)

    if request.headers.get("HX-Request") == "true":
        return notification_panel_response(request)

    return redirect(safe_redirect_target(request, reverse("notification_list")))


def notification_read_all(request):
    """Mark everything this user has unread as read.

    One UPDATE over the partial index, scoped to `request.user` - it cannot
    reach another recipient's rows, and running it twice moves nothing the
    second time.
    """

    if request.method != "POST":
        return redirect("notification_list")

    notifications.mark_all_read(request.user)

    if request.headers.get("HX-Request") == "true":
        return notification_panel_response(request)

    return redirect(safe_redirect_target(request, reverse("notification_list")))


def notification_list(request):
    """The whole history for one person, a page at a time.

    Filtered, ordered and paginated in the database. `-created_at, -id`,
    the same ordering the panel uses: the timestamp alone would leave the
    several rows one fan-out writes in whatever order the database felt
    like, and a list that reshuffles between two page loads is not a list.
    """

    unread_only = request.GET.get("unread") == "1"

    rows = notifications.unread_filter(
        notifications.for_user(request.user), unread_only
    )

    paginator = Paginator(rows, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/notification_list.html",
        {
            "notifications": page,
            "paginator": paginator,
            "pagination_query": query_with(request, page=None),
            "elided_page_range": list(
                paginator.get_elided_page_range(
                    page.number,
                    on_each_side=1,
                    on_ends=1,
                )
            ),
            "page_ellipsis": Paginator.ELLIPSIS,
            "unread_only": unread_only,
            "unread_count": notifications.unread_count(request.user),
            "unread_cap": notifications.UNREAD_CAP,
        },
    )
