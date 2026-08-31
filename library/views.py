from datetime import date, timedelta
import json
import os

from PIL import Image, UnidentifiedImageError

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.core.cache import cache
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.db import models
from django.db.models import Q

from .models import (
    Author,
    Category,
    Publisher,
    Location,
    Shelf,
    Book,
    BookVolume,
    BookContent,
    BookCopy,
    Loan,
    Borrower,
    User,
    ActivityLog,
    OrganizationSettings,
    validate_hex_color,
)
from .context_processors import clear_branding_cache
from .permissions import can_edit_library, role_required

PAGE_SIZE = 25
DEFAULT_LOAN_PERIOD_DAYS = 14

# How many suggestions the searchable dropdowns (comboboxes) show at once.
COMBOBOX_LIMIT = 20

# Accepted image uploads (book covers, organisation logo).
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# Favicons additionally allow .ico, which Pillow can read.
FAVICON_EXTENSIONS = IMAGE_EXTENSIONS + (".ico",)

CATEGORY_CACHE_KEY = "categories"
AUTHOR_CACHE_KEY = "authors"
PUBLISHER_CACHE_KEY = "publishers"
LOCATION_CACHE_KEY = "locations"
SHELF_CACHE_KEY = "shelves"
BOOK_CACHE_KEY = "books"
BOOK_VOLUME_CACHE_KEY = "book_volumes"
BOOK_CONTENT_CACHE_KEY = "book_contents"
BOOK_COPY_CACHE_KEY = "book_copies"
LOAN_CACHE_KEY = "loans"
BORROWER_CACHE_KEY = "borrowers"
USER_CACHE_KEY = "users"
ACTIVITY_LOG_CACHE_KEY = "activity_logs"
DASHBOARD_CACHE_KEY = "dashboard_stats"

BORROWER_TYPES = ("Student", "Teacher", "Staff", "Other")
USER_ROLES = ("Admin", "Librarian", "Assistant")


def is_combobox_request(request):
    """True for the searchable-dropdown (combobox) traffic on Add/Edit Book.

    The combobox reuses the ordinary list and add views; this flag is what
    tells them to answer with a small partial / trigger event instead of a
    full page or a redirect.
    """

    return (
        request.headers.get("HX-Request") == "true"
        and (
            request.GET.get("combobox")
            or request.POST.get("combobox")
        )
    )


def selected_name(model, pk):
    """Display name for an id submitted by a combobox, or "" if unusable.

    The searchable dropdowns submit an id but display a name, so any page
    that redisplays a combobox needs the name back — whether it is a form
    bouncing on a validation error or a list showing its active filters.
    """

    if not pk:
        return ""

    obj = model.objects.filter(id=pk).first()

    return obj.name if obj else ""


def describe_size_limit(limit):
    """"1 MB" / "256 KB" — whichever reads better for this limit."""

    if limit >= 1024 * 1024:
        return f"{limit // (1024 * 1024)} MB"

    return f"{limit // 1024} KB"


def validate_image_upload(
    upload,
    max_bytes,
    extensions=IMAGE_EXTENSIONS,
    label="Image",
):
    """Check an uploaded image, returning an error message or None.

    Both the file name and the browser-supplied content type are chosen by
    the client, so neither is trusted on its own: the bytes have to decode
    as an image before the file is stored.
    """

    if upload.size > max_bytes:

        return (
            f"{label} must be "
            f"{describe_size_limit(max_bytes)} or smaller."
        )

    extension = os.path.splitext(upload.name)[1].lower()

    if extension not in extensions:

        allowed = ", ".join(
            ext.lstrip(".").upper()
            for ext in extensions
        )

        return f"{label} must be a {allowed} file."

    try:
        upload.seek(0)
        Image.open(upload).verify()

    except (UnidentifiedImageError, OSError, ValueError):

        return "That file could not be read as an image."

    finally:
        # verify() consumes the stream; rewind so the file can be saved.
        upload.seek(0)

    return None


def validate_cover_image(upload):
    """Check an uploaded book cover."""

    return validate_image_upload(
        upload,
        max_bytes=settings.COVER_IMAGE_MAX_BYTES,
        label="Cover image",
    )


def combobox_options_response(
    request,
    items,
    search,
    entity_label,
    add_url,
):
    """Render the suggestion list for a combobox search."""

    folded = search.casefold()

    exact_match = any(
        item.name.casefold() == folded
        for item in items
    )

    # Comboboxes used as list filters opt out of creation: picking something
    # to filter by should never add a record.
    creation_offered = request.GET.get("allow_create") != "0"

    return render(
        request,
        "library/partials/combobox_options.html",
        {
            "items": items[:COMBOBOX_LIMIT],
            "total_count": len(items),
            "limit": COMBOBOX_LIMIT,
            "search": search,
            "exact_match": exact_match,
            "entity_label": entity_label,
            "add_url": add_url,
            "can_create": (
                creation_offered
                and can_edit_library(request.user)
            ),
        }
    )


def combobox_created_response(entity_type, obj):
    """Tell the page a combobox created (or matched) `obj`, so it can select it.

    Returns "no content" plus an HX-Trigger event; there is nothing to swap
    because the only thing that should change is the dropdown's selection,
    which the page's own JavaScript applies from the event payload.
    """

    response = HttpResponse(status=204)

    response["HX-Trigger"] = json.dumps({
        "comboboxItemCreated": {
            "type": entity_type,
            "id": obj.id,
            "name": obj.name,
        }
    })

    return response


def safe_redirect_target(request, fallback):
    next_url = request.POST.get("next") or request.GET.get("next")

    if next_url and url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url

    return fallback


@login_not_required
def login_view(request):

    error = None

    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)

            return redirect(safe_redirect_target(request, "dashboard"))

        error = "Invalid username or password."

    return render(
        request,
        "library/login.html",
        {
            "error": error,
        }
    )


def logout_view(request):
    logout(request)
    return redirect("login")


def profile_view(request):

    error = None
    success = None

    if request.method == "POST":

        current_password = request.POST.get("current_password", "")
        new_password = request.POST.get("new_password", "")
        confirm_password = request.POST.get("confirm_password", "")

        if not request.user.check_password(current_password):

            error = "Current password is incorrect."

        elif not new_password:

            error = "New password is required."

        elif new_password != confirm_password:

            error = "New password and confirmation do not match."

        elif len(new_password) < 8:

            error = "New password must be at least 8 characters."

        else:

            request.user.set_password(new_password)
            request.user.save()

            # Keep the current session valid after changing our own password.
            update_session_auth_hash(request, request.user)

            success = "Password updated successfully."

    return render(
        request,
        "library/profile.html",
        {
            "error": error,
            "success": success,
            "theme_choices": User.THEME_CHOICES,
        }
    )


#Appearance (Light / Dark / System)
def theme_set(request):
    """Save the signed-in user's appearance choice.

    Shared by the topbar selector (which posts with HTMX and wants nothing
    swapped back) and the profile page (a plain form post that should land
    back where it came from).
    """

    if request.method != "POST":

        return HttpResponseBadRequest("POST required.")

    theme = request.POST.get("theme", "").strip()

    valid = {choice for choice, _ in User.THEME_CHOICES}

    if theme not in valid:

        return HttpResponseBadRequest("Unknown theme.")

    request.user.theme_preference = theme
    request.user.save(update_fields=["theme_preference"])

    if request.headers.get("HX-Request") == "true":

        # Nothing to swap: the page already applied the change locally.
        return HttpResponse(status=204)

    messages.success(request, "Appearance updated.")

    return redirect(
        safe_redirect_target(request, "profile")
    )


#Organization branding
@role_required("Admin")
def branding_settings(request):

    branding = OrganizationSettings.load()

    error = None

    if request.method == "POST":

        name = request.POST.get("name", "").strip()
        primary_color = request.POST.get("primary_color", "").strip()
        secondary_color = request.POST.get("secondary_color", "").strip()
        accent_color = request.POST.get("accent_color", "").strip()
        contact_email = request.POST.get("contact_email", "").strip()
        contact_phone = request.POST.get("contact_phone", "").strip()
        footer_text = request.POST.get("footer_text", "").strip()

        logo = request.FILES.get("logo")
        favicon = request.FILES.get("favicon")

        remove_logo = request.POST.get("remove_logo") == "on"
        remove_favicon = request.POST.get("remove_favicon") == "on"

        # Colours first: cheapest to check, and a bad one shouldn't leave a
        # freshly uploaded file behind.
        for value in (primary_color, secondary_color, accent_color):

            try:
                validate_hex_color(value)

            except ValidationError as exc:

                error = exc.messages[0]
                break

        if error is None and logo:

            error = validate_image_upload(
                logo,
                max_bytes=settings.LOGO_MAX_BYTES,
                label="Logo",
            )

        if error is None and favicon:

            error = validate_image_upload(
                favicon,
                max_bytes=settings.FAVICON_MAX_BYTES,
                extensions=FAVICON_EXTENSIONS,
                label="Favicon",
            )

        if error is None:

            branding.name = name
            branding.primary_color = primary_color
            branding.secondary_color = secondary_color
            branding.accent_color = accent_color
            branding.contact_email = contact_email
            branding.contact_phone = contact_phone
            branding.footer_text = footer_text

            # Remember the previous files so their storage can be cleaned up
            # once the new state is safely saved. An upload wins over the
            # remove checkbox, since choosing a file is the more specific
            # intent — same rule as replacing a book cover.
            previous_logo = branding.logo.name
            previous_favicon = branding.favicon.name

            if logo:
                branding.logo = logo

            elif remove_logo:
                branding.logo = None

            if favicon:
                branding.favicon = favicon

            elif remove_favicon:
                branding.favicon = None

            branding.save()

            for previous, current in (
                (previous_logo, branding.logo),
                (previous_favicon, branding.favicon),
            ):

                if previous and current.name != previous:
                    current.storage.delete(previous)

            clear_branding_cache()

            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="OrganizationSettings",
                entity_id=branding.id,
                description="Organisation branding updated",
            )

            messages.success(
                request,
                "Branding updated.",
            )

            return redirect("branding_settings")

        # Fell through with an error: show what was typed rather than
        # silently discarding it.
        branding.name = name
        branding.primary_color = primary_color
        branding.secondary_color = secondary_color
        branding.accent_color = accent_color
        branding.contact_email = contact_email
        branding.contact_phone = contact_phone
        branding.footer_text = footer_text

    return render(
        request,
        "library/branding_settings.html",
        {
            "settings_obj": branding,
            "error": error,
        }
    )


