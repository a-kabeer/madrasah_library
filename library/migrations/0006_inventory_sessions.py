"""Add the two tables a stock check needs, and no more.

Counting the shelves is a job with a beginning and an end, so it is a
record: `inventory_sessions` says what was being counted and when, and
`inventory_scans` says what was picked up. Everything else a report shows -
expected, found, remaining, missing - is derived from `book_copies` and
from these scans, so nothing here can drift from the shelves it describes.

No copy identifier is introduced. A scan resolves to a `book_copies` row by
the `copy_code` the issue and return workflows already scan, and no status
on a copy is written by any of this: a stock check reports, and marking a
copy Missing stays the existing copy-management action.

Every model in this app is `managed = False`, so Django's schema editor
creates nothing - the autodetector would put two models in project state
and leave PostgreSQL empty. The tables are therefore created with explicit
SQL, paired with the matching state operations via SeparateDatabaseAndState
so `makemigrations` stays quiet afterwards - the same shape as
0002_book_cover_image, 0004_book_archived_at and 0005_borrowing_policy.

`IF NOT EXISTS` throughout, so this is safe against a database that already
has them - notably the test database, which is a schema clone restored from
scripts/test_schema.sql rather than built by running migrations, and which
is updated to match.

Four constraints are worth naming, because each is a rule that would
otherwise live only in Python:

  * `check_inventory_scope` - a location-scoped session has a location and
    no shelf, a shelf-scoped one has a shelf, a library-wide one has
    neither. A session that covers nothing coherent cannot be stored.
  * `check_inventory_status` - a session is Completed exactly when it has a
    completion time. The two cannot come apart.
  * `check_inventory_scan_outcome` - the four things a read can turn out to
    be, and nothing else.
  * `unique_found_copy_per_session` - a partial unique index over the found
    rows. This is what makes a repeated scan unable to raise the Found
    count, including when two people scan the same shelf at the same
    moment: the second insert is refused by the database, not by a check
    that could be raced.
"""

from django.db import migrations, models
import django.db.models.deletion


SESSIONS = """
CREATE TABLE IF NOT EXISTS public.inventory_sessions (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name character varying(255) NOT NULL,
    scope character varying(20) NOT NULL,
    location_id integer REFERENCES public.locations(id),
    shelf_id integer REFERENCES public.shelves(id),
    status character varying(20) DEFAULT 'In Progress'::character varying NOT NULL,
    started_by integer REFERENCES public.users(id),
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,

    CONSTRAINT check_inventory_scope CHECK (
        (scope = 'library' AND location_id IS NULL AND shelf_id IS NULL)
        OR (scope = 'location' AND location_id IS NOT NULL AND shelf_id IS NULL)
        OR (scope = 'shelf' AND shelf_id IS NOT NULL AND location_id IS NULL)
    ),

    CONSTRAINT check_inventory_status CHECK (
        (status = 'In Progress' AND completed_at IS NULL)
        OR (status = 'Completed' AND completed_at IS NOT NULL)
    )
);
"""

SCANS = """
CREATE TABLE IF NOT EXISTS public.inventory_scans (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id integer NOT NULL
        REFERENCES public.inventory_sessions(id) ON DELETE CASCADE,
    copy_id integer REFERENCES public.book_copies(id),
    copy_code character varying(50) NOT NULL,
    outcome character varying(20) NOT NULL,
    scanned_by integer REFERENCES public.users(id),
    scanned_at timestamp with time zone NOT NULL,

    CONSTRAINT check_inventory_scan_outcome CHECK (
        outcome IN ('found', 'duplicate', 'outside', 'unknown')
    ),

    -- A read that resolved to a copy has to say which; one that did not
    -- can only be an unknown code.
    CONSTRAINT check_inventory_scan_copy CHECK (
        (copy_id IS NOT NULL) OR (outcome = 'unknown')
    )
);
"""

# The rule that makes a repeated scan harmless, held by the database rather
# than by a check that two concurrent requests could both pass.
FOUND_ONCE = """
CREATE UNIQUE INDEX IF NOT EXISTS unique_found_copy_per_session
    ON public.inventory_scans (session_id, copy_id)
    WHERE outcome = 'found';
"""

# Every report reads the scans of one session.
BY_SESSION = """
CREATE INDEX IF NOT EXISTS idx_inventory_scans_session
    ON public.inventory_scans (session_id);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0005_borrowing_policy"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=SESSIONS,
                    reverse_sql="DROP TABLE IF EXISTS public.inventory_sessions;",
                ),
                migrations.RunSQL(
                    sql=SCANS,
                    reverse_sql="DROP TABLE IF EXISTS public.inventory_scans;",
                ),
                migrations.RunSQL(
                    sql=FOUND_ONCE,
                    reverse_sql="DROP INDEX IF EXISTS unique_found_copy_per_session;",
                ),
                migrations.RunSQL(
                    sql=BY_SESSION,
                    reverse_sql="DROP INDEX IF EXISTS idx_inventory_scans_session;",
                ),
            ],
            state_operations=[
                migrations.CreateModel(
                    name="InventorySession",
                    fields=[
                        ("id", models.AutoField(primary_key=True, serialize=False)),
                        ("name", models.CharField(max_length=255)),
                        (
                            "scope",
                            models.CharField(
                                choices=[
                                    ("library", "Entire library"),
                                    ("location", "One location"),
                                    ("shelf", "One shelf"),
                                ],
                                max_length=20,
                            ),
                        ),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("In Progress", "In Progress"),
                                    ("Completed", "Completed"),
                                ],
                                default="In Progress",
                                max_length=20,
                            ),
                        ),
                        ("started_at", models.DateTimeField()),
                        ("completed_at", models.DateTimeField(blank=True, null=True)),
                        (
                            "location",
                            models.ForeignKey(
                                blank=True,
                                db_column="location_id",
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                to="library.location",
                            ),
                        ),
                        (
                            "shelf",
                            models.ForeignKey(
                                blank=True,
                                db_column="shelf_id",
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                to="library.shelf",
                            ),
                        ),
                        (
                            "started_by",
                            models.ForeignKey(
                                blank=True,
                                db_column="started_by",
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="inventory_sessions",
                                to="library.user",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "inventory_sessions",
                        "managed": False,
                    },
                ),
                migrations.CreateModel(
                    name="InventoryScan",
                    fields=[
                        ("id", models.AutoField(primary_key=True, serialize=False)),
                        ("copy_code", models.CharField(max_length=50)),
                        (
                            "outcome",
                            models.CharField(
                                choices=[
                                    ("found", "Found"),
                                    ("duplicate", "Already scanned"),
                                    ("outside", "Outside this session"),
                                    ("unknown", "Unknown code"),
                                ],
                                max_length=20,
                            ),
                        ),
                        ("scanned_at", models.DateTimeField()),
                        (
                            "copy",
                            models.ForeignKey(
                                blank=True,
                                db_column="copy_id",
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="inventory_scans",
                                to="library.bookcopy",
                            ),
                        ),
                        (
                            "scanned_by",
                            models.ForeignKey(
                                blank=True,
                                db_column="scanned_by",
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="inventory_scans",
                                to="library.user",
                            ),
                        ),
                        (
                            "session",
                            models.ForeignKey(
                                db_column="session_id",
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="scans",
                                to="library.inventorysession",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "inventory_scans",
                        "managed": False,
                    },
                ),
            ],
        ),
    ]
