"""What every part of the web layer needs.

The pieces shared across the views: the page-size and sorting constants,
the helpers that read and rebuild a query string, the tests that say what
shape of answer a request is asking for (a page, a fragment, a dialog, a
dropdown), the copy-state rules the lists and the reports agree on, and the
one function that writes an activity-log entry.

Everything here is used by two or more of the modules beside it. Anything
used by only one belongs in that one.
"""

import json
import os
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from django.conf import settings
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.core.cache import cache
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.db import IntegrityError, models

from ..models import (
    ActivityLog,
    Book,
    BookCopy,
    Loan,
    Location,
    Shelf,
)

from .. import queries

from .. import policy
from ..context_processors import is_main_nav_request
from ..permissions import can_edit_library, passes_ceiling


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

# Locations and shelves sort the same way the copy list does, over the
# counts their rows already carry - the annotations are in the views, so
# nothing here needs a second query to order by them.
LOCATION_SORT_FIELDS = {
    "name": "name",
    "shelves": "shelf_count",
    "copies": "copy_count",
}

LOCATION_SORT_DEFAULT = "name"

SHELF_SORT_FIELDS = {
    "shelf": "shelf_code",
    "location": "location__name",
    "copies": "copy_count",
}

# Grouped by location by default, which is the order the shelf list has
# always come back in.
SHELF_SORT_DEFAULT = "location"

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

# Sortable loan-list columns, whitelisted for the same reason as the books
# above: the raw parameter never reaches order_by().
#
# Status is deliberately absent. It is not a column - it is derived from
# `return_date` being null and `due_date` against today - so ordering by it
# would mean sorting the table by an expression the reader cannot see. The
# status filter is how to narrow by it, which is the same answer the book
# list gives for Copies.
# Which columns the borrower list may be sorted by, and what each one means
# in the database. Same shape and the same reason as the book and loan
# lists': a whitelist, so a query string can only ever name a column that
# is actually on screen.
#
# `active_loans` is one of the annotations the list already computes, so
# sorting by what somebody is holding costs nothing extra - and cannot
# disagree with the number printed in the row, because it is that number.
BORROWER_SORT_FIELDS = {
    "name": "name",
    "registration_no": "registration_no",
    "borrower_type": "borrower_type",
    "active_loans": "active_loans",
    "status": "is_active",
}

# Alphabetical, which is what the list showed before it could be sorted at
# all and what somebody looking for a person wants.
BORROWER_SORT_DEFAULT = "name"
BORROWER_SORT_DEFAULT_DIRECTION = "asc"


LOAN_SORT_FIELDS = {
    # No "id": the list does not show the loan number, so nothing can ask
    # to be sorted by it.
    "copy": "copy__copy_code",
    "book": "copy__volume__book__title",
    "borrower": "borrower__name",
    "issue_date": "issue_date",
    "due_date": "due_date",
}

# Newest first, which is what the list showed before it could be sorted at
# all and what a circulation desk wants by default.
LOAN_SORT_DEFAULT = "issue_date"
LOAN_SORT_DEFAULT_DIRECTION = "desc"

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

# The lookup lists - Authors, Categories, Publishers - are one table with a
# different noun in it: a name, a count of the books filed under it, and a
# way into those books. One whitelist between the three, so they cannot
# drift apart in what they will sort by.
#
# `book_count` is the annotation `lookup_book_counts` adds, not a column.
LOOKUP_SORT_FIELDS = {
    "name": "name",
    "books": "book_count",
}

LOOKUP_SORT_DEFAULT = "name"

LOOKUP_NAME_LABELS = {
    "author": "Author Name",
    "category": "Category Name",
    "publisher": "Publisher Name",
}

LOOKUP_LABELS = {
    "author": "Author",
    "category": "Category",
    "publisher": "Publisher",
}

