"""The activity log's two filter dropdowns are read back from the log.

That is deliberate - the hand-written lists had drifted, offering six
actions while the log held ten - but it costs a DISTINCT over the whole
table, twice, on every page load, and the activity log is the fastest-
growing table in the database. Measured on 60,000 rows: 11.0 ms and
10.8 ms, both sequential scans, and no index can help a DISTINCT that has
to see every row.

So the pair is cached under ACTIVITY_LOG_CACHE_KEY, which
`create_activity_log` was already deleting on every write - the
invalidation existed before anything was stored under the key. These tests
hold both halves: the values still come from the log, and a new one appears
as soon as the entry that introduced it is recorded.
"""

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from library.views.common import ACTIVITY_LOG_CACHE_KEY, create_activity_log
from library.tests.helpers import make_user

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class ActivityLogFilterOptions(TestCase):

    def setUp(self):
        cache.clear()
        self.admin = make_user(username="opt_admin", password="pass12345",
                               role="Admin")
        self.client.force_login(self.admin)

    def options(self):
        from library.views.dashboard import activity_log_filter_options

        return activity_log_filter_options()

    def test_the_values_come_from_what_is_recorded(self):
        create_activity_log(user=self.admin, action="RESERVE",
                            entity_type="Reservation", entity_id=1,
                            description="held")

        actions, entity_types = self.options()

        self.assertIn("RESERVE", actions)
        self.assertIn("Reservation", entity_types)

    def test_a_second_read_does_not_query_again(self):
        create_activity_log(user=self.admin, action="CREATE",
                            entity_type="Book", entity_id=1, description="x")

        self.options()

        with self.assertNumQueries(0):
            self.options()

    def test_a_new_entry_makes_its_action_available_at_once(self):
        create_activity_log(user=self.admin, action="CREATE",
                            entity_type="Book", entity_id=1, description="x")

        self.assertNotIn("IMPORT", self.options()[0])

        create_activity_log(user=self.admin, action="IMPORT",
                            entity_type="Book", entity_id=2, description="y")

        self.assertIn("IMPORT", self.options()[0])

    def test_writing_an_entry_clears_the_key(self):
        self.options()

        self.assertIsNotNone(cache.get(ACTIVITY_LOG_CACHE_KEY))

        create_activity_log(user=self.admin, action="DELETE",
                            entity_type="Book", entity_id=3, description="z")

        self.assertIsNone(cache.get(ACTIVITY_LOG_CACHE_KEY))

    def test_a_blank_entity_type_is_not_offered(self):
        create_activity_log(user=self.admin, action="LOGIN", entity_type=None,
                            entity_id=None, description="signed in")

        self.assertNotIn(None, self.options()[1])
        self.assertNotIn("", self.options()[1])

    def test_the_page_still_renders_both_dropdowns(self):
        create_activity_log(user=self.admin, action="RENEW",
                            entity_type="Loan", entity_id=4, description="q")

        response = self.client.get(reverse("activity_log_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "RENEW")
