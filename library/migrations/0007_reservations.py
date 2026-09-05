"""Add the reservations table.

One table, six columns. A reservation is a borrower, a book, when they
asked, and how it ended - and nothing else. No expiry, no priority, no
copy held back on a shelf: see library/models.py for why.

Two constraints carry rules that would otherwise live only in Python:

  * `unique_active_reservation` - a partial unique index over the active
    rows. One borrower cannot hold two places in the same queue, and
    because the database enforces it, two simultaneous requests cannot
    both create the first one. Partial, so a borrower who has already had
    the book once can join the queue again.

  * `check_reservation_closed` - a reservation is active exactly when it
    has no closing time. The status and the timestamp cannot come apart,
    which is what keeps "fulfilled with no date" and "still waiting but
    closed last Tuesday" out of the table.

Ordering is `(created_at, id)` everywhere it matters. `created_at` alone
would leave two reservations made in the same millisecond in whatever
order the database felt like, and a queue whose order changes between two
page loads is not a queue.

Every model in this app is `managed = False`, so Django's schema editor
creates nothing - the table is made with explicit SQL and paired with the
matching state operation via SeparateDatabaseAndState, the same shape as
0002 through 0006. `IF NOT EXISTS` keeps it safe against a database that
already has it, notably the test database, which is restored from
scripts/test_schema.sql rather than built by running migrations.
"""

from django.db import migrations, models
import django.db.models.deletion


TABLE = """
CREATE TABLE IF NOT EXISTS public.reservations (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    borrower_id integer NOT NULL REFERENCES public.borrowers(id),
    book_id integer NOT NULL REFERENCES public.books(id),
    status character varying(20) DEFAULT 'Active'::character varying NOT NULL,
    created_at timestamp with time zone NOT NULL,
    closed_at timestamp with time zone,

    CONSTRAINT check_reservation_status CHECK (
        status IN ('Active', 'Fulfilled', 'Cancelled')
    ),

    CONSTRAINT check_reservation_closed CHECK (
        (status = 'Active' AND closed_at IS NULL)
        OR (status <> 'Active' AND closed_at IS NOT NULL)
    )
);
"""

# One place in one queue, held by the database rather than by a check that
# two concurrent requests could both pass.
ONE_ACTIVE = """
CREATE UNIQUE INDEX IF NOT EXISTS unique_active_reservation
    ON public.reservations (borrower_id, book_id)
    WHERE status = 'Active';
"""

# Every queue read is "the active reservations for this book, oldest
# first", which is exactly this index.
BY_BOOK = """
CREATE INDEX IF NOT EXISTS idx_reservations_book_queue
    ON public.reservations (book_id, created_at, id)
    WHERE status = 'Active';
"""

# And the borrower's own page asks the other way round.
BY_BORROWER = """
CREATE INDEX IF NOT EXISTS idx_reservations_borrower
    ON public.reservations (borrower_id, created_at);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0006_inventory_sessions"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=TABLE,
                    reverse_sql="DROP TABLE IF EXISTS public.reservations;",
                ),
                migrations.RunSQL(
                    sql=ONE_ACTIVE,
                    reverse_sql="DROP INDEX IF EXISTS unique_active_reservation;",
                ),
                migrations.RunSQL(
                    sql=BY_BOOK,
                    reverse_sql="DROP INDEX IF EXISTS idx_reservations_book_queue;",
                ),
                migrations.RunSQL(
                    sql=BY_BORROWER,
                    reverse_sql="DROP INDEX IF EXISTS idx_reservations_borrower;",
                ),
            ],
            state_operations=[
                migrations.CreateModel(
                    name="Reservation",
                    fields=[
                        ("id", models.AutoField(primary_key=True, serialize=False)),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("Active", "Waiting"),
                                    ("Fulfilled", "Fulfilled"),
                                    ("Cancelled", "Cancelled"),
                                ],
                                default="Active",
                                max_length=20,
                            ),
                        ),
                        ("created_at", models.DateTimeField()),
                        ("closed_at", models.DateTimeField(blank=True, null=True)),
                        (
                            "book",
                            models.ForeignKey(
                                db_column="book_id",
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="reservations",
                                to="library.book",
                            ),
                        ),
                        (
                            "borrower",
                            models.ForeignKey(
                                db_column="borrower_id",
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="reservations",
                                to="library.borrower",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "reservations",
                        "managed": False,
                    },
                ),
            ],
        ),
    ]
