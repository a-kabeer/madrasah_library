"""Accessibility, checked in the browser rather than argued about.

What this holds, and what each one cost when it was not held:

  * **Contrast.** Every foreground/background token pair the app actually
    puts together, in both themes, against WCAG AA. 374 nodes across 35
    pages were failing: the sidebar's own section headings at 2.34:1, its
    nav labels at 4.34, every table heading and breadcrumb link at 4.34,
    Bootstrap's outline buttons at 3.28-4.48, and - because
    `--bs-link-color-rgb` was never set while `--bs-link-color` was -
    every link in the application, which was Bootstrap's #0d6efd rather
    than the organisation's colour at all.

  * **Nesting.** No element with a widget role may contain something
    focusable. The list rows carried `role="button"` and `tabindex="0"`
    while containing the Edit and Delete buttons, so a screen reader was
    told each row was a button and then found buttons inside it. Twelve
    pages. The row is a row again and its first cell carries a real link.

  * **Heading order.** Levels may not skip. Pages went `h1` to `h5`
    because a card heading was chosen for its size rather than its level;
    they are `h2`/`h3` with a `.h5` class now, which looks identical.

  * **Names.** Every control has an accessible name, including the
    icon-only ones - which is most of the row actions.

A full axe-core pass runs too, when axe-core is available: put
`axe.min.js` where AXE_CORE_PATH points, or run
`npm pack axe-core && tar xzf axe-core-*.tgz` and pass
`AXE_CORE_PATH=package/axe.min.js`. Skipped, not failed, without it - the
four checks above are the ones that regressed, and they need no
dependency.
"""

import os
import pathlib
import unittest
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from library.tests.browser import BrowserTestCase, PASSWORD
from library.tests.helpers import (
    make_user, make_book, make_volume, make_content, make_location, make_shelf,
    make_copy, make_borrower, make_loan, make_author, make_category,
    make_publisher,
)

AXE_CORE_PATH = os.environ.get("AXE_CORE_PATH", "")

PAGES = (
    "library_home", "book_list", "book_copy_list", "shelf_list",
    "location_list", "author_list", "category_list", "publisher_list",
    "borrower_list", "loan_list", "reservation_list", "suggestion_list",
    "user_list", "activity_log_list", "book_content_list", "book_volume_list",
    "circulation_dashboard", "circulation_issue", "circulation_return_lookup",
    "inventory_session_list", "notification_list", "reports_home",
    "report_overdue", "report_popular", "report_inventory", "report_borrowers",
    "report_circulation", "report_condition", "analytics",
    "permissions_matrix", "profile", "branding_settings",
)

# The token pairs the app puts together. Each entry is
# (label, foreground custom property, background custom property).
TOKEN_PAIRS = (
    ("sidebar nav label", "--sidebar-text", "--surface-chrome"),
    ("sidebar section heading", "--sidebar-section", "--surface-chrome"),
    ("sidebar active entry", "--sidebar-text-strong", "--surface-chrome"),
    ("body text on a card", "--text-primary", "--surface-card"),
    ("body text on the page", "--text-primary", "--surface-page"),
    ("secondary text on a card", "--text-secondary", "--surface-card"),
    ("secondary text on the page", "--text-secondary", "--surface-page"),
    ("table heading", "--text-secondary", "--surface-inset"),
    ("body text in a raised panel", "--text-primary", "--surface-raised"),
)

# Resolve a custom property to an rgb triple and score the pair, in the
# page, so that the values tested are the ones the browser computes -
# including var() chains and the per-theme overrides.
CONTRAST = """(pairs) => {
    const root = document.documentElement;
    const style = getComputedStyle(root);
    const probe = document.createElement('span');
    document.body.appendChild(probe);

    function rgb(value) {
        probe.style.color = 'rgb(0, 0, 0)';
        probe.style.color = value;
        const computed = getComputedStyle(probe).color;
        const parts = computed.match(/[\\d.]+/g);
        return parts ? parts.slice(0, 3).map(Number) : null;
    }

    function luminance(channels) {
        const linear = channels.map(function (raw) {
            const part = raw / 255;
            return part <= 0.03928
                ? part / 12.92
                : Math.pow((part + 0.055) / 1.055, 2.4);
        });
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
    }

    const out = [];
    pairs.forEach(function (pair) {
        const label = pair[0];
        const fg = rgb(style.getPropertyValue(pair[1]).trim());
        const bg = rgb(style.getPropertyValue(pair[2]).trim());
        if (!fg || !bg) {
            out.push([label, null, 'unresolved']);
            return;
        }
        const a = luminance(fg);
        const b = luminance(bg);
        const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
        out.push([label, Math.round(ratio * 100) / 100,
                  'rgb(' + fg.join(',') + ') on rgb(' + bg.join(',') + ')']);
    });
    probe.remove();
    return out;
}"""

