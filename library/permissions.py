from functools import wraps

from django.core.exceptions import PermissionDenied


# Roles allowed to create/modify library catalogue data (books, authors,
# categories, publishers, ...). Kept here so templates and views can ask the
# same question the `role_required` decorators enforce.
LIBRARY_EDITOR_ROLES = ("Admin", "Librarian")


def can_edit_library(user):
    """True if `user` may create catalogue records.

    Used for UI gating only (e.g. whether to offer an inline "add new"
    option). The actual enforcement stays on the views themselves via
    `role_required`, so hiding the control is never the security boundary.
    """

    return getattr(user, "role", None) in LIBRARY_EDITOR_ROLES


def role_required(*allowed_roles):
    """Restrict a view to users whose `role` is one of `allowed_roles`.

    Must run after the auth middleware (request.user is always set by then,
    since LoginRequiredMiddleware already blocks anonymous access globally).
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if request.user.role not in allowed_roles:
                raise PermissionDenied(
                    "Your role does not have permission to perform this action."
                )
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator
