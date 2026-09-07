"""Menu permissions: the `role_features` table, and a SuperAdmin role.

Two changes, both additive.

**`role_features`** holds overrides and nothing else. A row says "this role
does / does not get this part of the menu"; no row means "whatever
library/features.py says by default". That is why the table is created
empty and why an installation upgrading to it behaves exactly as it did the
day before - there is nothing in it to change anything.

The unique index on `(role, feature_key)` is the whole of the integrity
story: one pair, one answer. Without it the matrix could save two
contradicting rows and the resolver would return whichever the planner
happened to read first.

`role` and `feature_key` are plain text with no foreign keys, and
deliberately without a CHECK constraint. Roles and feature keys are a fixed
list in *code* (`library/features.py`), not rows, and the resolver already
ignores anything it does not recognise - so a key that is renamed or
retired leaves a harmless orphan row rather than a migration to write. A
CHECK here would mean a migration every time a menu entry is added, which
is the deploy this whole feature exists to avoid.

**`check_user_role`** gains `SuperAdmin`. It is dropped and recreated rather
than altered because PostgreSQL has no ALTER for a CHECK's expression;
`DROP CONSTRAINT IF EXISTS` first keeps this safe to run twice and against a
database that already has it. Widening a CHECK cannot fail on existing
rows - every value that satisfied the old list satisfies the new one - so
no row is validated against anything it did not already meet.

Every model in this app is `managed = False`, so Django's schema editor
would skip a CreateModel entirely and the autodetector would change project
state without touching PostgreSQL. The table is therefore created with
explicit SQL paired with the matching state operation via
SeparateDatabaseAndState - the same shape as 0006, 0007, 0008 and 0010.

`IF NOT EXISTS` throughout keeps this safe against a database that already
has the table, notably the test database, which is a schema clone reused
with --keepdb rather than built by running migrations.
scripts/test_schema.sql is updated to match.
"""

import django.db.models.deletion
from django.db import migrations, models


TABLE = """
CREATE TABLE IF NOT EXISTS public.role_features (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role character varying(30) NOT NULL,
    feature_key character varying(50) NOT NULL,
    allowed boolean NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    updated_by integer REFERENCES public.users(id)
);
"""

# One pair, one answer. The matrix saves the whole grid in one POST, so
# without this a double submit could leave two rows disagreeing.
ONE_PER_PAIR = """
CREATE UNIQUE INDEX IF NOT EXISTS unique_role_feature
    ON public.role_features (role, feature_key);
"""

# The resolver reads every override at once and caches them, so there is no
# per-role lookup to index for. This is the only read shape there is.

ADD_SUPER_ADMIN = """
ALTER TABLE public.users DROP CONSTRAINT IF EXISTS check_user_role;
ALTER TABLE public.users ADD CONSTRAINT check_user_role CHECK (
    role::text = ANY (ARRAY[
        'SuperAdmin'::character varying,
        'Admin'::character varying,
        'Librarian'::character varying,
        'Assistant'::character varying
    ]::text[])
);
"""

# Reversing narrows the list again, which would fail if a SuperAdmin exists.
# That is the correct behaviour: the constraint is what keeps the column
# honest, and silently deleting or demoting an account to satisfy a
# rollback would be worse than refusing it.
REMOVE_SUPER_ADMIN = """
ALTER TABLE public.users DROP CONSTRAINT IF EXISTS check_user_role;
ALTER TABLE public.users ADD CONSTRAINT check_user_role CHECK (
    role::text = ANY (ARRAY[
        'Admin'::character varying,
        'Librarian'::character varying,
        'Assistant'::character varying
    ]::text[])
);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0010_acquisition_suggestions"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=TABLE,
                    reverse_sql="DROP TABLE IF EXISTS public.role_features;",
                ),
                migrations.RunSQL(
                    sql=ONE_PER_PAIR,
                    reverse_sql="DROP INDEX IF EXISTS unique_role_feature;",
                ),
                migrations.RunSQL(
                    sql=ADD_SUPER_ADMIN,
                    reverse_sql=REMOVE_SUPER_ADMIN,
                ),
            ],
            state_operations=[
                migrations.CreateModel(
                    name="RoleFeature",
                    fields=[
                        ("id", models.AutoField(primary_key=True, serialize=False)),
                        ("role", models.CharField(max_length=30)),
                        ("feature_key", models.CharField(max_length=50)),
                        ("allowed", models.BooleanField()),
                        ("updated_at", models.DateTimeField()),
                        (
                            "updated_by",
                            models.ForeignKey(
                                blank=True,
                                db_column="updated_by",
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="feature_changes",
                                to="library.user",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "role_features",
                        "managed": False,
                    },
                ),
                migrations.AlterField(
                    model_name="user",
                    name="role",
                    field=models.CharField(
                        choices=[
                            ("SuperAdmin", "Super Admin"),
                            ("Admin", "Admin"),
                            ("Librarian", "Librarian"),
                            ("Assistant", "Assistant"),
                        ],
                        max_length=30,
                    ),
                ),
            ],
        ),
    ]
