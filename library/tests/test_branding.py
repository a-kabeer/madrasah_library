import io

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from PIL import Image

from library.context_processors import (
    BRANDING_CACHE_KEY,
    clear_branding_cache,
    get_branding,
)
from library.models import (
    DARK_GROUND,
    MINIMUM_CONTRAST,
    OrganizationSettings,
    _contrast,
    hex_to_rgb_triplet,
    lifted_for_dark,
    readable_foreground,
)

from .helpers import make_branding, make_user


def make_image_bytes(fmt="PNG", size=(40, 40), name="logo.png"):
    """A real, decodable image file — not just bytes with the right suffix."""

    from django.core.files.uploadedfile import SimpleUploadedFile

    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 80, 200)).save(buffer, format=fmt)

    return SimpleUploadedFile(
        name,
        buffer.getvalue(),
        content_type=f"image/{fmt.lower()}",
    )


class BrandingDefaultsTests(TestCase):
    """The app has to work before any branding has been configured."""

    def test_load_returns_defaults_when_no_row_exists(self):
        self.assertEqual(OrganizationSettings.objects.count(), 0)

        settings_obj = OrganizationSettings.load()

        self.assertIsNone(settings_obj.pk)
        self.assertEqual(settings_obj.display_name, "Madrasah Library")
        self.assertEqual(settings_obj.display_primary_color, "#0d6efd")
        self.assertFalse(settings_obj.has_custom_colors)

    def test_pages_render_with_no_branding_row(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        for name in ("dashboard", "book_list", "profile"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Madrasah Library")

    def test_login_page_renders_branding_for_anonymous_user(self):
        # The context processor must not depend on request.user.
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Madrasah Library")

    def test_blank_fields_fall_back_to_defaults(self):
        settings_obj = make_branding(name="", primary_color="")

        self.assertEqual(settings_obj.display_name, "Madrasah Library")
        self.assertEqual(settings_obj.display_primary_color, "#0d6efd")


class BrandingSingletonTests(TestCase):

    def test_save_always_uses_the_same_row(self):
        first = make_branding(name="One")
        second = make_branding(name="Two")

        self.assertEqual(first.pk, OrganizationSettings.SINGLETON_ID)
        self.assertEqual(second.pk, OrganizationSettings.SINGLETON_ID)
        self.assertEqual(OrganizationSettings.objects.count(), 1)
        self.assertEqual(OrganizationSettings.load().name, "Two")

    def test_explicit_id_is_overridden_on_save(self):
        settings_obj = OrganizationSettings(id=99, name="Forced")
        settings_obj.save()

        self.assertEqual(settings_obj.pk, OrganizationSettings.SINGLETON_ID)
        self.assertEqual(OrganizationSettings.objects.count(), 1)


class BrandingContextProcessorTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_branding_is_available_in_template_context(self):
        make_branding(name="Al Noor Library")

        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(
            response.context["branding"].display_name,
            "Al Noor Library",
        )
        self.assertContains(response, "Al Noor Library")

    def test_theme_preference_is_blank_for_anonymous_users(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.context["theme_preference"], "")

    def test_theme_preference_reflects_signed_in_user(self):
        user = make_user(username="carol", password="pass12345", role="Admin")
        user.theme_preference = "dark"
        user.save()

        self.client.login(username="carol", password="pass12345")

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.context["theme_preference"], "dark")

    def test_custom_colors_are_injected_into_the_page(self):
        make_branding(primary_color="#ff8800")

        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "--brand-primary: #ff8800")
        # Bootstrap needs the channels split out; CSS cannot do that.
        self.assertContains(response, "--bs-primary-rgb: 255, 136, 0")

    def test_no_inline_style_block_when_no_colors_configured(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, "--brand-primary:")


class AssetVersionTests(TestCase):
    """The dev server serves CSS/JS at a stable URL with no Cache-Control,
    so without a version token the browser keeps a stale copy and script
    changes silently fail to take effect."""

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_assets_are_versioned_while_debugging(self):
        from library.context_processors import get_asset_version

        with self.settings(DEBUG=True):
            version = get_asset_version()

            self.assertTrue(version)
            self.assertTrue(version.isdigit())

    def test_no_version_needed_when_debug_is_off(self):
        # Production filenames already carry a content hash.
        from library.context_processors import get_asset_version

        with self.settings(DEBUG=False):
            self.assertEqual(get_asset_version(), "")

    def test_version_tracks_the_files_modification_time(self):
        import os

        from library.context_processors import LOCAL_ASSETS, get_asset_version

        with self.settings(DEBUG=True):
            before = get_asset_version()

            path = os.path.join(
                os.path.dirname(
                    __import__("library").__file__
                ),
                "static",
                LOCAL_ASSETS[1],
            )
            stat = os.stat(path)

            # The version is the newest mtime across all the local assets,
            # so the bump has to clear the newest one — not just this file's.
            # Adding to its own mtime silently proves nothing whenever a
            # sibling happens to have been edited more recently.
            try:
                os.utime(
                    path,
                    (stat.st_atime, int(before) + 3600),
                )
                self.assertNotEqual(get_asset_version(), before)

            finally:
                os.utime(path, (stat.st_atime, stat.st_mtime))

    def test_page_appends_the_version_to_local_assets(self):
        with self.settings(DEBUG=True):
            response = self.client.get(reverse("dashboard"))
            html = response.content.decode()

        self.assertIn("app.js?v=", html)
        self.assertIn("style.css?v=", html)


