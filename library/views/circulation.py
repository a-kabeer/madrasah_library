"""Issuing, returning and renewing - the desk's daily work.

Also the scanning that goes with it: a copy code typed or scanned at the
issue form, and the one search box on the return page.
"""

from collections import namedtuple
from datetime import date, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404
from django.core.cache import cache
from django.utils import timezone
from django.db import IntegrityError, models, transaction
from django.db.models import Q

from ..models import (
    BookCopy,
    Borrower,
    Loan,
)

from .. import notifications
from .. import policy
from .. import reservations

from ..permissions import (
    feature_required,
    passes_ceiling,
    role_required,
)

from django.utils.translation import gettext

from .common import (
    BOOK_COPY_CACHE_KEY,
    DASHBOARD_CACHE_KEY,
    INTERNAL_PARAMS,
    LOAN_CACHE_KEY,
    LOAN_SORT_DEFAULT,
    LOAN_SORT_DEFAULT_DIRECTION,
    LOAN_SORT_FIELDS,
    PAGE_SIZE,
    PolicyRefused,
    copy_for_code,
    create_activity_log,
    describe_loans,
    is_form_modal_request,
    lookup_saved_response,
    page_size_options,
    resolve_page_size,
    query_with,
    resolve_sort,
    safe_redirect_target,
    selected_name,
    sort_ordering,
    sortable_columns,
)
from .copies import COPY_LOOKUP_LIMIT


def issuable_copies():
    """The copies that may be issued, decided in one place.

    Marked Available - which the return workflow is what clears, and which
    `unique_active_loan_per_copy` backs - and belonging to a book that is
    still in the catalogue. Withdrawn copies fail the first test and
    archived books the second, so the form and the POST that follows it
    cannot disagree about what is lendable.
    """

    return BookCopy.objects.filter(
        status=BookCopy.STATUS_AVAILABLE,
        volume__book__archived_at__isnull=True,
    )


def selection_url(request, param, chosen_ids, clear_search=False):
    """This page with `param` set to `chosen_ids`, keeping the rest.

    What has been picked so far lives in the query string rather than in
    checkboxes, so searching again for the next one cannot lose the ones
    already picked - and the whole half-built form is a URL, which survives
    a reload and can be handed to a colleague.

    Both baskets work this way and this is the whole of it; only the
    parameter differs - `copies` for the copies being issued, `loans` for
    the loans being returned.

    `clear_search` drops the search term as well, which is what a scan
    wants: the code has been dealt with, and leaving it in the box would
    mean the next scan appends to it.
    """

    params = request.GET.copy()

    for key in INTERNAL_PARAMS:
        params.pop(key, None)

    if clear_search:
        for key in ("q", "copy_code"):
            params.pop(key, None)

    params.setlist(param, [str(value) for value in chosen_ids])

    return "?" + params.urlencode()


# What a whole copy code typed into the issue form turned out to mean.
# `copy` is None when the text was not a copy code at all, which is what
# lets the same field still search for a book title.
ScanOutcome = namedtuple("ScanOutcome", "copy message level add")




def scan_issue_outcome(query, chosen_ids):
    """Resolve a whole copy code typed or scanned into the issue form.

    A barcode or QR scanner acting as a keyboard types a complete code and
    presses Enter. What should follow is the copy in the basket - not a
    table with one row in it and another click to make. So a code that
    names a copy is answered here, and anything else falls through to the
    ordinary search below.

    Every refusal is `issuable_copies()`, the one rule the form and the
    POST already share, so scanning cannot add a copy that picking it from
    the results could not: a copy already out, one withdrawn from
    circulation, or one whose book has been archived is refused with the
    reason rather than silently listed as nothing.

    Nothing is written here and nothing is trusted afterwards - the POST
    re-checks every copy under a lock, which is what makes this a
    convenience rather than a way in.
    """

    copy = copy_for_code(query)

    if copy is None:
        # Not a code. Let the search have it.
        return ScanOutcome(None, "", messages.INFO, False)

    if copy.id in chosen_ids:
        return ScanOutcome(
            copy,
            "%s is already in the list." % copy.copy_code,
            messages.INFO,
            False,
        )

    if not issuable_copies().filter(pk=copy.pk).exists():

        if copy.status == BookCopy.STATUS_ISSUED:
            reason = "is already out on loan"

        elif copy.status != BookCopy.STATUS_AVAILABLE:
            reason = (
                "is marked %s and is out of circulation" % copy.status.lower()
            )

        else:
            # Available, so what fails the rule is the book above it.
            reason = "belongs to a book that has been archived"

        return ScanOutcome(
            copy,
            "%s %s, so it cannot be issued." % (copy.copy_code, reason),
            messages.WARNING,
            False,
        )

    return ScanOutcome(
        copy,
        "%s added. Scan the next one." % copy.copy_code,
        messages.SUCCESS,
        True,
    )


