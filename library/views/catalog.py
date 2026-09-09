"""The catalogue: books, and the names books are filed under.

Authors, categories and publishers first - one table with a different noun
in it - then the books themselves, then the volumes a book is divided into
and the contents inside a volume.
"""

import json

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.core.cache import cache
from django.utils import timezone
from django.db import IntegrityError, models, transaction

from ..models import (
    Author,
    Book,
    BookContent,
    BookCopy,
    BookVolume,
    Category,
    Loan,
    Location,
    Publisher,
)

from .. import acquisitions
from .. import history
from .. import inventory
from .. import reservations

from ..permissions import can_edit_library, feature_required, role_required

from .common import (
    AUTHOR_CACHE_KEY,
    BOOK_CACHE_KEY,
    BOOK_CONTENT_CACHE_KEY,
    BOOK_COPY_CACHE_KEY,
    BOOK_DETAIL_TABS,
    BOOK_VOLUME_CACHE_KEY,
    CATEGORY_CACHE_KEY,
    COPY_STATE_ALWAYS_SHOWN,
    COPY_STATE_LABELS,
    COPY_STATE_ORDER,
    COPY_STATE_TONES,
    COPY_STORED_STATES,
    PolicyRefused,
    DASHBOARD_CACHE_KEY,
    MAX_COPIES_PER_VOLUME,
    MAX_TOTAL_COPIES,
    PAGE_SIZE,
    PUBLISHER_CACHE_KEY,
    book_saved_response,
    combobox_created_response,
    combobox_options_response,
    create_activity_log,
    create_book_copies,
    describe_copies,
    describe_loans,
    is_combobox_request,
    is_form_modal_request,
    is_modal_request,
    lookup_delete_blocker,
    lookup_delete_modal,
    lookup_deleted_response,
    lookup_form_modal,
    lookup_options,
    lookup_saved_response,
    lookup_table,
    numeric_param,
    read_copy_plan,
    read_volume_rows,
    selected_name,
    shelf_options_for,
    validate_cover_image,
    volume_label,
)


#Category View
@feature_required("categories")
def category_list(request):

    search = request.GET.get("search", "").strip()

    categories = Category.objects.all()

    if search:
        categories = categories.filter(name__icontains=search)

    # The searchable dropdowns on Add/Edit Book ask this view for their
    # suggestions. Answered before the book counts and the paging: a
    # suggestion list wants a name and an id, not a table.
    if is_combobox_request(request):

        return combobox_options_response(
            request,
            items=lookup_options(
                Category,
                CATEGORY_CACHE_KEY,
                search,
                categories,
            ),
            search=search,
            entity_label="category",
            add_url=reverse("category_add"),
        )

    page, context = lookup_table(request, categories, "category")

    return render(
        request,
        "library/category_list.html",
        dict(context, categories=page, search=search),
    )

#Category Add
@feature_required("categories", "Admin", "Librarian")
def category_add(request):
    """Add a category, in the list's dialog or from a book form's dropdown.

    Two callers, and they want different answers. The dropdown posts a
    name and wants the record back so it can select it; the dialog wants
    the list to redraw behind it. Everything before that point is the
    same.
    """

    error = None
    name = ""
    from_combobox = is_combobox_request(request)
    modal = is_form_modal_request(request)

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
                user=request.user,
                action="CREATE",
                entity_type="Category",
                entity_id=category.id,
                description=f"{category.name} شامل کی گئی",
            )

            if from_combobox:

                return combobox_created_response("category", category)

            if modal:
                return lookup_saved_response(category.name)

            return redirect("category_list")

    if modal:
        return lookup_form_modal(
            request,
            "category",
            reverse("category_add"),
            error=error,
            name=name,
        )

    # There is no page of its own any more - the form is a dialog on the
    # list. A request that arrives without one goes there, carrying
    # whatever the validation had to say.
    if error:
        messages.error(request, error)

    return redirect("category_list")


