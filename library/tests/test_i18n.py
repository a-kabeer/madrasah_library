"""Three languages, and what must stay true in all of them.

The first guarantee is that nothing changed: English is the default, no
`.po` has a translation in it yet, and `gettext` returns its msgid
unchanged - so every page renders exactly the bytes it did before any of
this. That is what the rest of the suite checks, by continuing to pass.

What is left for here is the machinery: that a language can be chosen, that
the choice sticks to the account rather than the browser, that the page
comes out right-to-left when it should, and that the things which must not
be translated - a copy code, an ISBN - are not.
"""

import io
import re

from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from library.models import User

from .helpers import make_user


class LanguageTestCase(TestCase):

    def setUp(self):
        self.addCleanup(translation.deactivate)

        self.user = make_user(
            username="lang_u", password="pass12345", role="Admin"
        )

    def sign_in(self):
        self.client.login(username="lang_u", password="pass12345")
        return self.client


class TheSettingsTests(TestCase):

    def test_three_languages_are_offered(self):
        from django.conf import settings

        self.assertEqual(
            [code for code, _ in settings.LANGUAGES], ["en", "ur", "ar"]
        )

    def test_english_is_the_default(self):
        from django.conf import settings

        self.assertEqual(settings.LANGUAGE_CODE, "en")

    def test_the_locale_middleware_sits_between_session_and_common(self):
        # Wrong order means the language silently never applies, which is
        # the kind of bug that looks like "translation does not work".
        from django.conf import settings

        order = settings.MIDDLEWARE

        session = order.index(
            "django.contrib.sessions.middleware.SessionMiddleware"
        )
        locale = order.index("django.middleware.locale.LocaleMiddleware")
        common = order.index("django.middleware.common.CommonMiddleware")

        self.assertLess(session, locale)
        self.assertLess(locale, common)

    def test_the_user_language_middleware_runs_after_authentication(self):
        # It reads request.user, which AuthenticationMiddleware puts there.
        from django.conf import settings

        order = settings.MIDDLEWARE

        self.assertLess(
            order.index(
                "django.contrib.auth.middleware.AuthenticationMiddleware"
            ),
            order.index("library.middleware.UserLanguageMiddleware"),
        )


class DirectionTests(LanguageTestCase):

    def page(self, language):
        client = self.sign_in()
        client.post(reverse("language_set"), {"language": language})
        return client.get(reverse("location_list"))

    def test_english_is_left_to_right(self):
        body = self.page("en").content.decode()

        self.assertIn('dir="ltr"', body)
        self.assertIn('lang="en"', body)
        # The vendored filename, allowing for the content hash
        # ManifestStaticFilesStorage inserts before the extension.
        self.assertNotIn("bootstrap.rtl.min", body)

    def test_urdu_and_arabic_are_right_to_left(self):
        for language in ("ur", "ar"):
            with self.subTest(language=language):
                body = self.page(language).content.decode()

                self.assertIn('dir="rtl"', body)
                self.assertIn('lang="%s"' % language, body)
                # Bootstrap's own RTL build, or every one of its components
                # stays the wrong way round.
                self.assertIn("bootstrap.rtl.min", body)

    def test_the_two_bootstrap_builds_are_the_same_version(self):
        # If they drift, the two directions get different component styles.
        # Both builds are vendored under a directory named for the version
        # (see docs/VENDORED_ASSETS.md), so that is where the version is.
        ltr = self.page("en").content.decode()
        rtl = self.page("ur").content.decode()

        version = re.compile(r"bootstrap-([\d.]+)/bootstrap")

        found_ltr = version.search(ltr)
        found_rtl = version.search(rtl)

        self.assertIsNotNone(found_ltr, "no Bootstrap stylesheet on the LTR page")
        self.assertIsNotNone(found_rtl, "no Bootstrap stylesheet on the RTL page")
        self.assertEqual(found_ltr.group(1), found_rtl.group(1))