#Category View
def category_list(request):

    search = request.GET.get("search", "").strip()

    if search:
        categories = list(
            Category.objects.filter(
                name__icontains=search
            )
        )

    else:
        categories = cache.get(CATEGORY_CACHE_KEY)

        if categories is None:
            categories = list(
                Category.objects.all()
            )

            cache.set(
                CATEGORY_CACHE_KEY,
                categories,
                timeout=300
            )

    if is_combobox_request(request):

        return combobox_options_response(
            request,
            items=categories,
            search=search,
            entity_label="category",
            add_url=reverse("category_add"),
        )

    paginator = Paginator(categories, PAGE_SIZE)
    categories = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/category_list.html",
        {
            "categories": categories,
            "search": search,
        }
    )

def category_detail(request, category_id):

    category = get_object_or_404(Category, id=category_id)

    books = Book.objects.filter(
        category=category
    ).select_related(
        "author",
        "publisher",
    ).order_by("title")

    return render(
        request,
        "library/category_detail.html",
        {
            "category": category,
            "books": books,
        }
    )

#Category Add
@role_required("Admin", "Librarian")
def category_add(request):

    error = None
    name = ""
    from_combobox = is_combobox_request(request)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()

        duplicate = None

        if name:
            duplicate = Category.objects.filter(
                name__iexact=name
            ).first()

        if not name:

            error = "Category name is required."

        elif duplicate is not None:

            # The searchable dropdown asks for a name, not an id, so an
            # already-taken name means "use that one" rather than being an
            # error. Keeps a race with another user from dead-ending the
            # book form, and can't create a duplicate either way.
            if from_combobox:

                return combobox_created_response("category", duplicate)

            error = "A category with this name already exists."

        else:
            category = Category.objects.create(name=name)

            cache.delete(CATEGORY_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="Category",
                entity_id=category.id,
                description=f"{category.name} شامل کی گئی",
            )

            if from_combobox:

                return combobox_created_response("category", category)

            return redirect("category_list")

    return render(
        request,
        "library/category_add.html",
        {
            "error": error,
            "name": name,
        }
    )

#Category Edit
@role_required("Admin", "Librarian")
def category_edit(request, category_id):

    category = get_object_or_404(Category, id=category_id)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()

        if name:
            category.name = name
            category.save()

            cache.delete(CATEGORY_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="UPDATE",
                entity_type="Category",
                entity_id=category.id,
                description=f"{category.name} updated",
            )

            return redirect("category_list")

    return render(
        request,
        "library/category_edit.html",
        {
            "category": category
        }
    )

#Category Delete
@role_required("Admin", "Librarian")
def category_delete(request, category_id):

    category = get_object_or_404(Category, id=category_id)

    books_exist = Book.objects.filter(
        category_id=category.id
    ).exists()

    if request.method == "POST":

        if books_exist:

            return render(
                request,
                "library/category_delete.html",
                {
                    "category": category,
                    "books_exist": True,
                }
            )

        deleted_category_id = category.id
        deleted_category_name = category.name

        category.delete()

        cache.delete(CATEGORY_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="Category",
            entity_id=deleted_category_id,
            description=f"{deleted_category_name} deleted",
        )

        return redirect("category_list")

    return render(
        request,
        "library/category_delete.html",
        {
            "category": category,
            "books_exist": books_exist,
        }
    )

#Author View
def author_list(request):

    search = request.GET.get("search", "").strip()

    if search:
        authors = list(
            Author.objects.filter(
                name__icontains=search
            )
        )

    else:
        authors = cache.get(AUTHOR_CACHE_KEY)

        if authors is None:
            authors = list(
                Author.objects.all()
            )

            cache.set(
                AUTHOR_CACHE_KEY,
                authors,
                timeout=300
            )

    if is_combobox_request(request):

        return combobox_options_response(
            request,
            items=authors,
            search=search,
            entity_label="author",
            add_url=reverse("author_add"),
        )

    paginator = Paginator(authors, PAGE_SIZE)
    authors = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/author_list.html",
        {
            "authors": authors,
            "search": search,
        }
    )

def author_detail(request, author_id):

    author = get_object_or_404(Author, id=author_id)

    books = Book.objects.filter(
        author=author
    ).select_related(
        "category",
        "publisher",
    ).order_by("title")

    return render(
        request,
        "library/author_detail.html",
        {
            "author": author,
            "books": books,
        }
    )

#Author Add
@role_required("Admin", "Librarian")
def author_add(request):

    error = None
    name = ""
    from_combobox = is_combobox_request(request)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()

        duplicate = None

        if name:
            duplicate = Author.objects.filter(
                name__iexact=name
            ).first()

        if not name:

            error = "Author name is required."

        elif duplicate is not None:

            if from_combobox:

                return combobox_created_response("author", duplicate)

            error = "An author with this name already exists."

        else:
            author = Author.objects.create(name=name)

            cache.delete(AUTHOR_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="Author",
                entity_id=author.id,
                description=f"{author.name} شامل کیے گئے",
            )

            if from_combobox:

                return combobox_created_response("author", author)

            return redirect("author_list")

    return render(
        request,
        "library/author_add.html",
        {
            "error": error,
            "name": name,
        }
    )

#Author Edit
@role_required("Admin", "Librarian")
def author_edit(request, author_id):

    author = get_object_or_404(Author, id=author_id)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()

        if name:
            author.name = name
            author.save()

            cache.delete(AUTHOR_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="UPDATE",
                entity_type="Author",
                entity_id=author.id,
                description=f"{author.name} updated",
            )

            return redirect("author_list")

    return render(
        request,
        "library/author_edit.html",
        {
            "author": author
        }
    )

#Author Delete
@role_required("Admin", "Librarian")
def author_delete(request, author_id):

    author = get_object_or_404(Author, id=author_id)

    books_exist = Book.objects.filter(
        author_id=author.id
    ).exists()

    if request.method == "POST":

        if books_exist:

            return render(
                request,
                "library/author_delete.html",
                {
                    "author": author,
                    "books_exist": True,
                }
            )

        deleted_author_id = author.id
        deleted_author_name = author.name

        author.delete()

        cache.delete(AUTHOR_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="Author",
            entity_id=deleted_author_id,
            description=f"{deleted_author_name} deleted",
        )

        return redirect("author_list")

    return render(
        request,
        "library/author_delete.html",
        {
            "author": author,
            "books_exist": books_exist,
        }
    )

#Publisher View
def publisher_list(request):

    search = request.GET.get("search", "").strip()

    if search:
        publishers = list(
            Publisher.objects.filter(
                models.Q(name__icontains=search)
                | models.Q(city__icontains=search)
            )
        )

    else:
        publishers = cache.get(PUBLISHER_CACHE_KEY)

        if publishers is None:
            publishers = list(
                Publisher.objects.all()
            )

            cache.set(
                PUBLISHER_CACHE_KEY,
                publishers,
                timeout=300
            )

    if is_combobox_request(request):

        return combobox_options_response(
            request,
            items=publishers,
            search=search,
            entity_label="publisher",
            add_url=reverse("publisher_add"),
        )

    paginator = Paginator(publishers, PAGE_SIZE)
    publishers = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/publisher_list.html",
        {
            "publishers": publishers,
            "search": search,
        }
    )

def publisher_detail(request, publisher_id):

    publisher = get_object_or_404(Publisher, id=publisher_id)

    books = Book.objects.filter(
        publisher=publisher
    ).select_related(
        "author",
        "category",
    ).order_by("title")

    return render(
        request,
        "library/publisher_detail.html",
        {
            "publisher": publisher,
            "books": books,
        }
    )

