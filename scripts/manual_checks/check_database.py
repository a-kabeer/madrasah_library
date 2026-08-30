import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from database import get_connection

connection = get_connection()

print("Database connection successful!")

connection.close()

print("Database connection closed.")
