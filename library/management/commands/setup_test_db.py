import os
import subprocess

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from .backup_db import find_pg_dump


def find_psql():
    pg_dump = find_pg_dump()
    if pg_dump:
        candidate = os.path.join(os.path.dirname(pg_dump), "psql.exe")
        if os.path.isfile(candidate):
            return candidate
        candidate = os.path.join(os.path.dirname(pg_dump), "psql")
        if os.path.isfile(candidate):
            return candidate
    return None


class Command(BaseCommand):
    help = (
        "Create (or recreate) the test database from scripts/test_schema.sql, "
        "so `manage.py test --keepdb` has somewhere to run. All app models are "
        "managed=False, so Django's own migrations can't create these tables "
        "in a fresh test database — this command restores the real schema "
        "instead. Safe to re-run any time the schema changes."
    )

    def handle(self, *args, **options):

        db = settings.DATABASES["default"]
        test_name = db["TEST"]["NAME"]

        psql = find_psql()
        if not psql:
            raise CommandError(
                "Could not find psql. Set PGDUMP_PATH in your .env (psql is "
                "expected next to pg_dump) to its full path."
            )

        schema_file = os.path.join(
            settings.BASE_DIR, "scripts", "test_schema.sql"
        )

        if not os.path.isfile(schema_file):
            raise CommandError(f"Schema file not found: {schema_file}")

        env = os.environ.copy()
        if db.get("PASSWORD"):
            env["PGPASSWORD"] = db["PASSWORD"]

        def run_psql(extra_args, database="postgres"):
            cmd = [
                psql,
                "-h", db.get("HOST") or "localhost",
                "-p", str(db.get("PORT") or "5432"),
                "-U", db["USER"],
                "-d", database,
                "-v", "ON_ERROR_STOP=1",
            ] + extra_args
            return subprocess.run(cmd, env=env, capture_output=True, text=True)

        self.stdout.write(f"Dropping database (if exists): {test_name}")
        result = run_psql(["-c", f'DROP DATABASE IF EXISTS "{test_name}";'])
        if result.returncode != 0:
            raise CommandError(f"Failed to drop test database:\n{result.stderr}")

        self.stdout.write(f"Creating database: {test_name}")
        result = run_psql(["-c", f'CREATE DATABASE "{test_name}";'])
        if result.returncode != 0:
            raise CommandError(f"Failed to create test database:\n{result.stderr}")

        self.stdout.write("Enabling pg_trgm extension (used by trigram search indexes)")
        result = run_psql(
            ["-c", "CREATE EXTENSION IF NOT EXISTS pg_trgm;"],
            database=test_name,
        )
        if result.returncode != 0:
            raise CommandError(f"Failed to create pg_trgm extension:\n{result.stderr}")

        self.stdout.write(f"Restoring schema from {schema_file}")
        result = run_psql(["-f", schema_file], database=test_name)
        if result.returncode != 0:
            raise CommandError(f"Failed to restore schema:\n{result.stderr}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Test database '{test_name}' is ready. "
                f"Run tests with: python manage.py test --keepdb"
            )
        )