# Where a row's name points: the book list, already narrowed to that row
# and sorted by title.
#
# Written out per lookup rather than assembled from one shape, because the
# three are not the same request. The book list offers Author and Category
# as browsing modes, so those two select the matching mode and its control
# shows the filter that is in force; it has no publisher control at all, so
# that one browses everything with the publisher filter applied and relies
# on the list's filter chips to say so. `availability=` is sent empty for
# the same reason - it is the one narrowing that shares that panel.
#
# `page_size` is the list's own default, so a link cannot ask for a size
# the rows-per-page control does not offer.
LOOKUP_BOOK_QUERIES = {
    "author": (
        "mode=author&author={id}"
        "&sort=title&direction=asc&page_size={size}"
    ),
    "category": (
        "mode=category&category={id}"
        "&sort=title&direction=asc&page_size={size}"
    ),
    "publisher": (
        "mode=all&publisher={id}&availability="
        "&sort=title&direction=asc&page_size={size}"
    ),
}

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
#
# Four of the seventeen entity types the log records are absent, and each
# for a reason rather than by oversight:
#
#   User                  has no detail page.
#   RoleFeature           likewise - the matrix is the page, and a row in
#                         it is not a record with a URL.
#   OrganizationSettings  its page is Admin-only, so linking it would hand
#                         every other role a 403.
#   InventorySession      its page has an Admin/Librarian ceiling, same
#                         objection: an Assistant can never open it.
#
# The rule that separates those last two from everything here is *ceiling*,
# not toggle. Book, Loan and the rest can all be switched off for a role,
# and are still linked: a link to something switched off is a link the
# viewer would not have been shown the entry for. A link to something whose
# ceiling excludes the role is a link that can never work.
ACTIVITY_LOG_DETAIL_ROUTES = {
    "AcquisitionSuggestion": "suggestion_detail",
    "Book": "book_detail",
    "BookContent": "book_content_detail",
    "BookCopy": "book_copy_detail",
    "BookVolume": "book_volume_detail",
    "Borrower": "borrower_detail",
    "Loan": "loan_detail",
    "Reservation": "book_detail",
}


# The three lookups have no page of their own: their name in the list opens
# the book list filtered to them, and a log entry about one goes to the same
# place. Kept apart from the routes above because that is a URL with a query
# string, not a detail page with an id in the path.
ACTIVITY_LOG_LOOKUP_KINDS = {
    "Author": "author",
    "Category": "category",
    "Publisher": "publisher",
}


# What each action is called on screen, and how it is coloured.
#
# The stored value is a verb in the imperative - CREATE, ISSUE - which is
# right for a column in a table and wrong for a sentence a librarian reads.
# "Added" and "Issued" say the same thing about something that has already
# happened.
#
# Anything not listed falls back to the stored value, so a new action shows
# up as itself rather than disappearing.
ACTIVITY_LOG_ACTION_LABELS = {
    "CREATE": _("Added"),
    "UPDATE": _("Updated"),
    "DELETE": _("Deleted"),
    "ISSUE": _("Issued"),
    "RETURN": _("Returned"),
    "RENEW": _("Renewed"),
    "RESERVE": _("Reserved"),
    "CANCEL": _("Cancelled"),
    "FULFIL": _("Fulfilled"),
    "IMPORT": _("Imported"),
    "SEED": _("Seeded"),
}

# Only the ones that carry a warning are coloured. Everything else is
# neutral: a log where every row is coloured tells the reader nothing about
# which rows to look at.
ACTIVITY_LOG_ACTION_TONES = {
    "DELETE": "danger",
    "CANCEL": "warning",
    "ISSUE": "primary",
    "RETURN": "success",
    "CREATE": "secondary",
}


def label_activity_log(log):
    """Attach what the template needs to render one entry readably.

    Three things, set on the object rather than computed in the template:
    the action's readable name, its colour, and the URL of the record it
    refers to. `target_url` is the same helper the dashboard's recent
    activity already uses.
    """

    log.action_label = ACTIVITY_LOG_ACTION_LABELS.get(log.action, log.action)
    log.action_tone = ACTIVITY_LOG_ACTION_TONES.get(log.action, "light")
    log.target_url = activity_log_target(log)

    return log


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

    kind = ACTIVITY_LOG_LOOKUP_KINDS.get(log.entity_type)

    if kind:
        return lookup_books_url(kind, log.entity_id)

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


def resolve_sort(request, allowed, default, default_direction="asc"):
    """Validated (sort key, direction) for a list table.

    `default_direction` exists because not every table reads best
    ascending. A catalogue does - A before B - but a loan list does not:
    the circulation desk wants the newest issue at the top, which is what
    the loan list showed before it could be sorted at all. Defaulting it
    per table is what keeps that true without the view second-guessing the
    parameter.
    """

    sort = request.GET.get("sort", "")

    if sort not in allowed:
        sort = default

    direction = request.GET.get("direction", "")

    if direction not in ("asc", "desc"):
        direction = default_direction

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


