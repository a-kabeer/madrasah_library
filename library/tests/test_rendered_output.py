"""No page may show its own template syntax.

This project has shipped this class of bug three times, and each time
everything green stayed green:

  * committed conflict markers, which render as `<<<<<<< HEAD`;
  * `{% endblocktranslate}` - a missing `%`, which *is* a
    TemplateSyntaxError and took two pages to a 500;
  * `placeholder="{% translate 'Search book volumes...' %"` - a `%}`
    mistyped as `%"`, which is not an error at all. Django's lexer only
    matches a tag whose closing delimiter is on the same line, so the
    whole thing is emitted as ordinary text: the template compiles, the
    view answers 200, a test that greps the page for a word still finds
    the word, and the reader sees `{% translate '...' %` printed in the
    search box of the Book Volumes page.

  * `{% include ... with report_title="{{ heading }}" %}` - template
    syntax inside a quoted argument is not evaluated, so two report pages
    printed the seven characters `{{ heading }}` as their own heading.

Only the last two were caught, and only by looking at what came out. So
this looks at what comes out: every page a signed-in Admin can reach,
every dialog fragment, and both public pages, checked for a delimiter that
should have been consumed.

Deliberately not a template scan - `library/tests/test_i18n.py` already
scans the sources for a construct spanning a newline, and that is the
first bug above but not the fourth. A rendered page is the only place both
show up.
"""

import re
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from library.tests.helpers import (
    make_user, make_book, make_volume, make_content, make_location, make_shelf,
    make_copy, make_borrower, make_loan, make_author, make_category,
    make_publisher,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

DELIMITERS = ("{%", "{{", "{#")

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

# The dialogs, which are fragments and never appear in a whole-page render.
FRAGMENTS = (
    "book_add", "book_copy_add", "borrower_add", "author_add", "category_add",
    "publisher_add", "location_add", "shelf_add", "book_volume_add",
    "book_content_add", "suggestion_add", "user_add",
)


def leftovers(html):
    """Every delimiter still in the page, with a little of what follows."""

    found = []

    for delimiter in DELIMITERS:
        at = html.find(delimiter)

        while at != -1:
            found.append(html[at:at + 70].replace("\n", " "))
            at = html.find(delimiter, at + 1)

    return found


@override_settings(CACHES=LOCMEM)
class NothingLeaksTemplateSyntax(TestCase):

    def setUp(self):
        self.admin = make_user(username="render_admin", password="pass12345",
                               role="Admin")
        self.client.force_login(self.admin)

        author = make_author(name="Ibn Hajar")
        category = make_category(name="Hadith")
        publisher = make_publisher(name="Dar al-Salam")
        self.location = make_location(name="Reading Room")
        self.shelf = make_shelf(location=self.location, shelf_code="R-1")

        self.book = make_book(title="Fath al-Bari", author=author,
                              category=category, publisher=publisher)
        self.volume = make_volume(book=self.book, volume_number=1,
                                  title="Juz 1")
        self.content = make_content(volume=self.volume, title="Kitab al-Ilm")
        self.copy = make_copy(volume=self.volume, shelf=self.shelf,
                              copy_code="FB-0001")
        self.borrower = make_borrower(name="Abdullah")
        self.loan = make_loan(
            copy=self.copy, borrower=self.borrower,
            issue_date=timezone.now().date() - timedelta(days=20),
            due_date=timezone.now().date() - timedelta(days=6),
        )

    def check(self, label, response):
        self.assertEqual(response.status_code, 200, label)

        html = response.content.decode(errors="replace")

        self.assertEqual(
            leftovers(html), [],
            "%s renders template syntax: %s" % (label, leftovers(html)[:3]),
        )

    def test_no_whole_page_shows_a_delimiter(self):
        for name in PAGES:
            with self.subTest(page=name):
                self.check(name, self.client.get(reverse(name)))

    def test_no_detail_page_shows_a_delimiter(self):
        pages = (
            ("book_detail", (self.book.id,)),
            ("book_volume_detail", (self.volume.id,)),
            ("location_detail", (self.location.id,)),
            ("shelf_detail", (self.shelf.id,)),
            ("borrower_detail", (self.borrower.id,)),
        )

        for name, args in pages:
            with self.subTest(page=name):
                self.check(name, self.client.get(reverse(name, args=args)))

    def test_no_dialog_fragment_shows_a_delimiter(self):
        for name in FRAGMENTS:
            with self.subTest(fragment=name):
                self.check(
                    name,
                    self.client.get(reverse(name), {"modal": "1"},
                                    HTTP_HX_REQUEST="true"),
                )

    def test_no_public_page_shows_a_delimiter(self):
        self.client.logout()

        self.check("public_book_list",
                   self.client.get(reverse("public_book_list")))
        self.check("public_book_detail",
                   self.client.get(reverse("public_book_detail",
                                           args=[self.book.id])))

    def test_no_page_carries_a_conflict_marker(self):
        """The first of the three, and the one a delimiter check misses.

        Anchored to the start of a line, because a decorative comment rule
        (`<!-- ===== KPIs ===== -->`, of which the analytics page has three)
        is not a conflict marker and a bare substring test calls it one.
        """

        markers = re.compile(r"(?m)^(?:<{7} |={7}$|>{7} )")

        for name in PAGES:
            html = self.client.get(reverse(name)).content.decode(
                errors="replace")

            with self.subTest(page=name):
                self.assertIsNone(
                    markers.search(html),
                    "%s carries a merge conflict marker" % name,
                )
