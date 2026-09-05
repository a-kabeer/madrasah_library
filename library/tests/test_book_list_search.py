"""One search box, the secondary filters, and the copy counts in each row.

The counts are the copy list's own definitions, annotated onto the query the
list already runs: a copy is out when a loan says so, and available only
when it is shelved, marked Available and not out.
"""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library.models import BookCopy
from library.tests.helpers import (
    make_author,
    make_book,
    make_category,
    make_copy,
    make_loan,
    make_publisher,
    make_shelf,
    make_user,
    make_volume,
)


class BookSearchTestCase(TestCase):

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.url = reverse("book_list")

        self.rumi = make_author("Jalaluddin Rumi")
        self.ghazali = make_author("Imam Al-Ghazali")
        self.shelf = make_shelf()

        self.masnavi = make_book(title="Masnavi", author=self.rumi)
        self.ihya = make_book(title="Ihya Ulum al-Din", author=self.ghazali)

    def get(self, **params):
        return self.client.get(self.url, params)

    def titles(self, response):
        return sorted(b.title for b in response.context["books"])

    def row(self, response, book):
        return next(
            b for b in response.context["books"] if b.id == book.id
        )


class OneSearchBoxTests(BookSearchTestCase):

    def test_it_finds_a_book_by_title(self):
        self.assertEqual(self.titles(self.get(search="Masnavi")), ["Masnavi"])

    def test_it_finds_a_book_by_its_author(self):
        self.assertEqual(self.titles(self.get(search="Ghazali")),
                         ["Ihya Ulum al-Din"])

    def test_either_side_matches_case_insensitively(self):
        for term in ("MASNAVI", "jalaluddin", "GHAZALI"):
            with self.subTest(term=term):
                self.assertTrue(self.get(search=term).context["books"])

    def test_it_does_not_reach_into_category_or_publisher(self):
        publisher = make_publisher(name="Darul Ishaat")
        category = make_category(name="Poetry")
        make_book(
            title="Something Else", author=make_author("Someone"),
            category=category, publisher=publisher,
        )

        for term in ("Darul Ishaat", "Poetry"):
            with self.subTest(term=term):
                self.assertEqual(
                    self.get(search=term).context["paginator"].count, 0
                )

    def test_the_box_is_labelled_for_both(self):
        response = self.get()

        self.assertContains(response, "Book title or author name")

    def test_the_legacy_title_parameter_still_works(self):
        response = self.get(title="Masnavi")

        self.assertEqual(self.titles(response), ["Masnavi"])

    def test_no_match_says_so(self):
        response = self.get(search="Nothing Like This")

        self.assertEqual(response.context["paginator"].count, 0)
        self.assertContains(response, "No books found")


