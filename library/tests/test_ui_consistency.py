"""One app, not several that happen to share a database.

Three things this checks, each of which had drifted:

  * **One date format.** 37 templates write `|date:"j M Y"` and 11 write
    `j M Y, H:i`, but 17 places printed a date with no filter at all and
    fell through to Django's own `en` format, `N j, Y`. So the same loan's
    issue date read "9 Sep 2026" on the loans list and "Sept. 9, 2026" on
    the book's page, and the dashboard's activity feed disagreed with the
    activity log it links to. Fixed at the root, in
    config/formats/en/formats.py, rather than by adding a seventeenth
    filter - which is why the test asks what the page says rather than
    what the template contains.

  * **One look for one action.** A row's Edit button is
    `btn-outline-warning btn-sm` in fifteen tables and was
    `btn-outline-secondary btn-sm` in the sixteenth, sitting beside the
    same red Delete as everywhere else.

  * **Something on screen when there is nothing to show.** 23 table bodies
    have no `{% empty %}` clause, which looks alarming until you check:
    every one is inside an `{% if %}` that renders a whole-card empty state
    instead, which is better. So the invariant to hold is not "every loop
    has an empty clause" but "no page ever shows column headings over
    nothing", and that is what this asserts.
"""

import pathlib
import re
from datetime import date

from django.test import TestCase, override_settings
from django.urls import reverse

from library.tests.helpers import (
    make_user, make_book, make_volume, make_location, make_shelf, make_copy,
    make_borrower, make_loan, make_author, make_category, make_publisher,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

EMPTY_TBODY = re.compile(r"<tbody[^>]*>\s*</tbody>", re.S)

LIST_PAGES = (
    "library_home", "book_list", "book_copy_list", "shelf_list",
    "location_list", "author_list", "category_list", "publisher_list",
    "borrower_list", "loan_list", "reservation_list", "suggestion_list",
    "user_list", "activity_log_list", "book_content_list", "book_volume_list",
    "circulation_dashboard", "circulation_issue", "circulation_return_lookup",
    "inventory_session_list", "notification_list", "reports_home",
    "report_overdue", "report_popular", "report_inventory", "report_borrowers",
    "report_circulation", "report_condition", "analytics",
    "permissions_matrix", "public_book_list",
)


@override_settings(CACHES=LOCMEM)
class NothingShowsHeadingsOverNothing(TestCase):

    def setUp(self):
        self.admin = make_user(username="empty_admin", password="pass12345",
                               role="Admin")
        self.client.force_login(self.admin)

    def test_every_list_page_survives_an_empty_library(self):
        for name in LIST_PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))

                self.assertEqual(response.status_code, 200)

                html = response.content.decode(errors="replace")

                self.assertIsNone(
                    EMPTY_TBODY.search(html),
                    "%s renders a table with headings and no rows and no "
                    "message" % name,
                )


@override_settings(CACHES=LOCMEM)
class OneDateFormat(TestCase):
    """A date is the same shape wherever it is read."""

    ISSUED = date(2026, 3, 9)
    DUE = date(2026, 3, 23)

    # What the house format makes of them, and what Django's own `en`
    # format would have made of them.
    HOUSE = ("9 Mar 2026", "23 Mar 2026")
    DJANGO_DEFAULT = ("March 9, 2026", "March 23, 2026")

    def setUp(self):
        self.admin = make_user(username="date_admin", password="pass12345",
                               role="Admin")
        self.client.force_login(self.admin)

        author = make_author(name="Ibn Kathir")
        self.book = make_book(title="Tafsir Ibn Kathir", author=author,
                              category=make_category(name="Tafsir"),
                              publisher=make_publisher(name="Dar Taybah"))
        volume = make_volume(book=self.book, volume_number=1, title="Juz 1")
        location = make_location(name="Upper Hall")
        shelf = make_shelf(location=location, shelf_code="U-1")
        copy = make_copy(volume=volume, shelf=shelf, copy_code="TK-0001")
        borrower = make_borrower(name="Yusuf")

        self.loan = make_loan(copy=copy, borrower=borrower,
                              issue_date=self.ISSUED, due_date=self.DUE)

    def pages(self):
        return (
            ("book_detail", reverse("book_detail", args=[self.book.id])),
            ("loan_list", reverse("loan_list")),
            ("report_circulation",
             reverse("report_circulation") + "?start=2026-01-01&end=2026-12-31"),
            ("report_overdue", reverse("report_overdue")),
            ("dashboard", reverse("library_home")),
        )

    def test_no_page_uses_djangos_format_instead_of_the_houses(self):
        for name, url in self.pages():
            html = self.client.get(url).content.decode(errors="replace")

            for wrong in self.DJANGO_DEFAULT:
                with self.subTest(page=name, format=wrong):
                    self.assertNotIn(wrong, html)

    def test_the_pages_that_show_the_loan_show_it_the_house_way(self):
        for name, url in self.pages():
            html = self.client.get(url).content.decode(errors="replace")

            if self.loan.borrower.name not in html:
                continue        # this page does not list the loan at all

            with self.subTest(page=name):
                self.assertTrue(
                    any(shape in html for shape in self.HOUSE),
                    "%s shows the loan but not its dates in `j M Y`" % name,
                )

    def test_the_format_is_the_same_in_every_language(self):
        """A librarian holding a shelf label reads the same date on
        screen. The month name is translated; the order and the digits are
        not."""

        from django.utils import translation
        from django.utils.formats import get_format

        for language in ("en", "ur", "ar"):
            with self.subTest(language=language):
                with translation.override(language):
                    self.assertEqual(get_format("DATE_FORMAT"), "j M Y")
                    self.assertEqual(get_format("NUMBER_GROUPING"), 0)
                    self.assertEqual(get_format("DECIMAL_SEPARATOR"), ".")
                    self.assertEqual(get_format("THOUSAND_SEPARATOR"), ",")


class OneLookForOneAction(TestCase):
    """A row's Edit and Delete buttons look the same in every table."""

    ROW_ACTION_STYLES = {
        "bi-pencil": "btn-outline-warning",
        "bi-trash": "btn-outline-danger",
    }

    def small_buttons(self):
        """Every `btn-sm` anchor whose only content is one icon - which is
        the row-action shape, and not the page-level buttons that carry a
        label."""

        for path in sorted(pathlib.Path("library/templates").rglob("*.html")):
            body = path.read_text()

            for match in re.finditer(r"<a\b[^>]*>", body, re.S):
                tag = match.group(0)
                classes = re.search(r'class="([^"]*)"', tag)

                if not classes or "btn-sm" not in classes.group(1):
                    continue

                end = body.find("</a>", match.end())
                inner = body[match.end():end if end > 0 else match.end()]

                # An icon and at most a visually-hidden label: no visible text.
                stripped = re.sub(r"<[^>]+>", "", inner)
                stripped = re.sub(r"\{%.*?%\}", "", stripped, flags=re.S)

                if stripped.strip():
                    continue

                icon = re.search(r"class=\"bi (bi-[\w-]+)", inner)

                if icon:
                    yield (path, body[:match.start()].count("\n") + 1,
                           icon.group(1), classes.group(1))

    def test_row_edit_and_delete_use_one_style_each(self):
        offenders = []

        for path, line, icon, classes in self.small_buttons():
            wanted = self.ROW_ACTION_STYLES.get(icon)

            if wanted and wanted not in classes:
                offenders.append("%s:%d %s has %r, expected %s"
                                 % (path, line, icon, classes, wanted))

        self.assertEqual(offenders, [], "\n".join(offenders))
