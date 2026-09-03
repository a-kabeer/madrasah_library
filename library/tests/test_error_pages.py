from django.template import loader
from django.test import Client, TestCase
from django.test.utils import override_settings
from django.urls import reverse

from .helpers import make_branding, make_user


# The custom templates are only used when DEBUG is off.
@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
class ErrorPageTests(TestCase):

    def test_404_uses_the_shared_layout(self):
        response = Client().get("/library/definitely-not-a-page/")

        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Page not found", status_code=404)
        # Inherited from base.html via the error layout.
        self.assertContains(response, "auth-shell", status_code=404)
        self.assertContains(response, "style", status_code=404)

    def test_404_shows_branding_because_a_request_is_available(self):
        make_branding(name="Al Noor Library")

        response = Client().get("/library/nope/")

        self.assertContains(response, "Al Noor Library", status_code=404)

    def test_403_uses_the_shared_layout(self):
        make_user(
            username="assistant_u", password="pass12345", role="Assistant"
        )
        client = Client()
        client.login(username="assistant_u", password="pass12345")

        response = client.get(reverse("branding_settings"))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Permission denied", status_code=403)
        self.assertContains(response, "auth-shell", status_code=403)

    def test_error_pages_do_not_show_the_application_sidebar(self):
        # A nav full of links is misleading on an error page, and on a 500
        # those destinations may be broken too.
        response = Client().get("/library/nope/")

        self.assertNotContains(response, "sidebar-nav", status_code=404)


class ServerErrorTemplateTests(TestCase):
    """500.html has to survive having no context at all.

    Django's `server_error` view calls `template.render()` with no request,
    so no context processors run — `branding` simply does not exist. This
    mirrors that call exactly rather than going through a client.
    """

    def render_500(self):
        return loader.get_template("500.html").render()

    def test_renders_without_a_request_or_context(self):
        html = self.render_500()

        self.assertIn("500", html)
        self.assertIn("Something went wrong", html)
        self.assertIn("Back to Dashboard", html)

    def test_theme_script_still_present_so_the_page_is_not_unstyled(self):
        html = self.render_500()

        self.assertIn("data-bs-theme", html)
        self.assertIn("prefers-color-scheme", html)

    def test_no_broken_branding_references_without_context(self):
        html = self.render_500()

        # With no branding in context these must be omitted rather than
        # emitted empty, or the page would request a missing favicon/logo.
        self.assertNotIn('rel="icon"', html)
        self.assertNotIn("brand-logo", html)
        self.assertNotIn("--brand-primary:", html)

    def test_still_renders_when_branding_exists(self):
        # Configured branding must not change the no-context behaviour,
        # since the 500 handler never consults it either way.
        make_branding(name="Al Noor Library", primary_color="#0f766e")

        html = self.render_500()

        self.assertIn("Something went wrong", html)
        self.assertNotIn("Al Noor Library", html)