#Publisher Add
@role_required("Admin", "Librarian")
def publisher_add(request):

    form_data = {}
    from_combobox = is_combobox_request(request)

    def render_form(error, form_data):
        return render(
            request,
            "library/publisher_add.html",
            {
                "error": error,
                "form_data": form_data,
            }
        )

    if request.method == "POST":

        name = request.POST.get(
            "name",
            ""
        ).strip()

        city = request.POST.get(
            "city",
            ""
        ).strip()

        form_data = {
            "name": name,
            "city": city,
        }

        if not name:

            return render_form(
                "Publisher name is required.",
                form_data,
            )

        duplicate = Publisher.objects.filter(
            name__iexact=name
        ).first()

        if duplicate is not None:

            if from_combobox:

                return combobox_created_response(
                    "publisher",
                    duplicate,
                )

            return render_form(
                (
                    "A publisher with this name "
                    "already exists."
                ),
                form_data,
            )

        publisher = Publisher.objects.create(
            name=name,
            city=city or None,
        )

        cache.delete(
            PUBLISHER_CACHE_KEY
        )

        cache.delete(
            DASHBOARD_CACHE_KEY
        )

        create_activity_log(
            user=None,
            action="CREATE",
            entity_type="Publisher",
            entity_id=publisher.id,
            description=(
                f"{publisher.name} شامل کیا گیا"
            ),
        )

        if from_combobox:

            return combobox_created_response(
                "publisher",
                publisher,
            )

        return redirect(
            "publisher_list"
        )

    return render_form(None, form_data)

#Publisher Edit
@role_required("Admin", "Librarian")
def publisher_edit(request, publisher_id):

    publisher = get_object_or_404(
        Publisher,
        id=publisher_id
    )

    form_data = {
        "name": publisher.name,
        "city": publisher.city or "",
    }

    if request.method == "POST":

        name = request.POST.get(
            "name",
            ""
        ).strip()

        city = request.POST.get(
            "city",
            ""
        ).strip()

        form_data = {
            "name": name,
            "city": city,
        }

        if not name:

            return render(
                request,
                "library/publisher_edit.html",
                {
                    "publisher": publisher,
                    "error": (
                        "Publisher name is required."
                    ),
                    "form_data": form_data,
                }
            )

        duplicate_exists = Publisher.objects.filter(
            name__iexact=name
        ).exclude(
            id=publisher.id
        ).exists()

        if duplicate_exists:

            return render(
                request,
                "library/publisher_edit.html",
                {
                    "publisher": publisher,
                    "error": (
                        "A publisher with this name "
                        "already exists."
                    ),
                    "form_data": form_data,
                }
            )

        publisher.name = name
        publisher.city = city or None

        publisher.save()

        cache.delete(
            PUBLISHER_CACHE_KEY
        )

        cache.delete(
            DASHBOARD_CACHE_KEY
        )

        create_activity_log(
            user=None,
            action="UPDATE",
            entity_type="Publisher",
            entity_id=publisher.id,
            description=(
                f"{publisher.name} updated"
            ),
        )

        return redirect(
            "publisher_list"
        )

    return render(
        request,
        "library/publisher_edit.html",
        {
            "publisher": publisher,
            "form_data": form_data,
        }
    )

#Publisher Delete
@role_required("Admin", "Librarian")
def publisher_delete(request, publisher_id):

    publisher = get_object_or_404(Publisher, id=publisher_id)

    books_exist = Book.objects.filter(
        publisher_id=publisher.id
    ).exists()

    if request.method == "POST":

        if books_exist:

            return render(
                request,
                "library/publisher_delete.html",
                {
                    "publisher": publisher,
                    "books_exist": True,
                }
            )

        deleted_publisher_id = publisher.id
        deleted_publisher_name = publisher.name

        publisher.delete()

        cache.delete(PUBLISHER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="Publisher",
            entity_id=deleted_publisher_id,
            description=f"{deleted_publisher_name} deleted",
        )

        return redirect("publisher_list")

    return render(
        request,
        "library/publisher_delete.html",
        {
            "publisher": publisher,
            "books_exist": books_exist,
        }
    )

#Location View
def location_list(request):

    search = request.GET.get("search", "").strip()

    locations_query = Location.objects.all()

    if search:

        locations_query = locations_query.filter(
            models.Q(name__icontains=search)
            | models.Q(description__icontains=search)
        )

    locations = list(
        locations_query.annotate(
            shelf_count=models.Count(
                "shelf",
                distinct=True
            ),
            copy_count=models.Count(
                "shelf__bookcopy",
                distinct=True
            ),
        )
    )

    paginator = Paginator(locations, PAGE_SIZE)
    locations = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/location_list.html",
        {
            "locations": locations,
            "search": search,
        }
    )

def location_detail(request, location_id):

    location = get_object_or_404(
        Location,
        id=location_id
    )

    shelves = Shelf.objects.filter(
        location=location
    ).annotate(
        copy_count=models.Count(
            "bookcopy"
        )
    )

    return render(
        request,
        "library/location_detail.html",
        {
            "location": location,
            "shelves": shelves,
        }
    )

#Location Add
@role_required("Admin", "Librarian")
def location_add(request):

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()

        if name:
            location = Location.objects.create(
                name=name,
                description=description or None
            )

            cache.delete(LOCATION_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="Location",
                entity_id=location.id,
                description=f"{location.name} شامل کی گئی",
            )

            return redirect("location_list")

    return render(
        request,
        "library/location_add.html"
    )

#Location Edit
@role_required("Admin", "Librarian")
def location_edit(request, location_id):

    location = get_object_or_404(Location, id=location_id)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()

        if name:
            location.name = name
            location.description = description or None

            location.save()

            cache.delete(LOCATION_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="UPDATE",
                entity_type="Location",
                entity_id=location.id,
                description=f"{location.name} updated",
            )

            return redirect("location_detail", location_id=location.id)

    return render(
        request,
        "library/location_edit.html",
        {
            "location": location
        }
    )

#Location Delete
@role_required("Admin", "Librarian")
def location_delete(request, location_id):

    location = get_object_or_404(Location, id=location_id)

    shelves_exist = Shelf.objects.filter(
        location_id=location.id
    ).exists()

    if request.method == "POST":

        if shelves_exist:

            return render(
                request,
                "library/location_delete.html",
                {
                    "location": location,
                    "shelves_exist": True,
                }
            )

        deleted_location_id = location.id
        deleted_location_name = location.name

        location.delete()

        cache.delete(LOCATION_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="Location",
            entity_id=deleted_location_id,
            description=f"{deleted_location_name} deleted",
        )

        return redirect("location_list")

    return render(
        request,
        "library/location_delete.html",
        {
            "location": location,
            "shelves_exist": shelves_exist,
        }
    )

#Shelf List
def shelf_list(request):

    search = request.GET.get("search", "").strip()
    location_id = request.GET.get("location", "").strip()

    if search or location_id:
        shelves = Shelf.objects.select_related(
            "location"
        )

        if search:
            shelves = shelves.filter(
                models.Q(shelf_code__icontains=search)
                | models.Q(location__name__icontains=search)
                | models.Q(description__icontains=search)
            )

        if location_id:
            shelves = shelves.filter(location_id=location_id)

        shelves = list(shelves)

    else:
        shelves = cache.get(SHELF_CACHE_KEY)

        if shelves is None:
            shelves = list(
                Shelf.objects.select_related(
                    "location"
                )
            )

            cache.set(
                SHELF_CACHE_KEY,
                shelves,
                timeout=300
            )

    locations = Location.objects.all().order_by("name")

    paginator = Paginator(shelves, PAGE_SIZE)
    shelves = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/shelf_list.html",
        {
            "shelves": shelves,
            "search": search,
            "location_id": location_id,
            "locations": locations,
        }
    )

def shelf_detail(request, shelf_id):

    shelf = get_object_or_404(
        Shelf.objects.select_related(
            "location"
        ),
        id=shelf_id
    )

    copies = BookCopy.objects.filter(
        shelf=shelf
    ).select_related(
        "volume__book"
    )

    return render(
        request,
        "library/shelf_detail.html",
        {
            "shelf": shelf,
            "copies": copies,
        }
    )

#Shelf Add
@role_required("Admin", "Librarian")
def shelf_add(request):

    locations = Location.objects.all()

    if request.method == "POST":
        location_id = request.POST.get("location")
        shelf_code = request.POST.get("shelf_code", "").strip()
        description = request.POST.get("description", "").strip()

        if location_id and shelf_code:
            shelf = Shelf.objects.create(
                location_id=location_id,
                shelf_code=shelf_code,
                description=description or None
            )

            cache.delete(SHELF_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="Shelf",
                entity_id=shelf.id,
                description=f"{shelf.shelf_code} شامل کیا گیا",
            )

            return redirect(safe_redirect_target(request, "shelf_list"))

    return render(
        request,
        "library/shelf_add.html",
        {
            "locations": locations
        }
    )

#Shelf Edit
@role_required("Admin", "Librarian")
def shelf_edit(request, shelf_id):

    shelf = get_object_or_404(Shelf, id=shelf_id)
    locations = Location.objects.all()

    if request.method == "POST":
        location_id = request.POST.get("location")
        shelf_code = request.POST.get("shelf_code", "").strip()
        description = request.POST.get("description", "").strip()

        if location_id and shelf_code:
            shelf.location_id = location_id
            shelf.shelf_code = shelf_code
            shelf.description = description or None

            shelf.save()

            cache.delete(SHELF_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="UPDATE",
                entity_type="Shelf",
                entity_id=shelf.id,
                description=f"{shelf.shelf_code} updated",
            )

            return redirect("shelf_detail", shelf_id=shelf.id)

    return render(
        request,
        "library/shelf_edit.html",
        {
            "shelf": shelf,
            "locations": locations
        }
    )

