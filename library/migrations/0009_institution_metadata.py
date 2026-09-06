"""Add the four institution-metadata columns to organization_settings.

Who this installation belongs to, for the places that have to say so: the
sidebar, a printed report, a sheet of labels.

Only what was actually missing. The institution's *name* is already
`organization_settings.name` - the column the sidebar, the page titles, the
login page, the report headers and the label sheets have read since 0003 -
and its email and phone are already `contact_email` and `contact_phone`.
Adding `institution_name`, `institution_email` and `institution_phone`
beside them would give the library two answers to each of three questions,
and the day they disagreed nothing could say which was right. So these four
columns and no more, on the row this installation already configures itself
through: one institution, pinned to a single row by
`organization_settings_singleton`, with no branch and no campus hierarchy.

`institution_type` is plain text rather than a CHECK-constrained
enumeration, unlike every status column in this schema. Nothing branches on
it - it is printed and never tested - so a list would buy no correctness
and would need a migration the first time an institution described itself
in a way the list did not anticipate.

All four are `NOT NULL DEFAULT ''` rather than nullable, matching
`name`, `contact_email`, `contact_phone` and `footer_text` immediately
above them. On PostgreSQL 11 and later a column added with a constant
default is a catalogue change and rewrites nothing, so an existing
installation gains four empty strings and reads exactly as it did: blank
metadata stays valid, and every display added alongside this shows nothing
at all rather than an empty label. Nothing is dropped, narrowed or
renamed.

Every model in this app is `managed = False`, so Django's schema editor
skips AddField entirely and the autodetector would change project state
without touching PostgreSQL. The columns are therefore added with explicit
SQL, paired with the matching state operations via
SeparateDatabaseAndState, so `makemigrations --check` stays quiet
afterwards - the same shape as 0002, 0004 and 0005.

`IF NOT EXISTS` keeps this safe to run against a database that already has
the columns - notably the test database, which is a schema clone reused
with --keepdb rather than built by running migrations, and
scripts/test_schema.sql, which is updated to match.

No index: the row is read by primary key, once per request at most, and
there is one of it.
"""

from django.db import migrations, models


COLUMNS = (
    # (name, type, model field)
    (
        "name_arabic",
        "character varying(255)",
        models.CharField(blank=True, default="", max_length=255),
    ),
    (
        "institution_type",
        "character varying(100)",
        models.CharField(blank=True, default="", max_length=100),
    ),
    (
        "address",
        "text",
        models.TextField(blank=True, default=""),
    ),
    (
        "website",
        "character varying(255)",
        models.URLField(blank=True, default="", max_length=255),
    ),
)


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0008_notifications"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE organization_settings "
                        "ADD COLUMN IF NOT EXISTS %s %s NOT NULL DEFAULT '';"
                        % (name, sql_type)
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