class CopyCountTests(BookSearchTestCase):

    def setUp(self):
        super().setUp()
        self.volume = make_volume(book=self.masnavi, volume_number=1)

    def test_a_book_with_no_copies_reports_nothing(self):
        row = self.row(self.get(), self.masnavi)

        self.assertEqual(row.total_copies, 0)
        self.assertEqual(row.available_copies, 0)
        self.assertEqual(row.issued_copies, 0)
        self.assertContains(self.get(), "No copies")

    def test_a_shelved_copy_counts_as_available(self):
        make_copy(volume=self.volume, shelf=self.shelf, copy_code="S-1")

        row = self.row(self.get(), self.masnavi)

        self.assertEqual((row.total_copies, row.available_copies,
                          row.issued_copies), (1, 1, 0))

    def test_an_unshelved_copy_is_counted_but_not_available(self):
        make_copy(volume=self.volume, shelf=None, copy_code="S-2")

        row = self.row(self.get(), self.masnavi)

        self.assertEqual((row.total_copies, row.available_copies,
                          row.issued_copies), (1, 0, 0))

    def test_a_copy_on_loan_counts_as_out(self):
        copy = make_copy(volume=self.volume, shelf=self.shelf, copy_code="S-3")
        make_loan(copy=copy)

        row = self.row(self.get(), self.masnavi)

        self.assertEqual((row.total_copies, row.available_copies,
                          row.issued_copies), (1, 0, 1))

    def test_a_returned_loan_leaves_the_copy_available(self):
        copy = make_copy(volume=self.volume, shelf=self.shelf, copy_code="S-4")
        today = timezone.now().date()
        make_loan(
            copy=copy,
            issue_date=today - timedelta(days=30),
            due_date=today - timedelta(days=16),
            return_date=today - timedelta(days=20),
        )

        row = self.row(self.get(), self.masnavi)

        self.assertEqual((row.total_copies, row.available_copies,
                          row.issued_copies), (1, 1, 0))

    def test_a_withdrawn_copy_is_neither_available_nor_out(self):
        make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="S-5",
            status=BookCopy.STATUS_TRANSFERRED,
        )

        row = self.row(self.get(), self.masnavi)

        self.assertEqual((row.total_copies, row.available_copies,
                          row.issued_copies), (1, 0, 0))

    def test_the_counts_add_up_across_a_mixture(self):
        second = make_volume(book=self.masnavi, volume_number=2)
        make_copy(volume=self.volume, shelf=self.shelf, copy_code="M-1")
        make_copy(volume=second, shelf=self.shelf, copy_code="M-2")
        make_copy(volume=second, shelf=None, copy_code="M-3")
        make_copy(
            volume=second, shelf=self.shelf, copy_code="M-4",
            status=BookCopy.STATUS_LOST,
        )
        make_loan(copy=make_copy(
            volume=second, shelf=self.shelf, copy_code="M-5",
        ))

        row = self.row(self.get(), self.masnavi)

        self.assertEqual(row.total_copies, 5)
        self.assertEqual(row.available_copies, 2)
        self.assertEqual(row.issued_copies, 1)

    def test_counts_belong_only_to_their_own_book(self):
        make_copy(volume=self.volume, shelf=self.shelf, copy_code="OWN-1")
        other = make_volume(book=self.ihya, volume_number=1)
        make_copy(volume=other, shelf=self.shelf, copy_code="OWN-2")
        make_copy(volume=other, shelf=self.shelf, copy_code="OWN-3")

        response = self.get()

        self.assertEqual(self.row(response, self.masnavi).total_copies, 1)
        self.assertEqual(self.row(response, self.ihya).total_copies, 2)

    def test_zeroes_are_not_printed_three_times(self):
        make_copy(volume=self.volume, shelf=self.shelf, copy_code="Z-1")

        body = self.get().content.decode()

        self.assertIn("1 available", body)
        self.assertNotIn("0 out", body)
        self.assertNotIn("0 available", body)


class AvailabilityFilterTests(BookSearchTestCase):

    def setUp(self):
        super().setUp()
        volume = make_volume(book=self.masnavi, volume_number=1)
        make_copy(volume=volume, shelf=self.shelf, copy_code="AV-1")

        other = make_volume(book=self.ihya, volume_number=1)
        make_loan(copy=make_copy(
            volume=other, shelf=self.shelf, copy_code="AV-2",
        ))

        self.bare = make_book(title="Nothing Yet", author=make_author("Nobody"))

    def test_available_now_shows_only_books_with_a_free_copy(self):
        self.assertEqual(
            self.titles(self.get(availability="available")), ["Masnavi"]
        )

    def test_something_out_shows_only_books_with_a_copy_on_loan(self):
        self.assertEqual(
            self.titles(self.get(availability="issued")), ["Ihya Ulum al-Din"]
        )

    def test_no_copies_shows_only_books_without_any(self):
        self.assertEqual(
            self.titles(self.get(availability="none")), ["Nothing Yet"]
        )

    def test_an_unknown_value_is_ignored(self):
        response = self.get(availability="nonsense")

        self.assertEqual(response.context["availability"], "")
        self.assertEqual(response.context["paginator"].count, 3)

    def test_it_combines_with_the_search(self):
        response = self.get(search="Rumi", availability="available")

        self.assertEqual(self.titles(response), ["Masnavi"])

        response = self.get(search="Rumi", availability="issued")

        self.assertEqual(response.context["paginator"].count, 0)

    def test_it_shows_as_a_chip_that_clears_itself(self):
        response = self.get(availability="available")

        labels = [f["label"] for f in response.context["active_filters"]]
        self.assertIn("Availability", labels)

        chip = next(
            f for f in response.context["active_filters"]
            if f["label"] == "Availability"
        )
        self.assertNotIn("availability", chip["url"])

    def test_the_panel_opens_itself_when_it_is_in_force(self):
        self.assertTrue(self.get(availability="issued").context["secondary_active"])
        self.assertFalse(self.get().context["secondary_active"])


