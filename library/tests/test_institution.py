"""Institution metadata: who this installation belongs to.

Four new columns on the row the application already configures itself
through, and no second settings system. Three properties carry the tests.

The name is not new. `organization_settings.name` has always been the
institution's name - it is what the sidebar, the page titles, the login
page, the report headers and the label sheets read - so there is nothing
here that stores a second one, and nothing that could disagree with it.
What is new is the name in Arabic, the type, the address and the website.

All of it is optional. An installation that has configured none of it must
render exactly as it did before these columns existed, which is checked at
both ends: the properties on the model, and the pages that show them.

And none of it costs a query. `branding` is already loaded once per request
and cached by the context processor, so every display added here reads a
value that was going to be fetched anyway - which is why the query counts
below are equalities against the same page before the metadata was set.
"""

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from library.models import OrganizationSettings
from library.views import normalize_website

from .helpers import (
    make_author,
    make_book,
    make_branding,
    make_copy,
    make_location,
    make_shelf,
    make_user,
    make_volume,
)


# A real Arabic name, so "renders correctly" means the bytes survive the
# round trip through the form, the database and the template - not that
# some ASCII placeholder did.
ARABIC_NAME = "مكتبة النور"


class InstitutionTestCase(TestCase):

    def setUp(self):
        cache.clear()

        self.url = reverse("branding_settings")

        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def payload(self, **overrides):
        """The branding form as the page posts it.

        The institution fields are on this form, not a form of their own:
        the institution's name, logo, email and phone are all one answer to
        "who is this?", and they save together. The borrowing policy is the
        separate form, because lending rules are a different kind of state.
        """

        data = {
            "name": "Al Noor Library",
            "primary_color": "",
            "secondary_color": "",
            "accent_color": "",
            "contact_email": "office@alnoor.test",
            "contact_phone": "0300-1234567",
            "footer_text": "Serving since 1990",
            "name_arabic": ARABIC_NAME,
            "institution_type": "Madrasah",
            "address": "12 Mall Road\nLahore",
            "website": "https://alnoor.test",
        }
        data.update(overrides)

        return data


# ==========================================================================
# SAVING
# ==========================================================================


