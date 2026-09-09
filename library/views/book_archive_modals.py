"""Modal-first archive workflow for books."""

from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone

from ..models import Book, BookCopy, Loan
from ..permissions import feature_required
from .common import BOOK_CACHE_KEY, DASHBOARD_CACHE_KEY, BOOK_COPY_CACHE_KEY, create_activity_log, is_form_modal_request, modal_redirect


def _archive_blocker(book):
    issued = BookCopy.objects.filter(
        volume__book=book,
        id__in=Loan.objects.filter(return_date__isnull=True).values("copy_id"),
    ).count()
    if issued:
        return (
            "This book cannot be archived because %d of its cop%s currently "
            "out on loan. Take %s back first."
            % (
                issued,
                "y is" if issued == 1 else "ies are",
                "it" if issued == 1 else "them",
            )
        )
    return ""


@feature_required("books", "Admin", "Librarian")
def book_archive_modal(request, book_id):
    """Confirm or perform a book archive without leaving the current page."""
    book = get_object_or_404(Book, id=book_id)
    blocker = _archive_blocker(book) if not book.is_archived else ""

    if request.method == "POST" and not blocker and not book.is_archived:
        book.archived_at = timezone.now()
        book.save(update_fields=["archived_at"])

        cache.delete(BOOK_CACHE_KEY)
        cache.delete(BOOK_COPY_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="ARCHIVE",
            entity_type="Book",
            entity_id=book.id,
            description="%s archived" % book.title,
        )

        return modal_redirect(
            request,
            request.build_absolute_uri("/library/books/%s/" % book.id),
        )

    context = {
        "book": book,
        "blocker": blocker,
        "copy_count": BookCopy.objects.filter(volume__book=book).count(),
        "loan_count": Loan.objects.filter(copy__volume__book=book).count(),
    }

    if is_form_modal_request(request):
        return render(request, "library/partials/book_archive_modal.html", context)

    if request.method == "POST" and blocker:
        # Redirected before, but without a word about why.
        messages.error(request, blocker)
        return redirect("book_detail", book_id=book.id)

    return render(request, "library/book_archive.html", context)
