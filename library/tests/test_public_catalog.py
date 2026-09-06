"""The public catalogue: what a stranger can see, and what they cannot.

Four properties carry these tests.

It opens without a login, and nothing else does. The exemption is
`@login_not_required` on each public view, so `/catalog/` answers anonymous
requests while every representative page of `/library/` still redirects to
the sign-in form - which is asserted here rather than assumed, because an
exemption that leaked would be the whole security of this feature.

It shows bibliographic facts and nothing else. Borrowers, loans, due dates,
staff accounts, copy codes, shelves, locations, reservations, suggestions
and the activity log are absent from the response, and several of them are
absent because the queryset never joined to them. The tests below look for
their actual values in the rendered bytes rather than for the absence of a
label.

Archived books do not exist out here. Not in the list, not in a search, not
through a crafted URL - and an archived id answers exactly as an id that
was never used does, so neither confirms that a hidden record is there.

And it costs a bounded number of queries. Availability is Task 6's
annotation, counted by the query that fetched the page, so a catalogue of
one and a catalogue of four hundred cost the same.
"""

import re

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library.models import Book, Category, Publisher

from .helpers import (
    make_author,
    make_book,
    make_borrower,
    make_branding,
    make_category,
    make_copy,
    make_loan,
    make_location,
    make_publisher,
    make_shelf,
    make_user,
    make_volume,
)


ARABIC_NAME = "مكتبة النور"


class PublicCatalogTestCase(TestCase):
    """One shelf, one author, one book with one copy on it."""

    def setUp(self):
        self.list_url = reverse("public_book_list")

        self.location = make_location(name="Main Hall")
        self.shelf = make_shelf(location=self.location, shelf_code="P1")

        self.author = make_author("Ibn Hajar")
        self.category = make_category("Hadith")
        self.publisher = make_publisher(name="Dar al-Salam", city="Riyadh")

        self.book = make_book(
            title="Fath al-Bari",
            author=self.author,
            category=self.category,
            publisher=self.publisher,
        )
        self.volume = make_volume(
            book=self.book, volume_number=1, title="Introduction"
        )

    def detail_url(self, book=None):
        return reverse("public_book_detail", args=[(book or self.book).id])

    def a_copy(self, code="PUB-0001", status="Available", volume=None,
               shelf=True):
        return make_copy(
            volume=volume or self.volume,
            shelf=self.shelf if shelf else None,
            copy_code=code,
            status=status,
        )

    def another_book(self, title, author=None, **fields):
        book = make_book(
            title=title, author=author or self.author, **fields
        )
        make_volume(book=book, volume_number=1, title="")

        return book

    def titles(self, response):
        return [book.title for book in response.context["books"]]


# ==========================================================================
# ANONYMOUS ACCESS
# ==========================================================================


