"""No page offers a link the person looking at it cannot open.

The menu toggles in /library/permissions/ hide sidebar entries, and every
gated view refuses the URL as well - that part was already covered. What was
not is the space between them: a page that keeps linking to a feature after
it has been switched off, which is how a librarian ends up at a 403 by
clicking rather than by typing. The dashboard offered seven such links.

These follow every link a page actually renders, so they keep working as
cards and shortcuts are added.
"""

import re

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from library.models import ActivityLog, RoleFeature
from library.tests.helpers import make_user, make_copy, make_loan


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class OfferedLinksTests(TestCase):

    def setUp(self):
        self.assistant = make_user(
            username="asst_u", password="pass12345", role="Assistant"
        )
        copy = make_copy()
        make_loan(copy=copy)
        cache.clear()

    def switch_off(self, *keys):
        for key in keys:
            RoleFeature.objects.create(
                role="Assistant",
                feature_key=key,
                allowed=False,
                updated_by=None,
                updated_at=timezone.now(),
            )
        cache.clear()

    def links_on(self, url):
        """Every distinct in-app link the page at `url` renders."""

        body = self.client.get(url).content.decode(errors="replace")

        return sorted(set(re.findall(r'href="(/library/[^"#?]*)', body)))

    def refused(self, url):
        """The links on `url` that answer 4xx when followed."""

        return [
            (href, self.client.get(href).status_code)
            for href in self.links_on(url)
            if self.client.get(href).status_code >= 400
        ]

    def test_the_dashboard_offers_nothing_a_switched_off_role_cannot_open(self):
        self.switch_off(
            "copies", "authors", "categories", "publishers",
            "borrowers", "loans.active",
        )
        self.client.login(username="asst_u", password="pass12345")

        self.assertEqual(self.refused(reverse("dashboard")), [])

    def test_the_dashboard_still_offers_what_is_left_on(self):
        # The counterpart: the guards must hide what is off, not everything.
        self.switch_off("copies", "borrowers")
        self.client.login(username="asst_u", password="pass12345")

        links = self.links_on(reverse("dashboard"))

        self.assertIn(reverse("book_list"), links)
        self.assertNotIn(reverse("book_copy_list"), links)
        self.assertNotIn(reverse("borrower_list"), links)

    def test_overdue_is_not_offered_without_the_page_it_filters(self):
        # `loan_list` is gated on loans.active and nothing is keyed to
        # loans.overdue, so Overdue alone must not appear.
        self.switch_off("loans.active")
        self.client.login(username="asst_u", password="pass12345")

        links = self.links_on(reverse("dashboard"))

        self.assertNotIn(reverse("loan_list"), links)


@override_settings(CACHES=LOCMEM)
class ActivityOnTheDashboardTests(TestCase):
    """The recent-activity card follows the Activity Log toggle."""

    DESCRIPTION = "a distinctive log line"

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        make_user(username="asst_u", password="pass12345", role="Assistant")
        copy = make_copy()
        ActivityLog.objects.create(
            user=None,
            action="UPDATE",
            entity_type="Book",
            entity_id=copy.volume.book_id,
            description=self.DESCRIPTION,
            created_at=timezone.now(),
        )
        cache.clear()

    def test_a_role_the_log_is_switched_off_for_does_not_see_it(self):
        RoleFeature.objects.create(
            role="Assistant",
            feature_key="activity_log",
            allowed=False,
            updated_by=None,
            updated_at=timezone.now(),
        )
        cache.clear()
        self.client.login(username="asst_u", password="pass12345")

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(list(response.context["recent_logs"]), [])
        self.assertNotContains(response, self.DESCRIPTION)

    def test_a_role_that_holds_it_still_does(self):
        self.client.login(username="admin_u", password="pass12345")

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(len(response.context["recent_logs"]), 1)
        self.assertContains(response, self.DESCRIPTION)
