from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from library.models import ActivityLog
from library.views import activity_log_target

from .helpers import make_book, make_category, make_user


def make_log(action="CREATE", entity_type="Book", entity_id=1, description=""):
    return ActivityLog.objects.create(
        user=None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        description=description,
        created_at=timezone.now(),
    )


class ActivityLogTargetTests(TestCase):
    """Which activity entries get a link, and where it points."""

    def test_entities_with_a_detail_page_are_linked(self):
        expected = {
            "Author": "author_detail",
            "Book": "book_detail",
            "BookContent": "book_content_detail",
            "BookCopy": "book_copy_detail",
            "BookVolume": "book_volume_detail",
            "Borrower": "borrower_detail",
            "Category": "category_detail",
            "Loan": "loan_detail",
            "Location": "location_detail",
            "Publisher": "publisher_detail",
            "Shelf": "shelf_detail",
        }

        for entity_type, route in expected.items():
            with self.subTest(entity_type=entity_type):
                log = make_log(entity_type=entity_type, entity_id=7)

                self.assertEqual(
                    activity_log_target(log),
                    reverse(route, args=[7]),
                )

    def test_deletions_are_not_linked(self):
        # The record is gone, so its detail page would only 404.
        log = make_log(action="DELETE", entity_type="Book", entity_id=7)

        self.assertEqual(activity_log_target(log), "")

    def test_entities_without_a_detail_page_are_not_linked(self):
        for entity_type in ("User", "OrganizationSettings", "Mystery"):
            with self.subTest(entity_type=entity_type):
                log = make_log(entity_type=entity_type, entity_id=7)

                self.assertEqual(activity_log_target(log), "")

    def test_missing_entity_details_are_not_linked(self):
        self.assertEqual(activity_log_target(make_log(entity_type="")), "")
        self.assertEqual(activity_log_target(make_log(entity_id=None)), "")


class DashboardRecentActivityTests(TestCase):
    """The whole row should be clickable, matching Recent Loans."""

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def rows(self, response):
        """(tag, href) for each Recent Activity row, in page order."""

        import re

        html = response.content.decode()
        start = html.find("Recent Activity")
        block = html[start:]

        return re.findall(
            r"<(a|div)\s[^>]*?list-group-item[^>]*?>", block, re.S
        )

    def test_linkable_entry_wraps_the_whole_row_in_a_link(self):
        book = make_book()
        make_log(entity_type="Book", entity_id=book.id)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        # The anchor carries the row classes, so the entire row is the target.
        self.assertContains(
            response,
            f'href="{reverse("book_detail", args=[book.id])}"',
        )
        self.assertIn("a", self.rows(response))

    def test_linked_row_uses_the_same_affordance_as_recent_loans(self):
        book = make_book()
        make_log(entity_type="Book", entity_id=book.id)

        response = self.client.get(reverse("dashboard"))
        html = response.content.decode()

        start = html.find("Recent Activity")

        self.assertIn("list-group-item-action", html[start:])

    def test_unlinkable_entry_renders_a_plain_row(self):
        make_log(action="DELETE", entity_type="Book", entity_id=99999)

        response = self.client.get(reverse("dashboard"))

        # No link to the deleted record.
        self.assertNotContains(
            response,
            reverse("book_detail", args=[99999]),
        )

    def test_target_url_is_attached_to_each_entry(self):
        category = make_category()
        make_log(entity_type="Category", entity_id=category.id)
        make_log(action="DELETE", entity_type="Book", entity_id=42)

        response = self.client.get(reverse("dashboard"))

        targets = {
            log.entity_type: log.target_url
            for log in response.context["recent_logs"]
        }

        self.assertEqual(
            targets["Category"],
            reverse("category_detail", args=[category.id]),
        )
        self.assertEqual(targets["Book"], "")

    def test_dashboard_renders_with_no_activity(self):
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No recent activity found.")
