"""Modal-first workflows for physical book copies."""

from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from ..models import BookCopy, Location, Shelf
from ..permissions import feature_required
from .common import (
    BOOK_COPY_CACHE_KEY,
    DASHBOARD_CACHE_KEY,
    LOCATION_CACHE_KEY,
    SHELF_CACHE_KEY,
    create_activity_log,
    shelf_options_for,
    volume_label,
)

COPY_STATUSES = ["Available", "Lost", "Damaged", "Missing", "Transferred"]


def _next_url(request):
    return request.POST.get("next") or request.GET.get("next") or "/library/book-copies/"


def _redirect_response(url):
    response = HttpResponse(status=204)
    response["HX-Redirect"] = url
    return response


@feature_required("copies", "Admin", "Librarian")
def book_copy_edit_modal(request, copy_id):
    """Edit copy condition/location without leaving the current list."""
    copy = get_object_or_404(BookCopy.objects.select_related("volume__book", "shelf__location"), id=copy_id)
    is_issued = copy.status == "Issued"
    error_message = ""
    form_data = {
        "location_id": str(copy.shelf.location_id) if copy.shelf_id else "",
        "shelf_id": copy.shelf_id,
        "status": copy.status,
        "acquisition_date": copy.acquisition_date.strftime("%Y-%m-%d") if copy.acquisition_date else "",
        "notes": copy.notes or "",
    }
    if request.method == "POST":
        location_id = request.POST.get("location", "").strip()
        shelf_id = request.POST.get("shelf", "").strip()
        status = "Issued" if is_issued else request.POST.get("status", "Available")
        acquisition_date = request.POST.get("acquisition_date", "").strip()
        notes = request.POST.get("notes", "").strip()
        form_data = {"location_id": location_id, "shelf_id": int(shelf_id) if shelf_id.isdigit() else None, "status": status, "acquisition_date": acquisition_date, "notes": notes}
        shelf = Shelf.objects.filter(id=shelf_id, location_id=location_id).select_related("location").first() if shelf_id.isdigit() and location_id.isdigit() else None
        if shelf is None:
            error_message = "Choose a valid shelf in the selected location."
        elif status not in COPY_STATUSES and not is_issued:
            error_message = "Choose a valid copy status."
        if not error_message:
            old_shelf, old_status = copy.shelf, copy.status
            old_details = (copy.acquisition_date, copy.notes)
            copy.shelf, copy.status = shelf, status
            copy.acquisition_date, copy.notes = acquisition_date or None, notes or None
            copy.save(update_fields=["shelf", "status", "acquisition_date", "notes"])
            cache.delete(BOOK_COPY_CACHE_KEY); cache.delete(SHELF_CACHE_KEY); cache.delete(LOCATION_CACHE_KEY); cache.delete(DASHBOARD_CACHE_KEY)
            if copy.shelf_id != (old_shelf.id if old_shelf else None):
                create_activity_log(user=request.user, action="UPDATE", entity_type="BookCopy", entity_id=copy.id, description=f"{copy.copy_code} moved from {old_shelf or 'no shelf'} to {shelf}")
            if copy.status != old_status:
                create_activity_log(user=request.user, action="UPDATE", entity_type="BookCopy", entity_id=copy.id, description=f"{copy.copy_code} status changed from {old_status} to {copy.status}")
            if (copy.acquisition_date, copy.notes) != old_details:
                create_activity_log(user=request.user, action="UPDATE", entity_type="BookCopy", entity_id=copy.id, description=f"{copy.copy_code} details updated")
            return _redirect_response(_next_url(request))
    return render(request, "library/partials/copy_edit_modal.html", {"copy": copy, "volume_name": volume_label(copy.volume), "locations": Location.objects.order_by("name"), "shelves": shelf_options_for(form_data["location_id"]), "statuses": COPY_STATUSES, "is_issued": is_issued, "error_message": error_message, "form_data": form_data})


@feature_required("copies", "Admin", "Librarian")
def book_copy_move_modal(request, copy_id):
    """Move one copy without leaving its current list."""
    copy = get_object_or_404(BookCopy.objects.select_related("volume__book", "shelf__location"), id=copy_id)
    location_id = str(copy.shelf.location_id) if copy.shelf_id else ""
    error_message = ""
    if request.method == "POST":
        location_id = request.POST.get("location", "").strip()
        shelf_id = request.POST.get("shelf", "").strip()
        shelf = Shelf.objects.filter(id=shelf_id, location_id=location_id).select_related("location").first() if shelf_id.isdigit() and location_id.isdigit() else None
        if shelf is None:
            error_message = "Choose a valid shelf in the selected location."
        else:
            old_shelf = copy.shelf
            if copy.shelf_id != shelf.id:
                copy.shelf = shelf
                copy.save(update_fields=["shelf"])
                cache.delete(BOOK_COPY_CACHE_KEY); cache.delete(SHELF_CACHE_KEY); cache.delete(LOCATION_CACHE_KEY); cache.delete(DASHBOARD_CACHE_KEY)
                create_activity_log(user=request.user, action="UPDATE", entity_type="BookCopy", entity_id=copy.id, description=f"{copy.copy_code} moved from {old_shelf or 'no shelf'} to {shelf}")
            return _redirect_response(_next_url(request))
    return render(request, "library/partials/copy_move_modal.html", {"copy": copy, "location_id": location_id, "locations": Location.objects.order_by("name"), "shelves": shelf_options_for(location_id), "error_message": error_message})
