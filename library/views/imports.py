"""Bringing books in from a spreadsheet, and sending them back out.

The import is one view over four steps - upload, map the columns, review
what was found, then write - with the part-finished upload held in the
session and swept up if it is abandoned. The exports are plain downloads.
"""

from datetime import timedelta
import io
import os
from uuid import uuid4

from django.conf import settings
from django.contrib import messages
from django.core.files.storage import FileSystemStorage
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext
from django.db import DatabaseError, IntegrityError, transaction

from ..models import (
    Author,
    Book,
    BookCopy,
    BookVolume,
    Category,
    Location,
    Publisher,
    Shelf,
)

from .. import excel as book_excel

from ..permissions import feature_required

from .common import (
    AUTHOR_CACHE_KEY,
    BOOK_CACHE_KEY,
    BOOK_COPY_CACHE_KEY,
    BOOK_VOLUME_CACHE_KEY,
    CATEGORY_CACHE_KEY,
    DASHBOARD_CACHE_KEY,
    MAX_COPIES_PER_VOLUME,
    MAX_TOTAL_COPIES,
    MAX_VOLUMES,
    PUBLISHER_CACHE_KEY,
    create_activity_log,
    create_book_copies,
    numeric_param,
)


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


@feature_required("books", "Admin", "Librarian")
def book_import_template(request):
    """The blank template, with its instructions and example rows."""

    return workbook_response(
        book_excel.build_template(),
        "madrasah-library-book-template.xlsx",
    )


@feature_required("books", "Admin", "Librarian")
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


@feature_required("books", "Admin", "Librarian")
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
        messages.info(request, gettext("Import cancelled."))
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


@feature_required("books", "Admin", "Librarian")
def book_import_errors(request):
    """The last import's failures as a workbook, so the file can be fixed.

    Built from what the session kept about that import rather than from a
    fresh read, because by now the upload itself is gone.
    """

    failures = request.session.get(IMPORT_FAILURE_KEY) or []

    if not failures:
        messages.info(request, gettext("There is no error report to download."))
        return redirect("book_import")

    return workbook_response(
        book_excel.build_error_report(failures),
        "madrasah-library-import-errors.xlsx",
    )
