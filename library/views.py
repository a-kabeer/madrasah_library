from collections import namedtuple
import csv
from datetime import date, timedelta
import io
import json
import os
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.template.loader import render_to_string
from django.urls import reverse
from django.core.cache import cache
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.db import (
    DatabaseError,
    IntegrityError,
    models,
    transaction,
)
from django.db.models import Q

from .models import (
    Author,
    InventoryScan,
    InventorySession,
    Reservation,
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
from django.utils.safestring import mark_safe

from . import barcode
from . import excel as book_excel
from . import history
from . import inventory
from . import policy
from . import reports
from . import reservations
from .context_processors import clear_branding_cache, is_main_nav_request
from .permissions import can_edit_library, role_required

PAGE_SIZE = 25

# Re-exported so the one place outside the web layer that needs it - the
# development seed command - keeps working. The rule itself lives in
# library/policy.py now, along with the three that used to live nowhere.
DEFAULT_LOAN_PERIOD_DAYS = policy.DEFAULT_LOAN_PERIOD_DAYS


class PolicyRefused(Exception):
    """A borrowing rule said no.

    Raised inside the transaction that would have written the loan, so the
    refusal and the rollback are the same event. Separate from
    IntegrityError, which reports a copy that slipped away rather than a
    rule that was applied - two different things to tell the librarian.
    """

# Copy codes. The prefix is the one already used by the copies in this
# library (LIB-000001 ...), so generated codes continue the existing
# numbering convention rather than introducing a second one.
COPY_CODE_PREFIX = "LIB-"
COPY_CODE_DIGITS = 6

# What a copy's state looks like to a librarian. The stored `status` column
# is left exactly as it is — it has its own CHECK constraint and the loan
# views maintain it — but "Overdue" and "Unshelved" are not in it and must
# not be, because they are already implied by the loan's due date and by
# `book_copies.shelf_id` being NULL. Deriving them means the screen can
# never disagree with the data.
COPY_STATE_LABELS = {
    "available": "Available",
    "issued": "Issued",
    "overdue": "Overdue",
    "unshelved": "Unshelved",
    "lost": "Lost",
    "damaged": "Damaged",
    "missing": "Missing",
    "transferred": "Transferred",
}

COPY_STATE_TONES = {
    "available": "success",
    "issued": "info",
    "overdue": "danger",
    "unshelved": "warning",
    "lost": "danger",
    "damaged": "warning",
    "missing": "danger",
    "transferred": "secondary",
}

# Offered by the copy list's Status filter, in the order they appear.
COPY_STATE_FILTERS = (
    "available",
    "issued",
    "overdue",
    "unshelved",
    "lost",
    "damaged",
    "missing",
    "transferred",
)

# Statuses that live in the column rather than being worked out.
COPY_STORED_STATES = ("lost", "damaged", "missing", "transferred")

# The order the book detail page tallies copy states in, and the three it
# shows even when the count is nought - "how many can I lend out" is the
# question the summary exists to answer, and a blank where "Available"
# should be answers it too.
COPY_STATE_ORDER = (
    "available",
    "issued",
    "overdue",
    "unshelved",
) + COPY_STORED_STATES

COPY_STATE_ALWAYS_SHOWN = ("available", "issued", "overdue")

# The panes on the book detail page. `tab` only marks one active.
BOOK_DETAIL_TABS = (
    "overview", "volumes", "copies", "contents", "loans", "reservations",
)

COPY_SORT_FIELDS = {
    "code": "copy_code",
    "book": "volume__book__title",
    "volume": "volume__volume_number",
    "location": "shelf__location__name",
    "shelf": "shelf__shelf_code",
    "status": "status",
}

COPY_SORT_DEFAULT = "code"

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

# What the book list's Availability filter offers, read off the copy counts
# the list already annotates. Derived from the same rules the copy list
# applies, so the two cannot disagree.
BOOK_AVAILABILITY_FILTERS = ("available", "issued", "none")

BOOK_AVAILABILITY_LABELS = {
    "available": "Available now",
    "issued": "Something out",
    "none": "No copies",
}

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
    "Reservation": "book_detail",
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

    Anything that is not a plain id answers "" rather than reaching the
    database. `id=` on an integer column raises ValueError for a value like
    "abc", which is a 500 for what is only a redisplayed label — and the
    value can come straight from a query string or a posted form.
    """

    if not pk or not str(pk).isdigit():
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

    A navigation wins over the parameter. The container these fragments are
    swapped into declares `partial` once, for the sort, filter and paging
    links inside it, and htmx hands that down to everything within -
    including a plain link to another page, now that the shell boosts those.
    Asking to replace the main-content region is asking for a page, so that
    is what such a request gets, whatever it inherited on the way.
    """

    if is_main_nav_request(request):
        return ""

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


def active_loan_copies():
    """Copy ids with a loan still out. A subquery, so nothing is fetched.

    `loans` has a partial unique index on copy_id WHERE return_date IS
    NULL, so there is at most one of these per copy — which is what lets
    the list treat "has an active loan" as a state rather than a count.
    """

    return Loan.objects.filter(
        return_date__isnull=True
    ).values("copy_id")


def overdue_loan_copies(today):
    """Copy ids whose active loan is past its due date."""

    return Loan.objects.filter(
        return_date__isnull=True,
        due_date__lt=today,
    ).values("copy_id")


def copy_state(copy, active_loan, today):
    """The one word to show for a copy, worked out from what is true.

    Order matters. A copy that is out with a borrower is described by that
    first, however its column reads; then anything the column records as
    wrong with the book itself; then whether it has been put on a shelf.

    A stored "Issued" with no matching loan falls through to Available or
    Unshelved rather than being repeated — the loan is the record of a
    book being out, and this way the screen cannot claim otherwise.
    """

    if active_loan is not None:

        if active_loan.due_date < today:
            return "overdue"

        return "issued"

    stored = (copy.status or "").casefold()

    if stored in COPY_STORED_STATES:
        return stored

    if copy.shelf_id is None:
        return "unshelved"

    return "available"


def describe_copies(copies, today=None):
    """Attach the loan, the state and how to show it, to each copy.

    One query for the loans of the copies given — not of every copy in the
    library — so a page of results costs the same whatever the catalogue
    grows to.
    """

    copies = list(copies)

    if not copies:
        return copies

    if today is None:
        today = timezone.now().date()

    loans = {
        loan.copy_id: loan
        for loan in Loan.objects.filter(
            copy_id__in=[copy.id for copy in copies],
            return_date__isnull=True,
        ).select_related("borrower")
    }

    for copy in copies:

        copy.active_loan = loans.get(copy.id)

        copy.days_overdue = (
            (today - copy.active_loan.due_date).days
            if copy.active_loan and copy.active_loan.due_date < today
            else 0
        )

        copy.state = copy_state(copy, copy.active_loan, today)
        copy.state_label = COPY_STATE_LABELS[copy.state]
        copy.state_tone = COPY_STATE_TONES[copy.state]

    return copies


def filter_copies_by_state(copies, state, today):
    """Narrow `copies` to one state, or leave it alone if that is not one.

    The derived states become the queries that would have produced them,
    so filtering and display agree. `?status=Available` links made before
    this existed still work: the value is matched case-insensitively.
    """

    if state == "issued":
        return copies.filter(id__in=active_loan_copies())

    if state == "overdue":
        return copies.filter(id__in=overdue_loan_copies(today))

    if state == "unshelved":
        return copies.filter(shelf__isnull=True)

    if state == "available":
        # Shelved as well as un-loaned: a copy with no shelf is shown as
        # Unshelved, and a filter that also returned it would disagree with
        # the row the librarian is looking at.
        return copies.filter(
            status=BookCopy.STATUS_AVAILABLE,
            shelf__isnull=False,
        ).exclude(
            id__in=active_loan_copies()
        )

    if state in COPY_STORED_STATES:
        return copies.filter(status__iexact=state)

    return copies


def copy_state_options(current):
    """The Status filter's choices, and which one is in force."""

    return [
        {
            "value": state,
            "label": COPY_STATE_LABELS[state],
            "active": state == current,
        }
        for state in COPY_STATE_FILTERS
    ]


def volume_label(volume):
    """"Volume 2" — or "Single volume" for a book that only has the one.

    A single-volume book gets its BookVolume because a copy has to hang off
    one, not because the librarian asked for volumes. Naming it "Volume 1"
    on every screen would put that plumbing back in front of them.
    """

    if volume is None:
        return ""

    if volume.volume_number == 1 and not volume.title:
        return "Single volume"

    label = f"Volume {volume.volume_number}"

    if volume.title:
        label += f" — {volume.title}"

    return label


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


def read_policy_form(request, branding):
    """Apply the posted borrowing rules to `branding`, or say what is wrong.

    Blank means "not configured", which is stored as NULL and read back as
    the default - so clearing a field is how a library goes back to the
    behaviour it had before it set one, and there is no magic number to
    remember.
    """

    def whole_number(field, label, low, high):

        raw = (request.POST.get(field) or "").strip()

        if not raw:
            return None, None

        if not raw.isdigit():
            return None, "%s has to be a whole number." % label

        value = int(raw)

        if not low <= value <= high:
            return None, "%s has to be between %d and %d." % (
                label, low, high
            )

        return value, None

    fields = (
        (
            "loan_period_days",
            "Loan period",
            policy.LOAN_PERIOD_MIN,
            policy.LOAN_PERIOD_MAX,
        ),
        (
            "max_active_loans",
            "Maximum books per borrower",
            policy.LIMIT_MIN,
            policy.LIMIT_MAX,
        ),
        (
            "max_renewals",
            "Renewal limit",
            policy.LIMIT_MIN,
            policy.LIMIT_MAX,
        ),
    )

    values = {}

    for field, label, low, high in fields:

        value, problem = whole_number(field, label, low, high)

        if problem:
            return problem

        values[field] = value

    for field, value in values.items():
        setattr(branding, field, value)

    branding.block_when_overdue = (
        request.POST.get("block_when_overdue") == "on"
    )

    return None


#Organization branding and borrowing policy
@role_required("Admin")
def branding_settings(request):

    branding = OrganizationSettings.load()

    error = None
    policy_error = None

    # Two forms on one page, told apart by a hidden field. Separate because
    # they are separate state: saving the colours must not blank a lending
    # rule, and the branding form posts files while this one does not.
    if request.method == "POST" and request.POST.get("section") == "policy":

        policy_error = read_policy_form(request, branding)

        if policy_error is None:

            branding.save()

            clear_branding_cache()

            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="OrganizationSettings",
                entity_id=branding.id,
                description="Borrowing policy updated",
            )

            messages.success(request, "Borrowing policy updated.")

            return redirect("branding_settings")

    elif request.method == "POST":

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
            "policy_error": policy_error,
            "policy_defaults": {
                "loan_period_days": policy.DEFAULT_LOAN_PERIOD_DAYS,
                "loan_period_min": policy.LOAN_PERIOD_MIN,
                "loan_period_max": policy.LOAN_PERIOD_MAX,
                "limit_min": policy.LIMIT_MIN,
                "limit_max": policy.LIMIT_MAX,
            },
        }
    )


# ==========================================================================
# STOCK CHECK
#
# Counting the shelves against the record. Everything here reports; nothing
# here changes a copy. See library/inventory.py for why.
# ==========================================================================


@role_required("Admin", "Librarian")
def inventory_session_list(request):
    """Every stock check, open ones first.

    The counts on each row are annotated, not looped over: a page listing
    twenty sessions makes one query for them, not twenty.
    """

    sessions = InventorySession.objects.select_related(
        "started_by", "location", "shelf__location"
    ).annotate(
        found_count=models.Count(
            "scans",
            filter=models.Q(scans__outcome=InventoryScan.OUTCOME_FOUND),
            distinct=True,
        ),
    ).order_by("status", "-started_at", "-id")

    paginator = Paginator(sessions, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/inventory_session_list.html",
        {
            "sessions": page,
            "paginator": paginator,
            "pagination_query": query_with(request, page=None),
            "open_count": InventorySession.objects.filter(
                status=InventorySession.STATUS_IN_PROGRESS
            ).count(),
        },
    )


@role_required("Admin", "Librarian")
def inventory_session_start(request):
    """Begin a stock check over a chosen scope."""

    error = None

    form = {
        "name": "",
        "scope": InventorySession.SCOPE_LIBRARY,
        "location": "",
        "shelf": "",
    }

    if request.method == "POST":

        form["name"] = request.POST.get("name", "").strip()
        form["scope"] = request.POST.get("scope", "").strip()
        form["location"] = request.POST.get("location", "").strip()
        form["shelf"] = request.POST.get("shelf", "").strip()

        location = None
        shelf = None

        if not form["name"]:
            error = "Give the stock check a name, so it can be told apart."

        elif form["scope"] not in dict(InventorySession.SCOPE_CHOICES):
            error = "Choose what this stock check covers."

        elif form["scope"] == InventorySession.SCOPE_LOCATION:

            location = Location.objects.filter(
                id=form["location"]
            ).first() if form["location"].isdigit() else None

            if location is None:
                error = "Choose the location to check."

        elif form["scope"] == InventorySession.SCOPE_SHELF:

            shelf = Shelf.objects.select_related("location").filter(
                id=form["shelf"]
            ).first() if form["shelf"].isdigit() else None

            if shelf is None:
                error = "Choose the shelf to check."

        if error is None:

            session = InventorySession.objects.create(
                name=form["name"],
                scope=form["scope"],
                location=location,
                shelf=shelf,
                status=InventorySession.STATUS_IN_PROGRESS,
                started_by=request.user,
                started_at=timezone.now(),
            )

            create_activity_log(
                user=request.user,
                action="CREATE",
                entity_type="InventorySession",
                entity_id=session.id,
                description=(
                    "Stock check started: %s (%s)"
                    % (session.name, session.scope_label)
                ),
            )

            return redirect("inventory_session_detail", session_id=session.id)

    return render(
        request,
        "library/inventory_session_start.html",
        {
            "form": form,
            "error": error,
            "scopes": InventorySession.SCOPE_CHOICES,
            "locations": Location.objects.order_by("name"),
            "shelves": Shelf.objects.select_related(
                "location"
            ).order_by("location__name", "shelf_code"),
        },
    )


@role_required("Admin", "Librarian")
def inventory_session_detail(request, session_id):
    """An open session's counting screen, or a finished one's report.

    One template for both, because they describe the same thing at two
    points in its life and splitting them would mean keeping two accounts
    of what a session is in step.
    """

    session = get_object_or_404(
        InventorySession.objects.select_related(
            "started_by", "location", "shelf__location"
        ),
        id=session_id,
    )

    counts = inventory.session_counts(session)

    # The lists are worth the queries only once there is something to say,
    # which for a session still being counted is not yet.
    missing = []
    outside = []
    duplicates = []

    if not session.is_open:
        missing = inventory.missing_copies(session)
        outside = inventory.session_scans(
            session, InventoryScan.OUTCOME_OUTSIDE
        )
        duplicates = inventory.session_scans(
            session, InventoryScan.OUTCOME_DUPLICATE
        )

    return render(
        request,
        "library/inventory_session_detail.html",
        {
            "session": session,
            "counts": counts,
            "missing": missing,
            "outside": outside,
            "duplicates": duplicates,
            "recent": inventory.session_scans(
                session, InventoryScan.OUTCOME_FOUND
            )[:10] if session.is_open else [],
        },
    )


