from django.test import TestCase
from django.urls import reverse

from .helpers import make_user


class DashboardStructureTests(TestCase):
    def setUp(self):
        make_user(username="dashboard_admin", password="pass12345", role="Admin")
        self.client.login(username="dashboard_admin", password="pass12345")

    def test_dashboard_is_task_first_and_keeps_navigation_to_major_workflows(self):
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()

        self.assertLess(body.index("Today's Work"), body.index("Collection Snapshot"))
        self.assertLess(body.index("Collection Snapshot"), body.index("Recent Circulation"))
        self.assertLess(body.index("Recent Circulation"), body.index("Recent Activity"))

        for name in (
            "circulation_issue",
            "circulation_return_lookup",
            "book_list",
            "book_copy_list",
            "borrower_list",
            "activity_log_list",
        ):
            with self.subTest(route=name):
                self.assertIn(reverse(name), body)

    def test_dashboard_does_not_duplicate_the_circulation_card_as_a_statistic(self):
        response = self.client.get(reverse("dashboard"))
        body = response.content.decode()

        # Circulation remains available through the dedicated overview action,
        # while the main KPI grid is reserved for actual collection metrics.
        self.assertIn("Circulation Overview", body)
        self.assertNotIn("Issue / Return", body)