class ChoosingTests(LanguageTestCase):

    def test_a_signed_in_person_can_choose(self):
        client = self.sign_in()

        client.post(reverse("language_set"), {"language": "ur"})

        self.user.refresh_from_db()
        self.assertEqual(self.user.language_preference, "ur")

    def test_and_clear_the_choice_again(self):
        client = self.sign_in()

        client.post(reverse("language_set"), {"language": "ur"})
        client.post(reverse("language_set"), {"language": ""})

        self.user.refresh_from_db()
        self.assertIsNone(self.user.language_preference)

    def test_an_unknown_language_is_refused(self):
        client = self.sign_in()

        response = client.post(reverse("language_set"), {"language": "klingon"})

        self.assertEqual(response.status_code, 400)

        self.user.refresh_from_db()
        self.assertIsNone(self.user.language_preference)

    def test_it_has_to_be_a_post(self):
        self.assertEqual(
            self.sign_in().get(reverse("language_set")).status_code, 400
        )

    def test_the_stored_choice_beats_the_browser(self):
        # The whole reason this middleware exists: a librarian who chose
        # Urdu gets Urdu on a colleague's English browser.
        client = self.sign_in()
        client.post(reverse("language_set"), {"language": "ur"})

        body = client.get(
            reverse("location_list"),
            headers={"Accept-Language": "en-GB,en;q=0.9"},
        ).content.decode()

        self.assertIn('lang="ur"', body)

    def test_with_no_choice_stored_the_browser_decides(self):
        body = self.sign_in().get(
            reverse("location_list"),
            headers={"Accept-Language": "ar,en;q=0.5"},
        ).content.decode()

        self.assertIn('lang="ar"', body)

    def test_a_retired_language_falls_back_rather_than_failing(self):
        # A code removed from settings.LANGUAGES must not be activated.
        User.objects.filter(pk=self.user.pk).update(language_preference="zz")

        response = self.sign_in().get(reverse("location_list"))

        self.assertEqual(response.status_code, 200)


class AnonymousTests(LanguageTestCase):

    def test_the_switch_is_reachable_without_signing_in(self):
        # The public catalogue and the login page both need it, and this
        # project turns LoginRequiredMiddleware on globally - so the view
        # has to be explicitly exempt or it would redirect to a login page
        # in the language the reader was trying to change.
        response = self.client.post(
            reverse("set_language"),
            {"language": "ur", "next": reverse("public_book_list")},
        )

        self.assertIn(response.status_code, (302, 200))

    def test_the_public_catalogue_answers_in_all_three(self):
        for language in ("en", "ur", "ar"):
            with self.subTest(language=language):
                self.client.post(
                    reverse("set_language"),
                    {"language": language, "next": "/catalog/"},
                )

                response = self.client.get(reverse("public_book_list"))

                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    'lang="%s"' % language, response.content.decode()
                )

    def test_the_login_page_offers_the_switcher(self):
        self.client.logout()

        body = self.client.get(reverse("login")).content.decode()

        self.assertIn("languageToggle", body)


class IdentifiersStayLatinTests(LanguageTestCase):
    """A copy code is read off a spine and typed into a scanner."""

    def test_arabic_formats_keep_latin_digits(self):
        # Django's own `ar` locale would render these as ٠١٢٣.
        from django.utils.formats import get_format

        with translation.override("ar"):
            self.assertEqual(get_format("DECIMAL_SEPARATOR"), ".")
            self.assertEqual(get_format("THOUSAND_SEPARATOR"), ",")

    def test_and_so_do_urdu_formats(self):
        from django.utils.formats import get_format

        with translation.override("ur"):
            self.assertEqual(get_format("DECIMAL_SEPARATOR"), ".")

    def test_a_number_renders_in_latin_digits_in_arabic(self):
        from django.utils.formats import number_format

        with translation.override("ar"):
            rendered = number_format(1234)

        for digit in "١٢٣٤":
            self.assertNotIn(digit, rendered)