class AnonymousAccessTests(PublicCatalogTestCase):

    def test_the_list_opens_without_a_login(self):
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fath al-Bari")

    def test_the_detail_opens_without_a_login(self):
        response = self.client.get(self.detail_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fath al-Bari")

    def test_searching_works_without_a_login(self):
        response = self.client.get(self.list_url, {"search": "Fath"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.titles(response), ["Fath al-Bari"])

    def test_filtering_works_without_a_login(self):
        response = self.client.get(
            self.list_url, {"category": self.category.id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.titles(response), ["Fath al-Bari"])

    def test_paging_works_without_a_login(self):
        for index in range(30):
            self.another_book("Volume %02d" % index)

        response = self.client.get(self.list_url, {"page": "2"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["books"].has_previous())

    def test_an_empty_catalogue_still_renders(self):
        Book.objects.all().delete()

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no books in the catalogue")

    def test_the_urls_are_where_they_should_be(self):
        self.assertEqual(self.list_url, "/catalog/")
        self.assertTrue(self.detail_url().startswith("/catalog/book/"))


# ==========================================================================
# THE AUTHENTICATION BOUNDARY
# ==========================================================================


class AuthenticationBoundaryTests(PublicCatalogTestCase):
    """The exemption is per-view. It must not have widened anything."""

    # One page from each area a visitor must never reach.
    def internal_urls(self):
        borrower = make_borrower(name="Hafsa", phone="0300-1")

        return {
            "dashboard": reverse("dashboard"),
            "staff book list": reverse("book_list"),
            "staff book detail": reverse("book_detail", args=[self.book.id]),
            "book add": reverse("book_add"),
            "borrowers": reverse("borrower_list"),
            "borrower detail": reverse("borrower_detail", args=[borrower.id]),
            "loans": reverse("loan_list"),
            "issue": reverse("circulation_issue"),
            "inventory": reverse("inventory_session_list"),
            "reports": reverse("reports_home"),
            "reservations": reverse("reservation_list"),
            "suggestions": reverse("suggestion_list"),
            "notifications": reverse("notification_list"),
            "settings": reverse("branding_settings"),
            "users": reverse("user_list"),
            "activity log": reverse("activity_log_list"),
        }

    def test_every_internal_page_still_needs_a_login(self):
        for label, url in self.internal_urls().items():
            with self.subTest(page=label):

                response = self.client.get(url)

                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])

    def test_internal_posts_are_still_refused(self):
        for label, url in self.internal_urls().items():
            with self.subTest(page=label):

                response = self.client.post(url, {})

                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])

    def test_the_public_views_are_the_only_new_exemption(self):
        # Read off the views themselves rather than off behaviour: the
        # middleware exempts a view when `login_required` is False on it,
        # so this is the exemption, stated.
        from library import public_views, views

        for view in (
            public_views.public_book_list,
            public_views.public_book_detail,
        ):
            with self.subTest(view=view.__name__):
                self.assertFalse(getattr(view, "login_required", True))

        # And the staff tree still has exactly the one it always had.
        exempt = [
            name
            for name in dir(views)
            if callable(getattr(views, name, None))
            and getattr(getattr(views, name), "login_required", True) is False
        ]

        self.assertEqual(exempt, ["login_view"])

    def test_no_middleware_or_setting_was_relaxed(self):
        from django.conf import settings

        self.assertIn(
            "django.contrib.auth.middleware.LoginRequiredMiddleware",
            settings.MIDDLEWARE,
        )

    def test_signed_in_staff_may_read_the_public_pages(self):
        make_user(username="admin_p", password="pass12345", role="Admin")
        self.client.login(username="admin_p", password="pass12345")

        for url in (self.list_url, self.detail_url()):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_and_see_no_staff_controls_there(self):
        make_user(username="admin_p", password="pass12345", role="Admin")
        self.client.login(username="admin_p", password="pass12345")

        body = self.client.get(self.list_url).content.decode()

        for absent in (
            'class="sidebar"',
            "notificationToggle",
            "nav-link-custom",
            reverse("dashboard"),
            reverse("book_add"),
            reverse("logout"),
        ):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, body)


# ==========================================================================
# SEARCH AND FILTERS
# ==========================================================================


