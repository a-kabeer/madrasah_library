"""Add books.cover_image.

Every model in this app is `managed = False`, so Django's schema editor
skips AddField entirely — the autodetector would produce a migration that
changes the project state and never touches PostgreSQL. The column is
therefore added with explicit SQL, paired with the matching state change via
SeparateDatabaseAndState so `makemigrations` stays quiet afterwards.

The column is nullable and gets no default, so adding it does not rewrite
the table and leaves every existing row untouched (their cover is simply
NULL). `IF NOT EXISTS` keeps this safe to run against a database that
already has the column — notably the test database, which is built by
restoring scripts/test_schema.sql rather than by running migrations.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE books "
                        "ADD COLUMN IF NOT EXISTS "
                        "cover_image varchar(255) NULL;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE books "
                        "DROP COLUMN IF EXISTS cover_image;"
                    ),
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="book",
                    name="cover_image",
                    field=models.ImageField(
                        blank=True,
                        max_length=255,
                        null=True,
                        upload_to="book_covers/",
                    ),
                ),
            ],
        ),
    ]
