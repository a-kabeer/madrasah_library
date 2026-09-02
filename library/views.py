from datetime import date, timedelta
import json
import os
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.template.loader import render_to_string
from django.urls import reverse
from django.core.cache import cache
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.db import IntegrityError, models, transaction
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

# Copy codes. The prefix is the one already used by the copies in this
# library (LIB-000001 ...), so generated codes continue the existing
# numbering convention rather than introducing a second one.
COPY_CODE_PREFIX = "LIB-"
COPY_CODE_DIGITS = 6

# Ceilings for the guided Add Book form. They exist so a mistyped quantity
# cannot ask the database for thousands of rows in one request; they are far
# above anything a real acquisition would need.
MAX_VOLUMES = 100
MAX_COPIES_PER_VOLUME = 100
MAX_TOTAL_COPIES = 200

# Rows-per-page options offered by the book list's "Show" control.
PAGE_SIZE_CHOICES = (10, 25, 50, 100, 200)

# Sortable book-list columns: the `sort` parameter value -> the field to
# order by. Whitelisted deliberately — handing the raw parameter to
# order_by() would let a visitor traverse relations or trigger a FieldError.
#
# `id` doubles as the book number; the schema has no separate accession
# column (accession codes live per-copy on book_copies.copy_code) and no
# created-at column, so neither is offered here.
BOOK_SORT_FIELDS = {
    "id": "id",
    "title": "title",
    "author": "author__name",
    "category": "category__name",
    "publisher": "publisher__name",
}

BOOK_SORT_DEFAULT = "title"

# Book-list browsing modes. Each one decides which filter control is shown;
# the queryset itself applies whatever filter parameters are present.
BOOK_LIST_MODES = ("all", "author", "category")
BOOK_LIST_MODE_DEFAULT = "all"

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

# Query parameters that select a response *shape* rather than filter data:
# each one asks a view for a fragment instead of a page. They are stripped
# from any link a view builds (see `query_with`).
INTERNAL_PARAMS = (
    "partial",
    "modal",
    "combobox",
    "options",
    "refresh",
)

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


# Where an activity-log entry points, by the entity_type recorded with it.
# `User` is absent deliberately: it has no detail page. So is
# OrganizationSettings, whose page is Admin-only — linking it would hand
# other roles a 403.
ACTIVITY_LOG_DETAIL_ROUTES = {
    "Author": "author_detail",
    "Book": "book_detail",
    "BookContent": "book_content_detail",
    "BookCopy": "book_copy_detail",
    "BookVolume": "book_volume_detail",
    "Borrower": "borrower_detail",
    "Category": "category_detail",
    "Loan": "loan_detail",
    "Location": "location_detail",
    "Publisher": "publisher_detail",
    "Shelf": "shelf_detail",
}


def activity_log_target(log):
    """URL of the record a log entry refers to, or "" if there isn't one.

    Returning "" is what tells the template to render a plain row rather
    than a link. DELETE entries always land here: the record they describe
    is gone, so its detail page would only 404.
    """

    if not log.entity_type or not log.entity_id:
        return ""

    if log.action == "DELETE":
        return ""

    route = ACTIVITY_LOG_DETAIL_ROUTES.get(log.entity_type)

    if not route:
        return ""

    return reverse(route, args=[log.entity_id])


def query_with(request, **overrides):
    """The current query string with parameters replaced, or dropped on None.

    Used to build sort, paging and rows-per-page links that carry the rest
    of the table's state along, so any view of the list stays shareable as
    a URL.
    """

    params = request.GET.copy()

    # These only ever say *how* a request was made, never what it asks for.
    # Dropping them keeps every generated link the plain, shareable URL, and
    # stops `partial=1` from leaking into the address bar once the book list
    # starts answering its own links with fragments.
    for key in INTERNAL_PARAMS:
        params.pop(key, None)

    for key, value in overrides.items():

        if value is None:
            params.pop(key, None)

        else:
            params[key] = value

    return params.urlencode()


def resolve_page_size(request, default=PAGE_SIZE):
    """The requested rows-per-page, if it is one of the offered options."""

    try:
        requested = int(request.GET.get("page_size", ""))

    except ValueError:
        return default

    return requested if requested in PAGE_SIZE_CHOICES else default


def resolve_sort(request, allowed, default):
    """Validated (sort key, direction) for a list table."""

    sort = request.GET.get("sort", "")

    if sort not in allowed:
        sort = default

    direction = request.GET.get("direction", "")

    if direction not in ("asc", "desc"):
        direction = "asc"

    return sort, direction