@role_required("Admin", "Librarian")
def inventory_session_scan(request, session_id):
    """Record one code read during a stock check.

    A POST and a redirect, so a reload does not re-scan the last book and
    the field comes back empty for the next one. The outcome is reported as
    a message, which is what the librarian reads between books.
    """

    session = get_object_or_404(InventorySession, id=session_id)

    landing = redirect("inventory_session_detail", session_id=session.id)

    if request.method != "POST":
        return landing

    # A finished session is finished. Checked here as well as hidden in the
    # template, because a kept tab and a hand-made POST both arrive here.
    if not session.is_open:
        messages.warning(
            request,
            "This stock check is complete. Start a new one to carry on "
            "counting.",
        )
        return landing

    code = request.POST.get("copy_code", "").strip()

    if not code:
        return landing

    # The same targeted lookup the issue and return workflows use. There is
    # one scanner in this application, not three.
    copy = copy_for_code(code)

    outcome, scan = inventory.record_scan(session, copy, code, request.user)

    if outcome == InventoryScan.OUTCOME_FOUND:
        messages.success(
            request, "%s found. Scan the next one." % copy.copy_code
        )

    elif outcome == InventoryScan.OUTCOME_DUPLICATE:
        messages.info(
            request,
            "%s has already been counted in this check." % copy.copy_code,
        )

    elif outcome == InventoryScan.OUTCOME_OUTSIDE:
        messages.warning(
            request,
            "%s is not part of this check - the record puts it %s. "
            "Nothing has been changed; move it or edit the copy if it "
            "belongs here."
            % (
                copy.copy_code,
                (
                    "on %s, %s"
                    % (copy.shelf.shelf_code, copy.shelf.location.name)
                )
                if copy.shelf_id
                else "on no shelf",
            ),
        )

    else:
        messages.error(
            request,
            "No copy carries the code \u201c%s\u201d. Check the label." % code,
        )

    return landing


@role_required("Admin", "Librarian")
def inventory_session_complete(request, session_id):
    """Declare a stock check finished.

    Locked and re-read first, so completing twice is safe: the second
    request finds it already Completed and changes nothing rather than
    moving the completion time.
    """

    session = get_object_or_404(InventorySession, id=session_id)

    if request.method != "POST":
        return redirect("inventory_session_detail", session_id=session.id)

    with transaction.atomic():

        locked = InventorySession.objects.select_for_update().get(
            id=session.id
        )

        if locked.is_open:

            locked.status = InventorySession.STATUS_COMPLETED
            locked.completed_at = timezone.now()
            locked.save(update_fields=["status", "completed_at"])

            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="InventorySession",
                entity_id=locked.id,
                description="Stock check completed: %s" % locked.name,
            )

            messages.success(
                request,
                "Stock check complete. What was not found is listed below "
                "- nothing has been marked Missing.",
            )

        else:
            messages.info(request, "This stock check was already complete.")

    return redirect("inventory_session_detail", session_id=session.id)


# ==========================================================================
# REPORTS
#
# Six operational questions, each answered from the records that already
# hold the answer - see library/reports.py. Every one takes its filters
# from the query string, so a report is a URL: bookmarkable, shareable,
# printable, and exactly what the CSV export reads.
#
# Open to the same roles as the pages they summarise. A report shows no
# borrower detail that `borrower_detail` does not already show to every
# role, and building a second permission model for the same data would
# leave two answers to one question.
# ==========================================================================


def csv_response(filename, header, rows):
    """`rows` as a CSV download.

    The same records the page showed, from the same filtered queryset - the
    export is the report, not a second query that could answer differently.

    A UTF-8 BOM, because the librarians open these in Excel and without it
    the Arabic and Urdu titles arrive as mojibake.
    """

    response = HttpResponse(content_type="text/csv; charset=utf-8")

    # Only what a filename may safely hold, and always ending in .csv.
    safe = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in filename
    ).strip("-") or "report"

    response["Content-Disposition"] = (
        'attachment; filename="%s-%s.csv"'
        % (safe, timezone.now().date().isoformat())
    )

    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow(header)

    for row in rows:
        writer.writerow(row)

    return response


def wants_csv(request):
    return request.GET.get("format") == "csv"


def report_context(request, dates, **extra):
    """What every report page needs: its period, its filters, its links."""

    context = {
        "dates": dates,
        "start": dates.start.isoformat() if dates.start else "",
        "end": dates.end.isoformat() if dates.end else "",
        "error": dates.error,
        "printed_on": timezone.now(),
        # The current filters, so Print and Export carry them.
        "export_query": query_with(request, format="csv"),
        "locations": Location.objects.order_by("name"),
    }
    context.update(extra)

    return context


def reports_home(request):
    """One page listing the reports, rather than six sidebar entries."""

    return render(
        request,
        "library/reports_home.html",
        {
            "reports": [
                {
                    "url": reverse(name),
                    "title": title,
                    "icon": icon,
                    "blurb": blurb,
                }
                for name, title, icon, blurb in reports.REPORTS
            ],
        },
    )


def report_circulation(request):
    """What went out and came back over a period."""

    dates = reports.parse_range(request)

    location_id = numeric_param(request, "location")
    borrower_id = numeric_param(request, "borrower")
    title = (request.GET.get("title") or "").strip()

    if dates.error:
        # Nothing is reported on a period nobody can read. Saying so beats
        # answering a question that was not asked.
        return render(
            request,
            "library/report_circulation.html",
            report_context(request, dates, tallies=None, loans=[]),
        )

    tallies, loans = reports.circulation(
        dates, location_id, borrower_id, title
    )

    if wants_csv(request):
        return csv_response(
            "circulation",
            [
                "Copy code", "Book", "Author", "Borrower",
                "Issued", "Due", "Returned",
            ],
            (
                [
                    loan.copy.copy_code,
                    loan.copy.volume.book.title,
                    (
                        loan.copy.volume.book.author.name
                        if loan.copy.volume.book.author else ""
                    ),
                    loan.borrower.name,
                    loan.issue_date,
                    loan.due_date,
                    loan.return_date or "",
                ]
                for loan in loans
            ),
        )

    paginator = Paginator(loans, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/report_circulation.html",
        report_context(
            request,
            dates,
            tallies=tallies,
            # Named and ordered here rather than in the template, so the
            # page and the CSV describe the same five numbers.
            tallies_shown=[
                ("Issued", tallies["issued"]),
                ("Returned", tallies["returned"]),
                ("Renewals", tallies["renewals"]),
                ("Out now", tallies["out"]),
                ("Overdue now", tallies["overdue"]),
            ],
            loans=page,
            paginator=paginator,
            pagination_query=query_with(request, page=None),
            location_id=str(location_id or ""),
            borrower_id=str(borrower_id or ""),
            borrower_name=selected_name(Borrower, borrower_id),
            title_filter=title,
        ),
    )


def report_overdue(request):
    """Everything out past its due date, longest first."""

    today = timezone.now().date()
    location_id = numeric_param(request, "location")

    loans = describe_loans(reports.overdue(location_id), today)

    if wants_csv(request):
        return csv_response(
            "overdue",
            [
                "Borrower", "Registration no", "Phone", "Book", "Author",
                "Copy code", "Issued", "Due", "Days overdue",
            ],
            (
                [
                    loan.borrower.name,
                    loan.borrower.registration_no or "",
                    loan.borrower.phone,
                    loan.copy.volume.book.title,
                    (
                        loan.copy.volume.book.author.name
                        if loan.copy.volume.book.author else ""
                    ),
                    loan.copy.copy_code,
                    loan.issue_date,
                    loan.due_date,
                    loan.days_overdue,
                ]
                for loan in loans
            ),
        )

    return render(
        request,
        "library/report_overdue.html",
        report_context(
            request,
            reports.DateRange(None, None, ""),
            loans=loans,
            total=len(loans),
            location_id=str(location_id or ""),
        ),
    )


def inventory_report(request, template, states, heading):
    """Shared by the two reports that count copies by state."""

    today = timezone.now().date()

    location_id = numeric_param(request, "location")
    shelf_id = numeric_param(request, "shelf")
    state = (request.GET.get("status") or "").strip().casefold()

    if state not in states:
        state = ""

    copies = BookCopy.objects.select_related(
        "volume__book__author", "shelf__location"
    )

    if location_id:
        copies = copies.filter(shelf__location_id=location_id)

    if shelf_id:
        copies = copies.filter(shelf_id=shelf_id)

    # Counted over everything the location and shelf filters allow, so the
    # tiles keep describing the same set whichever state is being listed.
    tallies = reports.inventory_counts(copies, today, states)

    if state:
        listed = filter_copies_by_state(copies, state, today)

    elif states == reports.CONDITION_STATES:
        # With no state chosen, this report is about those states and
        # nothing else - it exists to answer "what is not usable".
        listed = copies.filter(
            status__in=[name.title() for name in states]
        )

    else:
        listed = copies

    listed = listed.order_by("copy_code")

    if wants_csv(request):
        return csv_response(
            heading.lower().replace(" ", "-"),
            ["Copy code", "Book", "Author", "Location", "Shelf", "Status"],
            (
                [
                    copy.copy_code,
                    copy.volume.book.title,
                    (
                        copy.volume.book.author.name
                        if copy.volume.book.author else ""
                    ),
                    copy.shelf.location.name if copy.shelf_id else "",
                    copy.shelf.shelf_code if copy.shelf_id else "",
                    copy.status,
                ]
                for copy in listed
            ),
        )

    paginator = Paginator(listed, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        template,
        report_context(
            request,
            reports.DateRange(None, None, ""),
            heading=heading,
            tallies=tallies,
            states=[
                (name, COPY_STATE_LABELS[name], tallies.get(name, 0))
                for name in states
            ],
            copies=page,
            paginator=paginator,
            pagination_query=query_with(request, page=None),
            location_id=str(location_id or ""),
            shelf_id=str(shelf_id or ""),
            state=state,
            shelves=Shelf.objects.select_related("location").order_by(
                "location__name", "shelf_code"
            ),
        ),
    )


def report_inventory(request):
    """Where the copies are and what state they are in."""

    return inventory_report(
        request,
        "library/report_inventory.html",
        reports.INVENTORY_STATES,
        "Inventory Status",
    )


def report_condition(request):
    """The copies that are not on the shelf in usable condition."""

    return inventory_report(
        request,
        "library/report_inventory.html",
        reports.CONDITION_STATES,
        "Lost, Damaged and Withdrawn",
    )


def report_borrowers(request):
    """What a borrower, or everyone, did over a period."""

    dates = reports.parse_range(request)
    borrower_id = numeric_param(request, "borrower")

    if dates.error:
        return render(
            request,
            "library/report_borrowers.html",
            report_context(request, dates, tallies=None, rows=[]),
        )

    tallies, rows = reports.borrower_activity(dates, borrower_id)

    if wants_csv(request):
        return csv_response(
            "borrower-activity",
            [
                "Borrower", "Registration no", "Issued", "Returned",
                "Currently out", "Overdue",
            ],
            (
                [
                    row.name,
                    row.registration_no or "",
                    row.issued,
                    row.returned,
                    row.out,
                    row.overdue,
                ]
                for row in rows
            ),
        )

    paginator = Paginator(rows, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/report_borrowers.html",
        report_context(
            request,
            dates,
            tallies=tallies,
            tallies_shown=[
                ("Issued", tallies["issued"]),
                ("Returned", tallies["returned"]),
                ("Renewals", tallies["renewals"]),
                ("Out now", tallies["out"]),
                ("Overdue now", tallies["overdue"]),
            ],
            rows=page,
            paginator=paginator,
            pagination_query=query_with(request, page=None),
            borrower_id=str(borrower_id or ""),
            borrower_name=selected_name(Borrower, borrower_id),
        ),
    )


def report_popular(request):
    """Books ranked by how often they were actually lent."""

    dates = reports.parse_range(request, days=365)

    if dates.error:
        return render(
            request,
            "library/report_popular.html",
            report_context(request, dates, books=[]),
        )

    books = reports.popular(dates)

    if wants_csv(request):
        return csv_response(
            "popular-books",
            ["Rank", "Book", "Author", "Times issued", "Copies available"],
            (
                [
                    rank,
                    book.title,
                    book.author.name if book.author else "",
                    book.times_issued,
                    book.copies_available,
                ]
                for rank, book in enumerate(books, start=1)
            ),
        )

    return render(
        request,
        "library/report_popular.html",
        report_context(request, dates, books=books),
    )


# ==========================================================================
# RESERVATIONS
#
# A queue of borrowers waiting for a book. Nothing here holds a copy back,
# and nothing happens automatically when one is returned - see
# library/reservations.py.
# ==========================================================================


def reservation_list(request):
    """Everyone currently waiting, and what for.

    Grouped by nothing: it is one list, oldest first, because the question
    a librarian brings to it is "who has been waiting longest".
    """

    status = (request.GET.get("status") or "").strip()

    if status not in dict(Reservation.STATUS_CHOICES):
        status = Reservation.STATUS_ACTIVE

    waiting = Reservation.objects.filter(
        status=status
    ).select_related(
        "borrower", "book__author"
    ).order_by("created_at", "id")

    paginator = Paginator(waiting, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/reservation_list.html",
        {
            "reservations": page,
            "paginator": paginator,
            "pagination_query": query_with(request, page=None),
            "status": status,
            "statuses": Reservation.STATUS_CHOICES,
            "active_total": Reservation.objects.filter(
                status=Reservation.STATUS_ACTIVE
            ).count(),
        },
    )


def reservation_add(request):
    """Put a borrower in a book's queue.

    A POST from the book's own page, which is where the queue is on
    screen. The borrower comes from the same searchable control the issue
    form uses, so there is one way of naming a borrower in this
    application.
    """

    book_id = request.POST.get("book", "") if request.method == "POST" else ""

    book = get_object_or_404(Book, id=book_id) if book_id.isdigit() else None

    if request.method != "POST" or book is None:
        return redirect("book_list")

    landing = redirect(
        "%s?tab=reservations" % reverse("book_detail", args=[book.id])
    )

    borrower_id = request.POST.get("borrower", "").strip()

    borrower = Borrower.objects.filter(
        id=borrower_id
    ).first() if borrower_id.isdigit() else None

    if borrower is None:
        messages.error(request, "Choose who is waiting for this book.")
        return landing

    reservation, error = reservations.reserve(book, borrower)

    if error:
        messages.warning(request, error)
        return landing

    create_activity_log(
        user=request.user,
        action="RESERVE",
        entity_type="Reservation",
        entity_id=book.id,
        description=(
            "%s reserved %s" % (borrower.name, book.title)
        ),
    )

    messages.success(
        request,
        "%s is in the queue for %s." % (borrower.name, book.title),
    )

    return landing


def reservation_cancel(request, reservation_id):
    """Take a borrower out of a queue.

    Cancelling closes that reservation and nothing else: the people behind
    move up because they were always behind, not because anything was
    renumbered.
    """

    reservation = get_object_or_404(
        Reservation.objects.select_related("borrower", "book"),
        id=reservation_id,
    )

    landing = redirect(
        safe_redirect_target(
            request, reverse("borrower_detail", args=[reservation.borrower_id])
        )
    )

    if request.method != "POST":
        return landing

    if reservations.close(
        reservation, Reservation.STATUS_CANCELLED, request.user
    ):
        create_activity_log(
            user=request.user,
            action="CANCEL",
            entity_type="Reservation",
            entity_id=reservation.book_id,
            description=(
                "%s cancelled their reservation for %s"
                % (reservation.borrower.name, reservation.book.title)
            ),
        )

        messages.success(
            request,
            "%s is no longer waiting for %s."
            % (reservation.borrower.name, reservation.book.title),
        )

    else:
        messages.info(
            request, "That reservation had already been closed."
        )

    return landing


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

    # Both counts in the one query the list already used. `distinct` is
    # what keeps them honest: the two joins multiply each other's rows, so
    # without it a location's shelves would be counted once per copy.
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
        ).order_by("name")
    )

    paginator = Paginator(locations, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/location_list.html",
        {
            "locations": page,
            "paginator": paginator,
            "search": search,
            # Copies that have arrived but not been placed belong to no
            # location, so they can only be reached from here — hence the
            # count, which doubles as the way in.
            "unshelved_count": BookCopy.objects.filter(
                shelf__isnull=True
            ).count(),
            "pagination_query": query_with(request, page=None),
            "elided_page_range": list(
                paginator.get_elided_page_range(
                    page.number,
                    on_each_side=1,
                    on_ends=1,
                )
            ),
            "page_ellipsis": Paginator.ELLIPSIS,
            "can_edit": can_edit_library(request.user),
        }
    )

