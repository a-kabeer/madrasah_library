"""Reading and writing the bulk import/export workbooks.

Kept out of views.py because it is all workbook mechanics: opening a file
safely, working out which spreadsheet column means what, turning rows into
book-shaped groups, and saying what is wrong with them. The views own the
steps and the database writes; this module never touches either.

The rules it enforces are the ones the Add Book form already enforces —
`library.views` owns those, and `plan_import` is handed the callables it
needs rather than importing them, so there is one set of rules and no
import cycle.
"""

from collections import OrderedDict
import re

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# What a spreadsheet is allowed to weigh. Generous for a catalogue — tens
# of thousands of rows fit well inside it — and small enough that a wrong
# file cannot exhaust memory before it is rejected.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# Rows we will look at in one go. Beyond this the answer is "split the
# file", which is honest, rather than a request that times out.
MAX_ROWS = 5000

# .xlsx files are zip archives; every one starts with this. Checked instead
# of trusting the file name, which anyone can write.
ZIP_MAGIC = b"PK\x03\x04"


# ---------------------------------------------------------------- columns

class Column:
    """One column the workbook may carry.

    `key` is what the rest of the code calls it, `label` is the heading
    written into the template, and `aliases` are the other spellings we
    accept so nobody has to rename their own spreadsheet first.
    """

    def __init__(self, key, label, aliases=(), required=False,
                 system=False, help_text=""):
        self.key = key
        self.label = label
        self.aliases = aliases
        self.required = required
        self.system = system
        self.help_text = help_text


# The order here is the order of the template's columns.
COLUMNS = (
    Column(
        "book_id", "Book ID (system — do not edit)",
        aliases=("book id", "id", "book_id"),
        system=True,
        help_text=(
            "Filled in by Export. Leave blank to add a new book; leave it "
            "as it is to update that book."
        ),
    ),
    Column(
        "title", "Book Title",
        aliases=("title", "book", "book name", "name"),
        required=True,
        help_text="Required.",
    ),
    Column(
        "author", "Author",
        aliases=("author name", "writer", "authors"),
        required=True,
        help_text="Required. Created if the library does not have it yet.",
    ),
    Column(
        "category", "Category",
        aliases=("subject", "categories"),
        help_text="Optional. Created if it does not exist yet.",
    ),
    Column(
        "publisher", "Publisher",
        aliases=("publishers", "publishing house"),
        help_text="Optional. Created if it does not exist yet.",
    ),
    Column(
        "volume_number", "Volume Number",
        aliases=("volume", "volume no", "vol", "vol no", "volume #"),
        help_text=(
            "Leave blank for a book that is not split into volumes. For a "
            "multi-volume book, use one row per volume."
        ),
    ),
    Column(
        "volume_title", "Volume Title",
        aliases=("volume name", "vol title", "part title"),
        help_text="Optional name for that volume.",
    ),
    Column(
        "copies", "Number of Copies",
        aliases=("copies", "copy quantity", "quantity", "qty", "no of copies"),
        help_text="How many physical copies to add for this row. 0 or blank for none.",
    ),
    Column(
        "copy_codes", "Copy Codes",
        aliases=("copy code", "codes", "accession number", "accession numbers"),
        help_text=(
            "Optional. Leave blank and codes are generated. To set them "
            "yourself, list one per copy separated by commas."
        ),
    ),
    Column(
        "location", "Location",
        aliases=("location name", "room", "hall"),
        help_text="Required when adding copies. Must already exist.",
    ),
    Column(
        "shelf", "Shelf",
        aliases=("shelf code", "shelf no", "rack"),
        help_text=(
            "Required when adding copies. Must already exist in the "
            "location named on the same row."
        ),
    ),
)

COLUMNS_BY_KEY = OrderedDict((column.key, column) for column in COLUMNS)

REQUIRED_KEYS = tuple(c.key for c in COLUMNS if c.required)

# What a column is mapped to when the user wants it left alone.
IGNORE = ""


