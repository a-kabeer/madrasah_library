"""Add books.archived_at.

Archiving a book had nowhere to live: `books` carries only its
bibliographic columns, and none of them can say "this is out of the active
catalogue" without also meaning something else. So one column, and only
one — copy withdrawal reuses the status values that already exist, and no
other entity gains an archive flag.

A timestamp rather than a boolean. NULL means active, which is
unambiguous, and a set value records *when* it happened — which is the
question anyone asking about an archived book actually has. One column
serves as both the flag and its own small audit trail.

Every model in this app is `managed = False`, so Django's schema editor
skips AddField entirely: the autodetector would change the project state
and never touch PostgreSQL. The column is therefore added with explicit
SQL, paired with the matching state change via SeparateDatabaseAndState so
`makemigrations` stays quiet afterwards — the same shape as
0002_book_cover_image.

Nullable with no default, so adding it does not rewrite the table and
every existing row stays active. `IF NOT EXISTS` keeps it safe to run
against a database that already has the column — notably the test
database, which is a schema clone reused with --keepdb rather than built
by running migrations, and scripts/test_schema.sql, which is updated to
match.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0003_organization_settings_and_theme_preference"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE books "
                        "ADD COLUMN IF NOT EXISTS "
                        "archived_at timestamp with time zone NULL;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE books "
                        "DROP COLUMN IF EXISTS archived_at;"
                    ),
                ),
                # Every catalogue query filters on this, and almost every
                # row is NULL, so a partial index over the archived ones is
                # both tiny and the only part worth indexing.
                migrations.RunSQL(
                    sql=(
                        "CREATE INDEX IF NOT EXISTS idx_books_archived_at "
                        "ON books (archived_at) "
                        "WHERE archived_at IS NOT NULL;"
                    ),
                    reverse_sql=(
                        "DROP INDEX IF EXISTS idx_books_archived_at;"
                    ),
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="book",
                    name="archived_at",
                    field=models.DateTimeField(blank=True, null=True),
                ),
            ],
        ),
    ]
