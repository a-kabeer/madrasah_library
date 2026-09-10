"""Modal-first CRUD workflows for library locations and shelves."""

from types import SimpleNamespace

from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from ..models import BookCopy, Location, Shelf
from ..permissions import feature_required
from .common import combobox_created_response, is_combobox_request, selected_name, is_form_modal_request, modal_refusal, create_activity_log, DASHBOARD_CACHE_KEY, LOCATION_CACHE_KEY, modal_redirect, safe_redirect_target, SHELF_CACHE_KEY


def _return_to(request):
    """Where the librarian was when they opened the dialog, if it said.

    The Add Shelf button on a location page sends `next`, so finishing the
    shelf puts them back on that location rather than on the whole-library
    shelf list, which is not where they were working. Read from POST as
    well as GET because the dialog posts it back as a hidden field.

    Not validated here - `safe_redirect_target` already refuses anything
    off-site, and it is what actually decides.
    """

    return (request.POST.get("next") or request.GET.get("next") or "").strip()


def _redirect_response(request, fallback):
    """Where to send the browser once a dialog has done its work.

    `reverse(fallback)` rather than the bare name. `safe_redirect_target`
    hands its fallback straight back, and `modal_redirect` puts that in
    HX-Redirect - which htmx assigns to `window.location`, so a view *name*
    was resolved by the browser as a relative URL: saving from the Locations
    page navigated to /library/locations/location_list and answered 404.
    Every add, edit and delete in this module landed there. `redirect()`
    accepted the name on the scriptless path, which is why only the dialogs
    were affected. This is what `borrower_modals._redirect_list` already
    does.
    """

    return modal_redirect(
        request, safe_redirect_target(request, reverse(fallback))
    )


@feature_required("locations", "Admin", "Librarian")
def location_add_modal(request):
    # "Add New" in the Location dropdown posts here the way the Author,
    # Category and Publisher ones post to theirs: a name, `combobox=1`, and
    # nothing else. The answer is 204 plus the event that tells the page to
    # select what was just made.
    from_combobox = is_combobox_request(request)

    name = request.POST.get("name", "").strip() if request.method == "POST" else ""
    description = request.POST.get("description", "").strip() if request.method == "POST" else ""
    error = ""
    if request.method == "POST":
        existing = Location.objects.filter(name__iexact=name).first() if name else None
        if not name:
            error = "Location name is required."
        elif existing:
            # The dropdown asks for a name, not an id, so a name already on
            # file means "use that one" rather than being an error - the
            # same reading `category_add` gives it. It also cannot create a
            # duplicate either way.
            if from_combobox:
                return combobox_created_response("location", existing)

            error = "A location with this name already exists."
        else:
            location = Location.objects.create(name=name, description=description or None)
            cache.delete(LOCATION_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)
            create_activity_log(user=request.user, action="CREATE", entity_type="Location", entity_id=location.id, description=f"{location.name} created")

            if from_combobox:
                return combobox_created_response("location", location)

            messages.success(request, f"{location.name} added.")
            return _redirect_response(request, "location_list")
    return render(request, "library/partials/location_form_modal.html", {"action": "location_add", "editing": False, "location": None, "name": name, "description": description, "error": error, "next": _return_to(request)})


@feature_required("locations", "Admin", "Librarian")
def location_edit_modal(request, location_id):
    location = get_object_or_404(Location, id=location_id)
    name = request.POST.get("name", location.name).strip() if request.method == "POST" else location.name
    description = request.POST.get("description", location.description or "").strip() if request.method == "POST" else (location.description or "")
    error = ""
    if request.method == "POST":
        duplicate = Location.objects.filter(name__iexact=name).exclude(id=location.id).exists() if name else False
        if not name:
            error = "Location name is required."
        elif duplicate:
            error = "A location with this name already exists."
        else:
            location.name, location.description = name, description or None
            location.save(update_fields=["name", "description"])
            cache.delete(LOCATION_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)
            create_activity_log(user=request.user, action="UPDATE", entity_type="Location", entity_id=location.id, description=f"{location.name} updated")
            messages.success(request, f"{location.name} updated.")
            return _redirect_response(request, "location_list")
    return render(request, "library/partials/location_form_modal.html", {"action": "location_edit", "editing": True, "location": location, "name": name, "description": description, "error": error, "next": _return_to(request)})


@feature_required("locations", "Admin", "Librarian")
def location_delete_modal(request, location_id):
    location = get_object_or_404(Location, id=location_id)
    shelves_exist = Shelf.objects.filter(location_id=location.id).exists()
    if request.method == "POST" and not shelves_exist:
        deleted_id, deleted_name = location.id, location.name
        location.delete()
        cache.delete(LOCATION_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)
        create_activity_log(user=request.user, action="DELETE", entity_type="Location", entity_id=deleted_id, description=f"{deleted_name} deleted")
        messages.success(request, f"{deleted_name} deleted.")
        return _redirect_response(request, "location_list")

    if request.method == "POST" and shelves_exist and not is_form_modal_request(request):
        return modal_refusal(
            request,
            "%s cannot be deleted while it still has shelves. Move or delete "
            "those shelves first." % location.name,
            "location_list",
        )

    return render(request, "library/partials/location_delete_modal.html", {"location": location, "shelves_exist": shelves_exist, "next": _return_to(request)})