def location_detail(request, location_id):

    location = get_object_or_404(
        Location,
        id=location_id
    )

    # Shelf-level only: a location can hold thousands of copies, and
    # listing them here would make the page grow with the library. Each
    # shelf carries its own count and opens its own contents.
    shelves = list(
        Shelf.objects.filter(
            location=location
        ).annotate(
            copy_count=models.Count("bookcopy")
        ).order_by("shelf_code")
    )

    return render(
        request,
        "library/location_detail.html",
        {
            "location": location,
            "shelves": shelves,
            "shelf_count": len(shelves),
            # Summed from the counts already fetched rather than asked for
            # again.
            "copy_count": sum(shelf.copy_count for shelf in shelves),
            "can_edit": can_edit_library(request.user),
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

    # The copy count comes back with the shelves, in the same query, so the
    # list costs the same however many copies the library holds.
    def shelves_with_counts():
        return Shelf.objects.select_related(
            "location"
        ).annotate(
            copy_count=models.Count("bookcopy")
        ).order_by("location__name", "shelf_code")

    if search or location_id:
        shelves = shelves_with_counts()

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
            shelves = list(shelves_with_counts())

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
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/shelf_list.html",
        {
            "shelves": page,
            "paginator": paginator,
            "search": search,
            "location_id": location_id,
            "locations": locations,
            "unshelved_count": BookCopy.objects.filter(
                shelf__isnull=True
            ).count(),
            "pagination_query": query_with(request, page=None),
            "elided_page_range": list(
                paginator.get_elided_page_range(
                    page.number,
                    on_each_side=1,
                    on_ends=1,
                )
            ),
            "page_ellipsis": Paginator.ELLIPSIS,
            "can_edit": can_edit_library(request.user),
        }
    )

def shelf_detail(request, shelf_id):
    """What is physically on one shelf.

    The end of the Locations -> Location -> Shelf trail. Each copy opens
    the existing copy detail; each book title opens the existing book
    page. Nothing about a copy or a book is described here that those
    pages do not already own.
    """

    shelf = get_object_or_404(
        Shelf.objects.select_related(
            "location"
        ),
        id=shelf_id
    )

    search = request.GET.get("search", "").strip()

    copies = BookCopy.objects.filter(
        shelf=shelf
    ).select_related(
        "volume__book"
    )

    if search:
        copies = copies.filter(
            Q(copy_code__icontains=search)
            | Q(volume__book__title__icontains=search)
        )

    copies = copies.order_by("volume__book__title", "copy_code")

    # A shelf can hold a lot; the page should not grow with it.
    paginator = Paginator(copies, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    # Status for the copies on this page only, from the same helper the
    # copy list uses, so the word shown is the same word.
    describe_copies(page.object_list)

    # What titles are on this shelf, and how many of each — one grouped
    # query rather than a count per book. Unfiltered by the search, since
    # it describes the shelf rather than the current view.
    titles = list(
        BookCopy.objects.filter(
            shelf=shelf
        ).values(
            "volume__book_id",
            "volume__book__title",
        ).annotate(
            copies=models.Count("id")
        ).order_by("volume__book__title")
    )

    return render(
        request,
        "library/shelf_detail.html",
        {
            "shelf": shelf,
            "copies": page,
            "paginator": paginator,
            "search": search,
            "titles": titles,
            "title_count": len(titles),
            "pagination_query": query_with(request, page=None),
            "elided_page_range": list(
                paginator.get_elided_page_range(
                    page.number,
                    on_each_side=1,
                    on_ends=1,
                )
            ),
            "page_ellipsis": Paginator.ELLIPSIS,
            "can_edit": can_edit_library(request.user),
        }
    )

#Shelf Add
@role_required("Admin", "Librarian")
def shelf_add(request):

    # Quick-add from the Add Book dialog, as in `location_add`.
    options = is_options_request(request)

    locations = Location.objects.all()

    # Preselect location when coming from Location Detail page
    preselected_location_id = request.GET.get("location", "").strip()
    preselected_location = None
    if preselected_location_id.isdigit():
        preselected_location = Location.objects.filter(
            id=int(preselected_location_id)
        ).first()

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
            "locations": locations,
            "preselected_location": preselected_location,
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


def annotate_copy_counts(books):
    """Total, available and issued copy counts, in the list's own query.

    One pass over the join that is already there rather than a count per
    row, so a page of results costs the same whatever the catalogue holds.
    `distinct=True` on each because the three aggregates share one join and
    would otherwise multiply each other.

    The definitions are the copy list's, not new ones: a copy is out when a
    loan says so, and available only when it is on a shelf, marked
    Available, and not out. Withdrawn copies - Lost, Damaged, Missing,
    Transferred - fall into neither, which is why the two can be less than
    the total.
    """

    out = active_loan_copies()

    return books.annotate(
        total_copies=models.Count(
            "bookvolume__bookcopy",
            distinct=True,
        ),
        issued_copies=models.Count(
            "bookvolume__bookcopy",
            filter=models.Q(bookvolume__bookcopy__id__in=out),
            distinct=True,
        ),
        available_copies=models.Count(
            "bookvolume__bookcopy",
            filter=models.Q(
                bookvolume__bookcopy__status=BookCopy.STATUS_AVAILABLE,
                bookvolume__bookcopy__shelf__isnull=False,
            ) & ~models.Q(bookvolume__bookcopy__id__in=out),
            distinct=True,
        ),
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

    # Archived books are out of the catalogue unless they are asked for.
    # `archived=1` is the only way to see them, and it is the whole of the
    # "where did it go" answer - the list, its search and its filters are
    # otherwise untouched.
    show_archived = request.GET.get("archived") == "1"

    books = (
        Book.objects.filter(archived_at__isnull=False)
        if show_archived
        else active_books()
    ).select_related(
        "author",
        "category",
        "publisher",
    )

    if search:
        # Title or author, from the one box. A librarian looking for a book
        # knows one or the other and should not have to say which, and
        # asking them to pick a mode first was the slower path.
        #
        # `author` is a forward many-to-one and NOT NULL, so this join
        # cannot multiply rows and no .distinct() is needed - which
        # matters, because .distinct() would fight the nulls-last ordering
        # below.
        books = books.filter(
            Q(title__icontains=search)
            | Q(author__name__icontains=search)
        )

    if author_id:
        books = books.filter(author_id=author_id)

    if category_id:
        books = books.filter(category_id=category_id)

    if publisher_id:
        books = books.filter(publisher_id=publisher_id)

    # Counted before it is filtered on, so the numbers in the row and the
    # filter that selected it are the same figures.
    books = annotate_copy_counts(books)

    availability = request.GET.get("availability", "").strip()

    if availability not in BOOK_AVAILABILITY_FILTERS:
        availability = ""

    if availability == "available":
        books = books.filter(available_copies__gt=0)

    elif availability == "issued":
        books = books.filter(issued_copies__gt=0)

    elif availability == "none":
        books = books.filter(total_copies=0)

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

    # How many are put away, so the list can offer the way to them without
    # a second page. One indexed count over the partial index.
    archived_total = Book.objects.filter(archived_at__isnull=False).count()

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
            (
                "Availability",
                ("availability",),
                BOOK_AVAILABILITY_LABELS.get(availability, ""),
            ),
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
        # Whether this is the archive rather than the catalogue, how many
        # books are in it, and the way in and out.
        "availability": availability,
        "availability_options": [
            {
                "value": value,
                "label": BOOK_AVAILABILITY_LABELS[value],
                "active": value == availability,
            }
            for value in BOOK_AVAILABILITY_FILTERS
        ],
        # The secondary filters, so the panel holding them can open itself
        # when one is in force rather than hiding an active filter.
        "secondary_active": bool(publisher_id or availability),
        "publishers": Publisher.objects.only("id", "name").order_by("name"),
        "show_archived": show_archived,
        "archived_total": archived_total,
        "archived_url": "?" + query_with(request, archived="1", page=None),
        "catalogue_url": "?" + query_with(request, archived=None, page=None),
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


# Where an upload waits between the steps of the import. The workbook is
# kept on disk, not in the session: a catalogue file is far too big to
# carry in one, and the steps only need to know which file is theirs.
IMPORT_SESSION_KEY = "book_import"
IMPORT_FAILURE_KEY = "book_import_failures"

# Deliberately not MEDIA_ROOT: that directory is served at /media/, so a
# workbook left there would be fetchable by anyone who guessed its name.
import_storage = FileSystemStorage(
    location=os.path.join(settings.BASE_DIR, "private", "imports")
)

# How long an upload in progress is kept. An import that is finished or
# cancelled deletes its own file straight away; this is for the ones that
# are simply abandoned — a closed tab leaves a file with nobody to clear
# it, and without a sweep they accumulate for ever.
IMPORT_KEEP_SECONDS = 24 * 60 * 60


def sweep_stale_imports():
    """Delete abandoned uploads. Called when a new one arrives.

    Opportunistic rather than scheduled: the work is trivial, it only runs
    when somebody is importing anyway, and it saves the project a
    background job it does not otherwise need.
    """

    cutoff = timezone.now() - timedelta(seconds=IMPORT_KEEP_SECONDS)

    try:
        _, names = import_storage.listdir("")

    except (OSError, NotImplementedError):
        return

    for name in names:
        try:
            if import_storage.get_modified_time(name) < cutoff:
                import_storage.delete(name)

        except (OSError, ValueError):
            # Someone else's import finishing at the same moment, or a
            # storage that cannot report times. Neither is worth failing
            # an upload over.
            continue


def import_limits():
    """The ceilings the Add Book form applies, handed to the planner.

    Passed rather than imported so `library.excel` never has to reach back
    into the views — and so a spreadsheet cannot ask for something the
    form itself would refuse.
    """

    return {
        "volumes": MAX_VOLUMES,
        "per_volume": MAX_COPIES_PER_VOLUME,
        "total_copies": MAX_TOTAL_COPIES,
    }


def import_lookups():
    """Every name the planner needs, fetched once.

    This is what keeps a thousand-row file from becoming a thousand
    queries: six reads up front, then the planner works from dictionaries.
    """

    books = {
        book.id: book
        for book in Book.objects.select_related("author").all()
    }

    return {
        "books": books,
        # Title alone is not unique in the schema, so the natural key for
        # "the library already has this" is title with author.
        "by_title_author": {
            (book.title.casefold(), book.author.name.casefold()): book
            for book in books.values()
        },
        "authors": {
            name.casefold(): pk
            for pk, name in Author.objects.values_list("id", "name")
        },
        "categories": {
            name.casefold(): pk
            for pk, name in Category.objects.values_list("id", "name")
        },
        "publishers": {
            name.casefold(): pk
            for pk, name in Publisher.objects.values_list("id", "name")
        },
        "locations": {
            name.casefold(): pk
            for pk, name in Location.objects.values_list("id", "name")
        },
        # Keyed by location as well as code, so a shelf can only ever be
        # found inside the location the row names.
        "shelves": {
            (location_id, code.casefold()): pk
            for pk, location_id, code in Shelf.objects.values_list(
                "id", "location_id", "shelf_code"
            )
        },
        # Code to the book that owns it, not just the set of codes: an
        # unchanged export re-imports its own codes, and those have to be
        # told apart from a genuine clash with another book.
        # Which volume numbers each book already has, so the
        # confirmation step counts only the ones that would be new.
        "volumes": _volume_numbers_by_book(),
        "codes": {
            code.casefold(): book_id
            for code, book_id in BookCopy.objects.values_list(
                "copy_code", "volume__book_id"
            )
        },
    }


def _volume_numbers_by_book():
    """{book id: {volume numbers}}, in one query."""

    index = {}

    for book_id, number in BookVolume.objects.values_list(
        "book_id", "volume_number"
    ):
        index.setdefault(book_id, set()).add(number)

    return index


def workbook_response(book, filename):
    """A workbook as a download, without it touching the disk."""

    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)

    response = HttpResponse(
        buffer.read(),
        content_type=(
            "application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet"
        ),
    )

    response["Content-Disposition"] = 'attachment; filename="%s"' % filename

    return response


@role_required("Admin", "Librarian")
def book_import_template(request):
    """The blank template, with its instructions and example rows."""

    return workbook_response(
        book_excel.build_template(),
        "madrasah-library-book-template.xlsx",
    )


@role_required("Admin", "Librarian")
def book_export(request):
    """Existing books as a workbook that can be edited and put back.

    One row per volume, which is the shape the importer reads, so an
    export is a round trip rather than a dead end. Copy codes come along
    for reference; the copies themselves are not re-created from them.

    Honours the book list's own filters, so "export what I am looking at"
    works without a second set of controls.
    """

    books = Book.objects.select_related(
        "author", "category", "publisher"
    )

    search = (request.GET.get("search") or "").strip()

    if search:
        books = books.filter(title__icontains=search)

    for param, field in (
        ("author", "author_id"),
        ("category", "category_id"),
        ("publisher", "publisher_id"),
    ):
        value = numeric_param(request, param)

        if value:
            books = books.filter(**{field: value})

    books = books.order_by("title", "id")

    # Volumes and their copies for every exported book, in two queries
    # rather than two per book.
    volumes = {}

    for volume in BookVolume.objects.filter(
        book__in=books
    ).order_by("book_id", "volume_number"):
        volumes.setdefault(volume.book_id, []).append(volume)

    copies = {}

    for copy in BookCopy.objects.filter(
        volume__book__in=books
    ).select_related("shelf__location").order_by("copy_code"):
        copies.setdefault(copy.volume_id, []).append(copy)

    rows = []

    for book in books:

        base = {
            "book_id": book.id,
            "title": book.title,
            "author": book.author.name if book.author_id else "",
            "category": book.category.name if book.category_id else "",
            "publisher": book.publisher.name if book.publisher_id else "",
        }

        book_volumes = volumes.get(book.id, [])

        if not book_volumes:
            rows.append(dict(base))
            continue

        for volume in book_volumes:

            volume_copies = copies.get(volume.id, [])

            # Where the copies are. They are grouped by shelf so one row
            # never claims a single shelf for copies sitting in two places;
            # a volume split across shelves gets a row per shelf.
            by_shelf = {}

            for copy in volume_copies:
                by_shelf.setdefault(copy.shelf_id, []).append(copy)

            row = dict(base)

            # A single implicit volume is left blank, the same way the rest
            # of the app declines to call it "Volume 1".
            if volume.volume_number != 1 or volume.title:
                row["volume_number"] = volume.volume_number
                row["volume_title"] = volume.title

            if not volume_copies:
                rows.append(row)
                continue

            # Every row of a volume repeats its number: that is what pairs
            # the rows back to one volume on re-import. Blanking it on the
            # second row made it read as a different, implicit volume.
            for shelf_id, group in by_shelf.items():

                shelf_row = dict(row)
                shelf_row["copies"] = len(group)
                shelf_row["copy_codes"] = ", ".join(
                    copy.copy_code for copy in group
                )

                shelf = group[0].shelf

                if shelf is not None:
                    shelf_row["location"] = shelf.location.name
                    shelf_row["shelf"] = shelf.shelf_code

                rows.append(shelf_row)

    return workbook_response(
        book_excel.build_export(rows),
        "madrasah-library-books.xlsx",
    )


def mapping_rows(headings, mapping):
    """One entry per spreadsheet column, for the mapping step to render.

    A list rather than the dict itself, because a template cannot look a
    dict up by index and inventing a filter for that would be the long way
    round.
    """

    return [
        {
            "index": index,
            "heading": heading,
            "key": mapping.get(index, book_excel.IGNORE),
        }
        for index, heading in enumerate(headings)
    ]


def import_state(request):
    """What the session remembers about the upload in progress."""

    return request.session.get(IMPORT_SESSION_KEY) or {}


def clear_import(request):
    """Forget the upload, and delete the file it left behind."""

    state = import_state(request)

    path = state.get("path")

    if path and import_storage.exists(path):
        import_storage.delete(path)

    request.session.pop(IMPORT_SESSION_KEY, None)


def read_stored_upload(request):
    """Headings and rows from the stored upload, or (None, None, error)."""

    state = import_state(request)

    path = state.get("path")

    if not path or not import_storage.exists(path):
        return None, None, (
            "That upload has expired. Please choose the file again."
        )

    with import_storage.open(path, "rb") as handle:
        try:
            headings, rows = book_excel.read_upload(handle)

        except book_excel.WorkbookError as problem:
            return None, None, str(problem)

    return headings, rows, ""


@role_required("Admin", "Librarian")
def book_import(request):
    """Upload a workbook, map its columns, check it, then import it.

    Four steps in one view and one template, because they are one task and
    the state between them is small. Nothing is written until the last
    step is confirmed.
    """

    state = import_state(request)
    step = "upload"
    error = ""
    context = {}

    action = request.POST.get("action", "") if request.method == "POST" else ""

    if action == "cancel":
        clear_import(request)
        messages.info(request, "Import cancelled.")
        return redirect("book_import")

    # ---- step 1: the file -----------------------------------------------
    if action == "upload":

        upload = request.FILES.get("workbook")

        if upload is None:
            error = "Choose an .xlsx file to upload."

        else:
            try:
                headings, rows = book_excel.read_upload(upload)

            except book_excel.WorkbookError as problem:
                error = str(problem)

            else:
                clear_import(request)
                request.session.pop(IMPORT_FAILURE_KEY, None)
                sweep_stale_imports()

                upload.seek(0)

                path = import_storage.save("%s.xlsx" % uuid4().hex, upload)

                request.session[IMPORT_SESSION_KEY] = {
                    "path": path,
                    "name": upload.name,
                    "rows": len(rows),
                }

                state = import_state(request)

                context["column_rows"] = mapping_rows(
                    headings, book_excel.guess_mapping(headings)
                )
                step = "map"

    # ---- steps 2-4: everything else works from the stored file ----------
    elif action in ("map", "confirm"):

        headings, rows, error = read_stored_upload(request)

        if not error:

            mapping = {}

            for index in range(len(headings)):
                mapping[index] = request.POST.get("column_%d" % index, "")

            problems = book_excel.check_mapping(mapping)

            context["column_rows"] = mapping_rows(headings, mapping)

            if problems:
                step = "map"
                error = " ".join(problems)

            else:
                lookups = import_lookups()

                groups = book_excel.build_groups(headings, rows, mapping)
                groups = book_excel.plan_import(
                    groups, lookups, import_limits()
                )

                summary = book_excel.summarise(groups, lookups["volumes"])

                context["summary"] = summary
                context["groups"] = groups

                if action == "map":
                    step = "preview"

                else:
                    results = run_book_import(request, summary["ready"])

                    # Everything the downloadable report needs, kept before
                    # the upload is thrown away: the row numbers, the values
                    # as they were read, and why each row was refused.
                    request.session[IMPORT_FAILURE_KEY] = (
                        import_failure_payload(
                            summary["blocked"], results["failed"]
                        )
                    )

                    context["results"] = results
                    context["failures"] = summary["blocked"]

                    clear_import(request)

                    step = "done"

    elif state:
        # Arriving back on the page with an upload still in hand.
        headings, rows, error = read_stored_upload(request)

        if not error:
            context["column_rows"] = mapping_rows(
                headings, book_excel.guess_mapping(headings)
            )
            step = "map"

    context.update({
        "step": step,
        "error": error,
        "state": state,
        "columns": book_excel.COLUMNS,
        "ignore": book_excel.IGNORE,
        "max_mb": book_excel.MAX_UPLOAD_BYTES // (1024 * 1024),
        "max_rows": book_excel.MAX_ROWS,
    })

    return render(request, "library/book_import.html", context)


def run_book_import(request, groups):
    """Create or update each group, one transaction at a time.

    Per group, not per file: a book with its volumes and copies is one
    thing as far as the librarian is concerned, so it lands whole or not
    at all — while a problem with one book does not throw away the others
    that were fine.

    Copies go in through `create_book_copies`, which is the same function
    the Add Book form uses, so generated codes come from the one
    generator and nothing here is a second implementation of it.
    """

    results = {
        "created": 0,
        "updated": 0,
        "volumes": 0,
        "copies": 0,
        "failed": [],
        "books": [],
    }

    lookups = import_lookups()

    # Cache shelf lookups to avoid repeated queries for the same shelf
    shelf_cache = {}

    def get_shelf(shelf_id):
        if shelf_id is None:
            return None
        if shelf_id not in shelf_cache:
            shelf_cache[shelf_id] = Shelf.objects.filter(id=shelf_id).first()
        return shelf_cache[shelf_id]

    for group in groups:

        try:
            with transaction.atomic():

                author_id = get_or_create_named(
                    Author, group.author, lookups["authors"],
                    AUTHOR_CACHE_KEY,
                )

                category_id = get_or_create_named(
                    Category, group.category, lookups["categories"],
                    CATEGORY_CACHE_KEY,
                ) if group.category else None

                publisher_id = get_or_create_named(
                    Publisher, group.publisher, lookups["publishers"],
                    PUBLISHER_CACHE_KEY,
                ) if group.publisher else None

                if group.existing_book is not None:

                    book = group.existing_book

                    book.title = group.title
                    book.author_id = author_id

                    # Blank cells leave a value alone rather than clearing
                    # it: an export the user only partly filled in must not
                    # quietly strip what it did not mention.
                    if category_id:
                        book.category_id = category_id

                    if publisher_id:
                        book.publisher_id = publisher_id

                    # Nothing here touches cover_image, volumes or copies.
                    book.save(update_fields=[
                        "title", "author", "category", "publisher",
                    ])

                    results["updated"] += 1

                else:
                    book = Book.objects.create(
                        title=group.title,
                        author_id=author_id,
                        category_id=category_id,
                        publisher_id=publisher_id,
                    )

                    results["created"] += 1

                # Volumes already on the book, so a re-import adds copies
                # to the volume that is there rather than a duplicate.
                existing_volumes = {
                    volume.volume_number: volume
                    for volume in BookVolume.objects.filter(book=book)
                }

                for planned in group.volumes:

                    volume = existing_volumes.get(planned["number"])

                    if volume is None:
                        volume = BookVolume.objects.create(
                            book=book,
                            volume_number=planned["number"],
                            title=planned["title"],
                        )

                        existing_volumes[planned["number"]] = volume
                        results["volumes"] += 1

                    if not planned["copies"]:
                        continue

                    shelf = get_shelf(planned["shelf_id"])

                    create_book_copies(
                        volume,
                        shelf,
                        planned["copies"],
                        codes=planned["codes"],
                    )

                    results["copies"] += planned["copies"]

        except (IntegrityError, DatabaseError, ValueError) as problem:
            # The group rolled back, so nothing half-built is left. The
            # message is deliberately about the data, not the exception.
            results["failed"].append({
                "label": group.label,
                "rows": group.row_numbers,
                "message": (
                    "This book could not be saved — something about it "
                    "clashed with what is already in the library. Nothing "
                    "from it was imported. (%s)"
                    % type(problem).__name__
                ),
            })

        else:
            results["books"].append(book)

    if results["created"] or results["updated"] or results["copies"]:
        cache.delete(BOOK_CACHE_KEY)
        cache.delete(BOOK_VOLUME_CACHE_KEY)
        cache.delete(BOOK_COPY_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        # One entry for the import, not one per book: a thousand-row file
        # should not bury the rest of the activity log.
        create_activity_log(
            user=request.user,
            action="IMPORT",
            entity_type="Book",
            entity_id=None,
            description=(
                "Excel import — %d book(s) added, %d updated, "
                "%d volume(s) and %d cop(y/ies) created"
                % (
                    results["created"],
                    results["updated"],
                    results["volumes"],
                    results["copies"],
                )
            ),
        )

    return results


def get_or_create_named(model, name, index, cache_key):
    """The id of `name` in `model`, creating the record if it is new.

    `index` is the name-to-id map already fetched in bulk, and is updated
    in place, so a name repeated down the file costs one query rather than
    one per row. Matching is case-insensitive, as it is everywhere else in
    the app; creation follows what the Add Book dropdowns already do.
    """

    folded = name.casefold()

    if folded in index:
        return index[folded]

    existing = model.objects.filter(name__iexact=name).first()

    if existing is None:
        existing = model.objects.create(name=name)
        cache.delete(cache_key)

    index[folded] = existing.id

    return existing.id


def import_failure_payload(blocked, failed):
    """The failures in a shape the report builder can use without the file.

    Only the refused rows are kept, so this stays small however large the
    upload was.
    """

    payload = []

    for group in blocked:
        payload.append({
            "label": group.label,
            "problems": [
                {"row": problem.row_number, "message": problem.message}
                for problem in group.problems
                if problem.blocking
            ],
            "values": {
                row["_row"]: {
                    key: value
                    for key, value in row.items()
                    if key != "_row"
                }
                for row in group.rows
            },
        })

    # Groups that passed the checks but were still refused by the database.
    for failure in failed:
        payload.append({
            "label": failure["label"],
            "problems": [
                {"row": row, "message": failure["message"]}
                for row in failure["rows"]
            ],
            "values": {},
        })

    return payload


@role_required("Admin", "Librarian")
def book_import_errors(request):
    """The last import's failures as a workbook, so the file can be fixed.

    Built from what the session kept about that import rather than from a
    fresh read, because by now the upload itself is gone.
    """

    failures = request.session.get(IMPORT_FAILURE_KEY) or []

    if not failures:
        messages.info(request, "There is no error report to download.")
        return redirect("book_import")

    return workbook_response(
        book_excel.build_error_report(failures),
        "madrasah-library-import-errors.xlsx",
    )


def normalize_title(value):
    """A title reduced to what a duplicate check should compare.

    Case, leading and trailing space, and runs of whitespace are accidents
    of typing rather than different books. Nothing else is touched:
    punctuation and diacritics distinguish real titles, especially in
    Arabic and Urdu, and folding them away would merge records that are
    genuinely different.
    """

    return " ".join((value or "").split()).lower()


def find_duplicate_books(title, author_id, exclude_id=None):
    """Existing books that are this same book, by title and author.

    Narrowed on `author_id` first, which `idx_books_author_id` covers, so
    the normalising expression only ever runs over that one author's books
    instead of the catalogue. One query, and it returns matches rather than
    rows to sift in Python.

    The normalising is done in SQL so it matches `normalize_title` exactly:
    collapse runs of whitespace, trim the ends, lower the case. `lower()`
    is used on both sides rather than Python's `casefold`, so the two
    cannot disagree.

    Deliberately narrow. The same title under a *different* author is left
    alone - a translation, a commentary, another author's work of the same
    name - and so is a similar-but-not-identical title. Blocking those
    would cost more in refused legitimate records than it saves.
    """

    normalized = normalize_title(title)

    if not normalized or not author_id:
        return Book.objects.none()

    matches = Book.objects.filter(
        author_id=author_id
    ).annotate(
        normalized_title=models.Func(
            models.Func(
                models.Func(
                    models.F("title"),
                    models.Value(r"\s+"),
                    models.Value(" "),
                    models.Value("g"),
                    function="regexp_replace",
                ),
                function="btrim",
            ),
            function="lower",
            output_field=models.CharField(),
        )
    ).filter(
        normalized_title=normalized
    )

    if exclude_id:
        matches = matches.exclude(id=exclude_id)

    return matches.select_related("author").order_by("id")


def duplicate_book_error(matches):
    """The message shown when a book is already on the shelf list."""

    return (
        "This book is already in the catalogue under the same author. "
        "Open the existing record instead of adding a second one, or "
        "change the title if this really is a different book."
        if len(matches) == 1 else
        "%d books with this title and author are already in the "
        "catalogue." % len(matches)
    )


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
    duplicates = []

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

        # Identity first: if this book is already on the shelves, nothing
        # about the cover or the volumes matters yet.
        duplicates = (
            list(find_duplicate_books(title, author_id)[:5])
            if title and author_id
            else []
        )

        if not title or not author_id:

            error = "Title and Author are required."

        elif duplicates:

            error = duplicate_book_error(duplicates)

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
        "duplicates": duplicates,
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
    duplicates = []

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

        # `exclude_id` is what stops a book being its own duplicate: save
        # it unchanged and the only match is itself, which is dropped.
        duplicates = (
            list(find_duplicate_books(title, author_id, exclude_id=book.id)[:5])
            if title and author_id
            else []
        )

        if not title or not author_id:

            error = "Title and Author are required."

        elif duplicates:

            error = duplicate_book_error(duplicates)

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
        "duplicates": duplicates,
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


def active_books():
    """The catalogue, without the books that have been archived.

    Every screen that means "the library's books" goes through this, so
    excluding archived ones is one decision in one place rather than a
    filter each caller has to remember. `idx_books_archived_at` is a
    partial index over the archived rows, which is the small side.
    """

    return Book.objects.filter(archived_at__isnull=True)


def book_archive_blocker(book):
    """Why `book` cannot be archived, or "" when it can be.

    Archiving is not destructive, but an archived book is out of the
    active catalogue, and a book cannot be out of the catalogue while
    somebody is holding a copy of it. So the one thing that stops it is an
    active loan - which is the same rule copy withdrawal follows, for the
    same reason.
    """

    issued = BookCopy.objects.filter(
        volume__book=book,
        id__in=Loan.objects.filter(
            return_date__isnull=True
        ).values("copy_id"),
    ).count()

    if issued:
        return (
            "This book cannot be archived because %d of its "
            "cop%s currently out on loan. Take %s back first."
            % (
                issued,
                "y is" if issued == 1 else "ies are",
                "it" if issued == 1 else "them",
            )
        )

    return ""


@role_required("Admin", "Librarian")
def book_archive(request, book_id):
    """Take a book out of the active catalogue, keeping everything.

    Nothing is deleted and nothing moves: the volumes, the copies, their
    codes, and every loan ever recorded against them stay exactly as they
    are. All that changes is one timestamp, and with it whether the book
    turns up in the catalogue.
    """

    book = get_object_or_404(Book, id=book_id)

    blocker = book_archive_blocker(book) if not book.is_archived else ""

    if request.method == "POST" and not blocker and not book.is_archived:

        book.archived_at = timezone.now()
        book.save(update_fields=["archived_at"])

        cache.delete(BOOK_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="ARCHIVE",
            entity_type="Book",
            entity_id=book.id,
            description="%s archived" % book.title,
        )

        return redirect("book_detail", book_id=book.id)

    return render(
        request,
        "library/book_archive.html",
        {
            "book": book,
            "blocker": blocker,
            "copy_count": BookCopy.objects.filter(volume__book=book).count(),
            "loan_count": Loan.objects.filter(copy__volume__book=book).count(),
        }
    )


@role_required("Admin", "Librarian")
def book_restore(request, book_id):
    """Put an archived book back into the active catalogue."""

    book = get_object_or_404(Book, id=book_id)

    if request.method == "POST" and book.is_archived:

        book.archived_at = None
        book.save(update_fields=["archived_at"])

        cache.delete(BOOK_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="RESTORE",
            entity_type="Book",
            entity_id=book.id,
            description="%s restored to the catalogue" % book.title,
        )

    return redirect("book_detail", book_id=book.id)


# The status a withdrawn copy carries. Deliberately one of the values the
# `check_copy_status` CHECK constraint already allows, and one the rest of
# the app already treats as out of circulation: `COPY_STORED_STATES` keeps
# it out of `copy_state`'s available/unshelved answers, and the issue form
# only ever offers copies marked Available. Inventing a "Withdrawn" value
# would mean altering that constraint and teaching every one of those
# places a second word for the same thing.
COPY_WITHDRAWN_STATUS = BookCopy.STATUS_TRANSFERRED


@role_required("Admin", "Librarian")
def book_copy_withdraw(request, copy_id):
    """Take a single copy out of circulation, keeping its history.

    Refused while the copy is out with a borrower: the loan is the record
    of where the book physically is, and marking it withdrawn underneath
    an open loan would leave the two disagreeing. Take it back first, then
    withdraw it.
    """

    copy = get_object_or_404(
        BookCopy.objects.select_related("volume__book", "shelf__location"),
        id=copy_id,
    )

    active_loan = Loan.objects.filter(
        copy=copy,
        return_date__isnull=True,
    ).select_related("borrower").first()

    already = (copy.status or "").casefold() in COPY_STORED_STATES

    blocker = ""

    if active_loan is not None:
        blocker = (
            "This copy cannot be withdrawn while it is out with %s. "
            "Take it back first." % active_loan.borrower.name
        )

    if request.method == "POST" and not blocker and not already:

        copy.status = COPY_WITHDRAWN_STATUS
        copy.save(update_fields=["status"])

        cache.delete(BOOK_COPY_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="WITHDRAW",
            entity_type="BookCopy",
            entity_id=copy.id,
            description="%s withdrawn from circulation" % copy.copy_code,
        )

        return redirect("book_copy_detail", copy_id=copy.id)

    return render(
        request,
        "library/book_copy_withdraw.html",
        {
            "copy": copy,
            "blocker": blocker,
            "already": already,
            "active_loan": active_loan,
            "withdrawn_status": COPY_WITHDRAWN_STATUS,
            "loan_count": Loan.objects.filter(copy=copy).count(),
        }
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
    #
    # It returns before the counting below: the dialog lists every copy
    # itself, so a per-volume count and a separate single-volume fetch
    # would be work it never shows.
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

    # Which section the page opens on. Only ever decides which pane is
    # marked active - every pane is rendered either way, so switching is
    # instant and nothing here changes what is fetched. It exists because
    # paging the loan history reloads the page, and landing back on
    # Overview would lose the reader's place; `_pagination.html` carries
    # every other query parameter through, so `tab` rides along with it.
    tab = request.GET.get("tab", "")

    if tab not in BOOK_DETAIL_TABS:
        # Loan history is the only pane that pages, and `_pagination.html`
        # is shared by a dozen pages so it cannot be taught to add `tab`.
        # A `page` with no tab named therefore means the loan history -
        # otherwise page 2 would arrive with Overview showing.
        tab = "loans" if request.GET.get("page") else BOOK_DETAIL_TABS[0]

    # How many copies each volume holds, so the page can offer the count as
    # the way through to them. Annotated rather than counted per row, so the
    # page costs the same whatever the book holds.
    volumes = list(
        volumes.annotate(copy_count=models.Count("bookcopy"))
    )

    # Every copy of the book, once. The availability summary, the copies
    # table, the shelf breakdown and a single-volume book's copy list are
    # all read off this one list rather than querying again per section.
    copies = describe_copies(
        BookCopy.objects.filter(
            volume__book=book
        ).select_related(
            "volume",
            "shelf__location",
        ).order_by(
            "volume__volume_number",
            "copy_code",
        )
    )

    # A book with one volume has it because a copy must hang off one, not
    # because anyone asked for volumes. Listing its copies here saves a hop
    # through a page that would only ever show the same thing under a
    # heading that means nothing to the librarian.
    single_volume = volumes[0] if len(volumes) == 1 else None

    volume_copies = copies if single_volume else []

    # Counted in Python off the list above: the states are derived (a copy
    # is issued because a loan says so, overdue because of its due date),
    # so there is nothing to group by in SQL that would not disagree with
    # what the copy list shows.
    tally = {}

    for copy in copies:
        tally[copy.state] = tally.get(copy.state, 0) + 1

    availability = [
        {
            "state": state,
            "label": COPY_STATE_LABELS[state],
            "tone": COPY_STATE_TONES[state],
            "count": tally.get(state, 0),
        }
        for state in COPY_STATE_ORDER
        # Available, issued and overdue are the question being asked, so
        # they show even at nought; the rest only when there are any.
        if tally.get(state, 0) or state in COPY_STATE_ALWAYS_SHOWN
    ]

    # Where the copies actually are. Off the same list, so it costs
    # nothing, and it answers "which shelves do I walk to" - which the
    # per-copy rows can only answer one row at a time.
    placements = {}

    for copy in copies:

        # `None` groups the unshelved together, and sorts before any id.
        key = copy.shelf_id

        if key not in placements:
            placements[key] = {
                "shelf_id": key,
                "location": copy.shelf.location.name if copy.shelf else "",
                "shelf_code": copy.shelf.shelf_code if copy.shelf else "",
                "count": 0,
            }

        placements[key]["count"] += 1

    shelf_summary = sorted(
        placements.values(),
        key=lambda entry: (entry["location"], entry["shelf_code"]),
    )

    # Contents hang off volumes, not off the book, so the page offers the
    # way in per volume rather than inventing a book-level list. One query
    # for every volume's count.
    content_counts = dict(
        BookContent.objects.filter(
            volume__book=book
        ).values_list("volume_id").annotate(
            total=models.Count("id")
        )
    )

    for volume in volumes:
        volume.content_count = content_counts.get(volume.id, 0)

    # Everything this book has ever been out on, newest first, a page at a
    # time: a popular book's history only grows.
    history = Loan.objects.filter(
        copy__volume__book=book
    ).select_related(
        "borrower",
        "copy__volume",
        "issued_by",
        "returned_to",
    ).order_by("-issue_date", "-id")

    paginator = Paginator(history, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    describe_loans(page.object_list)

    return render(
        request,
        "library/book_detail.html",
        {
            "book": book,
            "volumes": volumes,
            "single_volume": single_volume,
            "volume_copies": volume_copies,
            "copies": copies,
            "availability": availability,
            "total_copies": len(copies),
            "shelf_summary": shelf_summary,
            "content_total": sum(content_counts.values()),
            "loans": page,
            "paginator": paginator,
            "tab": tab,
            "can_edit": can_edit_library(request.user),
            # Who is waiting for this book. The count is worth having on
            # every tab - it belongs beside the availability summary - and
            # the queue itself only when it is being looked at.
            "reservation_count": reservations.active_count(book),
            "reservation_queue": (
                reservations.active_for_book(book)
                if tab == "reservations" else []
            ),
            # Off the tally above, which is already built from the
            # copies this page fetched - so saying "some are on the shelf"
            # costs no query, and it is the derived state the availability
            # summary shows rather than a second opinion from the status
            # column.
            "copies_available_now": tally.get("available", 0),
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


def isolated(text):
    """`text` wrapped so it cannot reorder the sentence around it.

    Book and volume titles here are mostly Urdu and Arabic, and dropping
    right-to-left text into the middle of an English sentence makes the
    bidirectional algorithm reshuffle the words either side of it - the
    message becomes hard to read even though every character is correct.
    U+2068/U+2069 are the Unicode isolate pair: they say "treat this run as
    one opaque item", which is exactly what a title is.
    """

    return "⁨%s⁩" % text


def read_volume_form(request, exclude_id=None):
    """The volume a form is asking for, and any complaint about it.

    Returns `(book, number, title, error)`, where `error` is "" when the
    values are usable. `number` comes back as whatever was typed when it is
    the thing being complained about, so the form can show it again rather
    than blanking the field.

    Shared by add and edit so the two cannot drift apart. Every check here
    stands in front of something the database would otherwise refuse:

      * `unique_book_volume` is a real UNIQUE constraint on
        (book_id, volume_number), so a repeat used to reach Postgres and
        come back as an unhandled IntegrityError - a 500 where the
        librarian only needed to be told the number was taken.
      * `volume_number` is an integer column, so "abc" was a 500 too.
      * `book_id` is NOT NULL with a foreign key, so an id that does not
        exist was a third.

    Pass `exclude_id` when editing, so a volume keeping its own number is
    not treated as clashing with itself.
    """

    book_id = (request.POST.get("book") or "").strip()
    raw_number = (request.POST.get("volume_number") or "").strip()
    title = request.POST.get("title", "").strip()

    # Resolved as early as possible: an error page that has lost the
    # librarian's book selection is its own small annoyance.
    book = (
        Book.objects.filter(id=book_id).first()
        if book_id.isdigit()
        else None
    )

    if not book_id or not raw_number or not title:
        return book, raw_number, title, "Please fill in all required fields."

    try:
        number = int(raw_number)
    except ValueError:
        return book, raw_number, title, (
            "Volume number must be a whole number, like 1 or 2."
        )

    if number < 1:
        return book, raw_number, title, (
            "Volume number must be 1 or more."
        )

    if book is None:
        return None, raw_number, title, (
            "Please choose a book from the list."
        )

    clash = BookVolume.objects.filter(book=book, volume_number=number)

    if exclude_id is not None:
        clash = clash.exclude(id=exclude_id)

    existing = clash.first()

    if existing is not None:
        return book, raw_number, title, (
            "%s already has a Volume %d%s. Give this one a different "
            "volume number." % (
                isolated(book.title),
                number,
                " (%s)" % isolated(existing.title) if existing.title else "",
            )
        )

    return book, number, title, ""


@role_required("Admin", "Librarian")
def book_volume_add(request):

    books = Book.objects.all()

    selected_book_id = (request.GET.get("book") or "").strip()

    # The book itself, for the back link. Kept apart from the raw id above,
    # which only has to match an <option> value: `{% url %}` cannot be
    # guarded inside the template, so handing it an id that resolves to
    # nothing is a 500 -- which "?book=abc" used to be.
    book = (
        Book.objects.filter(id=selected_book_id).first()
        if selected_book_id.isdigit()
        else None
    )

    error = None
    volume_number = ""
    title = ""

    if request.method == "POST":

        book, volume_number, title, error = read_volume_form(request)

        # Whatever was typed goes back into the form, so a rejection never
        # costs the librarian their work.
        selected_book_id = (request.POST.get("book") or "").strip()

        if not error:

            try:

                # Its own savepoint: a failed insert leaves the connection
                # usable, so the page can still be rendered to explain
                # itself. TestCase wraps each test in a transaction, where
                # that matters even though requests are not atomic here.
                with transaction.atomic():

                    volume = BookVolume.objects.create(
                        book=book,
                        volume_number=volume_number,
                        title=title,
                    )

            except IntegrityError:

                # Only reachable if someone else created the same volume
                # between the check above and this insert. The constraint is
                # the real guarantee; this turns losing that race into the
                # same sentence rather than a 500.
                error = (
                    "%s already has a Volume %s. Give this one a "
                    "different volume number."
                    % (isolated(book.title), volume_number)
                )

        if not error:

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
                book_id=book.id
            )

    return render(
        request,
        "library/book_volume_add.html",
        {
            "books": books,
            "selected_book_id": selected_book_id,
            "selected_book": book,
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

    copies = describe_copies(
        BookCopy.objects.select_related(
            "shelf",
            "shelf__location",
        ).filter(
            volume=volume
        ).order_by("copy_code")
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
            "volume_name": volume_label(volume),
            "copies": copies,
            "contents": contents,
            "can_edit": can_edit_library(request.user),
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

    error = ""

    if request.method == "POST":

        # `exclude_id`: a volume keeping the number it already has is not
        # clashing with itself.
        book, volume_number, title, error = read_volume_form(
            request,
            exclude_id=volume.id,
        )

        # Put what was typed onto the in-memory volume so the form shows it
        # again on the way back. Nothing is written unless it validates, so
        # the stored record is untouched on every error path below.
        if book is not None:
            volume.book = book

        volume.volume_number = volume_number
        volume.title = title

        if not error:

            try:

                # Its own savepoint, for the same reason as on the add
                # page: someone else can take the number between the check
                # and the write, and losing that race should read as a
                # sentence rather than a 500.
                with transaction.atomic():
                    volume.save()

            except IntegrityError:

                error = (
                    "%s already has a Volume %s. Give this one a "
                    "different volume number."
                    % (isolated(book.title), volume_number)
                )

        if not error:

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
            "error": error,
        }
    )


@role_required("Admin", "Librarian")
def book_volume_delete(request, volume_id):

    volume = get_object_or_404(BookVolume, id=volume_id)

    from_page = request.GET.get(
        "from",
        request.POST.get("from", "")
    )

    # `book_copies.volume_id` is a NO ACTION foreign key and BookCopy.volume
    # is DO_NOTHING, so Django neither cascades nor nullifies: deleting a
    # volume that still has copies reached Postgres and came back as an
    # unhandled IntegrityError. Same guard the shelf and copy delete pages
    # already use.
    copies_exist = BookCopy.objects.filter(
        volume_id=volume.id
    ).exists()

    if request.method == "POST":

        if copies_exist:

            return render(
                request,
                "library/book_volume_delete.html",
                {
                    "volume": volume,
                    "from_page": from_page,
                    "copies_exist": True,
                }
            )

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
            "copies_exist": copies_exist,
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

COPY_LIST_FRAGMENTS = {
    "results": "library/partials/copy_list_results.html",
}


def copy_list_fragment(request):
    """The copy-list fragment this request asks for, or "" for the page.

    Only one so far: the results, which the page swaps in after copies are
    moved so the current search, filters, sorting and page survive. Same
    shape as `book_list_fragment`, including the navigation guard.

    A navigation wins over the parameter. The container these fragments are
    swapped into declares `partial` once, for the sort, filter and paging
    links inside it, and htmx hands that down to everything within -
    including a plain link to another page, now that the shell boosts those.
    Asking to replace the main-content region is asking for a page, so that
    is what such a request gets, whatever it inherited on the way.
    """

    if is_main_nav_request(request):
        return ""

    if request.headers.get("HX-Request") != "true":
        return ""

    return COPY_LIST_FRAGMENTS.get(request.GET.get("partial", ""), "")


# What the copy list was asked to show: the queryset, and the filters as
# they were read, so a caller can put them back on the page.
CopyFilters = namedtuple(
    "CopyFilters",
    "queryset search state book_id volume_id location_id shelf_id",
)


def filtered_copies(request, today=None):
    """The copies matching the copy list's filters.

    Lifted out of `book_copy_list` unchanged, so the label sheet can print
    exactly what the librarian is looking at rather than reimplementing the
    same six filters and drifting from them. The list still reads the
    parameters it always did.
    """

    if today is None:
        today = timezone.now().date()

    # `search` covers both things a librarian has to hand: the code printed
    # on the book, and its title. `copy_code` is still honoured so links
    # made before the two were merged keep working.
    search = (
        request.GET.get("search")
        or request.GET.get("copy_code")
        or ""
    ).strip()

    # The state filter, which understands both the derived names and the
    # capitalised column values older links used.
    state = request.GET.get("status", "").strip().casefold()

    if state not in COPY_STATE_FILTERS:
        state = ""

    book_id = numeric_param(request, "book")
    volume_id = numeric_param(request, "volume")
    location_id = numeric_param(request, "location")
    shelf_id = numeric_param(request, "shelf")

    copies = BookCopy.objects.select_related(
        "volume__book",
        "shelf__location",
    )

    if search:
        copies = copies.filter(
            Q(copy_code__icontains=search)
            | Q(volume__book__title__icontains=search)
        )

    if book_id:
        copies = copies.filter(volume__book_id=book_id)

    if volume_id:
        copies = copies.filter(volume_id=volume_id)

    if location_id:
        copies = copies.filter(shelf__location_id=location_id)

    if shelf_id:
        copies = copies.filter(shelf_id=shelf_id)

    copies = filter_copies_by_state(copies, state, today)

    return CopyFilters(
        copies, search, state, book_id, volume_id, location_id, shelf_id
    )


def book_copy_list(request):
    """Every physical copy, with where it is and what it is doing.

    Reachable on its own and from a book or a volume (`?book=` / `?volume=`
    still filter, as they always did, so the links from those pages keep
    working).
    """

    today = timezone.now().date()

    (
        copies,
        search,
        state,
        book_id,
        volume_id,
        location_id,
        shelf_id,
    ) = filtered_copies(request, today)

    sort, direction = resolve_sort(
        request,
        COPY_SORT_FIELDS,
        COPY_SORT_DEFAULT,
    )

    copies = copies.order_by(
        *sort_ordering(COPY_SORT_FIELDS, sort, direction)
    )

    page_size = resolve_page_size(request)

    paginator = Paginator(copies, page_size)
    page = paginator.get_page(request.GET.get("page"))

    # Loans for this page only. Fetching every active loan in the library,
    # as this used to, costs more with every book that goes out.
    describe_copies(page.object_list, today)

    columns = sortable_columns(
        request,
        [
            ("code", "Copy Code"),
            ("book", "Book"),
            ("volume", "Volume"),
            ("location", "Location"),
            ("shelf", "Shelf"),
            ("status", "Status"),
        ],
        COPY_SORT_FIELDS,
        sort,
        direction,
    )

    # One chip per active filter, so nothing narrows the list invisibly —
    # including `?book=` and `?volume=`, which arrive from another page and
    # have no control of their own here.
    active_filters = [
        {"label": label, "value": value, "url": "?" + query_with(
            request,
            page=None,
            **{param: None for param in params}
        )}
        for label, params, value in (
            ("Search", ("search", "copy_code"), search),
            ("Status", ("status",), COPY_STATE_LABELS.get(state, "")),
            ("Book", ("book",), (
                Book.objects.filter(id=book_id)
                .values_list("title", flat=True).first() or ""
            ) if book_id else ""),
            ("Volume", ("volume",), volume_label(
                BookVolume.objects.filter(id=volume_id).first()
            ) if volume_id else ""),
            ("Location", ("location",), selected_name(Location, location_id)),
            ("Shelf", ("shelf", "location"), (
                Shelf.objects.filter(id=shelf_id).first().shelf_code
                if shelf_id and Shelf.objects.filter(id=shelf_id).exists()
                else ""
            )),
        )
        if value
    ]

    context = {
        "copies": page,
        "paginator": paginator,
        "search": search,
        "state": state,
        "state_options": copy_state_options(state),
        "book_id": book_id,
        "volume_id": volume_id,
        "location_id": location_id,
        "shelf_id": shelf_id,
        "active_filters": active_filters,
        # Locations and shelves are as many as the building has, so a plain
        # dropdown is right for them. Shelves are the ones for the chosen
        # location only — offering the rest would let someone file a copy
        # somewhere it cannot be.
        "locations": Location.objects.order_by("name"),
        "shelves": shelf_options_for(location_id),
        "columns": columns,
        "sort": sort,
        "direction": direction,
        "page_size": page_size,
        "page_size_options": page_size_options(page_size),
        "page_size_hx_url": "?" + query_with(
            request,
            page=None,
            page_size=None,
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
        # UI gating only: the move and edit controls are enforced by
        # `role_required` on the views themselves.
        "can_edit": can_edit_library(request.user),
    }

    fragment = copy_list_fragment(request)

    if fragment:

        response = render(request, fragment, context)

        # Same reasoning as the book list: the address bar should show the
        # plain URL, and a background redraw should not add a history step.
        if not request.GET.get("refresh"):

            query = query_with(request)
            response["HX-Push-Url"] = (
                request.path + "?" + query if query else request.path
            )

        return response

    return render(
        request,
        "library/book_copy_list.html",
        context,
    )


# A sheet of labels is a sheet of paper. Past this many the page has
# stopped being something anyone is about to print and started being a way
# to render the whole catalogue by accident.
LABEL_LIMIT = 200


def labelled(copies):
    """Attach the barcode each copy's label will carry.

    The value encoded is `copy_code` and nothing else - the same string the
    issue and return workflows scan, so a label made here reads back as the
    copy it names. There is no second identifier and nothing to migrate.

    A code that Code 128 cannot hold gets no barcode rather than a wrong
    one; the label still prints with the code as text, which is what a
    librarian would read out anyway. Only a hand-typed code can get into
    that state - generated ones are `LIB-` and digits.
    """

    for copy in copies:

        try:
            copy.label_barcode = mark_safe(barcode.svg(copy.copy_code))

        except barcode.BarcodeError:
            copy.label_barcode = None

    return copies


@role_required("Admin", "Librarian")
def book_copy_labels(request):
    """A printable sheet of labels for the copies asked for.

    Two ways in, both GET, so a sheet can be reloaded, kept as a bookmark
    and printed again without rebuilding the selection:

      * `copy=` repeated - the copies ticked on the list, or the single
        one on a copy's own page;
      * the copy list's own filters, which print everything the librarian
        is currently looking at rather than only the page on screen.

    Every id is read back from the database, so a hand-made request prints
    labels for the copies that exist and silently nothing for the rest -
    there is no way to have this render a code of the caller's choosing.

    Behind the same roles as the rest of copy management: printing a label
    is an inventory job, and the list only offers it to those roles.
    """

    ids = [
        int(value)
        for value in request.GET.getlist("copy")
        if value.isdigit()
    ]

    if ids:
        copies = BookCopy.objects.filter(id__in=ids)
        scope = "selected"

    elif request.GET.get("all") == "1":
        # Everything the filters describe, which is what the link beside
        # the result count asks for.
        copies = filtered_copies(request).queryset
        scope = "filtered"

    else:
        # Neither: an empty sheet and a note saying how to fill it. The
        # two intents are marked rather than guessed, so a request that
        # ticked nothing prints nothing instead of printing the catalogue.
        copies = BookCopy.objects.none()
        scope = "empty"

    copies = copies.select_related(
        "volume__book__author",
        "shelf__location",
    ).order_by("copy_code")

    total = copies.count()
    copies = labelled(list(copies[:LABEL_LIMIT]))

    return render(
        request,
        "library/book_copy_labels.html",
        {
            "copies": copies,
            "total": total,
            "shown": len(copies),
            "capped": total > LABEL_LIMIT,
            "limit": LABEL_LIMIT,
            "scope": scope,
            "printed_on": timezone.now(),
        },
    )


@role_required("Admin", "Librarian")
def book_copy_bulk_move(request):
    """Put several copies on one shelf at once.

    The same operation `book_copy_move` performs for a single copy — set
    the shelf, clear the caches, log it — repeated inside one transaction
    so a batch either lands completely or not at all.

    Copies that are out on loan are moved like any other: that is the rule
    `book_copy_move` has always followed, and it is not this task's to
    change. What the shelf records is where the copy belongs.
    """

    if request.method != "POST":
        return HttpResponseBadRequest("POST required")

    ids = [
        value
        for value in request.POST.getlist("copy")
        if value.isdigit()
    ]

    location_id = request.POST.get("location", "").strip()
    shelf_id = request.POST.get("shelf", "").strip()

    def failed(message):
        if is_options_request(request) or request.headers.get("HX-Request"):
            return copies_moved_response(0, message)

        messages.error(request, message)
        return redirect(safe_redirect_target(request, "book_copy_list"))

    if not ids:
        return failed("Select the copies to move first.")

    if not location_id.isdigit() or not shelf_id.isdigit():
        return failed("Choose a location and a shelf to move them to.")

    # The shelf has to be on the location that was chosen. The dropdowns
    # only offer matching pairs, but that is the browser's word for it.
    shelf = Shelf.objects.filter(
        id=shelf_id,
        location_id=location_id,
    ).select_related("location").first()

    if shelf is None:
        return failed("That shelf is not in the location you chose.")

    copies = list(
        BookCopy.objects.filter(id__in=ids).select_related(
            "shelf__location"
        )
    )

    if not copies:
        return failed("Those copies no longer exist.")

    moved = []

    with transaction.atomic():

        for copy in copies:

            if copy.shelf_id == shelf.id:
                # Already there. Saving it again would only add a log entry
                # describing a move that did not happen.
                continue

            previous = copy.shelf

            copy.shelf = shelf
            copy.save(update_fields=["shelf"])

            moved.append((copy, previous))

        for copy, previous in moved:
            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="BookCopy",
                entity_id=copy.id,
                description=(
                    f"{copy.copy_code} moved from "
                    f"{previous if previous else 'no shelf'} "
                    f"to {shelf}"
                ),
            )

    if moved:
        cache.delete(BOOK_COPY_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

    if request.headers.get("HX-Request"):
        return copies_moved_response(len(moved), shelf=shelf)

    if moved:
        messages.success(
            request,
            f"{len(moved)} cop{'y' if len(moved) == 1 else 'ies'} "
            f"moved to {shelf}.",
        )
    else:
        messages.info(request, f"Those copies are already on {shelf}.")

    return redirect(safe_redirect_target(request, "book_copy_list"))


def copies_moved_response(count, error="", shelf=None):
    """Tell the page how a move went, so it can redraw and confirm.

    "No content" plus an event: what should change is the copy list, which
    reloads its own results fragment and so keeps the current search,
    filters, sorting and page.
    """

    response = HttpResponse(status=204)

    if error:
        response["HX-Trigger"] = json.dumps({
            "copiesMoveFailed": {"message": error}
        })

        return response

    response["HX-Trigger"] = json.dumps({
        "copiesMoved": {
            "count": count,
            "shelf": str(shelf) if shelf else "",
        }
    })

    return response


def book_copy_detail(request, copy_id):
    """One physical copy: where it is, and what it is doing.

    The list opens this in a dialog rather than navigating, so the same
    view serves a fragment for that — same permissions, same queries.
    """

    from_page = request.GET.get(
        "from",
        ""
    )

    copy = get_object_or_404(
        BookCopy.objects.select_related(
            "volume__book",
            "volume__book__author",
            "shelf__location",
        ),
        id=copy_id
    )

    # Fills in `active_loan`, `days_overdue` and the derived state, so the
    # word shown here is the same one the list showed.
    describe_copies([copy])

    active_loan = copy.active_loan

    loan_history = Loan.objects.filter(
        copy=copy
    ).select_related(
        "borrower",
        "issued_by",
        "returned_to",
    ).order_by(
        "-issue_date"
    )

    # When this copy was last accounted for, and whether it turned up.
    # Two queries, on the page about that one copy - deliberately not on
    # the copy list, where the same question would be a cost per row.
    last_check, last_check_found = inventory.last_completed_check(copy)

    # How the copy got to where it is: read from the loans, the activity
    # log and the stock checks, never stored. Four queries whatever the
    # length of it, and paginated because a copy lent for twenty years has
    # a long one.
    timeline = history.copy_timeline(copy)

    history_pages = Paginator(timeline, PAGE_SIZE)
    history_page = history_pages.get_page(request.GET.get("history"))

    context = {
        "copy": copy,
        "active_loan": active_loan,
        "loan_history": loan_history,
        "from_page": from_page,
        "can_edit": can_edit_library(request.user),
        "last_check": last_check,
        "last_check_found": last_check_found,
        "history": history_page,
        "history_total": history_pages.count,
    }

    if is_modal_request(request):
        # Deliberately not the whole loan history: the dialog answers
        # "where is it and who has it", and links to the loan for the rest.
        return render(
            request,
            "library/partials/copy_detail_modal.html",
            context,
        )

    return render(
        request,
        "library/book_copy_detail.html",
        context,
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

    # Where it is now, so the Location dropdown opens on it.
    location_id = str(copy.shelf.location_id) if copy.shelf_id else ""

    error_message = ""

    if request.method == "POST":

        location_id = request.POST.get("location", "").strip()
        new_shelf_id = request.POST.get("shelf", "").strip()

        new_shelf = None

        if not new_shelf_id.isdigit() or not location_id.isdigit():

            error_message = "Choose a location and a shelf."

        else:
            # The shelf has to be on the location chosen alongside it. The
            # dropdowns only offer matching pairs, but that is the
            # browser's word for it.
            new_shelf = Shelf.objects.filter(
                id=new_shelf_id,
                location_id=location_id,
            ).select_related("location").first()

            if new_shelf is None:
                error_message = (
                    "That shelf is not in the location you chose."
                )

        if not error_message:

            old_shelf = copy.shelf

            copy.shelf = new_shelf
            copy.save(update_fields=["shelf"])

            cache.delete(BOOK_COPY_CACHE_KEY)
            cache.delete(SHELF_CACHE_KEY)
            cache.delete(LOCATION_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=request.user,
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
            "volume_name": volume_label(copy.volume),
            "locations": Location.objects.order_by("name"),
            "shelves": shelf_options_for(location_id),
            "location_id": location_id,
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
                SHELF_CACHE_KEY
            )
            cache.delete(
                LOCATION_CACHE_KEY
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
    """Manage one physical copy: where it sits, and its condition.

    Copy-level only. Nothing here reads or writes the Book or the
    BookVolume, so the title, author, category and every other copy are
    untouched by anything done on this page.

    Two things it deliberately does not offer:

      * the copy code, which is printed on the book itself and quoted in
        past activity-log entries — retyping it would leave the label, the
        record and the shelf disagreeing. It is set when the copy is
        created and read-only after that.
      * moving the copy to another volume, which would silently change
        which book a past loan appears to have been for. Wrongly-filed
        copies are a delete-and-re-add, not an edit.
    """

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

    statuses = [
        "Available",
        "Lost",
        "Damaged",
        "Missing",
        "Transferred",
    ]

    # A copy that is out stays "Issued": the loan is what says so, and the
    # return is what changes it. Same rule this view has always applied.
    is_issued = copy.status == "Issued"

    error_message = ""

    # Where it is now, so Location opens on it and Shelf can be narrowed
    # to that location's shelves.
    location_id = str(copy.shelf.location_id) if copy.shelf_id else ""

    form_data = {
        "location_id": location_id,
        "shelf_id": copy.shelf_id,
        "status": copy.status,
        "acquisition_date": (
            copy.acquisition_date.strftime("%Y-%m-%d")
            if copy.acquisition_date
            else ""
        ),
        "notes": copy.notes or "",
    }

    if request.method == "POST":

        location_id = request.POST.get("location", "").strip()
        shelf_id = request.POST.get("shelf", "").strip()

        acquisition_date = request.POST.get("acquisition_date")
        notes = request.POST.get("notes", "").strip()

        status = "Issued" if is_issued else request.POST.get(
            "status",
            "Available",
        )

        if status not in statuses and not is_issued:
            status = "Available"

        form_data = {
            "location_id": location_id,
            "shelf_id": int(shelf_id) if shelf_id.isdigit() else None,
            "status": status,
            "acquisition_date": acquisition_date or "",
            "notes": notes,
        }

        shelf = None

        if shelf_id.isdigit():
            # The shelf has to be on the location chosen alongside it. The
            # dropdowns only offer matching pairs, but that is the
            # browser's word for it.
            shelf = Shelf.objects.filter(
                id=shelf_id,
                location_id=location_id if location_id.isdigit() else None,
            ).select_related("location").first()

            if shelf is None:
                error_message = (
                    "That shelf is not in the location you chose."
                )

        if not error_message:

            # What it was, read before it is overwritten. The move views
            # have always recorded a shelf change as "from A to B"; this
            # one recorded only the result, which said where the copy
            # ended up and nothing about where it had been. Captured here
            # so the copy's history can say both.
            was_shelf = copy.shelf
            was_status = copy.status
            was_details = (copy.acquisition_date, copy.notes)

            # `book_copies.shelf_id` is nullable, and a copy that has
            # arrived but not been placed yet is a real state the list can
            # find, so clearing the shelf is allowed.
            copy.shelf = shelf
            copy.status = status
            copy.acquisition_date = acquisition_date or None
            copy.notes = notes or None

            copy.save(update_fields=[
                "shelf",
                "status",
                "acquisition_date",
                "notes",
            ])

            cache.delete(BOOK_COPY_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            # One entry per thing that actually moved, and none at all for
            # a value that was resubmitted unchanged - an edit that only
            # touched the notes should not read as a shelf change in the
            # history, and saving the form without altering anything
            # should not read as anything.
            #
            # Only from here on. Nothing reconstructs the changes made
            # before this recorded them; a copy's timeline says what is
            # known rather than guessing what is not.
            if copy.shelf_id != (was_shelf.id if was_shelf else None):
                create_activity_log(
                    user=request.user,
                    action="UPDATE",
                    entity_type="BookCopy",
                    entity_id=copy.id,
                    description=(
                        f"{copy.copy_code} moved from "
                        f"{was_shelf if was_shelf else 'no shelf'} "
                        f"to {shelf if shelf else 'no shelf'}"
                    ),
                )

            if copy.status != was_status:
                create_activity_log(
                    user=request.user,
                    action="UPDATE",
                    entity_type="BookCopy",
                    entity_id=copy.id,
                    description=(
                        f"{copy.copy_code} status changed from "
                        f"{was_status} to {copy.status}"
                    ),
                )

            # The rest of the form. Recorded generally, because a note or
            # an acquisition date is not a movement and spelling out its
            # before and after would put the note's whole text in the
            # history twice. Still attributed and still dated, which is
            # what an edit needs to be answerable for.
            if (copy.acquisition_date, copy.notes) != was_details:
                create_activity_log(
                    user=request.user,
                    action="UPDATE",
                    entity_type="BookCopy",
                    entity_id=copy.id,
                    description="%s details updated" % copy.copy_code,
                )

            if from_page == "shelf" and copy.shelf_id:
                return redirect("shelf_detail", shelf_id=copy.shelf_id)

            if from_page == "volume":
                return redirect(
                    "book_volume_detail",
                    volume_id=copy.volume_id,
                )

            return redirect("book_copy_detail", copy_id=copy.id)

    return render(
        request,
        "library/book_copy_edit.html",
        {
            "copy": copy,
            "volume_name": volume_label(copy.volume),
            "locations": Location.objects.order_by("name"),
            "shelves": shelf_options_for(form_data["location_id"]),
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
        cache.delete(SHELF_CACHE_KEY)
        cache.delete(LOCATION_CACHE_KEY)
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


# How many matching copies the issue form offers at once. A librarian
# picking a copy is looking for one they can name; a hundred rows is not an
# answer, it is the problem the search was meant to solve.
COPY_LOOKUP_LIMIT = 25


def issuable_copies():
    """The copies that may be issued, decided in one place.

    Marked Available - which the return workflow is what clears, and which
    `unique_active_loan_per_copy` backs - and belonging to a book that is
    still in the catalogue. Withdrawn copies fail the first test and
    archived books the second, so the form and the POST that follows it
    cannot disagree about what is lendable.
    """

    return BookCopy.objects.filter(
        status=BookCopy.STATUS_AVAILABLE,
        volume__book__archived_at__isnull=True,
    )


def copy_selection_url(request, copy_ids, clear_search=False):
    """This page with `copies` set to `copy_ids`, keeping the rest.

    The chosen copies live in the query string rather than in checkboxes,
    so searching again for the next one cannot lose the ones already
    picked - and the whole half-built issue is a URL, which survives a
    reload and can be handed to a colleague.

    `clear_search` drops the search term as well, which is what a scan
    wants: the code has been dealt with, and leaving it in the box would
    mean the next scan appends to it.
    """

    params = request.GET.copy()

    for key in INTERNAL_PARAMS:
        params.pop(key, None)

    if clear_search:
        params.pop("q", None)

    params.setlist("copies", [str(value) for value in copy_ids])

    return "?" + params.urlencode()


# What a whole copy code typed into the issue form turned out to mean.
# `copy` is None when the text was not a copy code at all, which is what
# lets the same field still search for a book title.
ScanOutcome = namedtuple("ScanOutcome", "copy message level add")


def copy_for_code(code):
    """The one copy carrying this exact code, or None.

    The targeted lookup every scan runs, named once so nothing has to write
    it again. `copy_code` is unique in the database
    (`book_copies_copy_code_key`), so this matches at most one row and its
    cost does not grow with the catalogue - which is the whole reason a
    scan is answered by a lookup rather than by a search.

    Matched case-insensitively, the same way the code generator checks for
    collisions, because a label read by a scanner and a label typed by hand
    should not be two different codes.
    """

    if not code:
        return None

    return BookCopy.objects.filter(
        copy_code__iexact=code
    ).select_related("volume__book").first()


def scan_issue_outcome(query, chosen_ids):
    """Resolve a whole copy code typed or scanned into the issue form.

    A barcode or QR scanner acting as a keyboard types a complete code and
    presses Enter. What should follow is the copy in the basket - not a
    table with one row in it and another click to make. So a code that
    names a copy is answered here, and anything else falls through to the
    ordinary search below.

    Every refusal is `issuable_copies()`, the one rule the form and the
    POST already share, so scanning cannot add a copy that picking it from
    the results could not: a copy already out, one withdrawn from
    circulation, or one whose book has been archived is refused with the
    reason rather than silently listed as nothing.

    Nothing is written here and nothing is trusted afterwards - the POST
    re-checks every copy under a lock, which is what makes this a
    convenience rather than a way in.
    """

    copy = copy_for_code(query)

    if copy is None:
        # Not a code. Let the search have it.
        return ScanOutcome(None, "", messages.INFO, False)

    if copy.id in chosen_ids:
        return ScanOutcome(
            copy,
            "%s is already in the list." % copy.copy_code,
            messages.INFO,
            False,
        )

    if not issuable_copies().filter(pk=copy.pk).exists():

        if copy.status == BookCopy.STATUS_ISSUED:
            reason = "is already out on loan"

        elif copy.status != BookCopy.STATUS_AVAILABLE:
            reason = (
                "is marked %s and is out of circulation" % copy.status.lower()
            )

        else:
            # Available, so what fails the rule is the book above it.
            reason = "belongs to a book that has been archived"

        return ScanOutcome(
            copy,
            "%s %s, so it cannot be issued." % (copy.copy_code, reason),
            messages.WARNING,
            False,
        )

    return ScanOutcome(
        copy,
        "%s added. Scan the next one." % copy.copy_code,
        messages.SUCCESS,
        True,
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
    today = timezone.now().date()

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
            due_date__lt=today
        )

    elif status == "due_today":

        loans_query = loans_query.filter(
            return_date__isnull=True,
            due_date=today
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
    """Issue one or more book copies to a borrower.

    GET: show the issue form with available copies and active borrowers.
    POST: validate borrower + copies + due_date, then issue atomically — all
    copies succeed or none are issued, enforced by transaction + select_for_update.
    """

    # Which copies the librarian has picked so far, and what they are
    # searching for now. Nothing is listed until something is asked for:
    # this page used to render every available copy in the library, which
    # was several hundred rows of table for a form whose answer is one or
    # two of them.
    query = (request.GET.get("q") or "").strip()

    chosen_ids = [
        int(value)
        for value in request.GET.getlist("copies")
        if value.isdigit()
    ]

    lendable = issuable_copies().select_related(
        # The author too: the match rows name it so the librarian can tell
        # two books with similar titles apart, and without it that was a
        # query per row.
        "volume__book__author",
        "shelf__location",
    )

    # Re-read rather than trusted: a copy chosen a minute ago may have been
    # issued to somebody else since, and it drops out here if so.
    chosen = list(
        lendable.filter(id__in=chosen_ids).order_by("copy_code")
    ) if chosen_ids else []

    chosen_ids = [copy.id for copy in chosen]

    # A whole code is resolved before anything is searched for, because
    # that is what a scanner sends. The answer comes back as a redirect
    # rather than a rendered page, which is what empties the search box and
    # leaves the cursor ready for the next scan - and keeps the borrower
    # and the basket, since both travel in the URL being redirected to.
    #
    # A message rather than a rewritten page, so a refusal is as visible as
    # a success and neither costs the librarian their basket.
    if request.method == "GET" and query:

        scan = scan_issue_outcome(query, chosen_ids)

        if scan.copy is not None:

            messages.add_message(request, scan.level, scan.message)

            # Only a copy that actually went in redirects. That is what
            # empties the box and leaves the cursor ready for the next one,
            # and the new basket has to reach the URL for a reload to keep
            # it.
            #
            # A refusal or a repeat stays on this page instead, with the
            # code still in the box: the librarian has a book in their
            # hand and needs to see which one was turned down. Nothing was
            # added, so there is no new state to put in the URL.
            if scan.add:
                return redirect(
                    request.path
                    + copy_selection_url(
                        request,
                        chosen_ids + [scan.copy.id],
                        clear_search=True,
                    )
                )

    matches = list(
        lendable.filter(
            Q(copy_code__icontains=query)
            | Q(volume__book__title__icontains=query)
            | Q(volume__book__author__name__icontains=query)
        ).exclude(
            id__in=chosen_ids
        ).order_by("copy_code")[:COPY_LOOKUP_LIMIT + 1]
    ) if query else []

    more_matches = len(matches) > COPY_LOOKUP_LIMIT
    matches = matches[:COPY_LOOKUP_LIMIT]

    for copy in matches:
        copy.add_url = copy_selection_url(request, chosen_ids + [copy.id])

    for copy in chosen:
        copy.remove_url = copy_selection_url(
            request,
            [value for value in chosen_ids if value != copy.id],
        )

    # Who is waiting for the books in the basket, so the form can say
    # before the librarian tries. The rule itself is applied in the POST -
    # this is the same answer shown early, not the place it is decided.
    # One query for the whole basket.
    queues = reservations.queues_for_copies(chosen)

    for copy in chosen:
        copy.queue_front, copy.queue_length = queues.get(
            copy.volume.book_id, (None, 0)
        )

    # The borrower is chosen through the searchable dropdown, which asks
    # `borrower_list` for its suggestions as you type. So this page no
    # longer loads every active borrower to fill a <select> - it only needs
    # the name of the one already chosen, to show it back.
    borrower_id = (
        request.POST.get("borrower")
        or request.GET.get("borrower")
        or ""
    )

    if request.method == "GET":

        # A borrower in the query string is a link from somewhere else - the
        # borrower's own page offers one. Resolved against the same rule the
        # POST below enforces, so an inactive borrower's id arrives with
        # nothing chosen: the alternative is a form that can be filled in
        # completely and then refused at the last step. The policy itself is
        # unchanged; this only decides what a link may preselect.
        preselected = (
            Borrower.objects.filter(
                id=borrower_id, is_active=True
            ).only("id", "name").first()
            if str(borrower_id).isdigit()
            else None
        )

        borrower_id = str(preselected.id) if preselected else ""
        borrower_name = preselected.name if preselected else ""

    else:
        # A posted id keeps the plain lookup: that path reports the refusal
        # itself, and the name is wanted to show back with the message.
        borrower_name = selected_name(Borrower, borrower_id)

    error_message = ""
    today = timezone.now().date()

    # The configured loan period fills the date field in. Offered, not
    # imposed: the librarian may still type a different date, which is
    # behaviour that predates the policy and is left exactly as it was.
    active_policy = policy.load()
    default_due_date = active_policy.due_date_for(today).isoformat()

    if request.method == "POST":

        borrower_id = request.POST.get("borrower", "").strip()
        copy_ids = request.POST.getlist("copies")
        issue_date_str = request.POST.get("issue_date", "").strip()
        due_date_str = request.POST.get("due_date", "").strip()
        notes = request.POST.get("notes", "").strip()

        # --- Validate borrower ---
        borrower = None
        if not borrower_id:
            error_message = "Please select a borrower."
        else:
            try:
                borrower = Borrower.objects.get(
                    id=int(borrower_id),
                    is_active=True,
                )
            except (ValueError, Borrower.DoesNotExist):
                error_message = "Invalid or inactive borrower selected."

        # --- Validate copies ---
        selected_copies = []
        if not error_message:
            if not copy_ids:
                error_message = "Please select at least one copy to issue."
            else:
                with transaction.atomic():
                    for copy_id in copy_ids:
                        try:
                            # Locked on its own row - no join, so nothing
                            # about FOR UPDATE and outer joins arises - then
                            # checked against the one lendable rule, which
                            # is what excludes a withdrawn copy or one whose
                            # book has since been archived.
                            copy = BookCopy.objects.select_for_update().get(
                                id=int(copy_id),
                                status="Available",
                            )

                            if not issuable_copies().filter(
                                pk=copy.pk
                            ).exists():
                                raise BookCopy.DoesNotExist

                            selected_copies.append(copy)
                        except (ValueError, BookCopy.DoesNotExist):
                            error_message = (
                                "One or more selected copies are no longer "
                                "available to issue. They may have just been "
                                "issued to someone else, withdrawn, or their "
                                "book archived. Please review and try again."
                            )
                            selected_copies = []
                            break

        # --- Validate dates ---
        issue_date = None
        due_date = None
        if not error_message:
            if not issue_date_str:
                error_message = "Issue date is required."
            else:
                try:
                    issue_date = date.fromisoformat(issue_date_str)
                except ValueError:
                    error_message = "Invalid issue date format."

        if not error_message and issue_date:
            if issue_date > today:
                error_message = "Issue date cannot be in the future."

        if not error_message:
            if not due_date_str:
                error_message = "Due date is required."
            else:
                try:
                    due_date = date.fromisoformat(due_date_str)
                except ValueError:
                    error_message = "Invalid due date format."

        if not error_message and due_date and issue_date:
            if due_date < issue_date:
                error_message = "Due date cannot be earlier than issue date."

        if not error_message and due_date:
            max_due = issue_date + timedelta(days=365)
            if due_date > max_due:
                error_message = "Due date cannot be more than one year after the issue date."

        # --- Who issued it ---
        # Whoever is signed in, not whoever a dropdown named. The record of
        # whose hands the book passed through is not the borrower's to
        # choose, and it was previously possible to attribute a loan to any
        # member of staff by posting their id.
        issued_by = request.user

        # --- Create loans (atomic) ---
        if not error_message:
            try:
                with transaction.atomic():

                    # The borrower's row is taken first and held for the
                    # rest of the transaction, so the counts the policy
                    # reads cannot change underneath it. Without this,
                    # two requests for the same borrower could each see
                    # room for one more book and each issue one.
                    #
                    # Always before the copies, never after, so two
                    # baskets sharing a borrower cannot end up holding
                    # half of each other's rows.
                    borrower = Borrower.objects.select_for_update().get(
                        id=borrower.id
                    )

                    # The whole basket at once. Checked here rather than
                    # before the transaction because a check outside it is
                    # advice, not a rule: the answer can change between
                    # asking and writing, and a posted form is not
                    # obliged to have asked at all.
                    refusal = policy.refuse_issue(
                        active_policy,
                        borrower,
                        len(selected_copies),
                        today,
                    )

                    if refusal:
                        raise PolicyRefused(refusal)

                    # And whether somebody is ahead of them in a queue for
                    # any of these books. Inside the transaction with the
                    # rest, so a form posted straight at this view is held
                    # to it exactly as the page is - the button being
                    # hidden is a courtesy, not the rule.
                    #
                    # Read after the borrower is locked and before any copy
                    # is, so it adds no new lock order.
                    queued = reservations.refuse_issue_for_copies(
                        selected_copies, borrower.id
                    )

                    if queued:
                        raise PolicyRefused(queued)

                    for copy in selected_copies:
                        copy = BookCopy.objects.select_for_update().get(
                            id=copy.id
                        )
                        if copy.status != "Available" or not issuable_copies(
                        ).filter(pk=copy.pk).exists():
                            raise IntegrityError(
                                f"Copy {copy.copy_code} is no longer available."
                            )

                        loan = Loan.objects.create(
                            copy=copy,
                            borrower=borrower,
                            issue_date=issue_date,
                            due_date=due_date,
                            issued_by=issued_by,
                            notes=notes or None,
                        )

                        copy.status = "Issued"
                        copy.save(update_fields=["status"])

                        create_activity_log(
                            user=request.user,
                            action="ISSUE",
                            entity_type="Loan",
                            entity_id=loan.id,
                            description=(
                                f"{copy.copy_code} issued to "
                                f"{borrower.name}. "
                                f"Due: {due_date.isoformat()}"
                            ),
                        )

                        # If this borrower was waiting for this book, they
                        # are not any more. Only their own reservation:
                        # issuing to somebody further down the queue leaves
                        # everyone in front of them exactly where they were.
                        #
                        # Inside the same transaction as the loan, with the
                        # borrower already locked above, so it cannot half
                        # happen and adds no new lock order.
                        fulfilled = reservations.fulfil_for(
                            copy.volume.book_id, borrower.id, request.user
                        )

                        if fulfilled is not None:
                            create_activity_log(
                                user=request.user,
                                action="FULFIL",
                                entity_type="Reservation",
                                entity_id=copy.volume.book_id,
                                description=(
                                    "%s's reservation for %s fulfilled by "
                                    "%s"
                                    % (
                                        borrower.name,
                                        copy.volume.book.title,
                                        copy.copy_code,
                                    )
                                ),
                            )

                cache.delete(LOAN_CACHE_KEY)
                cache.delete(BOOK_COPY_CACHE_KEY)
                cache.delete(DASHBOARD_CACHE_KEY)

                if len(selected_copies) == 1:
                    messages.success(
                        request,
                        f"Book issued successfully to {borrower.name}.",
                    )
                else:
                    messages.success(
                        request,
                        f"{len(selected_copies)} books issued successfully to {borrower.name}.",
                    )

                return redirect("loan_list")

            except PolicyRefused as refused:
                # Nothing was written: the transaction rolled back with the
                # loans, the copy statuses and the log entries in it.
                error_message = str(refused)

            except IntegrityError:
                error_message = (
                    "One or more copies became unavailable during processing. "
                    "Please review and try again."
                )

    return render(
        request,
        "library/loan_add.html",
        {
            "chosen": chosen,
            "chosen_ids": chosen_ids,
            "matches": matches,
            "more_matches": more_matches,
            "lookup_limit": COPY_LOOKUP_LIMIT,
            "query": query,
            "borrower_id": borrower_id,
            "borrower_name": borrower_name,
            "error_message": error_message,
            "default_issue_date": today.isoformat(),
            "default_due_date": default_due_date,
            # Somebody other than the chosen borrower is at the head of a
            # queue for something in the basket, so this issue will be
            # refused. Shown on the form and enforced in the POST - the
            # same rule said twice, in the place it can be read and the
            # place it cannot be avoided.
            "queue_blocks": [
                copy for copy in chosen
                if copy.queue_front
                and str(copy.queue_front.borrower_id) != str(borrower_id)
            ],
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

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    error_message = ""

    if request.method == "POST" and loan.return_date is None:

        return_date = request.POST.get(
            "return_date"
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

            # The member of staff taking the book back is the one signed
            # in, for the same reason `issued_by` is set that way on the
            # issue side.
            loan.returned_to = request.user

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

    # Whether anyone is waiting for this book. Shown to whoever is taking
    # it back, and nothing more: no copy is assigned, no message is sent,
    # and the return itself is unchanged. What to do about the queue is the
    # librarian's call, which is why this is a sentence and not a workflow.
    waiting_front = reservations.queue_front(loan.copy.volume.book)
    waiting_count = (
        reservations.active_count(loan.copy.volume.book)
        if waiting_front else 0
    )

    return render(
        request,
        "library/loan_return.html",
        {
            "loan": loan,
            "error_message": error_message,
            "next_url": next_url,
            "waiting_front": waiting_front,
            "waiting_count": waiting_count,
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


# Circulation dashboard: quick access point for issue, return, and active loans.
# Counts are calculated efficiently using database annotations rather than loading
# Loan objects into memory.
# Issue and return are the Assistant's daily work: `loan_add` has always
# been open to all three roles and test_permissions asserts it
# (test_assistant_can_add_loan), while only `loan_delete` is restricted.
# The circulation pages that front that workflow were stricter than the
# workflow itself, so an Assistant could return a book but not reach the
# page for finding which loan to return. They now match. Deleting a loan
# and renewing one stay Admin/Librarian - neither moves a book.
def circulation_dashboard(request):

    today = timezone.now().date()

    active_count = Loan.objects.filter(return_date__isnull=True).count()
    overdue_count = Loan.objects.filter(
        return_date__isnull=True,
        due_date__lt=today,
    ).count()
    due_today_count = Loan.objects.filter(
        return_date__isnull=True,
        due_date=today,
    ).count()

    return render(
        request,
        "library/circulation_dashboard.html",
        {
            "active_count": active_count,
            "overdue_count": overdue_count,
            "due_today_count": due_today_count,
        }
    )


# Return lookup by copy code. Allows staff to scan/enter a copy code directly
# and be taken to the return form for the active loan on that copy.
# Open to all three roles, like `loan_return` itself: this is the step that
# finds the loan being returned, and restricting it while allowing the
# return made the workflow unreachable for an Assistant.
# How many active loans the return search lists before asking for a
# narrower term. Same shape, and the same reasoning, as the issue form's cap.
RETURN_LOOKUP_LIMIT = 25


def active_loan_for_code(copy_code):
    """The one active loan on this exact copy code, or None.

    A targeted lookup, and the only thing a scan ever runs. `copy_code` is
    unique in the database and `unique_active_loan_per_copy` allows one
    unreturned loan per copy, so this can match at most one row - there is
    nothing here to search through and nothing that grows with the
    catalogue or with how long the library has been lending.
    """

    return Loan.objects.select_related(
        "copy__volume__book__author",
        "borrower",
        "issued_by",
    ).filter(
        copy__copy_code__iexact=copy_code,
        return_date__isnull=True,
    ).first()


def active_loans_matching(term):
    """Active loans whose book title or author matches `term`.

    For the half of the field that is not a scan: someone at the desk with
    a book in their hand and no readable label.

    Only what is actually out. The filter is on the loans, so a returned
    loan, a copy sitting on its shelf and a copy that was never lent are
    absent by construction rather than removed afterwards - and the cost is
    bounded by how much is out on loan today, not by the size of the
    catalogue or of the history.

    One row per loan, and a copy can hold only one active loan, so nothing
    can appear twice and no `distinct()` is needed. Ordered by due date, so
    whatever is most overdue is at the top of the list to be dealt with.
    """

    return list(
        Loan.objects.filter(
            return_date__isnull=True,
        ).filter(
            models.Q(copy__volume__book__title__icontains=term)
            | models.Q(copy__volume__book__author__name__icontains=term)
        ).select_related(
            "copy__volume__book__author",
            "borrower",
        ).order_by(
            "due_date", "copy__copy_code"
        )[:RETURN_LOOKUP_LIMIT + 1]
    )


def loan_return_lookup(request):
    """Find what to bring back: by its code, or by book or author.

    One field, two behaviours, in that order. A code is resolved on its own
    first - that is what a barcode or QR scanner sends, it can only ever
    name one copy, and it costs one indexed lookup. Only text that is not a
    known code is searched for, so the scan path never runs a search at
    all.

    Nothing here returns anything. Both answers lead into `loan_return`,
    which owns that workflow and is unchanged.
    """

    term = request.GET.get("copy_code", "").strip()

    loan = None
    matches = []
    more_matches = False
    error_message = ""
    today = timezone.now().date()

    if term:

        loan = active_loan_for_code(term)

        if loan is not None:
            # The overdue rule, from the one place that states it.
            describe_loans([loan], today)

        else:
            matches = active_loans_matching(term)
            more_matches = len(matches) > RETURN_LOOKUP_LIMIT
            matches = describe_loans(matches[:RETURN_LOOKUP_LIMIT], today)

            if not matches:

                if BookCopy.objects.filter(
                    copy_code__iexact=term
                ).exists():
                    error_message = (
                        "That copy is not out on loan, so there is nothing "
                        "to return. It may have been brought back already."
                    )

                else:
                    error_message = (
                        "No copy found with that code, and nothing on loan "
                        "matches \u201c%s\u201d. Check the label, or try "
                        "the book or the author." % term
                    )

    return render(
        request,
        "library/loan_return_lookup.html",
        {
            "copy_code": term,
            "loan": loan,
            "matches": matches,
            "more_matches": more_matches,
            "lookup_limit": RETURN_LOOKUP_LIMIT,
            "error_message": error_message,
        }
    )


# Renew an active loan. Extends the due date by the default loan period from
# the current due date. Returned loans cannot be renewed.
@role_required("Admin", "Librarian")
def loan_renew(request, loan_id):
    """Extend a loan by the configured period, up to the renewal limit.

    Who may renew is unchanged - Admin and Librarian, per the decorator
    above - and so is the shape of the page. What is new is that the period
    and the number of renewals allowed come from the policy rather than
    from a constant and from nowhere.
    """

    loan = get_object_or_404(
        Loan.objects.select_related(
            "copy__volume__book",
            "borrower",
            "issued_by",
        ),
        id=loan_id,
    )

    active_policy = policy.load()

    error_message = ""
    renewals_used = policy.renewals_used(loan)

    # Both refusals in one place, so the page and the POST cannot disagree
    # about why: a returned loan, or one that has had its renewals.
    error_message = policy.refuse_renewal(active_policy, loan)

    new_due_date = (
        None
        if loan.return_date is not None
        else active_policy.due_date_for(loan.due_date)
    )

    if not error_message and request.method == "POST":

        try:
            with transaction.atomic():

                # Re-read under a lock and re-check, because the count and
                # the act it counts have to be one event. Two simultaneous
                # renewals of the same loan would otherwise both read the
                # same tally and both be allowed - which is how a limit of
                # one becomes two.
                locked = Loan.objects.select_for_update().get(id=loan.id)

                refusal = policy.refuse_renewal(active_policy, locked)

                if refusal:
                    raise PolicyRefused(refusal)

                new_due_date = active_policy.due_date_for(locked.due_date)

                locked.due_date = new_due_date
                locked.save(update_fields=["due_date"])

                # Inside the transaction with the due date it records, so
                # the renewal and the count of renewals cannot come apart.
                create_activity_log(
                    user=request.user,
                    action="RENEW",
                    entity_type="Loan",
                    entity_id=locked.id,
                    description=(
                        f"{loan.copy.copy_code} renewed. "
                        f"New due date: {new_due_date.isoformat()}"
                    ),
                )

        except PolicyRefused as refused:
            error_message = str(refused)
            renewals_used = policy.renewals_used(loan)

        else:
            cache.delete(LOAN_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            return redirect("loan_detail", loan_id=loan.id)

    return render(
        request,
        "library/loan_renew.html",
        {
            "loan": loan,
            "new_due_date": new_due_date,
            "error_message": error_message,
            "renewals_used": renewals_used,
            "renewal_limit": (
                active_policy.max_renewals
                if active_policy.limits_renewals
                else 0
            ),
            "loan_period_days": active_policy.loan_period_days,
        }
    )


# What the borrower list's activity filter offers. Loan activity is kept
# separate from the borrower's own Active/Inactive status: one says whether
# the library still lends to them, the other what they are holding, and
# folding the two together would make both unreadable.
BORROWER_ACTIVITY_FILTERS = ("has_loans", "no_loans", "overdue", "no_overdue")


def describe_loans(loans, today=None):
    """Attach `days_overdue` to each loan, using the existing rule.

    The rule is the one the loan and copy lists already apply — an unreturned
    loan past its due date — expressed once here for the borrower screens
    rather than written out again. Nothing is stored: overdue is a fact about
    the due date, and a column for it could only ever disagree.
    """

    loans = list(loans)

    if today is None:
        today = timezone.now().date()

    for loan in loans:
        loan.days_overdue = (
            (today - loan.due_date).days
            if loan.return_date is None and loan.due_date < today
            else 0
        )

    return loans


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

@role_required("Admin", "Librarian")
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
        "RENEW",
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

            "due_today_loans": Loan.objects.filter(
                return_date__isnull=True,
                due_date=today
            ).count(),
        }

        cache.set(
            DASHBOARD_CACHE_KEY,
            dashboard_stats,
            timeout=300
        )

    # Outside the cached block above: these are lists of records, not
    # counts, and they are cheap - five rows each, with the joins the rows
    # actually name.
    #
    # `-id` as well as the date, because `issue_date` and `return_date` are
    # DateFields: without a tiebreaker, everything that happened today came
    # back in whatever order the database felt like, so "most recent" was
    # not reliably most recent.
    recent_loans = Loan.objects.select_related(
        "copy__volume__book",
        "borrower",
        "issued_by",
    ).order_by(
        "-issue_date",
        "-id",
    )[:5]

    # The other half of recent circulation. Taken from the loans themselves
    # rather than from the activity log: `return_date` and `returned_to` are
    # the record of a return, and reading the log instead would mean parsing
    # a description to find out which book it was.
    recent_returns = Loan.objects.filter(
        return_date__isnull=False
    ).select_related(
        "copy__volume__book",
        "borrower",
        "returned_to",
    ).order_by(
        "-return_date",
        "-id",
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
    dashboard_stats["recent_returns"] = recent_returns
    dashboard_stats["recent_logs"] = recent_logs

    # Which quick actions to offer. Issuing, returning and adding a borrower
    # are open to all three roles; adding a book is not, so offering it to
    # an Assistant would be offering a 403. The decorators on those views
    # are the enforcement - this only decides what is worth showing.
    dashboard_stats["can_edit"] = can_edit_library(request.user)

    return render(
        request,
        "library/dashboard.html",
        dashboard_stats
    )