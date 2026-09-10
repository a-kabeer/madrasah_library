"""The public catalogue: what the library has, for anybody who asks.

A separate surface, not a relaxed version of the staff application. Two
pages - a list and a book - both read-only, both anonymous, and neither
able to reach anything the desk uses. There is no form here that writes,
no action that changes a record, and no route into `/library/`.


The authentication boundary
---------------------------

`LoginRequiredMiddleware` blocks every request in this project unless the
view it resolved to is marked otherwise, and the mark is
`@login_not_required` on the view itself - which is how `login_view` has
always been exempt. Every public view below carries it.

That is the narrowest exemption available: it is per-view, so it cannot be
widened by a URL that happens to share a prefix, and adding a view to
`/library/` still leaves it protected because the default is to require a
login. Nothing about the middleware, `LOGIN_URL` or the settings is
touched, and there is no second authentication system - anonymous is
simply anonymous.


What is public
--------------

Bibliographic facts and one sentence about whether the book can be had:
title, author, publisher, category, cover, volumes, and how many copies are
on the shelf. Nothing else exists on these pages.

Not shown, and not fetched: borrowers, loans, due dates, reservations,
staff accounts, notifications, acquisition suggestions, inventory sessions,
the activity log, copy codes, copy statuses, shelves, locations, internal
notes and archive metadata. Several of those are not merely hidden from the
template - the querysets below never join to them, so there is nothing in
the response to leak.

Shelf and location are deliberately among them. Nothing in this project
treats where a copy physically sits as public information, and "which shelf
in which room" is a thing you tell somebody at the desk, not the open web.


Archived books
--------------

Invisible, through one queryset. `public_books()` is the only way any view
here reaches a `Book`, and it starts from `active_books()` - Task 5's own
"the catalogue, without the archived" - so the list, every search, every
filter and the detail page inherit the exclusion rather than each
remembering it. An archived id and an id that was never used answer
identically, so neither confirms that a hidden record exists.


Availability
------------

Task 6's definition, unchanged. `annotate_copy_counts` is the staff list's
own annotation - a copy is available when it is on a shelf, marked
Available, and not out on loan - and both public pages read the same three
numbers off it. There is no second definition here and no per-row
counting; what is different is only the wording, because a visitor wants to
know whether they can have the book, not how the stock breaks down.
"""

from django.contrib.auth.decorators import login_not_required
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Q
from django.shortcuts import render

from .models import Author, BookVolume, Category, Publisher
from .queries import active_books, annotate_copy_counts
from django.utils.translation import gettext

from .views import PAGE_SIZE, numeric_param, query_with


# What a visitor may narrow by. `available` and `unavailable` are the two
# answers a visitor can act on - can I have this today, or not - and both
# are read off `available_copies`, which is Task 6's annotation. The staff
# list's third value (`issued`, meaning "something is out") is deliberately
# absent: it describes circulation rather than obtainability, and saying
# *why* a book is not in is not the public's business.
PUBLIC_AVAILABILITY_FILTERS = ("available", "unavailable")

PUBLIC_AVAILABILITY_LABELS = {
    "available": "On the shelf now",
    "unavailable": "Not on the shelf",
}


# The three the catalogue's appearance control offers. "auto" is not a
# resolved theme: it is written straight onto `data-bs-theme` and left for
# the `prefers-color-scheme` block in style.css to settle, because this
# page has no script to resolve it with.
PUBLIC_THEMES = ("light", "dark", "auto")

PUBLIC_THEME_DEFAULT = "auto"

# A plain cookie, not the session. The catalogue has no session and wants
# none - see the base template - and an appearance preference is not worth
# starting one for. A year, because the next visit should look like the
# last one; `SameSite=Lax` so it rides an ordinary navigation and nothing
# else, and no `Secure` flag decision is made here beyond the project's.
PUBLIC_THEME_COOKIE = "catalogue_theme"

PUBLIC_THEME_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def resolve_public_theme(request):
    """The appearance this request should render in, and whether it changed.

    Read from `?theme=` first so a plain link can switch it - the catalogue
    is scriptless and formless by design, so a link in the query string is
    the whole mechanism - then from the cookie a previous link set, then
    the default.

    Anything unrecognised falls back rather than raising: this decides a
    colour, and a mistyped query string should not be an error page.
    """

    asked = (request.GET.get("theme") or "").strip().casefold()

    if asked in PUBLIC_THEMES:
        return asked, True

    stored = (request.COOKIES.get(PUBLIC_THEME_COOKIE) or "").strip().casefold()

    if stored in PUBLIC_THEMES:
        return stored, False

    return PUBLIC_THEME_DEFAULT, False