#Category Edit
@feature_required("categories", "Admin", "Librarian")
def category_edit(request, category_id):

    category = get_object_or_404(Category, id=category_id)

    modal = is_form_modal_request(request)
    error = None
    name = category.name

    if request.method == "POST":
        name = request.POST.get("name", "").strip()

        # Checked rather than left to the unique index. Renaming one
        # category onto another's name used to reach the database and come
        # back as a 500; the dialog can say so instead.
        taken = Category.objects.filter(
            name__iexact=name
        ).exclude(id=category.id).exists()

        if not name:

            error = "Category name is required."

        elif taken:

            error = "A category with this name already exists."

        else:
            category.name = name
            category.save()

            cache.delete(CATEGORY_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="Category",
                entity_id=category.id,
                description=f"{category.name} updated",
            )

            if modal:
                return lookup_saved_response(category.name)

            return redirect("category_list")

    if modal:
        return lookup_form_modal(
            request,
            "category",
            reverse("category_edit", args=[category.id]),
            error=error,
            name=name,
            editing=True,
        )

    if error:
        messages.error(request, error)

    return redirect("category_list")


#Category Delete
@feature_required("categories", "Admin", "Librarian")
def category_delete(request, category_id):

    category = get_object_or_404(Category, id=category_id)

    modal = is_form_modal_request(request)
    blocker = lookup_delete_blocker("category", category.id)

    if request.method == "POST" and not blocker:

        deleted_category_id = category.id
        deleted_category_name = category.name

        category.delete()

        cache.delete(CATEGORY_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="DELETE",
            entity_type="Category",
            entity_id=deleted_category_id,
            description=f"{deleted_category_name} deleted",
        )

        if modal:
            return lookup_deleted_response(deleted_category_name)

        return redirect("category_list")

    if modal:
        return lookup_delete_modal(
            request,
            "category",
            reverse("category_delete", args=[category.id]),
            category.name,
            blocker,
        )

    if blocker:
        messages.error(request, blocker)

    return redirect("category_list")


#Author View
@feature_required("authors")
def author_list(request):

    search = request.GET.get("search", "").strip()

    authors = Author.objects.all()

    if search:
        authors = authors.filter(name__icontains=search)

    # The searchable dropdowns on Add/Edit Book ask this view for their
    # suggestions. Answered before the book counts and the paging: a
    # suggestion list wants a name and an id, not a table.
    if is_combobox_request(request):

        return combobox_options_response(
            request,
            items=lookup_options(
                Author,
                AUTHOR_CACHE_KEY,
                search,
                authors,
            ),
            search=search,
            entity_label="author",
            add_url=reverse("author_add"),
        )

    page, context = lookup_table(request, authors, "author")

    return render(
        request,
        "library/author_list.html",
        dict(context, authors=page, search=search),
    )

#Author Add
@feature_required("authors", "Admin", "Librarian")
def author_add(request):
    """Add an author, in the list's dialog or from a book form's dropdown.

    Two callers, and they want different answers. The dropdown posts a
    name and wants the record back so it can select it; the dialog wants
    the list to redraw behind it. Everything before that point is the
    same.
    """

    error = None
    name = ""
    from_combobox = is_combobox_request(request)
    modal = is_form_modal_request(request)

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
                user=request.user,
                action="CREATE",
                entity_type="Author",
                entity_id=author.id,
                description=f"{author.name} شامل کیے گئے",
            )

            if from_combobox:

                return combobox_created_response("author", author)

            if modal:
                return lookup_saved_response(author.name)

            return redirect("author_list")

    if modal:
        return lookup_form_modal(
            request,
            "author",
            reverse("author_add"),
            error=error,
            name=name,
        )

    # There is no page of its own any more - the form is a dialog on the
    # list. A request that arrives without one goes there, carrying
    # whatever the validation had to say.
    if error:
        messages.error(request, error)

    return redirect("author_list")


