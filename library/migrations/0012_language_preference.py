"""Add `users.language_preference`.

One nullable column, the same shape `theme_preference` has had since 0003
and for the same reasons.

Nullable with no default, rather than `NOT NULL DEFAULT 'en'`, because "has
made no choice" and "chose English" are genuinely different states. The
first should follow the browser's `Accept-Language`, which is what somebody
who has never opened the switcher expects; the second should override it
even on a browser asking for Urdu. A default would collapse the two and
there would be no way back to the first.

No CHECK constraint on the value. The codes come from
`settings.LANGUAGES`, which is a list in code, and adding a language should
not need a migration - `library/middleware.py` ignores a stored code that
is no longer offered, so a retired language degrades to "follow the
browser" rather than to an error.

`varchar(5)` holds every code this is ever likely to carry: two letters, or
a language-region pair like `pt-br`.

Every model in this app is `managed = False`, so Django's schema editor
skips AddField entirely and the autodetector would change project state
without touching PostgreSQL. The column is therefore added with explicit
SQL paired with the matching state operation via SeparateDatabaseAndState -
the same shape as 0002, 0004, 0005 and 0009.

`ADD COLUMN IF NOT EXISTS` keeps this safe to run against a database that
already has it, notably the test database, which is a schema clone reused
with --keepdb rather than built by running migrations.
scripts/test_schema.sql is updated to match.

No index: it is read as part of the user row, by primary key, and never
searched on.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0011_role_features"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE users "
                        "ADD COLUMN IF NOT EXISTS language_preference "
                        "character varying(5);"
                    ),
                    reverse_sql=(
                        "ALTER TABLE users "
                        "DROP COLUMN IF EXISTS language_preference;"
                    ),
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="user",
                    name="language_preference",
                    field=models.CharField(
                        blank=True,
                        choices=[
                            ("en", "English"),
                            ("ur", "اردو"),
                            ("ar", "العربية"),
                        ],
                        max_length=5,
                        null=True,
                    ),
                ),
            ],
        ),
    ]