def lookup_books_url(kind, record_id):
    """The book list, already narrowed to one author, category or publisher.

    The only place any of the three can be "opened", now that none of them
    has a page of its own - so the list's rows, the dialogs and the
    activity log all come through here and cannot point anywhere different
    from each other.
    """

    return "%s?%s" % (
        reverse("book_list"),
        LOOKUP_BOOK_QUERIES[kind].format(id=record_id, size=PAGE_SIZE),
    )


def lookup_form_modal(
    request, kind, action, error=None, name="", city=None, editing=False
):
    """The Add or Edit dialog for one lookup, whichever of the three it is.

    One template behind all six: an author, a category and a publisher
    differ by their label and, for the publisher, one extra field. `city`
    is None for the two that have no such thing, which is what tells the
    template not to draw it - as opposed to "" for a publisher whose city
    is simply blank.

    Re-rendered in place when validation fails, so the dialog stays open
    with the message and whatever was typed.
    """

    return render(
        request,
        "library/partials/lookup_form_modal.html",
        {
            "kind": kind,
            "label": LOOKUP_LABELS[kind],
            "action": action,
            "editing": editing,
            "error": error,
            "name": name,
            "city": city or "",
            "has_city": city is not None,
        },
    )


def lookup_delete_modal(request, kind, action, name, blocker):
    """The Delete confirmation for one lookup.

    `blocker` set means the dialog explains why it cannot go and offers no
    Delete button, rather than letting the click fail against the books
    that are filed under it.
    """

    return render(
        request,
        "library/partials/lookup_delete_modal.html",
        {
            "kind": kind,
            "label": LOOKUP_LABELS[kind],
            "action": action,
            "name": name,
            "blocker": blocker,
        },
    )


def lookup_delete_blocker(kind, record_id):
    """Why this author, category or publisher cannot be deleted, or "".

    All three are refused on the same ground: books are filed under it.
    All three columns are NO ACTION in Postgres, so letting the delete
    through would abort the statement and surface as a 500 rather than as
    anything a librarian could act on. That does not belong behind a
    confirm button.

    Archived books count. They still hold the foreign key, and they still
    come back if the book is restored.
    """

    count = Book.objects.filter(**{"%s_id" % kind: record_id}).count()

    if not count:
        return ""

    return (
        "This %s cannot be deleted. %d book%s still filed under it - "
        "change those books first."
        % (
            LOOKUP_LABELS[kind].lower(),
            count,
            " is" if count == 1 else "s are",
        )
    )


def lookup_saved_response(name):
    """Tell the page a lookup was saved, so it can close up and refresh.

    No content, because the only two things that should change are the
    dialog (which closes) and the list behind it (which re-requests
    itself). app.js turns the event into both, and into a toast.
    """

    response = HttpResponse(status=204)

    response["HX-Trigger"] = json.dumps({"recordSaved": {"name": name}})

    return response


def lookup_deleted_response(name):
    """The same, for one that is gone."""

    response = HttpResponse(status=204)

    response["HX-Trigger"] = json.dumps({"recordDeleted": {"name": name}})

    return response


def copy_withdrawn_response(code):
    """The same, for a copy taken out of circulation.

    Its own event rather than `recordSaved` or `recordDeleted`, because
    neither says what happened: the copy was not deleted - keeping it, and
    its loans, is the whole point of withdrawing - and "saved" is not what
    a librarian just did. Everything else is the same contract those two
    use, so the copy list closes the dialog and re-requests its own
    results on this exactly as it does on theirs.
    """

    response = HttpResponse(status=204)

    response["HX-Trigger"] = json.dumps({"copyWithdrawn": {"code": code}})

    return response


def lookup_book_counts(queryset):
    """Each lookup row annotated with how many books are filed under it.

    Archived books are left out, so the number agrees with what clicking
    it opens: the book list shows the active catalogue unless `archived=1`
    asks otherwise.

    One aggregate over one reverse foreign key - no fan-out to undo with
    .distinct() - so a page of rows costs the one query it already made,
    whatever the catalogue holds.
    """

    return queryset.annotate(
        book_count=models.Count(
            "book",
            filter=models.Q(book__archived_at__isnull=True),
        ),
    )


