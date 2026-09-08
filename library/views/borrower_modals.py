"""Modal-first borrower CRUD endpoints.

The existing borrower model and business rules stay intact. These endpoints
only change the presentation flow: the list remains the working surface and
Add/Edit/Delete/Details are returned as fragments for the shared dialogs.
"""

from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from ..models import Borrower, Loan
from .. import reservations
from ..permissions import can_edit_library, feature_required
from .common import (
    BORROWER_CACHE_KEY,
    BORROWER_TYPES,
    DASHBOARD_CACHE_KEY,
    PAGE_SIZE,
    create_activity_log,
)

BORROWER_ACTIVITY_FILTERS = ("has_loans", "no_loans", "overdue", "no_overdue")


def _form_data(request, borrower=None):
    source = borrower
    return {
        "name": request.POST.get("name", source.name if source else "").strip(),
        "phone": request.POST.get("phone", source.phone if source else "").strip(),
        "borrower_type": request.POST.get(
            "borrower_type", source.borrower_type if source else ""
        ).strip(),
        "registration_no": request.POST.get(
            "registration_no", (source.registration_no or "") if source else ""
        ).strip(),
        "department": request.POST.get(
            "department", (source.department or "") if source else ""
        ).strip(),
        "address": request.POST.get(
            "address", (source.address or "") if source else ""
        ).strip(),
        "notes": request.POST.get(
            "notes", (source.notes or "") if source else ""
        ).strip(),
        "is_active": (
            request.POST.get("is_active", "True") == "True"
            if request.method == "POST"
            else (source.is_active if source else True)
        ),
    }


def _validate(form_data, borrower=None):
    if not form_data["name"]:
        return "Borrower name is required."
    if not form_data["phone"]:
        return "Phone number is required."
    if form_data["borrower_type"] not in BORROWER_TYPES:
        return "Please select a valid borrower type."

    phone_qs = Borrower.objects.filter(phone__iexact=form_data["phone"])
    if borrower:
        phone_qs = phone_qs.exclude(id=borrower.id)
    if phone_qs.exists():
        return "A borrower with this phone number already exists."

    if form_data["registration_no"]:
        reg_qs = Borrower.objects.filter(
            registration_no__iexact=form_data["registration_no"]
        )
        if borrower:
            reg_qs = reg_qs.exclude(id=borrower.id)
        if reg_qs.exists():
            return (
                'Registration number "%s" already belongs to another borrower.'
                % form_data["registration_no"]
            )
    return None


