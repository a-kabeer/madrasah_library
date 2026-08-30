import psycopg
from decouple import config

connection = psycopg.connect(
    host=config("DB_HOST", default="localhost"),
    port=config("DB_PORT", default="5432"),
    dbname=config("DB_NAME"),
    user=config("DB_USER"),
    password=config("DB_PASSWORD"),
)

cursor = connection.cursor()

cursor.execute(
    """
    UPDATE authors
    SET name = %s
    WHERE id = %s
    RETURNING id, name;
""",
    ("امام مسلم", 2),
)

restored_author = cursor.fetchone()

connection.commit()

print("Restored:", restored_author)

cursor.close()
connection.close()

print("Connection closed.")