class PublisherFilterTests(BookSearchTestCase):

    def setUp(self):
        super().setUp()
        self.publisher = make_publisher(name="Darul Ishaat")
        self.theirs = make_book(
            title="Their Book", author=make_author("Their Author"),
            publisher=self.publisher,
        )

    def test_it_now_has_a_control(self):
        response = self.get()

        self.assertContains(response, 'name="publisher"')
        self.assertContains(response, "Darul Ishaat")
        self.assertIn(
            self.publisher.id,
            [p.id for p in response.context["publishers"]],
        )

    def test_it_filters(self):
        self.assertEqual(
            self.titles(self.get(publisher=self.publisher.id)), ["Their Book"]
        )

    def test_it_combines_with_the_search(self):
        response = self.get(search="Their", publisher=self.publisher.id)

        self.assertEqual(self.titles(response), ["Their Book"])

    def test_the_panel_opens_itself_for_it(self):
        self.assertTrue(
            self.get(publisher=self.publisher.id).context["secondary_active"]
        )


class SearchAndPagingTests(BookSearchTestCase):

    def setUp(self):
        super().setUp()
        for number in range(30):
            make_book(
                title="Rumi Volume %02d" % number,
                author=make_author("Filler %d" % number),
            )

    def test_the_search_survives_a_page_change(self):
        response = self.get(search="Rumi", page_size=10, page=2)

        self.assertEqual(response.context["search"], "Rumi")
        self.assertTrue(
            all("Rumi" in b.title or "Rumi" in b.author.name
                for b in response.context["books"])
        )

    def test_paging_links_carry_the_search(self):
        response = self.get(search="Rumi", page_size=10)

        self.assertIn("search=Rumi", response.context["pagination_query"])

    def test_paging_links_carry_the_secondary_filters(self):
        response = self.get(availability="none", page_size=10)

        self.assertIn(
            "availability=none", response.context["pagination_query"]
        )

    def test_the_count_is_the_matches_not_the_page(self):
        response = self.get(search="Rumi", page_size=10)

        self.assertEqual(response.context["paginator"].count, 31)
        self.assertEqual(len(response.context["books"]), 10)


class QueryCostTests(BookSearchTestCase):

    def test_the_counts_do_not_cost_a_query_per_row(self):
        volume = make_volume(book=self.masnavi, volume_number=1)
        make_copy(volume=volume, shelf=self.shelf, copy_code="Q-1")

        with CaptureQueriesContext(connection) as few:
            self.client.get(self.url, {"page_size": 100})

        for number in range(20):
            book = make_book(
                title="Extra %02d" % number,
                author=make_author("Extra Author %d" % number),
            )
            extra = make_volume(book=book, volume_number=1)
            for index in range(3):
                make_copy(
                    volume=extra, shelf=self.shelf,
                    copy_code="Q-%d-%d" % (number, index),
                )

        with CaptureQueriesContext(connection) as many:
            response = self.client.get(self.url, {"page_size": 100})

        self.assertEqual(len(many), len(few))
        self.assertEqual(response.context["paginator"].count, 22)

    def test_searching_by_author_costs_no_extra_query(self):
        with CaptureQueriesContext(connection) as plain:
            self.client.get(self.url)

        with CaptureQueriesContext(connection) as searched:
            self.client.get(self.url, {"search": "Rumi"})

        self.assertEqual(len(searched), len(plain))


class ArchivedStillWorksTests(BookSearchTestCase):
    """Task 5's behaviour, unchanged by the new search and filters."""

    def setUp(self):
        super().setUp()
        self.ihya.archived_at = timezone.now()
        self.ihya.save(update_fields=["archived_at"])

    def test_the_catalogue_still_excludes_archived_books(self):
        self.assertEqual(self.titles(self.get()), ["Masnavi"])

    def test_searching_by_author_does_not_surface_them(self):
        self.assertEqual(
            self.get(search="Ghazali").context["paginator"].count, 0
        )

    def test_the_archive_still_shows_them(self):
        self.assertEqual(
            self.titles(self.get(archived="1")), ["Ihya Ulum al-Din"]
        )

    def test_searching_by_author_works_inside_the_archive(self):
        response = self.get(archived="1", search="Ghazali")

        self.assertEqual(self.titles(response), ["Ihya Ulum al-Din"])

    def test_the_archive_carries_the_copy_counts_too(self):
        volume = make_volume(book=self.ihya, volume_number=1)
        make_copy(volume=volume, shelf=self.shelf, copy_code="ARCH-1")

        row = self.row(self.get(archived="1"), self.ihya)

        self.assertEqual(row.total_copies, 1)
        self.assertEqual(row.available_copies, 1)

    def test_availability_filters_inside_the_archive(self):
        response = self.get(archived="1", availability="none")

        self.assertEqual(self.titles(response), ["Ihya Ulum al-Din"])