class SavingTests(InstitutionTestCase):

    def test_admin_can_save_every_field(self):
        response = self.client.post(self.url, self.payload())

        self.assertRedirects(response, self.url)

        saved = OrganizationSettings.load()

        self.assertEqual(saved.name, "Al Noor Library")
        self.assertEqual(saved.name_arabic, ARABIC_NAME)
        self.assertEqual(saved.institution_type, "Madrasah")
        self.assertEqual(saved.address, "12 Mall Road\nLahore")
        self.assertEqual(saved.website, "https://alnoor.test")

    def test_admin_can_see_what_was_saved(self):
        self.client.post(self.url, self.payload())

        response = self.client.get(self.url)

        self.assertContains(response, ARABIC_NAME)
        self.assertContains(response, "Madrasah")
        self.assertContains(response, "12 Mall Road")
        self.assertContains(response, "https://alnoor.test")

    def test_the_form_offers_every_field(self):
        response = self.client.get(self.url)

        for field in (
            "name_arabic", "institution_type", "address", "website"
        ):
            with self.subTest(field=field):
                self.assertContains(response, 'name="%s"' % field)

    def test_everything_optional_may_be_left_blank(self):
        response = self.client.post(
            self.url,
            self.payload(
                name_arabic="",
                institution_type="",
                address="",
                website="",
            ),
        )

        self.assertRedirects(response, self.url)

        saved = OrganizationSettings.load()

        self.assertEqual(saved.name_arabic, "")
        self.assertEqual(saved.institution_type, "")
        self.assertEqual(saved.address, "")
        self.assertEqual(saved.website, "")
        self.assertFalse(saved.has_institution_details)

    def test_a_partly_filled_form_saves_what_it_has(self):
        response = self.client.post(
            self.url,
            self.payload(institution_type="", address="", website=""),
        )

        self.assertRedirects(response, self.url)

        saved = OrganizationSettings.load()

        self.assertEqual(saved.name_arabic, ARABIC_NAME)
        self.assertEqual(saved.institution_type, "")
        self.assertTrue(saved.has_institution_details)

    def test_it_is_still_one_row(self):
        # Not a second settings table, and not a second row in this one.
        self.client.post(self.url, self.payload())
        self.client.post(self.url, self.payload(name_arabic="جامعة"))

        self.assertEqual(OrganizationSettings.objects.count(), 1)
        self.assertEqual(
            OrganizationSettings.load().pk,
            OrganizationSettings.SINGLETON_ID,
        )

    def test_saving_metadata_keeps_the_branding(self):
        make_branding(
            primary_color="#ff8800",
            secondary_color="#123456",
            footer_text="Serving since 1990",
        )

        self.client.post(
            self.url,
            self.payload(
                primary_color="#ff8800",
                secondary_color="#123456",
            ),
        )

        saved = OrganizationSettings.load()

        self.assertEqual(saved.primary_color, "#ff8800")
        self.assertEqual(saved.secondary_color, "#123456")
        self.assertEqual(saved.footer_text, "Serving since 1990")

    def test_saving_metadata_keeps_a_logo(self):
        make_branding(logo="branding/existing-logo.png")

        self.client.post(self.url, self.payload())

        self.assertEqual(
            OrganizationSettings.load().logo.name,
            "branding/existing-logo.png",
        )

    def test_saving_metadata_keeps_the_borrowing_policy(self):
        # The rules live on the same row and are posted by a different
        # form. Saving one must not blank the other.
        make_branding(
            loan_period_days=21,
            max_active_loans=4,
            max_renewals=2,
            block_when_overdue=True,
        )

        self.client.post(self.url, self.payload())

        saved = OrganizationSettings.load()

        self.assertEqual(saved.loan_period_days, 21)
        self.assertEqual(saved.max_active_loans, 4)
        self.assertEqual(saved.max_renewals, 2)
        self.assertTrue(saved.block_when_overdue)

    def test_saving_the_policy_keeps_the_metadata(self):
        # And the other way round, which is the half that would break if
        # the policy form ever started writing a fresh row.
        self.client.post(self.url, self.payload())

        self.client.post(
            self.url,
            {
                "section": "policy",
                "loan_period_days": "21",
                "max_active_loans": "",
                "max_renewals": "",
            },
        )

        saved = OrganizationSettings.load()

        self.assertEqual(saved.loan_period_days, 21)
        self.assertEqual(saved.name_arabic, ARABIC_NAME)
        self.assertEqual(saved.address, "12 Mall Road\nLahore")

    def test_the_activity_log_still_records_one_update(self):
        from library.models import ActivityLog

        self.client.post(self.url, self.payload())

        self.assertEqual(
            ActivityLog.objects.filter(
                entity_type="OrganizationSettings"
            ).count(),
            1,
        )


