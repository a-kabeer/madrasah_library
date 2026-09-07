"""Does a plain psycopg connection to the configured database work?

Deliberately not Django: this is the check you run when Django itself will
not start, to find out whether the database or the settings are at fault.
"""

from db_connection import get_connection

connection = get_connection()

print("Database connection successful!")

connection.close()

print("Database connection closed.")
