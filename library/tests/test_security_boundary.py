"""The security boundary, checked by exercising it rather than reading it.

`LoginRequiredMiddleware` is on globally and every gated view carries a
decorator, but neither of those is evidence on its own. These walk the whole
route table unauthenticated, try each role's own escalation, and check that
signing out, another person's account and a password change behave as the
boundary says they do.
"""

from django.contrib.auth import authenticate
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import get_resolver, reverse

from library.models import User
from library.tests.helpers import make_user

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# The only routes allowed to answer an anonymous visitor with content: the
# sign-in page, the public catalogue, and Django's own language switch.
PUBLIC_URLS = {"/library/login/", "/catalog/", "/i18n/setlang/"}


def every_reversible_url():
    """One URL per named route, with a stand-in id where one is needed."""

    for pattern in get_resolver().url_patterns:
        for route in getattr(pattern, "url_patterns", [pattern]):
            name = getattr(route, "name", None)

            if not name:
                continue

            for args in ((), (1,)):
                try:
                    yield name, reverse(name, args=args)

                except Exception:
                    continue

                break


@override_settings(CACHES=LOCMEM)
class AnonymousAccess(TestCase):
    """Every route, unauthenticated.

    Anything not public must redirect to sign-in or refuse outright.
    """

    def test_every_route_refuses_an_anonymous_visitor(self):
        answered = []

        for name, url in every_reversible_url():
            if url.startswith("/catalog/") or url in PUBLIC_URLS:
                continue

            response = self.client.get(url)
            location = response.headers.get("Location", "")

            refused = (
                response.status_code in (301, 302) and "/login/" in location
            ) or response.status_code in (403, 405)

            if not refused:
                answered.append(
                    "%s (%s) -> %s %s"
                    % (name, url, response.status_code, location)
                )

        self.assertEqual(
            answered,
            [],
            "these routes answered an anonymous visitor: %s" % answered,
        )


@override_settings(CACHES=LOCMEM)
class RoleEscalation(TestCase):
    """Nobody can widen their own reach, whatever they post."""

    def setUp(self):
        self.assistant = make_user(username="esc_asst", password="pass12345",
                                   role="Assistant")
        self.librarian = make_user(username="esc_lib", password="pass12345",
                                   role="Librarian")
        self.admin = make_user(username="esc_admin", password="pass12345",
                               role="Admin")

        cache.clear()

    def promote(self, actor, target, role):
        self.client.force_login(actor)

        return self.client.post(
            reverse("user_edit", args=[target.id]),
            {
                "username": target.username,
                "full_name": "Changed",
                "role": role,
                "is_active": "True",
            },
        )

    def test_an_assistant_cannot_promote_themselves(self):
        self.promote(self.assistant, self.assistant, "Admin")

        self.assertEqual(User.objects.get(id=self.assistant.id).role,
                         "Assistant")

    def test_a_librarian_cannot_promote_themselves(self):
        self.promote(self.librarian, self.librarian, "Admin")

        self.assertEqual(User.objects.get(id=self.librarian.id).role,
                         "Librarian")

    def test_an_admin_cannot_make_a_super_admin(self):
        self.promote(self.admin, self.assistant, "SuperAdmin")

        self.assertNotEqual(User.objects.get(id=self.assistant.id).role,
                            "SuperAdmin")

    def test_an_assistant_cannot_grant_themselves_a_feature(self):
        self.client.force_login(self.assistant)

        response = self.client.post(reverse("permissions_matrix"),
                                    {"Assistant:users": "on"})

        self.assertGreaterEqual(response.status_code, 400)


@override_settings(CACHES=LOCMEM)
class SessionAndObjectAccess(TestCase):

    def setUp(self):
        self.admin = make_user(username="s_admin", password="pass12345",
                               role="Admin")
        self.librarian = make_user(username="s_lib", password="pass12345",
                                   role="Librarian")
        self.assistant = make_user(username="s_asst", password="pass12345",
                                   role="Assistant")

        cache.clear()

    def test_logout_ends_the_session(self):
        self.client.login(username="s_admin", password="pass12345")

        self.assertEqual(
            self.client.get(reverse("book_list")).status_code, 200
        )

        self.client.post(reverse("logout"))

        after = self.client.get(reverse("book_list"))

        self.assertIn(after.status_code, (301, 302))
        self.assertIn("/login/", after.headers.get("Location", ""))

    def test_a_librarian_cannot_reach_another_account(self):
        self.client.force_login(self.librarian)

        for label, url in (
            ("the user list", reverse("user_list")),
            ("editing the admin", reverse("user_edit", args=[self.admin.id])),
            ("deleting the admin",
             reverse("user_delete", args=[self.admin.id])),
        ):
            with self.subTest(target=label):
                self.assertGreaterEqual(
                    self.client.get(url).status_code, 400
                )

    def test_a_users_own_profile_is_their_own(self):
        self.client.force_login(self.assistant)

        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "s_admin")

    def test_a_password_change_needs_the_current_one(self):
        self.client.login(username="s_asst", password="pass12345")

        self.client.post(
            reverse("profile"),
            {
                "action": "password",
                "current_password": "wrong-one",
                "new_password": "BrandNew12345",
                "confirm_password": "BrandNew12345",
            },
        )

        self.assertIsNone(
            authenticate(username="s_asst", password="BrandNew12345")
        )
        self.assertIsNotNone(
            authenticate(username="s_asst", password="pass12345")
        )
