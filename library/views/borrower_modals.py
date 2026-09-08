"""Modal-first borrower CRUD endpoints."""

from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, models, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from ..models import Borrower, Loan
from .. import reservations
from ..permissions import can_edit_library, feature_required
from .common import BORROWER_CACHE_KEY, BORROWER_TYPES, DASHBOARD_CACHE_KEY, PAGE_SIZE, create_activity_log

BORROWER_ACTIVITY_FILTERS = ("has_loans", "no_loans", "overdue", "no_overdue")


def _redirect_list():
    response = HttpResponse(status=204)
    response["HX-Redirect"] = reverse("borrower_list")
    return response


def _form_data(request, borrower=None):
    return {
        "name": request.POST.get("name", borrower.name if borrower else "").strip(),
        "phone": request.POST.get("phone", borrower.phone if borrower else "").strip(),
        "borrower_type": request.POST.get("borrower_type", borrower.borrower_type if borrower else "").strip(),
        "registration_no": request.POST.get("registration_no", (borrower.registration_no or "") if borrower else "").strip(),
        "department": request.POST.get("department", (borrower.department or "") if borrower else "").strip(),
        "address": request.POST.get("address", (borrower.address or "") if borrower else "").strip(),
        "notes": request.POST.get("notes", (borrower.notes or "") if borrower else "").strip(),
        "is_active": request.POST.get("is_active", "True") == "True" if request.method == "POST" else (borrower.is_active if borrower else True),
    }


def _validate(data, borrower=None):
    if not data["name"]:
        return "Borrower name is required."
    if len(data["name"]) > 255:
        return "Borrower name must be 255 characters or fewer."
    if not data["phone"]:
        return "Phone number is required."
    if len(data["phone"]) > 30:
        return "Phone number must be 30 characters or fewer."
    if data["borrower_type"] not in BORROWER_TYPES:
        return "Please select a valid borrower type."
    if len(data["registration_no"]) > 100:
        return "Registration number must be 100 characters or fewer."
    if len(data["department"]) > 255:
        return "Department must be 255 characters or fewer."
    q = Borrower.objects.filter(phone__iexact=data["phone"])
    if borrower:
        q = q.exclude(id=borrower.id)
    if q.exists():
        return "A borrower with this phone number already exists."
    if data["registration_no"]:
        q = Borrower.objects.filter(registration_no__iexact=data["registration_no"])
        if borrower:
            q = q.exclude(id=borrower.id)
        if q.exists():
            return 'Registration number "%s" already belongs to another borrower.' % data["registration_no"]
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
        active_loans=models.Count("loan", filter=models.Q(loan__return_date__isnull=True), distinct=True),
        overdue_loans=models.Count("loan", filter=models.Q(loan__return_date__isnull=True, loan__due_date__lt=today), distinct=True),
    )
    if search:
        query = query.filter(models.Q(name__icontains=search) | models.Q(phone__icontains=search) | models.Q(registration_no__icontains=search) | models.Q(department__icontains=search))
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
    paginator = Paginator(query.order_by("name", "id"), PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    types = list(Borrower.objects.exclude(borrower_type__isnull=True).exclude(borrower_type="").values_list("borrower_type", flat=True).distinct().order_by("borrower_type"))
    overdue_borrowers = Borrower.objects.filter(loan__return_date__isnull=True, loan__due_date__lt=today).distinct().count()
    return render(request, "library/borrower_list_modal.html", {"borrowers": page, "paginator": paginator, "search": search, "borrower_type": borrower_type, "active_status": active_status, "activity": activity, "borrower_types": types, "overdue_borrowers": overdue_borrowers, "can_delete": can_edit_library(request.user)})


@feature_required("borrowers")
def borrower_detail_modal(request, borrower_id):
    borrower = get_object_or_404(Borrower, id=borrower_id)
    today = timezone.now().date()
    loans = Loan.objects.filter(borrower=borrower).select_related("copy__volume__book")
    current = list(loans.filter(return_date__isnull=True).order_by("due_date", "id"))
    for loan in current:
        loan.days_overdue = (today - loan.due_date).days if loan.due_date and loan.due_date < today else 0
    page = Paginator(loans.order_by("-issue_date", "-id"), PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "library/partials/borrower_detail_modal.html", {"borrower": borrower, "current_loans": current, "active_count": len(current), "overdue_count": sum(1 for loan in current if loan.days_overdue), "loans": page, "reservations": reservations.active_for_borrower(borrower), "can_delete": can_edit_library(request.user)})


@feature_required("borrowers")
def borrower_add_modal(request):
    data, error = _form_data(request), None
    if request.method == "POST":
        error = _validate(data)
        if not error:
            try:
                with transaction.atomic():
                    borrower = Borrower.objects.create(name=data["name"], phone=data["phone"], borrower_type=data["borrower_type"], registration_no=data["registration_no"] or None, department=data["department"] or None, address=data["address"] or None, notes=data["notes"] or None, is_active=True, created_at=timezone.now())
            except IntegrityError:
                error = "This borrower could not be saved because a matching record already exists. Please review the phone and registration number."
            else:
                cache.delete(BORROWER_CACHE_KEY)
                cache.delete(DASHBOARD_CACHE_KEY)
                create_activity_log(request.user, "CREATE", "Borrower", borrower.id, f"{borrower.name} added")
                return _redirect_list()
    return render(request, "library/partials/borrower_form_modal.html", {"mode": "add", "form_data": data, "error": error, "borrower_types": BORROWER_TYPES})


@feature_required("borrowers")
def borrower_edit_modal(request, borrower_id):
    borrower = get_object_or_404(Borrower, id=borrower_id)
    data, error = _form_data(request, borrower), None
    if request.method == "POST":
        error = _validate(data, borrower)
        if not error:
            try:
                with transaction.atomic():
                    for field in ("name", "phone", "borrower_type", "registration_no", "department", "address", "notes"):
                        setattr(borrower, field, data[field] or None)
                    borrower.is_active = data["is_active"]
                    borrower.save()
            except IntegrityError:
                error = "These borrower details could not be saved because they conflict with another record. Please review the phone and registration number."
            else:
                cache.delete(BORROWER_CACHE_KEY)
                cache.delete(DASHBOARD_CACHE_KEY)
                create_activity_log(request.user, "UPDATE", "Borrower", borrower.id, f"{borrower.name} updated")
                return _redirect_list()
    return render(request, "library/partials/borrower_form_modal.html", {"mode": "edit", "borrower": borrower, "form_data": data, "error": error, "borrower_types": BORROWER_TYPES})


@feature_required("borrowers", "Admin", "Librarian")
def borrower_delete_modal(request, borrower_id):
    borrower = get_object_or_404(Borrower, id=borrower_id)
    loan_count = Loan.objects.filter(borrower_id=borrower.id).count()
    active_count = Loan.objects.filter(borrower_id=borrower.id, return_date__isnull=True).count()
    if request.method == "POST" and loan_count == 0:
        deleted_id, deleted_name = borrower.id, borrower.name
        borrower.delete()
        cache.delete(BORROWER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)
        create_activity_log(request.user, "DELETE", "Borrower", deleted_id, f"{deleted_name} deleted")
        return _redirect_list()
    return render(request, "library/partials/borrower_delete_modal.html", {"borrower": borrower, "loan_history_exists": loan_count > 0, "loan_count": loan_count, "active_loan_count": active_count})
