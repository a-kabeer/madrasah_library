"""Modal-first restore workflow for books."""

from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render, redirect

from ..models import Book
from ..permissions import feature_required
from .common import BOOK_CACHE_KEY, BOOK_COPY_CACHE_KEY, DASHBOARD_CACHE_KEY, create_activity_log, is_form_modal_request


@feature_required("books", "Admin", "Librarian")
def book_restore_modal(request, book_id):
    """Confirm or perform a book restore without leaving the current page."""
    book = get_object_or_404(Book, id=book_id)
    modal_request = is_form_modal_request(request)

    if request.method == "POST" and book.is_archived:
        book.archived_at = None
        book.save(update_fields=["archived_at"])

        cache.delete(BOOK_CACHE_KEY)
        cache.delete(BOOK_COPY_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="RESTORE",
            entity_type="Book",
            entity_id=book.id,
            description="%s restored to the catalogue" % book.title,
        )

        if modal_request:
            response = HttpResponse(status=204)
            response["HX-Redirect"] = request.build_absolute_uri(
                "/library/books/%s/" % book.id
            )
            return response

        return redirect("book_detail", book_id=book.id)

    if modal_request:
        return render(
            request,
            "library/partials/book_restore_modal.html",
            {"book": book},
        )

    return render(request, "library/book_detail.html", {"book": book})