class BrandingCacheTests(TestCase):
    """Caching is a no-op under DummyCache during tests, so exercise the
    helpers directly with a real backend."""

    def setUp(self):
        cache.clear()

    def test_get_branding_caches_and_invalidation_clears_it(self):
        locmem = "django.core.cache.backends.locmem.LocMemCache"

        with self.settings(CACHES={"default": {"BACKEND": locmem}}):
            cache.clear()

            make_branding(name="First Name")

            self.assertEqual(get_branding().display_name, "First Name")
            self.assertIsNotNone(cache.get(BRANDING_CACHE_KEY))

            # Writing straight to the DB leaves the cache stale on purpose,
            # which is what makes the next assertion meaningful.
            OrganizationSettings.objects.filter(
                id=OrganizationSettings.SINGLETON_ID
            ).update(name="Second Name")

            self.assertEqual(get_branding().display_name, "First Name")

            clear_branding_cache()

            self.assertIsNone(cache.get(BRANDING_CACHE_KEY))
            self.assertEqual(get_branding().display_name, "Second Name")


class BrandingPermissionTests(TestCase):

    def setUp(self):
        self.url = reverse("branding_settings")

        self.admin = make_user(
            username="admin_u", password="pass12345", role="Admin"
        )
        self.librarian = make_user(
            username="librarian_u", password="pass12345", role="Librarian"
        )
        self.assistant = make_user(
            username="assistant_u", password="pass12345", role="Assistant"
        )

    def login(self, user):
        self.client.login(username=user.username, password="pass12345")

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_admin_can_open_branding_settings(self):
        self.login(self.admin)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_librarian_cannot_open_branding_settings(self):
        self.login(self.librarian)

        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_assistant_cannot_open_branding_settings(self):
        self.login(self.assistant)

        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_librarian_post_is_denied_and_changes_nothing(self):
        self.login(self.librarian)

        response = self.client.post(self.url, {"name": "Hijacked"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(OrganizationSettings.objects.count(), 0)

    def test_assistant_post_is_denied_and_changes_nothing(self):
        self.login(self.assistant)

        response = self.client.post(self.url, {"name": "Hijacked"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(OrganizationSettings.objects.count(), 0)

    def test_branding_link_only_shown_to_admin(self):
        self.login(self.admin)
        self.assertContains(self.client.get(reverse("dashboard")), self.url)

        self.client.logout()

        self.login(self.librarian)
        self.assertNotContains(
            self.client.get(reverse("dashboard")), self.url
        )


class BrandingUpdateTests(TestCase):

    def setUp(self):
        self.url = reverse("branding_settings")
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def payload(self, **overrides):
        data = {
            "name": "Al Noor Library",
            "primary_color": "#ff8800",
            "secondary_color": "",
            "accent_color": "",
            "contact_email": "info@alnoor.test",
            "contact_phone": "0300-1234567",
            "footer_text": "Serving since 1990",
        }
        data.update(overrides)

        return data

    def test_admin_can_save_branding(self):
        response = self.client.post(self.url, self.payload())

        self.assertRedirects(response, self.url)

        settings_obj = OrganizationSettings.load()

        self.assertEqual(settings_obj.name, "Al Noor Library")
        self.assertEqual(settings_obj.primary_color, "#ff8800")
        self.assertEqual(settings_obj.contact_email, "info@alnoor.test")
        self.assertEqual(settings_obj.footer_text, "Serving since 1990")
        self.assertIsNotNone(settings_obj.updated_at)

    def test_saved_branding_appears_on_other_pages(self):
        self.client.post(self.url, self.payload())

        response = self.client.get(reverse("book_list"))

        self.assertContains(response, "Al Noor Library")

    def test_invalid_hex_color_is_rejected(self):
        response = self.client.post(
            self.url,
            self.payload(primary_color="not-a-color"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "hex code")
        self.assertEqual(OrganizationSettings.objects.count(), 0)

    def test_invalid_color_keeps_submitted_values_on_screen(self):
        response = self.client.post(
            self.url,
            self.payload(name="Typed Name", primary_color="#zzz"),
        )

        self.assertContains(response, "Typed Name")

    def test_shorthand_hex_is_accepted(self):
        response = self.client.post(
            self.url,
            self.payload(primary_color="#f80"),
        )

        self.assertRedirects(response, self.url)
        self.assertEqual(OrganizationSettings.load().primary_color, "#f80")

    def test_blank_colors_are_allowed(self):
        response = self.client.post(self.url, self.payload(primary_color=""))

        self.assertRedirects(response, self.url)
        self.assertEqual(
            OrganizationSettings.load().display_primary_color,
            "#0d6efd",
        )


class BrandingUploadTests(TestCase):

    def setUp(self):
        self.url = reverse("branding_settings")
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def base_payload(self, **overrides):
        data = {
            "name": "Al Noor",
            "primary_color": "",
            "secondary_color": "",
            "accent_color": "",
            "contact_email": "",
            "contact_phone": "",
            "footer_text": "",
        }
        data.update(overrides)

        return data

    def tearDown(self):
        # Uploads land in MEDIA_ROOT; don't leave files behind.
        settings_obj = OrganizationSettings.objects.filter(
            id=OrganizationSettings.SINGLETON_ID
        ).first()

        if settings_obj is None:
            return

        for field in (settings_obj.logo, settings_obj.favicon):
            if field:
                field.delete(save=False)

    def test_valid_logo_upload_is_stored(self):
        response = self.client.post(
            self.url,
            self.base_payload(logo=make_image_bytes()),
        )

        self.assertRedirects(response, self.url)

        settings_obj = OrganizationSettings.load()

        self.assertTrue(settings_obj.logo)
        self.assertIn("branding/", settings_obj.logo.name)

    def test_logo_appears_in_the_page_when_set(self):
        self.client.post(
            self.url,
            self.base_payload(logo=make_image_bytes()),
        )

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "brand-logo")

    def test_disallowed_extension_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile(
            "logo.txt",
            b"not an image",
            content_type="image/png",
        )

        response = self.client.post(
            self.url,
            self.base_payload(logo=bad),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must be a")
        self.assertEqual(OrganizationSettings.objects.count(), 0)

    def test_corrupt_image_with_valid_extension_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        # Right suffix, wrong bytes — only Pillow can catch this.
        fake = SimpleUploadedFile(
            "logo.png",
            b"this is definitely not a PNG",
            content_type="image/png",
        )

        response = self.client.post(
            self.url,
            self.base_payload(logo=fake),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be read as an image")
        self.assertEqual(OrganizationSettings.objects.count(), 0)

    def test_oversized_logo_is_rejected(self):
        with self.settings(LOGO_MAX_BYTES=10):
            response = self.client.post(
                self.url,
                self.base_payload(logo=make_image_bytes()),
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "or smaller")
        self.assertEqual(OrganizationSettings.objects.count(), 0)

    def test_replacing_a_logo_deletes_the_previous_file(self):
        self.client.post(
            self.url,
            self.base_payload(logo=make_image_bytes(name="first.png")),
        )

        settings_obj = OrganizationSettings.load()
        first_name = settings_obj.logo.name
        storage = settings_obj.logo.storage

        self.assertTrue(storage.exists(first_name))

        self.client.post(
            self.url,
            self.base_payload(logo=make_image_bytes(name="second.png")),
        )

        settings_obj = OrganizationSettings.load()

        self.assertNotEqual(settings_obj.logo.name, first_name)
        self.assertFalse(storage.exists(first_name))
        self.assertTrue(storage.exists(settings_obj.logo.name))

    def test_removing_a_logo_clears_it_and_deletes_the_file(self):
        self.client.post(
            self.url,
            self.base_payload(logo=make_image_bytes()),
        )

        settings_obj = OrganizationSettings.load()
        stored_name = settings_obj.logo.name
        storage = settings_obj.logo.storage

        self.client.post(
            self.url,
            self.base_payload(remove_logo="on"),
        )

        settings_obj = OrganizationSettings.load()

        self.assertFalse(settings_obj.logo)
        self.assertFalse(storage.exists(stored_name))

    def test_upload_wins_over_the_remove_checkbox(self):
        self.client.post(
            self.url,
            self.base_payload(logo=make_image_bytes(name="first.png")),
        )

        self.client.post(
            self.url,
            self.base_payload(
                logo=make_image_bytes(name="second.png"),
                remove_logo="on",
            ),
        )

        self.assertTrue(OrganizationSettings.load().logo)

    def test_the_uploaded_favicon_is_used_when_there_is_one(self):
        self.client.post(
            self.url,
            self.base_payload(favicon=make_image_bytes(name="icon.png")),
        )

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, 'rel="icon"')
        self.assertContains(response, OrganizationSettings.load().favicon.url)
        self.assertNotContains(response, "data:image/svg+xml")

    def test_a_generated_favicon_stands_in_when_there_is_not(self):
        """A page that names no icon makes the browser ask for
        /favicon.ico by itself, which is a 404 on every page load - so
        there is always a link, and without an upload it is an inline SVG
        in the organisation's own colour."""

        self.assertFalse(OrganizationSettings.load().favicon)

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, 'rel="icon"')
        self.assertContains(response, "data:image/svg+xml")

    def test_the_generated_favicon_carries_the_organisation_colour(self):
        self.client.post(self.url, self.base_payload(primary_color="#7c3aed"))

        response = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn("%237c3aed", response)

        # And its text colour is the readable one for that background, by
        # the same rule the rest of the chrome uses.
        self.assertIn(
            "%23" + OrganizationSettings.load().display_on_primary.lstrip("#"),
            response,
        )

    def test_the_public_catalogue_gets_one_too(self):
        self.client.logout()

        response = self.client.get(reverse("public_book_list"))

        self.assertContains(response, 'rel="icon"')


class ColorHelperTests(TestCase):

    def test_hex_to_rgb_triplet(self):
        self.assertEqual(hex_to_rgb_triplet("#0d6efd"), "13, 110, 253")
        self.assertEqual(hex_to_rgb_triplet("#fff"), "255, 255, 255")
        self.assertEqual(hex_to_rgb_triplet(""), "")
        self.assertEqual(hex_to_rgb_triplet("nope"), "")

    def test_readable_foreground_picks_a_contrasting_colour(self):
        # Saturated/dark brand colours keep white text, matching both
        # Bootstrap's convention and the static default in style.css.
        for dark in ("#0d6efd", "#000000", "#198754", "#6c757d"):
            with self.subTest(color=dark):
                self.assertEqual(readable_foreground(dark), "#fff")

        # Pale colours flip to black — this is the case that would otherwise
        # be unreadable.
        for pale in ("#ffff00", "#ffffff", "#e5e7eb", "#ffd6e7"):
            with self.subTest(color=pale):
                self.assertEqual(readable_foreground(pale), "#000")

    def test_readable_foreground_handles_unusable_input(self):
        self.assertEqual(readable_foreground(""), "#fff")

    def test_lifted_for_dark_clears_wcag_aa_on_the_dark_ground(self):
        """A brand colour is chosen against a light page. On the dark
        theme's near-black ground the default indigo is 2.99:1, where AA
        wants 4.5 - so links and the outline buttons that borrow the link
        colour read a lifted version of it."""

        for colour in ("#4f46e5", "#0d6efd", "#198754", "#000000", "#7c3aed",
                       "#dc3545", "#212529"):
            with self.subTest(color=colour):
                triplet = lifted_for_dark(colour)
                channels = tuple(int(part) for part in triplet.split(", "))

                self.assertGreaterEqual(
                    _contrast(channels, DARK_GROUND), MINIMUM_CONTRAST)

    def test_lifted_for_dark_leaves_a_pale_colour_alone(self):
        """Nothing to lift: a colour that already reads on the dark ground
        comes back unchanged, so a light brand is not washed out."""

        self.assertEqual(lifted_for_dark("#ffffff"), "255, 255, 255")
        self.assertEqual(lifted_for_dark("#ffc107"), "255, 193, 7")

    def test_lifted_for_dark_stays_recognisably_the_same_hue(self):
        """It mixes towards white rather than substituting a colour, so the
        channel ordering - which is what makes indigo indigo - survives."""

        red, green, blue = (
            int(part) for part in lifted_for_dark("#4f46e5").split(", "))

        self.assertGreater(blue, red)
        self.assertGreater(red, green)

    def test_lifted_for_dark_handles_unusable_input(self):
        self.assertEqual(lifted_for_dark(""), "")
        self.assertEqual(lifted_for_dark("nope"), "")

    def test_the_page_carries_the_lifted_channels_when_colours_are_set(self):
        make_branding(primary_color="#4f46e5")

        make_user(username="lift_admin", password="pass12345", role="Admin")
        self.client.login(username="lift_admin", password="pass12345")

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "--brand-primary-rgb-on-dark:")
        self.assertContains(
            response,
            "--brand-primary-rgb-on-dark: %s"
            % OrganizationSettings.load().display_primary_rgb_on_dark,
        )
        self.assertEqual(readable_foreground("nope"), "#fff")