def lookup_options(model, cache_key, search, matching):
    """The suggestions a combobox over one of the lookup models offers.

    Unchanged from what these three views have always done: a search is a
    query, and the unsearched list is cached whole for five minutes. Every
    Add/Edit Book page asks for one of these, they are small, and they
    change rarely, so the cache earns its keep here.

    Deliberately not used for the list page any more. Those rows now carry
    a live count of the books filed under each one, and a five-minute-old
    count is worse than no count at all.
    """

    if search:
        return list(matching)

    options = cache.get(cache_key)

    if options is None:

        options = list(model.objects.all())

        cache.set(cache_key, options, timeout=300)

    return options


def lookup_table(request, queryset, kind):
    """Sort, page and link one page of a lookup list.

    Returns `(page, context)` - the page separately so each view can name
    its own rows the way the rest of this project does (`authors`,
    `categories`, `publishers`) rather than gaining a second name for the
    same object.

    Everything the three lists have in common lives here, built out of the
    same utilities the book list uses: `resolve_sort` whitelists the sort
    key, `sort_ordering` adds the tiebreaker that stops a row appearing on
    two pages, `resolve_page_size` bounds the rows-per-page, and
    `sortable_columns` turns the headers into links that carry the rest of
    the table's state.
    """

    sort, direction = resolve_sort(
        request,
        LOOKUP_SORT_FIELDS,
        LOOKUP_SORT_DEFAULT,
    )

    rows = lookup_book_counts(queryset).order_by(
        *sort_ordering(LOOKUP_SORT_FIELDS, sort, direction)
    )

    page_size = resolve_page_size(request)

    paginator = Paginator(rows, page_size)
    page = paginator.get_page(request.GET.get("page"))

    # The book list, already filtered to this row. Set on the objects of
    # the one page being shown rather than worked out in the template, so
    # the three pages cannot disagree about which parameters to send.
    for row in page:
        row.books_url = lookup_books_url(kind, row.id)

    context = {
        "paginator": paginator,
        "columns": sortable_columns(
            request,
            [
                ("name", LOOKUP_NAME_LABELS[kind]),
                ("books", "Books"),
            ],
            LOOKUP_SORT_FIELDS,
            sort,
            direction,
        ),
        "sort": sort,
        "direction": direction,
        "page_size": page_size,
        "page_size_options": page_size_options(page_size),
        # Where the rows-per-page control sends its value. `sort` and
        # `direction` are pinned rather than left to the query string so
        # this is never a bare "?" - htmx appends to it, and appending to
        # nothing produces "?&page_size=50" in the address bar. `page` goes
        # so a size change returns to page 1, and `page_size` so the value
        # the <select> sends is the only one in the query.
        "page_size_url": "?" + query_with(
            request,
            sort=sort,
            direction=direction,
            page=None,
            page_size=None,
        ),
        # Everything except `page`, so a paging link keeps the search, the
        # sorting and the rows-per-page. The three are pinned for the same
        # reason as above: they apply whether or not the URL says so, and a
        # link that states them stays right when it is copied elsewhere.
        "pagination_query": query_with(
            request,
            sort=sort,
            direction=direction,
            page_size=page_size,
            page=None,
        ),
        "elided_page_range": list(
            paginator.get_elided_page_range(
                page.number,
                on_each_side=1,
                on_ends=1,
            )
        ),
        "page_ellipsis": Paginator.ELLIPSIS,
        # UI gating only: Add, Edit and Delete are enforced by
        # `role_required` on the views behind them.
        "can_edit": can_edit_library(request.user),
    }

    return page, context


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


def date_param(request, name):
    """A GET parameter, kept only if it is a real ISO date.

    `numeric_param`'s sibling, and for the same reason: filtering a date
    column on a string Django cannot parse raises ValidationError, which is
    a 500 rather than a validation message - and so does a well-formed but
    impossible date like 2026-02-30. A filter the viewer cannot see is not
    worth a crash, so anything unreadable is discarded and read as "no
    filter", exactly as an unknown status or sort value already is.

    Returns a `date`, so a caller can build a half-open range from it - the
    reason to have the object rather than the string.
    """

    value = request.GET.get(name, "").strip()

    if not value:
        return None

    try:
        return date.fromisoformat(value)

    except ValueError:
        return None