#Author Edit
@feature_required("authors", "Admin", "Librarian")
def author_edit(request, author_id):

    author = get_object_or_404(Author, id=author_id)

    modal = is_form_modal_request(request)
    error = None
    name = author.name

    if request.method == "POST":
        name = request.POST.get("name", "").strip()

        # Checked rather than left to the unique index. Renaming one
        # author onto another's name used to reach the database and come
        # back as a 500; the dialog can say so instead.
        taken = Author.objects.filter(
            name__iexact=name
        ).exclude(id=author.id).exists()

        if not name:

            error = "Author name is required."

        elif taken:

            error = "An author with this name already exists."

        else:
            author.name = name
            author.save()

            cache.delete(AUTHOR_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="Author",
                entity_id=author.id,
                description=f"{author.name} updated",
            )

            if modal:
                return lookup_saved_response(author.name)

            return redirect("author_list")

    if modal:
        return lookup_form_modal(
            request,
            "author",
            reverse("author_edit", args=[author.id]),
            error=error,
            name=name,
            editing=True,
        )

    if error:
        messages.error(request, error)

    return redirect("author_list")


#Author Delete
@feature_required("authors", "Admin", "Librarian")
def author_delete(request, author_id):

    author = get_object_or_404(Author, id=author_id)

    modal = is_form_modal_request(request)
    blocker = lookup_delete_blocker("author", author.id)

    if request.method == "POST" and not blocker:

        deleted_author_id = author.id
        deleted_author_name = author.name

        author.delete()

        cache.delete(AUTHOR_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="DELETE",
            entity_type="Author",
            entity_id=deleted_author_id,
            description=f"{deleted_author_name} deleted",
        )

        if modal:
            return lookup_deleted_response(deleted_author_name)

        return redirect("author_list")

    if modal:
        return lookup_delete_modal(
            request,
            "author",
            reverse("author_delete", args=[author.id]),
            author.name,
            blocker,
        )

    if blocker:
        messages.error(request, blocker)

    return redirect("author_list")


#Publisher View
@feature_required("publishers")
def publisher_list(request):

    search = request.GET.get("search", "").strip()

    publishers = Publisher.objects.all()

    if search:
        # City as well as name, which is what this page has always
        # searched: a publisher is often remembered by where it is.
        publishers = publishers.filter(
            models.Q(name__icontains=search)
            | models.Q(city__icontains=search)
        )

    # The searchable dropdowns on Add/Edit Book ask this view for their
    # suggestions. Answered before the book counts and the paging: a
    # suggestion list wants a name and an id, not a table.
    if is_combobox_request(request):

        return combobox_options_response(
            request,
            items=lookup_options(
                Publisher,
                PUBLISHER_CACHE_KEY,
                search,
                publishers,
            ),
            search=search,
            entity_label="publisher",
            add_url=reverse("publisher_add"),
        )

    page, context = lookup_table(request, publishers, "publisher")

    return render(
        request,
        "library/publisher_list.html",
        dict(context, publishers=page, search=search),
    )

#Publisher Add
@feature_required("publishers", "Admin", "Librarian")
def publisher_add(request):
    """Add a publisher, in the list's dialog or from a book form's dropdown.

    The one lookup with a second field. The dropdown can only send a name,
    so a publisher created that way has no city until somebody edits it.
    """

    from_combobox = is_combobox_request(request)
    modal = is_form_modal_request(request)

    error = None
    name = ""
    city = ""

    if request.method == "POST":

        name = request.POST.get("name", "").strip()
        city = request.POST.get("city", "").strip()

        duplicate = (
            Publisher.objects.filter(name__iexact=name).first()
            if name
            else None
        )

        if not name:

            error = "Publisher name is required."

        elif duplicate is not None:

            if from_combobox:

                return combobox_created_response("publisher", duplicate)

            error = "A publisher with this name already exists."

        else:

            publisher = Publisher.objects.create(
                name=name,
                city=city or None,
            )

            cache.delete(PUBLISHER_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=request.user,
                action="CREATE",
                entity_type="Publisher",
                entity_id=publisher.id,
                description=f"{publisher.name} شامل کیا گیا",
            )

            if from_combobox:

                return combobox_created_response("publisher", publisher)

            if modal:
                return lookup_saved_response(publisher.name)

            return redirect("publisher_list")

    if modal:
        return lookup_form_modal(
            request,
            "publisher",
            reverse("publisher_add"),
            error=error,
            name=name,
            city=city,
        )

    # There is no page of its own any more - the form is a dialog on the
    # list. A request that arrives without one goes there, carrying
    # whatever the validation had to say.
    if error:
        messages.error(request, error)

    return redirect("publisher_list")


