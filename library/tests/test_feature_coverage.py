"""Every switchable feature has to switch something.

`library/features.py` names 22 features and the permissions page offers all
22 as checkboxes. A key that no view is gated on is a checkbox that does
nothing - and `permissions.py` states the opposite in as many words: "Hiding
the menu entry is never the boundary - this is. A person who types the URL
of a feature switched off for them gets the same 403 as one who types the URL
of a view their role never had."

One key was exactly that. `loans.overdue` gated a sidebar entry and no view,
so switching it off hid the link and left `/library/loans/?status=overdue`
open. It is declared `menu_only=True` now, with the reasoning in
features.py - an overdue loan is an active loan, so gating the filtered URL
would refuse a link while leaving the data one sort away - and that flag is
what these tests make an exception rather than a hole.

The other direction matters too: a view gated on a key that is not in
FEATURES can never be switched on, because the permissions page has no
checkbox for it and `role_has` falls back to a default the key does not
have.
"""

import re
import pathlib

from django.test import SimpleTestCase
from django.urls import get_resolver

from library import features

BASE_TEMPLATE = pathlib.Path("library/templates/library/base.html")


def gated_keys():
    """Every feature key some routed view is actually gated on."""

    found = set()

    def walk(patterns):
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                walk(pattern.url_patterns)
                continue

            key = getattr(pattern.callback, "feature_key", None)

            if key:
                found.add(key)

    walk(get_resolver().url_patterns)

    return found


class EveryFeatureSwitchesSomething(SimpleTestCase):

    def setUp(self):
        self.gated = gated_keys()
        self.sidebar = BASE_TEMPLATE.read_text()

    def test_every_feature_gates_a_view_unless_declared_menu_only(self):
        ungated = sorted(
            feature.key for feature in features.FEATURES
            if feature.key not in self.gated and not feature.menu_only
        )

        self.assertEqual(
            ungated, [],
            "these features offer a checkbox that changes nothing but the "
            "menu; either gate a view with @feature_required or declare "
            "menu_only=True with the reason: %s" % ungated,
        )

    def test_only_the_one_key_is_menu_only(self):
        """Named, so that a second one has to be a decision rather than a
        convenience."""

        self.assertEqual(
            [feature.key for feature in features.FEATURES
             if feature.menu_only],
            ["loans.overdue"],
        )

    def test_no_view_is_gated_on_a_key_that_does_not_exist(self):
        unknown = sorted(self.gated - set(features.BY_KEY))

        self.assertEqual(
            unknown, [],
            "these keys gate a view and are not in FEATURES, so the "
            "permissions page cannot switch them and role_has has no "
            "default for them: %s" % unknown,
        )

    def test_every_feature_is_named_in_the_sidebar(self):
        """A feature nobody can see the effect of is a feature nobody will
        use. Every key should decide some entry in the navigation."""

        absent = sorted(
            feature.key for feature in features.FEATURES
            if not re.search(r'["\']%s["\']' % re.escape(feature.key),
                             self.sidebar)
        )

        self.assertEqual(absent, [], "not referenced in base.html: %s" % absent)

    def test_a_ceiling_never_excludes_its_own_default(self):
        """`Feature.__init__` asserts this, and an assert is stripped under
        `python -O`."""

        for feature in features.FEATURES:
            with self.subTest(feature=feature.key):
                self.assertLessEqual(set(feature.default),
                                     set(feature.ceiling))

    def test_the_administration_features_are_admin_only(self):
        """The three that decide who can reach what must never be reachable
        by a role that could then widen its own reach."""

        for key in ("users", "branding", "permissions"):
            with self.subTest(feature=key):
                self.assertEqual(features.BY_KEY[key].ceiling,
                                 features.ADMIN_ONLY)

    def test_the_dashboard_cannot_be_switched_off(self):
        self.assertTrue(features.BY_KEY["dashboard"].locked)