def normalise(text):
    """A heading reduced to something worth comparing.

    Case, punctuation and runs of whitespace all vary between people's
    spreadsheets and none of them change the meaning.
    """

    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def guess_mapping(headings):
    """Match each spreadsheet heading to a column, as far as it can.

    Exact label first, then the aliases, both case- and punctuation-blind.
    Anything it cannot place is left for the user to map or ignore, which
    is why this returns a suggestion rather than a decision.
    """

    lookup = {}

    for column in COLUMNS:
        lookup[normalise(column.label)] = column.key

        for alias in column.aliases:
            lookup.setdefault(normalise(alias), column.key)

    mapping = {}
    taken = set()

    for index, heading in enumerate(headings):

        key = lookup.get(normalise(heading))

        # One spreadsheet column per field: a second match is left
        # unmapped rather than quietly overriding the first.
        if key and key not in taken:
            mapping[index] = key
            taken.add(key)
        else:
            mapping[index] = IGNORE

    return mapping


def check_mapping(mapping):
    """What is wrong with a mapping, as a list of messages."""

    problems = []

    used = [key for key in mapping.values() if key != IGNORE]

    for key in used:
        if used.count(key) > 1 and key in COLUMNS_BY_KEY:
            message = (
                "%s is mapped to more than one column."
                % COLUMNS_BY_KEY[key].label
            )

            if message not in problems:
                problems.append(message)

    for key in REQUIRED_KEYS:
        if key not in used:
            problems.append(
                "%s has to be mapped — it is required."
                % COLUMNS_BY_KEY[key].label
            )

    return problems


# ------------------------------------------------------------- the upload

class WorkbookError(Exception):
    """The file cannot be used, with a reason fit to show the user."""


