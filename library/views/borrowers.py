"""The people who borrow books.

Borrowers are not users. They do not sign in and they have no password;
they are records the library keeps about who has what.

Listing, reading, adding, editing and deleting a borrower all happen in
the shared dialog and live in `borrower_modals.py`. What stays here is the
one borrower action that is neither a form nor a page: switching a
borrower between active and inactive from the button on their own record.
"""

from django.core.cache import cache
from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse

from ..models import Borrower

from ..permissions import feature_required

from .common import (
    BORROWER_CACHE_KEY,
    DASHBOARD_CACHE_KEY,
    create_activity_log,
    safe_redirect_target,
)


@feature_required("borrowers")
def borrower_toggle_active(request, borrower_id):

    borrower = get_object_or_404(Borrower, id=borrower_id)

    if request.method == "POST":

        borrower.is_active = not borrower.is_active
        borrower.save(update_fields=["is_active"])

        cache.delete(BORROWER_CACHE_KEY)
        cache.delete(DASHBOARD_CACHE_KEY)

        create_activity_log(
            user=request.user,
            action="UPDATE",
            entity_type="Borrower",
            entity_id=borrower.id,
            description=(
                f"{borrower.name} "
                f"{'activated' if borrower.is_active else 'deactivated'}"
            ),
        )

    fallback = reverse("borrower_detail", args=[borrower.id])
    return redirect(safe_redirect_target(request, fallback))