class BlankInstallationTests(TestCase):
    """An installation that has never opened the settings page."""

    def setUp(self):
        cache.clear()

        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_the_defaults_carry_no_metadata(self):
        settings_obj = OrganizationSettings.load()

        self.assertIsNone(settings_obj.pk)
        self.assertEqual(settings_obj.name_arabic, "")
        self.assertEqual(settings_obj.institution_type, "")
        self.assertEqual(settings_obj.display_address, "")
        self.assertEqual(settings_obj.print_contact_line, "")
        self.assertFalse(settings_obj.has_institution_details)

    def test_the_name_still_falls_back(self):
        self.assertEqual(
            OrganizationSettings.load().display_name, "Madrasah Library"
        )

    def test_the_pages_still_render(self):
        for name in ("dashboard", "book_list", "profile", "branding_settings"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Madrasah Library")

    def test_a_row_with_no_metadata_is_valid(self):
        settings_obj = make_branding(name="Al Noor Library")

        self.assertEqual(settings_obj.name_arabic, "")
        self.assertEqual(settings_obj.print_contact_line, "")


# ==========================================================================
# VALIDATION
# ==========================================================================


class ValidationTests(InstitutionTestCase):

    def test_an_invalid_email_is_rejected(self):
        response = self.client.post(
            self.url, self.payload(contact_email="not-an-address")
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid email address")
        self.assertEqual(OrganizationSettings.objects.count(), 0)

    def test_a_valid_email_is_accepted(self):
        response = self.client.post(
            self.url, self.payload(contact_email="office@alnoor.test")
        )

        self.assertRedirects(response, self.url)
        self.assertEqual(
            OrganizationSettings.load().contact_email, "office@alnoor.test"
        )

    def test_a_blank_email_is_accepted(self):
        # Optional means optional: "nobody has said" is not "wrong".
        response = self.client.post(self.url, self.payload(contact_email=""))

        self.assertRedirects(response, self.url)
        self.assertEqual(OrganizationSettings.load().contact_email, "")

    def test_an_invalid_website_is_rejected(self):
        response = self.client.post(
            self.url, self.payload(website="https://not a website")
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid web address")
        self.assertEqual(OrganizationSettings.objects.count(), 0)

    def test_an_unsupported_scheme_is_rejected(self):
        # A typed scheme is left alone rather than replaced, so this is
        # refused rather than quietly turned into something else.
        response = self.client.post(
            self.url, self.payload(website="ftp://alnoor.test")
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid web address")

    def test_a_website_without_a_scheme_is_accepted_and_completed(self):
        response = self.client.post(
            self.url, self.payload(website="alnoor.test")
        )

        self.assertRedirects(response, self.url)
        self.assertEqual(
            OrganizationSettings.load().website, "https://alnoor.test"
        )

    def test_a_blank_website_is_accepted(self):
        response = self.client.post(self.url, self.payload(website=""))

        self.assertRedirects(response, self.url)
        self.assertEqual(OrganizationSettings.load().website, "")

    def test_normalize_website_leaves_a_stated_scheme_alone(self):
        self.assertEqual(
            normalize_website("http://alnoor.test"), "http://alnoor.test"
        )
        self.assertEqual(
            normalize_website("ftp://alnoor.test"), "ftp://alnoor.test"
        )

    def test_normalize_website_leaves_nothing_as_nothing(self):
        self.assertEqual(normalize_website(""), "")
        self.assertEqual(normalize_website("   "), "")

    def test_a_rejected_form_keeps_what_was_typed(self):
        response = self.client.post(
            self.url,
            self.payload(
                contact_email="not-an-address",
                name_arabic=ARABIC_NAME,
                institution_type="Jamia",
            ),
        )

        self.assertContains(response, ARABIC_NAME)
        self.assertContains(response, "Jamia")

    def test_a_rejected_form_writes_nothing(self):
        make_branding(name="Al Noor Library", name_arabic="سابق")

        self.client.post(
            self.url,
            self.payload(name_arabic="جديد", contact_email="broken"),
        )

        self.assertEqual(OrganizationSettings.load().name_arabic, "سابق")

    def test_a_bad_colour_is_still_rejected(self):
        # The metadata checks were added ahead of the file checks and after
        # the colours; the existing order still holds.
        response = self.client.post(
            self.url, self.payload(primary_color="not-a-colour")
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "hex code")


# ==========================================================================
# DISPLAY
# ==========================================================================


class ShellDisplayTests(InstitutionTestCase):

    def test_the_configured_name_is_in_the_sidebar(self):
        make_branding(name="Al Noor Library")

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "Al Noor Library")

    def test_the_name_falls_back_when_unset(self):
        make_branding(name="")

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "Madrasah Library")

    def test_the_arabic_name_appears_beside_it(self):
        make_branding(name="Al Noor Library", name_arabic=ARABIC_NAME)

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "Al Noor Library")
        self.assertContains(response, ARABIC_NAME)

    def test_the_arabic_name_is_marked_up_for_its_direction(self):
        make_branding(name_arabic=ARABIC_NAME)

        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn('dir="rtl"', body)
        self.assertIn('lang="ar"', body)

    def test_nothing_is_shown_when_no_arabic_name_is_set(self):
        make_branding(name="Al Noor Library")

        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertNotIn("brand-name-arabic", body)

    def test_unicode_survives_the_round_trip(self):
        self.client.post(self.url, self.payload())

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.charset.lower(), "utf-8")
        self.assertContains(response, ARABIC_NAME)

    def test_the_page_title_still_uses_the_one_name(self):
        make_branding(name="Al Noor Library", name_arabic=ARABIC_NAME)

        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn("<title>", body)
        self.assertIn("Al Noor Library", body)

    def test_an_htmx_navigation_is_unaffected(self):
        # The shell is not rendered for a main-content navigation, so the
        # brand block is simply not part of the answer.
        make_branding(name="Al Noor Library", name_arabic=ARABIC_NAME)

        body = self.client.get(
            reverse("dashboard"),
            headers={"hx-request": "true", "hx-target": "mainContent"},
        ).content.decode()

        self.assertNotIn("brand-name-arabic", body)


