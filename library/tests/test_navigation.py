"""Main-content navigation: one URL, two response shapes.

Every page answers an ordinary request with the whole application shell and
an HTMX navigation with just the main-content region. These tests pin both
shapes, pin what marks a request as a navigation, and pin that the pages
which also swap fragments of themselves still answer those separately.
"""

import io
import os
import re
from urllib.parse import unquote

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from library.models import Book

from library.context_processors import MAIN_CONTENT_ID
from library.tests.helpers import (
    make_book,
    make_borrower,
    make_category,
    make_content,
    make_copy,
    make_loan,
    make_location,
    make_publisher,
    make_shelf,
    make_user,
    make_volume,
)


NAV = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": MAIN_CONTENT_ID}


class MainContentTargetTests(TestCase):
    """base.html has exactly one swap target, and it is stable."""

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_the_layout_carries_the_target_and_the_two_oob_ids(self):
        body = self.client.get(reverse("dashboard")).content.decode()

        for marker in (
            'id="%s"' % MAIN_CONTENT_ID,
            'id="pageHeading"',
            'id="pageBreadcrumbs"',
        ):
            with self.subTest(marker=marker):
                self.assertEqual(body.count(marker), 1)


class ConvertedPageTests(TestCase):

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")
        self.borrower = make_borrower()
        self.location = make_location()

    def urls(self):
        return {
            "dashboard": reverse("dashboard"),
            "borrower list": reverse("borrower_list"),
            "borrower detail": reverse(
                "borrower_detail", args=[self.borrower.id]
            ),
            "location list": reverse("location_list"),
            "location detail": reverse(
                "location_detail", args=[self.location.id]
            ),
        }

    def test_an_ordinary_request_renders_the_whole_shell(self):
        for label, url in self.urls().items():
            with self.subTest(page=label):
                body = self.client.get(url).content.decode()

                self.assertIn("<!DOCTYPE html>", body)
                self.assertIn("sidebar-nav", body)
                self.assertIn('id="%s"' % MAIN_CONTENT_ID, body)

    def test_a_navigation_returns_only_the_main_content_fragment(self):
        for label, url in self.urls().items():
            with self.subTest(page=label):
                response = self.client.get(url, **NAV)
                body = response.content.decode()

                self.assertEqual(response.status_code, 200)
                self.assertNotIn("<!DOCTYPE html>", body)
                self.assertNotIn("sidebar-nav", body)
                # The target is not sent back inside itself.
                self.assertNotIn('id="%s"' % MAIN_CONTENT_ID, body)
                # Shared chrome is left alone.
                self.assertNotIn('id="globalModal"', body)

    def test_a_navigation_brings_the_topbar_with_it_out_of_band(self):
        # The heading and breadcrumbs live outside the swapped region, so
        # without these they would keep describing the previous page.
        for label, url in self.urls().items():
            with self.subTest(page=label):
                body = self.client.get(url, **NAV).content.decode()

                self.assertIn('id="pageHeading" hx-swap-oob="true"', body)
                self.assertIn('id="pageBreadcrumbs" hx-swap-oob="true"', body)
                self.assertIn("<title>", body)

    def test_a_navigation_is_smaller_than_the_page(self):
        for label, url in self.urls().items():
            with self.subTest(page=label):
                full = len(self.client.get(url).content)
                fragment = len(self.client.get(url, **NAV).content)

                self.assertLess(fragment, full)

    def test_the_breadcrumb_trail_survives_a_navigation(self):
        body = self.client.get(
            reverse("borrower_detail", args=[self.borrower.id]), **NAV
        ).content.decode()

        self.assertIn(reverse("borrower_list"), body)
        self.assertIn(self.borrower.name, body)


