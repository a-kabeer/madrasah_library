"""Suggestions for books the library does not have yet.

A suggestion is never a Book. Turning one into a book goes through the
ordinary Add Book form, so nothing here is a way into the catalogue that
skips it.
"""

from django.contrib import messages
from django.utils.translation import gettext
from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404

from ..models import (
    AcquisitionSuggestion,
)

from .. import acquisitions
from .. import notifications

from ..permissions import can_edit_library, feature_required

from .catalog import suggestion_matches
from .common import (
    PAGE_SIZE,
    create_activity_log,
    query_with,
)


# ==========================================================================
# ACQUISITION SUGGESTIONS
#
# Books somebody thinks the library should have. A suggestion is a note, not
# a catalogue record: nothing here creates an Author, a Publisher, a
# Category, a Book or a BookCopy, and the only route into the catalogue is
# `book_add` with its own validation, its own duplicate check and its own
# permissions. See library/acquisitions.py.
#
# Everyone may write one and read the list - the person who notices a gap
# on the shelf is usually the one standing at it. Only Admin and Librarian
# may decide about one, which is the same line `can_edit_library` already
# draws through the catalogue.
# ==========================================================================


def can_review_suggestions(user):
    """True if `user` may approve, reject or mark a suggestion acquired.

    UI gating only, exactly like `can_edit_library`. The security boundary
    is `@feature_required("suggestions", "Admin", "Librarian")` on
    `suggestion_review` - the same ceiling as before, now with the menu
    toggle in front of it.
    """

    return can_edit_library(user)


@feature_required("suggestions")
def suggestion_list(request):
    """Every suggestion: what still needs deciding, then what came of the rest.

    Filtered, ordered and paginated in the database. Both people are joined
    in the same query because every row names the suggester and, once
    decided, the reviewer - without that a page of twenty-five would cost
    fifty more queries, which is the whole of the N+1 this page could have.
    """

    status = (request.GET.get("status") or "").strip()

    if status not in dict(AcquisitionSuggestion.STATUS_CHOICES):
        status = ""

    search = (request.GET.get("search") or "").strip()

    rows = acquisitions.filtered(
        acquisitions.all_suggestions(), status=status, search=search
    )

    paginator = Paginator(rows, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "library/suggestion_list.html",
        {
            "suggestions": page,
            "paginator": paginator,
            # Everything except `page`, so the filter and the search
            # survive being paged through.
            "pagination_query": query_with(request, page=None),
            "elided_page_range": list(
                paginator.get_elided_page_range(
                    page.number,
                    on_each_side=1,
                    on_ends=1,
                )
            ),
            "page_ellipsis": Paginator.ELLIPSIS,
            "status": status,
            "search": search,
            "statuses": AcquisitionSuggestion.STATUS_CHOICES,
            # Shaped exactly like the loan list's `status_options`, so this
            # page can use that page's pill markup unchanged rather than a
            # second filter control that looks like nothing else. Same keys,
            # and `query_with` so choosing a status keeps the search and
            # returns to page 1 - which the hand-built links here did by
            # repeating the search parameter in each href.
            "status_options": [
                {
                    "value": value,
                    "label": label,
                    "icon": icon,
                    "active": status == value,
                    "url": "?" + query_with(
                        request, status=value or None, page=None
                    ),
                }
                for value, label, icon in (
                    [("", gettext("All"), "bi-list-ul")]
                    + [
                        (value, label, icon)
                        for (value, label), icon in zip(
                            AcquisitionSuggestion.STATUS_CHOICES,
                            (
                                "bi-hourglass-split",
                                "bi-check2-circle",
                                "bi-x-circle",
                                "bi-bag-check",
                            ),
                        )
                    ]
                )
            ],
            "can_review": can_review_suggestions(request.user),
        },
    )


@feature_required("suggestions")
def suggestion_add(request):
    """Write down a book the library should look for.

    Open to every role. Only the title is required; everything else is
    optional and stored as it was typed, because a suggestion is what
    somebody knew at the time.

    `suggested_by` is `request.user` and there is no field for it in the
    form - `acquisitions.create` takes the user as a keyword and has
    nowhere to put a posted id, so one cannot be honoured by mistake.

    A GET shows the form. A POST that matches something already in the
    catalogue is refused *once*, with the matches named and a "yes, still
    add it" to come back with: imperfect matching must not silently block
    a real suggestion, and it must not be so quiet that the librarian
    orders a second copy of something on the shelf.
    """

    error = None
    matches = []

    form = {
        "title": "",
        "author_name": "",
        "publisher_name": "",
        "isbn": "",
        "notes": "",
    }

    if request.method == "POST":

        form = {
            name: (request.POST.get(name) or "").strip()
            for name in form
        }

        confirmed = request.POST.get("confirm_duplicate") == "1"

        if not form["title"]:

            error = "A title is required — everything else is optional."

        else:

            # Task 4's own comparison, and only when it might refuse: a
            # confirmed submission does not pay for the query again.
            matches = (
                []
                if confirmed
                else suggestion_matches(form["title"], form["author_name"])
            )

            if matches:

                error = (
                    "The catalogue already has %s under this title. Open "
                    "the existing record, or confirm below if this really "
                    "is a different book."
                    % (
                        "a book"
                        if len(matches) == 1
                        else "%d books" % len(matches)
                    )
                )

        if error is None:

            suggestion = acquisitions.create(
                user=request.user,
                title=form["title"],
                author_name=form["author_name"],
                publisher_name=form["publisher_name"],
                isbn=form["isbn"],
                notes=form["notes"],
            )

            create_activity_log(
                user=request.user,
                action="CREATE",
                entity_type="AcquisitionSuggestion",
                entity_id=suggestion.id,
                description="Suggested for acquisition: %s" % suggestion.title,
            )

            # The people who may decide about it, and not the person who
            # wrote it. Task 17's own mechanism, keyed to this suggestion,
            # so it is announced once.
            notifications.announce_suggestion(
                suggestion, submitted_by=request.user
            )

            messages.success(
                request,
                gettext("%s has been suggested.") % suggestion.title,
            )

            return redirect("suggestion_detail", suggestion_id=suggestion.id)

    return render(
        request,
        "library/suggestion_add.html",
        {
            "form": form,
            "error": error,
            "matches": matches,
        },
    )


@feature_required("suggestions")
def suggestion_detail(request, suggestion_id):
    """One suggestion: what was asked for, and what was decided.

    Read-only. Every action on it is a POST elsewhere, so nothing here
    changes anything and a refresh costs one query for the row.
    """

    suggestion = get_object_or_404(
        AcquisitionSuggestion.objects.select_related(
            "suggested_by", "reviewed_by"
        ),
        id=suggestion_id,
    )

    return render(
        request,
        "library/suggestion_detail.html",
        {
            "suggestion": suggestion,
            # Two separate questions, and they are not the same line.
            # Deciding about a suggestion is Admin/Librarian; adding the
            # book is whatever `book_add` already allows, which this does
            # not widen.
            "can_review": can_review_suggestions(request.user),
            "can_catalogue": can_edit_library(request.user),
            "statuses": AcquisitionSuggestion.STATUS_CHOICES,
        },
    )
