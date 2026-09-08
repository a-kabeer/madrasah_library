"""Which roles may reach which part of the library, and who decides."""

# The four roles, widest first. SuperAdmin is deliberately absent from every
# `ceiling` below: it is answered before the table is consulted at all.
SUPER_ADMIN = "SuperAdmin"
ADMIN = "Admin"
LIBRARIAN = "Librarian"
ASSISTANT = "Assistant"
MANAGED_ROLES = (ADMIN, LIBRARIAN, ASSISTANT)
EVERYONE = (ADMIN, LIBRARIAN, ASSISTANT)
ADMIN_ONLY = (ADMIN,)


class Feature:
    """One switchable part of the library."""

    __slots__ = ("key", "label", "section", "ceiling", "default", "locked")

    def __init__(self, key, label, section, ceiling, default=None, locked=False):
        self.key = key
        self.label = label
        self.section = section
        self.ceiling = tuple(ceiling)
        self.default = tuple(default if default is not None else ceiling)
        self.locked = locked
        assert set(self.default) <= set(self.ceiling), key


FEATURES = (
    Feature("dashboard", "Dashboard", "", EVERYONE, locked=True),
    Feature("circulation.issue", "Issue Books", "CIRCULATION", EVERYONE),
    Feature("circulation.return", "Return Books", "CIRCULATION", EVERYONE),
    Feature("reservations", "Reservations", "CIRCULATION", EVERYONE),
    Feature("loans.active", "Active Loans", "CIRCULATION", EVERYONE),
    Feature("loans.overdue", "Overdue", "CIRCULATION", EVERYONE),
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
    Feature("branding", "Settings / Branding", "ADMINISTRATION", ADMIN_ONLY),
    Feature("permissions", "Menu Permissions", "ADMINISTRATION", ADMIN_ONLY),
    Feature("analytics", "Analytics", "ADMINISTRATION", (ADMIN, LIBRARIAN)),
    Feature("reports", "Reports", "ADMINISTRATION", EVERYONE),
    Feature("activity_log", "Activity Log", "ADMINISTRATION", EVERYONE),
)

BY_KEY = {feature.key: feature for feature in FEATURES}
SECTIONS = tuple(dict.fromkeys(f.section for f in FEATURES if f.section))


def overrides():
    """Return every stored override from the database.

    This intentionally does not use Django's process-local LocMemCache.
    Gunicorn workers are separate processes, so a worker-local cache can
    make workers disagree for minutes after an Admin changes permissions.
    """
    from .models import RoleFeature

    return {
        (row.role, row.feature_key): row.allowed
        for row in RoleFeature.objects.all()
    }


def overrides_for(request):
    """Memoize the database snapshot only for the current request."""
    stored = getattr(request, "_role_features", None)
    if stored is None:
        stored = overrides()
        request._role_features = stored
    return stored


def clear_cache():
    """Compatibility no-op; role features are no longer process-cached."""
    return None


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
    """Every feature key this user holds, for the sidebar."""
    role = getattr(user, "role", None)
    if role is None:
        return frozenset()
    if stored is None:
        stored = overrides()
    return frozenset(f.key for f in FEATURES if role_has(role, f.key, stored))


def sections_for(held):
    """The sidebar headings that still have something under them."""
    return frozenset(f.section for f in FEATURES if f.section and f.key in held)


def matrix_for(editor_role):
    """The rows and columns the permissions page should show."""
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
