"""Physical copies, and the shelves they sit on.

Locations and shelves are where the library puts things; copies are the
things. They are together because almost every question about one is really
a question about the other: what is on this shelf, where is this copy, move
these copies there.
"""

from collections import namedtuple
import json

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.template.loader import render_to_string
from django.core.cache import cache
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.db import models, transaction
from django.db.models import Q

from ..models import (
    Author,
    Book,
    BookCopy,
    BookVolume,
    Category,
    Loan,
    Location,
    Publisher,
    Shelf,
)

from .. import queries

from .. import barcode
from .. import history
from .. import inventory
from ..context_processors import is_main_nav_request
from ..permissions import can_edit_library, feature_required, role_required

from .common import (
    BOOK_AVAILABILITY_FILTERS,
    BOOK_AVAILABILITY_LABELS,
    BOOK_COPY_CACHE_KEY,
    BOOK_LIST_MODES,
    BOOK_LIST_MODE_DEFAULT,
    BOOK_SORT_DEFAULT,
    BOOK_SORT_FIELDS,
    COPY_SORT_DEFAULT,
    COPY_SORT_FIELDS,
    COPY_STATE_FILTERS,
    COPY_STATE_LABELS,
    DASHBOARD_CACHE_KEY,
    LOAN_CACHE_KEY,
    LOCATION_CACHE_KEY,
    PAGE_SIZE,
    SHELF_CACHE_KEY,
    book_list_fragment,
    copy_state_options,
    create_activity_log,
    describe_copies,
    filter_copies_by_state,
    is_modal_request,
    is_options_request,
    numeric_param,
    page_size_options,
    query_with,
    resolve_page_size,
    resolve_sort,
    safe_redirect_target,
    selected_name,
    shelf_options_for,
    sort_ordering,
    sortable_columns,
    volume_label,
)


#Location View
@feature_required("locations")
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

@feature_required("locations")
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
@feature_required("locations", "Admin", "Librarian")
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
@feature_required("locations", "Admin", "Librarian")
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
@feature_required("locations", "Admin", "Librarian")
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
@feature_required("shelves")
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

@feature_required("shelves")
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
@feature_required("shelves", "Admin", "Librarian")
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
@feature_required("shelves", "Admin", "Librarian")
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
@feature_required("shelves", "Admin", "Librarian")
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




@feature_required("books")
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
        else queries.active_books()
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
    books = queries.annotate_copy_counts(books)

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


@feature_required("copies")
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


@feature_required("copies", "Admin", "Librarian")
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


@feature_required("copies", "Admin", "Librarian")
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


@feature_required("copies")
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

@feature_required("copies", "Admin", "Librarian")
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

@feature_required("copies", "Admin", "Librarian")
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


@feature_required("copies", "Admin", "Librarian")
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


@feature_required("copies", "Admin", "Librarian")
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
