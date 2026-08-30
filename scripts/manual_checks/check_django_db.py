import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import django
from django.db import connection


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django.setup()

with connection.cursor() as cursor:
    cursor.execute("SELECT current_database();")
    database_name = cursor.fetchone()[0]

print("Django PostgreSQL connection successful!")
print("Database:", database_name)