def read_upload(upload):
    """Headings and rows from an uploaded .xlsx, or a plain explanation.

    Everything openpyxl might raise is turned into a `WorkbookError` whose
    message says what to do about it — a traceback or a file path would
    tell the user nothing and expose more than it should.
    """

    if upload.size == 0:
        raise WorkbookError("That file is empty.")

    if upload.size > MAX_UPLOAD_BYTES:
        raise WorkbookError(
            "That file is larger than %d MB. Split it into smaller files."
            % (MAX_UPLOAD_BYTES // (1024 * 1024))
        )

    # An .xlsx is a zip archive. Checking the first bytes catches a .csv or
    # an .xls renamed to .xlsx, which the extension alone never would.
    head = upload.read(len(ZIP_MAGIC))
    upload.seek(0)

    if head != ZIP_MAGIC:
        raise WorkbookError(
            "That does not look like an .xlsx file. Save it from Excel as "
            "\"Excel Workbook (.xlsx)\" and try again."
        )

    try:
        # read_only keeps a large sheet off the heap; data_only takes the
        # cached result of any formula rather than its text.
        book = load_workbook(upload, read_only=True, data_only=True)

    except Exception:
        raise WorkbookError(
            "That file could not be opened. It may be damaged, or saved in "
            "an older Excel format."
        )

    try:
        sheet = book.worksheets[0] if book.worksheets else None

        if sheet is None:
            raise WorkbookError("That workbook has no sheets in it.")

        rows = sheet.iter_rows(values_only=True)

        headings = None

        for candidate in rows:
            if any(str(cell or "").strip() for cell in candidate):
                headings = [str(cell or "").strip() for cell in candidate]
                break

        if not headings:
            raise WorkbookError(
                "That sheet has no column headings. The first row should "
                "name the columns."
            )

        data = []

        for values in rows:

            if len(data) >= MAX_ROWS:
                raise WorkbookError(
                    "That file has more than %d rows. Split it into smaller "
                    "files." % MAX_ROWS
                )

            cells = [_clean(value) for value in values]

            # A row of nothing is a spacer, not a record.
            if not any(cells):
                continue

            data.append(cells)

        if not data:
            raise WorkbookError(
                "That sheet has headings but no rows underneath them."
            )

        return headings, data

    finally:
        book.close()


def _clean(value):
    """One cell as text, with Excel's number formatting undone.

    A volume number typed as 3 arrives as 3.0, and a code typed into a
    General cell can arrive as a float too — both would otherwise be
    written to the database with a stray ".0".
    """

    if value is None:
        return ""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return str(value).strip()


# -------------------------------------------------------------- the plan

class RowProblem:
    """One thing wrong with one row, and whether it stops the import."""

    def __init__(self, row_number, message, blocking=True):
        self.row_number = row_number
        self.message = message
        self.blocking = blocking


class BookGroup:
    """One book and the volumes and copies that go with it.

    Rows are grouped because a multi-volume book is spread over several of
    them. A group is the unit that succeeds or fails together: it is what
    the view wraps a transaction around, so a bad volume never leaves a
    half-built book behind.
    """

    def __init__(self, key, first_row):
        self.key = key
        self.first_row = first_row
        self.rows = []

        self.book_id = None
        self.title = ""
        self.author = ""
        self.category = ""
        self.publisher = ""

        self.volumes = []
        self.problems = []

        # Filled in by the planner.
        self.existing_book = None
        self.action = "create"

        # Copies named on the rows that the book already owns — an
        # unchanged export mentions them, and they are left alone.
        self.existing_copies = 0

    @property
    def blocked(self):
        return any(problem.blocking for problem in self.problems)

    @property
    def row_numbers(self):
        return [row["_row"] for row in self.rows]

    @property
    def copy_total(self):
        return sum(volume["copies"] for volume in self.volumes)

    @property
    def label(self):
        return self.title or "(no title)"


def build_groups(headings, rows, mapping):
    """Turn mapped spreadsheet rows into book groups.

    Grouping is by Book ID when one is given, and otherwise by title and
    author together. Title alone will not do: nothing in the schema makes
    it unique, and two different books can share one.
    """

    groups = OrderedDict()

    for offset, cells in enumerate(rows):

        # +2 because the heading row is row 1 and spreadsheets count from 1,
        # so this is the number the user sees in Excel.
        row_number = offset + 2

        row = {"_row": row_number}

        for index, key in mapping.items():
            if key == IGNORE:
                continue

            row[key] = cells[index] if index < len(cells) else ""

        book_id = (row.get("book_id") or "").strip()
        title = (row.get("title") or "").strip()
        author = (row.get("author") or "").strip()

        if book_id:
            key = ("id", book_id)
        else:
            key = ("new", title.casefold(), author.casefold())

        group = groups.get(key)

        if group is None:
            group = BookGroup(key, row_number)
            group.book_id = book_id
            group.title = title
            group.author = author
            group.category = (row.get("category") or "").strip()
            group.publisher = (row.get("publisher") or "").strip()
            groups[key] = group

        group.rows.append(row)

    return list(groups.values())


def parse_copy_codes(raw):
    """The codes on one row, as a list. Commas, semicolons or newlines."""

    if not raw:
        return []

    return [
        part.strip()
        for part in re.split(r"[,;\n]+", raw)
        if part.strip()
    ]


def plan_import(groups, context, limits):
    """Work out what each group would do, and what is wrong with it.

    `context` holds the lookups the caller has already fetched in bulk —
    books by id, names to ids for authors, categories, publishers,
    locations and shelves, and every copy code with the book it belongs to
    — so nothing here goes near the database. That is what keeps a
    thousand-row file from becoming a thousand queries.

    `limits` carries the same ceilings the Add Book form applies, so a
    spreadsheet cannot ask for something the form would refuse.
    """

    # Codes claimed earlier in this same file, so two rows cannot both
    # claim one.
    claimed = {}

    for group in groups:

        if not group.title:
            group.problems.append(RowProblem(
                group.first_row, "Book Title is missing."
            ))

        if not group.author:
            group.problems.append(RowProblem(
                group.first_row, "Author is missing."
            ))

        # ---- update or create -------------------------------------------
        if group.book_id:

            if not group.book_id.isdigit():
                group.problems.append(RowProblem(
                    group.first_row,
                    "Book ID \"%s\" is not a number. Leave it blank to add "
                    "a new book." % group.book_id,
                ))
            else:
                existing = context["books"].get(int(group.book_id))

                if existing is None:
                    group.problems.append(RowProblem(
                        group.first_row,
                        "No book has ID %s. Leave the column blank to add a "
                        "new book instead." % group.book_id,
                    ))
                else:
                    group.existing_book = existing
                    group.action = "update"

        else:
            # Nothing in the schema makes a title unique, so a match on
            # title and author is a warning rather than a refusal — a
            # library can hold two editions of one work.
            match = context["by_title_author"].get(
                (group.title.casefold(), group.author.casefold())
            )

            if match is not None:
                group.problems.append(RowProblem(
                    group.first_row,
                    "The library already has \"%s\" by %s. This will be "
                    "added as a second, separate book. To update the "
                    "existing one instead, export it and re-import with its "
                    "Book ID." % (group.title, group.author),
                    blocking=False,
                ))

        # ---- related records --------------------------------------------
        for key, label, store in (
            ("author", "Author", "authors"),
            ("category", "Category", "categories"),
            ("publisher", "Publisher", "publishers"),
        ):
            name = getattr(group, key)

            if name and name.casefold() not in context[store]:
                group.problems.append(RowProblem(
                    group.first_row,
                    "%s \"%s\" is new — it will be created." % (label, name),
                    blocking=False,
                ))

        # ---- volumes and copies -----------------------------------------
        seen_numbers = {}

        for row in group.rows:

            row_number = row["_row"]

            raw_number = (row.get("volume_number") or "").strip()
            volume_title = (row.get("volume_title") or "").strip()

            named_volume = bool(raw_number)

            if named_volume:
                if not raw_number.isdigit() or int(raw_number) < 1:
                    group.problems.append(RowProblem(
                        row_number,
                        "Volume Number \"%s\" should be a whole number of 1 "
                        "or more." % raw_number,
                    ))
                    continue

                number = int(raw_number)

            else:
                # No volume number: the book is not split into volumes. It
                # gets the single implicit volume a copy has to hang off —
                # but only if this row actually has copies. A blank cell is
                # not the Add Book form's explicit "Single volume" choice,
                # and creating an empty volume for it would be a change
                # nobody asked for.
                number = 1
                volume_title = ""

            if number in seen_numbers:
                # Not a clash: copies of one volume can sit on more than
                # one shelf, and the export writes a row per shelf. The
                # volume is made once and each row adds its own copies.
                # Said out loud all the same, so a row duplicated by
                # accident is visible rather than silently doubling.
                group.problems.append(RowProblem(
                    row_number,
                    "Volume %d is also on row %d. Both rows' copies will be "
                    "added to the same volume."
                    % (number, seen_numbers[number]),
                    blocking=False,
                ))
            else:
                seen_numbers[number] = row_number

            # ---- copies --------------------------------------------------
            raw_copies = (row.get("copies") or "").strip()
            copies = 0

            if raw_copies:
                if not raw_copies.isdigit():
                    group.problems.append(RowProblem(
                        row_number,
                        "Number of Copies \"%s\" should be a whole number."
                        % raw_copies,
                    ))
                    continue

                copies = int(raw_copies)

                if copies > limits["per_volume"]:
                    group.problems.append(RowProblem(
                        row_number,
                        "At most %d copies per row." % limits["per_volume"],
                    ))
                    continue

            location_name = (row.get("location") or "").strip()
            shelf_code = (row.get("shelf") or "").strip()

            shelf_id = None

            if copies:

                if not location_name or not shelf_code:
                    group.problems.append(RowProblem(
                        row_number,
                        "Copies need both a Location and a Shelf. Leave the "
                        "copy count blank to add the book without copies.",
                    ))
                    continue

                location_id = context["locations"].get(location_name.casefold())

                if location_id is None:
                    group.problems.append(RowProblem(
                        row_number,
                        "No location called \"%s\". Add it in Locations "
                        "first — locations are not created from a "
                        "spreadsheet." % location_name,
                    ))
                    continue

                # The shelf is looked up inside the location, never on its
                # own, so a shelf code that exists somewhere else cannot
                # pull a copy into the wrong room.
                shelf_id = context["shelves"].get(
                    (location_id, shelf_code.casefold())
                )

                if shelf_id is None:
                    group.problems.append(RowProblem(
                        row_number,
                        "Location \"%s\" has no shelf \"%s\"."
                        % (location_name, shelf_code),
                    ))
                    continue

            elif location_name or shelf_code:
                group.problems.append(RowProblem(
                    row_number,
                    "A Location or Shelf is given but no copies, so nothing "
                    "will be placed there.",
                    blocking=False,
                ))

            # ---- copy codes ----------------------------------------------
            codes = parse_copy_codes(row.get("copy_codes"))

            if codes and not copies:
                group.problems.append(RowProblem(
                    row_number,
                    "Copy Codes are given but Number of Copies is blank.",
                ))
                continue

            if codes and len(codes) != copies:
                group.problems.append(RowProblem(
                    row_number,
                    "%d copy code%s given for %d cop%s. Give one per copy, "
                    "or leave the column blank to have them generated."
                    % (len(codes), "" if len(codes) == 1 else "s",
                       copies, "y" if copies == 1 else "ies"),
                ))
                continue

            bad_code = False

            # Codes on this row that the book already owns. An unchanged
            # export re-imports through here: those copies exist, so they
            # are not created again and are not a clash either.
            already_ours = []
            new_codes = []

            owner_id = (
                group.existing_book.id
                if group.existing_book is not None
                else None
            )

            for code in codes:
                folded = code.casefold()

                owner = context["codes"].get(folded)

                if owner is not None:

                    if owner_id is not None and owner == owner_id:
                        already_ours.append(code)
                        continue

                    group.problems.append(RowProblem(
                        row_number,
                        "Copy code \"%s\" is already in use in the library."
                        % code,
                    ))
                    bad_code = True

                elif folded in claimed:
                    group.problems.append(RowProblem(
                        row_number,
                        "Copy code \"%s\" is used twice in this file (also "
                        "on row %d)." % (code, claimed[folded]),
                    ))
                    bad_code = True

                else:
                    claimed[folded] = row_number
                    new_codes.append(code)

            if bad_code:
                continue

            # Only the codes that are new make copies. All of them being
            # already ours is the ordinary round-trip case, and means this
            # row asks for nothing to be created.
            if codes:
                copies = len(new_codes)
                codes = new_codes

            if already_ours and not new_codes:
                group.existing_copies += len(already_ours)

            # Nothing to record: no volume named and nothing to place.
            if not named_volume and not copies:
                continue

            group.volumes.append({
                "row": row_number,
                "number": number,
                "title": volume_title,
                "copies": copies,
                "shelf_id": shelf_id,
                "codes": codes or None,
            })

        if len({v["number"] for v in group.volumes}) > limits["volumes"]:
            group.problems.append(RowProblem(
                group.first_row,
                "At most %d volumes per book." % limits["volumes"],
            ))

        if group.copy_total > limits["total_copies"]:
            group.problems.append(RowProblem(
                group.first_row,
                "At most %d copies per book in one import."
                % limits["total_copies"],
            ))

    return groups


def summarise(groups, existing_volumes=None):
    """What the import would do, for the confirmation step.

    `existing_volumes` maps a book id to the volume numbers it already
    has, so an update is not counted as creating a volume that is already
    there. Without it every volume on an update row would be reported as
    new — which is what once made this promise 0 and deliver 203.
    """

    existing_volumes = existing_volumes or {}

    ready = [g for g in groups if not g.blocked]
    blocked = [g for g in groups if g.blocked]

    new_volumes = 0

    for group in ready:

        have = (
            existing_volumes.get(group.existing_book.id, set())
            if group.existing_book is not None
            else set()
        )

        new_volumes += len(
            {v["number"] for v in group.volumes} - have
        )

    counts = {
        "groups": len(groups),
        "new_books": sum(1 for g in ready if g.action == "create"),
        "updated_books": sum(1 for g in ready if g.action == "update"),
        "new_volumes": new_volumes,
        "new_copies": sum(g.copy_total for g in ready),
        "skipped": len(blocked),
        "warnings": sum(
            1 for g in groups for p in g.problems if not p.blocking
        ),
    }

    counts.update({
        "ready": ready,
        "blocked": blocked,
        # Shaped here rather than in the template, which has no business
        # deciding which numbers matter or how to colour them.
        "tiles": (
            ("New books", counts["new_books"], "success"),
            ("Updated", counts["updated_books"], "info"),
            ("New volumes", counts["new_volumes"], "body"),
            ("New copies", counts["new_copies"], "body"),
            ("Warnings", counts["warnings"], "warning"),
            ("Skipped", counts["skipped"], "danger"),
        ),
    })

    return counts


# ------------------------------------------------------------- workbooks

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
SYSTEM_FILL = PatternFill("solid", fgColor="7F7F7F")
NOTE_FILL = PatternFill("solid", fgColor="FFF2CC")


def _write_headings(sheet, columns, row=1):
    for index, column in enumerate(columns, start=1):
        cell = sheet.cell(row=row, column=index, value=column.label)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = SYSTEM_FILL if column.system else HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)

        sheet.column_dimensions[get_column_letter(index)].width = max(
            14, min(38, len(column.label) + 4)
        )

    sheet.freeze_panes = sheet.cell(row=row + 1, column=1)


