"""Searchable combobox endpoints for catalogue list filters."""

from django.db import models
from django.shortcuts import render
from django.urls import reverse

from ..models import BookContent, BookVolume
from ..permissions import feature_required
from .common import combobox_options_response


def _volume_label(volume):
    number = "Volume %s" % volume.volume_number
    title = (volume.title or "").strip()
    if title:
        return "%s — %s — %s" % (volume.book.title, number, title)
    return "%s — %s" % (volume.book.title, number)


@feature_required("books")
def volume_filter_combobox(request):
    """Return volume choices for the Book Contents filter."""
    search = request.GET.get("search", "").strip()
    volumes = BookVolume.objects.select_related("book").order_by(
        "book__title", "volume_number", "id"
    )

    if search:
        query = (
            models.Q(book__title__icontains=search)
            | models.Q(title__icontains=search)
        )
        if search.isdigit():
            query |= models.Q(volume_number=int(search))
        volumes = volumes.filter(query)

    items = list(volumes)
    for volume in items:
        volume.name = _volume_label(volume)

    return combobox_options_response(
        request,
        items=items,
        search=search,
        entity_label="volume",
        add_url=reverse("book_volume_add"),
    )


@feature_required("books")
def book_filter_combobox(request):
    """Return book choices for the Book Volumes filter."""
    from ..models import Book

    search = request.GET.get("search", "").strip()
    books = Book.objects.all().order_by("title", "id")

    if search:
        books = books.filter(title__icontains=search)

    items = list(books)
    for book in items:
        book.name = book.title

    return combobox_options_response(
        request,
        items=items,
        search=search,
        entity_label="book",
        add_url=reverse("book_add"),
    )


@feature_required("books")
def content_type_filter_combobox(request):
    """Return content-type choices when a searchable type filter is useful."""
    search = request.GET.get("search", "").strip()
    values = BookContent.objects.exclude(
        content_type__isnull=True
    ).exclude(
        content_type=""
    ).values_list("content_type", flat=True).distinct().order_by("content_type")

    if search:
        values = values.filter(content_type__icontains=search)

    items = []
    for value in values:
        items.append(type("ComboItem", (), {"id": value, "name": value})())

    return render(
        request,
        "library/partials/combobox_options.html",
        {
            "items": items[:20],
            "total_count": len(items),
            "limit": 20,
            "search": search,
            "exact_match": any(item.name.casefold() == search.casefold() for item in items),
            "entity_label": "content type",
            "add_url": "",
            "can_create": False,
        },
    )