class NavigationMarkerTests(TestCase):
    """What counts as a navigation, and what does not."""

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_the_htmx_header_alone_is_not_a_navigation(self):
        # Every combobox, modal and list-fragment request in this project
        # sends HX-Request. Reading that as navigation would answer them
        # with the wrong body.
        body = self.client.get(
            reverse("borrower_list"), HTTP_HX_REQUEST="true"
        ).content.decode()

        self.assertIn("<!DOCTYPE html>", body)

    def test_a_request_aimed_elsewhere_is_not_a_navigation(self):
        body = self.client.get(
            reverse("borrower_list"),
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="bookResults",
        ).content.decode()

        self.assertIn("<!DOCTYPE html>", body)

    def test_the_target_header_alone_is_not_a_navigation(self):
        body = self.client.get(
            reverse("borrower_list"), HTTP_HX_TARGET=MAIN_CONTENT_ID
        ).content.decode()

        self.assertIn("<!DOCTYPE html>", body)


class FragmentPageTests(TestCase):
    """Pages that also swap parts of themselves answer three shapes.

    The book and copy lists replace their own table on a search, a filter,
    a sort or a page change. That is a different request from a navigation
    and has to stay one: the fragment must not arrive wrapped in the page,
    and a navigation must not arrive as a bare table.
    """

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")
        make_book()

    def urls(self):
        return {
            "book list": reverse("book_list"),
            "copy list": reverse("book_copy_list"),
            "loan list": reverse("loan_list"),
            "shelf list": reverse("shelf_list"),
            "volume list": reverse("book_volume_list"),
        }

    def test_they_render_the_whole_shell_for_an_ordinary_request(self):
        for label, url in self.urls().items():
            with self.subTest(page=label):
                body = self.client.get(url).content.decode()

                self.assertIn("<!DOCTYPE html>", body)
                self.assertIn("sidebar-nav", body)

    def test_they_answer_a_navigation_with_the_region_alone(self):
        for label, url in self.urls().items():
            with self.subTest(page=label):
                response = self.client.get(url, **NAV)
                body = response.content.decode()

                self.assertEqual(response.status_code, 200)
                self.assertNotIn("<!DOCTYPE html>", body)
                self.assertNotIn("sidebar-nav", body)
                self.assertIn('id="pageHeading" hx-swap-oob="true"', body)

    def test_a_navigation_outranks_an_inherited_partial(self):
        # The results container declares `partial` for the links inside it,
        # and htmx hands that down to every boosted link within - so a
        # plain link to another page arrives carrying it. Asking for the
        # main-content region is asking for a page.
        for label, url in (
            ("book list", reverse("book_list")),
            ("copy list", reverse("book_copy_list")),
        ):
            with self.subTest(page=label):
                body = self.client.get(
                    url + "?partial=results", **NAV
                ).content.decode()

                self.assertIn('id="pageHeading" hx-swap-oob="true"', body)

    def test_a_link_inside_the_results_container_states_its_own_target(self):
        # The way in and out of the archive is a plain link that happens to
        # live inside the fragment container, so it would otherwise inherit
        # a target aimed at the table it was clicked from.
        book = Book.objects.first()
        book.archived_at = timezone.now()
        book.save(update_fields=["archived_at"])

        body = self.client.get(reverse("book_list")).content.decode()

        link = re.search(
            r'<a\s[^>]*href="[^"]*archived[^"]*"[^>]*>', body, re.S
        )

        self.assertIsNotNone(link)
        self.assertIn('hx-target="#%s"' % MAIN_CONTENT_ID, link.group(0))

    def test_the_book_list_fragments_are_untouched(self):
        for partial in ("results", "browser"):
            with self.subTest(partial=partial):
                response = self.client.get(
                    reverse("book_list") + "?partial=%s" % partial,
                    HTTP_HX_REQUEST="true",
                    HTTP_HX_TARGET="bookResults",
                )
                body = response.content.decode()

                self.assertEqual(response.status_code, 200)
                self.assertNotIn("<!DOCTYPE html>", body)
                self.assertNotIn("sidebar-nav", body)

    def test_the_copy_list_fragment_is_untouched(self):
        response = self.client.get(
            reverse("book_copy_list") + "?partial=results",
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="copyResults",
        )
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<!DOCTYPE html>", body)