#Shelf Delete
@role_required("Admin", "Librarian")
def shelf_delete(request, shelf_id):

    shelf = get_object_or_404(Shelf, id=shelf_id)

    copies_exist = BookCopy.objects.filter(
        shelf_id=shelf.id
    ).exists()

    if request.method == "POST":

        if copies_exist:

            return render(
                request,
                "library/shelf_delete.html",
                {
                    "shelf": shelf,
                    "copies_exist": True,
                }
            )

        deleted_shelf_id = shelf.id
        deleted_shelf_code = shelf.shelf_code

        shelf.delete()

        cache.delete(SHELF_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="Shelf",
            entity_id=deleted_shelf_id,
            description=f"{deleted_shelf_code} deleted",
        )

        return redirect("shelf_list")

    return render(
        request,
        "library/shelf_delete.html",
        {
            "shelf": shelf,
            "copies_exist": copies_exist,
        }
    )


def book_list(request):

    title = request.GET.get("title", "").strip()
    author_id = request.GET.get("author", "").strip()
    category_id = request.GET.get("category", "").strip()
    publisher_id = request.GET.get("publisher", "").strip()

    books = Book.objects.select_related(
        "author",
        "category",
        "publisher",
    )

    if title:
        books = books.filter(title__icontains=title)

    if author_id:
        books = books.filter(author_id=author_id)

    if category_id:
        books = books.filter(category_id=category_id)

    if publisher_id:
        books = books.filter(publisher_id=publisher_id)

    books = books.order_by("title")

    paginator = Paginator(books, PAGE_SIZE)
    books = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/book_list.html",
        {
            "books": books,
            "title": title,
            "author_id": author_id,
            "category_id": category_id,
            "publisher_id": publisher_id,
            # The filter dropdowns submit an id but display a name, so each
            # active filter needs its label to stay filled in on reload.
            "author_name": selected_name(Author, author_id),
            "category_name": selected_name(Category, category_id),
            "publisher_name": selected_name(Publisher, publisher_id),
        }
    )


@role_required("Admin", "Librarian")
def book_add(request):

    error = None

    form_data = {
        "title": "",
        "author": "",
        "author_name": "",
        "category": "",
        "category_name": "",
        "publisher": "",
        "publisher_name": "",
    }

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        author_id = request.POST.get("author")
        category_id = request.POST.get("category")
        publisher_id = request.POST.get("publisher")

        form_data = {
            "title": title,
            "author": author_id or "",
            "author_name": selected_name(Author, author_id),
            "category": category_id or "",
            "category_name": selected_name(Category, category_id),
            "publisher": publisher_id or "",
            "publisher_name": selected_name(Publisher, publisher_id),
        }

        cover_image = request.FILES.get("cover_image")

        cover_error = (
            validate_cover_image(cover_image)
            if cover_image
            else None
        )

        if not title or not author_id:

            error = "Title and Author are required."

        elif cover_error:

            error = cover_error

        else:
            book = Book.objects.create(
                title=title,
                author_id=author_id,
                category_id=category_id or None,
                publisher_id=publisher_id or None,
                cover_image=cover_image or None,
            )

            cache.delete(BOOK_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="Book",
                entity_id=book.id,
                description=f"{book.title} شامل کی گئی",
            )

            return redirect("book_list")

    return render(
        request,
        "library/book_add.html",
        {
            "error": error,
            "form_data": form_data,
        }
    )


@role_required("Admin", "Librarian")
def book_edit(request, book_id):

    book = get_object_or_404(Book, id=book_id)

    from_page = request.GET.get(
        "from",
        request.POST.get("from", "")
    )

    error = None

    form_data = {
        "title": book.title,
        "author": book.author_id or "",
        "author_name": book.author.name if book.author_id else "",
        "category": book.category_id or "",
        "category_name": book.category.name if book.category_id else "",
        "publisher": book.publisher_id or "",
        "publisher_name": book.publisher.name if book.publisher_id else "",
    }

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        author_id = request.POST.get("author")
        category_id = request.POST.get("category")
        publisher_id = request.POST.get("publisher")

        form_data = {
            "title": title,
            "author": author_id or "",
            "author_name": selected_name(Author, author_id),
            "category": category_id or "",
            "category_name": selected_name(Category, category_id),
            "publisher": publisher_id or "",
            "publisher_name": selected_name(Publisher, publisher_id),
        }

        cover_image = request.FILES.get("cover_image")
        remove_cover = request.POST.get("remove_cover") == "on"

        cover_error = (
            validate_cover_image(cover_image)
            if cover_image
            else None
        )

        if not title or not author_id:

            error = "Title and Author are required."

        elif cover_error:

            error = cover_error

        else:
            book.title = title
            book.author_id = author_id
            book.category_id = category_id or None
            book.publisher_id = publisher_id or None

            # Whatever the cover was before, so its file can be deleted once
            # the new state is safely saved. An upload wins over the remove
            # checkbox, since choosing a file is the more specific intent.
            previous_cover = book.cover_image.name

            if cover_image:
                book.cover_image = cover_image

            elif remove_cover:
                book.cover_image = None

            book.save()

            replaced = (
                previous_cover
                and book.cover_image.name != previous_cover
            )

            if replaced:
                # Nothing references the old file now; leaving it behind
                # would just accumulate orphans in MEDIA_ROOT.
                book.cover_image.storage.delete(previous_cover)

            if remove_cover and not book.cover_image:
                # A cleared FileField saves as "" rather than NULL. Books
                # that never had a cover are NULL, so write NULL here too
                # and keep one representation of "no cover" in the column.
                Book.objects.filter(pk=book.pk).update(cover_image=None)

            cache.delete(BOOK_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="UPDATE",
                entity_type="Book",
                entity_id=book.id,
                description=f"{book.title} updated",
            )

            if from_page == "detail":

                return redirect("book_detail", book_id=book.id)

            return redirect("book_list")

    return render(
        request,
        "library/book_edit.html",
        {
            "book": book,
            "error": error,
            "form_data": form_data,
            "from_page": from_page,
        }
    )


@role_required("Admin", "Librarian")
def book_delete(request, book_id):

    book = get_object_or_404(Book, id=book_id)

    from_page = request.GET.get(
        "from",
        request.POST.get("from", "")
    )

    if request.method == "POST":
        deleted_book_id = book.id
        deleted_book_title = book.title

        book.delete()

        cache.delete(BOOK_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="Book",
            entity_id=deleted_book_id,
            description=f"{deleted_book_title} deleted",
        )

        return redirect("book_list")

    return render(
        request,
        "library/book_delete.html",
        {
            "book": book,
            "from_page": from_page,
        }
    )

def book_detail(request, book_id):

    book = get_object_or_404(
        Book.objects.select_related(
            "author",
            "category",
            "publisher",
        ),
        id=book_id
    )

    volumes = BookVolume.objects.filter(
        book=book
    ).order_by(
        "volume_number"
    )

    return render(
        request,
        "library/book_detail.html",
        {
            "book": book,
            "volumes": volumes,
        }
    )

def book_volume_list(request):

    search = request.GET.get("search", "").strip()
    book_id = request.GET.get("book", "").strip()

    if search or book_id:

        volumes = BookVolume.objects.select_related(
            "book"
        )

        if search:

            query = (
                models.Q(book__title__icontains=search)
                | models.Q(title__icontains=search)
            )

            if search.isdigit():
                query |= models.Q(volume_number=int(search))

            volumes = volumes.filter(query)

        if book_id:
            volumes = volumes.filter(book_id=book_id)

        volumes = list(volumes)

    else:

        volumes = cache.get(BOOK_VOLUME_CACHE_KEY)

        if volumes is None:

            volumes = list(
                BookVolume.objects.select_related(
                    "book"
                )
            )

            cache.set(
                BOOK_VOLUME_CACHE_KEY,
                volumes,
                timeout=300
            )

    books = Book.objects.all().order_by("title")

    paginator = Paginator(volumes, PAGE_SIZE)
    volumes = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/book_volume_list.html",
        {
            "volumes": volumes,
            "search": search,
            "book_id": book_id,
            "books": books,
        }
    )


@role_required("Admin", "Librarian")
def book_volume_add(request):

    books = Book.objects.all()

    selected_book_id = request.GET.get("book", "")

    error = None
    volume_number = ""
    title = ""

    if request.method == "POST":

        book_id = request.POST.get("book")
        volume_number = request.POST.get("volume_number", "")
        title = request.POST.get("title", "").strip()

        selected_book_id = book_id or ""

        if not book_id or not volume_number or not title:

            error = "Please fill in all required fields."

        else:

            volume = BookVolume.objects.create(
                book_id=book_id,
                volume_number=volume_number,
                title=title
            )

            cache.delete(BOOK_VOLUME_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="BookVolume",
                entity_id=volume.id,
                description=(
                    f"{volume.title} "
                    f"(Volume {volume.volume_number}) شامل کی گئی"
                ),
            )

            # Return to the selected book
            return redirect(
                "book_detail",
                book_id=book_id
            )

    return render(
        request,
        "library/book_volume_add.html",
        {
            "books": books,
            "selected_book_id": selected_book_id,
            "error": error,
            "volume_number": volume_number,
            "title": title,
        }
    )

def book_volume_detail(request, volume_id):

    volume = get_object_or_404(
        BookVolume.objects.select_related(
            "book",
            "book__author",
            "book__category",
            "book__publisher",
        ),
        id=volume_id
    )

    copies = BookCopy.objects.select_related(
        "shelf",
        "shelf__location",
    ).filter(
        volume=volume
    )

    contents = BookContent.objects.filter(
    volume=volume
).order_by(
    "sort_order"
)
    return render(
        request,
        "library/book_volume_detail.html",
        {
            "volume": volume,
            "copies": copies,
            "contents": contents,
        }
    )

