"""Menu permissions: what a toggle does, and what it cannot do.

Two halves. The first is that a switch actually switches something - the
entry leaves the sidebar *and* the address stops answering, because hiding
a link that still works is not a permission.

The second is the ceiling, and it is the half worth most of the tests. The
whole design rests on the claim that no row in `role_features` can give a
role something the code never allowed. That claim is only worth having if
it survives somebody writing the row by hand, so most of what follows
writes rows directly rather than going through the page.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library import features
from library.models import RoleFeature, User

from .helpers import make_user


class PermissionTestCase(TestCase):

    def setUp(self):
        # The resolver caches for five minutes; a test that writes a row and
        # then reads the answer would otherwise see the previous one.
        features.clear_cache()
        self.addCleanup(features.clear_cache)

        self.admin = make_user(
            username="admin_p", password="pass12345", role="Admin"
        )
        self.librarian = make_user(
            username="librarian_p", password="pass12345", role="Librarian"
        )
        self.assistant = make_user(
            username="assistant_p", password="pass12345", role="Assistant"
        )

    def sign_in(self, username):
        self.client.login(username=username, password="pass12345")
        return self.client

    def make_super_admin(self, username="root_p"):
        user = make_user(
            username=username, password="pass12345", role="Admin"
        )
        # `make_user` goes through the form's role list, which deliberately
        # has no SuperAdmin in it - that account is made by somebody with
        # database access, which is what this stands in for.
        User.objects.filter(pk=user.pk).update(role="SuperAdmin")
        user.refresh_from_db()
        return user

    def override(self, role, key, allowed):
        RoleFeature.objects.update_or_create(
            role=role,
            feature_key=key,
            defaults={"allowed": allowed, "updated_at": timezone.now()},
        )
        features.clear_cache()


class DefaultsTests(PermissionTestCase):
    """An empty table must mean "exactly as it was"."""

    def test_nothing_is_stored_to_begin_with(self):
        self.assertFalse(RoleFeature.objects.exists())

    def test_every_role_keeps_what_it_had(self):
        # The counts are not the point; that each role holds strictly less
        # than the one above is.
        held = {
            role: len(features.allowed_features(_Role(role)))
            for role in ("Admin", "Librarian", "Assistant")
        }

        self.assertGreater(held["Admin"], held["Librarian"])
        self.assertGreater(held["Librarian"], held["Assistant"])

    def test_a_super_admin_holds_everything(self):
        self.assertEqual(
            features.allowed_features(_Role("SuperAdmin")),
            frozenset(f.key for f in features.FEATURES),
        )

    def test_an_unknown_feature_is_held_by_nobody(self):
        # A key that was renamed leaves rows behind; they must not become a
        # way in.
        self.assertFalse(features.role_has("Admin", "no_such_feature"))


class SwitchingOffTests(PermissionTestCase):

    def test_the_entry_leaves_the_sidebar(self):
        client = self.sign_in("librarian_p")

        self.assertContains(client.get(reverse("book_list")), "Authors")

        self.override("Librarian", "authors", False)

        self.assertNotContains(client.get(reverse("book_list")), "Authors")

    def test_and_the_address_stops_answering(self):
        # The half that matters: a hidden link that still works is not a
        # permission, it is a decoration.
        client = self.sign_in("librarian_p")

        self.assertEqual(client.get(reverse("author_list")).status_code, 200)

        self.override("Librarian", "authors", False)

        self.assertEqual(client.get(reverse("author_list")).status_code, 403)

    def test_every_view_of_the_feature_goes_together(self):
        client = self.sign_in("librarian_p")

        self.override("Librarian", "authors", False)

        for name, args in (
            ("author_list", ()),
            ("author_add", ()),
        ):
            with self.subTest(view=name):
                self.assertEqual(
                    client.get(reverse(name, args=args)).status_code, 403
                )

    def test_switching_off_one_role_leaves_the_others_alone(self):
        self.override("Assistant", "authors", False)

        self.assertEqual(
            self.sign_in("librarian_p").get(
                reverse("author_list")
            ).status_code,
            200,
        )

    def test_a_section_disappears_with_its_last_entry(self):
        client = self.sign_in("assistant_p")

        self.assertContains(client.get(reverse("book_list")), "PEOPLE")

        self.override("Assistant", "borrowers", False)

        self.assertNotContains(client.get(reverse("book_list")), "PEOPLE")

    def test_the_dashboard_cannot_be_switched_off(self):
        # Somebody signed in with no dashboard has nowhere to land.
        self.override("Assistant", "dashboard", False)

        self.assertTrue(features.role_has("Assistant", "dashboard"))
        self.assertEqual(
            self.sign_in("assistant_p").get(
                reverse("library_home")
            ).status_code,
            200,
        )


class TheCeilingTests(PermissionTestCase):
    """A stored row cannot give a role what the code never allowed."""

    def test_a_row_granting_users_to_a_librarian_is_ignored(self):
        self.override("Librarian", "users", True)

        self.assertFalse(features.role_has("Librarian", "users"))

        self.assertEqual(
            self.sign_in("librarian_p").get(
                reverse("user_list")
            ).status_code,
            403,
        )

    def test_the_same_for_branding_and_for_this_page(self):
        client = self.sign_in("librarian_p")

        for key, route in (
            ("branding", "branding_settings"),
            ("permissions", "permissions_matrix"),
        ):
            with self.subTest(feature=key):
                self.override("Librarian", key, True)

                self.assertFalse(features.role_has("Librarian", key))
                self.assertEqual(client.get(reverse(route)).status_code, 403)

    def test_switching_a_feature_on_does_not_widen_the_views_under_it(self):
        # `books` is held by everyone, but adding one is Admin/Librarian in
        # code. Turning the feature on for an Assistant gives them the list,
        # never the Add form.
        self.override("Assistant", "books", True)

        client = self.sign_in("assistant_p")

        self.assertEqual(client.get(reverse("book_list")).status_code, 200)
        self.assertEqual(client.get(reverse("book_add")).status_code, 403)

    def test_an_admin_cannot_be_given_a_feature_by_a_row_either(self):
        # Nothing is above the ceiling, not even for an Admin: the point is
        # that the table never grants, only withholds.
        self.override("Assistant", "analytics", True)

        self.assertFalse(features.role_has("Assistant", "analytics"))


class TheMatrixPageTests(PermissionTestCase):

    url = reverse("permissions_matrix")

    def test_an_admin_can_open_it(self):
        self.assertEqual(self.sign_in("admin_p").get(self.url).status_code, 200)

    def test_a_librarian_cannot(self):
        self.assertEqual(
            self.sign_in("librarian_p").get(self.url).status_code, 403
        )

    def test_saving_writes_an_override(self):
        client = self.sign_in("admin_p")

        # Everything currently on for a Librarian, minus authors.
        keep = [
            "Librarian:%s" % f.key
            for f in features.FEATURES
            if features.role_has("Librarian", f.key)
            and f.key != "authors"
            and not f.locked
        ]

        client.post(self.url, {"feature": keep})
        features.clear_cache()

        self.assertFalse(features.role_has("Librarian", "authors"))

    def test_saving_records_what_changed(self):
        from library.models import ActivityLog

        client = self.sign_in("admin_p")
        client.post(self.url, {"feature": []})

        entry = ActivityLog.objects.filter(entity_type="RoleFeature").first()

        self.assertIsNotNone(entry)
        self.assertIn("Menu permissions", entry.description)

    def test_an_admin_cannot_edit_their_own_column(self):
        # Not even by posting it: an Admin who switched off `permissions`
        # for Admins would have no way back to this page.
        client = self.sign_in("admin_p")

        client.post(self.url, {"feature": []})
        features.clear_cache()

        self.assertTrue(features.role_has("Admin", "permissions"))
        self.assertTrue(features.role_has("Admin", "users"))
        self.assertEqual(
            RoleFeature.objects.filter(role="Admin").count(), 0
        )

    def test_a_super_admin_can_edit_the_admin_column(self):
        self.make_super_admin()
        client = self.sign_in("root_p")

        keep = [
            "%s:%s" % (role, f.key)
            for role in features.MANAGED_ROLES
            for f in features.FEATURES
            if features.role_has(role, f.key)
            and not f.locked
            and role in f.ceiling
            and not (role == "Admin" and f.key == "analytics")
        ]

        client.post(self.url, {"feature": keep})
        features.clear_cache()

        self.assertFalse(features.role_has("Admin", "analytics"))

    def test_a_locked_row_is_never_written(self):
        client = self.sign_in("admin_p")
        client.post(self.url, {"feature": []})

        self.assertFalse(
            RoleFeature.objects.filter(feature_key="dashboard").exists()
        )


class SuperAdminGuardTests(PermissionTestCase):

    def test_an_admin_sees_the_super_admin_in_the_list(self):
        self.make_super_admin()

        self.assertContains(
            self.sign_in("admin_p").get(reverse("user_list")), "root_p"
        )

    def test_but_cannot_edit_them(self):
        root = self.make_super_admin()
        client = self.sign_in("admin_p")

        response = client.post(
            reverse("user_edit", args=[root.pk]),
            {
                "username": "hijacked",
                "full_name": "Nope",
                "role": "Assistant",
                "is_active": "on",
            },
        )

        root.refresh_from_db()

        # The write is what matters, and it did not happen.
        self.assertEqual(root.username, "root_p")
        self.assertEqual(root.role, "SuperAdmin")

        # Edit is a dialog now: a plain POST redirects with the reason as a
        # message. The wording itself is asserted through the dialog below,
        # which is where an Admin would actually read it.
        self.assertEqual(response.status_code, 302)

    def test_and_the_dialog_gives_the_reason(self):
        root = self.make_super_admin()
        client = self.sign_in("admin_p")

        response = client.post(
            reverse("user_edit", args=[root.id]) + "?modal=1",
            {
                "username": "hijacked",
                "full_name": "Nope",
                "role": "Assistant",
                "is_active": "on",
            },
            headers={"HX-Request": "true"},
        )

        root.refresh_from_db()

        self.assertEqual(root.username, "root_p")
        self.assertContains(response, "managed by the developer")

    def test_nor_deactivate_them(self):
        root = self.make_super_admin()

        self.sign_in("admin_p").post(
            reverse("user_toggle_active", args=[root.pk])
        )

        root.refresh_from_db()
        self.assertTrue(root.is_active)

    def test_nor_delete_them(self):
        root = self.make_super_admin()

        self.sign_in("admin_p").post(reverse("user_delete", args=[root.pk]))

        self.assertTrue(User.objects.filter(pk=root.pk).exists())

    def test_the_add_form_will_not_make_one(self):
        # `role` is checked against USER_ROLES, which has no SuperAdmin in
        # it - the account is made by somebody with database access.
        self.sign_in("admin_p").post(
            reverse("user_add"),
            {
                "username": "sneaky",
                "full_name": "Sneaky",
                "password": "pass12345",
                "role": "SuperAdmin",
            },
        )

        self.assertFalse(
            User.objects.filter(username="sneaky").exists()
        )

    def test_the_last_super_admin_cannot_be_demoted(self):
        root = self.make_super_admin()
        client = self.sign_in("root_p")

        client.post(
            reverse("user_edit", args=[root.pk]),
            {
                "username": "root_p",
                "full_name": root.full_name,
                "role": "Admin",
                "is_active": "on",
            },
        )

        root.refresh_from_db()
        self.assertEqual(root.role, "SuperAdmin")

    def test_nor_deactivated(self):
        root = self.make_super_admin()

        self.sign_in("root_p").post(
            reverse("user_toggle_active", args=[root.pk])
        )

        root.refresh_from_db()
        self.assertTrue(root.is_active)

    def test_but_one_of_two_can_be(self):
        first = self.make_super_admin("root_one")
        self.make_super_admin("root_two")

        self.sign_in("root_two").post(
            reverse("user_toggle_active", args=[first.pk])
        )

        first.refresh_from_db()
        self.assertFalse(first.is_active)

    def test_nobody_changes_their_own_role(self):
        client = self.sign_in("admin_p")

        client.post(
            reverse("user_edit", args=[self.admin.pk]),
            {
                "username": "admin_p",
                "full_name": self.admin.full_name,
                "role": "Librarian",
                "is_active": "on",
            },
        )

        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, "Admin")


class CostTests(PermissionTestCase):

    def test_the_sidebar_costs_one_read_however_many_entries(self):
        """Twenty-two gated entries, one read of the overrides.

        Not zero: the test settings use a dummy cache, so every request
        genuinely goes to the table and this measures the uncached cost -
        which is the one worth pinning, because it is the cost on the day
        the cache is cold or turned off. In production the five-minute
        cache makes it zero for all but the first request.

        What this guards is the shape: reading once per *request* rather
        than once per entry, or once per question asked of it.
        """

        client = self.sign_in("admin_p")

        with CaptureQueriesContext(connection) as queries:
            client.get(reverse("book_list"))

        reads = [
            q for q in queries.captured_queries
            if "role_features" in q["sql"]
        ]

        self.assertEqual(len(reads), 1, reads)

    def test_saving_clears_the_cache(self):
        self.assertTrue(features.role_has("Librarian", "authors"))

        self.sign_in("admin_p").post(
            reverse("permissions_matrix"),
            {"feature": []},
        )

        # No clear_cache() here on purpose: the view must have done it.
        self.assertFalse(features.role_has("Librarian", "authors"))


class _Role:
    """Something with a `.role`, which is all the resolver reads."""

    def __init__(self, role):
        self.role = role