def build_template():
    """The blank template, with its instructions and two example rows."""

    book = Workbook()

    sheet = book.active
    sheet.title = "Books"

    _write_headings(sheet, COLUMNS)

    # Two examples rather than one, because the difference between them is
    # the thing worth showing: a plain book, then a multi-volume book with
    # one row per volume.
    examples = [
        {
            "title": "Riyad as-Salihin",
            "author": "Imam Nawawi",
            "category": "Hadith",
            "publisher": "Dar Ibn Kathir",
            "copies": "3",
            "location": "Main Hall",
            "shelf": "A-1",
        },
        {
            "title": "Sahih al-Bukhari",
            "author": "Imam Bukhari",
            "category": "Hadith",
            "volume_number": "1",
            "volume_title": "Kitab al-Iman",
            "copies": "2",
            "location": "Main Hall",
            "shelf": "A-1",
        },
        {
            "title": "Sahih al-Bukhari",
            "author": "Imam Bukhari",
            "category": "Hadith",
            "volume_number": "2",
            "volume_title": "Kitab al-Salah",
            "copies": "1",
            "location": "Main Hall",
            "shelf": "A-2",
        },
    ]

    for offset, example in enumerate(examples):
        for index, column in enumerate(COLUMNS, start=1):
            cell = sheet.cell(
                row=offset + 2,
                column=index,
                value=example.get(column.key, ""),
            )
            cell.fill = NOTE_FILL

    # The wordier part goes on its own sheet. Deliberately not a note in
    # column A of the Books sheet: the importer reads every non-empty row
    # there, so a sentence sitting under the examples came back as a row
    # with an unreadable Book ID.
    guide = book.create_sheet("Instructions")
    guide.column_dimensions["A"].width = 34
    guide.column_dimensions["B"].width = 16
    guide.column_dimensions["C"].width = 82

    intro = guide.cell(row=1, column=1, value=(
        "The three shaded rows on the Books sheet are examples — delete "
        "them before importing. The last two of them show one multi-volume "
        "book: the same title and author on every row, one row per volume."
    ))
    intro.font = Font(italic=True)
    intro.alignment = Alignment(wrap_text=True, vertical="top")
    guide.merge_cells(start_row=1, start_column=1, end_row=1, end_column=3)

    for index, heading in enumerate(("Column", "Required?", "What it means"),
                                    start=1):
        cell = guide.cell(row=3, column=index, value=heading)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL

    for offset, column in enumerate(COLUMNS, start=4):
        guide.cell(row=offset, column=1, value=column.label)
        guide.cell(
            row=offset, column=2,
            value="Required" if column.required else (
                "System" if column.system else "Optional"
            ),
        )
        cell = guide.cell(row=offset, column=3, value=column.help_text)
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    guide.freeze_panes = "A4"

    return book


