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


# The four roles, widest first. SuperAdmin is deliberately absent from every
# `ceiling` below: it is answered before the table is consulted at all.
SUPER_ADMIN = "SuperAdmin"
ADMIN = "Admin"
LIBRARIAN = "Librarian"
ASSISTANT = "Assistant"

# The roles a permissions matrix can show columns for, in order.
MANAGED_ROLES = (ADMIN, LIBRARIAN, ASSISTANT)

# Everyone who is not a SuperAdmin.
EVERYONE = (ADMIN, LIBRARIAN, ASSISTANT)

# Only an Admin may ever hold these, whatever the table says. Users,
# branding and the permissions matrix itself are how an installation is
# governed; handing any of them to a Librarian by toggle would be a way
# around the roles rather than a use of them.
ADMIN_ONLY = (ADMIN,)


CACHE_KEY = "role_features"
CACHE_TIMEOUT = 300


class Feature:
    """One switchable part of the library.

    `section` is the sidebar heading it appears under, which is also how the
    permissions matrix groups its rows - so the page a person is
    configuring and the menu they are configuring it for read the same way.
    """

    __slots__ = ("key", "label", "section", "ceiling", "default", "locked")

    def __init__(self, key, label, section, ceiling, default=None, locked=False):
        self.key = key
        self.label = label
        self.section = section
        self.ceiling = tuple(ceiling)
        # Unstated means "everyone the ceiling allows", which is the common
        # case: most of the library is visible to all three roles today.
        self.default = tuple(default if default is not None else ceiling)
        self.locked = locked

        assert set(self.default) <= set(self.ceiling), key


# The sidebar, one entry per line, in the order it is drawn. Two menu
# entries point at the same view (Active Loans and Overdue are one list with
# a filter), so `loans.overdue` hides a link without gating a URL - there is
# no separate URL to gate. That is stated here rather than discovered later.
FEATURES = (
    Feature("dashboard", "Dashboard", "", EVERYONE, locked=True),

    Feature("circulation.issue", "Issue Books", "CIRCULATION", EVERYONE),
    # Everyone, because everyone can reach it today: the sidebar hides this
    # from an Assistant but the view never did, and
    # test_circulation_workflow asserts all three roles get 200. The menu
    # now tells the truth about that; hiding it again is a toggle away.
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
    # Admin/Librarian is the ceiling, not just the default: every stock-check
    # view carries that role check in code, so offering an Assistant column
    # here would be offering a switch that does nothing.
    Feature("inventory.sessions", "Stock Check", "INVENTORY",
            (ADMIN, LIBRARIAN)),

    Feature("users", "Users", "ADMINISTRATION", ADMIN_ONLY),
    Feature("branding", "Settings / Branding", "ADMINISTRATION", ADMIN_ONLY),
    Feature("permissions", "Menu Permissions", "ADMINISTRATION", ADMIN_ONLY),
    # Same as stock check: the view itself is Admin/Librarian only.
    Feature("analytics", "Analytics", "ADMINISTRATION", (ADMIN, LIBRARIAN)),
    # Reports, unlike analytics, carry no role check at all - the sidebar
    # hides them from an Assistant but the URLs have always answered. Same
    # decision as Return Books above.
    Feature("reports", "Reports", "ADMINISTRATION", EVERYONE),
    Feature("activity_log", "Activity Log", "ADMINISTRATION", EVERYONE),
)

BY_KEY = {feature.key: feature for feature in FEATURES}

# The sidebar headings in order, for the matrix page.
SECTIONS = tuple(dict.fromkeys(f.section for f in FEATURES if f.section))


def overrides():
    """Every stored override, as {(role, key): allowed}.

    One query, cached for five minutes - the same shape and the same reason
    as `get_branding()`. The sidebar asks this on every request, and the
    answer changes when somebody saves the matrix, which clears the cache.
    """

    stored = cache.get(CACHE_KEY)

    if stored is not None:
        return stored

    # Imported here, not at module scope: models.py is loaded through the
    # app registry and this module is imported by permissions.py, which
    # views import at import time.
    from .models import RoleFeature

    stored = {
        (row.role, row.feature_key): row.allowed
        for row in RoleFeature.objects.all()
    }

    cache.set(CACHE_KEY, stored, timeout=CACHE_TIMEOUT)

    return stored


def overrides_for(request):
    """`overrides()`, remembered for the life of one request.

    A request asks twice: once when the decorator checks the view, and once
    when the sidebar is drawn. The five-minute cache makes the second free
    in production, but not on a cold cache and not at all where the cache
    backend is a no-op - so the answer is kept on the request as well.
    """

    stored = getattr(request, "_role_features", None)

    if stored is None:
        stored = overrides()
        request._role_features = stored

    return stored


def clear_cache():
    """Forget the stored overrides. Called whenever the matrix is saved."""

    cache.delete(CACHE_KEY)


def role_has(role, key, stored=None):
    """Whether `role` holds feature `key` right now.

    The order is what makes the ceiling a ceiling: SuperAdmin first, then
    the locked list, then the code's ceiling, and only then the table. A row
    saying an Assistant may manage users is read, found to be outside the
    ceiling, and ignored.
    """

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
    """Every feature key this user holds, for the sidebar to test against.

    A set, so the template can say `{% if "users" in allowed_features %}`,
    and built from one read of the overrides rather than a query per entry.
    """

    role = getattr(user, "role", None)

    if role is None:
        return frozenset()

    if stored is None:
        stored = overrides()

    return frozenset(
        f.key for f in FEATURES if role_has(role, f.key, stored)
    )


def sections_for(held):
    """The sidebar headings that still have something under them.

    A heading over nothing reads as a bug, so a section disappears when its
    last entry is switched off. Worked out here rather than in the template
    because only this module knows which entries belong to which heading.

    Takes the feature set rather than the user so the caller can read the
    overrides once and answer both questions from it - the sidebar needs
    both on every request, and asking twice would be two reads on any
    installation whose cache is a no-op.
    """

    return frozenset(
        f.section for f in FEATURES if f.section and f.key in held
    )


def matrix_for(editor_role):
    """The rows and columns the permissions page should show `editor_role`.

    A SuperAdmin configures all three managed roles. An Admin configures the
    two below them and sees their own column read-only - nobody edits the
    permissions of the role they are signed in as, which is what stops an
    Admin from locking themselves out of the page that would let them back
    in.
    """

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
                    # Outside the ceiling there is nothing to switch: the
                    # cell shows a dash rather than an unticked box, because
                    # an unticked box invites a click that would do nothing.
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
    """Every (role, key) `editor_role` is allowed to save.

    The matrix page posts a form; this is what the view checks each field
    against, so a hand-crafted POST cannot set a pair the page never
    offered.
    """

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