@feature_required("loans.active")
def loan_list(request):

    search = request.GET.get("search", "").strip()
    status = request.GET.get("status", "").strip()
    borrower_id = request.GET.get("borrower", "").strip()
    issue_date = request.GET.get("issue_date", "").strip()
    due_date = request.GET.get("due_date", "").strip()

    loans_query = Loan.objects.select_related(
        "copy__volume__book",
        "borrower",
        "issued_by",
        "returned_to",
    )

    # Search
    if search:

        query = (
            models.Q(borrower__name__icontains=search)
            | models.Q(borrower__phone__icontains=search)
            | models.Q(copy__copy_code__icontains=search)
            | models.Q(copy__volume__book__title__icontains=search)
            | models.Q(issued_by__username__icontains=search)
            | models.Q(issued_by__full_name__icontains=search)
            | models.Q(returned_to__username__icontains=search)
            | models.Q(returned_to__full_name__icontains=search)
        )

        loans_query = loans_query.filter(query)

    # Status filter
    today = timezone.now().date()

    if status == "active":

        loans_query = loans_query.filter(
            return_date__isnull=True
        )

    elif status == "returned":

        loans_query = loans_query.filter(
            return_date__isnull=False
        )

    elif status == "overdue":

        loans_query = loans_query.filter(
            return_date__isnull=True,
            due_date__lt=today
        )

    elif status == "due_today":

        loans_query = loans_query.filter(
            return_date__isnull=True,
            due_date=today
        )

    # Borrower filter
    if borrower_id:

        loans_query = loans_query.filter(
            borrower_id=borrower_id
        )

    # Issue date filter
    if issue_date:

        loans_query = loans_query.filter(
            issue_date=issue_date
        )

    # Due date filter
    if due_date:

        loans_query = loans_query.filter(
            due_date=due_date
        )

    # Ordering, from the whitelist. `-id` stays as the tie-break: two
    # loans issued on the same day would otherwise come back in whatever
    # order the database felt like, which makes a paginated table drop and
    # repeat rows between pages.
    sort, direction = resolve_sort(
        request,
        LOAN_SORT_FIELDS,
        LOAN_SORT_DEFAULT,
        default_direction=LOAN_SORT_DEFAULT_DIRECTION,
    )

    loans = loans_query.order_by(
        *sort_ordering(LOAN_SORT_FIELDS, sort, direction)
    )

    page_size = resolve_page_size(request)

    paginator = Paginator(loans, page_size)
    loans = paginator.get_page(request.GET.get("page"))

    # Calculate overdue days
    today = timezone.now().date()

    for loan in loans:

        loan.days_overdue = 0

        if (
            loan.return_date is None
            and loan.due_date < today
        ):

            loan.days_overdue = (
                today - loan.due_date
            ).days

    return render(
        request,
        "library/loan_list.html",
        {
            "loans": loans,
            "search": search,
            "status": status,
            "borrower_id": borrower_id,
            "issue_date": issue_date,
            "due_date": due_date,

            "columns": sortable_columns(
                request,
                [
                    ("copy", gettext("Copy")),
                    ("book", gettext("Book")),
                    ("borrower", gettext("Borrower")),
                    ("issue_date", gettext("Issued")),
                    ("due_date", gettext("Due")),
                    (None, gettext("Status")),
                ],
                LOAN_SORT_FIELDS,
                sort,
                direction,
            ),

            "sort": sort,
            "direction": direction,

            "paginator": paginator,
            "page_size": page_size,
            "page_size_options": page_size_options(page_size),
            # Everything except `page`, so the search, the status and both
            # date filters survive being paged through or re-sorted.
            "pagination_query": query_with(request, page=None),
            # For the rows-per-page control, which sets its own value.
            "page_size_url": "?" + query_with(
                request, page=None, page_size=None
            ),
            "elided_page_range": list(
                paginator.get_elided_page_range(
                    loans.number,
                    on_each_side=1,
                    on_ends=1,
                )
            ),
            "page_ellipsis": Paginator.ELLIPSIS,

            # The status filter's own links, each carrying the rest of the
            # table's state. Built here rather than in the template because
            # `query_with` needs the request.
            "status_options": [
                {
                    "value": value,
                    "label": label,
                    "icon": icon,
                    "active": status == value,
                    "url": "?" + query_with(
                        request, status=value or None, page=None
                    ),
                }
                for value, label, icon in (
                    ("", gettext("All"), "bi-list-ul"),
                    ("active", gettext("Active"), "bi-arrow-up-right-circle"),
                    (
                        "overdue",
                        gettext("Overdue"),
                        "bi-exclamation-triangle",
                    ),
                    (
                        "due_today",
                        gettext("Due today"),
                        "bi-calendar-event",
                    ),
                    ("returned", gettext("Returned"), "bi-check2-circle"),
                )
            ],

            # Whether to offer Renew at all. `loan_renew` admits Admin
            # and Librarian - and SuperAdmin, whom `feature_required`
            # exempts - so an Assistant would otherwise be looking at the
            # one button their row has and getting a 403 from it. The same
            # ceiling, asked of the same helper, so the button and the
            # decorator cannot drift apart. The decorator is still the
            # rule; this only decides what is offered.
            "can_renew": passes_ceiling(
                request.user, "Admin", "Librarian"
            ),
        }
    )

@feature_required("loans.active")
def loan_detail(request, loan_id):

    loan = get_object_or_404(
        Loan.objects.select_related(
            "copy__volume__book",
            "borrower",
            "issued_by",
            "returned_to",
        ),
        id=loan_id
    )

    today = timezone.now().date()

    loan.days_overdue = 0

    if (
        loan.return_date is None
        and loan.due_date < today
    ):

        loan.days_overdue = (
            today - loan.due_date
        ).days

    # There is no loan page any more - the dialog is the whole of it. A
    # request without `?modal=1` still has to answer, because six other
    # pages link to this URL, so it goes to the list rather than 404ing on
    # a link somebody has bookmarked.
    if not is_form_modal_request(request):
        return redirect("loan_list")

    return render(
        request,
        "library/partials/loan_detail_modal.html",
        {
            "loan": loan,
        }
    )