class NavigationPermissionTests(TestCase):
    """A navigation is still an ordinary request as far as access goes."""

    def setUp(self):
        self.borrower = make_borrower()

    def test_an_assistant_is_still_refused(self):
        make_user(username="assistant_u", password="pass12345", role="Assistant")
        self.client.login(username="assistant_u", password="pass12345")

        response = self.client.get(
            reverse("borrower_delete", args=[self.borrower.id]), **NAV
        )

        self.assertEqual(response.status_code, 403)

    def test_a_signed_out_navigation_is_redirected_not_answered(self):
        response = self.client.get(reverse("borrower_list"), **NAV)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_the_sign_in_page_is_a_whole_page_even_under_the_headers(self):
        # A navigation follows its own redirect, so what comes back when
        # the session has gone is the sign-in page, not a 302 anyone can
        # see. app.js recognises it by the doctype and hands it to the
        # browser instead of swapping it into the chrome - which is only
        # possible because the page keeps a doctype under these headers.
        response = self.client.get(
            reverse("borrower_list"), follow=True, **NAV
        )
        body = response.content.decode()

        self.assertIn("<!DOCTYPE html>", body)
        self.assertIn("Sign in", body)
        # And it comes back with somewhere to go afterwards.
        self.assertIn(
            reverse("borrower_list"),
            unquote(response.request["QUERY_STRING"]),
        )

    def test_a_refusal_is_a_whole_page_too(self):
        make_user(username="assistant_u", password="pass12345", role="Assistant")
        self.client.login(username="assistant_u", password="pass12345")

        response = self.client.get(
            reverse("borrower_delete", args=[self.borrower.id]), **NAV
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("<!DOCTYPE html>", response.content.decode())


TEMPLATE_DIR = os.path.join(
    settings.BASE_DIR, "library", "templates", "library"
)

# The three documents that must stay documents.
#
#   base.html        the shell itself, which the others extend
#   error_base.html  403 / 404 / 500. An error is not a region of the
#                    application: showing one inside the chrome would offer
#                    a sidebar full of links from a page that just refused.
#   login.html       there is no application to be inside yet.
WHOLE_DOCUMENTS = ("base.html", "error_base.html", "login.html")


class EveryPageIsNavigableTests(TestCase):
    """No page renders a document of its own any more.

    The shell boosts every link inside it, so a page still extending
    base.html directly would answer a navigation with a complete document,
    and swapping that into a region of the page it came from would nest a
    second sidebar inside the first. Checked here rather than one URL at a
    time, because the property belongs to the template and a list of URLs
    would fall behind the next page someone adds.
    """

    def page_templates(self):

        for name in sorted(os.listdir(TEMPLATE_DIR)):

            # Includes, by the leading-underscore convention. They are
            # fragments of a page, never a page.
            if not name.endswith(".html") or name.startswith("_"):
                continue

            if name in WHOLE_DOCUMENTS:
                continue

            yield name

    def test_they_all_extend_the_shared_layout(self):

        for name in self.page_templates():
            with self.subTest(template=name):

                source = io.open(
                    os.path.join(TEMPLATE_DIR, name), encoding="utf-8"
                ).read()

                self.assertIn("{% extends layout %}", source)

    def test_none_of_them_carries_a_doctype(self):

        for name in self.page_templates():
            with self.subTest(template=name):

                source = io.open(
                    os.path.join(TEMPLATE_DIR, name), encoding="utf-8"
                ).read()

                self.assertNotIn("<!DOCTYPE", source.upper())

    def test_the_error_pages_still_do(self):
        # The other half of the rule: these are meant to be whole pages,
        # and app.js hands a whole page back to the browser to load rather
        # than swapping it into the chrome.
        source = io.open(
            os.path.join(TEMPLATE_DIR, "error_base.html"), encoding="utf-8"
        ).read()

        self.assertIn("library/base.html", source)


class BoostedShellTests(TestCase):
    """What the shell declares, and where it declines to."""

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def shell(self):
        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_wrapper_boosts_everything_inside_it(self):
        body = self.shell()

        wrapper = re.search(r'<div\s+class="app-wrapper"[^>]*>', body)

        self.assertIsNotNone(wrapper)
        self.assertIn('hx-boost="true"', wrapper.group(0))
        self.assertIn('hx-target="#%s"' % MAIN_CONTENT_ID, wrapper.group(0))

    def test_signing_out_is_left_to_the_browser(self):
        # Logging out replaces the whole shell, not a region of it.
        body = self.shell()

        link = re.search(
            r'<a[^>]*href="%s"[^>]*>' % reverse("logout"), body, re.S
        )

        self.assertIsNotNone(link)
        self.assertIn('hx-boost="false"', link.group(0))

    def test_the_sidebar_links_carry_no_wiring_of_their_own(self):
        # One declaration on the shell rather than one per link, so a link
        # added later navigates without anyone remembering to mark it up.
        body = self.shell()

        links = re.findall(r'<a[^>]*class="nav-link-custom"[^>]*>', body, re.S)

        self.assertTrue(links)

        for link in links:
            with self.subTest(link=" ".join(link.split())[:70]):
                self.assertNotIn("hx-get", link)
                self.assertNotIn("hx-push-url", link)

    def test_the_sign_in_page_is_not_boosted(self):
        self.client.logout()

        body = self.client.get(reverse("login")).content.decode()

        self.assertNotIn("app-wrapper", body)
        self.assertNotIn('hx-boost="true"', body)


class NotNavigationTests(TestCase):
    """Downloads and uploads, which a background fetch would break."""

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")
        make_book()

    def links_to(self, body, url):
        return re.findall(
            r'<a[^>]*href="%s[^"]*"[^>]*>' % re.escape(url), body, re.S
        )

    def test_a_spreadsheet_is_not_fetched_in_the_background(self):
        # Boosted, the export would arrive as a response to be swapped
        # into the page rather than a file to be saved.
        body = self.client.get(reverse("book_list")).content.decode()

        links = self.links_to(body, reverse("book_export"))

        self.assertTrue(links)

        for link in links:
            with self.subTest(link=" ".join(link.split())[:70]):
                self.assertIn('hx-boost="false"', link)

    def test_the_import_page_leaves_its_downloads_alone(self):
        body = self.client.get(reverse("book_import")).content.decode()

        for name in ("book_import_template", "book_export"):
            with self.subTest(download=name):

                links = self.links_to(body, reverse(name))

                self.assertTrue(links)

                for link in links:
                    self.assertIn('hx-boost="false"', link)

    def test_the_forms_that_post_a_file_are_left_alone(self):
        for label, url in (
            ("book add", reverse("book_add")),
            ("book import", reverse("book_import")),
            ("branding", reverse("branding_settings")),
        ):
            with self.subTest(page=label):

                body = self.client.get(url).content.decode()

                form = re.search(
                    r"<form[^>]*multipart/form-data[^>]*>", body, re.S
                )

                self.assertIsNotNone(form)
                self.assertIn('hx-boost="false"', form.group(0))


class NavigatedPageSweepTests(TestCase):
    """Every page in the application, navigated to in turn.

    The template sweep proves each one can answer a navigation. This proves
    each one does, through its own view and its own context.
    """

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        self.category = make_category()
        self.publisher = make_publisher()
        self.book = make_book(
            category=self.category, publisher=self.publisher
        )
        self.author = self.book.author
        self.volume = make_volume(book=self.book)
        self.content = make_content(volume=self.volume)
        self.location = make_location()
        self.shelf = make_shelf(location=self.location)
        self.copy = make_copy(volume=self.volume, shelf=self.shelf)
        self.borrower = make_borrower()
        self.loan = make_loan(copy=self.copy, borrower=self.borrower)

    def urls(self):
        return {
            "dashboard": reverse("dashboard"),
            "profile": reverse("profile"),
            "branding": reverse("branding_settings"),
            "activity log": reverse("activity_log_list"),

            "circulation": reverse("circulation_dashboard"),
            "issue": reverse("circulation_issue"),
            "return lookup": reverse("circulation_return_lookup"),
            "active loans": reverse("circulation_active_loans"),
            "loan list": reverse("loan_list"),
            "loan detail": reverse("loan_detail", args=[self.loan.id]),
            "loan edit": reverse("loan_edit", args=[self.loan.id]),
            "loan return": reverse("loan_return", args=[self.loan.id]),
            "loan renew": reverse("loan_renew", args=[self.loan.id]),
            "loan delete": reverse("loan_delete", args=[self.loan.id]),

            "book list": reverse("book_list"),
            "book add": reverse("book_add"),
            "book import": reverse("book_import"),
            "book detail": reverse("book_detail", args=[self.book.id]),
            "book edit": reverse("book_edit", args=[self.book.id]),
            "book archive": reverse("book_archive", args=[self.book.id]),
            "book delete": reverse("book_delete", args=[self.book.id]),

            "volume list": reverse("book_volume_list"),
            "volume add": reverse("book_volume_add"),
            "volume detail": reverse(
                "book_volume_detail", args=[self.volume.id]
            ),
            "volume edit": reverse("book_volume_edit", args=[self.volume.id]),
            "volume delete": reverse(
                "book_volume_delete", args=[self.volume.id]
            ),

            "content list": reverse("book_content_list"),
            "content add": reverse("book_content_add"),
            "content detail": reverse(
                "book_content_detail", args=[self.content.id]
            ),
            "content edit": reverse(
                "book_content_edit", args=[self.content.id]
            ),
            "content delete": reverse(
                "book_content_delete", args=[self.content.id]
            ),

            "copy list": reverse("book_copy_list"),
            "copy add": reverse("book_copy_add"),
            "copy detail": reverse("book_copy_detail", args=[self.copy.id]),
            "copy edit": reverse("book_copy_edit", args=[self.copy.id]),
            "copy move": reverse("book_copy_move", args=[self.copy.id]),
            "copy withdraw": reverse(
                "book_copy_withdraw", args=[self.copy.id]
            ),
            "copy delete": reverse("book_copy_delete", args=[self.copy.id]),

            "borrower list": reverse("borrower_list"),
            "borrower add": reverse("borrower_add"),
            "borrower detail": reverse(
                "borrower_detail", args=[self.borrower.id]
            ),
            "borrower edit": reverse("borrower_edit", args=[self.borrower.id]),
            "borrower delete": reverse(
                "borrower_delete", args=[self.borrower.id]
            ),

            "location list": reverse("location_list"),
            "location add": reverse("location_add"),
            "location detail": reverse(
                "location_detail", args=[self.location.id]
            ),
            "location edit": reverse("location_edit", args=[self.location.id]),
            "location delete": reverse(
                "location_delete", args=[self.location.id]
            ),

            "shelf list": reverse("shelf_list"),
            "shelf add": reverse("shelf_add"),
            "shelf detail": reverse("shelf_detail", args=[self.shelf.id]),
            "shelf edit": reverse("shelf_edit", args=[self.shelf.id]),
            "shelf delete": reverse("shelf_delete", args=[self.shelf.id]),

            "author list": reverse("author_list"),
            "author add": reverse("author_add"),
            "author detail": reverse("author_detail", args=[self.author.id]),
            "author edit": reverse("author_edit", args=[self.author.id]),
            "author delete": reverse("author_delete", args=[self.author.id]),

            "category list": reverse("category_list"),
            "category add": reverse("category_add"),
            "category detail": reverse(
                "category_detail", args=[self.category.id]
            ),
            "category edit": reverse("category_edit", args=[self.category.id]),
            "category delete": reverse(
                "category_delete", args=[self.category.id]
            ),

            "publisher list": reverse("publisher_list"),
            "publisher add": reverse("publisher_add"),
            "publisher detail": reverse(
                "publisher_detail", args=[self.publisher.id]
            ),
            "publisher edit": reverse(
                "publisher_edit", args=[self.publisher.id]
            ),
            "publisher delete": reverse(
                "publisher_delete", args=[self.publisher.id]
            ),

            "user list": reverse("user_list"),
            "user add": reverse("user_add"),
        }

    def test_each_one_answers_a_navigation_with_the_region_alone(self):
        for label, url in self.urls().items():
            with self.subTest(page=label):

                response = self.client.get(url, **NAV)
                body = response.content.decode()

                self.assertEqual(response.status_code, 200)
                self.assertNotIn("<!DOCTYPE html>", body)
                self.assertNotIn("sidebar-nav", body)
                # The target is never sent back inside itself.
                self.assertNotIn('id="%s"' % MAIN_CONTENT_ID, body)

    def test_each_one_brings_its_heading_and_title_along(self):
        for label, url in self.urls().items():
            with self.subTest(page=label):

                body = self.client.get(url, **NAV).content.decode()

                self.assertIn('id="pageHeading" hx-swap-oob="true"', body)
                self.assertIn("<title>", body)

    def test_each_one_is_still_a_whole_page_without_the_headers(self):
        for label, url in self.urls().items():
            with self.subTest(page=label):

                body = self.client.get(url).content.decode()

                self.assertIn("<!DOCTYPE html>", body)
                self.assertIn("sidebar-nav", body)


class BookCopiesInTheSidebarTests(TestCase):
    """Book Copies has an entry of its own again, under INVENTORY."""

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def sidebar(self):
        body = self.client.get(reverse("dashboard")).content.decode()

        start = body.index('class="sidebar-nav"')

        return body[start:body.index("</nav>", start)]

    def test_it_is_there(self):
        nav = self.sidebar()

        self.assertIn(reverse("book_copy_list"), nav)
        self.assertIn("Book Copies", nav)

    def test_it_sits_in_the_inventory_group(self):
        nav = self.sidebar()

        inventory = nav.index("INVENTORY")
        administration = nav.index("ADMINISTRATION")
        entry = nav.index(reverse("book_copy_list"))

        self.assertLess(inventory, entry)
        self.assertLess(entry, administration)

    def test_it_is_a_plain_link_like_every_other_entry(self):
        # Which is what makes the shell boost it: a swap, not a reload.
        nav = self.sidebar()

        link = re.search(
            r'<a[^>]*href="%s"[^>]*>' % reverse("book_copy_list"),
            nav,
            re.S,
        )

        self.assertIsNotNone(link)
        self.assertNotIn("hx-get", link.group(0))
        self.assertNotIn("hx-boost=\"false\"", link.group(0))

    def test_books_no_longer_claims_the_copy_list(self):
        # Both would match /library/book-copies/ at the same length, and
        # `markActive` keeps the first it finds - which is Books. Dropping
        # it from the prefix list is what lets the new entry light up.
        nav = self.sidebar()

        prefix = re.search(r'data-nav-prefix="([^"]*)"', nav)

        self.assertIsNotNone(prefix)
        self.assertNotIn(reverse("book_copy_list"), prefix.group(1))

    def test_volumes_and_contents_still_keep_books_lit(self):
        # They have no entry of their own, so Books still stands in.
        prefix = re.search(
            r'data-nav-prefix="([^"]*)"', self.sidebar()
        ).group(1)

        self.assertIn(reverse("book_volume_list"), prefix)
        self.assertIn(reverse("book_content_list"), prefix)

    def test_their_urls_are_untouched(self):
        for name in (
            "book_copy_list", "book_volume_list", "book_content_list"
        ):
            with self.subTest(page=name):
                self.assertEqual(
                    self.client.get(reverse(name)).status_code, 200
                )

    def test_volumes_and_contents_are_still_not_in_the_sidebar(self):
        nav = self.sidebar()

        for name in ("book_volume_list", "book_content_list"):
            with self.subTest(page=name):
                # Only inside the Books prefix attribute, never as an href.
                self.assertNotIn('href="%s"' % reverse(name), nav)

    def test_the_copy_list_still_answers_a_navigation_with_a_fragment(self):
        response = self.client.get(reverse("book_copy_list"), **NAV)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<!DOCTYPE html>", response.content.decode())

    def test_its_permissions_are_unchanged(self):
        # Open to every role, exactly as before.
        for role in ("Admin", "Librarian", "Assistant"):
            with self.subTest(role=role):
                self.client.logout()
                make_user(
                    username="c_%s" % role, password="pass12345", role=role
                )
                self.client.login(
                    username="c_%s" % role, password="pass12345"
                )

                self.assertEqual(
                    self.client.get(reverse("book_copy_list")).status_code,
                    200,
                )
                self.assertIn(
                    reverse("book_copy_list"), self.sidebar()
                )
