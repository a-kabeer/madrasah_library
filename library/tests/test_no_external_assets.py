"""Nothing the pages need may come from somewhere else.

Bootstrap, Bootstrap Icons and htmx used to be loaded from
`cdn.jsdelivr.net` and `unpkg.com`, with no `integrity` attribute on any of
the six references. Two consequences, and the first is fatal rather than
degrading:

  * Without Bootstrap's CSS there is no layout, and without htmx no link in
    the shell navigates - `hx-boost` is what turns a sidebar entry into a
    swap of `#mainContent`. A madrasah on an intermittent, filtered or
    entirely local network therefore did not get a slower app; it got an
    unusable one.

  * With no subresource integrity, whatever those hosts returned ran with
    the privileges of whoever was signed in. A pinned version is not a
    pinned file.

They are vendored now (see docs/VENDORED_ASSETS.md). This test is what
stops them drifting back: a `<link>` or `<script>` pointing at another host
fails it, and so does a `{% static %}` path that no file answers - which is
the way an upgrade goes wrong, since a stale path is a silently missing
stylesheet rather than an error.

No browser needed. It reads the templates and the static finders.
"""

import pathlib
import re

from django.contrib.staticfiles import finders
from django.test import SimpleTestCase

TEMPLATES = pathlib.Path("library/templates")

# Anything that fetches: a stylesheet, a script, an image, a font, an
# iframe. `http://` and `https://` and protocol-relative `//host/...`.
EXTERNAL = re.compile(
    r"""(?:src|href)\s*=\s*["'](?P<url>(?:https?:)?//[^"']+)["']""",
    re.IGNORECASE,
)

# A URL in prose - a comment, a documentation link, an XML namespace - is
# not a fetch. Only tags that load something count, so the pattern above is
# applied to <link>, <script>, <img>, <iframe> and <source> only.
LOADING_TAGS = re.compile(
    r"<(?:link|script|img|iframe|source|audio|video)\b[^>]*>",
    re.IGNORECASE | re.DOTALL,
)

STATIC_PATH = re.compile(r"""\{%\s*static\s+['"]([^'"]+)['"]""")


def templates():
    return sorted(TEMPLATES.rglob("*.html"))


class NothingIsFetchedFromAnotherHost(SimpleTestCase):

    def test_no_template_loads_from_an_external_host(self):
        offenders = []

        for path in templates():
            body = path.read_text()

            for tag in LOADING_TAGS.finditer(body):
                found = EXTERNAL.search(tag.group(0))

                if found:
                    line = body[:tag.start()].count("\n") + 1
                    offenders.append("%s:%d %s"
                                     % (path, line, found.group("url")[:70]))

        self.assertEqual(
            offenders, [],
            "these templates fetch from another host; vendor them under "
            "library/static/library/vendor/ instead: %s" % offenders,
        )

    def test_every_static_path_a_template_names_exists(self):
        missing = []

        for path in templates():
            body = path.read_text()

            for found in STATIC_PATH.finditer(body):
                asset = found.group(1)

                if finders.find(asset) is None:
                    line = body[:found.start()].count("\n") + 1
                    missing.append("%s:%d %s" % (path, line, asset))

        self.assertEqual(
            missing, [],
            "these {%% static %%} paths have no file behind them: %s"
            % missing,
        )


class TheVendoredFilesAreAllThere(SimpleTestCase):
    """The seven files docs/VENDORED_ASSETS.md lists, by the paths the
    templates ask for."""

    EXPECTED = (
        "library/vendor/bootstrap-5.3.8/bootstrap.min.css",
        "library/vendor/bootstrap-5.3.8/bootstrap.rtl.min.css",
        "library/vendor/bootstrap-5.3.8/bootstrap.bundle.min.js",
        "library/vendor/bootstrap-icons-1.13.1/bootstrap-icons.min.css",
        "library/vendor/bootstrap-icons-1.13.1/fonts/bootstrap-icons.woff2",
        "library/vendor/bootstrap-icons-1.13.1/fonts/bootstrap-icons.woff",
        "library/vendor/htmx-2.0.8/htmx.min.js",
    )

    def test_each_one_is_findable(self):
        for asset in self.EXPECTED:
            with self.subTest(asset=asset):
                self.assertIsNotNone(finders.find(asset))

    def test_both_directions_of_bootstrap_are_present(self):
        """The RTL build is what Urdu and Arabic use. It must be the same
        version as the LTR one, or the two directions get different
        component styles - and it must actually be the flipped build, not a
        copy of the LTR one under an RTL name."""

        ltr = pathlib.Path(finders.find(
            "library/vendor/bootstrap-5.3.8/bootstrap.min.css")).read_text()
        rtl = pathlib.Path(finders.find(
            "library/vendor/bootstrap-5.3.8/bootstrap.rtl.min.css")).read_text()

        for label, body in (("ltr", ltr), ("rtl", rtl)):
            with self.subTest(build=label):
                self.assertRegex(body[:300], r"Bootstrap\s+v5\.3\.8")

        # `me-*` is "margin-end": right in LTR, left in RTL. Cheapest
        # one-property proof that the flip happened.
        self.assertIn(".me-1{margin-right", ltr)
        self.assertIn(".me-1{margin-left", rtl)

    def test_the_icon_css_still_points_at_the_fonts_beside_it(self):
        css = pathlib.Path(finders.find(
            "library/vendor/bootstrap-icons-1.13.1/bootstrap-icons.min.css"
        )).read_text()

        self.assertIn("fonts/bootstrap-icons.woff2", css)
        self.assertIn("fonts/bootstrap-icons.woff", css)