@feature_required("circulation.issue")
def loan_add(request):
    """Issue one or more book copies to a borrower.

    GET: show the issue form with available copies and active borrowers.
    POST: validate borrower + copies + due_date, then issue atomically — all
    copies succeed or none are issued, enforced by transaction + select_for_update.
    """

    # Which copies the librarian has picked so far, and what they are
    # searching for now. Nothing is listed until something is asked for:
    # this page used to render every available copy in the library, which
    # was several hundred rows of table for a form whose answer is one or
    # two of them.
    query = (request.GET.get("q") or "").strip()

    chosen_ids = [
        int(value)
        for value in request.GET.getlist("copies")
        if value.isdigit()
    ]

    lendable = issuable_copies().select_related(
        # The author too: the match rows name it so the librarian can tell
        # two books with similar titles apart, and without it that was a
        # query per row.
        "volume__book__author",
        "shelf__location",
    )

    # Re-read rather than trusted: a copy chosen a minute ago may have been
    # issued to somebody else since, and it drops out here if so.
    chosen = list(
        lendable.filter(id__in=chosen_ids).order_by("copy_code")
    ) if chosen_ids else []

    chosen_ids = [copy.id for copy in chosen]

    # A whole code is resolved before anything is searched for, because
    # that is what a scanner sends. The answer comes back as a redirect
    # rather than a rendered page, which is what empties the search box and
    # leaves the cursor ready for the next scan - and keeps the borrower
    # and the basket, since both travel in the URL being redirected to.
    #
    # A message rather than a rewritten page, so a refusal is as visible as
    # a success and neither costs the librarian their basket.
    if request.method == "GET" and query:

        scan = scan_issue_outcome(query, chosen_ids)

        if scan.copy is not None:

            messages.add_message(request, scan.level, scan.message)

            # Only a copy that actually went in redirects. That is what
            # empties the box and leaves the cursor ready for the next one,
            # and the new basket has to reach the URL for a reload to keep
            # it.
            #
            # A refusal or a repeat stays on this page instead, with the
            # code still in the box: the librarian has a book in their
            # hand and needs to see which one was turned down. Nothing was
            # added, so there is no new state to put in the URL.
            if scan.add:
                return redirect(
                    request.path
                    + selection_url(
                        request,
                        "copies",
                        chosen_ids + [scan.copy.id],
                        clear_search=True,
                    )
                )

    matches = list(
        lendable.filter(
            Q(copy_code__icontains=query)
            | Q(volume__book__title__icontains=query)
            | Q(volume__book__author__name__icontains=query)
        ).exclude(
            id__in=chosen_ids
        ).order_by("copy_code")[:COPY_LOOKUP_LIMIT + 1]
    ) if query else []

    more_matches = len(matches) > COPY_LOOKUP_LIMIT
    matches = matches[:COPY_LOOKUP_LIMIT]

    for copy in matches:
        copy.add_url = selection_url(
            request, "copies", chosen_ids + [copy.id]
        )

    for copy in chosen:
        copy.remove_url = selection_url(
            request,
            "copies",
            [value for value in chosen_ids if value != copy.id],
        )

    # Who is waiting for the books in the basket, so the form can say
    # before the librarian tries. The rule itself is applied in the POST -
    # this is the same answer shown early, not the place it is decided.
    # One query for the whole basket.
    queues = reservations.queues_for_copies(chosen)

    for copy in chosen:
        copy.queue_front, copy.queue_length = queues.get(
            copy.volume.book_id, (None, 0)
        )

    # The borrower is chosen through the searchable dropdown, which asks
    # `borrower_list` for its suggestions as you type. So this page no
    # longer loads every active borrower to fill a <select> - it only needs
    # the name of the one already chosen, to show it back.
    borrower_id = (
        request.POST.get("borrower")
        or request.GET.get("borrower")
        or ""
    )

    if request.method == "GET":

        # A borrower in the query string is a link from somewhere else - the
        # borrower's own page offers one. Resolved against the same rule the
        # POST below enforces, so an inactive borrower's id arrives with
        # nothing chosen: the alternative is a form that can be filled in
        # completely and then refused at the last step. The policy itself is
        # unchanged; this only decides what a link may preselect.
        preselected = (
            Borrower.objects.filter(
                id=borrower_id, is_active=True
            ).only("id", "name").first()
            if str(borrower_id).isdigit()
            else None
        )

        borrower_id = str(preselected.id) if preselected else ""
        borrower_name = preselected.name if preselected else ""

    else:
        # A posted id keeps the plain lookup: that path reports the refusal
        # itself, and the name is wanted to show back with the message.
        borrower_name = selected_name(Borrower, borrower_id)

    error_message = ""
    today = timezone.now().date()

    # The configured loan period fills the date field in. Offered, not
    # imposed: the librarian may still type a different date, which is
    # behaviour that predates the policy and is left exactly as it was.
    active_policy = policy.load()
    default_due_date = active_policy.due_date_for(today).isoformat()

    if request.method == "POST":

        borrower_id = request.POST.get("borrower", "").strip()
        copy_ids = request.POST.getlist("copies")
        issue_date_str = request.POST.get("issue_date", "").strip()
        due_date_str = request.POST.get("due_date", "").strip()
        notes = request.POST.get("notes", "").strip()

        # --- Validate borrower ---
        borrower = None
        if not borrower_id:
            error_message = "Please select a borrower."
        else:
            try:
                borrower = Borrower.objects.get(
                    id=int(borrower_id),
                    is_active=True,
                )
            except (ValueError, Borrower.DoesNotExist):
                error_message = "Invalid or inactive borrower selected."

        # --- Validate copies ---
        selected_copies = []
        if not error_message:
            if not copy_ids:
                error_message = "Please select at least one copy to issue."
            else:
                with transaction.atomic():
                    for copy_id in copy_ids:
                        try:
                            # Locked on its own row - no join, so nothing
                            # about FOR UPDATE and outer joins arises - then
                            # checked against the one lendable rule, which
                            # is what excludes a withdrawn copy or one whose
                            # book has since been archived.
                            copy = BookCopy.objects.select_for_update().get(
                                id=int(copy_id),
                                status="Available",
                            )

                            if not issuable_copies().filter(
                                pk=copy.pk
                            ).exists():
                                raise BookCopy.DoesNotExist

                            selected_copies.append(copy)
                        except (ValueError, BookCopy.DoesNotExist):
                            error_message = (
                                "One or more selected copies are no longer "
                                "available to issue. They may have just been "
                                "issued to someone else, withdrawn, or their "
                                "book archived. Please review and try again."
                            )
                            selected_copies = []
                            break

        # --- Validate dates ---
        issue_date = None
        due_date = None
        if not error_message:
            if not issue_date_str:
                error_message = "Issue date is required."
            else:
                try:
                    issue_date = date.fromisoformat(issue_date_str)
                except ValueError:
                    error_message = "Invalid issue date format."

        if not error_message and issue_date:
            if issue_date > today:
                error_message = "Issue date cannot be in the future."

        if not error_message:
            if not due_date_str:
                error_message = "Due date is required."
            else:
                try:
                    due_date = date.fromisoformat(due_date_str)
                except ValueError:
                    error_message = "Invalid due date format."

        if not error_message and due_date and issue_date:
            if due_date < issue_date:
                error_message = "Due date cannot be earlier than issue date."

        if not error_message and due_date:
            max_due = issue_date + timedelta(days=365)
            if due_date > max_due:
                error_message = "Due date cannot be more than one year after the issue date."

        # --- Who issued it ---
        # Whoever is signed in, not whoever a dropdown named. The record of
        # whose hands the book passed through is not the borrower's to
        # choose, and it was previously possible to attribute a loan to any
        # member of staff by posting their id.
        issued_by = request.user

        # --- Create loans (atomic) ---
        if not error_message:
            try:
                with transaction.atomic():

                    # The borrower's row is taken first and held for the
                    # rest of the transaction, so the counts the policy
                    # reads cannot change underneath it. Without this,
                    # two requests for the same borrower could each see
                    # room for one more book and each issue one.
                    #
                    # Always before the copies, never after, so two
                    # baskets sharing a borrower cannot end up holding
                    # half of each other's rows.
                    borrower = Borrower.objects.select_for_update().get(
                        id=borrower.id
                    )

                    # The whole basket at once. Checked here rather than
                    # before the transaction because a check outside it is
                    # advice, not a rule: the answer can change between
                    # asking and writing, and a posted form is not
                    # obliged to have asked at all.
                    refusal = policy.refuse_issue(
                        active_policy,
                        borrower,
                        len(selected_copies),
                        today,
                    )

                    if refusal:
                        raise PolicyRefused(refusal)

                    # And whether somebody is ahead of them in a queue for
                    # any of these books. Inside the transaction with the
                    # rest, so a form posted straight at this view is held
                    # to it exactly as the page is - the button being
                    # hidden is a courtesy, not the rule.
                    #
                    # Read after the borrower is locked and before any copy
                    # is, so it adds no new lock order.
                    queued = reservations.refuse_issue_for_copies(
                        selected_copies, borrower.id
                    )

                    if queued:
                        raise PolicyRefused(queued)

                    for copy in selected_copies:
                        copy = BookCopy.objects.select_for_update().get(
                            id=copy.id
                        )
                        if copy.status != "Available" or not issuable_copies(
                        ).filter(pk=copy.pk).exists():
                            raise IntegrityError(
                                f"Copy {copy.copy_code} is no longer available."
                            )

                        loan = Loan.objects.create(
                            copy=copy,
                            borrower=borrower,
                            issue_date=issue_date,
                            due_date=due_date,
                            issued_by=issued_by,
                            notes=notes or None,
                        )

                        copy.status = "Issued"
                        copy.save(update_fields=["status"])

                        create_activity_log(
                            user=request.user,
                            action="ISSUE",
                            entity_type="Loan",
                            entity_id=loan.id,
                            description=(
                                f"{copy.copy_code} issued to "
                                f"{borrower.name}. "
                                f"Due: {due_date.isoformat()}"
                            ),
                        )

                        # If this borrower was waiting for this book, they
                        # are not any more. Only their own reservation:
                        # issuing to somebody further down the queue leaves
                        # everyone in front of them exactly where they were.
                        #
                        # Inside the same transaction as the loan, with the
                        # borrower already locked above, so it cannot half
                        # happen and adds no new lock order.
                        fulfilled = reservations.fulfil_for(
                            copy.volume.book_id, borrower.id, request.user
                        )

                        if fulfilled is not None:
                            create_activity_log(
                                user=request.user,
                                action="FULFIL",
                                entity_type="Reservation",
                                entity_id=copy.volume.book_id,
                                description=(
                                    "%s's reservation for %s fulfilled by "
                                    "%s"
                                    % (
                                        borrower.name,
                                        copy.volume.book.title,
                                        copy.copy_code,
                                    )
                                ),
                            )

                            # The queue just advanced. If another copy of
                            # the same book is still on the shelf, whoever
                            # is now at the front can be served too - and
                            # is told once, under their own reservation's
                            # key. Task 16 decides who that is; this only
                            # reads the answer.
                            notifications.announce_ready(copy.volume.book)

                cache.delete(LOAN_CACHE_KEY)
                cache.delete(BOOK_COPY_CACHE_KEY)
                cache.delete(DASHBOARD_CACHE_KEY)

                if len(selected_copies) == 1:
                    messages.success(
                        request,
                        f"Book issued successfully to {borrower.name}.",
                    )
                else:
                    messages.success(
                        request,
                        f"{len(selected_copies)} books issued successfully to {borrower.name}.",
                    )

                return redirect("loan_list")

            except PolicyRefused as refused:
                # Nothing was written: the transaction rolled back with the
                # loans, the copy statuses and the log entries in it.
                error_message = str(refused)

            except IntegrityError:
                error_message = (
                    "One or more copies became unavailable during processing. "
                    "Please review and try again."
                )

    return render(
        request,
        "library/loan_add.html",
        {
            "chosen": chosen,
            "chosen_ids": chosen_ids,
            "matches": matches,
            "more_matches": more_matches,
            "lookup_limit": COPY_LOOKUP_LIMIT,
            "query": query,
            "borrower_id": borrower_id,
            "borrower_name": borrower_name,
            "error_message": error_message,
            "default_issue_date": today.isoformat(),
            "default_due_date": default_due_date,
            # Somebody other than the chosen borrower is at the head of a
            # queue for something in the basket, so this issue will be
            # refused. Shown on the form and enforced in the POST - the
            # same rule said twice, in the place it can be read and the
            # place it cannot be avoided.
            "queue_blocks": [
                copy for copy in chosen
                if copy.queue_front
                and str(copy.queue_front.borrower_id) != str(borrower_id)
            ],
        }
    )