class PrintableOutputTests(InstitutionTestCase):
    """The two places this application puts on paper."""

    def setUp(self):
        super().setUp()

        self.shelf = make_shelf(location=make_location(name="Main Hall"))
        self.author = make_author("Abu Yusuf")
        self.book = make_book(title="Kitab al-Kharaj", author=self.author)
        self.volume = make_volume(book=self.book, volume_number=1, title="")
        self.copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-700001"
        )

    def labels(self):
        return self.client.get(
            reverse("book_copy_labels"), {"copy": self.copy.id}
        )

    def report(self):
        return self.client.get(reverse("report_inventory"))

    def test_labels_carry_the_configured_name(self):
        make_branding(name="Al Noor Library")

        self.assertContains(self.labels(), "Al Noor Library")

    def test_labels_fall_back_to_the_default_name(self):
        # Nothing configured at all: the sheet says what it always said.
        self.assertContains(self.labels(), "Madrasah Library")

    def test_the_label_footer_carries_the_institution(self):
        make_branding(
            name="Al Noor Library",
            name_arabic=ARABIC_NAME,
            address="12 Mall Road\nLahore",
            contact_phone="0300-1234567",
        )

        response = self.labels()

        self.assertContains(response, ARABIC_NAME)
        self.assertContains(response, "12 Mall Road, Lahore")
        self.assertContains(response, "0300-1234567")

    def test_the_report_letterhead_carries_the_institution(self):
        make_branding(
            name="Al Noor Library",
            name_arabic=ARABIC_NAME,
            address="12 Mall Road\nLahore",
            website="https://alnoor.test",
        )

        response = self.report()

        self.assertContains(response, "Al Noor Library")
        self.assertContains(response, ARABIC_NAME)
        self.assertContains(response, "12 Mall Road, Lahore")
        self.assertContains(response, "https://alnoor.test")

    def test_the_report_letterhead_is_just_the_name_when_nothing_is_set(self):
        response = self.report()

        self.assertContains(response, "Madrasah Library")
        self.assertContains(response, "generated")

    def test_the_label_itself_is_unchanged(self):
        # Only the sheet footer gained anything. The label is 63.5 x 33.9 mm
        # and still carries the institution's name, the title, the author
        # and the code - nothing was added to it.
        make_branding(name="Al Noor Library", name_arabic=ARABIC_NAME)

        response = self.labels()

        for expected in (
            "Al Noor Library",
            "Kitab al-Kharaj",
            "Abu Yusuf",
            "LIB-700001",
        ):
            with self.subTest(shows=expected):
                self.assertContains(response, expected)

    def test_no_hardcoded_institution_text_was_left_behind(self):
        # The two printable templates name no institution of their own:
        # everything they print comes off the configured row.
        import io as _io
        import os

        from django.conf import settings as django_settings

        base = os.path.join(
            django_settings.BASE_DIR, "library", "templates", "library"
        )

        for path in (
            os.path.join(base, "book_copy_labels.html"),
            os.path.join(base, "partials", "report_header.html"),
            os.path.join(base, "partials", "institution_print_line.html"),
        ):
            with self.subTest(template=os.path.basename(path)):

                source = _io.open(path, encoding="utf-8").read()

                self.assertNotIn("Madrasah Library", source)


