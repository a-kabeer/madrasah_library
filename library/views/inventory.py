"""Stock checks: counting the shelves against the record.

The session is the job of counting, the scans are what was actually picked
up, and neither of them writes to a copy. What the count found is a report,
not a correction - deciding what to do about a missing book stays with a
person.
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext
from django.db import models, transaction

from ..models import (
    InventoryScan,
    InventorySession,
    Location,
    Shelf,
)

from .. import inventory
from .. import notifications

from ..permissions import feature_required

from .common import (
    PAGE_SIZE,
    copy_for_code,
    create_activity_log,
    query_with,
)


# ==========================================================================
# STOCK CHECK
#
# Counting the shelves against the record. Everything here reports; nothing
# here changes a copy. See library/inventory.py for why.
# ==========================================================================


@feature_required("inventory.sessions", "Admin", "Librarian")
def inventory_session_list(request):
    """Every stock check, open ones first.

    The counts on each row are annotated, not looped over: a page listing
    twenty sessions makes one query for them, not twenty.
    """

    sessions = InventorySession.objects.select_related(
        "started_by", "location", "shelf__location"
    ).annotate(
        found_count=models.Count(
            "scans",
            filter=models.Q(scans__outcome=InventoryScan.OUTCOME_FOUND),
            distinct=True,
        ),
    ).order_by("status", "-started_at", "-id")

    paginator = Paginator(sessions, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/inventory_session_list.html",
        {
            "sessions": page,
            "paginator": paginator,
            "pagination_query": query_with(request, page=None),
            "open_count": InventorySession.objects.filter(
                status=InventorySession.STATUS_IN_PROGRESS
            ).count(),
        },
    )


@feature_required("inventory.sessions", "Admin", "Librarian")
def inventory_session_start(request):
    """Begin a stock check over a chosen scope."""

    error = None

    form = {
        "name": "",
        "scope": InventorySession.SCOPE_LIBRARY,
        "location": "",
        "shelf": "",
    }

    if request.method == "POST":

        form["name"] = request.POST.get("name", "").strip()
        form["scope"] = request.POST.get("scope", "").strip()
        form["location"] = request.POST.get("location", "").strip()
        form["shelf"] = request.POST.get("shelf", "").strip()

        location = None
        shelf = None

        if not form["name"]:
            error = "Give the stock check a name, so it can be told apart."

        elif form["scope"] not in dict(InventorySession.SCOPE_CHOICES):
            error = "Choose what this stock check covers."

        elif form["scope"] == InventorySession.SCOPE_LOCATION:

            location = Location.objects.filter(
                id=form["location"]
            ).first() if form["location"].isdigit() else None

            if location is None:
                error = "Choose the location to check."

        elif form["scope"] == InventorySession.SCOPE_SHELF:

            shelf = Shelf.objects.select_related("location").filter(
                id=form["shelf"]
            ).first() if form["shelf"].isdigit() else None

            if shelf is None:
                error = "Choose the shelf to check."

        if error is None:

            session = InventorySession.objects.create(
                name=form["name"],
                scope=form["scope"],
                location=location,
                shelf=shelf,
                status=InventorySession.STATUS_IN_PROGRESS,
                started_by=request.user,
                started_at=timezone.now(),
            )

            create_activity_log(
                user=request.user,
                action="CREATE",
                entity_type="InventorySession",
                entity_id=session.id,
                description=(
                    "Stock check started: %s (%s)"
                    % (session.name, session.scope_label)
                ),
            )

            return redirect("inventory_session_detail", session_id=session.id)

    return render(
        request,
        "library/inventory_session_start.html",
        {
            "form": form,
            "error": error,
            "scopes": InventorySession.SCOPE_CHOICES,
            "locations": Location.objects.order_by("name"),
            "shelves": Shelf.objects.select_related(
                "location"
            ).order_by("location__name", "shelf_code"),
        },
    )


@feature_required("inventory.sessions", "Admin", "Librarian")
def inventory_session_detail(request, session_id):
    """An open session's counting screen, or a finished one's report.

    One template for both, because they describe the same thing at two
    points in its life and splitting them would mean keeping two accounts
    of what a session is in step.
    """

    session = get_object_or_404(
        InventorySession.objects.select_related(
            "started_by", "location", "shelf__location"
        ),
        id=session_id,
    )

    counts = inventory.session_counts(session)

    # The lists are worth the queries only once there is something to say,
    # which for a session still being counted is not yet.
    missing = []
    outside = []
    duplicates = []

    if not session.is_open:
        missing = inventory.missing_copies(session)
        outside = inventory.session_scans(
            session, InventoryScan.OUTCOME_OUTSIDE
        )
        duplicates = inventory.session_scans(
            session, InventoryScan.OUTCOME_DUPLICATE
        )

    return render(
        request,
        "library/inventory_session_detail.html",
        {
            "session": session,
            "counts": counts,
            "missing": missing,
            "outside": outside,
            "duplicates": duplicates,
            "recent": inventory.session_scans(
                session, InventoryScan.OUTCOME_FOUND
            )[:10] if session.is_open else [],
        },
    )


@feature_required("inventory.sessions", "Admin", "Librarian")
def inventory_session_scan(request, session_id):
    """Record one code read during a stock check.

    A POST and a redirect, so a reload does not re-scan the last book and
    the field comes back empty for the next one. The outcome is reported as
    a message, which is what the librarian reads between books.
    """

    session = get_object_or_404(InventorySession, id=session_id)

    landing = redirect("inventory_session_detail", session_id=session.id)

    if request.method != "POST":
        return landing

    # A finished session is finished. Checked here as well as hidden in the
    # template, because a kept tab and a hand-made POST both arrive here.
    if not session.is_open:
        messages.warning(
            request,
            gettext(
                "This stock check is complete. Start a new one to carry on "
                "counting."
            ),
        )
        return landing

    code = request.POST.get("copy_code", "").strip()

    if not code:
        return landing

    # The same targeted lookup the issue and return workflows use. There is
    # one scanner in this application, not three.
    copy = copy_for_code(code)

    outcome, scan = inventory.record_scan(session, copy, code, request.user)

    if outcome == InventoryScan.OUTCOME_FOUND:
        messages.success(
            request, gettext("%s found. Scan the next one.") % copy.copy_code
        )

    elif outcome == InventoryScan.OUTCOME_DUPLICATE:
        messages.info(
            request,
            gettext("%s has already been counted in this check.") % copy.copy_code,
        )

    elif outcome == InventoryScan.OUTCOME_OUTSIDE:
        messages.warning(
            request,
            gettext(
                "%s is not part of this check - the record puts it %s. "
                "Nothing has been changed; move it or edit the copy if it "
                "belongs here."
            )
            % (
                copy.copy_code,
                (
                    "on %s, %s"
                    % (copy.shelf.shelf_code, copy.shelf.location.name)
                )
                if copy.shelf_id
                else "on no shelf",
            ),
        )

    else:
        messages.error(
            request,
            gettext("No copy carries the code \u201c%s\u201d. Check the label.") % code,
        )

    return landing


@feature_required("inventory.sessions", "Admin", "Librarian")
def inventory_session_complete(request, session_id):
    """Declare a stock check finished.

    Locked and re-read first, so completing twice is safe: the second
    request finds it already Completed and changes nothing rather than
    moving the completion time.
    """

    session = get_object_or_404(InventorySession, id=session_id)

    if request.method != "POST":
        return redirect("inventory_session_detail", session_id=session.id)

    with transaction.atomic():

        locked = InventorySession.objects.select_for_update().get(
            id=session.id
        )

        if locked.is_open:

            locked.status = InventorySession.STATUS_COMPLETED
            locked.completed_at = timezone.now()
            locked.save(update_fields=["status", "completed_at"])

            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="InventorySession",
                entity_id=locked.id,
                description="Stock check completed: %s" % locked.name,
            )

            # Only when something is actually unaccounted for, and only
            # once - completing is already guarded by the lock above, and
            # the session's own id keys the notification. Counted in the
            # database, from the same expected-minus-found set the report
            # below is drawn from.
            notifications.announce_stock_check(
                locked,
                inventory.expected_copies(locked).exclude(
                    id__in=inventory.found_copy_ids(locked)
                ).count(),
            )

            messages.success(
                request,
                gettext(
                    "Stock check complete. What was not found is listed "
                    "below - nothing has been marked Missing."
                ),
            )

        else:
            messages.info(request, gettext("This stock check was already complete."))

    return redirect("inventory_session_detail", session_id=session.id)
