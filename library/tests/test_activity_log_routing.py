"""An audit-trail entry that goes nowhere is half an answer.

The activity log records `entity_type` with every entry, and
`ACTIVITY_LOG_DETAIL_ROUTES` (plus `ACTIVITY_LOG_LOOKUP_KINDS` for the three
lookup tables) turns that into a link to the record the entry is about.
Seventeen entity types are written across the application, and the maps are
hand-maintained - which is exactly the drift this project has already been
bitten by twice: the log's own filter dropdowns once offered six actions
while the log held ten.

So this compares the maps against what the code actually writes, in both
directions, and requires every exclusion to be named. `AcquisitionSuggestion`
was missing when this was written: suggestions were the one record type whose
log entries could not be followed, and nothing said why.
"""

import pathlib
import re

from django.test import SimpleTestCase
from django.urls import NoReverseMatch, reverse

from library.views.common import (
    ACTIVITY_LOG_DETAIL_ROUTES,
    ACTIVITY_LOG_LOOKUP_KINDS,
)

# Entity types deliberately not linked, each with the reason. Keeping them
# here rather than in a comment is what makes adding a fifth a decision.
UNLINKED = {
    "User": "has no detail page",
    "RoleFeature": "the matrix is the page; a row in it has no URL",
    "OrganizationSettings": "its page is Admin-only, so a link would 403",
    "InventorySession": "Admin/Librarian ceiling, so a link can never work "
                        "for an Assistant",
}

WRITE = re.compile(
    r'create_activity_log\((?:[^()]|\([^()]*\))*?entity_type="(\w+)"', re.S
)


def entity_types_written():
    """Every `entity_type` the application records, read from the source."""

    found = set()

    for path in sorted(pathlib.Path("library").rglob("*.py")):
        if "/tests/" in str(path):
            continue

        found.update(WRITE.findall(path.read_text()))

    return found


class EveryEntityTypeIsAccountedFor(SimpleTestCase):

    def setUp(self):
        self.written = entity_types_written()
        self.handled = (
            set(ACTIVITY_LOG_DETAIL_ROUTES)
            | set(ACTIVITY_LOG_LOOKUP_KINDS)
            | set(UNLINKED)
        )

    def test_the_source_still_writes_the_types_this_test_thinks_it_does(self):
        """A guard on the guard: if the regex stops matching, everything
        below passes vacuously."""

        self.assertGreaterEqual(len(self.written), 15)
        self.assertIn("Book", self.written)
        self.assertIn("Loan", self.written)

    def test_every_recorded_type_is_linked_or_named_as_unlinked(self):
        loose = sorted(self.written - self.handled)

        self.assertEqual(
            loose, [],
            "the log records these and nothing decides where an entry about "
            "one points; add a route, or add it to UNLINKED with the "
            "reason: %s" % loose,
        )

    def test_nothing_is_both_linked_and_declared_unlinked(self):
        contradictions = sorted(
            (set(ACTIVITY_LOG_DETAIL_ROUTES) | set(ACTIVITY_LOG_LOOKUP_KINDS))
            & set(UNLINKED)
        )

        self.assertEqual(contradictions, [])

    def test_nothing_is_routed_that_the_log_never_records(self):
        """A route for a type nothing writes is dead weight, and usually
        means a rename happened on one side only."""

        stale = sorted(
            (set(ACTIVITY_LOG_DETAIL_ROUTES) | set(ACTIVITY_LOG_LOOKUP_KINDS))
            - self.written
        )

        self.assertEqual(stale, [], "routed but never recorded: %s" % stale)

    def test_every_route_name_reverses(self):
        for entity, name in sorted(ACTIVITY_LOG_DETAIL_ROUTES.items()):
            with self.subTest(entity_type=entity):
                try:
                    reverse(name, args=[1])

                except NoReverseMatch as failure:
                    self.fail("%s -> %r: %s" % (entity, name, failure))