class SearchTests(PublicCatalogTestCase):

    def setUp(self):
        super().setUp()

        self.nawawi = make_author("Al-Nawawi")
        self.other = self.another_book(
            "Riyad as-Salihin", author=self.nawawi
        )

    def search(self, term):
        return self.titles(self.client.get(self.list_url, {"search": term}))

    def test_a_title_search(self):
        self.assertEqual(self.search("Fath"), ["Fath al-Bari"])

    def test_a_partial_title_search(self):
        self.assertEqual(self.search("Bari"), ["Fath al-Bari"])

    def test_a_full_author_search(self):
        self.assertEqual(self.search("Al-Nawawi"), ["Riyad as-Salihin"])

    def test_a_partial_author_search(self):
        self.assertEqual(self.search("Nawaw"), ["Riyad as-Salihin"])

    def test_the_search_is_case_insensitive(self):
        self.assertEqual(self.search("fath AL-bari"), ["Fath al-Bari"])
        self.assertEqual(self.search("ibn hajar"), ["Fath al-Bari"])

    def test_one_box_searches_both_fields(self):
        # The same pair the staff list searches, in the same way.
        self.assertEqual(sorted(self.search("a")), sorted(
            ["Fath al-Bari", "Riyad as-Salihin"]
        ))

    def test_a_search_that_matches_nothing(self):
        response = self.client.get(
            self.list_url, {"search": "nothing like this"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.titles(response), [])
        self.assertContains(response, "Nothing matches that")

    def test_a_blank_search_lists_everything(self):
        self.assertEqual(len(self.search("")), 2)

    def test_the_search_box_keeps_what_was_typed(self):
        response = self.client.get(self.list_url, {"search": "Fath"})

        self.assertContains(response, 'value="Fath"')


class FilterTests(PublicCatalogTestCase):

    def setUp(self):
        super().setUp()

        self.fiqh = make_category("Fiqh")
        self.press = make_publisher(name="Crescent Press", city="Lahore")
        self.nawawi = make_author("Al-Nawawi")

        self.other = self.another_book(
            "Riyad as-Salihin",
            author=self.nawawi,
            category=self.fiqh,
            publisher=self.press,
        )

    def filtered(self, **params):
        return self.titles(self.client.get(self.list_url, params))

    def test_the_category_filter(self):
        self.assertEqual(
            self.filtered(category=self.category.id), ["Fath al-Bari"]
        )

    def test_the_author_filter(self):
        self.assertEqual(
            self.filtered(author=self.nawawi.id), ["Riyad as-Salihin"]
        )

    def test_the_publisher_filter(self):
        self.assertEqual(
            self.filtered(publisher=self.press.id), ["Riyad as-Salihin"]
        )

    def test_the_availability_filter(self):
        self.a_copy("PUB-1001")

        self.assertEqual(self.filtered(availability="available"),
                         ["Fath al-Bari"])
        self.assertEqual(self.filtered(availability="unavailable"),
                         ["Riyad as-Salihin"])

    def test_filters_combine_with_the_search(self):
        self.assertEqual(
            self.filtered(search="Riyad", category=self.fiqh.id),
            ["Riyad as-Salihin"],
        )

        # And a combination that matches nothing really matches nothing.
        self.assertEqual(
            self.filtered(search="Riyad", category=self.category.id), []
        )

    def test_filters_combine_with_each_other(self):
        self.assertEqual(
            self.filtered(
                author=self.nawawi.id, publisher=self.press.id
            ),
            ["Riyad as-Salihin"],
        )

        self.assertEqual(
            self.filtered(author=self.author.id, publisher=self.press.id), []
        )

    def test_an_unknown_category_id_matches_nothing_rather_than_erroring(self):
        response = self.client.get(self.list_url, {"category": "999999"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.titles(response), [])

    def test_a_non_numeric_id_is_read_as_no_filter(self):
        for field in ("category", "author", "publisher"):
            with self.subTest(field=field):

                response = self.client.get(
                    self.list_url, {field: "'; DROP TABLE books; --"}
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(self.titles(response)), 2)

        self.assertEqual(Book.objects.count(), 2)

    def test_an_unknown_availability_value_is_read_as_no_filter(self):
        response = self.client.get(
            self.list_url, {"availability": "issued"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["availability"], "")
        self.assertEqual(len(self.titles(response)), 2)

    def test_arbitrary_parameters_are_ignored(self):
        response = self.client.get(
            self.list_url,
            {
                "archived": "1",
                "borrower": "1",
                "status": "Issued",
                "mode": "author",
                "order_by": "id",
                "sort": "id",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.titles(response)), 2)

    def test_a_filter_survives_pagination(self):
        for index in range(30):
            self.another_book("Nawawi work %02d" % index, author=self.nawawi)

        response = self.client.get(
            self.list_url, {"author": self.nawawi.id, "page": "2"}
        )

        self.assertTrue(
            all(
                book.author_id == self.nawawi.id
                for book in response.context["books"]
            )
        )

    def test_a_search_survives_pagination(self):
        for index in range(30):
            self.another_book("Nawawi work %02d" % index, author=self.nawawi)

        response = self.client.get(
            self.list_url, {"search": "Nawawi work", "page": "2"}
        )

        self.assertTrue(
            all("Nawawi work" in book.title
                for book in response.context["books"])
        )

    def test_the_pagination_query_carries_the_filters_and_not_the_page(self):
        for index in range(30):
            self.another_book("Nawawi work %02d" % index, author=self.nawawi)

        query = self.client.get(
            self.list_url,
            {"search": "Nawawi", "availability": "unavailable", "page": "2"},
        ).context["pagination_query"]

        self.assertIn("search=Nawawi", query)
        self.assertIn("availability=unavailable", query)
        self.assertNotIn("page=", query)

    def test_the_reset_link_is_offered_and_clears_everything(self):
        response = self.client.get(self.list_url, {"search": "Fath"})

        self.assertTrue(response.context["has_filters"])
        self.assertContains(response, "Clear")

        cleared = self.client.get(self.list_url)

        self.assertFalse(cleared.context["has_filters"])
        self.assertNotContains(cleared, ">Clear<")
        self.assertEqual(len(self.titles(cleared)), 2)

    def test_an_active_author_filter_says_so_and_offers_to_drop_it(self):
        response = self.client.get(
            self.list_url, {"author": self.nawawi.id}
        )

        self.assertContains(response, "Al-Nawawi")
        self.assertEqual(response.context["author_name"], "Al-Nawawi")


# ==========================================================================
# AVAILABILITY
# ==========================================================================


class AvailabilityTests(PublicCatalogTestCase):
    """Task 6's definition, phrased for a visitor. Not a second one."""

    def label(self, response=None):
        response = response or self.client.get(self.list_url)

        return response.context["books"][0].availability["label"]

    def test_a_book_with_no_copies(self):
        self.assertEqual(self.label(), "No copies")

    def test_one_copy_on_the_shelf(self):
        self.a_copy("PUB-2001")

        self.assertEqual(self.label(), "Available")

    def test_several_copies_on_the_shelf(self):
        for index in range(3):
            self.a_copy("PUB-200%d" % index)

        self.assertEqual(self.label(), "3 available")

    def test_a_copy_out_on_loan_is_not_available(self):
        copy = self.a_copy("PUB-2010", status="Issued")
        make_loan(copy=copy, borrower=make_borrower(name="Hafsa", phone="1"))

        self.assertEqual(self.label(), "Currently unavailable")

    def test_a_copy_with_no_shelf_is_not_available(self):
        # Task 6 counts a copy available only when it is on a shelf.
        self.a_copy("PUB-2020", shelf=False)

        self.assertEqual(self.label(), "Currently unavailable")

    def test_withdrawn_copies_are_not_available(self):
        for index, status in enumerate(
            ("Lost", "Damaged", "Missing", "Transferred")
        ):
            with self.subTest(status=status):

                self.book.bookvolume_set.first().bookcopy_set.all().delete()
                self.a_copy("PUB-203%d" % index, status=status)

                self.assertEqual(self.label(), "Currently unavailable")

    def test_one_available_among_several_states(self):
        self.a_copy("PUB-2040")
        self.a_copy("PUB-2041", status="Lost")

        issued = self.a_copy("PUB-2042", status="Issued")
        make_loan(copy=issued, borrower=make_borrower(name="Omar", phone="2"))

        self.assertEqual(self.label(), "Available")

    def test_the_detail_page_agrees_with_the_list(self):
        for setup in (
            lambda: None,
            lambda: self.a_copy("PUB-2050"),
            lambda: self.a_copy("PUB-2051", status="Lost"),
        ):
            with self.subTest(setup=setup):

                self.book.bookvolume_set.first().bookcopy_set.all().delete()
                setup()

                listed = self.label()
                detailed = self.client.get(
                    self.detail_url()
                ).context["book"].availability["label"]

                self.assertEqual(listed, detailed)

    def test_the_label_appears_on_both_pages(self):
        self.a_copy("PUB-2060")

        self.assertContains(self.client.get(self.list_url), "Available")
        self.assertContains(self.client.get(self.detail_url()), "Available")

    def test_no_count_of_what_is_out_is_shown(self):
        copy = self.a_copy("PUB-2070", status="Issued")
        make_loan(copy=copy, borrower=make_borrower(name="Hafsa", phone="1"))

        for url in (self.list_url, self.detail_url()):
            with self.subTest(url=url):

                body = self.client.get(url).content.decode()

                self.assertNotIn("issued", body.lower())
                self.assertNotIn("1 out", body.lower())


# ==========================================================================
# ARCHIVED BOOKS AND DIRECT OBJECT SAFETY
# ==========================================================================


class ArchiveTests(PublicCatalogTestCase):

    def setUp(self):
        super().setUp()

        self.hidden = self.another_book("Withdrawn Work")
        self.hidden.archived_at = timezone.now()
        self.hidden.save()

    def test_an_archived_book_is_not_in_the_list(self):
        self.assertEqual(
            self.titles(self.client.get(self.list_url)), ["Fath al-Bari"]
        )

    def test_an_archived_book_is_not_found_by_search(self):
        response = self.client.get(self.list_url, {"search": "Withdrawn"})

        self.assertEqual(self.titles(response), [])
        self.assertNotContains(response, "Withdrawn Work")

    def test_an_archived_book_is_not_found_by_a_filter(self):
        for params in (
            {"author": self.author.id},
            {"availability": "unavailable"},
            {"category": self.category.id},
        ):
            with self.subTest(params=params):
                self.assertNotIn(
                    "Withdrawn Work",
                    self.titles(self.client.get(self.list_url, params)),
                )

    def test_asking_for_archived_ones_does_not_reveal_them(self):
        # The staff list has `?archived=1`. The public one does not read it.
        response = self.client.get(self.list_url, {"archived": "1"})

        self.assertEqual(self.titles(response), ["Fath al-Bari"])

    def test_an_archived_book_has_no_public_page(self):
        response = self.client.get(self.detail_url(self.hidden))

        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Withdrawn Work", status_code=404)

    def test_an_archived_id_answers_exactly_as_an_unused_one(self):
        # Otherwise the difference between the two is an oracle for
        # "is there a hidden book with this id".
        archived = self.client.get(self.detail_url(self.hidden))
        missing = self.client.get(
            reverse("public_book_detail", args=[999999])
        )

        self.assertEqual(archived.status_code, missing.status_code)
        self.assertEqual(archived.content, missing.content)

    def test_the_not_found_page_is_the_public_one(self):
        response = self.client.get(
            reverse("public_book_detail", args=[999999])
        )

        body = response.content.decode()

        self.assertIn("not in the catalogue", body)
        self.assertNotIn(reverse("dashboard"), body)
        self.assertNotIn(reverse("book_list"), body)

    def test_restoring_a_book_brings_it_back(self):
        self.hidden.archived_at = None
        self.hidden.save()

        self.assertIn(
            "Withdrawn Work", self.titles(self.client.get(self.list_url))
        )
        self.assertEqual(
            self.client.get(self.detail_url(self.hidden)).status_code, 200
        )


# ==========================================================================
# DATA EXPOSURE
# ==========================================================================


class DataExposureTests(PublicCatalogTestCase):
    """What is in the bytes, not what is in the labels."""

    def setUp(self):
        super().setUp()

        self.staff = make_user(
            username="librarian_p", password="pass12345", role="Librarian"
        )

        self.borrower = make_borrower(
            name="Hafsa Bibi", phone="0300-7654321"
        )

        self.on_shelf = self.a_copy("PUB-SECRET-1")
        self.on_loan = self.a_copy("PUB-SECRET-2", status="Issued")

        self.loan = make_loan(
            copy=self.on_loan,
            borrower=self.borrower,
            issued_by=self.staff,
        )

    def bodies(self):
        return {
            "list": self.client.get(self.list_url).content.decode(),
            "detail": self.client.get(self.detail_url()).content.decode(),
        }

    def test_no_borrower_appears(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn("Hafsa Bibi", body)
                self.assertNotIn("0300-7654321", body)

    def test_no_staff_account_appears(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn("librarian_p", body)
                self.assertNotIn(self.staff.full_name, body)

    def test_no_copy_code_appears(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn("PUB-SECRET-1", body)
                self.assertNotIn("PUB-SECRET-2", body)

    def test_no_shelf_or_location_appears(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn("Main Hall", body)
                self.assertNotIn("P1", body)

    def test_no_due_date_appears(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn(self.loan.due_date.isoformat(), body)

    def test_no_internal_url_appears(self):
        for page, body in self.bodies().items():
            for internal in (
                "/library/books/",
                "/library/borrowers/",
                "/library/loans/",
                "/library/reservations/",
                "/library/suggestions/",
                "/library/notifications/",
                "/library/stock-check/",
                "/library/reports/",
                "/library/settings/",
                "/library/users/",
                "/library/activity-logs/",
                "/library/login/",
            ):
                with self.subTest(page=page, url=internal):
                    self.assertNotIn(internal, body)

    def test_the_queryset_never_reaches_the_private_tables(self):
        # The strongest form of the rule: not "the template hid it" but
        # "the query never asked". A join that is not made cannot leak.
        with CaptureQueriesContext(connection) as captured:
            self.client.get(self.list_url)
            self.client.get(self.detail_url())

        sql = " ".join(query["sql"] for query in captured.captured_queries)

        for table in (
            '"borrowers"',
            '"users"',
            '"reservations"',
            '"acquisition_suggestions"',
            '"notifications"',
            '"inventory_sessions"',
            '"inventory_scans"',
            '"activity_logs"',
            '"shelves"',
            '"locations"',
        ):
            with self.subTest(table=table):
                self.assertNotIn(table, sql)

    def test_loans_are_only_ever_a_subquery_for_the_count(self):
        # `annotate_copy_counts` asks which copies are out; that is the one
        # place loans are touched, and it selects a copy id and nothing
        # about the loan or the borrower.
        with CaptureQueriesContext(connection) as captured:
            self.client.get(self.list_url)

        loan_queries = [
            query["sql"]
            for query in captured.captured_queries
            if '"loans"' in query["sql"]
        ]

        for sql in loan_queries:
            self.assertNotIn('"loans"."borrower_id"', sql)
            self.assertNotIn('"loans"."due_date"', sql)
            self.assertNotIn('"loans"."issued_by"', sql)

    def test_no_management_form_is_rendered(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn("csrfmiddlewaretoken", body)
                self.assertNotIn('method="post"', body.lower())

    def test_the_only_form_is_the_search(self):
        body = self.bodies()["list"]

        forms = re.findall(r"<form[^>]*>", body)

        self.assertEqual(len(forms), 1)
        self.assertIn('method="get"', forms[0])

    def test_no_archive_metadata_is_shown(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn("archived", body.lower())


# ==========================================================================
# INSTITUTION METADATA
# ==========================================================================


class InstitutionTests(PublicCatalogTestCase):

    def test_the_configured_name_appears(self):
        make_branding(name="Al Noor Library")

        for url in (self.list_url, self.detail_url()):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "Al Noor Library")

    def test_the_name_falls_back_when_unset(self):
        response = self.client.get(self.list_url)

        self.assertContains(response, "Madrasah Library")

    def test_a_blank_name_falls_back_too(self):
        make_branding(name="")

        self.assertContains(self.client.get(self.list_url), "Madrasah Library")

    def test_the_arabic_name_renders(self):
        make_branding(name="Al Noor Library", name_arabic=ARABIC_NAME)

        response = self.client.get(self.list_url)

        self.assertEqual(response.charset.lower(), "utf-8")
        self.assertContains(response, ARABIC_NAME)
        self.assertContains(response, 'dir="rtl"')
        self.assertContains(response, 'lang="ar"')

    def test_the_footer_text_is_shown(self):
        # Already public: it is on the anonymous sign-in page.
        make_branding(footer_text="Serving since 1990")

        self.assertContains(
            self.client.get(self.list_url), "Serving since 1990"
        )

    def test_contact_details_are_not_published(self):
        # They appear on the staff shell behind a login and on printed
        # sheets handed over deliberately. Neither is publishing them.
        make_branding(
            name="Al Noor Library",
            contact_email="office@alnoor.test",
            contact_phone="0300-1234567",
            address="12 Mall Road\nLahore",
            website="https://alnoor.test",
        )

        for url in (self.list_url, self.detail_url()):
            body = self.client.get(url).content.decode()

            for secret in (
                "office@alnoor.test",
                "0300-1234567",
                "12 Mall Road",
                "alnoor.test",
            ):
                with self.subTest(url=url, value=secret):
                    self.assertNotIn(secret, body)

    def test_no_second_settings_model_was_created(self):
        from library.models import OrganizationSettings

        make_branding(name="Al Noor Library")

        self.client.get(self.list_url)

        self.assertEqual(OrganizationSettings.objects.count(), 1)


# ==========================================================================
# LAYOUT AND NAVIGATION
# ==========================================================================


class LayoutTests(PublicCatalogTestCase):

    def bodies(self):
        return {
            "list": self.client.get(self.list_url).content.decode(),
            "detail": self.client.get(self.detail_url()).content.decode(),
            "not found": self.client.get(
                reverse("public_book_detail", args=[999999])
            ).content.decode(),
        }

    def test_no_staff_shell_is_rendered(self):
        for page, body in self.bodies().items():
            for absent in (
                'class="sidebar"',
                'id="sidebar"',
                "sidebar-nav",
                "nav-link-custom",
                'class="topbar"',
                "app-wrapper",
            ):
                with self.subTest(page=page, absent=absent):
                    self.assertNotIn(absent, body)

    def test_no_notification_controls(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn("notificationToggle", body)
                self.assertNotIn("notificationBadge", body)
                self.assertNotIn("notificationPanelBody", body)

    def test_no_user_menu_or_admin_links(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn("user-menu-button", body)
                self.assertNotIn("themeToggle", body)
                self.assertNotIn("globalModal", body)
                self.assertNotIn("formModal", body)
                self.assertNotIn("barcodeScanner", body)

    def test_it_is_a_whole_document_of_its_own(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertIn("<!DOCTYPE", body.upper())
                self.assertIn("public-body", body)

    def test_it_does_not_extend_the_staff_shell(self):
        import io
        import os

        from django.conf import settings

        base = os.path.join(
            settings.BASE_DIR, "library", "templates", "public", "base.html"
        )

        source = io.open(base, encoding="utf-8").read()

        # The staff shell is named in the comment at the top of that file,
        # explaining why it is not extended - so look for the inheritance
        # itself rather than for the string.
        self.assertNotIn("{% extends", source)
        self.assertIn("<!DOCTYPE html>", source)

    def test_there_is_no_htmx_on_the_page(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn("htmx.org", body)
                self.assertNotIn("hx-get", body)
                self.assertNotIn("hx-post", body)
                self.assertNotIn("hx-boost", body)
                self.assertNotIn("hx-target", body)

    def test_nothing_targets_the_staff_content_container(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn("mainContent", body)

    def test_nothing_links_to_a_login_protected_media_file(self):
        """A public page must not point at something a visitor cannot fetch.

        `MEDIA_URL` is served by `django.views.static.serve`, an ordinary
        view, so `LoginRequiredMiddleware` protects it like every other one
        - an uploaded cover or logo linked from here would answer a visitor
        with a redirect to the sign-in page and render as a broken image.
        Publishing the media directory is a decision about the installation
        and is deliberately not made here, so these pages link to none of
        it.
        """

        from django.conf import settings

        make_branding(
            name="Al Noor Library",
            logo="branding/logo.png",
            favicon="branding/favicon.png",
        )

        self.book.cover_image = "book_covers/cover.png"
        self.book.save()

        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn(settings.MEDIA_URL, body)
                self.assertNotIn("book_covers/", body)
                self.assertNotIn("branding/", body)

    def test_media_really_is_behind_the_login(self):
        # The premise of the test above, asserted rather than assumed - if
        # this ever stops being true, that decision should be made on
        # purpose and these pages revisited.
        response = self.client.get("/media/book_covers/anything.png")

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_no_javascript_is_loaded_at_all(self):
        for page, body in self.bodies().items():
            with self.subTest(page=page):
                self.assertNotIn("<script", body)
                self.assertNotIn("app.js", body)

    def test_search_and_paging_are_ordinary_links_and_forms(self):
        for index in range(30):
            self.another_book("Volume %02d" % index)

        body = self.client.get(self.list_url).content.decode()

        form = re.search(r"<form[^>]*>", body)

        self.assertIsNotNone(form)
        self.assertIn('method="get"', form.group(0))
        self.assertNotIn("hx-", form.group(0))

        # And the page links are hrefs, not script hooks.
        self.assertIn('class="page-link"', body)
        self.assertRegex(body, r'<a\s+class="page-link"\s+href="\?')

    def test_the_pages_carry_a_title_and_a_heading(self):
        listing = self.client.get(self.list_url)
        detail = self.client.get(self.detail_url())

        self.assertContains(listing, "<title>")
        self.assertContains(listing, "Catalogue")

        self.assertContains(detail, "<title>")
        self.assertContains(detail, "Fath al-Bari")

    def test_the_detail_page_links_back_to_the_catalogue(self):
        self.assertContains(
            self.client.get(self.detail_url()), self.list_url
        )


# ==========================================================================
# QUERY EFFICIENCY
# ==========================================================================


class QueryCountTests(PublicCatalogTestCase):

    made = 0

    def many(self, count, on_shelf=True):
        """Add `count` more books, with codes that never collide."""

        first = self.made
        self.made += count

        for index in range(first, first + count):
            book = make_book(
                title="Bulk book %04d" % index,
                author=self.author,
                category=self.category,
                publisher=self.publisher,
            )
            volume = make_volume(book=book, volume_number=1, title="")

            if on_shelf:
                make_copy(
                    volume=volume,
                    shelf=self.shelf,
                    copy_code="BULK-%04d" % index,
                    status="Available",
                )

    def count_for(self, url, params=None):
        with CaptureQueriesContext(connection) as captured:
            self.client.get(url, params or {})

        return len(captured.captured_queries)

    def test_an_empty_catalogue_costs_no_more_than_a_populated_one(self):
        """And measurably less: Django skips the page query at count 0.

        The four a populated page costs are the count, the page, the
        branding row and the category list - and the empty one is those
        minus the page. What matters is that neither grows with the
        catalogue, which the next test pins down.
        """

        # The fixture's own book has no copies; drop it so the list really
        # is empty, then fill it. The author, category and shelf stay, so
        # nothing unique is created twice.
        self.book.bookvolume_set.all().delete()
        self.book.delete()

        empty = self.count_for(self.list_url)

        self.assertEqual(Book.objects.count(), 0)

        self.many(50)

        populated = self.count_for(self.list_url)

        self.assertLessEqual(empty, populated)
        self.assertLessEqual(populated - empty, 1)

    def test_a_small_and_a_large_catalogue_cost_the_same(self):
        self.many(5)

        small = self.count_for(self.list_url)

        self.many(400)

        large = self.count_for(self.list_url)

        self.assertEqual(small, large)

    def test_availability_is_not_counted_per_book(self):
        # A full page of rows, every one of them showing an availability
        # label, and no query beyond the page's own.
        self.many(40)

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(self.list_url)

            body = response.content.decode()

        self.assertIn("Available", body)
        self.assertEqual(len(response.context["books"]), 25)

        copy_queries = [
            query["sql"]
            for query in captured.captured_queries
            if '"book_copies"' in query["sql"]
        ]

        # The page query and the count query, each carrying the aggregate.
        # Not one per row.
        self.assertLessEqual(len(copy_queries), 2)

    def test_a_search_costs_the_same_however_much_it_searches(self):
        self.many(5)

        small = self.count_for(self.list_url, {"search": "Bulk"})

        self.many(400)

        large = self.count_for(self.list_url, {"search": "Bulk"})

        self.assertEqual(small, large)

    def test_filters_cost_the_same(self):
        self.many(5)

        small = self.count_for(
            self.list_url,
            {"category": self.category.id, "availability": "available"},
        )

        self.many(400)

        large = self.count_for(
            self.list_url,
            {"category": self.category.id, "availability": "available"},
        )

        self.assertEqual(small, large)

    def test_the_detail_page_is_flat(self):
        for index in range(4):
            self.a_copy("PUB-300%d" % index)

        make_volume(book=self.book, volume_number=2, title="Second")

        before = self.count_for(self.detail_url())

        self.many(400)

        after = self.count_for(self.detail_url())

        self.assertEqual(before, after)

    def test_the_whole_list_is_a_handful_of_queries(self):
        self.many(200)

        self.assertLessEqual(self.count_for(self.list_url), 6)

    def test_the_detail_page_is_a_handful_of_queries(self):
        self.a_copy("PUB-4001")

        self.assertLessEqual(self.count_for(self.detail_url()), 4)

    def test_browsing_writes_nothing(self):
        from library.models import ActivityLog, Notification

        self.many(5)

        self.client.get(self.list_url)
        self.client.get(self.list_url, {"search": "Bulk"})
        self.client.get(self.detail_url())
        self.client.get(reverse("public_book_detail", args=[999999]))

        self.assertEqual(ActivityLog.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), 0)
