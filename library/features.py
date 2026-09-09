"""Which roles may reach which part of the library, and who decides.

Until now that answer was in the code twice: fifty-four `role_required`
decorators and a handful of `{% if request.user.role == "Admin" %}` in the
sidebar. Changing a menu meant a deploy.

Now the code sets a **ceiling** and the database toggles underneath it.

  * `ceiling` - the roles that may *ever* hold a feature. Fixed here, in
    code. No row in `role_features` can grant anything outside it, so a
    mistaken toggle cannot become a privilege escalation.
  * `default` - who holds it when nothing is stored. These are exactly the
    roles that hold it today, so an installation with an empty
    `role_features` table behaves precisely as it did before this file
    existed.
  * `locked` - never switchable off, whatever the table says. The dashboard
    is the only one: a signed-in person with no dashboard has nowhere to
    land.

`role_features` holds **overrides only**. No row for a (role, feature) pair
means "use the default", which is why the table starts empty and why
turning something back on is a delete rather than a second kind of row.

SuperAdmin is outside all of it. It is the developer's account, it holds
every feature unconditionally, and it is the only role that can change what
an Admin sees.
"""

from django.core.cache import cache


SUPER_ADMIN = "SuperAdmin"
ADMIN = "Admin"
LIBRARIAN = "Librarian"
ASSISTANT = "Assistant"

MANAGED_ROLES = (ADMIN, LIBRARIAN, ASSISTANT)
EVERYONE = (ADMIN, LIBRARIAN, ASSISTANT)
ADMIN_ONLY = (ADMIN,)


CACHE_KEY = "role_features"
CACHE_TIMEOUT = 300


class Feature:
    """One switchable part of the library."""

    __slots__ = ("key", "label", "section", "ceiling", "default", "locked",
                 "menu_only")

    def __init__(self, key, label, section, ceiling, default=None,
                 locked=False, menu_only=False):
        self.key = key
        self.label = label
        self.section = section
        self.ceiling = tuple(ceiling)
        self.default = tuple(default if default is not None else ceiling)
        self.locked = locked

        # `menu_only` says this key decides whether a menu entry is offered
        # and nothing more - no view carries `@feature_required` for it,
        # because the rows behind the entry are reachable another way. It
        # exists so that "every feature gates a view" can be a test rather
        # than an assumption, with the one exception named here instead of
        # discovered later. Do not add another without reading the note on
        # `loans.overdue` below.
        self.menu_only = menu_only

        assert set(self.default) <= set(self.ceiling), key


FEATURES = (
    Feature("dashboard", "Dashboard", "", EVERYONE, locked=True),

    Feature("circulation.issue", "Issue Books", "CIRCULATION", EVERYONE),
    Feature("circulation.return", "Return Books", "CIRCULATION", EVERYONE),
    Feature("reservations", "Reservations", "CIRCULATION", EVERYONE),
    Feature("loans.active", "Active Loans", "CIRCULATION", EVERYONE),
    # Menu-only, and honestly so. "Overdue" links to the loans list with a
    # status filter, and an overdue loan *is* an active loan: anyone with
    # `loans.active` reaches the same rows by sorting that list by due
    # date. Gating the filtered URL as well would be theatre - it would
    # refuse a URL while leaving the data one click away - so this key
    # decides whether the entry is offered and nothing more. The sidebar
    # requires both this and `loans.active` before showing it.
    #
    # If it should become a real boundary, the loans list has to stop
    # showing overdue rows to a role without it, which is a different and
    # larger change than a decorator.
    Feature("loans.overdue", "Overdue", "CIRCULATION", EVERYONE,
            menu_only=True),

    Feature("books", "Books", "CATALOG", EVERYONE),
    Feature("suggestions", "Suggestions", "CATALOG", EVERYONE),
    Feature("authors", "Authors", "CATALOG", EVERYONE),
    Feature("categories", "Categories", "CATALOG", EVERYONE),
    Feature("publishers", "Publishers", "CATALOG", EVERYONE),

    Feature("borrowers", "Borrowers", "PEOPLE", EVERYONE),

    Feature("locations", "Locations", "INVENTORY", EVERYONE),
    Feature("copies", "Book Copies", "INVENTORY", EVERYONE),
    Feature("shelves", "Shelves", "INVENTORY", EVERYONE),
    Feature("inventory.sessions", "Stock Check", "INVENTORY", (ADMIN, LIBRARIAN)),

    Feature("users", "Users", "ADMINISTRATION", ADMIN_ONLY),
    Feature("branding", "Branding", "ADMINISTRATION", ADMIN_ONLY),
    Feature("permissions", "Menu Permissions", "ADMINISTRATION", ADMIN_ONLY),
    Feature("analytics", "Analytics", "ADMINISTRATION", (ADMIN, LIBRARIAN)),
    Feature("reports", "Reports", "ADMINISTRATION", EVERYONE),
    Feature("activity_log", "Activity Log", "ADMINISTRATION", EVERYONE),
)

