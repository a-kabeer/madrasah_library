"""Add the acquisition_suggestions table.

One table, eleven columns: a title somebody wrote down, whatever else they
knew about it, and what came of it.

It is not a Book and it never becomes one by itself. `author_name` and
`publisher_name` are free text with no foreign key, on purpose - resolving
"ibn kathir" to an `authors` row on the strength of a suggestion would put
a record in the catalogue that nobody checked. Nothing in this migration
touches `books`, `authors`, `publishers`, `categories` or `book_copies`,
and nothing in the application creates any of them from a suggestion: the
catalogue is still entered through `book_add`, with its own validation and
its own duplicate check.

It is not a purchase order either. No supplier, no quotation, no price, no
invoice, no receiving. Those are an accounting system.

`check_acquisition_status` pins the four states, the same way
`check_reservation_status` and `check_inventory_status` pin theirs, and in
the same Title case every other status column in this schema uses. Which
state may follow which is not in the database - it is a conditional UPDATE
in library/acquisitions.py, so a repeated POST matches nothing rather than
reopening a decision.

`suggested_by` and `reviewed_by` are nullable, matching `loans.issued_by`,
`inventory_sessions.started_by` and `activity_logs.user_id` rather than
`notifications.recipient_id`. An account can be removed, and a suggestion
outliving the person who made it is better than a delete that fails with
an unhandled foreign-key violation.

One index. Every list this table serves is "the suggestions in this state,
newest first", so `(status, created_at DESC, id DESC)` is that query and
the id breaks ties so a page cannot reshuffle between loads. No index on
`suggested_by` or `reviewed_by`: nothing looks a suggestion up by either -
they are only ever joined outwards to `users` by primary key - and an
index nothing reads is a write nobody needed.

The second operation widens `check_notification_event_type` to admit
`suggestion_submitted`. That is the designed way to add a notification
type: the constraint enumerates the kinds the application can render, and
a new kind has to be named in it. Widening a CHECK cannot invalidate an
existing row, and the constraint is dropped and recreated under its own
name so re-running finds the same shape.

Every model in this app is `managed = False`, so Django's schema editor
creates nothing - the table is made with explicit SQL and paired with the
matching state operation via SeparateDatabaseAndState, the same shape as
0002 through 0009. `IF NOT EXISTS` keeps it safe against a database that
already has it, notably the test database, which is restored from
scripts/test_schema.sql rather than built by running migrations.
"""

from django.db import migrations, models
import django.db.models.deletion


TABLE = """
CREATE TABLE IF NOT EXISTS public.acquisition_suggestions (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title character varying(500) NOT NULL,
    author_name character varying(255) DEFAULT ''::character varying NOT NULL,
    publisher_name character varying(255) DEFAULT ''::character varying NOT NULL,
    isbn character varying(32) DEFAULT ''::character varying NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    status character varying(20) DEFAULT 'Pending'::character varying NOT NULL,
    suggested_by integer REFERENCES public.users(id),
    reviewed_by integer REFERENCES public.users(id),
    reviewed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,

    CONSTRAINT check_acquisition_status CHECK (
        status IN ('Pending', 'Approved', 'Rejected', 'Acquired')
    ),

    CONSTRAINT check_acquisition_reviewed CHECK (
        (status = 'Pending' AND reviewed_at IS NULL)
        OR (status <> 'Pending' AND reviewed_at IS NOT NULL)
    )
);
"""

# Every list is "the suggestions in this state, newest first".
BY_STATUS = """
CREATE INDEX IF NOT EXISTS idx_acquisition_suggestions_queue
    ON public.acquisition_suggestions (status, created_at DESC, id DESC);
"""

# Task 17's notifications table enumerates the kinds it can render, so a
# new kind has to be named in the constraint. Widening it cannot invalidate
# an existing row.
WIDEN_NOTIFICATION_TYPES = """
ALTER TABLE public.notifications
    DROP CONSTRAINT IF EXISTS check_notification_event_type;

ALTER TABLE public.notifications
    ADD CONSTRAINT check_notification_event_type CHECK (
        event_type IN (
            'reservation_ready',
            'stock_check_missing',
            'suggestion_submitted'
        )
    );
"""

NARROW_NOTIFICATION_TYPES = """
ALTER TABLE public.notifications
    DROP CONSTRAINT IF EXISTS check_notification_event_type;

ALTER TABLE public.notifications
    ADD CONSTRAINT check_notification_event_type CHECK (
        event_type IN ('reservation_ready', 'stock_check_missing')
    );
"""


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0009_institution_metadata"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=TABLE,
                    reverse_sql=(
                        "DROP TABLE IF EXISTS public.acquisition_suggestions;"
                    ),
                ),
                migrations.RunSQL(
                    sql=BY_STATUS,
                    reverse_sql=(
                        "DROP INDEX IF EXISTS "
                        "idx_acquisition_suggestions_queue;"
                    ),
                ),
                migrations.RunSQL(
                    sql=WIDEN_NOTIFICATION_TYPES,
                    reverse_sql=NARROW_NOTIFICATION_TYPES,
                ),
            ],
            state_operations=[
                migrations.CreateModel(
                    name="AcquisitionSuggestion",
                    fields=[
                        ("id", models.AutoField(primary_key=True, serialize=False)),
                        ("title", models.CharField(max_length=500)),
                        (
                            "author_name",
                            models.CharField(blank=True, default="", max_length=255),
                        ),
                        (
                            "publisher_name",
                            models.CharField(blank=True, default="", max_length=255),
                        ),
                        (
                            "isbn",
                            models.CharField(blank=True, default="", max_length=32),
                        ),
                        ("notes", models.TextField(blank=True, default="")),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("Pending", "Pending"),
                                    ("Approved", "Approved"),
                                    ("Rejected", "Rejected"),
                                    ("Acquired", "Acquired"),
                                ],
                                default="Pending",
                                max_length=20,
                            ),
                        ),
                        ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                        ("created_at", models.DateTimeField()),
                        ("updated_at", models.DateTimeField()),
                        (
                            "reviewed_by",
                            models.ForeignKey(
                                blank=True,
                                db_column="reviewed_by",
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="acquisition_reviews",
                                to="library.user",
                            ),
                        ),
                        (
                            "suggested_by",
                            models.ForeignKey(
                                blank=True,
                                db_column="suggested_by",
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="acquisition_suggestions",
                                to="library.user",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "acquisition_suggestions",
                        "managed": False,
                    },
                ),
            ],
        ),
    ]
