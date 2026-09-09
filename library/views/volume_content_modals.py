"""Modal-first CRUD for book volumes and their contents."""

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from ..models import Book, BookContent, BookCopy, BookVolume
from ..permissions import feature_required
from .common import (
    BOOK_CONTENT_CACHE_KEY,
    BOOK_VOLUME_CACHE_KEY,
    DASHBOARD_CACHE_KEY,
    create_activity_log,
    modal_redirect,
    safe_redirect_target,
)


def _redirect_response(request, fallback="book_volume_list"):
    return modal_redirect(
        request,
        request.build_absolute_uri(safe_redirect_target(request, fallback)),
    )


def _form_request(request):
    return (
        request.GET.get("modal") == "1"
        or request.POST.get("modal") == "1"
        or request.headers.get("HX-Request") == "true"
    )


@feature_required("books", "Admin", "Librarian")
def book_volume_add_modal(request):
    book_id = (request.GET.get("book") or request.POST.get("book") or "").strip()
    books = Book.objects.all().order_by("title")
    book = Book.objects.filter(id=book_id).first() if book_id.isdigit() else None
    error = ""
    volume_number = (request.POST.get("volume_number") or "").strip()
    title = (request.POST.get("title") or "").strip()

    if request.method == "POST":
        if not book:
            error = "Please choose a book from the list."
        elif not volume_number or not title:
            error = "Please fill in all required fields."
        else:
            try:
                number = int(volume_number)
            except ValueError:
                number = 0
                error = "Volume number must be a whole number, like 1 or 2."
            if not error and number < 1:
                error = "Volume number must be 1 or more."
            if not error and BookVolume.objects.filter(book=book, volume_number=number).exists():
                error = f"Volume {number} already exists for this book."
            if not error:
                try:
                    with transaction.atomic():
                        volume = BookVolume.objects.create(book=book, volume_number=number, title=title)
                except IntegrityError:
                    error = f"Volume {number} already exists for this book."
                else:
                    cache.delete(BOOK_VOLUME_CACHE_KEY)
                    cache.delete(DASHBOARD_CACHE_KEY)
                    create_activity_log(user=request.user, action="CREATE", entity_type="BookVolume", entity_id=volume.id, description=f"{volume.title} (Volume {volume.volume_number}) added")
                    return _redirect_response(request, "book_volume_list")

    return render(request, "library/partials/book_volume_form_modal.html", {"books": books, "book": book, "volume": None, "volume_number": volume_number, "title": title, "error": error, "editing": False})


@feature_required("books", "Admin", "Librarian")
def book_volume_edit_modal(request, volume_id):
    volume = get_object_or_404(BookVolume.objects.select_related("book"), id=volume_id)
    books = Book.objects.all().order_by("title")
    error = ""
    book_id = (request.POST.get("book") or str(volume.book_id)).strip()
    volume_number = (request.POST.get("volume_number") or str(volume.volume_number)).strip()
    title = (request.POST.get("title") if request.method == "POST" else volume.title).strip()

    if request.method == "POST":
        book = Book.objects.filter(id=book_id).first() if book_id.isdigit() else None
        try:
            number = int(volume_number)
        except ValueError:
            number = 0
            error = "Volume number must be a whole number, like 1 or 2."
        if not book:
            error = "Please choose a book from the list."
        elif number < 1:
            error = "Volume number must be 1 or more."
        elif not title:
            error = "Volume title is required."
        elif BookVolume.objects.filter(book=book, volume_number=number).exclude(id=volume.id).exists():
            error = f"Volume {number} already exists for this book."
        if not error:
            volume.book = book
            volume.volume_number = number
            volume.title = title
            try:
                with transaction.atomic():
                    volume.save()
            except IntegrityError:
                error = f"Volume {number} already exists for this book."
            else:
                cache.delete(BOOK_VOLUME_CACHE_KEY)
                cache.delete(DASHBOARD_CACHE_KEY)
                create_activity_log(user=request.user, action="UPDATE", entity_type="BookVolume", entity_id=volume.id, description=f"{volume.title} (Volume {volume.volume_number}) updated")
                return _redirect_response(request)

    return render(request, "library/partials/book_volume_form_modal.html", {"books": books, "book": volume.book, "volume": volume, "volume_number": volume_number, "title": title, "error": error, "editing": True})


@feature_required("books", "Admin", "Librarian")
def book_volume_delete_modal(request, volume_id):
    volume = get_object_or_404(BookVolume.objects.select_related("book"), id=volume_id)
    copies_exist = volume.bookcopy_set.exists()
    error = "This volume cannot be deleted while it still has physical copies. Delete or move the copies first."
    if request.method == "POST" and not copies_exist:
        volume_id_value = volume.id
        label = f"{volume.title} (Volume {volume.volume_number})"
        volume.delete()
        cache.delete(BOOK_VOLUME_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)
        create_activity_log(user=request.user, action="DELETE", entity_type="BookVolume", entity_id=volume_id_value, description=f"{label} deleted")
        return _redirect_response(request)
    return render(request, "library/partials/book_volume_delete_modal.html", {"volume": volume, "copies_exist": copies_exist, "error": error if copies_exist else ""})


