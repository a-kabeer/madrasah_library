"""The search indexes have to match the SQL the ORM actually sends.

Django's PostgreSQL backend compiles a case-insensitive lookup with the
*column* wrapped in a function:

    name__icontains  ->  UPPER("borrowers"."name"::text) LIKE UPPER(%s)
    name__iexact     ->  UPPER("borrowers"."name"::text) = UPPER(%s)

So an index on `name` is unusable for either, and the schema shipped four
trigram indexes in exactly that unusable shape. Measured on 40,000
borrowers, a one-column search took 13.7 ms with the index present and
0.6 ms once the index was on `UPPER(name)`.

These tests hold the shape rather than the speed. A timing assertion would
be flaky - PostgreSQL picks a sequential scan on a small table however good
the index is, and the test database is small - but "there is an index on
UPPER(this column)" is exact and cannot pass by luck.
"""

import re

from django.db import connection
from django.test import TestCase, override_settings

from library.tests.helpers import make_user, make_borrower

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Every column the app searches case-insensitively *without* crossing a join
# in the same OR - the only ones an index can serve at all. The join-OR
# searches (book list, copy list, loan list) cannot use an index whatever
# is defined, which is recorded in 0013's docstring rather than pretended
# about here.
EXPRESSION_INDEXED = (
    ("authors", "name"),
    ("books", "title"),
    ("book_contents", "title"),
    ("borrowers", "name"),
    ("borrowers", "phone"),
    ("borrowers", "registration_no"),
    ("borrowers", "department"),
    ("book_copies", "copy_code"),
    ("book_copies", "status"),
)

REPLACED = (
    "idx_authors_name_trgm",
    "idx_books_title_trgm",
    "idx_borrowers_name_trgm",
    "idx_book_contents_title_trgm",
)


def index_defs():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s",
            ["public"],
        )
        return dict(cursor.fetchall())


class SearchIndexShape(TestCase):

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("expression indexes are checked against PostgreSQL")

        self.defs = index_defs()

    def test_every_searchable_column_is_indexed_on_upper(self):
        for table, column in EXPRESSION_INDEXED:
            with self.subTest(table=table, column=column):
                # pg renders the expression with the cast Django's SQL
                # also carries: `btree (upper((copy_code)::text))`.
                pattern = re.compile(
                    r"upper\(+%s\b" % re.escape(column), re.IGNORECASE
                )
                found = [
                    name
                    for name, definition in self.defs.items()
                    if "ON public.%s USING" % table in definition
                    and pattern.search(definition)
                ]

                self.assertTrue(
                    found,
                    "no index on UPPER(%s.%s); a case-insensitive lookup on "
                    "it cannot use an index" % (table, column),
                )

    def test_the_unusable_raw_column_trigrams_are_gone(self):
        for name in REPLACED:
            with self.subTest(index=name):
                self.assertNotIn(name, self.defs)

    def test_the_planner_accepts_the_expression_for_an_orm_lookup(self):
        """The generated SQL casts the column (`name::text`). That cast is
        binary-coercible and the planner strips it, so `UPPER(name)` matches
        - this asserts that rather than assuming it."""

        from library.models import Borrower

        sql, params = (
            Borrower.objects.filter(name__icontains="x").values("id").query
            .sql_with_params()
        )

        self.assertIn("UPPER", sql)

        with connection.cursor() as cursor:
            cursor.execute("SET enable_seqscan = off")
            try:
                cursor.execute("EXPLAIN " + sql, params)
                plan = "\n".join(row[0] for row in cursor.fetchall())
            finally:
                cursor.execute("SET enable_seqscan = on")

        self.assertIn("idx_borrowers_name_upper_trgm", plan)


@override_settings(CACHES=LOCMEM)
class SearchStillFindsTheSameRows(TestCase):
    """The indexes changed; the answers must not have."""

    def setUp(self):
        self.admin = make_user(username="idx_admin", password="pass12345",
                               role="Admin")
        self.client.force_login(self.admin)

        self.borrower = make_borrower(name="Abdul Rahman")
        self.borrower.phone = "03001234567"
        self.borrower.registration_no = "REG-2026-0042"
        self.borrower.department = "Darja Thaniya"
        self.borrower.save()

    def search(self, term):
        from django.urls import reverse

        return self.client.get(reverse("borrower_list"), {"search": term})

    def test_a_borrower_is_found_by_each_of_the_four_searched_fields(self):
        for term in ("rahman", "1234567", "2026-0042", "thaniya"):
            with self.subTest(term=term):
                self.assertContains(self.search(term), "Abdul Rahman")

    def test_the_search_is_still_case_insensitive(self):
        for term in ("ABDUL", "abdul", "AbDuL"):
            with self.subTest(term=term):
                self.assertContains(self.search(term), "Abdul Rahman")

    def test_a_term_that_matches_nothing_returns_nothing(self):
        self.assertNotContains(self.search("zzzz-no-such-borrower"),
                               "Abdul Rahman")