def sort_ordering(allowed, sort, direction):
    """order_by() arguments for an already-validated sort key and direction.

    Two details matter for a paginated table:

    * Nullable columns (category, publisher) keep their blank rows at the
      end whichever way the sort runs, rather than flipping to the top.
    * `id` is appended as a tiebreaker. Rows that compare equal otherwise
      have no guaranteed order, which lets the same book appear on two
      pages while another is skipped entirely.
    """

    field = models.F(allowed[sort])

    if direction == "desc":
        primary = field.desc(nulls_last=True)

    else:
        primary = field.asc(nulls_last=True)

    if allowed[sort] == "id":
        return [primary]

    return [primary, "id"]


def sortable_columns(request, columns, allowed, sort, direction):
    """Header definitions with a sort link and the current state on each.

    `columns` is a list of (key, label) pairs; a key of None marks a column
    that cannot be sorted (the cover thumbnail).
    """

    prepared = []

    for key, label in columns:

        column = {
            "key": key,
            "label": label,
            "active": False,
            "direction": "",
            "url": "",
        }

        if key in allowed:

            active = key == sort

            # Clicking the active column flips it; a new column starts
            # ascending, which is the least surprising default.
            next_direction = (
                "desc"
                if active and direction == "asc"
                else "asc"
            )

            column["active"] = active
            column["direction"] = direction if active else ""
            column["url"] = "?" + query_with(
                request,
                sort=key,
                direction=next_direction,
                page=None,
            )

        prepared.append(column)

    return prepared


def page_size_options(current):
    """The rows-per-page choices, and which one is in force.

    Just the numbers: the control is a <select> that sends its own value to
    `page_size_hx_url`, so each option needs no link of its own.
    """

    return [
        {
            "value": size,
            "active": size == current,
        }
        for size in PAGE_SIZE_CHOICES
    ]


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


def numeric_param(request, name):
    """A GET parameter, kept only if it is a plain positive integer.

    Filtering an integer column on a non-numeric string raises ValueError
    and surfaces as a 500, so anything else is discarded and read as "no
    filter" instead.
    """

    value = request.GET.get(name, "").strip()

    return value if value.isdigit() else ""


def is_modal_request(request):
    """True for the fragment traffic behind a details modal.

    Same belt-and-braces shape as `is_combobox_request`: the header alone
    would make one URL return two different bodies, which a cache could then
    mix up.
    """

    return (
        request.headers.get("HX-Request") == "true"
        and request.GET.get("modal")
    )


# The two pieces of the book list that can be replaced on their own, by the
# `partial` parameter that asks for them.
#
#   results — a search, a filter, a sort, a page or a page-size change. Only
#             the table and its trimmings move; the control the user is
#             typing in stays exactly as it is.
#   browser — a change of browsing mode. That swaps the mode pills and the
#             filter control too, since which control is shown is the whole
#             point of the mode.
BOOK_LIST_FRAGMENTS = {
    "results": "library/partials/book_list_results.html",
    "browser": "library/partials/book_list_browser.html",
}


def book_list_fragment(request):
    """The book-list fragment this request asks for, or "" for the page.

    Gated on the HX-Request header as well as the parameter — the same
    belt-and-braces as `is_modal_request`. Without the parameter one URL
    would return several different bodies, which a cache could then mix up.
    """

    if request.headers.get("HX-Request") != "true":
        return ""

    return BOOK_LIST_FRAGMENTS.get(request.GET.get("partial", ""), "")


def is_options_request(request):
    """True when only the <option> list of a dropdown is being asked for.

    Used by the Add Book dialog's Location and Shelf selects, which reload
    their own options instead of the page. Same shape as the flags beside
    it: header plus an explicit parameter, so one URL never returns two
    different bodies.
    """

    return (
        request.headers.get("HX-Request") == "true"
        and (
            request.GET.get("options")
            or request.POST.get("options")
        )
    )


def is_form_modal_request(request):
    """True for the Add / Edit / Delete Book forms served into a dialog.

    Separate from `is_modal_request`, which serves the read-only details
    popup: the two put different things in different dialogs, and a request
    asks for exactly one of them. Gated on the HX-Request header as well as
    the parameter, the same belt-and-braces as its neighbours — without the
    parameter one URL would return two different bodies, which a cache
    could then mix up.
    """

    return (
        request.headers.get("HX-Request") == "true"
        and request.GET.get("modal")
    )


