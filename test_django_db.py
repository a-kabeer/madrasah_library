import os

import django
from django.db import connection


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django.setup()

with connection.cursor() as cursor:
    cursor.execute("SELECT current_database();")
    database_name = cursor.fetchone()[0]

print("Django PostgreSQL connection successful!")
print("Database:", database_name)