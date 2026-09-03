"""Add the organization_settings table and users.theme_preference.

Every model in this app is `managed = False`, so Django's schema editor
skips CreateModel/AddField entirely — the autodetector would change project
state and never touch PostgreSQL. Both changes are therefore made with
explicit SQL, paired with matching state operations via
SeparateDatabaseAndState so `makemigrations` stays quiet afterwards.

Neither change rewrites existing data:

  * organization_settings starts empty. `OrganizationSettings.load()`
    returns an unsaved instance with defaults until a row is saved, so the
    app works before anything is configured.
  * users.theme_preference is nullable with no default, so adding it does
    not rewrite the table and every existing user keeps working. NULL is
    read as "system".

`IF NOT EXISTS` keeps this safe against a database that already has them —
notably the test database, which is built by restoring
scripts/test_schema.sql rather than by running migrations.
"""

from django.db import migrations, models

import library.models


CREATE_ORGANIZATION_SETTINGS = """
CREATE TABLE IF NOT EXISTS organization_settings (
    id integer NOT NULL,
    name character varying(255) NOT NULL DEFAULT '',
    logo character varying(255),
    favicon character varying(255),
    primary_color character varying(7) NOT NULL DEFAULT '',
    secondary_color character varying(7) NOT NULL DEFAULT '',
    accent_color character varying(7) NOT NULL DEFAULT '',
    contact_email character varying(255) NOT NULL DEFAULT '',
    contact_phone character varying(50) NOT NULL DEFAULT '',
    footer_text text NOT NULL DEFAULT '',
    updated_at timestamp with time zone,
    CONSTRAINT organization_settings_pkey PRIMARY KEY (id),
    CONSTRAINT organization_settings_singleton CHECK (id = 1)
);
"""

DROP_ORGANIZATION_SETTINGS = """
DROP TABLE IF EXISTS organization_settings;
"""

ADD_THEME_PREFERENCE = """
ALTER TABLE users
ADD COLUMN IF NOT EXISTS theme_preference character varying(10) NULL;
"""

DROP_THEME_PREFERENCE = """
ALTER TABLE users
DROP COLUMN IF EXISTS theme_preference;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0002_book_cover_image"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=CREATE_ORGANIZATION_SETTINGS,
                    reverse_sql=DROP_ORGANIZATION_SETTINGS,
                ),
                migrations.RunSQL(
                    sql=ADD_THEME_PREFERENCE,
                    reverse_sql=DROP_THEME_PREFERENCE,
                ),
            ],
            state_operations=[
                migrations.CreateModel(
                    name="OrganizationSettings",
                    fields=[
                        (
                            "id",
                            models.AutoField(
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        (
                            "name",
                            models.CharField(
                                blank=True,
                                default="",
                                max_length=255,
                            ),
                        ),
                        (
                            "logo",
                            models.ImageField(
                                blank=True,
                                max_length=255,
                                null=True,
                                upload_to="branding/",
                            ),
                        ),
                        (
                            "favicon",
                            models.ImageField(
                                blank=True,
                                max_length=255,
                                null=True,
                                upload_to="branding/",
                            ),
                        ),
                        (
                            "primary_color",
                            models.CharField(
                                blank=True,
                                default="",
                                max_length=7,
                                validators=[
                                    library.models.validate_hex_color
                                ],
                            ),
                        ),
                        (
                            "secondary_color",
                            models.CharField(
                                blank=True,
                                default="",
                                max_length=7,
                                validators=[
                                    library.models.validate_hex_color
                                ],
                            ),
                        ),
                        (
                            "accent_color",
                            models.CharField(
                                blank=True,
                                default="",
                                max_length=7,
                                validators=[
                                    library.models.validate_hex_color
                                ],
                            ),
                        ),
                        (
                            "contact_email",
                            models.EmailField(
                                blank=True,
                                default="",
                                max_length=255,
                            ),
                        ),
                        (
                            "contact_phone",
                            models.CharField(
                                blank=True,
                                default="",
                                max_length=50,
                            ),
                        ),
                        (
                            "footer_text",
                            models.TextField(
                                blank=True,
                                default="",
                            ),
                        ),
                        (
                            "updated_at",
                            models.DateTimeField(
                                blank=True,
                                null=True,
                            ),
                        ),
                    ],
                    options={
                        "db_table": "organization_settings",
                        "managed": False,
                    },
                ),
                migrations.AddField(
                    model_name="user",
                    name="theme_preference",
                    field=models.CharField(
                        blank=True,
                        choices=[
                            ("light", "Light"),
                            ("dark", "Dark"),
                            ("system", "System"),
                        ],
                        max_length=10,
                        null=True,
                    ),
                ),
            ],
        ),
    ]