class PropertyTests(TestCase):
    """What the templates actually read, decided once on the model."""

    def test_the_address_is_flattened_for_one_line(self):
        settings_obj = OrganizationSettings(
            address="12 Mall Road\n\n  Lahore  \nPakistan"
        )

        self.assertEqual(
            settings_obj.display_address, "12 Mall Road, Lahore, Pakistan"
        )

    def test_a_blank_address_flattens_to_nothing(self):
        self.assertEqual(
            OrganizationSettings(address="  \n \n ").display_address, ""
        )

    def test_the_contact_line_joins_only_what_is_set(self):
        settings_obj = OrganizationSettings(
            address="12 Mall Road",
            contact_phone="",
            website="https://alnoor.test",
        )

        self.assertEqual(
            settings_obj.print_contact_line,
            "12 Mall Road · https://alnoor.test",
        )

    def test_the_contact_line_is_empty_when_nothing_is_set(self):
        self.assertEqual(OrganizationSettings().print_contact_line, "")

    def test_the_email_is_not_on_the_printed_line(self):
        settings_obj = OrganizationSettings(
            contact_email="office@alnoor.test"
        )

        self.assertEqual(settings_obj.print_contact_line, "")

    def test_has_institution_details_notices_each_field(self):
        for field, value in (
            ("name_arabic", ARABIC_NAME),
            ("institution_type", "Madrasah"),
            ("address", "12 Mall Road"),
            ("website", "https://alnoor.test"),
        ):
            with self.subTest(field=field):
                self.assertTrue(
                    OrganizationSettings(
                        **{field: value}
                    ).has_institution_details
                )


# ==========================================================================
# PERMISSIONS
# ==========================================================================