def build_export(rows):
    """An export of existing books, in the shape the template expects.

    The same columns, so what comes out can be edited and put straight
    back in. Book ID travels with it, which is what makes an update land
    on the right record instead of adding a second one.
    """

    book = Workbook()

    sheet = book.active
    sheet.title = "Books"

    _write_headings(sheet, COLUMNS)

    for offset, row in enumerate(rows, start=2):
        for index, column in enumerate(COLUMNS, start=1):
            sheet.cell(
                row=offset,
                column=index,
                value=row.get(column.key, ""),
            )

    return book


def build_error_report(failures):
    """A workbook of what could not be imported, and why.

    Carries the original row number and the values as they were read, so
    the file can be corrected against it without guesswork.
    """

    book = Workbook()

    sheet = book.active
    sheet.title = "Errors"

    columns = ["Spreadsheet Row", "Book", "Problem"] + [
        c.label for c in COLUMNS if not c.system
    ]

    for index, heading in enumerate(columns, start=1):
        cell = sheet.cell(row=index and 1, column=index, value=heading)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        sheet.column_dimensions[get_column_letter(index)].width = max(
            14, min(52, len(heading) + 4)
        )

    sheet.freeze_panes = "A2"

    line = 2

    for failure in failures:
        for problem in failure["problems"]:

            sheet.cell(row=line, column=1, value=problem["row"])
            sheet.cell(row=line, column=2, value=failure["label"])
            sheet.cell(row=line, column=3, value=problem["message"])

            values = failure["values"].get(problem["row"], {})

            for index, column in enumerate(
                [c for c in COLUMNS if not c.system], start=4
            ):
                sheet.cell(row=line, column=index,
                           value=values.get(column.key, ""))

            line += 1

    return book
