"""Modal-first workflow for adding a physical book copy."""

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.shortcuts import render

from ..models import BookCopy, BookVolume, Location, Shelf
from ..permissions import feature_required
from .common import (
    BOOK_COPY_CACHE_KEY,
    COPY_CODE_DIGITS,
    COPY_CODE_PREFIX,
    create_activity_log,
    DASHBOARD_CACHE_KEY,
    LOCATION_CACHE_KEY,
    modal_redirect,
    safe_redirect_target,
    SHELF_CACHE_KEY,
    shelf_options_for,
    volume_label,
)


COPY_STATUSES = ["Available", "Lost", "Damaged", "Missing", "Transferred"]


def _modal_request(request):
    return request.GET.get("modal") == "1" or request.POST.get("modal") == "1" or request.headers.get("HX-Request") == "true"


def _redirect_response(request, fallback="book_copy_list"):
    return modal_redirect(
        request,
        request.build_absolute_uri(safe_redirect_target(request, fallback)),
    )


def _next_copy_code():
    """Return the next LIB-###### code; the browser never chooses it."""
    last_number = 0
    for value in BookCopy.objects.filter(
        copy_code__startswith=COPY_CODE_PREFIX
    ).values_list("copy_code", flat=True):
        suffix = value[len(COPY_CODE_PREFIX):]
        if suffix.isdigit():
            last_number = max(last_number, int(suffix))
    return f"{COPY_CODE_PREFIX}{last_number + 1:0{COPY_CODE_DIGITS}d}"


@feature_required("copies", "Admin", "Librarian")
def book_copy_add_modal(request):
    """Create a physical copy with a server-generated immutable copy code."""
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
        status = request.POST.get("status", "Available").strip()
        acquisition_date = request.POST.get("acquisition_date", "").strip()
        notes = request.POST.get("notes", "").strip()

        volume = BookVolume.objects.filter(id=volume_id).first() if volume_id.isdigit() else None
        shelf = Shelf.objects.select_related("location").filter(id=shelf_id).first() if shelf_id.isdigit() else None

        if volume is None:
            error_message = "Choose a volume."
        elif shelf is None:
            error_message = "Choose a shelf."
        elif status not in COPY_STATUSES:
            error_message = "Choose a valid copy status."
        else:
            copy = None
            for _ in range(5):
                copy_code = _next_copy_code()
                try:
                    with transaction.atomic():
                        copy = BookCopy.objects.create(
                            volume=volume,
                            shelf=shelf,
                            copy_code=copy_code,
                            status=status,
                            acquisition_date=acquisition_date or None,
                            notes=notes or None,
                        )
                    break
                except IntegrityError:
                    # Another request may have taken the generated code.
                    # Re-read and generate the next code instead of exposing
                    # a duplicate-code error to the librarian.
                    copy = None

            if copy is None:
                error_message = "A copy code could not be generated. Please try again."
            else:
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
                    "book_volume_detail" if selected_volume else "book_copy_list",
                )

        form_data.update({
            "volume_id": int(volume_id) if volume_id.isdigit() else None,
            "shelf_id": int(shelf_id) if shelf_id.isdigit() else None,
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
        "generated_code": _next_copy_code(),
    }

    if _modal_request(request):
        return render(request, "library/partials/book_copy_add_modal.html", context)

    from .copies import book_copy_add
    return book_copy_add(request)
