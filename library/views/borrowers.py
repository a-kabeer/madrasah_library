"""The people who borrow books.

Borrowers are not users. They do not sign in and they have no password;
they are records the library keeps about who has what.
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.core.cache import cache
from django.utils import timezone
from django.db import models, transaction

from ..models import (
    Borrower,
    Loan,
)

from .. import reservations

from ..permissions import feature_required, passes_ceiling

from .common import (
    BORROWER_CACHE_KEY,
    BORROWER_SORT_DEFAULT,
    BORROWER_SORT_DEFAULT_DIRECTION,
    BORROWER_SORT_FIELDS,
    BORROWER_TYPES,
    COMBOBOX_LIMIT,
    DASHBOARD_CACHE_KEY,
    combobox_options_response,
    create_activity_log,
    describe_loans,
    is_combobox_request,
    is_form_modal_request,
    lookup_deleted_response,
    lookup_saved_response,
    page_size_options,
    query_with,
    resolve_page_size,
    resolve_sort,
    safe_redirect_target,
    sort_ordering,
    sortable_columns,
)


class BorrowerInUse(Exception):
    """Raised inside the delete transaction to roll it back.

    An exception rather than a return, because the check has to happen
    inside `atomic()` and the way out of a transaction that must not
    commit is to raise.
    """


# What each column will take, so the form can refuse a longer value itself
# rather than letting it reach the database and raise DataError.
BORROWER_FIELD_LIMITS = {
    "name": 255,
    "phone": 30,
    "registration_no": 100,
    "department": 255,
}


def validate_borrower_details(request, borrower=None):
    """The rules a borrower is held to, in one place.

    Add and Edit both call this, so the two cannot come apart. Pass
    `borrower` when editing: it is what excludes the record from its own
    uniqueness checks, so saving somebody unchanged is not a clash with
    themselves.

    Returns `(form_data, errors)`. `form_data` is what to show back, so a
    refused form keeps everything that was typed into it, and `errors` maps
    a field's POST name to what is wrong with it - each rendered under its
    own input.
    """

    errors = {}

    form_data = {
        field: request.POST.get(field, "").strip()
        for field in (
            "name",
            "phone",
            "borrower_type",
            "registration_no",
            "department",
            "address",
            "notes",
        )
    }

    # Only Edit offers this. On Add the borrower is active by definition -
    # there is no reason to add somebody the library will not lend to.
    form_data["is_active"] = (
        request.POST.get("is_active", "") == "True"
        if borrower is not None
        else True
    )

    if not form_data["name"]:
        errors["name"] = "Enter the borrower's name."

    if not form_data["phone"]:
        errors["phone"] = "Enter a phone number."

    if form_data["borrower_type"] not in BORROWER_TYPES:
        errors["borrower_type"] = "Choose what kind of borrower this is."

    for field, limit in BORROWER_FIELD_LIMITS.items():

        if field in errors:
            continue

        if len(form_data[field]) > limit:
            errors[field] = (
                "That is %d characters. Shorten it to %d or fewer."
                % (len(form_data[field]), limit)
            )

    # Two people can share a phone in real life, so this is not merged
    # automatically: it says which record it matched and leaves the choice
    # to the librarian.
    if "phone" not in errors and form_data["phone"]:

        clash = Borrower.objects.filter(
            phone__iexact=form_data["phone"]
        )

        if borrower is not None:
            clash = clash.exclude(id=borrower.id)

        existing = clash.only("id", "name").first()

        if existing is not None:
            errors["phone"] = (
                "%s already has this phone number. Use a different one, "
                "or edit that record instead." % existing.name
            )

    # `borrowers.registration_no` has a partial UNIQUE index - over the
    # non-blank values only - so a repeat would otherwise reach the
    # database as an IntegrityError.
    if "registration_no" not in errors and form_data["registration_no"]:

        clash = Borrower.objects.filter(
            registration_no__iexact=form_data["registration_no"]
        )

        if borrower is not None:
            clash = clash.exclude(id=borrower.id)

        existing = clash.only("id", "name").first()

        if existing is not None:
            errors["registration_no"] = (
                "%s already has this registration number."
                % existing.name
            )

    return form_data, errors


def borrower_field_error_summary(errors):
    """What the alert above the form says when fields are wrong.

    A count, not a list: each message is already under the input it is
    about. The same wording the book form uses, for the same reason.
    """

    if len(errors) == 1:
        return "There is a problem with one of the fields below."

    return "There are problems with %d of the fields below." % len(errors)


# What the borrower list's activity filter offers. Loan activity is kept
# separate from the borrower's own Active/Inactive status: one says whether
# the library still lends to them, the other what they are holding, and
# folding the two together would make both unreadable.
BORROWER_ACTIVITY_FILTERS = ("has_loans", "no_loans", "overdue", "no_overdue")




@feature_required("borrowers")
def borrower_list(request):

    search = request.GET.get(
        "search",
        ""
    ).strip()

    borrower_type = request.GET.get(
        "borrower_type",
        ""
    ).strip()

    active_status = request.GET.get(
        "status",
        ""
    ).strip()

    activity = request.GET.get("activity", "").strip()

    if activity not in BORROWER_ACTIVITY_FILTERS:
        activity = ""

    today = timezone.now().date()

    # Both counts in the one query the list already made. Derived from the
    # loans, never stored, so they cannot drift from what Loans says.
    borrowers_query = Borrower.objects.annotate(
        active_loans=models.Count(
            "loan",
            filter=models.Q(loan__return_date__isnull=True),
            distinct=True,
        ),
        overdue_loans=models.Count(
            "loan",
            filter=models.Q(
                loan__return_date__isnull=True,
                loan__due_date__lt=today,
            ),
            distinct=True,
        ),
    )

    if search:

        borrowers_query = borrowers_query.filter(
            models.Q(
                name__icontains=search
            )
            | models.Q(
                phone__icontains=search
            )
            | models.Q(
                registration_no__icontains=search
            )
            | models.Q(
                department__icontains=search
            )
        )

    if borrower_type:

        borrowers_query = borrowers_query.filter(
            borrower_type=borrower_type
        )

    if active_status == "active":

        borrowers_query = borrowers_query.filter(
            is_active=True
        )

    elif active_status == "inactive":

        borrowers_query = borrowers_query.filter(
            is_active=False
        )

    # Filtering on the annotations rather than on a second stored field, so
    # the filter and the number in the row can never disagree.
    if activity == "has_loans":
        borrowers_query = borrowers_query.filter(active_loans__gt=0)

    elif activity == "no_loans":
        borrowers_query = borrowers_query.filter(active_loans=0)

    elif activity == "overdue":
        borrowers_query = borrowers_query.filter(overdue_loans__gt=0)

    elif activity == "no_overdue":
        borrowers_query = borrowers_query.filter(overdue_loans=0)

    # The searchable dropdown on the issue form asks this view for its
    # suggestions, the same way the Author, Category and Publisher lists
    # already serve theirs. It answers before the annotations are paid for
    # and before paging: a suggestion list needs a name and an id, not a
    # loan count.
    if is_combobox_request(request):

        return combobox_options_response(
            request,
            items=list(
                Borrower.objects.filter(is_active=True).filter(
                    models.Q(name__icontains=search)
                    | models.Q(phone__icontains=search)
                    | models.Q(registration_no__icontains=search)
                ).only(
                    "id", "name", "phone", "registration_no",
                ).order_by("name")[:COMBOBOX_LIMIT + 1]
            ) if search else list(
                Borrower.objects.filter(is_active=True).only(
                    "id", "name", "phone", "registration_no",
                ).order_by("name")[:COMBOBOX_LIMIT + 1]
            ),
            search=search,
            entity_label="borrower",
            add_url=reverse("borrower_add"),
        )

    # Deliberately not cached. The rows carry live loan and overdue
    # counts, and a five-minute-old count of what someone is holding is
    # worse than no count at all. It is one indexed query either way.
    sort, direction = resolve_sort(
        request,
        BORROWER_SORT_FIELDS,
        BORROWER_SORT_DEFAULT,
        BORROWER_SORT_DEFAULT_DIRECTION,
    )

    # `sort_ordering` takes the whole whitelist and the validated key, and
    # already appends `id` as a tiebreaker - without which a run of equal
    # values can show the same borrower on two pages and skip another.
    borrowers_query = borrowers_query.order_by(
        *sort_ordering(BORROWER_SORT_FIELDS, sort, direction)
    )

    borrower_types = list(
        Borrower.objects.exclude(
            borrower_type__isnull=True
        ).exclude(
            borrower_type=""
        ).values_list(
            "borrower_type",
            flat=True
        ).distinct().order_by(
            "borrower_type"
        )
    )

    page_size = resolve_page_size(request)

    # The queryset, not a list of every borrower in the library: this used
    # to fetch and annotate all of them to show twenty-five.
    paginator = Paginator(borrowers_query, page_size)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/borrower_list.html",
        {
            "borrowers": page,
            "paginator": paginator,
            "search": search,
            "borrower_type": borrower_type,
            "active_status": active_status,
            "activity": activity,
            "borrower_types": borrower_types,
            # Overall, so the list can offer a way straight to the people
            # holding something late.
            "overdue_borrowers": Borrower.objects.filter(
                loan__return_date__isnull=True,
                loan__due_date__lt=today,
            ).distinct().count(),
            "can_delete": passes_ceiling(
                request.user, "Admin", "Librarian"
            ),

            # The table's state, by the same helpers the book and loan
            # lists use. Every link carries the rest of it, so re-sorting
            # or turning a page keeps the search and all three filters.
            "sort": sort,
            "direction": direction,
            "columns": sortable_columns(
                request,
                [
                    ("name", "Borrower"),
                    ("registration_no", "Registration No"),
                    ("borrower_type", "Type"),
                    ("active_loans", "On loan"),
                    ("status", "Status"),
                ],
                BORROWER_SORT_FIELDS,
                sort,
                direction,
            ),
            "page_size": page_size,
            # The current size, not the request: `page_size_options` marks
            # the active option by comparing against what it is given, so
            # passing the request left every option unselected and the
            # select showing the first one whatever was in force.
            "page_size_options": page_size_options(page_size),
            # Everything except the size itself, which htmx appends from
            # the select's own value, and the page, so changing the size
            # starts at the first page rather than landing past the end of
            # a shorter list. Exactly as the loan list builds it.
            "page_size_url": "?" + query_with(
                request, page=None, page_size=None
            ),
            "pagination_query": query_with(request, page=None),
            "elided_page_range": list(
                paginator.get_elided_page_range(
                    page.number,
                    on_each_side=1,
                    on_ends=1,
                )
            ),
            "page_ellipsis": Paginator.ELLIPSIS,
        }
    )

def borrower_form_modal(request, action, form_data, errors, borrower=None):
    """The Add / Edit dialog, from one template.

    One template for both, so the fields, their names and their error
    slots cannot differ between adding somebody and editing them.
    `action` is where the form posts, which is the only real difference.
    """

    return render(
        request,
        "library/partials/borrower_form_modal.html",
        {
            "action": action,
            "form_data": form_data,
            "errors": errors,
            "error": (
                borrower_field_error_summary(errors) if errors else ""
            ),
            "borrower": borrower,
            "borrower_types": BORROWER_TYPES,
        },
    )


@feature_required("borrowers")
def borrower_add(request):
    """Add a borrower, in a dialog. There is no page any more."""

    form_data = {
        "name": "",
        "phone": "",
        "borrower_type": "",
        "registration_no": "",
        "department": "",
        "address": "",
        "notes": "",
        "is_active": True,
    }

    errors = {}

    if request.method == "POST":

        form_data, errors = validate_borrower_details(request)

        if not errors:

            borrower = Borrower.objects.create(
                name=form_data["name"],
                phone=form_data["phone"],
                borrower_type=form_data["borrower_type"],
                registration_no=form_data["registration_no"] or None,
                department=form_data["department"] or None,
                address=form_data["address"] or None,
                notes=form_data["notes"] or None,
                # Active by definition: there is no reason to add
                # somebody the library will not lend to.
                is_active=True,
                created_at=timezone.now(),
            )

            cache.delete(BORROWER_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="Borrower",
                entity_id=borrower.id,
                description=f"{borrower.name} شامل کیا گیا",
            )

            if is_form_modal_request(request):
                return lookup_saved_response(borrower.name)

            return redirect("borrower_list")

    if is_form_modal_request(request):
        return borrower_form_modal(
            request, reverse("borrower_add"), form_data, errors
        )

    if errors:
        messages.error(request, borrower_field_error_summary(errors))

    return redirect("borrower_list")


# How much of a borrower's history the dialog shows before handing over to
# the loan list. Ten is a screenful in a tab; the rest is one button away,
# on a page that already sorts and pages.
BORROWER_HISTORY_PREVIEW = 10


@feature_required("borrowers")
def borrower_detail(request, borrower_id):
    """One borrower, in a dialog with a tab for each thing it says.

    Who they are, what they are holding, what they are waiting for, and
    what they have had - the same four, and the same permission, as the
    page this replaced.

    Current loans in full, because there are only ever a handful. The
    history is the ten most recent: it grows without limit, and paging it
    inside a dialog would mean a second copy of the pagination partial
    that knows how to swap a modal body. `history_url` points at the loan
    list filtered to this borrower, which pages and sorts already.
    """

    borrower = get_object_or_404(Borrower, id=borrower_id)

    # There is no borrower page any more - the dialog is the whole of it.
    # A request without `?modal=1` still has to answer, because several
    # pages link here, so it goes to the list rather than 404ing.
    if not is_form_modal_request(request):
        return redirect("borrower_list")

    today = timezone.now().date()

    # Only what the two tables actually name. `issued_by` and `returned_to`
    # were joined here as well and read nowhere, which is two joins to the
    # users table on every row of a history that only grows.
    loans = Loan.objects.filter(
        borrower=borrower
    ).select_related(
        "copy__volume__book",
    )

    # What they are holding now. Short by nature, so shown whole.
    current = describe_loans(
        loans.filter(return_date__isnull=True).order_by("due_date"),
        today,
    )

    history = describe_loans(
        loans.order_by("-issue_date", "-id")[
            :BORROWER_HISTORY_PREVIEW
        ],
        today,
    )

    return render(
        request,
        "library/partials/borrower_detail_modal.html",
        {
            "borrower": borrower,
            "current_loans": current,
            "active_count": len(current),
            "overdue_count": sum(
                1 for loan in current if loan.days_overdue
            ),
            "history": history,
            "history_count": loans.count(),
            "history_preview": BORROWER_HISTORY_PREVIEW,
            # The whole history, where it pages and sorts already.
            "history_url": (
                "%s?borrower=%d" % (reverse("loan_list"), borrower.id)
            ),
            "can_delete": passes_ceiling(
                request.user, "Admin", "Librarian"
            ),
            # What they are waiting for, with their place in each queue.
            # One query, and a borrower waits for a handful of books.
            "reservations": reservations.active_for_borrower(borrower),
        }
    )

@feature_required("borrowers")
def borrower_toggle_active(request, borrower_id):

    borrower = get_object_or_404(Borrower, id=borrower_id)

    if request.method == "POST":

        borrower.is_active = not borrower.is_active
        borrower.save(update_fields=["is_active"])

        cache.delete(BORROWER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="UPDATE",
            entity_type="Borrower",
            entity_id=borrower.id,
            description=(
                f"{borrower.name} "
                f"{'activated' if borrower.is_active else 'deactivated'}"
            ),
        )

    fallback = reverse("borrower_detail", args=[borrower.id])
    return redirect(safe_redirect_target(request, fallback))


@feature_required("borrowers")
def borrower_edit(request, borrower_id):
    """Change a borrower's details, in a dialog.

    The same rules Add is held to, from the same validator, minus this
    borrower's own phone and registration number - which they are allowed
    to keep.
    """

    borrower = get_object_or_404(Borrower, id=borrower_id)

    form_data = {
        "name": borrower.name,
        "phone": borrower.phone,
        "borrower_type": borrower.borrower_type,
        "registration_no": borrower.registration_no or "",
        "department": borrower.department or "",
        "address": borrower.address or "",
        "notes": borrower.notes or "",
        "is_active": borrower.is_active,
    }

    errors = {}

    if request.method == "POST":

        form_data, errors = validate_borrower_details(request, borrower)

        if not errors:

            borrower.name = form_data["name"]
            borrower.phone = form_data["phone"]
            borrower.borrower_type = form_data["borrower_type"]
            borrower.registration_no = (
                form_data["registration_no"] or None
            )
            borrower.department = form_data["department"] or None
            borrower.address = form_data["address"] or None
            borrower.notes = form_data["notes"] or None
            borrower.is_active = form_data["is_active"]

            borrower.save()

            cache.delete(BORROWER_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="UPDATE",
                entity_type="Borrower",
                entity_id=borrower.id,
                description=f"{borrower.name} updated",
            )

            if is_form_modal_request(request):
                return lookup_saved_response(borrower.name)

            return redirect("borrower_list")

    if is_form_modal_request(request):
        return borrower_form_modal(
            request,
            reverse("borrower_edit", args=[borrower.id]),
            form_data,
            errors,
            borrower=borrower,
        )

    if errors:
        messages.error(request, borrower_field_error_summary(errors))

    return redirect("borrower_list")


def borrower_delete_blocker(borrower):
    """Why `borrower` cannot be deleted, or "" when they can be.

    The real foreign key (loans.borrower_id -> borrowers.id) is NO ACTION,
    so ANY loan referencing them - active or long returned - blocks the
    delete at the database level. The history is the point: it says who had
    which book and when, and deleting the borrower would take that with it.

    Said as a sentence rather than left to raise an IntegrityError, and it
    names the way out, which is deactivating them - what `is_active` is
    for.
    """

    total = Loan.objects.filter(borrower_id=borrower.id).count()

    if not total:
        return ""

    out = Loan.objects.filter(
        borrower_id=borrower.id,
        return_date__isnull=True,
    ).count()

    if out:
        return (
            "%s cannot be deleted: %d of their %d loan%s %s still out, "
            "and the record of who had which book would go with them. "
            "Take the books back, then deactivate them instead."
            % (
                borrower.name,
                out,
                total,
                "" if total == 1 else "s",
                "is" if out == 1 else "are",
            )
        )

    return (
        "%s cannot be deleted: they have %d loan%s on record, and that "
        "history would go with them. Deactivate them instead - it stops "
        "the library lending to them and keeps the record."
        % (borrower.name, total, "" if total == 1 else "s")
    )


def borrower_delete_modal(request, borrower, blocker):
    return render(
        request,
        "library/partials/borrower_delete_modal.html",
        {
            "borrower": borrower,
            "blocker": blocker,
        },
    )


@feature_required("borrowers", "Admin", "Librarian")
def borrower_delete(request, borrower_id):
    """Delete a borrower, once it is established that nothing is lost."""

    borrower = get_object_or_404(Borrower, id=borrower_id)

    blocker = borrower_delete_blocker(borrower)

    if request.method == "POST" and not blocker:

        deleted_id = borrower.id
        deleted_name = borrower.name

        try:
            with transaction.atomic():

                # Asked again, inside the transaction that deletes. The
                # check above is read outside any transaction, so a loan
                # issued in between would otherwise reach the foreign key
                # and raise - a 500 where there is a sentence to say.
                blocker = borrower_delete_blocker(borrower)

                if blocker:
                    raise BorrowerInUse(blocker)

                borrower.delete()

        except BorrowerInUse as refused:
            blocker = str(refused)

        else:
            cache.delete(BORROWER_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="DELETE",
                entity_type="Borrower",
                entity_id=deleted_id,
                description=f"{deleted_name} deleted",
            )

            if is_form_modal_request(request):
                return lookup_deleted_response(deleted_name)

            return redirect("borrower_list")

    if is_form_modal_request(request):
        return borrower_delete_modal(request, borrower, blocker)

    if blocker:
        messages.error(request, blocker)

    return redirect("borrower_list")

