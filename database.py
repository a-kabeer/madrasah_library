import psycopg
from decouple import config


def get_connection():
    connection = psycopg.connect(
        host=config("DB_HOST", default="localhost"),
        port=config("DB_PORT", default="5432"),
        dbname=config("DB_NAME"),
        user=config("DB_USER"),
        password=config("DB_PASSWORD"),
    )

    return connection