@role_required("Admin", "Librarian")
def book_volume_edit(request, volume_id):

    volume = get_object_or_404(BookVolume, id=volume_id)
    books = Book.objects.all()

    from_page = request.GET.get(
        "from",
        request.POST.get("from", "")
    )

    if request.method == "POST":
        book_id = request.POST.get("book")
        volume_number = request.POST.get("volume_number")
        title = request.POST.get("title", "").strip()

        if book_id and volume_number and title:
            volume.book_id = book_id
            volume.volume_number = volume_number
            volume.title = title

            volume.save()

            cache.delete(BOOK_VOLUME_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="UPDATE",
                entity_type="BookVolume",
                entity_id=volume.id,
                description=(
                    f"{volume.title} "
                    f"(Volume {volume.volume_number}) updated"
                ),
            )

            if from_page == "detail":

                return redirect("book_volume_detail", volume_id=volume.id)

            return redirect("book_volume_list")

    return render(
        request,
        "library/book_volume_edit.html",
        {
            "volume": volume,
            "books": books,
            "from_page": from_page,
        }
    )


@role_required("Admin", "Librarian")
def book_volume_delete(request, volume_id):

    volume = get_object_or_404(BookVolume, id=volume_id)

    from_page = request.GET.get(
        "from",
        request.POST.get("from", "")
    )

    if request.method == "POST":
        deleted_volume_id = volume.id
        deleted_volume_title = volume.title
        deleted_volume_number = volume.volume_number

        volume.delete()

        cache.delete(BOOK_VOLUME_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="BookVolume",
            entity_id=deleted_volume_id,
            description=(
                f"{deleted_volume_title} "
                f"(Volume {deleted_volume_number}) deleted"
            ),
        )

        return redirect("book_volume_list")

    return render(
        request,
        "library/book_volume_delete.html",
        {
            "volume": volume,
            "from_page": from_page,
        }
    )


def book_content_list(request):

    search = request.GET.get("search", "").strip()
    volume_id = request.GET.get("volume", "").strip()
    content_type = request.GET.get("content_type", "").strip()

    if search or volume_id or content_type:

        contents = BookContent.objects.select_related(
            "volume__book",
            "parent",
        )

        if search:

            query = (
                models.Q(title__icontains=search)
                | models.Q(content_type__icontains=search)
                | models.Q(volume__title__icontains=search)
                | models.Q(volume__book__title__icontains=search)
            )

            if search.isdigit():
                query |= models.Q(
                    page_number=int(search)
                )

            contents = contents.filter(query)

        if volume_id:
            contents = contents.filter(volume_id=volume_id)

        if content_type:
            contents = contents.filter(content_type=content_type)

        contents = list(contents)

    else:

        contents = cache.get(BOOK_CONTENT_CACHE_KEY)

        if contents is None:

            contents = list(
                BookContent.objects.select_related(
                    "volume__book",
                    "parent",
                )
            )

            cache.set(
                BOOK_CONTENT_CACHE_KEY,
                contents,
                timeout=300
            )

    volumes = BookVolume.objects.select_related(
        "book"
    ).order_by(
        "book__title",
        "volume_number"
    )

    content_types = list(
        BookContent.objects.exclude(
            content_type__isnull=True
        ).exclude(
            content_type=""
        ).values_list(
            "content_type",
            flat=True
        ).distinct().order_by("content_type")
    )

    paginator = Paginator(contents, PAGE_SIZE)
    contents = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/book_content_list.html",
        {
            "contents": contents,
            "search": search,
            "volume_id": volume_id,
            "content_type": content_type,
            "volumes": volumes,
            "content_types": content_types,
        }
    )

def book_content_detail(request, content_id):

    content = get_object_or_404(
        BookContent.objects.select_related(
            "volume",
            "volume__book",
            "parent",
        ),
        id=content_id
    )

    from_page = request.GET.get(
        "from",
        ""
    )

    return render(
        request,
        "library/book_content_detail.html",
        {
            "content": content,
            "from_page": from_page,
        }
    )

@role_required("Admin", "Librarian")
def book_content_add(request):

    selected_volume_id = request.GET.get(
        "volume",
        ""
    )

    selected_volume = None

    if selected_volume_id.isdigit():

        selected_volume = BookVolume.objects.select_related(
            "book"
        ).filter(
            id=selected_volume_id
        ).first()


    volumes = BookVolume.objects.select_related(
        "book"
    ).order_by(
        "book__title",
        "volume_number"
    )


    parents = BookContent.objects.select_related(
        "volume"
    ).order_by(
        "volume",
        "sort_order"
    )


    error_message = ""


    form_data = {
        "volume_id": (
            selected_volume.id
            if selected_volume
            else None
        ),
        "parent_id": None,
        "title": "",
        "content_type": "",
        "page_number": "",
        "sort_order": 0,
    }


    if request.method == "POST":

        volume_id = request.POST.get(
            "volume"
        )

        parent_id = request.POST.get(
            "parent"
        )

        title = request.POST.get(
            "title",
            ""
        ).strip()

        content_type = request.POST.get(
            "content_type",
            ""
        ).strip()

        page_number = request.POST.get(
            "page_number"
        )

        sort_order = request.POST.get(
            "sort_order"
        ) or 0


        form_data = {
            "volume_id": (
                int(volume_id)
                if volume_id and volume_id.isdigit()
                else None
            ),
            "parent_id": (
                int(parent_id)
                if parent_id and parent_id.isdigit()
                else None
            ),
            "title": title,
            "content_type": content_type,
            "page_number": page_number or "",
            "sort_order": sort_order,
        }


        if not volume_id or not title:

            error_message = (
                "Please fill in all required fields."
            )

        else:

            content = BookContent.objects.create(
                volume_id=volume_id,
                parent_id=parent_id or None,
                title=title,
                content_type=content_type or None,
                page_number=page_number or None,
                sort_order=sort_order,
            )


            cache.delete(
                BOOK_CONTENT_CACHE_KEY
            )

            cache.delete(
                DASHBOARD_CACHE_KEY
            )


            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="BookContent",
                entity_id=content.id,
                description=(
                    f"{content.title} شامل کیا گیا"
                ),
            )


            # If opened from Volume Detail,
            # return to the same Volume

            if selected_volume:

                return redirect(
                    "book_volume_detail",
                    volume_id=volume_id
                )


            # Otherwise return to content list

            return redirect(
                "book_content_list"
            )


    # If a volume was selected,
    # only show parents from that volume

    if selected_volume:

        parents = parents.filter(
            volume=selected_volume
        )


    return render(
        request,
        "library/book_content_add.html",
        {
            "volumes": volumes,
            "selected_volume": selected_volume,
            "parents": parents,
            "error_message": error_message,
            "form_data": form_data,
        }
    )


@role_required("Admin", "Librarian")
def book_content_edit(request, content_id):

    content = get_object_or_404(
        BookContent.objects.select_related(
            "volume",
            "volume__book",
        ),
        id=content_id
    )

    from_page = request.GET.get(
    "from",
    request.POST.get("from", "")
)

    volumes = BookVolume.objects.select_related(
        "book"
    ).order_by(
        "book__title",
        "volume_number"
    )

    parents = BookContent.objects.exclude(
        id=content.id
    ).filter(
        volume=content.volume
    )

    if request.method == "POST":

        volume_id = request.POST.get(
            "volume"
        )

        parent_id = request.POST.get(
            "parent"
        )

        content.title = request.POST.get(
            "title",
            ""
        ).strip()

        content.content_type = request.POST.get(
            "content_type",
            ""
        ).strip()

        content.page_number = (
            request.POST.get("page_number")
            or None
        )

        content.sort_order = (
            request.POST.get("sort_order")
            or 0
        )

        content.parent_id = (
            parent_id
            if parent_id
            else None
        )

        # Keep the selected/current volume
        if volume_id:
            content.volume_id = volume_id

        content.save()

        cache.delete(
            BOOK_CONTENT_CACHE_KEY
        )

        cache.delete(
            DASHBOARD_CACHE_KEY
        )

        if from_page == "volume":

            return redirect(
                "book_volume_detail",
                volume_id=content.volume.id
            )

        return redirect(
            "book_content_list"
        )

    return render(
    request,
    "library/book_content_edit.html",
    {
        "content": content,
        "volumes": volumes,
        "parents": parents,
        "from_page": from_page,
        "volume_id": content.volume_id,
    }
)


@role_required("Admin", "Librarian")
def book_content_delete(request, content_id):

    content = get_object_or_404(
        BookContent.objects.select_related(
            "volume",
            "volume__book",
        ),
        id=content_id
    )

    from_page = request.GET.get(
        "from",
        request.POST.get("from", "")
    )

    volume_id = content.volume_id

    if request.method == "POST":

        content_title = content.title

        content.delete()

        cache.delete(
            BOOK_CONTENT_CACHE_KEY
        )

        cache.delete(
            DASHBOARD_CACHE_KEY
        )

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="BookContent",
            entity_id=content_id,
            description=(
                f"{content_title} حذف کیا گیا"
            ),
        )

        if from_page == "volume":

            return redirect(
                "book_volume_detail",
                volume_id=volume_id
            )

        return redirect(
            "book_content_list"
        )

    return render(
        request,
        "library/book_content_delete.html",
        {
            "content": content,
            "from_page": from_page,
            "volume_id": volume_id,
        }
    )

