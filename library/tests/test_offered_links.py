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

    # Following every link means visiting /library/logout/ too, which ends
    # the session and turns every later link into a redirect to the sign-in
    # page - a 302, not a 4xx, so a refusal would go unnoticed. Sign-out is
    # skipped, and the session is re-established before each follow so one
    # link cannot mask the next.
    NOT_FOLLOWED = ("/library/logout/",)

    def links_on(self, url):
        """Every distinct in-app link the page at `url` renders."""

        self.sign_in()
        body = self.client.get(url).content.decode(errors="replace")
        found = sorted(set(re.findall(r'href="(/library/[^"#?]*)', body)))

        return [h for h in found if h not in self.NOT_FOLLOWED]

    def sign_in(self):
        self.client.force_login(self.assistant)

    def refused(self, url):
        """The links on `url` that do not open for the person shown them."""

        out = []

        for href in self.links_on(url):
            self.sign_in()
            response = self.client.get(href)

            # A redirect to the sign-in page means the session was lost
            # rather than the link refused; that would hide a real refusal,
            # so it is reported rather than skipped.
            if response.status_code >= 400 or "/login/" in response.headers.get(
                "Location", ""
            ):
                out.append((href, response.status_code))

        return out

    def test_the_dashboard_offers_nothing_a_switched_off_role_cannot_open(self):
        self.switch_off(
            "copies", "authors", "categories", "publishers",
            "borrowers", "loans.active",
        )
        self.assertEqual(self.refused(reverse("dashboard")), [])

    def test_the_dashboard_still_offers_what_is_left_on(self):
        # The counterpart: the guards must hide what is off, not everything.
        self.switch_off("copies", "borrowers")
        links = self.links_on(reverse("dashboard"))

        self.assertIn(reverse("book_list"), links)
        self.assertNotIn(reverse("book_copy_list"), links)
        self.assertNotIn(reverse("borrower_list"), links)

    def test_overdue_is_not_offered_without_the_page_it_filters(self):
        # `loan_list` is gated on loans.active and nothing is keyed to
        # loans.overdue, so Overdue alone must not appear.
        self.switch_off("loans.active")
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

NAVIGATION = (
    "library_home", "circulation_issue", "circulation_return_lookup",
    "reservation_list", "loan_list", "book_list", "suggestion_list",
    "author_list", "category_list", "publisher_list", "borrower_list",
    "location_list", "book_copy_list", "shelf_list",
    "inventory_session_list", "user_list", "branding_settings",
    "permissions_matrix", "analytics", "reports_home",
    "activity_log_list", "notification_list", "profile",
)


@override_settings(CACHES=LOCMEM)
class EveryNavigationPageTests(TestCase):
    """No sidebar destination offers an Assistant a control that refuses.

    Assistant is the role with the least, so it is where a control shown to
    everybody shows up as a refusal. The Books page offered five - Add,
    Import, Export and per-row Edit and Delete - while its own comment noted
    that all of them are Admin/Librarian only.

    Pages the role cannot open at all are skipped: a 403 for Stock Check or
    Users is the boundary working, not a broken link.
    """

    NOT_FOLLOWED = ("/library/logout/",)

    @classmethod
    def setUpTestData(cls):
        cls.assistant = make_user(
            username="sweep_asst", password="pass12345", role="Assistant"
        )
        copy = make_copy()
        make_loan(copy=copy)

    def setUp(self):
        cache.clear()

    def test_no_navigation_page_offers_a_control_that_refuses(self):
        offenders = {}

        for name in NAVIGATION:
            url = reverse(name)
            self.client.force_login(self.assistant)
            page = self.client.get(url)

            if page.status_code != 200:
                continue

            body = page.content.decode(errors="replace")
            refused = []

            for href in sorted(set(re.findall(r'href="(/library/[^"#?]*)', body))):
                if href in self.NOT_FOLLOWED:
                    continue

                # Signed in again each time: following every link means
                # visiting sign-out too, and a lost session turns later
                # links into redirects that would hide a real refusal.
                self.client.force_login(self.assistant)
                followed = self.client.get(href)

                if followed.status_code >= 400 or "/login/" in followed.headers.get(
                    "Location", ""
                ):
                    refused.append((href, followed.status_code))

            if refused:
                offenders[name] = refused

        self.assertEqual(offenders, {})

@override_settings(CACHES=LOCMEM)
class BookListControlsTests(TestCase):
    """The Books page hides its editing controls from the role that lacks them.

    Gating them is only half the requirement: the first attempt read
    `can_edit` in the templates while the view never put it in the context,
    so the controls vanished for everybody, Admin included, and the sweep
    reported a clean page because there was nothing left to refuse. Both
    directions are asserted here for that reason.
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user(username="bl_admin", password="p", role="Admin")
        cls.assistant = make_user(username="bl_asst", password="p", role="Assistant")
        cls.copy = make_copy()

    def setUp(self):
        cache.clear()

    def body_for(self, user):
        self.client.force_login(user)

        return self.client.get(reverse("book_list")).content.decode(errors="replace")

    def test_an_admin_is_offered_add_import_export_and_the_row_actions(self):
        body = self.body_for(self.admin)
        book = self.copy.volume.book

        self.assertIn(reverse("book_add"), body)
        self.assertIn(reverse("book_import"), body)
        self.assertIn(reverse("book_export"), body)
        self.assertIn(reverse("book_edit", args=[book.id]), body)
        self.assertIn(reverse("book_delete", args=[book.id]), body)

    def test_an_assistant_is_offered_none_of_them(self):
        body = self.body_for(self.assistant)
        book = self.copy.volume.book

        self.assertNotIn(reverse("book_add"), body)
        self.assertNotIn(reverse("book_import"), body)
        self.assertNotIn(reverse("book_export"), body)
        self.assertNotIn(reverse("book_edit", args=[book.id]), body)
        self.assertNotIn(reverse("book_delete", args=[book.id]), body)

    def test_an_assistant_can_still_read_the_list(self):
        body = self.body_for(self.assistant)

        self.assertIn(self.copy.volume.book.title, body)
        self.assertIn("data-book-row", body)