def remember_public_theme(response, theme, changed):
    """Persist a theme the reader just chose, so it survives navigation."""

    if changed:
        response.set_cookie(
            PUBLIC_THEME_COOKIE,
            theme,
            max_age=PUBLIC_THEME_COOKIE_MAX_AGE,
            samesite="Lax",
        )

    return response


def public_theme_links(request, theme):
    """The appearance switcher's three links, for whichever page is asking.

    Every public page carries the switcher, so the list is built once here
    rather than repeated per view. `query_with` keeps the rest of the query
    string, so choosing an appearance never drops a search or a page.
    """

    return [
        {
            "value": value,
            "label": label,
            "icon": icon,
            "active": theme == value,
            "url": "?" + query_with(request, theme=value),
        }
        for value, label, icon in (
            ("light", gettext("Light"), "bi-sun-fill"),
            ("dark", gettext("Dark"), "bi-moon-stars-fill"),
            ("auto", gettext("System"), "bi-circle-half"),
        )
    ]


def public_books():
    """Every book the public may see, with its availability counted.

    The single gate. Both views start here, so the archive exclusion and
    the joins that are allowed are decided once - a view cannot widen
    either by forgetting.

    `select_related` covers exactly the three things the pages render.
    There is no join to copies beyond the aggregate, none to loans beyond
    the subquery the aggregate already uses, and none at all to borrowers,
    shelves, users or anything else.
    """

    return annotate_copy_counts(
        active_books().select_related("author", "category", "publisher")
    )


def availability_of(book):
    """One public sentence about whether `book` can be had.

    Read off the same three annotated numbers on both pages, so the list
    and the detail page cannot disagree - they are not two readings of the
    catalogue, they are one annotation phrased once.

    Deliberately vague about the absent copies. "Not on the shelf right
    now" covers a book that is out on loan and one whose only copy is
    missing, and the difference between those is the library's business.
    """

    total = getattr(book, "total_copies", 0)
    available = getattr(book, "available_copies", 0)

    if not total:
        return {
            "state": "none",
            "label": "No copies",
            "detail": "This title is catalogued but the library holds no copy.",
            "tone": "secondary",
        }

    if available:
        return {
            "state": "available",
            "label": (
                "Available" if available == 1 else "%d available" % available
            ),
            "detail": "On the shelf and ready to borrow.",
            "tone": "success",
        }

    # Deliberately silent about why. A copy may be out on loan or it may
    # be lost, and the difference is the library's business - saying "every
    # copy is out" would also be untrue half the time.
    return {
        "state": "unavailable",
        "label": "Currently unavailable",
        "detail": "No copy is on the shelf right now. Ask at the desk.",
        "tone": "warning",
    }


def describe_availability(books):
    """Attach the public wording to each row. No query, per row or at all."""

    for book in books:
        book.availability = availability_of(book)

    return books


def named(model, pk):
    """The name of one filtered-on record, or "".

    For the chip that says which filter is in force and offers to drop it.
    One query, and only when that filter is actually set - an id nobody
    asked about is never looked up, and an id that matches nothing yields
    "" rather than an error.
    """

    if not pk:
        return ""

    row = model.objects.filter(id=pk).first()

    return row.name if row else ""


# How many rows a browse list of names shows at once, and how many books
# the dashboard puts in front of a visitor. Deliberately small: these are
# a way in, not a report.
PUBLIC_BROWSE_SIZE = 24

PUBLIC_DASHBOARD_BOOKS = 8

PUBLIC_DASHBOARD_CATEGORIES = 8


def _named_counts(model, search):
    """Names of one kind, each with how many public books carry it.

    One query. The count is over `active_books()` rather than the whole
    table, so an archived book does not inflate a number on a page whose
    whole point is what the library actually holds.
    """

    rows = model.objects.annotate(
        book_count=models.Count(
            "book",
            filter=models.Q(book__archived_at__isnull=True),
            distinct=True,
        )
    )

    if search:
        rows = rows.filter(name__icontains=search)

    # Nothing with no public books: a name a visitor cannot follow anywhere
    # is not a way into the catalogue.
    return rows.filter(book_count__gt=0).order_by("name")