def book_copy_list(request):

    copy_code = request.GET.get("copy_code", "").strip()
    book_id = request.GET.get("book", "").strip()
    volume_id = request.GET.get("volume", "").strip()
    status = request.GET.get("status", "").strip()
    location_id = request.GET.get("location", "").strip()
    shelf_id = request.GET.get("shelf", "").strip()

    copies_query = BookCopy.objects.select_related(
        "volume__book",
        "shelf__location",
    )

    if copy_code:
        copies_query = copies_query.filter(
            copy_code__icontains=copy_code
        )

    if book_id:
        copies_query = copies_query.filter(
            volume__book_id=book_id
        )

    if volume_id:
        copies_query = copies_query.filter(
            volume_id=volume_id
        )

    if status:
        copies_query = copies_query.filter(
            status=status
        )

    if location_id:
        copies_query = copies_query.filter(
            shelf__location_id=location_id
        )

    if shelf_id:
        copies_query = copies_query.filter(
            shelf_id=shelf_id
        )

    copies = copies_query.order_by("copy_code")

    paginator = Paginator(copies, PAGE_SIZE)
    copies = paginator.get_page(request.GET.get("page"))

    active_loans = Loan.objects.filter(
        return_date__isnull=True
    ).select_related(
        "borrower"
    )

    active_loans_dict = {
        loan.copy_id: loan
        for loan in active_loans
    }

    today = timezone.now().date()

    for copy in copies:

        copy.active_loan = active_loans_dict.get(
            copy.id
        )

        copy.days_overdue = 0

        if (
            copy.active_loan
            and copy.active_loan.due_date < today
        ):
            copy.days_overdue = (
                today - copy.active_loan.due_date
            ).days

    books = Book.objects.all().order_by(
        "title"
    )

    volumes = BookVolume.objects.select_related(
        "book"
    ).order_by(
        "book__title",
        "volume_number"
    )

    locations = Location.objects.all().order_by(
        "name"
    )

    shelves = Shelf.objects.select_related(
        "location"
    ).order_by(
        "location__name",
        "shelf_code"
    )

    statuses = [
        "Available",
        "Issued",
        "Lost",
        "Damaged",
        "Missing",
        "Transferred",
    ]

    return render(
        request,
        "library/book_copy_list.html",
        {
            "copies": copies,
            "copy_code": copy_code,
            "book_id": book_id,
            "volume_id": volume_id,
            "status": status,
            "location_id": location_id,
            "shelf_id": shelf_id,
            "books": books,
            "volumes": volumes,
            "locations": locations,
            "shelves": shelves,
            "statuses": statuses,
        }
    )

def book_copy_detail(request, copy_id):

    from_page = request.GET.get(
        "from",
        ""
    )

    copy = get_object_or_404(
        BookCopy.objects.select_related(
            "volume__book",
            "shelf__location",
        ),
        id=copy_id
    )

    active_loan = Loan.objects.filter(
        copy=copy,
        return_date__isnull=True
    ).select_related(
        "borrower"
    ).first()

    loan_history = Loan.objects.filter(
        copy=copy
    ).select_related(
        "borrower",
        "issued_by",
        "returned_to",
    ).order_by(
        "-issue_date"
    )

    return render(
        request,
        "library/book_copy_detail.html",
        {
            "copy": copy,
            "active_loan": active_loan,
            "loan_history": loan_history,
            "from_page": from_page,
        }
    )

@role_required("Admin", "Librarian")
def book_copy_move(request, copy_id):

    copy = get_object_or_404(
        BookCopy.objects.select_related(
            "volume__book",
            "shelf__location",
        ),
        id=copy_id
    )

    from_page = request.GET.get(
        "from",
        request.POST.get("from", "")
    )

    shelves = Shelf.objects.select_related(
        "location"
    ).order_by(
        "location__name",
        "shelf_code"
    )

    error_message = ""

    if request.method == "POST":

        new_shelf_id = request.POST.get("shelf")

        if not new_shelf_id:

            error_message = "Please select a shelf."

        else:

            new_shelf = get_object_or_404(Shelf, id=new_shelf_id)
            old_shelf = copy.shelf

            copy.shelf = new_shelf
            copy.save()

            cache.delete(BOOK_COPY_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="UPDATE",
                entity_type="BookCopy",
                entity_id=copy.id,
                description=(
                    f"{copy.copy_code} moved from "
                    f"{old_shelf if old_shelf else 'no shelf'} "
                    f"to {new_shelf}"
                ),
            )

            if from_page == "shelf" and old_shelf:
                return redirect("shelf_detail", shelf_id=old_shelf.id)

            if from_page == "volume":
                return redirect("book_volume_detail", volume_id=copy.volume_id)

            return redirect("book_copy_detail", copy_id=copy.id)

    return render(
        request,
        "library/book_copy_move.html",
        {
            "copy": copy,
            "shelves": shelves,
            "error_message": error_message,
            "from_page": from_page,
        }
    )

@role_required("Admin", "Librarian")
def book_copy_add(request):

    selected_volume_id = request.GET.get(
        "volume",
        ""
    )

    selected_volume = None

    if selected_volume_id.isdigit():

        selected_volume = BookVolume.objects.select_related(
            "book"
        ).filter(
            id=selected_volume_id
        ).first()

    volumes = BookVolume.objects.select_related(
        "book"
    ).order_by(
        "book__title",
        "volume_number"
    )

    shelves = Shelf.objects.select_related(
        "location"
    ).order_by(
        "location__name",
        "shelf_code"
    )

    statuses = [
        "Available",
        "Lost",
        "Damaged",
        "Missing",
        "Transferred",
    ]

    error_message = ""

    form_data = {
        "volume_id": (
            selected_volume.id
            if selected_volume
            else None
        ),
        "shelf_id": None,
        "copy_code": "",
        "status": "Available",
        "acquisition_date": "",
        "notes": "",
    }

    if request.method == "POST":

        volume_id = request.POST.get(
            "volume"
        )

        shelf_id = request.POST.get(
            "shelf"
        )

        copy_code = request.POST.get(
            "copy_code",
            ""
        ).strip()

        status = request.POST.get(
            "status",
            "Available"
        )

        acquisition_date = request.POST.get(
            "acquisition_date"
        )

        notes = request.POST.get(
            "notes",
            ""
        ).strip()

        form_data = {
            "volume_id": (
                int(volume_id)
                if volume_id and volume_id.isdigit()
                else None
            ),
            "shelf_id": (
                int(shelf_id)
                if shelf_id and shelf_id.isdigit()
                else None
            ),
            "copy_code": copy_code,
            "status": status,
            "acquisition_date": acquisition_date or "",
            "notes": notes,
        }

        if not volume_id or not shelf_id or not copy_code:

            error_message = (
                "Please fill in all required fields."
            )

        elif BookCopy.objects.filter(
            copy_code__iexact=copy_code
        ).exists():

            error_message = (
                "A book copy with this Copy Code "
                "already exists."
            )

        else:

            copy = BookCopy.objects.create(
                volume_id=volume_id,
                shelf_id=shelf_id,
                copy_code=copy_code,
                status=status,
                acquisition_date=acquisition_date or None,
                notes=notes or None,
            )

            cache.delete(
                BOOK_COPY_CACHE_KEY
            )

            cache.delete(
                DASHBOARD_CACHE_KEY
            )

            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="BookCopy",
                entity_id=copy.id,
                description=(
                    f"{copy.copy_code} شامل کی گئی"
                ),
            )

            if selected_volume:

                return redirect(
                    "book_volume_detail",
                    volume_id=volume_id
                )

            return redirect(
                "book_copy_list"
            )

    return render(
        request,
        "library/book_copy_add.html",
        {
            "volumes": volumes,
            "selected_volume": selected_volume,
            "shelves": shelves,
            "statuses": statuses,
            "error_message": error_message,
            "form_data": form_data,
        }
    )