@feature_required("loans.active")
def loan_edit(request, loan_id):

    loan = get_object_or_404(
        Loan.objects.select_related(
            "copy__volume__book",
            "borrower",
            "issued_by",
            "returned_to",
        ),
        id=loan_id
    )

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    borrowers = Borrower.objects.filter(
        is_active=True
    ).order_by(
        "name"
    )

    if loan.return_date:

        copies = BookCopy.objects.filter(
            id=loan.copy_id
        ).select_related(
            "volume__book"
        )

    else:

        copies = BookCopy.objects.select_related(
            "volume__book"
        ).filter(
            models.Q(status="Available")
            | models.Q(id=loan.copy_id)
        ).order_by(
            "copy_code"
        )

    error_message = ""

    if request.method == "POST":

        borrower_id = request.POST.get(
            "borrower",
            ""
        ).strip()

        issue_date = request.POST.get(
            "issue_date",
            ""
        ).strip()

        due_date = request.POST.get(
            "due_date",
            ""
        ).strip()

        notes = request.POST.get(
            "notes",
            ""
        ).strip()

        if not all([
            borrower_id,
            issue_date,
            due_date,
        ]):

            error_message = (
                "Please fill in all required fields."
            )

        elif due_date < issue_date:

            error_message = (
                "Due date cannot be earlier "
                "than issue date."
            )

        elif not Borrower.objects.filter(
            id=borrower_id,
            is_active=True
        ).exists():

            error_message = (
                "Selected borrower is not available."
            )

        else:

            if loan.return_date:

                loan.borrower_id = borrower_id
                loan.issue_date = issue_date
                loan.due_date = due_date
                loan.notes = notes or None

                loan.save()

            else:

                copy_id = request.POST.get(
                    "copy",
                    ""
                ).strip()

                if not copy_id:

                    error_message = (
                        "Please select a book copy."
                    )

                else:

                    try:

                        new_copy = BookCopy.objects.get(
                            id=copy_id
                        )

                        if (
                            int(copy_id) != loan.copy_id
                            and new_copy.status != "Available"
                        ):

                            error_message = (
                                "Selected book copy is "
                                "no longer available."
                            )

                        else:

                            old_copy_id = loan.copy_id

                            loan.copy_id = copy_id
                            loan.borrower_id = borrower_id
                            loan.issue_date = issue_date
                            loan.due_date = due_date
                            loan.notes = notes or None

                            loan.save()

                            if old_copy_id != int(copy_id):

                                old_copy = BookCopy.objects.get(
                                    id=old_copy_id
                                )

                                old_copy.status = "Available"
                                old_copy.save()

                                new_copy.status = "Issued"
                                new_copy.save()

                    except (BookCopy.DoesNotExist, ValueError):

                        error_message = (
                            "Selected book copy does not exist."
                        )

            if not error_message:

                cache.delete(
                    LOAN_CACHE_KEY
                )

                cache.delete(
                    BOOK_COPY_CACHE_KEY
                )

                cache.delete(
                    DASHBOARD_CACHE_KEY
                )

                create_activity_log(
                    user=loan.issued_by,
                    action="UPDATE",
                    entity_type="Loan",
                    entity_id=loan.id,
                    description=(
                        f"Loan #{loan.id} updated"
                    ),
                )

                return redirect(
                    safe_redirect_target(request, "loan_list")
                )

    return render(
        request,
        "library/loan_edit.html",
        {
            "loan": loan,
            "borrowers": borrowers,
            "copies": copies,
            "error_message": error_message,
            "next_url": next_url,
        }
    )



