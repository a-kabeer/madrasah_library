from django.contrib.auth import authenticate
from django.test import TestCase
from django.urls import reverse

from library.models import User
from library.tests.helpers import make_user, make_loan


class UserEditTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")
        self.target = make_user(username="target_u", password="OldPass123", role="Assistant")

    def test_edit_without_new_password_keeps_old_password_working(self):
        response = self.client.post(reverse("user_edit", args=[self.target.id]), {
            "username": "target_u",
            "full_name": "Target Updated",
            "new_password": "",
            "role": "Assistant",
            "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)

        self.target.refresh_from_db()
        self.assertEqual(self.target.full_name, "Target Updated")
        self.assertIsNotNone(authenticate(username="target_u", password="OldPass123"))

    def test_setting_new_password_actually_changes_it(self):
        response = self.client.post(reverse("user_edit", args=[self.target.id]), {
            "username": "target_u",
            "full_name": "Target User",
            "new_password": "BrandNewPass1",
            "role": "Assistant",
            "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)

        self.assertIsNone(authenticate(username="target_u", password="OldPass123"))
        self.assertIsNotNone(authenticate(username="target_u", password="BrandNewPass1"))

    def test_too_short_new_password_is_rejected(self):
        response = self.client.post(reverse("user_edit", args=[self.target.id]), {
            "username": "target_u",
            "full_name": "Target User",
            "new_password": "short",
            "role": "Assistant",
        })

        # Edit is a dialog now, so a plain POST redirects and carries the
        # reason as a message rather than re-rendering a page.
        self.assertEqual(response.status_code, 302)

        # original password must still work
        self.assertIsNotNone(authenticate(username="target_u", password="OldPass123"))

    def test_and_the_dialog_says_so_without_closing(self):
        """The message reaches the person editing, where they are.

        Asserted separately from the redirect because this is the path
        anyone actually takes: the dialog re-renders in place with the
        reason and whatever was typed still in the fields.
        """

        response = self.client.post(
            reverse("user_edit", args=[self.target.id]) + "?modal=1",
            {
                "username": "target_u",
                "full_name": "Target User",
                "new_password": "short",
                "role": "Assistant",
            },
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at least 8 characters")
        self.assertIsNotNone(
            authenticate(username="target_u", password="OldPass123")
        )


class UserDeleteTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_user_delete_blocked_when_they_issued_a_loan(self):
        """The records point at them, so the account stays.

        `users.id` is referenced by `loans.issued_by`/`returned_to` and by
        `activity_logs.user_id`, all NO ACTION - so deleting one who has
        ever issued a loan would come back as an unhandled IntegrityError.
        """

        staff = make_user(username="staff_u", password="pass12345", role="Librarian")
        make_loan(issued_by=staff)

        response = self.client.post(reverse("user_delete", args=[staff.id]))

        # A plain POST redirects and says so in a message; there is no
        # standalone page left to re-render.
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(id=staff.id).exists())

    def test_the_dialog_says_why_rather_than_offering_the_button(self):
        staff = make_user(username="staff_u", password="pass12345", role="Librarian")
        make_loan(issued_by=staff)

        response = self.client.get(
            reverse("user_delete", args=[staff.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deactivate it instead")
        # No Delete button when it cannot go.
        self.assertNotContains(response, "btn-danger")

    def test_an_admin_cannot_delete_another_admin(self):
        """Reversible things are allowed; this one is not.

        One Admin removing the others is how an installation ends up held
        by a single person, so deletion of an Admin is the SuperAdmin's to
        do. Deactivating is still open, and is undone from the same switch.
        """

        other = make_user(username="other_admin", password="pass12345", role="Admin")

        response = self.client.post(reverse("user_delete", args=[other.id]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(id=other.id).exists())

    def test_and_the_dialog_offers_deactivating_instead(self):
        other = make_user(username="other_admin", password="pass12345", role="Admin")

        response = self.client.get(
            reverse("user_delete", args=[other.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )

        self.assertContains(response, "Only a Super Admin can delete an Admin")

    def test_but_an_admin_may_deactivate_another_admin(self):
        other = make_user(username="other_admin", password="pass12345", role="Admin")

        self.client.post(reverse("user_toggle_active", args=[other.id]))

        other.refresh_from_db()
        self.assertFalse(other.is_active)

    def test_a_super_admin_can_delete_an_admin(self):
        User.objects.filter(pk=self.admin.pk).update(role="SuperAdmin")
        other = make_user(username="other_admin", password="pass12345", role="Admin")

        response = self.client.post(reverse("user_delete", args=[other.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(id=other.id).exists())

    def test_user_delete_succeeds_with_no_related_records(self):
        staff = make_user(username="staff_u", password="pass12345", role="Librarian")

        response = self.client.post(reverse("user_delete", args=[staff.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(id=staff.id).exists())


class UserAddTests(TestCase):
    """Add User, which did not work at all before the dialog rewrite.

    `user_add` called `User.objects.create(password_hash=...)`. The model
    field is `password` with `db_column="password_hash"`, so that keyword
    was not a field and every submission raised

        TypeError: User() got unexpected keyword arguments: 'password_hash'

    No test called it, so nothing caught that the feature was dead. These
    do.
    """

    def setUp(self):
        self.admin = make_user(
            username="admin_u", password="pass12345", role="Admin"
        )
        self.client.login(username="admin_u", password="pass12345")

    def post(self, **overrides):
        data = {
            "username": "new_person",
            "full_name": "New Person",
            "role": "Librarian",
            "password": "brand-new-pass",
        }
        data.update(overrides)
        return self.client.post(reverse("user_add"), data)

    def test_it_creates_the_account(self):
        response = self.post()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username="new_person").exists())

    def test_the_password_is_hashed_and_the_account_can_sign_in(self):
        self.post()

        created = User.objects.get(username="new_person")

        # The stored value must not be what was typed - `authenticate` on
        # the login page compares a hash, so a raw value would also mean
        # nobody could ever sign in with it.
        self.assertNotEqual(created.password, "brand-new-pass")
        self.assertTrue(created.check_password("brand-new-pass"))
        self.assertIsNotNone(
            authenticate(username="new_person", password="brand-new-pass")
        )

    def test_a_short_password_is_refused(self):
        response = self.post(password="short")

        self.assertFalse(User.objects.filter(username="new_person").exists())
        self.assertEqual(response.status_code, 302)

    def test_a_duplicate_username_is_refused_before_the_database(self):
        response = self.client.get(
            reverse("user_add") + "?modal=1",
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 200)

        self.post(username="admin_u")

        # Still exactly one, and no IntegrityError reached the caller.
        self.assertEqual(User.objects.filter(username="admin_u").count(), 1)

    def test_super_admin_cannot_be_granted_from_the_form(self):
        """The role list is the ceiling, whatever the request says.

        USER_ROLES holds Admin, Librarian and Assistant. A SuperAdmin is
        made by somebody with database access, deliberately, so posting the
        role directly must not work either.
        """

        self.post(username="sneaky", role="SuperAdmin")

        self.assertFalse(User.objects.filter(username="sneaky").exists())

    def test_the_dialog_offers_only_the_three_manageable_roles(self):
        response = self.client.get(
            reverse("user_add") + "?modal=1",
            headers={"HX-Request": "true"},
        )

        body = response.content.decode()

        for role in ("Admin", "Librarian", "Assistant"):
            with self.subTest(role=role):
                self.assertIn('value="%s"' % role, body)

        self.assertNotIn('value="SuperAdmin"', body)