@feature_required("shelves", "Admin", "Librarian")
def shelf_add_modal(request):
    # "Add New" in the Shelf dropdown posts here the way the other four do,
    # with one difference the dropdown itself supplies: a shelf belongs to a
    # location, so the create button sends the chosen one alongside the
    # name. The name *is* the shelf code - a shelf has no other.
    from_combobox = is_combobox_request(request)

    location_id = request.POST.get("location", "").strip() if request.method == "POST" else request.GET.get("location", "").strip()

    if from_combobox:
        shelf_code = request.POST.get("name", "").strip()
    else:
        shelf_code = request.POST.get("shelf_code", "").strip() if request.method == "POST" else ""
    description = request.POST.get("description", "").strip() if request.method == "POST" else ""
    error = ""
    if request.method == "POST":
        valid_location = location_id.isdigit() and Location.objects.filter(id=location_id).exists()
        existing = Shelf.objects.filter(location_id=location_id, shelf_code__iexact=shelf_code).first() if valid_location and shelf_code else None
        if not valid_location:
            error = "Choose a location for the shelf."
        elif not shelf_code:
            error = "Shelf code is required."
        elif existing:
            # A code already on that shelf's location means "use that one",
            # exactly as a taken name does in the other dropdowns.
            if from_combobox:
                return combobox_created_response(
                    "shelf", SimpleNamespace(id=existing.id, name=existing.shelf_code)
                )

            error = "That location already has a shelf with this code."
        else:
            shelf = Shelf.objects.create(location_id=location_id, shelf_code=shelf_code, description=description or None)
            cache.delete(SHELF_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)
            create_activity_log(user=request.user, action="CREATE", entity_type="Shelf", entity_id=shelf.id, description=f"{shelf.shelf_code} created")

            if from_combobox:
                return combobox_created_response(
                    "shelf", SimpleNamespace(id=shelf.id, name=shelf.shelf_code)
                )

            messages.success(request, f"Shelf {shelf.shelf_code} added to {shelf.location.name}.")
            return _redirect_response(request, "shelf_list")
    return render(request, "library/partials/shelf_form_modal.html", {"action": "shelf_add", "editing": False, "shelf": None, "locations": Location.objects.order_by("name"), "location_id": location_id, "location_name": selected_name(Location, location_id), "shelf_code": shelf_code, "description": description, "error": error, "next": _return_to(request)})


@feature_required("shelves", "Admin", "Librarian")
def shelf_edit_modal(request, shelf_id):
    shelf = get_object_or_404(Shelf, id=shelf_id)
    location_id = request.POST.get("location", str(shelf.location_id)).strip() if request.method == "POST" else str(shelf.location_id)
    shelf_code = request.POST.get("shelf_code", shelf.shelf_code).strip() if request.method == "POST" else shelf.shelf_code
    description = request.POST.get("description", shelf.description or "").strip() if request.method == "POST" else (shelf.description or "")
    error = ""
    if request.method == "POST":
        valid_location = location_id.isdigit() and Location.objects.filter(id=location_id).exists()
        duplicate = Shelf.objects.filter(location_id=location_id, shelf_code__iexact=shelf_code).exclude(id=shelf.id).exists() if valid_location and shelf_code else False
        if not valid_location:
            error = "Choose a location for the shelf."
        elif not shelf_code:
            error = "Shelf code is required."
        elif duplicate:
            error = "That location already has a shelf with this code."
        else:
            shelf.location_id, shelf.shelf_code, shelf.description = location_id, shelf_code, description or None
            shelf.save(update_fields=["location", "shelf_code", "description"])
            cache.delete(SHELF_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)
            create_activity_log(user=request.user, action="UPDATE", entity_type="Shelf", entity_id=shelf.id, description=f"{shelf.shelf_code} updated")
            messages.success(request, f"Shelf {shelf.shelf_code} updated.")
            return _redirect_response(request, "shelf_list")
    return render(request, "library/partials/shelf_form_modal.html", {"action": "shelf_edit", "editing": True, "shelf": shelf, "locations": Location.objects.order_by("name"), "location_id": location_id, "location_name": selected_name(Location, location_id), "shelf_code": shelf_code, "description": description, "error": error, "next": _return_to(request)})


@feature_required("shelves", "Admin", "Librarian")
def shelf_delete_modal(request, shelf_id):
    shelf = get_object_or_404(Shelf, id=shelf_id)
    copies_exist = BookCopy.objects.filter(shelf_id=shelf.id).exists()
    if request.method == "POST" and not copies_exist:
        deleted_id, deleted_code = shelf.id, shelf.shelf_code
        shelf.delete()
        cache.delete(SHELF_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)
        create_activity_log(user=request.user, action="DELETE", entity_type="Shelf", entity_id=deleted_id, description=f"{deleted_code} deleted")
        messages.success(request, f"Shelf {deleted_code} deleted.")
        return _redirect_response(request, "shelf_list")

    if request.method == "POST" and copies_exist and not is_form_modal_request(request):
        return modal_refusal(
            request,
            "Shelf %s cannot be deleted while copies are filed on it. Move "
            "those copies first." % shelf.shelf_code,
            "shelf_list",
        )

    return render(request, "library/partials/shelf_delete_modal.html", {"shelf": shelf, "copies_exist": copies_exist, "next": _return_to(request)})