@feature_required("borrowers")
def borrower_list_modal(request):
    search = request.GET.get("search", "").strip()
    borrower_type = request.GET.get("borrower_type", "").strip()
    active_status = request.GET.get("status", "").strip()
    activity = request.GET.get("activity", "").strip()
    if activity not in BORROWER_ACTIVITY_FILTERS:
        activity = ""

    today = timezone.now().date()
    query = Borrower.objects.annotate(
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
        query = query.filter(
            models.Q(name__icontains=search)
            | models.Q(phone__icontains=search)
            | models.Q(registration_no__icontains=search)
            | models.Q(department__icontains=search)
        )
    if borrower_type:
        query = query.filter(borrower_type=borrower_type)
    if active_status == "active":
        query = query.filter(is_active=True)
    elif active_status == "inactive":
        query = query.filter(is_active=False)
    if activity == "has_loans":
        query = query.filter(active_loans__gt=0)
    elif activity == "no_loans":
        query = query.filter(active_loans=0)
    elif activity == "overdue":
        query = query.filter(overdue_loans__gt=0)
    elif activity == "no_overdue":
        query = query.filter(overdue_loans=0)

    borrowers = list(query.order_by("name", "id"))
    paginator = Paginator(borrowers, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    borrower_types = list(
        Borrower.objects.exclude(borrower_type__isnull=True)
        .exclude(borrower_type="")
        .values_list("borrower_type", flat=True)
        .distinct()
        .order_by("borrower_type")
    )
    return render(
        request,
        "library/borrower_list_modal.html",
        {
            "borrowers": page,
            "paginator": paginator,
            "search": search,
            "borrower_type": borrower_type,
            "active_status": active_status,
            "activity": activity,
            "borrower_types": borrower_types,
            "overdue_borrowers": Borrower.objects.filter(
                loan__return_date__isnull=True,
                loan__due_date__lt=today,
            ).distinct().count(),
            "can_delete": can_edit_library(request.user),
        },
    )


@feature_required("borrowers")
def borrower_detail_modal(request, borrower_id):
    borrower = get_object_or_404(Borrower, id=borrower_id)
    today = timezone.now().date()
    loans = Loan.objects.filter(borrower=borrower).select_related("copy__volume__book")
    current = list(loans.filter(return_date__isnull=True).order_by("due_date", "id"))
    history = loans.order_by("-issue_date", "-id")
    page = Paginator(history, PAGE_SIZE).get_page(request.GET.get("page"))
    for loan in current:
        loan.days_overdue = max((today - loan.due_date).days, 0) if loan.due_date and loan.due_date < today else 0
    return render(
        request,
        "library/partials/borrower_detail_modal.html",
        {
            "borrower": borrower,
            "current_loans": current,
            "active_count": len(current),
            "overdue_count": sum(1 for loan in current if loan.days_overdue),
            "loans": page,
            "reservations": reservations.active_for_borrower(borrower),
            "can_delete": can_edit_library(request.user),
        },
    )


@feature_required("borrowers")
def borrower_add_modal(request):
    form_data = _form_data(request)
    error = None
    if request.method == "POST":
        error = _validate(form_data)
        if not error:
            borrower = Borrower.objects.create(
                name=form_data["name"], phone=form_data["phone"],
                borrower_type=form_data["borrower_type"],
                registration_no=form_data["registration_no"] or None,
                department=form_data["department"] or None,
                address=form_data["address"] or None,
                notes=form_data["notes"] or None,
                is_active=True, created_at=timezone.now(),
            )
            cache.delete(BORROWER_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)
            create_activity_log(None, "CREATE", "Borrower", borrower.id, f"{borrower.name} شامل کیا گیا")
            return redirect("borrower_list")
    return render(request, "library/partials/borrower_form_modal.html", {
        "mode": "add", "form_data": form_data, "error": error,
        "borrower_types": BORROWER_TYPES,
    })


@feature_required("borrowers")
def borrower_edit_modal(request, borrower_id):
    borrower = get_object_or_404(Borrower, id=borrower_id)
    form_data = _form_data(request, borrower)
    error = None
    if request.method == "POST":
        error = _validate(form_data, borrower)
        if not error:
            for field in ("name", "phone", "borrower_type", "registration_no", "department", "address", "notes"):
                setattr(borrower, field, form_data[field] or None)
            borrower.is_active = form_data["is_active"]
            borrower.save()
            cache.delete(BORROWER_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)
            create_activity_log(None, "UPDATE", "Borrower", borrower.id, f"{borrower.name} updated")
            return redirect("borrower_list")
    return render(request, "library/partials/borrower_form_modal.html", {
        "mode": "edit", "borrower": borrower, "form_data": form_data,
        "error": error, "borrower_types": BORROWER_TYPES,
    })


@feature_required("borrowers", "Admin", "Librarian")
def borrower_delete_modal(request, borrower_id):
    borrower = get_object_or_404(Borrower, id=borrower_id)
    loan_count = Loan.objects.filter(borrower_id=borrower.id).count()
    active_loan_count = Loan.objects.filter(
        borrower_id=borrower.id, return_date__isnull=True
    ).count()
    has_history = loan_count > 0
    if request.method == "POST" and not has_history:
        deleted_id, deleted_name = borrower.id, borrower.name
        borrower.delete()
        cache.delete(BORROWER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)
        create_activity_log(None, "DELETE", "Borrower", deleted_id, f"{deleted_name} deleted")
        return redirect("borrower_list")
    return render(request, "library/partials/borrower_delete_modal.html", {
        "borrower": borrower,
        "loan_history_exists": has_history,
        "loan_count": loan_count,
        "active_loan_count": active_loan_count,
    })