@role_required("Admin", "Librarian")
def book_copy_edit(request, copy_id):

    copy = get_object_or_404(
    BookCopy.objects.select_related(
        "volume__book",
        "shelf__location",
                            ),
        id=copy_id
        )
    from_page = request.GET.get(
    "from",
    request.POST.get("from", "")
)

    volumes = BookVolume.objects.select_related(
        "book"
    ).order_by(
        "book__title",
        "volume_number"
    )

    shelves = Shelf.objects.select_related(
        "location"
    ).order_by(
        "location__name",
        "shelf_code"
    )

    statuses = [
        "Available",
        "Lost",
        "Damaged",
        "Missing",
        "Transferred",
    ]

    is_issued = copy.status == "Issued"

    error_message = ""

    form_data = {
        "volume_id": copy.volume_id,
        "shelf_id": copy.shelf_id,
        "copy_code": copy.copy_code,
        "status": copy.status,
        "acquisition_date": (
            copy.acquisition_date.strftime("%Y-%m-%d")
            if copy.acquisition_date
            else ""
        ),
        "notes": copy.notes or "",
    }

    if request.method == "POST":

        volume_id = request.POST.get(
            "volume"
        )

        shelf_id = request.POST.get(
            "shelf"
        )

        copy_code = request.POST.get(
            "copy_code",
            ""
        ).strip()

        acquisition_date = request.POST.get(
            "acquisition_date"
        )

        notes = request.POST.get(
            "notes",
            ""
        ).strip()

        if is_issued:

            status = "Issued"

        else:

            status = request.POST.get(
                "status",
                "Available"
            )

        form_data = {
            "volume_id": int(volume_id)
            if volume_id and volume_id.isdigit()
            else None,

            "shelf_id": int(shelf_id)
            if shelf_id and shelf_id.isdigit()
            else None,

            "copy_code": copy_code,

            "status": status,

            "acquisition_date": acquisition_date or "",

            "notes": notes,
        }

        if volume_id and shelf_id and copy_code:

            duplicate_exists = BookCopy.objects.filter(
                copy_code__iexact=copy_code
            ).exclude(
                id=copy.id
            ).exists()

            if duplicate_exists:

                error_message = (
                    "A book copy with this Copy Code "
                    "already exists."
                )

            else:

                copy.volume_id = volume_id
                copy.shelf_id = shelf_id
                copy.copy_code = copy_code
                copy.status = status
                copy.acquisition_date = (
                    acquisition_date or None
                )
                copy.notes = notes or None

                copy.save()

                cache.delete(
                    BOOK_COPY_CACHE_KEY
                )

                cache.delete(
                    DASHBOARD_CACHE_KEY
                )

                create_activity_log(
                    user=None,
                    action="UPDATE",
                    entity_type="BookCopy",
                    entity_id=copy.id,
                    description=(
                        f"{copy.copy_code} updated"
                    ),
                )

                if from_page == "shelf":

                    return redirect(
                    "shelf_detail",
                    shelf_id=copy.shelf_id
    )

                if from_page == "volume":

                    return redirect(
                        "book_volume_detail",
                        volume_id=copy.volume_id
                    )

                return redirect(
                "book_copy_list"
            )

        else:

            error_message = (
                "Please fill in all required fields."
            )

    return render(
        request,
        "library/book_copy_edit.html",
        {
            "copy": copy,
            "volumes": volumes,
            "shelves": shelves,
            "statuses": statuses,
            "is_issued": is_issued,
            "error_message": error_message,
            "form_data": form_data,
            "from_page": from_page,
        }
    )

@role_required("Admin", "Librarian")
def book_copy_delete(request, copy_id):

    copy = get_object_or_404(
    BookCopy.objects.select_related(
        "volume__book",
        "shelf__location",
    ),
    id=copy_id
)
    from_page = request.GET.get(
    "from",
    request.POST.get("from", "")
)

    # The real FK (loans.copy_id -> book_copies.id) is NO ACTION, so ANY
    # loan record referencing this copy — active or already returned —
    # blocks the delete at the database level, not just active ones.
    loan_history_exists = Loan.objects.filter(
        copy_id=copy.id
    ).exists()

    if request.method == "POST":

        if loan_history_exists:

            return render(
                request,
                "library/book_copy_delete.html",
                {
                    "copy": copy,
                    "loan_history_exists": True,
                    "from_page": from_page,
                }
            )

        deleted_copy_id = copy.id
        deleted_copy_code = copy.copy_code

        copy.delete()

        cache.delete(BOOK_COPY_CACHE_KEY)
        cache.delete(LOAN_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="BookCopy",
            entity_id=deleted_copy_id,
            description=(
                f"{deleted_copy_code} deleted"
            ),
        )

        if from_page == "shelf":

            return redirect(
        "shelf_detail",
        shelf_id=copy.shelf_id
    )

        if from_page == "volume":

            return redirect(
                "book_volume_detail",
                volume_id=copy.volume_id
            )

        return redirect(
    "book_copy_list"
)

    return render(
        request,
        "library/book_copy_delete.html",
        {
            "copy": copy,
            "loan_history_exists": loan_history_exists,
            "from_page": from_page,
        }
    )


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
            due_date__lt=timezone.now().date()
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

    # Order loans
    loans = loans_query.order_by(
        "-issue_date",
        "-id"
    )

    paginator = Paginator(loans, PAGE_SIZE)
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

    # Borrowers for dropdown
    borrowers = Borrower.objects.all().order_by(
        "name"
    )

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
            "borrowers": borrowers,
        }
    )

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

    return render(
        request,
        "library/loan_detail.html",
        {
            "loan": loan,
        }
    )

def loan_add(request):

    copies = BookCopy.objects.select_related(
        "volume__book"
    ).filter(
        status="Available"
    ).order_by(
        "copy_code"
    )

    borrowers = Borrower.objects.filter(
        is_active=True
    ).order_by(
        "name"
    )

    users = User.objects.all().order_by(
        "full_name"
    )

    error_message = ""

    if request.method == "POST":

        copy_id = request.POST.get("copy", "").strip()
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

        issued_by_id = request.POST.get(
            "issued_by",
            ""
        ).strip()

        notes = request.POST.get(
            "notes",
            ""
        ).strip()

        if not all([
            copy_id,
            borrower_id,
            issue_date,
            due_date,
        ]):

            error_message = (
                "Please fill in all required fields."
            )

        elif due_date < issue_date:

            error_message = (
                "Due date cannot be earlier than issue date."
            )

        else:

            try:

                copy = BookCopy.objects.get(
                    id=copy_id
                )

                if copy.status != "Available":

                    error_message = (
                        "This book copy is no longer available."
                    )

                else:

                    loan = Loan.objects.create(
                        copy_id=copy_id,
                        borrower_id=borrower_id,
                        issue_date=issue_date,
                        due_date=due_date,
                        issued_by_id=issued_by_id or None,
                        notes=notes or None,
                    )

                    copy.status = "Issued"
                    copy.save()

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
                        action="ISSUE",
                        entity_type="BookCopy",
                        entity_id=loan.copy_id,
                        description=(
                            f"{loan.copy.copy_code} "
                            f"{loan.borrower.name} کو "
                            f"issue کی گئی"
                        ),
                    )

                    return redirect(
                        "loan_list"
                    )

            except (BookCopy.DoesNotExist, ValueError):

                error_message = (
                    "Selected book copy does not exist."
                )

    today = timezone.now().date()

    return render(
        request,
        "library/loan_add.html",
        {
            "copies": copies,
            "borrowers": borrowers,
            "users": users,
            "error_message": error_message,
            "default_issue_date": today.isoformat(),
            "default_due_date": (
                today + timedelta(days=DEFAULT_LOAN_PERIOD_DAYS)
            ).isoformat(),
        }
    )


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

    users = User.objects.filter(
        is_active=True
    )

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    error_message = ""

    if request.method == "POST" and loan.return_date is None:

        return_date = request.POST.get(
            "return_date"
        )

        returned_to_id = request.POST.get(
            "returned_to"
        )

        notes = request.POST.get(
            "notes",
            ""
        ).strip()

        parsed_return_date = None

        if not return_date:

            error_message = "Return date is required."

        else:

            try:
                parsed_return_date = date.fromisoformat(return_date)
            except ValueError:
                parsed_return_date = None

            if parsed_return_date is None:

                error_message = "Please enter a valid return date."

            elif parsed_return_date < loan.issue_date:

                error_message = (
                    "Return date cannot be earlier than issue date."
                )

        if not error_message:

            loan.return_date = parsed_return_date

            loan.returned_to_id = (
                returned_to_id or None
            )

            if notes:
                loan.notes = notes

            loan.save()

            loan.copy.status = "Available"

            loan.copy.save()

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
                user=loan.returned_to,
                action="RETURN",
                entity_type="BookCopy",
                entity_id=loan.copy_id,
                description=(
                    f"{loan.copy.copy_code} "
                    f"{loan.borrower.name} سے واپس وصول کی گئی"
                ),
            )

            return redirect(
                safe_redirect_target(request, "loan_list")
            )

    return render(
        request,
        "library/loan_return.html",
        {
            "loan": loan,
            "users": users,
            "error_message": error_message,
            "next_url": next_url,
        }
    )
@role_required("Admin", "Librarian")
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

    borrowers_query = Borrower.objects.all()

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

    if search or borrower_type or active_status:

        borrowers = list(
            borrowers_query.order_by(
                "name"
            )
        )

    else:

        borrowers = cache.get(
            BORROWER_CACHE_KEY
        )

        if borrowers is None:

            borrowers = list(
                borrowers_query.order_by(
                    "name"
                )
            )

            cache.set(
                BORROWER_CACHE_KEY,
                borrowers,
                timeout=300
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

    paginator = Paginator(borrowers, PAGE_SIZE)
    borrowers = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/borrower_list.html",
        {
            "borrowers": borrowers,
            "search": search,
            "borrower_type": borrower_type,
            "active_status": active_status,
            "borrower_types": borrower_types,
        }
    )

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

def borrower_detail(request, borrower_id):

    borrower = get_object_or_404(
        Borrower,
        id=borrower_id
    )

    loans = Loan.objects.filter(
        borrower=borrower
    ).select_related(
        "copy",
        "copy__volume",
        "copy__volume__book"
    ).order_by(
        "-issue_date"
    )

    active_loans = loans.filter(
        return_date__isnull=True
    )

    return render(
        request,
        "library/borrower_detail.html",
        {
            "borrower": borrower,
            "loans": loans,
            "active_loans": active_loans,
        }
    )

