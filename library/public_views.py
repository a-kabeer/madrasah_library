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
from django.db.models import Q
from django.shortcuts import render

from .models import Author, BookVolume, Category, Publisher
from .views import (
    PAGE_SIZE,
    active_books,
    annotate_copy_counts,
    numeric_param,
    query_with,
)


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

    return render(
        request,
        "public/book_list.html",
        {
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
    )


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

    return render(
        request,
        "public/book_detail.html",
        {
            "book": book,
            "volumes": volumes,
        },
    )
