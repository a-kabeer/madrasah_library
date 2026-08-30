import glob
import os
import shutil
import subprocess
from datetime import datetime

from decouple import config
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def find_pg_dump():
    configured = config("PGDUMP_PATH", default="")
    if configured and os.path.isfile(configured):
        return configured

    on_path = shutil.which("pg_dump") or shutil.which("pg_dump.exe")
    if on_path:
        return on_path

    candidates = sorted(
        glob.glob(r"C:\Program Files\PostgreSQL\*\bin\pg_dump.exe"),
        reverse=True,
    )
    if candidates:
        return candidates[0]

    return None


class Command(BaseCommand):
    help = (
        "Back up the Postgres database configured in settings.DATABASES "
        "using pg_dump (custom format, suitable for pg_restore)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--out-dir",
            default=None,
            help="Directory to write the backup into "
            "(default: BACKUP_DIR env var, or 'backups/').",
        )

    def handle(self, *args, **options):
        db = settings.DATABASES["default"]

        pg_dump = find_pg_dump()
        if not pg_dump:
            raise CommandError(
                "Could not find pg_dump. Set PGDUMP_PATH in your .env to its "
                "full path (e.g. C:\\Program Files\\PostgreSQL\\18\\bin\\pg_dump.exe)."
            )

        out_dir = options["out_dir"] or config("BACKUP_DIR", default="backups")
        if not os.path.isabs(out_dir):
            out_dir = os.path.join(settings.BASE_DIR, out_dir)
        os.makedirs(out_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = os.path.join(out_dir, f"{db['NAME']}_{timestamp}.dump")

        env = os.environ.copy()
        if db.get("PASSWORD"):
            env["PGPASSWORD"] = db["PASSWORD"]

        cmd = [
            pg_dump,
            "-h", db.get("HOST") or "localhost",
            "-p", str(db.get("PORT") or "5432"),
            "-U", db["USER"],
            "-Fc",
            "-f", out_file,
            db["NAME"],
        ]

        self.stdout.write(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)

        if result.returncode != 0:
            raise CommandError(f"pg_dump failed:\n{result.stderr}")

        size_kb = os.path.getsize(out_file) / 1024
        self.stdout.write(
            self.style.SUCCESS(
                f"Backup written to {out_file} ({size_kb:.1f} KB)"
            )
        )
        self.stdout.write(
            "Restore with: pg_restore --clean --if-exists "
            f"-h <host> -U <user> -d {db['NAME']} \"{out_file}\""
        )