def _browse_page(request, model, template, title, icon, filter_param, nav_section):
    """One of the three name lists - authors, categories, publishers.

    They differ only in which model they read and which filter their rows
    link to, so they are one function rather than three that would drift.
    Read-only by construction: there is nothing here but a search box, a
    page of names and a link into the book list.
    """

    search = (request.GET.get("search") or "").strip()

    rows = _named_counts(model, search)

    paginator = Paginator(rows, PUBLIC_BROWSE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    theme, theme_changed = resolve_public_theme(request)

    return remember_public_theme(render(
        request,
        template,
        {
            "rows": page,
            "paginator": paginator,
            "search": search,
            "nav_section": nav_section,
            "browse_title": title,
            "browse_icon": icon,
            "filter_param": filter_param,
            "pagination_query": query_with(request, page=None),
            "elided_page_range": list(
                paginator.get_elided_page_range(
                    page.number,
                    on_each_side=1,
                    on_ends=1,
                )
            ),
            "page_ellipsis": Paginator.ELLIPSIS,
            "public_theme": theme,
            "theme_links": public_theme_links(request, theme),
        },
    ), theme, theme_changed)


@login_not_required
def public_dashboard(request):
    """The catalogue's front door.

    What a visitor wants first: a search box, how big the collection is,
    what has just arrived, and what subjects it is strongest in. Nothing
    about loans, borrowers, copies or the desk - those are the library's
    business, not the catalogue's, and none of them is read here.

    The four totals and the two lists are five bounded queries; none grows
    with the size of the collection beyond the counts themselves.
    """

    books = active_books()

    theme, theme_changed = resolve_public_theme(request)

    return remember_public_theme(render(
        request,
        "public/dashboard.html",
        {
            "nav_section": "dashboard",
            "total_books": books.count(),
            "total_authors": Author.objects.filter(
                book__archived_at__isnull=True
            ).distinct().count(),
            "total_categories": Category.objects.filter(
                book__archived_at__isnull=True
            ).distinct().count(),
            "total_publishers": Publisher.objects.filter(
                book__archived_at__isnull=True
            ).distinct().count(),

            # Newest first. `Book` records no date, so id order is what the
            # table actually knows about "recently added" - the same proxy
            # the staff dashboard uses.
            "recent_books": describe_availability(
                list(public_books().order_by("-id")[:PUBLIC_DASHBOARD_BOOKS])
            ),

            # "Popular" here means how much of the collection is on that
            # subject, which is a fact about the shelves. It deliberately
            # does not mean how often it is borrowed: that is circulation,
            # and circulation is not public.
            "popular_categories": _named_counts(
                Category, ""
            ).order_by("-book_count", "name")[:PUBLIC_DASHBOARD_CATEGORIES],

            "public_theme": theme,
            "theme_links": public_theme_links(request, theme),
        },
    ), theme, theme_changed)


@login_not_required
def public_author_list(request):
    return _browse_page(
        request,
        Author,
        "public/browse_list.html",
        gettext("Authors"),
        "bi-person",
        "author",
        "authors",
    )


@login_not_required
def public_category_list(request):
    return _browse_page(
        request,
        Category,
        "public/browse_list.html",
        gettext("Categories"),
        "bi-tags",
        "category",
        "categories",
    )


@login_not_required
def public_publisher_list(request):
    return _browse_page(
        request,
        Publisher,
        "public/browse_list.html",
        gettext("Publishers"),
        "bi-building",
        "publisher",
        "publishers",
    )


@login_not_required
def public_book_list(request):
    """Browse and search the catalogue.

    Everything is a GET parameter and every parameter is validated here
    rather than trusted: an id that is not a number is discarded by
    `numeric_param`, an availability value that is not one of the two is
    read as no filter, and anything else in the query string is simply not
    read. So a crafted URL narrows the list or does nothing; there is no
    parameter that can widen it.

    Filtering, ordering and paging all happen in SQL. One page of rows is
    fetched however large the catalogue grows, and the availability shown
    beside each one was counted by the same query that fetched it - there
    is no per-book query anywhere on this page.
    """

    search = (request.GET.get("search") or "").strip()

    # Ids, not names: validated as integers before they reach a query, so a
    # non-numeric value is "no filter" rather than a 500.
    category_id = numeric_param(request, "category")
    author_id = numeric_param(request, "author")
    publisher_id = numeric_param(request, "publisher")

    availability = (request.GET.get("availability") or "").strip()

    if availability not in PUBLIC_AVAILABILITY_FILTERS:
        availability = ""

    books = public_books()

    if search:
        # Title or author from the one box, the same pair the staff list
        # searches and in the same way - case-insensitive, partial, and
        # backed by the trigram indexes that already exist on both columns.
        #
        # `author` is a forward many-to-one and NOT NULL, so this join
        # cannot multiply rows and needs no `.distinct()`.
        books = books.filter(
            Q(title__icontains=search)
            | Q(author__name__icontains=search)
        )

    if category_id:
        books = books.filter(category_id=category_id)

    if author_id:
        books = books.filter(author_id=author_id)

    if publisher_id:
        books = books.filter(publisher_id=publisher_id)

    # Applied after the annotation, so it filters on the same numbers the
    # page displays.
    if availability == "available":
        books = books.filter(available_copies__gt=0)

    elif availability == "unavailable":
        books = books.filter(available_copies=0)

    # Title order, with the id breaking ties: two books of the same name
    # would otherwise come back in whatever order the database felt like,
    # and a list that reshuffles between two page loads is not a list.
    books = books.order_by("title", "id")

    paginator = Paginator(books, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    describe_availability(page.object_list)

    theme, theme_changed = resolve_public_theme(request)

    return remember_public_theme(render(
        request,
        "public/book_list.html",
        {
            "public_theme": theme,
            # The switcher's three links, each carrying the rest of the
            # query so choosing an appearance never drops a search or a
            # page. Built here rather than in the template because
            # `query_with` is where every other link on this page gets its
            # state from.
            "theme_links": public_theme_links(request, theme),
            "nav_section": "books",
            "books": page,
            "paginator": paginator,
            # Everything except `page`, so a filter and a search survive
            # being paged through.
            "pagination_query": query_with(request, page=None),
            "elided_page_range": list(
                paginator.get_elided_page_range(
                    page.number,
                    on_each_side=1,
                    on_ends=1,
                )
            ),
            "page_ellipsis": Paginator.ELLIPSIS,
            "search": search,
            "category_id": category_id,
            "availability": availability,
            "availability_filters": [
                (value, PUBLIC_AVAILABILITY_LABELS[value])
                for value in PUBLIC_AVAILABILITY_FILTERS
            ],
            # One query, bounded by how many categories a library has
            # rather than by how many books - so this is a select, while
            # authors and publishers are links from a book instead of a
            # dropdown that would grow without limit.
            "categories": Category.objects.order_by("name"),
            # Named only when actually filtered on, for the chip that
            # offers to drop it.
            "author_name": named(Author, author_id),
            "publisher_name": named(Publisher, publisher_id),
            "author_id": author_id,
            "publisher_id": publisher_id,
            "has_filters": bool(
                search
                or category_id
                or author_id
                or publisher_id
                or availability
            ),
        },
    ), theme, theme_changed)


@login_not_required
def public_book_detail(request, book_id):
    """One book, as a visitor may see it.

    A separate view and a separate template from the staff `book_detail`,
    which is untouched and still requires a login. Nothing is shared but
    the queryset helper, and that helper is the public one.

    An archived book and a book that never existed both end here as the
    same 404 page, rendered directly rather than raised: Django's project
    404 belongs to the staff application and offers a way back to the
    dashboard, which is not a door a visitor can open. Answering both the
    same way is what keeps a crafted id from confirming that a hidden
    record exists.
    """

    book = public_books().filter(id=book_id).first()

    if book is None:
        return render(request, "public/not_found.html", status=404)

    book.availability = availability_of(book)

    # Volume number and title, and nothing else about them. No copies, no
    # copy codes, no shelves: a visitor is told what the work is made of,
    # not where each piece of it sits.
    volumes = list(
        BookVolume.objects.filter(book=book)
        .order_by("volume_number")
        .only("id", "volume_number", "title")
    )

    theme, theme_changed = resolve_public_theme(request)

    return remember_public_theme(render(
        request,
        "public/book_detail.html",
        {
            "nav_section": "books",
            "book": book,
            "volumes": volumes,
            "public_theme": theme,
            # The same three links as the list. This page has no other
            # query state to carry, so they are the bare parameter.
            "theme_links": public_theme_links(request, theme),
        },
    ), theme, theme_changed)
