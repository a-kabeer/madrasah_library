"""`scripts/test_schema.sql` is the source of truth, so it has to be true.

Every model in this project is `managed = False`: Django's schema editor
never touches these tables, and that file is what creates them. Which means
two things can go wrong quietly, and both had:

  * **The file does not load.** It is a `pg_dump` 18 dump being loaded into
    PostgreSQL 16, and it carried `SET transaction_timeout = 0` - a
    parameter that did not exist before 17. With `ON_ERROR_STOP`, which is
    what anyone loading a schema should use, the whole file aborted on line
    13. Without it, seven `CREATE INDEX … gin_trgm_ops` statements failed
    because nothing had created the `pg_trgm` extension - pg_dump does not
    write it when the extension lives outside the dumped schema. A fresh
    database got 20 tables, 62 of the 69 indexes, and seven silent
    failures, every one of them a search index. Nothing said why the
    searches were slow.

  * **The file and the database drift apart.** A migration adds an index
    with `RunSQL`; the file is not updated; the next fresh install is
    missing it, including every test database built from the file. Nothing
    fails, because the database this suite runs against already has it.

So these tests compare the file against the database the suite is connected
to, in both directions, and hold the two things that made it unloadable.
"""

import pathlib
import re

from django.db import connection
from django.test import TestCase

SCHEMA = pathlib.Path("scripts/test_schema.sql")

# `CREATE [UNIQUE] INDEX [IF NOT EXISTS] name ON …`
INDEX = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:public\.)?(\w+)",
    re.IGNORECASE,
)

TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?(\w+)",
    re.IGNORECASE,
)

# Indexes PostgreSQL creates for a constraint rather than a CREATE INDEX
# statement, so the file names them as ADD CONSTRAINT instead.
CONSTRAINT_INDEX = re.compile(
    r"ADD\s+CONSTRAINT\s+(\w+)\s+(?:PRIMARY\s+KEY|UNIQUE)", re.IGNORECASE
)

# Tables Django owns. They are created by `migrate`, not by this file, and
# the file rightly says nothing about them.
DJANGO_OWNED = {
    "django_migrations", "django_session", "django_content_type",
    "django_cache", "auth_permission", "auth_group",
    "auth_group_permissions", "django_admin_log",
}


class TheSchemaFileLoads(TestCase):

    def setUp(self):
        self.body = SCHEMA.read_text()

    def test_it_creates_the_extension_its_indexes_need(self):
        self.assertRegex(
            self.body,
            r"CREATE EXTENSION IF NOT EXISTS pg_trgm",
            "seven indexes in this file use gin_trgm_ops; without the "
            "extension they fail and the file carries on without them",
        )

    def test_the_extension_comes_before_the_indexes_that_need_it(self):
        extension = self.body.index("CREATE EXTENSION IF NOT EXISTS pg_trgm")

        # The first *statement* that needs it, not the first mention: the
        # comment above the extension explains what it is for and names the
        # operator class while doing so.
        first = re.search(
            r"(?m)^CREATE\s+INDEX[^;]*gin_trgm_ops", self.body)

        self.assertIsNotNone(first, "no trigram index in the file")
        self.assertLess(extension, first.start())

    def test_it_sets_no_parameter_this_server_does_not_know(self):
        """`SET transaction_timeout` is PostgreSQL 17+. A dump from a newer
        server can carry settings the target does not have, and each one
        aborts the load under ON_ERROR_STOP."""

        parameters = set(re.findall(r"(?m)^SET\s+(\w+)\s*=", self.body))
        unknown = []

        with connection.cursor() as cursor:
            for parameter in sorted(parameters):
                cursor.execute(
                    "SELECT count(*) FROM pg_settings WHERE name = %s",
                    [parameter],
                )

                if not cursor.fetchone()[0]:
                    unknown.append(parameter)

        self.assertEqual(
            unknown, [],
            "this server does not recognise: %s - loading the file with "
            "ON_ERROR_STOP will abort on the first one" % unknown,
        )


class TheFileAndTheDatabaseAgree(TestCase):
    """Both directions, because either one drifting is a broken install."""

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("the schema file is PostgreSQL")

        body = SCHEMA.read_text()

        self.file_tables = set(TABLE.findall(body))
        self.file_indexes = (
            set(INDEX.findall(body)) | set(CONSTRAINT_INDEX.findall(body))
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            self.db_tables = {row[0] for row in cursor.fetchall()}

            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            self.db_indexes = {row[0] for row in cursor.fetchall()}

    def test_every_table_in_the_database_is_in_the_file(self):
        missing = sorted(self.db_tables - self.file_tables - DJANGO_OWNED)

        self.assertEqual(
            missing, [],
            "these tables exist but the schema file does not create them, so "
            "a fresh install will not have them: %s" % missing,
        )

    def test_every_table_in_the_file_exists(self):
        missing = sorted(self.file_tables - self.db_tables)

        self.assertEqual(
            missing, [],
            "the file creates these and this database has not got them: %s"
            % missing,
        )

    def test_every_index_in_the_database_is_in_the_file(self):
        django_indexes = {
            name for name in self.db_indexes
            if any(name.startswith(table) for table in DJANGO_OWNED)
        }

        # PostgreSQL names the index behind an inline `… PRIMARY KEY`
        # after the table, and the file writes those inside CREATE TABLE
        # rather than as an ADD CONSTRAINT - so there is no name to
        # match on. A `<table>_pkey` for a table the file creates is
        # accounted for.
        implied = {
            "%s_pkey" % table for table in self.file_tables
        } & self.db_indexes

        missing = sorted(
            self.db_indexes - self.file_indexes - django_indexes - implied)

        self.assertEqual(
            missing, [],
            "these indexes exist but the schema file does not create them - "
            "usually a migration that used RunSQL without updating the "
            "file, which leaves every fresh install and every new test "
            "database without them: %s" % missing,
        )

    def test_every_index_in_the_file_exists(self):
        missing = sorted(self.file_indexes - self.db_indexes)


        self.assertEqual(
            missing, [],
            "the file creates these and this database has not got them, so "
            "either a migration dropped one without updating the file or the "
            "file failed part way through loading: %s" % missing,
        )
