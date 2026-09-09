"""Every page, in a browser, at three widths.

The Django client says a view answered 200. It cannot say the page is
usable: whether a script threw, whether an asset 404ed, whether two
elements ended up sharing an id, or whether the layout pushes the document
sideways on a phone. Those are what this checks, on every page a signed-in
Admin can reach plus both public catalogue pages.

Three widths because the failures differ: a table that scrolls politely at
1440 can widen the whole document at 375, which is what happened to the
users list - a `visually-hidden` label, absolutely positioned and therefore
escaping the scroll container it was inside, made a 375px viewport into a
598px document.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from library.tests.browser import BrowserTestCase, PASSWORD
from library.tests.helpers import (
    make_user, make_book, make_volume, make_content, make_location, make_shelf,
    make_copy, make_borrower, make_loan, make_author, make_category,
    make_publisher,
)

# Every page reachable from the navigation, by url name.
LIST_PAGES = (
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

WIDTHS = (
    (1440, 900, "desktop"),
    (768, 1024, "tablet"),
    (375, 812, "phone"),
)

# A template construct that survived into the page. Django's lexer only
# matches a tag whose closing delimiter is on the same line, so a `%}`
# mistyped as `%"` is emitted as ordinary text: `manage.py check` passes,
# the template compiles, the view answers 200, and the reader sees
# `{% translate '...' %` printed in a search box. That shipped, on the Book
# Volumes page, and this is the cheapest thing that would have caught it.
RAW_TEMPLATE_SYNTAX = """() => {
    const found = [];
    const html = document.documentElement.outerHTML;
    ['{%', '{{', '{#'].forEach(function (opener) {
        let at = html.indexOf(opener);
        while (at !== -1 && found.length < 4) {
            found.push(html.slice(at, at + 60));
            at = html.indexOf(opener, at + 1);
        }
    });
    return found;
}"""

DUPLICATE_IDS = """() => {
    const seen = {};
    const repeated = [];
    document.querySelectorAll('[id]').forEach(function (el) {
        if (seen[el.id]) { repeated.push(el.id); }
        seen[el.id] = true;
    });
    return repeated;
}"""

# What is sticking out past the right edge, innermost first, so a failure
# names the element to fix rather than just the number of pixels.
OVERFLOWING = """() => {
    const limit = document.documentElement.clientWidth;
    const out = [];
    document.querySelectorAll('body *').forEach(function (el) {
        if (el.getBoundingClientRect().right <= limit + 1) { return; }
        for (const child of el.children) {
            if (child.getBoundingClientRect().right > limit + 1) { return; }
        }
        out.push(el.tagName + '.' + (el.className || '').toString().slice(0, 40));
    });
    return out.slice(0, 6);
}"""


class EveryPageInABrowser(BrowserTestCase):

    def setUp(self):
        self.admin = make_user(username="browser_admin", password=PASSWORD,
                               role="Admin")

        author = make_author(name="Imam Bukhari")
        category = make_category(name="Hadith")
        publisher = make_publisher(name="Dar al-Kutub")
        location = make_location(name="Main Hall")
        shelf = make_shelf(location=location, shelf_code="A-1")

        self.location = location
        self.shelf = shelf

        for index in range(6):
            book = make_book(title="Sahih Bukhari Volume Number %d" % index,
                             author=author, category=category,
                             publisher=publisher)
            volume = make_volume(book=book, volume_number=1,
                                 title="Juz %d" % index)
            make_content(volume=volume, title="Kitab al-Iman")
            copy = make_copy(volume=volume, shelf=shelf,
                             copy_code="BK-%04d" % index)

            if index == 0:
                self.book = book
                self.volume = volume

            if index < 3:
                make_loan(
                    copy=copy,
                    borrower=make_borrower(name="Talib ul Ilm %d" % index),
                    issue_date=timezone.now().date() - timedelta(days=30),
                    due_date=timezone.now().date() - timedelta(days=16),
                )

    def pages(self):
        urls = [(name, reverse(name)) for name in LIST_PAGES]
        urls += [
            ("book_detail", reverse("book_detail", args=[self.book.id])),
            ("book_volume_detail",
             reverse("book_volume_detail", args=[self.volume.id])),
            ("location_detail",
             reverse("location_detail", args=[self.location.id])),
            ("shelf_detail", reverse("shelf_detail", args=[self.shelf.id])),
            ("public_book_list", reverse("public_book_list")),
            ("public_book_detail",
             reverse("public_book_detail", args=[self.book.id])),
        ]

        return urls

    def test_no_page_complains_at_any_width(self):
        for width, height, label in WIDTHS:
            page, problems = self.watched_page(
                viewport={"width": width, "height": height})
            self.sign_in(page, "browser_admin")

            for name, path in self.pages():
                with self.subTest(page=name, width=label):
                    mark = len(problems)
                    page.goto(self.live_server_url + path)
                    page.wait_for_load_state("networkidle")

                    self.assertEqual(
                        problems[mark:], [],
                        "%s at %s: %s" % (name, label, problems[mark:]),
                    )

                    self.assertEqual(
                        page.evaluate(DUPLICATE_IDS), [],
                        "%s at %s has repeated element ids" % (name, label),
                    )

                    self.assertEqual(
                        page.evaluate(RAW_TEMPLATE_SYNTAX), [],
                        "%s at %s shows unrendered template syntax"
                        % (name, label),
                    )

                    sideways = page.evaluate(
                        "() => document.documentElement.scrollWidth - "
                        "document.documentElement.clientWidth")

                    self.assertLessEqual(
                        sideways, 1,
                        "%s at %s pushes the document %dpx sideways, via %s"
                        % (name, label, sideways, page.evaluate(OVERFLOWING)),
                    )

            page.context.close()
