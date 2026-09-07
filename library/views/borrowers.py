"""The people who borrow books.

Borrowers are not users. They do not sign in and they have no password;
they are records the library keeps about who has what.
"""

from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.core.cache import cache
from django.utils import timezone
from django.db import models

from ..models import (
    Borrower,
    Loan,
)

from .. import history
from .. import reservations

from ..permissions import can_edit_library, feature_required, role_required

from .common import (
    BORROWER_CACHE_KEY,
    BORROWER_TYPES,
    COMBOBOX_LIMIT,
    DASHBOARD_CACHE_KEY,
    PAGE_SIZE,
    combobox_options_response,
    create_activity_log,
    describe_loans,
    is_combobox_request,
    query_with,
    safe_redirect_target,
)


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

    # Deliberately not cached any more. The rows now carry live loan and
    # overdue counts, and a five-minute-old count of what someone is holding
    # is worse than no count at all. It is one indexed query either way.
    borrowers = list(borrowers_query.order_by("name"))

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

    paginator = Paginator(borrowers, PAGE_SIZE)
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
            "can_delete": can_edit_library(request.user),
        }
    )

@feature_required("borrowers")
def borrower_add(request):

    error = None

    form_data = {
        "name": "",
        "phone": "",
        "borrower_type": "",
        "registration_no": "",
        "department": "",
        "address": "",
        "notes": "",
    }

    if request.method == "POST":

        form_data["name"] = request.POST.get(
            "name",
            ""
        ).strip()

        form_data["phone"] = request.POST.get(
            "phone",
            ""
        ).strip()

        form_data["borrower_type"] = request.POST.get(
            "borrower_type",
            ""
        ).strip()

        form_data["registration_no"] = request.POST.get(
            "registration_no",
            ""
        ).strip()

        form_data["department"] = request.POST.get(
            "department",
            ""
        ).strip()

        form_data["address"] = request.POST.get(
            "address",
            ""
        ).strip()

        form_data["notes"] = request.POST.get(
            "notes",
            ""
        ).strip()

        if not form_data["name"]:

            error = "Borrower name is required."

        elif not form_data["phone"]:

            error = "Phone number is required."

        elif form_data["borrower_type"] not in BORROWER_TYPES:

            error = "Please select a valid borrower type."

        elif Borrower.objects.filter(
            phone__iexact=form_data["phone"]
        ).exists():

            error = (
                "A borrower with this phone number "
                "already exists."
            )

        # `borrowers.registration_no` has a partial UNIQUE index — over the
        # non-blank values only — so a repeat would otherwise reach the
        # database as an IntegrityError. Checked here, and never merged:
        # two people can share a phone, so this says which record it
        # matched and leaves the choice to the librarian.
        elif form_data["registration_no"] and Borrower.objects.filter(
            registration_no__iexact=form_data["registration_no"]
        ).exists():

            error = (
                "Registration number \"%s\" already belongs to another "
                "borrower." % form_data["registration_no"]
            )

        else:

            borrower = Borrower.objects.create(
                name=form_data["name"],
                phone=form_data["phone"],
                borrower_type=form_data[
                    "borrower_type"
                ],
                registration_no=(
                    form_data["registration_no"]
                    or None
                ),
                department=(
                    form_data["department"]
                    or None
                ),
                address=(
                    form_data["address"]
                    or None
                ),
                notes=(
                    form_data["notes"]
                    or None
                ),
                is_active=True,
                created_at=timezone.now(),
            )

            cache.delete(
                BORROWER_CACHE_KEY
            )

            cache.delete(
                DASHBOARD_CACHE_KEY
            )

            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="Borrower",
                entity_id=borrower.id,
                description=(
                    f"{borrower.name} شامل کیا گیا"
                ),
            )

            return redirect(
                "borrower_list"
            )

    return render(
        request,
        "library/borrower_add.html",
        {
            "error": error,
            "form_data": form_data,
            "borrower_types": BORROWER_TYPES,
        }
    )

