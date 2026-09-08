from functools import wraps

from django.core.exceptions import PermissionDenied

from . import features


# Roles allowed to create/modify library catalogue data (books, authors,
# categories, publishers, ...). Kept here so templates and views can ask the
# same question the `role_required` decorators enforce.
LIBRARY_EDITOR_ROLES = ("Admin", "Librarian")

# Roles allowed to change the organisation's identity — name, logo, colours.
# Deliberately narrower than LIBRARY_EDITOR_ROLES: Librarians manage the
# catalogue, not the institution's branding.
BRANDING_ADMIN_ROLES = ("Admin",)


def can_manage_branding(user):
    """True if `user` may edit organisation branding.

    UI gating only, exactly like `can_edit_library` — the security boundary
    is `@role_required("Admin")` on the view itself.
    """

    return getattr(user, "role", None) in BRANDING_ADMIN_ROLES


def can_edit_library(user):
    """True if `user` may create catalogue records.

    Used for UI gating only (e.g. whether to offer an inline "add new"
    option). The actual enforcement stays on the views themselves via
    `role_required`, so hiding the control is never the security boundary.
    """

    return getattr(user, "role", None) in LIBRARY_EDITOR_ROLES


def passes_ceiling(user, *ceiling):
    """True if `user`'s role is inside a `feature_required` ceiling.

    Exactly the test `feature_required` makes below, SuperAdmin exemption
    included, asked from outside a request so a template can avoid offering
    a control the view would refuse. Stated once here rather than restated
    at each call site, because a copy of this rule that drifts from the
    decorator is a button that lies.

    UI gating only, like `can_edit_library` and `can_manage_branding` - and
    unlike them, it knows about SuperAdmin, who those two predate.
    """

    role = getattr(user, "role", None)

    return role in ceiling or role == features.SUPER_ADMIN


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


def feature_required(feature_key, *ceiling):
    """Restrict a view to a feature that is switched on for the user's role.

    Replaces `role_required` on the views behind a menu entry, and does two
    jobs where that did one:

      * `ceiling` is the same check `role_required` made - the roles that
        may *ever* reach this view. Stated per view, not per feature,
        because a feature is usually one menu entry over several views with
        different answers: everybody may read the book list, only an Admin
        or Librarian may add to it. Leave it out to mean "any signed-in
        role", which is what an undecorated view meant before.

      * the feature toggle, which an Admin (or a SuperAdmin) sets from
        /library/permissions/ without a deploy.

    The order matters and is the whole safety argument: the ceiling is
    checked first and comes from code, so no row in `role_features` can let
    a role past it. Turning a feature *on* for a role the ceiling excludes
    changes nothing.

    Hiding the menu entry is never the boundary - this is. A person who
    types the URL of a feature switched off for them gets the same 403 as
    one who types the URL of a view their role never had.
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            role = getattr(request.user, "role", None)

            if ceiling and role not in ceiling and role != features.SUPER_ADMIN:
                raise PermissionDenied(
                    "Your role does not have permission to perform this action."
                )

            if not features.role_has(
                role, feature_key, features.overrides_for(request)
            ):
                raise PermissionDenied(
                    "This part of the library is switched off for your role."
                )

            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator
