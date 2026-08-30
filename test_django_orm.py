import os
import django

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "config.settings"
)

django.setup()

from django.core.cache import cache
from django.db import connection, reset_queries

from library.models import (
    Category,
    Author,
    Publisher,
    Location,
    Shelf,
    Book,
    BookVolume,
    BookContent,
    BookCopy,
    Loan,
    Borrower,
    User,
)


TESTS = [
    ("categories", Category),
    ("authors", Author),
    ("publishers", Publisher),
    ("locations", Location),
    ("shelves", Shelf),
    ("books", Book),
    ("book_volumes", BookVolume),
    ("book_contents", BookContent),
    ("book_copies", BookCopy),
    ("loans", Loan),
    ("borrowers", Borrower),
    ("users", User),
]


for cache_key, model in TESTS:

    cache.delete(cache_key)

    reset_queries()

    data = cache.get(cache_key)

    if data is None:
        data = list(model.objects.all())
        cache.set(cache_key, data, timeout=300)

    first_queries = len(connection.queries)

    reset_queries()

    data = cache.get(cache_key)

    second_queries = len(connection.queries)

    print(f"\n{model.__name__}")
    print(f"First request: Cache MISS")
    print(f"SQL Queries: {first_queries}")
    print(f"Second request: Cache HIT")
    print(f"SQL Queries: {second_queries}")

    print("\n" + "=" * 50)
print("CACHE INVALIDATION TEST")
print("=" * 50)


CACHE_KEYS = [
    "categories",
    "authors",
    "publishers",
    "locations",
    "shelves",
    "books",
    "book_volumes",
    "book_contents",
    "book_copies",
    "loans",
    "borrowers",
    "users",
]


for cache_key in CACHE_KEYS:

    # Step 1: Create cache
    test_data = ["test-cache-data"]

    cache.set(
        cache_key,
        test_data,
        timeout=300
    )

    print(f"\n{cache_key}")
    print("Initial cache created.")

    # Step 2: Verify HIT
    reset_queries()

    cached_data = cache.get(cache_key)

    print(
        "Before change: Cache HIT"
        if cached_data is not None
        else "Before change: Cache MISS"
    )

    print(
        f"SQL Queries: {len(connection.queries)}"
    )

    # Step 3: Simulate database change
    # Actual Add/Update/Delete views already
    # call cache.delete().

    cache.delete(cache_key)

    print("Cache invalidated.")

    # Step 4: Verify MISS
    reset_queries()

    cached_data = cache.get(cache_key)

    print(
        "After invalidation: Cache MISS"
        if cached_data is None
        else "After invalidation: Cache HIT"
    )

    print(
        f"SQL Queries: {len(connection.queries)}"
    )