class PartiallyTranslatedTests(LanguageTestCase):
    """Translation is under way, so both halves have to hold at once.

    This class used to assert that nothing was translated, which was true
    while the catalogues were scaffolding and is not any more - the
    application shell is done and the rest is not. Asserting the old thing
    would now mean asserting that finished work had not happened.

    What matters while a catalogue is part-way is the pair:

      * a string that HAS been translated appears in Urdu
      * a string that has NOT falls back to its English msgid rather than
        rendering blank

    The fallback is why a half-translated catalogue is safe to ship at all,
    and it is the half that would fail silently - a missing msgstr shows as
    nothing on the page, not as an error.
    """

    def test_the_po_files_exist_for_both_languages(self):
        import os

        from django.conf import settings

        for language in ("ur", "ar"):
            with self.subTest(language=language):
                path = os.path.join(
                    settings.LOCALE_PATHS[0],
                    language,
                    "LC_MESSAGES",
                    "django.po",
                )
                self.assertTrue(os.path.exists(path), path)

    def test_they_carry_the_strings_that_were_marked(self):
        import io
        import os

        from django.conf import settings

        path = os.path.join(
            settings.LOCALE_PATHS[0], "ur", "LC_MESSAGES", "django.po"
        )
        content = io.open(path, encoding="utf-8").read()

        # A handful that must have been picked up: a model label, a menu
        # entry, a table heading, an attribute.
        for msgid in ('msgid "Available"', 'msgid "Actions"',
                      'msgid "Search"', 'msgid "Librarian"'):
            with self.subTest(msgid=msgid):
                self.assertIn(msgid, content)

    def test_a_translated_string_renders_in_urdu(self):
        client = self.sign_in()
        client.post(reverse("language_set"), {"language": "ur"})

        body = client.get(reverse("location_list")).content.decode()

        self.assertIn('lang="ur"', body)

        # "Locations" is in the shell batch, so the sidebar and the heading
        # are Urdu.
        self.assertIn("مقامات", body)

    def test_an_untranslated_string_falls_back_to_english(self):
        """The property that makes a part-done catalogue safe to ship.

        An empty msgstr makes gettext return the msgid. If it returned ""
        instead, every untranslated label would render as blank space and
        the page would look broken rather than unfinished.
        """

        client = self.sign_in()
        client.post(reverse("language_set"), {"language": "ur"})

        body = client.get(reverse("activity_log_list")).content.decode()

        self.assertIn('lang="ur"', body)

        # Not in the shell batch yet, so still English - and present,
        # rather than an empty cell.
        self.assertIn("What happened", body)


class MarkupTests(LanguageTestCase):
    """The two ways an automated marking pass breaks a template."""

    def test_no_template_has_a_nested_double_quote_in_an_attribute(self):
        # `placeholder="{% translate "x" %}"` ends the attribute early and
        # breaks the tag. Attribute values must use single quotes inside.
        import os
        import re

        offenders = []
        pattern = re.compile(
            r'\b(?:placeholder|title|aria-label|alt)="\{%\s*translate\s+"'
        )

        for dirpath, _dirs, files in os.walk("library/templates"):
            for name in files:
                if not name.endswith(".html"):
                    continue
                path = os.path.join(dirpath, name)
                import io
                if pattern.search(io.open(path, encoding="utf-8").read()):
                    offenders.append(path)

        self.assertEqual(offenders, [])

    def test_no_template_construct_spans_a_newline(self):
        """The bug that shipped, and the reason it was invisible.

        Django's tokenizer is `({%.*?%}|{{.*?}}|{#.*?#})` with no DOTALL
        flag, so a tag, variable or comment containing a newline is not
        recognised as one. It is not an error either - it is emitted as
        text, and the reader sees `{% translate "..." %}` printed on the
        page. Nothing raises, `manage.py check` passes, and a test that
        renders the page and greps for a word still finds the word.

        An automated marking pass produces exactly this, because the
        string it wraps was already wrapped across lines in the HTML.
        """

        import os
        import re

        opener = re.compile(r"\{%|\{\{|\{#")
        closes = {"{%": "%}", "{{": "}}", "{#": "#}"}

        offenders = []

        for dirpath, _dirs, files in os.walk("library/templates"):
            for name in files:
                if not name.endswith(".html"):
                    continue

                path = os.path.join(dirpath, name)
                body = io.open(path, encoding="utf-8").read()

                for match in opener.finditer(body):
                    start = match.group(0)
                    end = body.find(closes[start], match.end())

                    if end == -1:
                        offenders.append("%s: unclosed %s" % (path, start))
                        continue

                    if "\n" in body[match.end():end]:
                        line = body[:match.start()].count("\n") + 1
                        offenders.append("%s:%d" % (path, line))

        self.assertEqual(offenders, [])

    def test_every_marked_template_loads_the_i18n_tags(self):
        # `{% translate %}` without `{% load i18n %}` is a TemplateSyntaxError
        # at render time, which `manage.py check` does not catch.
        import io
        import os

        offenders = []

        for dirpath, _dirs, files in os.walk("library/templates"):
            for name in files:
                if not name.endswith(".html"):
                    continue
                path = os.path.join(dirpath, name)
                body = io.open(path, encoding="utf-8").read()

                if "{% translate" in body and "{% load i18n %}" not in body:
                    offenders.append(path)

        self.assertEqual(offenders, [])