@feature_required("borrowers")
def borrower_detail(request, borrower_id):
    """One borrower: who they are, what they hold, and what they have had.

    Current loans in full — there are only ever a handful — and the history
    paginated, because that grows without limit and loading all of it would
    make the page slower every year.
    """

    borrower = get_object_or_404(Borrower, id=borrower_id)

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

    # Everything, newest first, a page at a time.
    history = loans.order_by("-issue_date", "-id")

    paginator = Paginator(history, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    describe_loans(page.object_list, today)

    return render(
        request,
        "library/borrower_detail.html",
        {
            "borrower": borrower,
            "current_loans": current,
            "active_count": len(current),
            "overdue_count": sum(
                1 for loan in current if loan.days_overdue
            ),
            "loans": page,
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
            "can_delete": can_edit_library(request.user),
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

    borrower = get_object_or_404(
        Borrower,
        id=borrower_id
    )

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    error = None

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

    if request.method == "POST":

        form_data["name"] = request.POST.get(
            "name",
            ""
        ).strip()

        form_data["phone"] = request.POST.get(
            "phone",
            ""
        ).strip()

        form_data["borrower_type"] = request.POST.get(
            "borrower_type",
            ""
        ).strip()

        form_data["registration_no"] = request.POST.get(
            "registration_no",
            ""
        ).strip()

        form_data["department"] = request.POST.get(
            "department",
            ""
        ).strip()

        form_data["address"] = request.POST.get(
            "address",
            ""
        ).strip()

        form_data["notes"] = request.POST.get(
            "notes",
            ""
        ).strip()

        active_status = request.POST.get(
            "is_active",
            ""
        )

        form_data["is_active"] = (
            active_status == "True"
        )

        if not form_data["name"]:

            error = "Borrower name is required."

        elif not form_data["phone"]:

            error = "Phone number is required."

        elif (
            form_data["borrower_type"]
            not in BORROWER_TYPES
        ):

            error = (
                "Please select a valid borrower type."
            )

        elif Borrower.objects.filter(
            phone__iexact=form_data["phone"]
        ).exclude(
            id=borrower.id
        ).exists():

            error = (
                "A borrower with this phone number "
                "already exists."
            )

        # Same partial UNIQUE index as on Add, so the same check — minus
        # this borrower, which is allowed to keep the number it has.
        elif form_data["registration_no"] and Borrower.objects.filter(
            registration_no__iexact=form_data["registration_no"]
        ).exclude(
            id=borrower.id
        ).exists():

            error = (
                "Registration number \"%s\" already belongs to another "
                "borrower." % form_data["registration_no"]
            )

        else:

            borrower.name = form_data["name"]

            borrower.phone = form_data["phone"]

            borrower.borrower_type = (
                form_data["borrower_type"]
            )

            borrower.registration_no = (
                form_data["registration_no"]
                or None
            )

            borrower.department = (
                form_data["department"]
                or None
            )

            borrower.address = (
                form_data["address"]
                or None
            )

            borrower.notes = (
                form_data["notes"]
                or None
            )

            borrower.is_active = (
                form_data["is_active"]
            )

            borrower.save()

            cache.delete(
                BORROWER_CACHE_KEY
            )

            cache.delete(
                DASHBOARD_CACHE_KEY
            )

            create_activity_log(
                user=None,
                action="UPDATE",
                entity_type="Borrower",
                entity_id=borrower.id,
                description=(
                    f"{borrower.name} updated"
                ),
            )

            return redirect(
                safe_redirect_target(request, "borrower_list")
            )

    return render(
        request,
        "library/borrower_edit.html",
        {
            "borrower": borrower,
            "form_data": form_data,
            "error": error,
            "borrower_types": BORROWER_TYPES,
            "next_url": next_url,
        }
    )

@feature_required("borrowers", "Admin", "Librarian")
def borrower_delete(request, borrower_id):

    borrower = get_object_or_404(
        Borrower,
        id=borrower_id
    )

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    # The real FK (loans.borrower_id -> borrowers.id) is NO ACTION, so ANY
    # loan record referencing this borrower — active or already returned —
    # blocks the delete at the database level, not just active ones. The
    # history is the point: it says who had which book and when, and
    # deleting the borrower would take that with it.
    loan_history_exists = Loan.objects.filter(
        borrower_id=borrower.id
    ).exists()

    # Numbers for the refusal page, so it can say what is in the way and
    # offer deactivating instead — which is what `is_active` is for.
    loan_count = Loan.objects.filter(borrower_id=borrower.id).count()

    active_loan_count = Loan.objects.filter(
        borrower_id=borrower.id,
        return_date__isnull=True,
    ).count()

    if request.method == "POST":

        if loan_history_exists:

            return render(
                request,
                "library/borrower_delete.html",
                {
                    "borrower": borrower,
                    "loan_history_exists": True,
                    "loan_count": loan_count,
                    "active_loan_count": active_loan_count,
                    "next_url": next_url,
                }
            )

        deleted_borrower_id = borrower.id
        deleted_borrower_name = borrower.name

        borrower.delete()

        cache.delete(
            BORROWER_CACHE_KEY
        )

        cache.delete(
            DASHBOARD_CACHE_KEY
        )

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="Borrower",
            entity_id=deleted_borrower_id,
            description=(
                f"{deleted_borrower_name} deleted"
            ),
        )

        return redirect(
            safe_redirect_target(request, "borrower_list")
        )

    return render(
        request,
        "library/borrower_delete.html",
        {
            "borrower": borrower,
            "loan_history_exists": loan_history_exists,
            "loan_count": loan_count,
            "active_loan_count": active_loan_count,
            "next_url": next_url,
        }
    )
