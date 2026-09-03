from django.test import TestCase
from django.urls import reverse

from library.models import User

from .helpers import make_user


class ThemePreferenceModelTests(TestCase):

    def test_existing_users_default_to_system(self):
        # The column is nullable with no default, so users created before it
        # existed read as NULL — which must behave as "system".
        user = make_user(username="legacy_u", password="pass12345")

        self.assertIsNone(user.theme_preference)
        self.assertEqual(user.theme, "system")

    def test_each_allowed_value_is_stored_and_read_back(self):
        user = make_user(username="carol", password="pass12345")

        for value in ("light", "dark", "system"):
            with self.subTest(theme=value):
                user.theme_preference = value
                user.save(update_fields=["theme_preference"])

                user.refresh_from_db()

                self.assertEqual(user.theme_preference, value)
                self.assertEqual(user.theme, value)

    def test_unrecognised_stored_value_reads_as_system(self):
        user = make_user(username="odd_u", password="pass12345")

        User.objects.filter(pk=user.pk).update(theme_preference="neon")
        user.refresh_from_db()

        self.assertEqual(user.theme, "system")


class ThemeSetViewTests(TestCase):

    def setUp(self):
        self.url = reverse("theme_set")
        self.user = make_user(
            username="carol", password="pass12345", role="Librarian"
        )
        self.client.login(username="carol", password="pass12345")

    def test_saving_each_choice_persists_to_the_database(self):
        for value in ("dark", "light", "system"):
            with self.subTest(theme=value):
                response = self.client.post(self.url, {"theme": value})

                self.assertEqual(response.status_code, 302)

                self.user.refresh_from_db()
                self.assertEqual(self.user.theme_preference, value)

    def test_htmx_request_gets_no_content_rather_than_a_redirect(self):
        response = self.client.post(
            self.url,
            {"theme": "dark"},
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 204)

        self.user.refresh_from_db()
        self.assertEqual(self.user.theme_preference, "dark")

    def test_invalid_theme_is_rejected_and_nothing_is_saved(self):
        self.user.theme_preference = "dark"
        self.user.save()

        response = self.client.post(self.url, {"theme": "rainbow"})

        self.assertEqual(response.status_code, 400)

        self.user.refresh_from_db()
        self.assertEqual(self.user.theme_preference, "dark")

    def test_missing_theme_is_rejected(self):
        self.assertEqual(self.client.post(self.url, {}).status_code, 400)

    def test_get_is_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 400)

    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()

        response = self.client.post(self.url, {"theme": "dark"})

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_preference_survives_logout_and_login(self):
        self.client.post(self.url, {"theme": "dark"})
        self.client.logout()

        self.client.login(username="carol", password="pass12345")

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.context["theme_preference"], "dark")


class ThemeRenderingTests(TestCase):

    def setUp(self):
        self.user = make_user(
            username="carol", password="pass12345", role="Librarian"
        )
        self.client.login(username="carol", password="pass12345")

    def test_explicit_choice_is_rendered_server_side(self):
        # Rendering the attribute in the markup is what avoids a flash of
        # the wrong theme before JavaScript runs.
        for value in ("light", "dark"):
            with self.subTest(theme=value):
                self.user.theme_preference = value
                self.user.save(update_fields=["theme_preference"])

                response = self.client.get(reverse("dashboard"))

                self.assertContains(
                    response, f'data-bs-theme="{value}"'
                )

    def test_system_choice_leaves_resolution_to_the_client(self):
        self.user.theme_preference = "system"
        self.user.save(update_fields=["theme_preference"])

        response = self.client.get(reverse("dashboard"))

        # Falls back to light in the markup; the pre-paint script then
        # applies the OS preference.
        self.assertContains(response, 'data-bs-theme="light"')
        self.assertContains(response, "prefers-color-scheme")

    def test_save_url_is_exposed_only_to_authenticated_users(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "data-theme-save-url")

        self.client.logout()

        response = self.client.get(reverse("login"))
        self.assertNotContains(response, "data-theme-save-url")

    def test_selector_is_present_on_login_and_app_pages(self):
        for name in ("dashboard", "profile"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertContains(response, 'data-theme-value="system"')

        self.client.logout()

        response = self.client.get(reverse("login"))
        self.assertContains(response, 'data-theme-value="system"')


class ProfileAppearanceTests(TestCase):
    """The profile page keeps its password form and gains the selector."""

    def setUp(self):
        self.user = make_user(
            username="carol", password="OldPassword1", role="Librarian"
        )
        self.client.login(username="carol", password="OldPassword1")

    def test_profile_shows_appearance_options_with_current_one_checked(self):
        self.user.theme_preference = "dark"
        self.user.save(update_fields=["theme_preference"])

        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="light"')
        self.assertContains(response, 'value="dark"')
        self.assertContains(response, 'value="system"')
        self.assertContains(response, 'id="themedark"')

    def test_profile_still_changes_the_password(self):
        response = self.client.post(
            reverse("profile"),
            {
                "current_password": "OldPassword1",
                "new_password": "NewPassword1",
                "confirm_password": "NewPassword1",
            },
        )

        self.assertEqual(response.status_code, 200)

        self.client.logout()

        self.assertTrue(
            self.client.login(username="carol", password="NewPassword1")
        )