def allocate_copy_code(copy_id):
    """The generated code for a copy, e.g. "LIB-000042" for copy 42.

    The number is the copy's own primary key. `book_copies.id` comes from a
    Postgres IDENTITY sequence, which never goes backwards and never hands
    out the same value twice, so:

      * codes are unique without having to search for a free one,
      * deleting a copy does not free its code for the next one,
      * and nothing in the code comes from the book's metadata, so editing
        a title leaves the label on the physical book still correct.

    Gaps in the numbering are expected and harmless: it is an accession
    number, not a count.
    """

    base = "%s%0*d" % (COPY_CODE_PREFIX, COPY_CODE_DIGITS, copy_id)

    if not BookCopy.objects.filter(copy_code__iexact=base).exists():
        return base

    # Only reachable when someone typed this exact code in by hand earlier.
    # Suffixing keeps the code clear of future ids, which picking a
    # different number would not.
    for suffix in range(2, 100):

        candidate = "%s-%d" % (base, suffix)

        if not BookCopy.objects.filter(copy_code__iexact=candidate).exists():
            return candidate

    raise IntegrityError("could not allocate a copy code near %s" % base)


def create_book_copies(volume, shelf, quantity, codes=None):
    """Create `quantity` copies of `volume` on `shelf`, and return them.

    Uses `objects.create()` per row rather than `bulk_create`, so whatever
    the model does on save — now or later — still happens. Callers run this
    inside a transaction: a generated code is written by a second statement,
    because it contains the row's own id.
    """

    created = []

    for index in range(quantity):

        if codes is not None:

            copy = BookCopy.objects.create(
                volume=volume,
                shelf=shelf,
                copy_code=codes[index],
                status=BookCopy.STATUS_AVAILABLE,
            )

        else:

            # A reservation, only ever visible inside this transaction: the
            # real code needs the id the insert is about to allocate, and
            # the column is both NOT NULL and UNIQUE.
            copy = BookCopy.objects.create(
                volume=volume,
                shelf=shelf,
                copy_code="pending-%s" % uuid4().hex,
                status=BookCopy.STATUS_AVAILABLE,
            )

            copy.copy_code = allocate_copy_code(copy.id)
            copy.save(update_fields=["copy_code"])

        created.append(copy)

    return created


def read_volume_rows(request):
    """The volumes the Add Book form is asking for, and any complaint.

    Returns `(rows, raw, error)`. `rows` is what to create, already
    validated; `raw` is what the user typed, so a rejected form can be
    handed straight back with their work still in it.

    A single-volume book still gets one BookVolume row, because a copy can
    only hang off a volume — but the librarian is never shown it. Its title
    is left empty rather than copied from the book, so there is nothing to
    go stale when the title is edited; `BookVolume.__str__` already renders
    that case from the book's own title.
    """

    mode = request.POST.get("volume_mode", "single")

    if mode not in ("single", "multiple"):
        mode = "single"

    numbers = request.POST.getlist("volume_number")
    titles = request.POST.getlist("volume_title")
    quantities = request.POST.getlist("copy_qty")

    # One entry per row the form submitted, so a row and its copy count
    # stay together even where the user left part of it blank.
    raw_rows = [
        {
            "number": numbers[index] if index < len(numbers) else "",
            "title": titles[index] if index < len(titles) else "",
            "quantity": quantities[index] if index < len(quantities) else "",
        }
        for index in range(max(len(numbers), len(titles), len(quantities)))
    ]

    raw = {"mode": mode, "rows": raw_rows}

    if mode == "single":

        rows = [{
            "number": 1,
            "title": "",
            "quantity": quantities[0] if quantities else "",
        }]

        return rows, raw, ""

    rows = []
    seen = set()

    for entry in raw_rows:

        number = entry["number"].strip()
        title = entry["title"].strip()

        # A row the user added and then left completely blank is dropped
        # rather than rejected: removing it is what they meant.
        if not number and not title:
            continue

        if not number.isdigit() or int(number) < 1:
            return [], raw, "Every volume needs a volume number of 1 or more."

        number = int(number)

        if number in seen:
            return [], raw, "Volume number %d is listed twice." % number

        seen.add(number)

        rows.append({
            "number": number,
            "title": title,
            "quantity": entry["quantity"],
        })

    if not rows:
        return [], raw, "Add at least one volume, or choose Single Volume."

    if len(rows) > MAX_VOLUMES:
        return (
            [],
            raw,
            "A book can be given at most %d volumes here." % MAX_VOLUMES,
        )

    return rows, raw, ""