def borrower_toggle_active(request, borrower_id):

    borrower = get_object_or_404(Borrower, id=borrower_id)

    if request.method == "POST":

        borrower.is_active = not borrower.is_active
        borrower.save()

        cache.delete(BORROWER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
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

@role_required("Admin", "Librarian")
def borrower_delete(request, borrower_id):

    borrower = get_object_or_404(
        Borrower,
        id=borrower_id
    )

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    # The real FK (loans.borrower_id -> borrowers.id) is NO ACTION, so ANY
    # loan record referencing this borrower — active or already returned —
    # blocks the delete at the database level, not just active ones.
    loan_history_exists = Loan.objects.filter(
        borrower_id=borrower.id
    ).exists()

    if request.method == "POST":

        if loan_history_exists:

            return render(
                request,
                "library/borrower_delete.html",
                {
                    "borrower": borrower,
                    "loan_history_exists": True,
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
            "next_url": next_url,
        }
    )


@role_required("Admin")
def user_list(request):

    search = request.GET.get("search", "").strip()
    role = request.GET.get("role", "").strip()
    active_status = request.GET.get("status", "").strip()

    if search or role or active_status:
        users = User.objects.all()

        if search:
            users = users.filter(
                models.Q(username__icontains=search)
                | models.Q(full_name__icontains=search)
                | models.Q(role__icontains=search)
            )

        if role:
            users = users.filter(role=role)

        if active_status == "active":
            users = users.filter(is_active=True)
        elif active_status == "inactive":
            users = users.filter(is_active=False)

        users = list(users)

    else:
        users = cache.get(USER_CACHE_KEY)

        if users is None:
            users = list(
                User.objects.all()
            )

            cache.set(
                USER_CACHE_KEY,
                users,
                timeout=300
            )

    paginator = Paginator(users, PAGE_SIZE)
    users = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/user_list.html",
        {
            "role": role,
            "active_status": active_status,
            "roles": User.ROLE_CHOICES,
            "users": users,
            "search": search,
        }
    )


@role_required("Admin")
def user_add(request):

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        full_name = request.POST.get("full_name", "").strip()
        password_hash = request.POST.get("password_hash", "").strip()
        role = request.POST.get("role", "").strip()

        if username and full_name and password_hash and role in USER_ROLES:
            user = User.objects.create(
                username=username,
                full_name=full_name,
                password_hash=password_hash,
                role=role,
                is_active=True,
                created_at=timezone.now(),
            )

            cache.delete(USER_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=None,
                action="CREATE",
                entity_type="User",
                entity_id=user.id,
                description=f"{user.username} شامل کیا گیا",
            )

            return redirect("user_list")

    return render(
        request,
        "library/user_add.html"
    )


@role_required("Admin")
def user_toggle_active(request, user_id):

    user = get_object_or_404(User, id=user_id)

    if request.method == "POST":

        user.is_active = not user.is_active
        user.save()

        cache.delete(USER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="UPDATE",
            entity_type="User",
            entity_id=user.id,
            description=(
                f"{user.username} "
                f"{'activated' if user.is_active else 'deactivated'}"
            ),
        )

    fallback = reverse("user_list")
    return redirect(safe_redirect_target(request, fallback))


@role_required("Admin")
def user_edit(request, user_id):

    target_user = get_object_or_404(
        User,
        id=user_id
    )

    error = None

    if request.method == "POST":

        username = request.POST.get(
            "username",
            ""
        ).strip()

        full_name = request.POST.get(
            "full_name",
            ""
        ).strip()

        new_password = request.POST.get(
            "new_password",
            ""
        ).strip()

        role = request.POST.get(
            "role",
            ""
        ).strip()

        if not (username and full_name and role in USER_ROLES):

            error = "Please fill in all required fields."

        elif new_password and len(new_password) < 8:

            error = "New password must be at least 8 characters."

        else:

            target_user.username = username
            target_user.full_name = full_name

            if new_password:

                target_user.set_password(new_password)

            target_user.role = role

            target_user.is_active = (
                request.POST.get("is_active")
                == "on"
            )

            target_user.save()

            cache.delete(
                USER_CACHE_KEY
            )

            cache.delete(
                DASHBOARD_CACHE_KEY
            )

            create_activity_log(
                user=None,
                action="UPDATE",
                entity_type="User",
                entity_id=target_user.id,
                description=f"{target_user.username} updated",
            )

            return redirect(
                "user_list"
            )

    return render(
        request,
        "library/user_edit.html",
        {
            "target_user": target_user,
            "error": error,
        }
    )


@role_required("Admin")
def user_delete(request, user_id):

    target_user = get_object_or_404(User, id=user_id)

    # users.id is referenced by loans.issued_by/returned_to and
    # activity_logs.user_id, all NO ACTION FKs — deleting a user who has
    # ever issued/returned a loan (or been logged doing something) would
    # otherwise crash with an unhandled IntegrityError.
    has_related_records = (
        Loan.objects.filter(
            models.Q(issued_by_id=target_user.id)
            | models.Q(returned_to_id=target_user.id)
        ).exists()
        or ActivityLog.objects.filter(user_id=target_user.id).exists()
    )

    if request.method == "POST":

        if has_related_records:

            return render(
                request,
                "library/user_delete.html",
                {
                    "target_user": target_user,
                    "has_related_records": True,
                }
            )

        deleted_user_id = target_user.id
        deleted_username = target_user.username

        target_user.delete()

        cache.delete(USER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="User",
            entity_id=deleted_user_id,
            description=f"{deleted_username} deleted",
        )

        return redirect("user_list")

    return render(
        request,
        "library/user_delete.html",
        {
            "target_user": target_user,
            "has_related_records": has_related_records,
        }
    )

def activity_log_list(request):

    search = request.GET.get("search", "").strip()
    user_id = request.GET.get("user", "").strip()
    action = request.GET.get("action", "").strip()
    entity_type = request.GET.get("entity_type", "").strip()
    date = request.GET.get("date", "").strip()

    logs_query = ActivityLog.objects.select_related(
        "user"
    )

    if search:

        query = (
            models.Q(action__icontains=search)
            | models.Q(entity_type__icontains=search)
            | models.Q(description__icontains=search)
            | models.Q(user__username__icontains=search)
            | models.Q(user__full_name__icontains=search)
        )

        if search.isdigit():
            query |= models.Q(
                entity_id=int(search)
            )

        logs_query = logs_query.filter(query)

    if user_id:
        logs_query = logs_query.filter(
            user_id=user_id
        )

    if action:
        logs_query = logs_query.filter(
            action=action
        )

    if entity_type:
        logs_query = logs_query.filter(
            entity_type=entity_type
        )

    if date:
        logs_query = logs_query.filter(
            created_at__date=date
        )

    logs = logs_query.order_by(
        "-created_at"
    )

    paginator = Paginator(logs, PAGE_SIZE)
    logs = paginator.get_page(request.GET.get("page"))

    users = User.objects.all().order_by(
        "full_name"
    )

    actions = [
        "CREATE",
        "UPDATE",
        "DELETE",
        "ISSUE",
        "RETURN",
    ]

    entity_types = [
        "Book",
        "Author",
        "Category",
        "Publisher",
        "BookVolume",
        "BookContent",
        "BookCopy",
        "Borrower",
        "Loan",
        "Location",
        "Shelf",
        "User",
    ]

    return render(
        request,
        "library/activity_log_list.html",
        {
            "logs": logs,
            "search": search,
            "user_id": user_id,
            "action": action,
            "entity_type": entity_type,
            "date": date,
            "users": users,
            "actions": actions,
            "entity_types": entity_types,
        }
    )


def create_activity_log(
    user=None,
    action="",
    entity_type=None,
    entity_id=None,
    description=None,
):
    ActivityLog.objects.create(
        user=user,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        description=description,
        created_at=timezone.now(),
    )

    cache.delete(ACTIVITY_LOG_CACHE_KEY)

def library_home(request):

    dashboard_stats = cache.get(
        DASHBOARD_CACHE_KEY
    )

    if dashboard_stats is None:

        today = timezone.now().date()

        dashboard_stats = {
            "total_books": Book.objects.count(),
            "total_authors": Author.objects.count(),
            "total_categories": Category.objects.count(),
            "total_publishers": Publisher.objects.count(),

            "total_book_copies": BookCopy.objects.count(),

            "available_copies": BookCopy.objects.filter(
                status="Available"
            ).count(),

            "issued_copies": BookCopy.objects.filter(
                status="Issued"
            ).count(),

            "total_borrowers": Borrower.objects.count(),

            "active_borrowers": Borrower.objects.filter(
                is_active=True
            ).count(),

            "active_loans": Loan.objects.filter(
                return_date__isnull=True
            ).count(),

            "overdue_loans": Loan.objects.filter(
                return_date__isnull=True,
                due_date__lt=today
            ).count(),
        }

        cache.set(
            DASHBOARD_CACHE_KEY,
            dashboard_stats,
            timeout=300
        )

    recent_loans = Loan.objects.select_related(
        "copy__volume__book",
        "borrower",
        "issued_by",
    ).order_by(
        "-issue_date"
    )[:5]

    recent_logs = ActivityLog.objects.select_related(
        "user"
    ).order_by(
        "-created_at"
    )[:5]

    dashboard_stats["recent_loans"] = recent_loans
    dashboard_stats["recent_logs"] = recent_logs

    return render(
        request,
        "library/dashboard.html",
        dashboard_stats
    )