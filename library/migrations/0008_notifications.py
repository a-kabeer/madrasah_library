"""Add the notifications table.

One table, eight columns: who it is for, what kind of thing happened, what
it says, where to go, when it arrived and when it was read.

Everything a notification displays is written into it. It does not join to
the reservation or the stock check that caused it, and it is not meant to:
a notification is a record of what was said at the time, so it has to
survive the reservation being fulfilled, the book being archived and the
session being deleted. Reading a page of them is one query with no joins.

Two things are held by the database rather than by anything in Python:

  * `unique_notification_event` - a unique index over
    (recipient_id, event_key). `event_key` names the one-time transition
    the notification is about, so a refresh, a retried POST or two
    simultaneous returns of the same book all leave exactly one row. A
    check in Python would be a read followed by a write, and two requests
    could both read "not sent" and both write.

  * `check_notification_event_type` - the same list of types the model
    carries, so a row nothing can render cannot be written.

Three indexes, one per question the application actually asks: the unread
count for the shell, the newest few for the panel, and a page of the list.
`(recipient_id, created_at DESC, id DESC)` is exactly the ordering every
read uses - the id breaks ties so a page cannot reshuffle between loads.

Every model in this app is `managed = False`, so Django's schema editor
creates nothing - the table is made with explicit SQL and paired with the
matching state operation via SeparateDatabaseAndState, the same shape as
0002 through 0007. `IF NOT EXISTS` keeps it safe against a database that
already has it, notably the test database, which is restored from
scripts/test_schema.sql rather than built by running migrations.
"""

from django.db import migrations, models
import django.db.models.deletion


TABLE = """
CREATE TABLE IF NOT EXISTS public.notifications (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recipient_id integer NOT NULL REFERENCES public.users(id),
    event_type character varying(50) NOT NULL,
    event_key character varying(200) NOT NULL,
    title character varying(255) NOT NULL,
    message text DEFAULT ''::text NOT NULL,
    url character varying(500) DEFAULT ''::character varying NOT NULL,
    created_at timestamp with time zone NOT NULL,
    read_at timestamp with time zone,

    CONSTRAINT check_notification_event_type CHECK (
        event_type IN ('reservation_ready', 'stock_check_missing')
    )
);
"""

# One notification per recipient per transition, held by the database
# rather than by a check two concurrent requests could both pass.
ONE_PER_EVENT = """
CREATE UNIQUE INDEX IF NOT EXISTS unique_notification_event
    ON public.notifications (recipient_id, event_key);
"""

# The panel and the list: this recipient's notifications, newest first.
BY_RECIPIENT = """
CREATE INDEX IF NOT EXISTS idx_notifications_recipient_newest
    ON public.notifications (recipient_id, created_at DESC, id DESC);
"""

# The shell's unread count, and the list's unread filter. Partial, so it
# holds only what is still unread - which is the small part, and stays
# small however long the history grows.
UNREAD = """
CREATE INDEX IF NOT EXISTS idx_notifications_unread
    ON public.notifications (recipient_id, created_at DESC, id DESC)
    WHERE read_at IS NULL;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0007_reservations"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=TABLE,
                    reverse_sql="DROP TABLE IF EXISTS public.notifications;",
                ),
                migrations.RunSQL(
                    sql=ONE_PER_EVENT,
                    reverse_sql=(
                        "DROP INDEX IF EXISTS unique_notification_event;"
                    ),
                ),
                migrations.RunSQL(
                    sql=BY_RECIPIENT,
                    reverse_sql=(
                        "DROP INDEX IF EXISTS "
                        "idx_notifications_recipient_newest;"
                    ),
                ),
                migrations.RunSQL(
                    sql=UNREAD,
                    reverse_sql="DROP INDEX IF EXISTS idx_notifications_unread;",
                ),
            ],
            state_operations=[
                migrations.CreateModel(
                    name="Notification",
                    fields=[
                        ("id", models.AutoField(primary_key=True, serialize=False)),
                        (
                            "event_type",
                            models.CharField(
                                choices=[
                                    (
                                        "reservation_ready",
                                        "Reserved book available",
                                    ),
                                    (
                                        "stock_check_missing",
                                        "Stock check found copies missing",
                                    ),
                                ],
                                max_length=50,
                            ),
                        ),
                        ("event_key", models.CharField(max_length=200)),
                        ("title", models.CharField(max_length=255)),
                        ("message", models.TextField(blank=True, default="")),
                        ("url", models.CharField(blank=True, default="", max_length=500)),
                        ("created_at", models.DateTimeField()),
                        ("read_at", models.DateTimeField(blank=True, null=True)),
                        (
                            "recipient",
                            models.ForeignKey(
                                db_column="recipient_id",
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="notifications",
                                to="library.user",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "notifications",
                        "managed": False,
                    },
                ),
            ],
        ),
    ]
