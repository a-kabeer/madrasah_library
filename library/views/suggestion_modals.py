"""Modal-first workflows for acquisition suggestions."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, render, redirect
from django.utils.translation import gettext

from .. import acquisitions, notifications
from ..models import AcquisitionSuggestion
from ..permissions import can_edit_library, feature_required
from .catalog import suggestion_matches
from .common import create_activity_log, modal_redirect, safe_redirect_target


def _redirect_response(request, fallback="suggestion_list"):
    return modal_redirect(
        request,
        request.build_absolute_uri(safe_redirect_target(request, fallback)),
    )


def _modal(request):
    return (
        request.GET.get("modal") == "1"
        or request.POST.get("modal") == "1"
        or request.headers.get("HX-Request") == "true"
    )


@feature_required("suggestions")
def suggestion_add_modal(request):
    if not _modal(request):
        from .acquisitions import suggestion_add
        return suggestion_add(request)

    form = {
        "title": "",
        "author_name": "",
        "publisher_name": "",
        "isbn": "",
        "notes": "",
    }
    error = None
    matches = []

    if request.method == "POST":
        form = {
            name: (request.POST.get(name) or "").strip()
            for name in form
        }
        confirmed = request.POST.get("confirm_duplicate") == "1"

        if not form["title"]:
            error = "A title is required — everything else is optional."
        else:
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
                    % ("a book" if len(matches) == 1 else "%d books" % len(matches))
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
            notifications.announce_suggestion(
                suggestion, submitted_by=request.user
            )
            messages.success(
                request,
                gettext("%s has been suggested.") % suggestion.title,
            )
            return _redirect_response(request)

    return render(
        request,
        "library/partials/suggestion_form_modal.html",
        {"form": form, "error": error, "matches": matches},
    )


@feature_required("suggestions")
def suggestion_detail_modal(request, suggestion_id):
    if not _modal(request):
        from .acquisitions import suggestion_detail
        return suggestion_detail(request, suggestion_id)

    suggestion = get_object_or_404(
        AcquisitionSuggestion.objects.select_related("suggested_by", "reviewed_by"),
        id=suggestion_id,
    )
    return render(
        request,
        "library/partials/suggestion_detail_modal.html",
        {
            "suggestion": suggestion,
            "can_review": can_edit_library(request.user),
            "can_catalogue": can_edit_library(request.user),
        },
    )


@feature_required("suggestions", "Admin", "Librarian")
def suggestion_review_modal(request, suggestion_id):
    if request.method != "POST":
        return redirect("suggestion_detail", suggestion_id=suggestion_id)

    to_status = (request.POST.get("status") or "").strip()
    if to_status not in dict(AcquisitionSuggestion.STATUS_CHOICES):
        messages.warning(request, gettext("That is not a state a suggestion can be in."))
        return _redirect_response(request)

    moved = acquisitions.advance(suggestion_id, to_status, user=request.user)
    if not moved:
        messages.info(
            request,
            gettext("Nothing changed — that suggestion is no longer waiting for this decision."),
        )
        return _redirect_response(request)

    create_activity_log(
        user=request.user,
        action="UPDATE",
        entity_type="AcquisitionSuggestion",
        entity_id=suggestion_id,
        description="Suggestion marked %s" % to_status,
    )
    messages.success(request, gettext("Suggestion marked %s.") % to_status)
    return _redirect_response(request)
