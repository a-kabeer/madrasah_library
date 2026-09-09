"""Make the search indexes match the queries the ORM actually sends.

The schema shipped four trigram indexes - `idx_authors_name_trgm`,
`idx_books_title_trgm`, `idx_borrowers_name_trgm` and
`idx_book_contents_title_trgm` - on the raw columns. Not one of them could
ever be used, because Django's PostgreSQL backend compiles a case-
insensitive lookup with the *column* wrapped in a function:

    name__icontains  ->  UPPER("borrowers"."name"::text) LIKE UPPER(%s)
    name__iexact     ->  UPPER("borrowers"."name"::text) = UPPER(%s)

An index on `name` cannot serve a predicate on `UPPER(name)`, so every one
of those searches sequentially scanned the table while the index sat there
collecting writes. Measured on 40,000 borrowers: 13.7 ms for a one-column
search with the index present, 0.6 ms once the index was on `UPPER(name)`.
The `::text` cast is binary-coercible and the planner strips it, so
indexing `UPPER(col)` is enough - verified, not assumed.

So the four are rebuilt as expression indexes, and three more columns are
added because of how PostgreSQL treats OR. The borrower search reads

    Q(name) | Q(phone) | Q(registration_no) | Q(department)

and a bitmap OR needs an index for *every* branch: with only `UPPER(name)`
indexed the whole query still scanned (33.9 ms); with all four it became a
bitmap heap scan (0.45 ms).

Two btree expression indexes go on the paths that are hit most often
rather than searched:

  * `book_copies (UPPER(copy_code))` - `copy_code__iexact` is the barcode
    scan behind issue, return, the return lookup and the copy list, and it
    is also what `_next_copy_code` calls `.exists()` on in a loop while it
    looks for a free code. 9.1 ms -> 0.036 ms on 40,000 copies, per call.
  * `authors (UPPER(name))` - `name__iexact` is the per-row dedupe the book
    import runs for every line of the workbook. 5.0 ms -> 0.021 ms on
    20,000 authors, times the number of rows imported.
  * `book_copies (UPPER(status))` - the copy list's status filter, and the
    Lost/Damaged/Missing report queries. 8.3 ms -> 0.023 ms.

What this does NOT fix, and no index can: the searches that OR across a
join, which is most of the list pages -

    Q(copy_code__icontains=s) | Q(volume__book__title__icontains=s)

PostgreSQL cannot turn an OR spanning two tables into a bitmap OR, so it
joins and filters. Measured with every column above indexed: book list
14.5 ms, copy list 44.2 ms, loan list 52.7 ms - identical to unindexed.
Making those fast needs a different query shape (each side searched
separately and combined by id), which is a change to the views, not to the
schema, and is recorded as such rather than smuggled in here.

`book_contents.title` gets its expression index for symmetry, and honestly:
the only content search ORs across a join too, so it is not live yet.

Every model here is `managed = False` and no `Meta.indexes` is declared, so
this migration is pure `RunSQL` with no state operations - there is no
Django state to keep in step. `IF EXISTS` / `IF NOT EXISTS` throughout so
it is safe against a database that already has them, notably the test
database, which is a schema clone reused with --keepdb rather than built by
running migrations. `scripts/test_schema.sql` is updated to match.
"""

from django.db import migrations


TRIGRAM = (
    # (index name, table, column, the raw-column index it replaces)
    ("idx_authors_name_upper_trgm", "authors", "name", "idx_authors_name_trgm"),
    ("idx_books_title_upper_trgm", "books", "title", "idx_books_title_trgm"),
    ("idx_borrowers_name_upper_trgm", "borrowers", "name",
     "idx_borrowers_name_trgm"),
    ("idx_book_contents_title_upper_trgm", "book_contents", "title",
     "idx_book_contents_title_trgm"),
    ("idx_borrowers_phone_upper_trgm", "borrowers", "phone", None),
    ("idx_borrowers_registration_no_upper_trgm", "borrowers",
     "registration_no", None),
    ("idx_borrowers_department_upper_trgm", "borrowers", "department", None),
)

BTREE = (
    ("idx_book_copies_copy_code_upper", "book_copies", "copy_code"),
    ("idx_book_copies_status_upper", "book_copies", "status"),
    ("idx_authors_name_upper", "authors", "name"),
)


def _forward():
    lines = []

    for name, table, column, replaces in TRIGRAM:
        lines.append(
            "CREATE INDEX IF NOT EXISTS %s ON public.%s "
            "USING gin (UPPER(%s) public.gin_trgm_ops);" % (name, table, column)
        )
        if replaces:
            lines.append("DROP INDEX IF EXISTS public.%s;" % replaces)

    for name, table, column in BTREE:
        lines.append(
            "CREATE INDEX IF NOT EXISTS %s ON public.%s "
            "USING btree (UPPER(%s));" % (name, table, column)
        )

    return "\n".join(lines)


def _reverse():
    lines = []

    for name, table, column, replaces in TRIGRAM:
        if replaces:
            lines.append(
                "CREATE INDEX IF NOT EXISTS %s ON public.%s "
                "USING gin (%s public.gin_trgm_ops);" % (replaces, table, column)
            )
        lines.append("DROP INDEX IF EXISTS public.%s;" % name)

    for name, _table, _column in BTREE:
        lines.append("DROP INDEX IF EXISTS public.%s;" % name)

    return "\n".join(lines)


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0012_language_preference"),
    ]

    operations = [
        migrations.RunSQL(sql=_forward(), reverse_sql=_reverse()),
    ]