BY_KEY = {feature.key: feature for feature in FEATURES}
SECTIONS = tuple(dict.fromkeys(f.section for f in FEATURES if f.section))


def overrides():
    """Every stored override, as {(role, key): allowed}."""

    stored = cache.get(CACHE_KEY)

    if stored is not None:
        return stored

    from .models import RoleFeature

    stored = {
        (row.role, row.feature_key): row.allowed
        for row in RoleFeature.objects.all()
    }

    cache.set(CACHE_KEY, stored, timeout=CACHE_TIMEOUT)

    return stored


def overrides_for(request):
    """`overrides()`, remembered for the life of one request."""

    stored = getattr(request, "_role_features", None)

    if stored is None:
        stored = overrides()
        request._role_features = stored

    return stored


def clear_cache():
    """Forget the stored overrides. Called whenever the matrix is saved."""

    cache.delete(CACHE_KEY)


def role_has(role, key, stored=None):
    """Whether `role` holds feature `key` right now."""

    feature = BY_KEY.get(key)

    if feature is None:
        return False

    if role == SUPER_ADMIN:
        return True

    if role not in feature.ceiling:
        return False

    if feature.locked:
        return True

    if stored is None:
        stored = overrides()

    return stored.get((role, key), role in feature.default)


def user_has(user, key):
    """Whether the signed-in `user` holds feature `key`."""

    return role_has(getattr(user, "role", None), key)


def allowed_features(user, stored=None):
    """Every feature key this user holds, for the sidebar to test against."""

    role = getattr(user, "role", None)

    if role is None:
        return frozenset()

    if stored is None:
        stored = overrides()

    return frozenset(
        f.key for f in FEATURES if role_has(role, f.key, stored)
    )


def sections_for(held):
    """The sidebar headings that still have something under them."""

    return frozenset(
        f.section for f in FEATURES if f.section and f.key in held
    )


def matrix_for(editor_role):
    """The rows and columns the permissions page should show `editor_role`."""

    if editor_role == SUPER_ADMIN:
        editable = set(MANAGED_ROLES)
    elif editor_role == ADMIN:
        editable = {LIBRARIAN, ASSISTANT}
    else:
        editable = set()

    stored = overrides()
    sections = []

    for section in SECTIONS:
        rows = []

        for feature in FEATURES:
            if feature.section != section:
                continue

            cells = []

            for role in MANAGED_ROLES:
                cells.append({
                    "role": role,
                    "allowed": role_has(role, feature.key, stored),
                    "available": role in feature.ceiling,
                    "editable": (
                        role in editable
                        and role in feature.ceiling
                        and not feature.locked
                    ),
                    "name": "%s:%s" % (role, feature.key),
                })

            rows.append({
                "key": feature.key,
                "label": feature.label,
                "locked": feature.locked,
                "cells": cells,
            })

        sections.append({"title": section, "rows": rows})

    return sections


def editable_pairs(editor_role):
    """Every (role, key) `editor_role` is allowed to save."""

    if editor_role == SUPER_ADMIN:
        roles = MANAGED_ROLES
    elif editor_role == ADMIN:
        roles = (LIBRARIAN, ASSISTANT)
    else:
        return frozenset()

    return frozenset(
        (role, feature.key)
        for feature in FEATURES
        for role in roles
        if role in feature.ceiling and not feature.locked
    )