@feature_required("circulation.return")
def loan_return(request, loan_id):

    loan = get_object_or_404(
        Loan.objects.select_related(
            "copy__volume__book",
            "borrower",
            "issued_by",
            "returned_to",
        ),
        id=loan_id
    )

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    error_message = ""

    if request.method == "POST" and loan.return_date is None:

        notes = request.POST.get(
            "notes",
            ""
        ).strip()

        error_message, parsed_return_date = refuse_return(
            loan, request.POST.get("return_date")
        )

        if not error_message:

            # One book back, by the one helper that does it. The batch on
            # the return page calls the same thing, so there is a single
            # place a loan is closed and a copy goes back on its shelf.
            record_return(loan, parsed_return_date, notes, request.user)

            cache.delete(
                LOAN_CACHE_KEY
            )

            cache.delete(
                BOOK_COPY_CACHE_KEY
            )

            cache.delete(
                DASHBOARD_CACHE_KEY
            )

            if is_form_modal_request(request):
                return lookup_saved_response(loan.copy.copy_code)

            return redirect(
                safe_redirect_target(request, "loan_list")
            )

    # Whether anyone is waiting for this book. Shown to whoever is taking
    # it back, and nothing more: no copy is assigned, no message is sent,
    # and the return itself is unchanged. What to do about the queue is the
    # librarian's call, which is why this is a sentence and not a workflow.
    waiting_front = reservations.queue_front(loan.copy.volume.book)
    waiting_count = (
        reservations.active_count(loan.copy.volume.book)
        if waiting_front else 0
    )

    context = {
        "loan": loan,
        "error_message": error_message,
        "next_url": next_url,
        "waiting_front": waiting_front,
        "waiting_count": waiting_count,
        # The date the desk almost always wants, and the latest one the
        # field will accept: a book cannot come back tomorrow.
        "today": timezone.now().date(),
    }

    if is_form_modal_request(request):

        return render(
            request,
            "library/partials/loan_return_modal.html",
            context,
        )

    # The standalone page is gone; without the dialog this is a redirect
    # with the reason attached.
    if error_message:
        messages.error(request, error_message)

    return redirect(safe_redirect_target(request, "loan_list"))
@feature_required("loans.active", "Admin", "Librarian")
def loan_delete(request, loan_id):

    loan = get_object_or_404(
    Loan.objects.select_related(
        "copy",
        "borrower",
    ),
    id=loan_id
)

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    if request.method == "POST":

        deleted_loan_id = loan.id
        deleted_copy_code = loan.copy.copy_code
        deleted_borrower_name = loan.borrower.name

        was_active = loan.return_date is None

        if was_active:

            loan.copy.status = "Available"
            loan.copy.save()

        loan.delete()

        cache.delete(LOAN_CACHE_KEY)
        cache.delete(BOOK_COPY_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="Loan",
            entity_id=deleted_loan_id,
            description=(
                f"{deleted_copy_code} کا loan "
                f"({deleted_borrower_name}) deleted"
            ),
        )

        return redirect(safe_redirect_target(request, "loan_list"))

    return render(
        request,
        "library/loan_delete.html",
        {
            "loan": loan,
            "next_url": next_url,
        }
    )


# Circulation dashboard: quick access point for issue, return, and active loans.
# Counts are calculated efficiently using database annotations rather than loading
# Loan objects into memory.
# Issue and return are the Assistant's daily work: `loan_add` has always
# been open to all three roles and test_permissions asserts it
# (test_assistant_can_add_loan), while only `loan_delete` is restricted.
# The circulation pages that front that workflow were stricter than the
# workflow itself, so an Assistant could return a book but not reach the
# page for finding which loan to return. They now match. Deleting a loan
# and renewing one stay Admin/Librarian - neither moves a book.
@feature_required("loans.active")
def circulation_dashboard(request):

    today = timezone.now().date()

    active_count = Loan.objects.filter(return_date__isnull=True).count()
    overdue_count = Loan.objects.filter(
        return_date__isnull=True,
        due_date__lt=today,
    ).count()
    due_today_count = Loan.objects.filter(
        return_date__isnull=True,
        due_date=today,
    ).count()

    return render(
        request,
        "library/circulation_dashboard.html",
        {
            "active_count": active_count,
            "overdue_count": overdue_count,
            "due_today_count": due_today_count,
        }
    )


# Return lookup by copy code. Allows staff to scan/enter a copy code directly
# and be taken to the return form for the active loan on that copy.
# Open to all three roles, like `loan_return` itself: this is the step that
# finds the loan being returned, and restricting it while allowing the
# return made the workflow unreachable for an Assistant.
# How many active loans the return search lists before asking for a
# narrower term. Same shape, and the same reasoning, as the issue form's cap.
RETURN_LOOKUP_LIMIT = 25


def active_loan_for_code(copy_code):
    """The one active loan on this exact copy code, or None.

    A targeted lookup, and the only thing a scan ever runs. `copy_code` is
    unique in the database and `unique_active_loan_per_copy` allows one
    unreturned loan per copy, so this can match at most one row - there is
    nothing here to search through and nothing that grows with the
    catalogue or with how long the library has been lending.
    """

    return Loan.objects.select_related(
        "copy__volume__book__author",
        "borrower",
        "issued_by",
    ).filter(
        copy__copy_code__iexact=copy_code,
        return_date__isnull=True,
    ).first()


def active_loans_matching(term):
    """Active loans whose book, author or borrower matches `term`.

    For the half of the field that is not a scan: someone at the desk with
    a book in their hand and no readable label.

    Only what is actually out. The filter is on the loans, so a returned
    loan, a copy sitting on its shelf and a copy that was never lent are
    absent by construction rather than removed afterwards - and the cost is
    bounded by how much is out on loan today, not by the size of the
    catalogue or of the history.

    One row per loan, and a copy can hold only one active loan, so nothing
    can appear twice and no `distinct()` is needed. Ordered by due date, so
    whatever is most overdue is at the top of the list to be dealt with.
    """

    return list(
        Loan.objects.filter(
            return_date__isnull=True,
        ).filter(
            models.Q(copy__volume__book__title__icontains=term)
            | models.Q(copy__volume__book__author__name__icontains=term)
            # The borrower as well, which is what makes returning a stack
            # possible: somebody arrives with four books and their name
            # lists all four at once. Nothing new is exposed - these are
            # the loans the page already lists, found by the third thing
            # printed on the row.
            | models.Q(borrower__name__icontains=term)
        ).select_related(
            "copy__volume__book__author",
            "borrower",
        ).order_by(
            "due_date", "copy__copy_code"
        )[:RETURN_LOOKUP_LIMIT + 1]
    )


def refuse_return(loan, return_date):
    """Why this loan cannot be returned on this date, and the date.

    The validation `loan_return` already did, lifted out unchanged so the
    single return and the batch hold a book to the same three rules:
    a date is required, it has to parse, and it cannot precede the day the
    book went out.

    Returns `("", date)` when there is nothing wrong, and
    `(reason, None)` when there is.
    """

    if not return_date:
        return "Return date is required.", None

    try:
        parsed = date.fromisoformat(return_date)
    except (TypeError, ValueError):
        return "Please enter a valid return date.", None

    if parsed < loan.issue_date:
        return "Return date cannot be earlier than issue date.", None

    return "", parsed


def record_return(loan, return_date, notes, user):
    """Take one book back: the loan, the copy, the log and the queue.

    The body of `loan_return`, moved here so the batch on the return page
    does not have a second copy of it. Nothing about it changed.

    The return, the copy going back on the shelf, the log entry and the
    notification for whoever is now at the front of the queue are one
    transition: all of it happened, or none of it did.

    Locked and re-read first, the same shape as `reservations.close` and
    `inventory_session_complete`. Whether the loan is unreturned is read
    outside any transaction by both callers, so a double-clicked button or
    a retried request could both pass that check; this is where it is
    actually decided. `False` means the loan was already returned and this
    call wrote nothing rather than moving the return date - which is what
    lets the batch roll itself back and say so.
    """

    with transaction.atomic():

        locked = Loan.objects.select_for_update().filter(
            id=loan.id,
            return_date__isnull=True,
        ).first()

        if locked is None:
            return False

        locked.return_date = return_date

        # The member of staff taking the book back is the one signed in,
        # for the same reason `issued_by` is set that way on the issue
        # side.
        locked.returned_to = user

        if notes:
            locked.notes = notes

        locked.save()

        loan.copy.status = "Available"

        loan.copy.save()

        create_activity_log(
            user=locked.returned_to,
            action="RETURN",
            entity_type="BookCopy",
            entity_id=locked.copy_id,
            description=(
                f"{loan.copy.copy_code} "
                f"{loan.borrower.name} سے واپس وصول کی گئی"
            ),
        )

        # A copy is on the shelf again, so whoever is at the front of this
        # book's queue can be served now. The queue itself is untouched:
        # nothing is assigned and no copy is set aside - this only tells
        # the desk.
        notifications.announce_ready(loan.copy.volume.book)

    return True


@feature_required("circulation.return")
def loan_return_lookup(request):
    """Find what to bring back, and bring back as many as were found.

    One field, two behaviours, in that order. A code is resolved on its own
    first - that is what a barcode or QR scanner sends, it can only ever
    name one copy, and it costs one indexed lookup. Only text that is not a
    known code is searched for, so the scan path never runs a search at
    all.

    What is found goes into a list, exactly as the issue form's basket
    works and by the same function: the chosen loans ride in the query
    string, so searching for the next book cannot lose the ones already
    picked. Somebody arriving with four books is four searches and one
    submission, not four separate returns.

    The list may hold loans belonging to different borrowers. Nothing needs
    them to match - each loan already names its own borrower, and a stack
    coming off a drop-box shelf is not one person's.
    """

    today = timezone.now().date()

    # The list survives the search on a GET and comes back as hidden
    # fields on the POST, so the same two lines read both.
    source = request.POST if request.method == "POST" else request.GET

    chosen_ids = [
        int(value)
        for value in source.getlist("loans")
        if value.isdigit()
    ]

    # Re-read rather than trusted, and only what is still out: a loan
    # picked a minute ago may have been returned at another desk since, and
    # it drops out here if so.
    chosen = describe_loans(
        Loan.objects.filter(
            id__in=chosen_ids,
            return_date__isnull=True,
        ).select_related(
            "copy__volume__book__author",
            "borrower",
        ).order_by("copy__copy_code"),
        today,
    ) if chosen_ids else []

    # What is left after that read, and what was asked for. On a GET the
    # difference does not matter - a stale row simply is not shown. On a
    # POST it does: returning four of five books and saying "4 returned"
    # is worse than refusing, so the difference is reported below.
    returnable_ids = [item.id for item in chosen]
    missing_ids = [
        value for value in chosen_ids if value not in returnable_ids
    ]

    chosen_ids = returnable_ids

    term = request.GET.get("copy_code", "").strip()

    loan = None
    matches = []
    more_matches = False
    error_message = ""
    return_date = request.POST.get("return_date", "") or today.isoformat()
    notes = request.POST.get("notes", "")

    if request.method == "POST":

        if missing_ids:
            # Already back, at this desk or another one. Named, so the
            # librarian knows which row to take out rather than being told
            # a count that does not match the pile in front of them.
            already = list(
                Loan.objects.filter(
                    id__in=missing_ids
                ).select_related("copy").order_by("copy__copy_code")
            )

            error_message = gettext(
                "%(codes)s had already been returned, so nothing was "
                "recorded. Take them out of the list and submit again."
            ) % {
                "codes": ", ".join(
                    item.copy.copy_code for item in already
                )
            }

        else:
            error_message = return_chosen_loans(
                request, chosen, return_date, notes.strip()
            )

            if not error_message:
                return redirect("circulation_return_lookup")

    elif term:

        loan = active_loan_for_code(term)

        if loan is not None:
            # The overdue rule, from the one place that states it.
            describe_loans([loan], today)

            loan.add_url = selection_url(
                request, "loans", chosen_ids + [loan.id], clear_search=True
            )

        else:
            matches = active_loans_matching(term)
            more_matches = len(matches) > RETURN_LOOKUP_LIMIT
            matches = describe_loans(matches[:RETURN_LOOKUP_LIMIT], today)

            # What is already in the list is not offered again.
            matches = [
                match for match in matches if match.id not in chosen_ids
            ]

            for match in matches:
                match.add_url = selection_url(
                    request, "loans", chosen_ids + [match.id]
                )

            if not matches:

                if BookCopy.objects.filter(
                    copy_code__iexact=term
                ).exists():
                    error_message = (
                        "That copy is not out on loan, so there is nothing "
                        "to return. It may have been brought back already."
                    )

                else:
                    error_message = (
                        "No copy found with that code, and nothing on loan "
                        "matches \u201c%s\u201d. Check the label, or try "
                        "the book, the author or the borrower." % term
                    )

    for item in chosen:
        item.remove_url = selection_url(
            request,
            "loans",
            [value for value in chosen_ids if value != item.id],
        )

    return render(
        request,
        "library/loan_return_lookup.html",
        {
            "copy_code": term,
            "loan": loan,
            "matches": matches,
            "more_matches": more_matches,
            "lookup_limit": RETURN_LOOKUP_LIMIT,
            "error_message": error_message,
            "chosen": chosen,
            "chosen_ids": chosen_ids,
            "return_date": return_date,
            "notes": notes,
            "today": today,
        }
    )