@feature_required("books", "Admin", "Librarian")
def book_volume_detail_modal(request, volume_id):
    if not _form_request(request):
        from .catalog import book_volume_detail
        return book_volume_detail(request, volume_id)

    volume = get_object_or_404(
        BookVolume.objects.select_related(
            "book", "book__author", "book__category", "book__publisher"
        ),
        id=volume_id,
    )
    copies = list(
        BookCopy.objects.filter(volume_id=volume.id)
        .select_related("shelf__location")
        .order_by("copy_code", "id")
    )
    contents = list(
        BookContent.objects.filter(volume_id=volume.id)
        .select_related("parent")
        .order_by("sort_order", "id")
    )
    return render(
        request,
        "library/partials/book_volume_detail_modal.html",
        {
            "volume": volume,
            "copies": copies,
            "contents": contents,
            "can_edit": True,
        },
    )


@feature_required("books", "Admin", "Librarian")
def book_content_add_modal(request):
    volume_id = (request.GET.get("volume") or request.POST.get("volume") or "").strip()
    volume = BookVolume.objects.select_related("book").filter(id=volume_id).first() if volume_id.isdigit() else None
    volumes = BookVolume.objects.select_related("book").order_by("book__title", "volume_number")
    return _content_form(request, None, volume, volumes)


@feature_required("books", "Admin", "Librarian")
def book_content_edit_modal(request, content_id):
    content = get_object_or_404(BookContent.objects.select_related("volume__book", "parent"), id=content_id)
    volumes = BookVolume.objects.select_related("book").order_by("book__title", "volume_number")
    return _content_form(request, content, content.volume, volumes)


def _content_form(request, content, selected_volume, volumes):
    error = ""
    if request.method == "POST":
        volume_id = (request.POST.get("volume") or "").strip()
        parent_id = (request.POST.get("parent") or "").strip()
        title = (request.POST.get("title") or "").strip()
        content_type = (request.POST.get("content_type") or "").strip()
        page_number = (request.POST.get("page_number") or "").strip()
        sort_order = (request.POST.get("sort_order") or "0").strip()
        volume = BookVolume.objects.filter(id=volume_id).first() if volume_id.isdigit() else None
        parent = BookContent.objects.filter(id=parent_id).first() if parent_id.isdigit() else None
        try:
            page_value = int(page_number) if page_number else None
            sort_value = int(sort_order)
        except ValueError:
            page_value = None
            sort_value = 0
            error = "Page number and sort order must be whole numbers."
        if not volume:
            error = "Please choose a volume."
        elif not title:
            error = "Content title is required."
        elif page_value is not None and page_value < 1:
            error = "Page number must be 1 or more."
        elif sort_value < 0:
            error = "Sort order cannot be negative."
        elif parent and parent.volume_id != volume.id:
            error = "Parent content must belong to the selected volume."
        elif content and parent and parent.id == content.id:
            error = "Content cannot be its own parent."
        if not error:
            if content is None:
                content = BookContent(volume=volume)
                action = "CREATE"
            else:
                content.volume = volume
                action = "UPDATE"
            content.parent = parent
            content.title = title
            content.content_type = content_type
            content.page_number = page_value
            content.sort_order = sort_value
            content.save()
            cache.delete(BOOK_CONTENT_CACHE_KEY)
            cache.delete(DASHBOARD_CACHE_KEY)
            create_activity_log(user=request.user, action=action, entity_type="BookContent", entity_id=content.id, description=f"{content.title} {'added' if action == 'CREATE' else 'updated'}")
            return _redirect_response(request, "book_content_list")
    else:
        title = content.title if content else ""
        content_type = content.content_type if content else ""
        page_number = content.page_number if content and content.page_number is not None else ""
        sort_order = content.sort_order if content else 0
        volume_id = selected_volume.id if selected_volume else ""
        parent_id = content.parent_id if content else ""

    parents = BookContent.objects.filter(volume_id=volume_id).exclude(id=content.id if content else None).order_by("sort_order", "id") if str(volume_id).isdigit() else BookContent.objects.none()
    return render(request, "library/partials/book_content_form_modal.html", {"content": content, "volumes": volumes, "selected_volume": selected_volume, "parents": parents, "volume_id": volume_id, "parent_id": parent_id, "title": title, "content_type": content_type, "page_number": page_number, "sort_order": sort_order, "error": error, "editing": content is not None})


@feature_required("books", "Admin", "Librarian")
def book_content_detail_modal(request, content_id):
    if not _form_request(request):
        from .catalog import book_content_detail
        return book_content_detail(request, content_id)

    content = get_object_or_404(
        BookContent.objects.select_related("volume__book", "parent"),
        id=content_id,
    )
    children = list(
        BookContent.objects.filter(parent_id=content.id)
        .order_by("sort_order", "id")
    )
    return render(
        request,
        "library/partials/book_content_detail_modal.html",
        {"content": content, "children": children, "can_edit": True},
    )


@feature_required("books", "Admin", "Librarian")
def book_content_delete_modal(request, content_id):
    content = get_object_or_404(BookContent.objects.select_related("volume__book", "parent"), id=content_id)
    child_count = content.children.count()
    if request.method == "POST":
        content.delete()
        cache.delete(BOOK_CONTENT_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)
        create_activity_log(user=request.user, action="DELETE", entity_type="BookContent", entity_id=content_id, description=f"{content.title} deleted")
        return _redirect_response(request, "book_content_list")
    return render(request, "library/partials/book_content_delete_modal.html", {"content": content, "child_count": child_count})