class PermissionTests(TestCase):
    """Admin configures the institution; nobody else does."""

    def setUp(self):
        cache.clear()

        self.url = reverse("branding_settings")

        make_branding(name="Al Noor Library", name_arabic=ARABIC_NAME)

        for role in ("Admin", "Librarian", "Assistant"):
            make_user(
                username=role.lower() + "_i",
                password="pass12345",
                role=role,
            )

    def sign_in(self, role):
        self.client.login(
            username=role.lower() + "_i", password="pass12345"
        )

    def payload(self):
        return {
            "name": "Renamed By Force",
            "primary_color": "",
            "secondary_color": "",
            "accent_color": "",
            "contact_email": "",
            "contact_phone": "",
            "footer_text": "",
            "name_arabic": "مُغيَّر",
            "institution_type": "School",
            "address": "",
            "website": "",
        }

    def test_admin_may_open_the_settings(self):
        self.sign_in("Admin")

        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_admin_may_save(self):
        self.sign_in("Admin")

        self.client.post(self.url, self.payload())

        self.assertEqual(
            OrganizationSettings.load().institution_type, "School"
        )

    def test_a_librarian_is_refused_the_page(self):
        self.sign_in("Librarian")

        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_an_assistant_is_refused_the_page(self):
        self.sign_in("Assistant")

        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_a_librarian_post_changes_nothing(self):
        self.sign_in("Librarian")

        response = self.client.post(self.url, self.payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(OrganizationSettings.load().name_arabic, ARABIC_NAME)

    def test_an_assistant_post_changes_nothing(self):
        self.sign_in("Assistant")

        response = self.client.post(self.url, self.payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(OrganizationSettings.load().name_arabic, ARABIC_NAME)

    def test_every_role_still_sees_the_institution_where_it_is_shown(self):
        # Reading is not configuring. The sidebar names the institution for
        # everybody; only Admin may change what it says.
        for role in ("Admin", "Librarian", "Assistant"):
            with self.subTest(role=role):

                self.sign_in(role)

                response = self.client.get(reverse("dashboard"))

                self.assertContains(response, "Al Noor Library")
                self.assertContains(response, ARABIC_NAME)

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.client.logout()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_an_anonymous_post_is_sent_to_sign_in_and_changes_nothing(self):
        self.client.logout()

        response = self.client.post(self.url, self.payload())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(OrganizationSettings.load().name_arabic, ARABIC_NAME)

    def test_the_sign_in_page_still_shows_the_library_name(self):
        # Unchanged behaviour: the login page has always been branded, and
        # the context processor still must not touch request.user.
        self.client.logout()

        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Al Noor Library")


# ==========================================================================
# QUERY EFFICIENCY
# ==========================================================================


class QueryCountTests(TestCase):
    """The metadata is free: it rides on a row already being fetched."""

    def setUp(self):
        cache.clear()

        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        self.shelf = make_shelf(location=make_location(name="Main Hall"))
        self.author = make_author("Abu Yusuf")
        self.book = make_book(title="Kitab al-Kharaj", author=self.author)
        self.volume = make_volume(book=self.book, volume_number=1, title="")

        self.copies = [
            make_copy(
                volume=self.volume,
                shelf=self.shelf,
                copy_code="LIB-8000%02d" % index,
            )
            for index in range(20)
        ]

    def count_for(self, url, params=None, **kwargs):
        with CaptureQueriesContext(connection) as captured:
            self.client.get(url, params or {}, **kwargs)

        return len(captured.captured_queries)

    def configure(self):
        make_branding(
            name="Al Noor Library",
            name_arabic=ARABIC_NAME,
            institution_type="Madrasah",
            address="12 Mall Road\nLahore",
            website="https://alnoor.test",
            contact_phone="0300-1234567",
        )

        cache.clear()

    def assert_unchanged(self, url, params=None, **kwargs):
        """The page costs the same before and after the metadata is set."""

        before = self.count_for(url, params, **kwargs)

        self.configure()

        after = self.count_for(url, params, **kwargs)

        self.assertEqual(before, after)

    def test_the_dashboard_is_unchanged(self):
        self.assert_unchanged(reverse("dashboard"))

    def test_an_unrelated_list_page_is_unchanged(self):
        self.assert_unchanged(reverse("book_list"))

    def test_the_copy_list_is_unchanged(self):
        self.assert_unchanged(reverse("book_copy_list"))

    def test_an_htmx_navigation_is_unchanged(self):
        self.assert_unchanged(
            reverse("book_list"),
            headers={"hx-request": "true", "hx-target": "mainContent"},
        )

    def test_a_list_fragment_is_unchanged(self):
        # The copy list's own results fragment - not a navigation, and not
        # a full page. It must not have gained a settings query either.
        self.assert_unchanged(
            reverse("book_copy_list"),
            {"partial": "results"},
            headers={"hx-request": "true"},
        )

    def test_a_combobox_endpoint_is_unchanged(self):
        self.assert_unchanged(
            reverse("borrower_list"),
            {"search": "Ab", "combobox": "1", "allow_create": "0"},
            headers={"hx-request": "true"},
        )

    def test_the_label_sheet_does_not_query_per_label(self):
        # Twenty labels, and the institution is read once for the sheet -
        # not once per row.
        self.configure()

        one = self.count_for(
            reverse("book_copy_labels"), {"copy": self.copies[0].id}
        )

        many = self.count_for(
            reverse("book_copy_labels"),
            {"copy": [copy.id for copy in self.copies]},
        )

        self.assertEqual(one, many)

    def test_the_settings_page_reads_the_row_itself(self):
        # The one place that must not read a cached copy: an admin who has
        # just saved has to see what they saved.
        self.configure()

        with CaptureQueriesContext(connection) as captured:
            self.client.get(reverse("branding_settings"))

        reads = [
            query["sql"]
            for query in captured.captured_queries
            if "organization_settings" in query["sql"]
        ]

        self.assertTrue(reads)
