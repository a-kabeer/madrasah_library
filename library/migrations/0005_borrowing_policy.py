"""Add the four borrowing-policy columns to organization_settings.

The rules had nowhere to live. `DEFAULT_LOAN_PERIOD_DAYS` was a constant in
views.py, and the other three - how many books one borrower may hold,
whether an overdue item stops them borrowing, how many renewals a loan gets
- were not rules the library could state at all.

They go on `organization_settings` rather than in a table of their own.
That table is already the single row this installation configures itself
through, already has a settings screen behind `@role_required("Admin")`,
and already has a CHECK pinning it to one row - so there is exactly one
answer to each of these questions, which is the property a policy needs. A
`borrowing_policy` table with one row would be the same thing with more
joins, and a key-value store would give every rule the type `text`.

Four columns and no more. No per-borrower-type limits, no fines, no grace
periods: see library/policy.py for why.

All nullable with no default, which does three things at once. Adding them
does not rewrite the table; an install that predates them reads as
unconfigured rather than as zero, so nothing about its lending changes
until someone chooses to configure it; and NULL is a real answer -
"nobody has said" - distinct from a 0 that means "no limit".

Every model in this app is `managed = False`, so Django's schema editor
skips AddField entirely and the autodetector would change project state
without touching PostgreSQL. The columns are therefore added with explicit
SQL, paired with the matching state operations via
SeparateDatabaseAndState, so `makemigrations` stays quiet afterwards - the
same shape as 0002_book_cover_image and 0004_book_archived_at.

`IF NOT EXISTS` keeps this safe to run against a database that already has
the columns - notably the test database, which is a schema clone reused
with --keepdb rather than built by running migrations, and
scripts/test_schema.sql, which is updated to match.

No index: the row is read by primary key, once per request, and there is
one of it.
"""

from django.db import migrations, models


COLUMNS = (
    # (name, type, model field)
    ("loan_period_days", "integer", models.IntegerField(blank=True, null=True)),
    ("max_active_loans", "integer", models.IntegerField(blank=True, null=True)),
    ("max_renewals", "integer", models.IntegerField(blank=True, null=True)),
    ("block_when_overdue", "boolean", models.BooleanField(blank=True, null=True)),
)


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0004_book_archived_at"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE organization_settings "
                        "ADD COLUMN IF NOT EXISTS %s %s NULL;" % (name, sql_type)
                    ),
                    reverse_sql=(
                        "ALTER TABLE organization_settings "
                        "DROP COLUMN IF EXISTS %s;" % name
                    ),
                )
                for name, sql_type, _ in COLUMNS
            ],
            state_operations=[
                migrations.AddField(
                    model_name="organizationsettings",
                    name=name,
                    field=field,
                )
                for name, _, field in COLUMNS
            ],
        ),
    ]