WIDGET_WITH_FOCUSABLE = """() => {
    const widgets = 'button,[role=button],[role=link],[role=checkbox],'
        + '[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch]';
    const focusable = 'a[href],button,input,select,textarea,[tabindex]';
    const out = [];
    document.querySelectorAll(widgets).forEach(function (el) {
        if (el.querySelector(focusable)) {
            out.push(el.tagName.toLowerCase() + '.' +
                     (el.className || '').toString().slice(0, 40));
        }
    });
    return out.slice(0, 6);
}"""

HEADING_SKIPS = """() => {
    const out = [];
    let previous = 0;
    document.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(function (el) {
        // A heading that is not rendered is not in the outline. The two
        // shared dialogs sit in every page's markup with their titles
        // inside, closed; counting those would report a skip on every
        // page for markup nobody can see. A `visually-hidden` heading
        // still has a box and still counts, which is right - a screen
        // reader reads it.
        if (!el.getClientRects().length) { return; }
        const level = Number(el.tagName.slice(1));
        if (previous && level > previous + 1) {
            out.push('h' + previous + ' -> h' + level + ': ' +
                     (el.textContent || '').replace(/\\s+/g, ' ')
                       .trim().slice(0, 40));
        }
        previous = level;
    });
    return out;
}"""

UNNAMED_CONTROLS = """() => {
    const out = [];
    const controls = document.querySelectorAll(
        'a[href],button,input:not([type=hidden]),select,textarea');
    controls.forEach(function (el) {
        if (el.disabled || el.type === 'hidden') { return; }
        const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
        const named = text
            || el.getAttribute('aria-label')
            || el.getAttribute('title')
            || (el.getAttribute('aria-labelledby') &&
                document.getElementById(el.getAttribute('aria-labelledby')))
            || (el.id && document.querySelector('label[for="' + el.id + '"]'))
            || el.closest('label')
            || el.getAttribute('placeholder');
        if (!named) {
            out.push(el.tagName.toLowerCase() +
                     (el.type ? '[' + el.type + ']' : '') + ' ' +
                     (el.outerHTML || '').replace(/\\s+/g, ' ').slice(0, 70));
        }
    });
    return out.slice(0, 6);
}"""


class TokenContrast(BrowserTestCase):
    """The colours, in both themes, at the source."""

    MINIMUM = 4.5

    def setUp(self):
        make_user(username="contrast_admin", password=PASSWORD, role="Admin")

    def test_every_token_pair_meets_wcag_aa_in_both_themes(self):
        page, problems = self.watched_page()
        self.sign_in(page, "contrast_admin")
        self.at(page, "library_home")

        for theme in ("light", "dark"):
            page.evaluate(
                "t => document.documentElement.setAttribute("
                "'data-bs-theme', t)", theme)
            page.wait_for_timeout(60)

            scored = page.evaluate(CONTRAST, [list(p) for p in TOKEN_PAIRS])

            for label, ratio, detail in scored:
                with self.subTest(theme=theme, pair=label):
                    self.assertIsNotNone(
                        ratio, "%s: %s did not resolve" % (label, detail))
                    self.assertGreaterEqual(
                        ratio, self.MINIMUM,
                        "%s in %s is %s:1 (%s), under WCAG AA's %s"
                        % (label, theme, ratio, detail, self.MINIMUM),
                    )

        self.assertEqual(problems, [])
        page.context.close()

    def test_a_link_takes_the_organisations_colour(self):
        """`--bs-link-color` was set and `--bs-link-color-rgb` was not, and
        Bootstrap 5.3 compiles `a` from the second - so every link in the
        app was Bootstrap's blue whatever the organisation configured."""

        page, problems = self.watched_page()
        self.sign_in(page, "contrast_admin")
        self.at(page, "reports_home")

        colour = page.evaluate("""() => {
            const link = document.querySelector('#mainContent a:not(.btn)');
            return link ? getComputedStyle(link).color : null;
        }""")

        self.assertIsNotNone(colour, "no plain link on the reports page")
        self.assertNotEqual(
            colour, "rgb(13, 110, 253)",
            "links are still Bootstrap's own blue, not the brand colour",
        )

        page.context.close()


