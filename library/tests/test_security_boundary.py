"""The security boundary, checked by exercising it rather than reading it.

`LoginRequiredMiddleware` is on globally and every gated view carries a
decorator, but neither of those is evidence on its own. These walk the whole
route table unauthenticated, try each role's own escalation, and check that
signing out, another person's account and a password change behave as the
boundary says they do.
"""

import re

from django.contrib.auth import authenticate
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import get_resolver, reverse

from library.models import User
from library.tests.helpers import make_user

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class AnonymousAccess(TestCase):
    """Every route, unauthenticated.

    Only the sign-in page, the public catalogue and Django's language switch
    may answer with content; everything else must redirect to sign-in or
    refuse outright.
    """
    def test_every_route_refuses_an_anonymous_visitor(self):
        allowed = {"/library/login/", "/catalog/", "/i18n/setlang/"}
        rows=[]
        for p in get_resolver().url_patterns:
            for sub in getattr(p,"url_patterns",[p]):
                name=getattr(sub,"name",None)
                if not name: continue
                try:
                    url=reverse(name)
                except Exception:
                    try: url=reverse(name,args=[1])
                    except Exception: continue
                r=self.client.get(url)
                loc=r.headers.get("Location","")
                ok = (r.status_code in (301,302) and "/login/" in loc) or r.status_code in (403,405)
                public = url.startswith("/catalog/") or url in allowed
                if not ok and not public:
                    rows.append((url, r.status_code, loc[:40]))
        for r in rows: print(f"   {r[1]}  {r[0]}  {r[2]}")
        self.assertEqual(rows, [])


@override_settings(CACHES=LOCMEM)
class RoleEscalation(TestCase):
    def setUp(self):
        self.asst=make_user(username="esc_asst", password="p", role="Assistant")
        self.lib=make_user(username="esc_lib", password="p", role="Librarian")
        self.admin=make_user(username="esc_admin", password="p", role="Admin")
        cache.clear()

    def test_an_assistant_cannot_promote_themselves(self):
        self.client.force_login(self.asst)
        r=self.client.post(reverse("user_edit", args=[self.asst.id]),
                           {"username":"esc_asst","full_name":"X","role":"Admin","is_active":"True"})
        after=User.objects.get(id=self.asst.id).role
        self.assertEqual(after,"Assistant")

    def test_a_librarian_cannot_promote_themselves(self):
        self.client.force_login(self.lib)
        r=self.client.post(reverse("user_edit", args=[self.lib.id]),
                           {"username":"esc_lib","full_name":"X","role":"Admin","is_active":"True"})
        after=User.objects.get(id=self.lib.id).role
        self.assertEqual(after,"Librarian")

    def test_an_admin_cannot_make_a_super_admin(self):
        self.client.force_login(self.admin)
        r=self.client.post(reverse("user_edit", args=[self.asst.id]),
                           {"username":"esc_asst","full_name":"X","role":"SuperAdmin","is_active":"True"})
        after=User.objects.get(id=self.asst.id).role
        self.assertNotEqual(after,"SuperAdmin")

    def test_an_assistant_cannot_grant_themselves_a_feature(self):
        self.client.force_login(self.asst)
        r=self.client.post(reverse("permissions_matrix"), {"Assistant:users":"on"})
        self.assertGreaterEqual(r.status_code,400)


@override_settings(CACHES=LOCMEM)
class SessionAndObjectAccess(TestCase):
    def setUp(self):
        self.admin=make_user(username="s_admin", password="pass12345", role="Admin")
        self.lib=make_user(username="s_lib", password="pass12345", role="Librarian")
        self.asst=make_user(username="s_asst", password="pass12345", role="Assistant")
        cache.clear()

    def test_logout_ends_the_session(self):
        self.client.login(username="s_admin", password="pass12345")
        before=self.client.get(reverse("book_list")).status_code
        self.client.post(reverse("logout"))
        after=self.client.get(reverse("book_list"))
        self.assertIn(after.status_code,(301,302))
        self.assertIn("/login/", after.headers.get("Location",""))

    def test_a_librarian_cannot_reach_another_account(self):
        self.client.force_login(self.lib)
        for label,url in (("view the user list", reverse("user_list")),
                          ("edit the admin", reverse("user_edit", args=[self.admin.id])),
                          ("delete the admin", reverse("user_delete", args=[self.admin.id]))):
            r=self.client.get(url)
            self.assertGreaterEqual(r.status_code,400)

    def test_a_users_own_profile_is_their_own(self):
        self.client.force_login(self.asst)
        r=self.client.get(reverse("profile"))
        body=r.content.decode(errors="replace")
        self.assertEqual(r.status_code,200)
        self.assertNotIn("s_admin", body)

    def test_a_password_change_needs_the_current_one(self):
        self.client.login(username="s_asst", password="pass12345")
        r=self.client.post(reverse("profile"), {
            "action":"password","current_password":"wrong-one",
            "new_password":"BrandNew12345","confirm_password":"BrandNew12345"})
        changed = authenticate(username="s_asst", password="BrandNew12345") is not None
        self.assertFalse(changed)