#Publisher Edit
@feature_required("publishers", "Admin", "Librarian")
def publisher_edit(request, publisher_id):

    publisher = get_object_or_404(Publisher, id=publisher_id)

    modal = is_form_modal_request(request)

    error = None
    name = publisher.name
    city = publisher.city or ""

    if request.method == "POST":

        name = request.POST.get("name", "").strip()
        city = request.POST.get("city", "").strip()

        taken = Publisher.objects.filter(
            name__iexact=name
        ).exclude(id=publisher.id).exists()

        if not name:

            error = "Publisher name is required."

        elif taken:

            error = "A publisher with this name already exists."

        else:

            publisher.name = name
            publisher.city = city or None

            publisher.save()

            cache.delete(PUBLISHER_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            create_activity_log(
                user=request.user,
                action="UPDATE",
                entity_type="Publisher",
                entity_id=publisher.id,
                description=f"{publisher.name} updated",
            )

            if modal:
                return lookup_saved_response(publisher.name)

            return redirect("publisher_list")

    if modal:
        return lookup_form_modal(
            request,
            "publisher",
            reverse("publisher_edit", args=[publisher.id]),
            error=error,
            name=name,
            city=city,
            editing=True,
        )

    if error:
        messages.error(request, error)

    return redirect("publisher_list")


#Publisher Delete
@feature_required("publishers", "Admin", "Librarian")
def publisher_delete(request, publisher_id):

    publisher = get_object_or_404(Publisher, id=publisher_id)

    modal = is_form_modal_request(request)
    blocker = lookup_delete_blocker("publisher", publisher.id)

    if request.method == "POST" and not blocker:

        deleted_publisher_id = publisher.id
        deleted_publisher_name = publisher.name

        publisher.delete()

        cache.delete(PUBLISHER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="DELETE",
            entity_type="Publisher",
            entity_id=deleted_publisher_id,
            description=f"{deleted_publisher_name} deleted",
        )

        if modal:
            return lookup_deleted_response(deleted_publisher_name)

        return redirect("publisher_list")

    if modal:
        return lookup_delete_modal(
            request,
            "publisher",
            reverse("publisher_delete", args=[publisher.id]),
            publisher.name,
            blocker,
        )

    if blocker:
        messages.error(request, blocker)

    return redirect("publisher_list")


def normalize_title(value):
    """A title reduced to what a duplicate check should compare.

    Case, leading and trailing space, and runs of whitespace are accidents
    of typing rather than different books. Nothing else is touched:
    punctuation and diacritics distinguish real titles, especially in
    Arabic and Urdu, and folding them away would merge records that are
    genuinely different.
    """

    return " ".join((value or "").split()).lower()


def normalized_title_expression():
    """`books.title` reduced in SQL exactly as `normalize_title` reduces it.

    Collapse runs of whitespace, trim the ends, lower the case. Written
    once and used by everything that compares a title, so the catalogue's
    duplicate check and the suggestion form's duplicate warning can never
    end up disagreeing about whether two titles are the same. `lower()` is
    used on both sides rather than Python's `casefold`, for the same
    reason.
    """

    return models.Func(
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


def book_field_error_summary(errors):
    """What the alert above the form says when fields are wrong.

    A count, not a list. Each message is already under the input it is
    about, and repeating them all at the top would say everything twice
    while still not pointing anywhere.
    """

    if len(errors) == 1:
        return "There is a problem with one of the fields below."

    return (
        "There are problems with %d of the fields below." % len(errors)
    )


# The longest title the column will take. Stated here because the form has
# to refuse a longer one itself: `books.title` is varchar(500), and reaching
# it with more raises DataError, which is a 500 rather than an answer.
TITLE_MAX_LENGTH = 500


def resolve_related(model, raw, label, errors, field):
    """An id from the form, checked against the table it names.

    The comboboxes post an id in a hidden field, so what arrives is
    whatever that field held - and a page left open while somebody else
    deleted the author, or a cleared script, can post a value that names
    no row or is not a number at all. Unchecked, the first went to the
    database as a dangling foreign key and the second raised ValueError
    from `create()`.

    Returns the id when it names a row, and None otherwise, recording why
    under `field`.
    """

    value = (raw or "").strip()

    if not value:
        return None

    # Both sentences take the bare noun - "author", not "an author" - so
    # one label serves all three fields without an article that fits only
    # one of them.
    if not value.isdigit():
        errors[field] = (
            "The %s was not recognised. Pick one from the list." % label
        )
        return None

    if not model.objects.filter(pk=int(value)).exists():
        errors[field] = (
            "That %s no longer exists. Pick another from the list."
            % label
        )
        return None

    return int(value)


def validate_book_details(request, book=None):
    """The rules a book's own details are held to, in one place.

    Add and Edit both call this, so the two cannot come apart. It reads
    the POST and answers three things:

      * `cleaned` - what to save: title, the three ids, the cover upload
      * `errors` - field name -> what is wrong with it, empty when nothing
        is. Each one is rendered under its own input.
      * `form_data` - what to show back, so a refused form keeps every
        value that was typed into it

    Duplicates are deliberately not checked here. Whether this book is
    already on the shelves is not a fault in a field - it needs the list
    of matches and its own wording - so each view asks
    `find_duplicate_books` itself, Add over the whole catalogue and Edit
    excluding the book being edited.
    """

    errors = {}

    title = request.POST.get("title", "").strip()

    if not title:
        errors["title"] = "Enter the book's title."

    elif len(title) > TITLE_MAX_LENGTH:
        errors["title"] = (
            "That title is %d characters. Shorten it to %d or fewer."
            % (len(title), TITLE_MAX_LENGTH)
        )

    author_id = resolve_related(
        Author, request.POST.get("author"), "author", errors, "author"
    )

    if author_id is None and "author" not in errors:
        errors["author"] = "Choose the author."

    category_id = resolve_related(
        Category,
        request.POST.get("category"),
        "category",
        errors,
        "category",
    )

    publisher_id = resolve_related(
        Publisher,
        request.POST.get("publisher"),
        "publisher",
        errors,
        "publisher",
    )

    cover_image = request.FILES.get("cover_image")

    if cover_image:
        cover_error = validate_cover_image(cover_image)

        if cover_error:
            errors["cover_image"] = cover_error

    cleaned = {
        "title": title,
        "author_id": author_id,
        "category_id": category_id,
        "publisher_id": publisher_id,
        "cover_image": cover_image,
    }

    # Shown back exactly as posted, including a value that was refused -
    # the names come from the ids, so a rejected id shows an empty box,
    # which is the truth about what the form is holding.
    form_data = {
        "title": title,
        "author": author_id or "",
        "author_name": selected_name(Author, author_id),
        "category": category_id or "",
        "category_name": selected_name(Category, category_id),
        "publisher": publisher_id or "",
        "publisher_name": selected_name(Publisher, publisher_id),
    }

    return cleaned, errors, form_data


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
        normalized_title=normalized_title_expression()
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


def suggestion_matches(title, author_name, limit=5):
    """Books already in the catalogue that a suggestion may duplicate.

    Task 4's comparison and no other. When the suggested author names an
    author the catalogue already knows, this *is* `find_duplicate_books` -
    the same exact, normalised title-and-author match that `book_add`
    refuses a duplicate on. With no author, or an author nobody has
    catalogued yet, it falls back to the same normalised title against
    every book, which is the identical comparison with one side left off.

    There is deliberately no second, looser matcher. A suggestion is a note
    somebody typed, so a fuzzy search over it would produce misses that
    mean nothing and near-misses nobody could act on - and this warns
    rather than refuses, so being wrong is expensive in exactly the wrong
    direction.

    Bounded by `limit` in the database. The warning names a few books; it
    is not a search results page.
    """

    normalized = normalize_title(title)

    if not normalized:
        return []

    author = (
        Author.objects.filter(name__iexact=author_name.strip()).first()
        if author_name and author_name.strip()
        else None
    )

    if author is not None:
        return list(find_duplicate_books(title, author.id)[:limit])

    return list(
        Book.objects.annotate(
            normalized_title=normalized_title_expression()
        ).filter(
            normalized_title=normalized
        ).select_related("author").order_by("id")[:limit]
    )


def suggestion_prefill(suggestion):
    """`book_add`'s form_data, filled in from an approved suggestion.

    Only what is safe to fill in, and nothing at all is created. The title
    is the suggestion's own text and still has to be confirmed. The author
    and publisher are catalogue records looked up by exact name, and are
    left blank when nobody of that name is catalogued yet - a suggestion
    saying "ibn kathir" must not add an `Author` called that, so the
    librarian picks or creates one in the form, exactly as they would
    without a suggestion.

    Every value here comes off the stored row, not off the request. The URL
    carries one integer - which suggestion - so there is no parameter a
    caller could use to put a value into this form that the form would not
    have validated anyway.
    """

    author = Author.objects.filter(
        name__iexact=suggestion.author_name.strip()
    ).first() if suggestion.author_name.strip() else None

    publisher = Publisher.objects.filter(
        name__iexact=suggestion.publisher_name.strip()
    ).first() if suggestion.publisher_name.strip() else None

    return {
        "title": suggestion.title,
        "author": str(author.id) if author else "",
        "author_name": author.name if author else "",
        "category": "",
        "category_name": "",
        "publisher": str(publisher.id) if publisher else "",
        "publisher_name": publisher.name if publisher else "",
    }


@feature_required("books", "Admin", "Librarian")
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
    errors = {}
    duplicates = []

    # The catalogue shortcut from an approved suggestion. One integer in
    # the query string, and everything it leads to is read from that row -
    # so it cannot carry a value into the form, and cannot skip a check.
    #
    # It rides on the query string rather than a hidden field because the
    # form has no `action` and therefore posts back to this same URL: the
    # link survives a validation error and a re-render without the
    # template knowing anything about suggestions. `?modal=1` already
    # works exactly this way.
    #
    # `open_for_catalogue` answers only for an Approved suggestion, so a
    # Pending or Rejected one prefills nothing and - the half that matters
    # - is never marked Acquired further down.
    suggestion = acquisitions.open_for_catalogue(
        numeric_param(request, "suggestion")
    )

    form_data = {
        "title": "",
        "author": "",
        "author_name": "",
        "category": "",
        "category_name": "",
        "publisher": "",
        "publisher_name": "",
    }

    if suggestion is not None and request.method != "POST":
        form_data = suggestion_prefill(suggestion)

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

        # The rules, from the one place that states them. Edit calls the
        # same function, so the two cannot come apart.
        cleaned, errors, form_data = validate_book_details(request)

        title = cleaned["title"]
        author_id = cleaned["author_id"]
        category_id = cleaned["category_id"]
        publisher_id = cleaned["publisher_id"]
        cover_image = cleaned["cover_image"]

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

        if errors:

            # Each one is rendered under its own input. The alert above the
            # form is for problems that are not about a single field, so it
            # only says how many there are.
            error = book_field_error_summary(errors)

        elif duplicates:

            error = duplicate_book_error(duplicates)

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
                    user=request.user,
                    action="CREATE",
                    entity_type="Book",
                    entity_id=book.id,
                    description=f"{book.title} شامل کی گئی",
                )

                # This book was added through a particular approved
                # suggestion's shortcut, so that suggestion is now
                # Acquired. A conditional UPDATE, so it happens once and
                # only from Approved.
                #
                # Adding a book the ordinary way marks nothing: there is
                # no suggestion on the request, `suggestion` is None, and
                # this is not reached. Adding a *similar* book without the
                # shortcut marks nothing either - nothing here compares
                # titles.
                if suggestion is not None:

                    acquisitions.mark_acquired(
                        suggestion.id, user=request.user
                    )

                    create_activity_log(
                        user=request.user,
                        action="UPDATE",
                        entity_type="AcquisitionSuggestion",
                        entity_id=suggestion.id,
                        description=(
                            "Suggestion '%s' added to the catalogue as %s"
                            % (suggestion.title, book.title)
                        ),
                    )

                for volume in volumes:
                    create_activity_log(
                        user=request.user,
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
                        user=request.user,
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
        "errors": errors,
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

    # There is no Add Book page any more - the dialog was always the
    # whole of it, and the page a shell around the same partials. A
    # request without `?modal=1` still has to answer, because links to
    # this URL exist, so it goes to the list with the reason attached.
    if error:
        messages.error(request, error)

    return redirect("book_list")


@feature_required("books", "Admin", "Librarian")
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
    errors = {}
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

        # The same rules Add is held to, from the same function.
        cleaned, errors, form_data = validate_book_details(request, book)

        title = cleaned["title"]
        author_id = cleaned["author_id"]
        category_id = cleaned["category_id"]
        publisher_id = cleaned["publisher_id"]
        cover_image = cleaned["cover_image"]

        remove_cover = request.POST.get("remove_cover") == "on"

        # `exclude_id` is what stops a book being its own duplicate: save
        # it unchanged and the only match is itself, which is dropped.
        duplicates = (
            list(find_duplicate_books(title, author_id, exclude_id=book.id)[:5])
            if title and author_id
            else []
        )

        if errors:

            error = book_field_error_summary(errors)

        elif duplicates:

            error = duplicate_book_error(duplicates)

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
                user=request.user,
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
        "errors": errors,
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

    # No page left. Back to wherever the edit was started from, with
    # the reason if there was one.
    if error:
        messages.error(request, error)

    if from_page == "detail":
        return redirect("book_detail", book_id=book.id)

    return redirect("book_list")


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


@feature_required("books", "Admin", "Librarian")
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


@feature_required("books", "Admin", "Librarian")
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


@feature_required("copies", "Admin", "Librarian")
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

        # The copy's own page is a dialog now, and its URL redirects to
        # the list - so going there took two hops to reach one place.
        return redirect("book_copy_list")

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


@feature_required("books", "Admin", "Librarian")
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
        # database), and `blocker` has established there are no copies
        # hanging off them.
        #
        # Asked again inside the transaction, because the answer above was
        # read outside any transaction: a copy added, or a loan issued,
        # between the two would otherwise reach `book_copies.volume_id`
        # and abort the statement - a 500 where there is a sentence to
        # say. Raising is how a transaction that must not commit gets out.
        try:
            with transaction.atomic():

                blocker = book_delete_blocker(book)

                if blocker:
                    raise PolicyRefused(blocker)

                book.delete()

        except PolicyRefused as refused:
            blocker = str(refused)

        else:
            cache.delete(BOOK_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)

            if volume_count:
                cache.delete(BOOK_VOLUME_CACHE_KEY)

            create_activity_log(
                user=request.user,
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

    # No page left. A blocker means the book is still there, so the
    # reason goes back with the reader to it; otherwise to the list.
    if blocker:
        messages.error(request, blocker)

        if from_page == "detail":
            return redirect("book_detail", book_id=book.id)

    return redirect("book_list")

@feature_required("books")
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


@feature_required("books")
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


@feature_required("books", "Admin", "Librarian")
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
                user=request.user,
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

@feature_required("books")
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

@feature_required("books", "Admin", "Librarian")
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
                user=request.user,
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


@feature_required("books", "Admin", "Librarian")
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
            user=request.user,
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


@feature_required("books")
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

@feature_required("books")
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

@feature_required("books", "Admin", "Librarian")
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
                user=request.user,
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


@feature_required("books", "Admin", "Librarian")
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


@feature_required("books", "Admin", "Librarian")
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
            user=request.user,
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