def day_bounds(day):
    """The half-open range of instants that fall on `day`.

    For filtering a timestamp column by calendar day. The obvious spelling,
    `created_at__date=day`, compiles to a function call on the column -
    `(created_at AT TIME ZONE 'UTC')::date = %s` - which no index on
    `created_at` can satisfy, so PostgreSQL walks rows until it has filled
    the page. A half-open range on the bare column uses the index directly.
    Measured on 60,000 activity-log rows: 1.005 ms the first way, 0.030 ms
    this way, for the same rows.

    Half-open rather than `__range`, which is inclusive at both ends and
    would take midnight of the following day as well.
    """

    start = timezone.make_aware(datetime.combine(day, time.min))

    return start, start + timedelta(days=1)


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
        return copies.filter(id__in=queries.active_loan_copies())

    if state == "overdue":
        return copies.filter(id__in=queries.overdue_loan_copies(today))

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
            id__in=queries.active_loan_copies()
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

    # Ids. Both controls are the application's searchable dropdown now, the
    # same one Author, Category and Publisher use: it posts the id of the
    # record chosen, and its own "Add new" is what creates a location or a
    # shelf that did not exist - so by the time this runs there is always a
    # real record behind each id.
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
    create_extra="",
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
            # Extra fields the create button must send, for a record that
            # needs more than a name. "" for every dropdown but Shelf.
            "create_extra": create_extra,
            # Whether to offer "Add new" at the foot of the list. Every one
            # of the five add views this can post to carries the same
            # `feature_required(..., "Admin", "Librarian")` ceiling, and that
            # ceiling exempts SuperAdmin - so `passes_ceiling` is the
            # question to ask. `can_edit_library` predates SuperAdmin and
            # does not know about it, which is why the one role that may
            # always create was told "No author found matching ..." instead
            # of being offered the button. An Assistant still gets nothing,
            # because they fail the ceiling itself.
            "can_create": (
                creation_offered
                and passes_ceiling(request.user, "Admin", "Librarian")
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


def modal_refusal(request, message, url):
    """Say why a POST was refused, for a request with no dialog to say it in.

    The modal endpoints answer a refusal by re-rendering the dialog fragment
    with the reason inside it, which is right when htmx put that fragment in
    a dialog and useless otherwise: without JavaScript the browser replaces
    the whole page with a bare partial that explains nothing. Nothing is lost
    by redirecting here - a refused delete has no typed input to preserve -
    so the reason travels as a message instead.
    """

    messages.error(request, message)

    return redirect(url)


def modal_redirect(request, url):
    """Send the browser to `url`, whether or not htmx is driving.

    htmx will not follow a 302 raised from inside a dialog - it swaps the
    redirected page into the dialog body instead - so it is told to navigate
    with HX-Redirect on an empty 204. A form posted without JavaScript has
    nothing that reads that header and would simply sit there on a 204, so
    that case gets an ordinary redirect.

    `book_restore_modals` and `language_set` already branched this way; the
    other modal endpoints returned 204 unconditionally, which is why a
    scriptless Delete or Save appeared to do nothing at all. This is that
    same rule in one place.
    """

    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response

    return redirect(url)


def safe_redirect_target(request, fallback):
    next_url = request.POST.get("next") or request.GET.get("next")

    if next_url and url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url

    return fallback


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


def create_activity_log(
    user,
    action="",
    entity_type=None,
    entity_id=None,
    description=None,
):
    """Record one state change against the person who made it.

    `user` is required and has no default on purpose. It used to default to
    None, and 36 of the 84 call sites - every add, edit and delete of users,
    borrowers, books, volumes, contents, locations, shelves, copies and the
    lookup tables - quietly took that default. The log therefore could not
    answer "who deleted this", which is the one question it exists to
    answer. Leaving it required means a forgotten actor is a TypeError at
    the call site rather than an anonymous row in the audit trail.

    `activity_logs.user_id` is still nullable, for a genuinely system-driven
    entry; pass None deliberately in that case.
    """

    ActivityLog.objects.create(
        user=user,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        description=description,
        created_at=timezone.now(),
    )

    cache.delete(ACTIVITY_LOG_CACHE_KEY)