class StructureAndNames(BrowserTestCase):
    """Nesting, heading order and accessible names, on every page."""

    def setUp(self):
        make_user(username="a11y_admin", password=PASSWORD, role="Admin")

        author = make_author(name="Imam Nawawi")
        category = make_category(name="Fiqh")
        publisher = make_publisher(name="Dar al-Minhaj")
        self.location = make_location(name="Main Hall")
        self.shelf = make_shelf(location=self.location, shelf_code="A-1")

        for index in range(4):
            book = make_book(title="Riyad as-Salihin %d" % index,
                             author=author, category=category,
                             publisher=publisher)
            volume = make_volume(book=book, volume_number=1,
                                 title="Juz %d" % index)
            make_content(volume=volume, title="Bab al-Ikhlas")
            copy = make_copy(volume=volume, shelf=self.shelf,
                             copy_code="RS-%04d" % index)

            if index == 0:
                self.book, self.volume = book, volume

            if index < 2:
                make_loan(
                    copy=copy,
                    borrower=make_borrower(name="Talib %d" % index),
                    issue_date=timezone.now().date() - timedelta(days=30),
                    due_date=timezone.now().date() - timedelta(days=16),
                )

    def pages(self):
        urls = [(name, reverse(name)) for name in PAGES]
        urls += [
            ("book_detail", reverse("book_detail", args=[self.book.id])),
            ("shelf_detail", reverse("shelf_detail", args=[self.shelf.id])),
            ("location_detail",
             reverse("location_detail", args=[self.location.id])),
            ("public_book_detail",
             reverse("public_book_detail", args=[self.book.id])),
        ]

        return urls

    def sweep(self, script, message):
        page, problems = self.watched_page()
        self.sign_in(page, "a11y_admin")

        for name, path in self.pages():
            page.goto(self.live_server_url + path)
            page.wait_for_load_state("networkidle")

            with self.subTest(page=name):
                self.assertEqual(page.evaluate(script), [],
                                 "%s: %s" % (name, message))

        page.context.close()

    def test_nothing_interactive_is_nested_inside_something_interactive(self):
        self.sweep(WIDGET_WITH_FOCUSABLE,
                   "an element with a widget role contains something "
                   "focusable, so a screen reader is told it is one control "
                   "and then finds others inside it")

    def test_no_heading_level_is_skipped(self):
        self.sweep(HEADING_SKIPS,
                   "the heading outline skips a level, so the page reads as "
                   "though a section is missing")

    def test_every_control_has_a_name(self):
        self.sweep(UNNAMED_CONTROLS,
                   "a control has no accessible name, so it is announced as "
                   "just its type")


@unittest.skipUnless(AXE_CORE_PATH and pathlib.Path(AXE_CORE_PATH).exists(),
                     "axe-core is not available; set AXE_CORE_PATH")
class TheWholeAxeSuite(StructureAndNames):
    """Everything else axe-core knows about, when it is installed."""

    TAGS = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice")

    def test_no_violations_in_either_theme(self):
        source = pathlib.Path(AXE_CORE_PATH).read_text()
        run = """async (tags) => {
            const results = await axe.run(document, {
                resultTypes: ['violations'],
                runOnly: {type: 'tag', values: tags}
            });
            return results.violations.map(v => v.impact + ' ' + v.id + ': ' +
                v.nodes.length + ' node(s), e.g. ' +
                (v.nodes[0] ? v.nodes[0].target.join(' ') : ''));
        }"""

        page, problems = self.watched_page()
        self.sign_in(page, "a11y_admin")

        for name, path in self.pages():
            page.goto(self.live_server_url + path)
            page.wait_for_load_state("networkidle")

            for theme in ("light", "dark"):
                page.evaluate(
                    "t => document.documentElement.setAttribute("
                    "'data-bs-theme', t)", theme)
                page.wait_for_timeout(50)
                page.add_script_tag(content=source)

                with self.subTest(page=name, theme=theme):
                    self.assertEqual(
                        page.evaluate(run, list(self.TAGS)), [],
                        "%s in %s" % (name, theme),
                    )

        page.context.close()