def read_copy_plan(request, rows):
    """How many copies to create per volume, where, and with which codes.

    Returns `(plan, raw, error)`. `plan` is None when the librarian chose to
    add copies later, which is allowed and lossless: copies can be added
    from the volume's own page whenever the shelf is known.
    """

    mode = request.POST.get("copies_mode", "skip")

    if mode not in ("skip", "add"):
        mode = "skip"

    code_mode = request.POST.get("code_mode", "auto")

    if code_mode not in ("auto", "manual"):
        code_mode = "auto"

    location_id = request.POST.get("location", "").strip()
    shelf_id = request.POST.get("shelf", "").strip()

    manual_codes = [
        code.strip()
        for code in request.POST.getlist("copy_code")
    ]

    raw = {
        "mode": mode,
        "code_mode": code_mode,
        "location": location_id,
        "shelf": shelf_id,
        "codes": manual_codes,
    }

    if mode == "skip":
        return None, raw, ""

    quantities = []
    total = 0

    for row in rows:

        value = str(row.get("quantity", "")).strip() or "0"

        if not value.isdigit():
            return None, raw, "Number of copies must be a whole number."

        count = int(value)

        if count > MAX_COPIES_PER_VOLUME:
            return (
                None,
                raw,
                "At most %d copies per volume can be added at once."
                % MAX_COPIES_PER_VOLUME,
            )

        quantities.append(count)
        total += count

    if total < 1:
        return (
            None,
            raw,
            "Enter how many copies to add, or choose to add them later.",
        )

    if total > MAX_TOTAL_COPIES:
        return (
            None,
            raw,
            "At most %d copies can be added in one go." % MAX_TOTAL_COPIES,
        )

    if not location_id.isdigit() or not shelf_id.isdigit():
        return None, raw, "Choose a location and a shelf for the copies."

    # The shelf has to actually be on the chosen location. The dropdowns
    # only offer matching pairs, but that is the browser's word for it.
    shelf = Shelf.objects.filter(
        id=shelf_id,
        location_id=location_id,
    ).select_related("location").first()

    if shelf is None:
        return None, raw, "That shelf is not in the location you chose."

    codes = None

    if code_mode == "manual":

        codes = [code for code in manual_codes if code]

        if len(codes) != total:
            return (
                None,
                raw,
                "Enter a copy code for each of the %d copies." % total,
            )

        folded = [code.casefold() for code in codes]

        if len(set(folded)) != len(folded):
            return (
                None,
                raw,
                "The copy codes you entered are not all different.",
            )

        # The rest of the app treats codes case-insensitively, so a
        # differently-cased match is still a duplicate.
        for code in codes:

            clash = BookCopy.objects.filter(
                copy_code__iexact=code
            ).values_list("copy_code", flat=True).first()

            if clash:
                return None, raw, "Copy code %s is already in use." % clash

    return (
        {"shelf": shelf, "quantities": quantities, "codes": codes},
        raw,
        "",
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

    # Quick-add from the Add Book dialog: answer with the refreshed option
    # list rather than a redirect, so the librarian carries on where they
    # were instead of losing the half-filled form.
    options = is_options_request(request)

    error = ""

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()

        # `locations.name` is UNIQUE, so a repeat would otherwise be an
        # IntegrityError. Treat it as "you meant this one", the same way
        # the searchable dropdowns handle a duplicate author.
        existing = (
            Location.objects.filter(name__iexact=name).first()
            if name
            else None
        )

        if not name:

            error = "Location name is required."

        elif existing is not None:

            if options:
                return location_options_response(existing)

            error = "A location with this name already exists."

        else:
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

            if options:
                return location_options_response(location)

            return redirect("location_list")

    if options:
        return location_options_response(None, error=error)

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

    # The Add Book dialog asks for just the options for one location, so
    # its Shelf dropdown never lists shelves from anywhere else. Same view,
    # same `?location=` filter that was already here.
    if is_options_request(request):

        return render(
            request,
            "library/partials/shelf_options.html",
            {
                "shelves": shelf_options_for(location_id),
                "selected_id": request.GET.get("selected", ""),
                "has_location": str(location_id).isdigit(),
            }
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

    # Quick-add from the Add Book dialog, as in `location_add`.
    options = is_options_request(request)

    locations = Location.objects.all()

    error = ""

    if request.method == "POST":
        location_id = request.POST.get("location")
        shelf_code = request.POST.get("shelf_code", "").strip()
        description = request.POST.get("description", "").strip()

        # A shelf only means anything inside a location, and the pair is
        # UNIQUE, so both are required and a repeat resolves to the shelf
        # that is already there. This is also what stops the dialog
        # creating a shelf under the wrong location.
        existing = None

        if location_id and str(location_id).isdigit() and shelf_code:
            existing = Shelf.objects.filter(
                location_id=location_id,
                shelf_code__iexact=shelf_code,
            ).first()

        if not location_id or not str(location_id).isdigit():

            error = "Choose a location for the shelf."

        elif not shelf_code:

            error = "Shelf code is required."

        elif existing is not None:

            if options:
                return shelf_options_response(location_id, existing)

            error = "That location already has a shelf with this code."

        else:
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

            if options:
                return shelf_options_response(location_id, shelf)

            return redirect(safe_redirect_target(request, "shelf_list"))

    if options:
        return shelf_options_response(
            request.POST.get("location", ""),
            None,
            error=error,
        )

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

    # `search` is the documented parameter; `title` is still honoured so
    # links made before the rename keep working.
    search = (
        request.GET.get("search")
        or request.GET.get("title")
        or ""
    ).strip()

    # Only digits are kept: these go straight into a filter on an integer
    # column, where a value like "abc" raises ValueError and returns a 500
    # rather than an empty list. Anything else is treated as no filter.
    author_id = numeric_param(request, "author")
    category_id = numeric_param(request, "category")
    publisher_id = numeric_param(request, "publisher")

    # Which browsing control to show. Only a UI concern: the filters below
    # are applied whenever their parameter is present, whatever the mode, so
    # `?publisher=` keeps working even though it no longer has a control.
    mode = request.GET.get("mode", "")

    if mode not in BOOK_LIST_MODES:
        mode = BOOK_LIST_MODE_DEFAULT

    books = Book.objects.select_related(
        "author",
        "category",
        "publisher",
    )

    if search:
        # Title only. Author and category are filtered through their own
        # modes, so folding them in here would make one control quietly
        # overlap the other two.
        books = books.filter(title__icontains=search)

    if author_id:
        books = books.filter(author_id=author_id)

    if category_id:
        books = books.filter(category_id=category_id)

    if publisher_id:
        books = books.filter(publisher_id=publisher_id)

    # Sorting and paging both happen in SQL — only one page of rows is ever
    # fetched, however large the catalogue grows.
    sort, direction = resolve_sort(
        request,
        BOOK_SORT_FIELDS,
        BOOK_SORT_DEFAULT,
    )

    books = books.order_by(
        *sort_ordering(BOOK_SORT_FIELDS, sort, direction)
    )

    page_size = resolve_page_size(request)

    paginator = Paginator(books, page_size)
    page = paginator.get_page(request.GET.get("page"))

    # Only these two columns are shown now, but BOOK_SORT_FIELDS still
    # accepts the others so older ?sort= links keep working.
    columns = sortable_columns(
        request,
        [
            ("title", "Book Name"),
            ("author", "Author Name"),
        ],
        BOOK_SORT_FIELDS,
        sort,
        direction,
    )

    # Switching mode drops the other modes' filters, so each mode starts
    # clean rather than inheriting a filter its control cannot show.
    mode_links = [
        {
            "key": key,
            "label": label,
            "icon": icon,
            "active": key == mode,
            "url": "?" + query_with(
                request,
                mode=key,
                search=None,
                author=None,
                category=None,
                page=None,
            ),
        }
        for key, label, icon in (
            ("all", "All Books", "bi-list-ul"),
            ("author", "By Author", "bi-person"),
            ("category", "By Category", "bi-tags"),
        )
    ]

    # One chip per active filter. Each mode shows only its own control, so
    # without these a filter set in another mode — or `?publisher=`, which
    # has no control at all — would narrow the results with nothing on
    # screen to explain it and no way to lift it.
    active_filters = [
        {
            "label": label,
            "value": value,
            "url": "?" + query_with(
                request,
                page=None,
                # `search` clears its legacy `title` alias too, or removing
                # the chip would appear to do nothing.
                **{param: None for param in params}
            ),
        }
        for label, params, value in (
            ("Search", ("search", "title"), search),
            ("Author", ("author",), selected_name(Author, author_id)),
            ("Category", ("category",), selected_name(Category, category_id)),
            ("Publisher", ("publisher",), selected_name(Publisher, publisher_id)),
        )
        if value
    ]

    context = {
        "books": page,
        "paginator": paginator,
        "search": search,
        "mode": mode,
        "mode_links": mode_links,
        "active_filters": active_filters,
        "author_id": author_id,
        "category_id": category_id,
        "publisher_id": publisher_id,
        # The filter dropdowns submit an id but display a name, so each
        # active filter needs its label to stay filled in on reload.
        "author_name": selected_name(Author, author_id),
        "category_name": selected_name(Category, category_id),
        "publisher_name": selected_name(Publisher, publisher_id),
        "columns": columns,
        "sort": sort,
        "direction": direction,
        "page_size": page_size,
        "page_size_options": page_size_options(page_size),
        # Where the rows-per-page control sends its value. Carries the
        # rest of the table's state, minus `page` so a size change
        # returns to page 1 and minus `page_size` so the size the
        # <select> sends is the only one in the query.
        "page_size_hx_url": "?" + query_with(
            request,
            mode=mode,
            page=None,
            page_size=None,
        ),
        # Everything except `page`, so paging links keep the rest of the
        # table's state.
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

    # Everything the page does to itself — searching, filtering, sorting,
    # paging, resizing, switching mode — asks for one of these fragments and
    # swaps it into place. Same context and the same templates the full page
    # includes, so there is no second code path to keep in step.
    fragment = book_list_fragment(request)

    if fragment:

        response = render(request, fragment, context)

        # The request URL carries `partial=`; the address bar should not.
        # This header overrides the container's hx-push-url, so what gets
        # pushed is the same plain URL a full navigation would produce.
        #
        # `refresh` marks a background reload — the list redrawing itself
        # after a book was added, edited or deleted. Nothing navigated, so
        # pushing would only add a history entry that goes nowhere.
        if not request.GET.get("refresh"):

            query = query_with(request)
            response["HX-Push-Url"] = (
                request.path + "?" + query if query else request.path
            )

        return response

    return render(request, "library/book_list.html", context)


def location_options_response(selected, error=""):
    """The Location dropdown's options, with `selected` chosen.

    Answers the dialog's quick-add. The whole option list is re-rendered
    rather than one <option> appended, so the new location lands in
    alphabetical order like the rest.
    """

    return render_options(
        "library/partials/location_options.html",
        {
            "locations": Location.objects.order_by("name"),
            "selected_id": str(selected.id) if selected else "",
            "error": error,
        },
        selected,
        "location",
    )


def shelf_options_response(location_id, selected, error=""):
    """The Shelf dropdown's options for one location, with `selected` chosen."""

    return render_options(
        "library/partials/shelf_options.html",
        {
            "shelves": shelf_options_for(location_id),
            "selected_id": str(selected.id) if selected else "",
            "has_location": str(location_id).isdigit(),
            "error": error,
        },
        selected,
        "shelf",
    )


def render_options(template, context, selected, entity):
    """Render an option list, and say in the headers how it went.

    The body is nothing but <option> elements, because it is swapped into a
    <select>, where the browser's parser moves or discards anything else.
    That is why the outcome travels as an event rather than as markup:

      quickAddDone   — created (or matched something already there). Closes
                       the quick-add box.
      quickAddFailed — nothing was created. Leaves the box open and shows
                       the reason.

    `entity` matters on success: a new location means the Shelf list now
    belongs to the wrong one and has to be reloaded, while a new shelf is
    already in the list this very response carries — reloading then would
    only discard the selection just made.
    """

    response = HttpResponse(render_to_string(template, context))

    if selected is not None:
        response["HX-Trigger"] = json.dumps({
            "quickAddDone": {
                "entity": entity,
                "id": selected.id,
                "label": str(selected),
            }
        })

    elif context.get("error"):
        response["HX-Trigger"] = json.dumps({
            "quickAddFailed": {
                "entity": entity,
                "message": context["error"],
            }
        })

    return response


def shelf_options_for(location_id):
    """The shelves on one location, or none when no location is chosen.

    Keeping this in one place is what stops the Shelf dropdown from ever
    listing every shelf in the building.
    """

    if not str(location_id).isdigit():
        return Shelf.objects.none()

    return Shelf.objects.filter(
        location_id=location_id
    ).order_by("shelf_code")


def book_saved_response(book, copies_made=0):
    """Tell the page a book was saved, so it can close up and refresh.

    "No content" plus an HX-Trigger: there is nothing to swap here, because
    what should change is the book list, which reloads its own results
    fragment on this event and so keeps the current search, filters,
    sorting and page exactly as they were.
    """

    response = HttpResponse(status=204)

    response["HX-Trigger"] = json.dumps({
        "bookSaved": {
            "id": book.id,
            "title": book.title,
            "copies": copies_made,
            "url": reverse("book_detail", args=[book.id]),
        }
    })

    return response


@role_required("Admin", "Librarian")
def book_add(request):
    """Add a book, and optionally its volumes and physical copies.

    One form and one POST covers the whole thing, so the librarian never
    walks the Book -> Volume -> Copy chain by hand. Everything it creates
    goes in inside a single transaction: if any part fails, no half-built
    book is left behind.

    The same view answers the full page and the dialog on the book list —
    same validation, same permissions, only the wrapper differs.
    """

    modal = is_form_modal_request(request)

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

    # What the volume / copy half of the form should show. Replaced by
    # whatever was submitted if the form comes back with an error.
    inventory = {
        "mode": "single",
        "rows": [],
        "copies_mode": "skip",
        "code_mode": "auto",
        "location": "",
        "shelf": "",
        "codes": [],
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

        volume_rows, volume_raw, volume_error = read_volume_rows(request)

        copy_plan, copy_raw, copy_error = read_copy_plan(
            request,
            volume_rows,
        )

        inventory = {
            "mode": volume_raw["mode"],
            "rows": volume_raw["rows"],
            "copies_mode": copy_raw["mode"],
            "code_mode": copy_raw["code_mode"],
            "location": copy_raw["location"],
            "shelf": copy_raw["shelf"],
            "codes": copy_raw["codes"],
        }

        if not title or not author_id:

            error = "Title and Author are required."

        elif cover_error:

            error = cover_error

        elif volume_error:

            error = volume_error

        elif copy_error:

            error = copy_error

        else:

            try:

                # Book, volumes and copies are one unit of work as far as
                # the librarian is concerned, so they are one unit of work
                # here too.
                with transaction.atomic():

                    book = Book.objects.create(
                        title=title,
                        author_id=author_id,
                        category_id=category_id or None,
                        publisher_id=publisher_id or None,
                        cover_image=cover_image or None,
                    )

                    volumes = [
                        BookVolume.objects.create(
                            book=book,
                            volume_number=row["number"],
                            title=row["title"],
                        )
                        for row in volume_rows
                    ]

                    copies_made = 0

                    if copy_plan:

                        # Manual codes arrive as one flat list in volume
                        # order, so each volume takes its own slice.
                        offset = 0

                        for volume, quantity in zip(
                            volumes,
                            copy_plan["quantities"],
                        ):

                            if not quantity:
                                continue

                            slice_ = None

                            if copy_plan["codes"] is not None:
                                slice_ = copy_plan["codes"][
                                    offset:offset + quantity
                                ]

                            create_book_copies(
                                volume,
                                copy_plan["shelf"],
                                quantity,
                                codes=slice_,
                            )

                            offset += quantity
                            copies_made += quantity

            except IntegrityError:

                # Nothing was written: the transaction rolled back. The
                # realistic cause is a copy code claimed by someone else
                # between validation and insert.
                error = (
                    "The book could not be saved because one of the copy "
                    "codes was just taken. Please try again."
                )

            else:

                cache.delete(BOOK_CACHE_KEY)
                cache.delete(DASHBOARD_CACHE_KEY)

                if volume_rows:
                    cache.delete(BOOK_VOLUME_CACHE_KEY)

                if copies_made:
                    cache.delete(BOOK_COPY_CACHE_KEY)

                create_activity_log(
                    user=None,
                    action="CREATE",
                    entity_type="Book",
                    entity_id=book.id,
                    description=f"{book.title} شامل کی گئی",
                )

                for volume in volumes:
                    create_activity_log(
                        user=None,
                        action="CREATE",
                        entity_type="BookVolume",
                        entity_id=volume.id,
                        description=(
                            f"{book.title} "
                            f"(Volume {volume.volume_number}) شامل کی گئی"
                        ),
                    )

                if copies_made:
                    create_activity_log(
                        user=None,
                        action="CREATE",
                        entity_type="Book",
                        entity_id=book.id,
                        description=(
                            f"{copies_made} copies of {book.title} "
                            f"added to {copy_plan['shelf']}"
                        ),
                    )

                if modal:
                    return book_saved_response(book, copies_made)

                return redirect("book_list")

    context = {
        "error": error,
        "form_data": form_data,
        "inventory": inventory,
        "locations": Location.objects.order_by("name"),
        "shelves": shelf_options_for(inventory["location"]),
        "max_copies_per_volume": MAX_COPIES_PER_VOLUME,
        "max_total_copies": MAX_TOTAL_COPIES,
    }

    if modal:
        return render(
            request,
            "library/partials/book_add_modal.html",
            context,
        )

    return render(
        request,
        "library/book_add.html",
        context,
    )


@role_required("Admin", "Librarian")
def book_edit(request, book_id):
    """Change a book's bibliographic details. Inventory is untouched.

    Nothing here reads or writes BookVolume, BookCopy, Shelf or Loan: a
    retitled book keeps the same volumes, the same copies, the same copy
    codes on the shelf and the same loan history. Volumes and copies are
    managed from their own pages.

    The same view answers the full page and the dialog on the book list.
    """

    modal = is_form_modal_request(request)

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

            if modal:
                return book_saved_response(book)

            if from_page == "detail":

                return redirect("book_detail", book_id=book.id)

            return redirect("book_list")

    context = {
        "book": book,
        "error": error,
        "form_data": form_data,
        "from_page": from_page,
    }

    if modal:
        return render(
            request,
            "library/partials/book_edit_modal.html",
            context,
        )

    return render(
        request,
        "library/book_edit.html",
        context,
    )


def book_deleted_response(title):
    """Tell the page a book is gone, so it can close up and refresh."""

    response = HttpResponse(status=204)

    response["HX-Trigger"] = json.dumps({
        "bookDeleted": {"title": title}
    })

    return response


def book_delete_blocker(book):
    """Why `book` cannot be deleted, or "" when it can be.

    Deleting a book cascades to its volumes in the database, but
    `book_copies.volume_id` is a NO ACTION foreign key, so a volume that
    still has copies aborts the whole statement. Reaching that point raises
    an IntegrityError and returns a 500, which is what this prevents.

    Copies are also where the history lives: `loans.copy_id` is NO ACTION
    too, so any loan — current or long returned — pins its copy in place.
    The message says which of those it is, because the way out differs.
    """

    copies = BookCopy.objects.filter(volume__book=book)

    copy_count = copies.count()

    if not copy_count:
        return ""

    issued = copies.filter(
        id__in=Loan.objects.filter(
            return_date__isnull=True
        ).values("copy_id")
    ).count()

    if issued:
        return (
            "This book cannot be deleted because "
            f"{issued} of its {copy_count} "
            f"cop{'y is' if issued == 1 else 'ies are'} currently issued. "
            "Take them back first."
        )

    if Loan.objects.filter(copy__volume__book=book).exists():
        return (
            "This book cannot be deleted because its copies have loan "
            "history, which would be lost. Delete the copies first if you "
            "really mean to remove it."
        )

    return (
        f"This book cannot be deleted because it still has {copy_count} "
        f"physical cop{'y' if copy_count == 1 else 'ies'}. "
        "Delete those first."
    )


@role_required("Admin", "Librarian")
def book_delete(request, book_id):

    modal = is_form_modal_request(request)

    book = get_object_or_404(Book, id=book_id)

    from_page = request.GET.get(
        "from",
        request.POST.get("from", "")
    )

    blocker = book_delete_blocker(book)

    volume_count = BookVolume.objects.filter(book=book).count()

    if request.method == "POST" and not blocker:

        deleted_book_id = book.id
        deleted_book_title = book.title

        # Volumes go with the book (books -> book_volumes cascades in the
        # database). `blocker` has already established there are no copies
        # hanging off them.
        with transaction.atomic():
            book.delete()

        cache.delete(BOOK_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        if volume_count:
            cache.delete(BOOK_VOLUME_CACHE_KEY)

        create_activity_log(
            user=None,
            action="DELETE",
            entity_type="Book",
            entity_id=deleted_book_id,
            description=f"{deleted_book_title} deleted",
        )

        if modal:
            return book_deleted_response(deleted_book_title)

        return redirect("book_list")

    context = {
        "book": book,
        "from_page": from_page,
        "blocker": blocker,
        "volume_count": volume_count,
    }

    if modal:
        return render(
            request,
            "library/partials/book_delete_modal.html",
            context,
        )

    return render(
        request,
        "library/book_delete.html",
        context,
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

    # The book list opens this in a modal rather than navigating, so serve a
    # fragment for that case — same view, same permissions.
    #
    # Gated on an explicit `modal` parameter as well as the header, matching
    # is_combobox_request(). Branching on the header alone would return two
    # different bodies for one URL with no Vary header, so a cache could
    # serve the bare fragment to a full-page navigation; the parameter makes
    # the two responses distinct URLs instead.
    if is_modal_request(request):

        # Every copy of every volume in one query: without the
        # select_related, rendering each copy's volume, shelf and location
        # would be three more queries per row.
        copies = BookCopy.objects.filter(
            volume__book=book
        ).select_related(
            "volume",
            "shelf__location",
        ).order_by(
            "volume__volume_number",
            "copy_code",
        )

        return render(
            request,
            "library/partials/book_detail_modal.html",
            {
                "book": book,
                "volumes": volumes,
                "copies": copies,
            }
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

    recent_logs = list(
        ActivityLog.objects.select_related(
            "user"
        ).order_by(
            "-created_at"
        )[:5]
    )

    for log in recent_logs:
        log.target_url = activity_log_target(log)

    dashboard_stats["recent_loans"] = recent_loans
    dashboard_stats["recent_logs"] = recent_logs

    return render(
        request,
        "library/dashboard.html",
        dashboard_stats
    )