def return_chosen_loans(request, chosen, return_date, notes):
    """Take the whole list back at once, or none of it.

    All-or-nothing, like the issue basket, and for the same reason: a
    partial answer leaves the librarian holding five books and knowing
    only that something went wrong. Every loan is checked against
    `refuse_return` first, then every loan is written inside one
    transaction, so a copy returned at another desk in the meantime rolls
    the submission back with the reason instead of half-recording it.

    Rows are taken in ascending id order, so two submissions that share a
    loan take its row in the same order and cannot deadlock.

    Returns "" when the books are back, and the reason when they are not.
    """

    if not chosen:
        return (
            "Nothing is in the return list. Scan a label or search for "
            "the book, then add it."
        )

    parsed = None

    for item in chosen:

        refusal, parsed_for_item = refuse_return(item, return_date)

        if refusal:
            return "%s: %s" % (item.copy.copy_code, refusal)

        parsed = parsed_for_item

    try:
        with transaction.atomic():

            for item in sorted(chosen, key=lambda item: item.id):

                if not record_return(item, parsed, notes, request.user):
                    raise PolicyRefused(
                        "%s had already been returned, so nothing was "
                        "recorded. Remove it from the list and submit "
                        "again." % item.copy.copy_code
                    )

    except PolicyRefused as refused:
        return str(refused)

    cache.delete(LOAN_CACHE_KEY)
    cache.delete(BOOK_COPY_CACHE_KEY)
    cache.delete(DASHBOARD_CACHE_KEY)

    if len(chosen) == 1:
        messages.success(
            request,
            "%s returned." % chosen[0].copy.copy_code,
        )
    else:
        messages.success(
            request,
            "%d books returned." % len(chosen),
        )

    return ""


# Renew an active loan. Extends the due date by the default loan period from
# the current due date. Returned loans cannot be renewed.
@feature_required("loans.active", "Admin", "Librarian")
def loan_renew(request, loan_id):
    """Extend a loan by the configured period, up to the renewal limit.

    Who may renew is unchanged - Admin and Librarian, per the decorator
    above - and so is the shape of the page. What is new is that the period
    and the number of renewals allowed come from the policy rather than
    from a constant and from nowhere.
    """

    loan = get_object_or_404(
        Loan.objects.select_related(
            "copy__volume__book",
            "borrower",
            "issued_by",
        ),
        id=loan_id,
    )

    active_policy = policy.load()

    error_message = ""
    renewals_used = policy.renewals_used(loan)

    # Both refusals in one place, so the page and the POST cannot disagree
    # about why: a returned loan, or one that has had its renewals.
    error_message = policy.refuse_renewal(active_policy, loan)

    new_due_date = (
        None
        if loan.return_date is not None
        else active_policy.due_date_for(loan.due_date)
    )

    today = timezone.now().date()

    if not error_message and request.method == "POST":

        # The date the librarian picked, or the policy's if the form did
        # not send one. This used to be computed and never asked for, so
        # a loan could only ever be extended by exactly the policy period.
        chosen = request.POST.get("new_due_date", "").strip()

        if chosen:

            try:
                chosen_date = date.fromisoformat(chosen)
            except ValueError:
                chosen_date = None

            if chosen_date is None:

                error_message = "Please enter a valid due date."

            elif chosen_date < today:

                # A renewal moves the date forward. Backdating one would
                # mark a book overdue that is not, which is why the field
                # will not offer a past day either.
                error_message = "The new due date cannot be in the past."

            elif chosen_date < loan.due_date:

                error_message = (
                    "The new due date must be on or after the current "
                    "due date."
                )

            else:
                new_due_date = chosen_date

    if not error_message and request.method == "POST":

        try:
            with transaction.atomic():

                # Re-read under a lock and re-check, because the count and
                # the act it counts have to be one event. Two simultaneous
                # renewals of the same loan would otherwise both read the
                # same tally and both be allowed - which is how a limit of
                # one becomes two.
                locked = Loan.objects.select_for_update().get(id=loan.id)

                refusal = policy.refuse_renewal(active_policy, locked)

                if refusal:
                    raise PolicyRefused(refusal)

                # The chosen date if there is one, the policy's otherwise.
                # Recomputed from the locked row so a concurrent renewal
                # cannot leave this extending a due date that has moved.
                if not chosen:
                    new_due_date = active_policy.due_date_for(
                        locked.due_date
                    )

                locked.due_date = new_due_date
                locked.save(update_fields=["due_date"])

                # Inside the transaction with the due date it records, so
                # the renewal and the count of renewals cannot come apart.
                create_activity_log(
                    user=request.user,
                    action="RENEW",
                    entity_type="Loan",
                    entity_id=locked.id,
                    description=(
                        f"{loan.copy.copy_code} renewed. "
                        f"New due date: {new_due_date.isoformat()}"
                    ),
                )

        except PolicyRefused as refused:
            error_message = str(refused)
            renewals_used = policy.renewals_used(loan)

        else:
            cache.delete(LOAN_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            if is_form_modal_request(request):
                return lookup_saved_response(loan.copy.copy_code)

            # The loan page it used to return to no longer exists.
            return redirect("loan_list")

    context = {
        "loan": loan,
        "new_due_date": new_due_date,
        "error_message": error_message,
        "renewals_used": renewals_used,
        "renewal_limit": (
            active_policy.max_renewals
            if active_policy.limits_renewals
            else 0
        ),
        "loan_period_days": active_policy.loan_period_days,
        # The earliest day the picker will offer. Today, or the current due
        # date when that is later - either way, never a day in the past.
        "earliest_due_date": max(today, loan.due_date),
    }

    if is_form_modal_request(request):

        return render(
            request,
            "library/partials/loan_renew_modal.html",
            context,
        )

    if error_message:
        messages.error(request, error_message)

    return redirect("loan_list")
