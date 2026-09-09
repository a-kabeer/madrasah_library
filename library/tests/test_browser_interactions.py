"""The half of the app that only exists once JavaScript runs.

`hx-boost` on `.app-wrapper` turns every ordinary link inside the shell
into a background fetch swapped into `#mainContent`. That makes the whole
session one document, which is the thing worth testing: a listener attached
to an element that a swap replaces would either stop working or be attached
twice, and neither shows up in a view test. `app.js` guards against both
with `setupEach` (a WeakSet of elements already set up) and `setupOnce` (a
single delegated install), and these check that the guards hold across a
dozen navigations rather than trusting them.

The double-submit test is the sharp one: opening the same dialog twice and
saving twice must create exactly one record each time. Two bound submit
handlers would create two.
"""

from django.urls import reverse

from library.models import Author
from library.tests.browser import BrowserTestCase, PASSWORD
from library.tests.helpers import make_user, make_book, make_author

SIDEBAR_LINKS = """() => Array.from(
    document.querySelectorAll('#sidebar a[href^="/library/"]')
).map(a => a.getAttribute('href'))"""


class NavigatingWithoutReloading(BrowserTestCase):

    def setUp(self):
        make_user(username="nav_admin", password=PASSWORD, role="Admin")

        author = make_author(name="Imam Malik")

        for index in range(3):
            make_book(title="Muwatta %d" % index, author=author)

    def test_the_whole_session_is_one_document(self):
        page, problems = self.watched_page()
        self.sign_in(page, "nav_admin")

        self.at(page, "library_home")
        page.evaluate("window.__sameDocument = true")

        visited = []

        for href in page.evaluate(SIDEBAR_LINKS):
            page.click('#sidebar a[href="%s"]' % href)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(200)
            visited.append((href, page.url.replace(self.live_server_url, "")))

        self.assertGreaterEqual(len(visited), 8,
                                "expected a sidebar worth walking")

        self.assertTrue(
            page.evaluate("() => window.__sameDocument === true"),
            "one of the %d sidebar links reloaded the page instead of "
            "swapping: %s" % (len(visited), visited),
        )

        for asked, arrived in visited:
            with self.subTest(href=asked):
                self.assertTrue(
                    arrived.startswith(asked.split("?")[0]),
                    "clicking %s left the address bar at %s" % (asked, arrived),
                )

        self.assertEqual(problems, [])

        page.context.close()

    def test_a_dialog_saves_once_however_often_it_is_opened(self):
        page, problems = self.watched_page()
        self.sign_in(page, "nav_admin")

        # Reached by navigation, not by URL, so the page under test is one
        # htmx swapped in - which is where a re-bound listener would show.
        self.at(page, "library_home")
        page.click('#sidebar a[href="%s"]' % reverse("author_list"))
        page.wait_for_load_state("networkidle")

        for attempt in (1, 2, 3):
            name = "Dialog Author %d" % attempt

            page.click("[data-form-modal]")
            page.wait_for_selector("#formModal.show", timeout=10000)
            page.wait_for_selector("#formModal input[name=name]", timeout=10000)
            page.fill("#formModal input[name=name]", name)
            page.click("#formModal button[type=submit]")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)

            with self.subTest(attempt=attempt):
                self.assertEqual(
                    Author.objects.filter(name=name).count(), 1,
                    "opening the dialog %d time(s) saved %d copies of %r"
                    % (attempt, Author.objects.filter(name=name).count(), name),
                )

        self.assertEqual(problems, [])

        page.context.close()

    def test_a_table_row_opens_the_shared_dialog(self):
        page, problems = self.watched_page()
        self.sign_in(page, "nav_admin")

        self.at(page, "library_home")
        page.click('#sidebar a[href="%s"]' % reverse("book_list"))
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(400)

        rows = page.locator("[data-book-row], [data-row-primary]")

        self.assertEqual(rows.count(), 3)

        rows.first.click()
        page.wait_for_selector(".modal.show", timeout=10000)

        self.assertTrue(
            page.evaluate("() => !!document.querySelector('.modal.show')")
        )

        # Bootstrap listens for Escape on the dialog itself, and moves focus
        # there on `shown.bs.modal` - which is after the `.show` class
        # appears. Pressing Escape before that goes to the page behind and
        # does nothing, so wait for the focus rather than for a duration.
        page.wait_for_function(
            """() => {
                const m = document.querySelector('.modal.show');
                return !!(m && m.contains(document.activeElement));
            }""",
            timeout=10000,
        )

        page.keyboard.press("Escape")
        page.wait_for_timeout(600)

        self.assertFalse(
            page.evaluate("() => !!document.querySelector('.modal.show')"),
            "Escape left the dialog open",
        )

        self.assertEqual(problems, [])

        page.context.close()


class TheSidebarOnAPhone(BrowserTestCase):

    viewport = {"width": 375, "height": 812}

    def setUp(self):
        make_user(username="phone_admin", password=PASSWORD, role="Admin")

    def test_it_opens_from_the_toggle_and_closes_from_the_overlay(self):
        page, problems = self.watched_page()
        self.sign_in(page, "phone_admin")
        self.at(page, "book_list")

        shown = "() => document.getElementById('sidebar')" \
                ".classList.contains('show')"

        self.assertFalse(page.evaluate(shown), "the sidebar starts open")
        self.assertEqual(page.get_attribute("#menuToggle", "aria-expanded"),
                         "false")

        page.click("#menuToggle")
        page.wait_for_timeout(500)

        self.assertTrue(page.evaluate(shown), "the toggle did not open it")
        self.assertEqual(page.get_attribute("#menuToggle", "aria-expanded"),
                         "true")

        # The overlay covers the whole viewport, but the open sidebar sits
        # on top of its left-hand side, so a click at the centre lands on a
        # nav link. Tap the strip a thumb would actually reach.
        page.click("#sidebarOverlay", position={"x": 340, "y": 500})
        page.wait_for_timeout(600)

        self.assertFalse(page.evaluate(shown),
                         "tapping the overlay did not close it")
        self.assertEqual(page.get_attribute("#menuToggle", "aria-expanded"),
                         "false")

        self.assertEqual(problems, [])

        page.context.close()
