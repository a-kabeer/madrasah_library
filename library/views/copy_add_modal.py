"""Modal-first workflow for adding a physical book copy."""

from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from ..models import BookCopy, BookVolume, Location, Shelf
from ..permissions import feature_required
from .common import (
    BOOK_COPY_CACHE_KEY,
    DASHBOARD_CACHE_KEY,
    LOCATION_CACHE_KEY,
    SHELF_CACHE_KEY,
    create_activity_log,
    safe_redirect_target,
    shelf_options_for,
    volume_label,
)


COPY_STATUSES = ["Available", "Lost", "Damaged", "Missing", "Transferred"]


def _modal_request(request):
    return request.GET.get("modal") == "1" or request.POST.get("modal") == "1" or request.headers.get("HX-Request") == "true"


def _redirect_response(request, fallback="book_copy_list"):
    response = HttpResponse(status=204)
    response["HX-Redirect"] = request.build_absolute_uri(
        safe_redirect_target(request, fallback)
    )
    return response


@feature_required("copies", "Admin", "Librarian")
def book_copy_add_modal(request):
    """Create a physical copy without leaving the current list/page."""
    selected_volume_id = request.GET.get("volume", request.POST.get("volume", "")).strip()
    selected_volume = None
    if selected_volume_id.isdigit():
        selected_volume = BookVolume.objects.select_related("book").filter(id=int(selected_volume_id)).first()

    locations = Location.objects.order_by("name")
    location_id = request.POST.get("location", "").strip()
    shelves = shelf_options_for(location_id) if location_id.isdigit() else Shelf.objects.none()

    form_data = {
        "volume_id": selected_volume.id if selected_volume else (int(selected_volume_id) if selected_volume_id.isdigit() else None),
        "location_id": int(location_id) if location_id.isdigit() else None,
        "shelf_id": None,
        "copy_code": request.POST.get("copy_code", "").strip(),
        "status": request.POST.get("status", "Available"),
        "acquisition_date": request.POST.get("acquisition_date", ""),
        "notes": request.POST.get("notes", "").strip(),
    }
    shelf_id = request.POST.get("shelf", "").strip()
    if shelf_id.isdigit():
        form_data["shelf_id"] = int(shelf_id)

    error_message = ""

    if request.method == "POST":
        volume_id = request.POST.get("volume", "").strip()
        shelf_id = request.POST.get("shelf", "").strip()
        copy_code = request.POST.get("copy_code", "").strip()
        status = request.POST.get("status", "Available").strip()
        acquisition_date = request.POST.get("acquisition_date", "").strip()
        notes = request.POST.get("notes", "").strip()

        volume = BookVolume.objects.filter(id=volume_id).first() if volume_id.isdigit() else None
        shelf = Shelf.objects.select_related("location").filter(id=shelf_id).first() if shelf_id.isdigit() else None

        if volume is None:
            error_message = "Choose a volume."
        elif shelf is None:
            error_message = "Choose a shelf."
        elif not copy_code:
            error_message = "Copy code is required."
        elif status not in COPY_STATUSES:
            error_message = "Choose a valid copy status."
        elif BookCopy.objects.filter(copy_code__iexact=copy_code).exists():
            error_message = "A book copy with this Copy Code already exists."
        else:
            copy = BookCopy.objects.create(
                volume=volume,
                shelf=shelf,
                copy_code=copy_code,
                status=status,
                acquisition_date=acquisition_date or None,
                notes=notes or None,
            )
            cache.delete(BOOK_COPY_CACHE_KEY)
            cache.delete(SHELF_CACHE_KEY)
            cache.delete(LOCATION_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)
            create_activity_log(
                user=request.user,
                action="CREATE",
                entity_type="BookCopy",
                entity_id=copy.id,
                description=f"{copy.copy_code} added",
            )
            return _redirect_response(
                request,
                f"book_volume_detail" if selected_volume else "book_copy_list",
            )

        form_data.update({
            "volume_id": int(volume_id) if volume_id.isdigit() else None,
            "shelf_id": int(shelf_id) if shelf_id.isdigit() else None,
            "copy_code": copy_code,
            "status": status,
            "acquisition_date": acquisition_date,
            "notes": notes,
        })
        selected_volume = volume or selected_volume
        location_id = str(shelf.location_id) if shelf else request.POST.get("location", "").strip()
        shelves = shelf_options_for(location_id) if location_id.isdigit() else Shelf.objects.none()

    context = {
        "selected_volume": selected_volume,
        "volumes": BookVolume.objects.select_related("book").order_by("book__title", "volume_number"),
        "locations": locations,
        "shelves": shelves,
        "statuses": COPY_STATUSES,
        "error_message": error_message,
        "form_data": form_data,
        "volume_name": volume_label(selected_volume) if selected_volume else "",
    }

    if _modal_request(request):
        return render(request, "library/partials/book_copy_add_modal.html", context)

    from .copies import book_copy_add
    return book_copy_add(request)
