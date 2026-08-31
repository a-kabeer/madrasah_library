from django.test import TestCase
from django.urls import reverse

from library.views import PAGE_SIZE, PAGE_SIZE_CHOICES

from .helpers import make_author, make_book, make_category, make_publisher, make_user


class BookListTableTestCase(TestCase):
    """Shared fixture: enough books to page through, with some blanks."""

    @classmethod
    def setUpTestData(cls):
        cls.alpha = make_author(name="Author Alpha")
        cls.zulu = make_author(name="Author Zulu")
        cls.category = make_category(name="Cat Beta")
        cls.publisher = make_publisher(name="Pub Gamma")

        for index in range(1, 31):
            make_book(
                title=f"Book {index:02d}",
                author=cls.alpha if index % 2 else cls.zulu,
                # Every third book has no category or publisher, so null
                # ordering is exercised.
                category=None if index % 3 == 0 else cls.category,
                publisher=None if index % 3 == 0 else cls.publisher,
            )

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")
        self.url = reverse("book_list")

    def get(self, **params):
        return self.client.get(self.url, params)

    def titles(self, response):
        return [book.title for book in response.context["books"]]


class SortingTests(BookListTableTestCase):

    def test_default_sort_is_title_ascending(self):
        response = self.get()

        self.assertEqual(response.context["sort"], "title")
        self.assertEqual(response.context["direction"], "asc")
        self.assertEqual(self.titles(response)[0], "Book 01")

    def test_each_column_sorts_both_ways(self):
        cases = {
            "id": lambda book: book.id,
            "title": lambda book: book.title,
            "author": lambda book: book.author.name,
        }

        for key, value_of in cases.items():
            for direction in ("asc", "desc"):
                with self.subTest(sort=key, direction=direction):
                    response = self.get(
                        sort=key, direction=direction, page_size=100
                    )

                    values = [
                        value_of(book) for book in response.context["books"]
                    ]

                    self.assertEqual(
                        values,
                        sorted(values, reverse=direction == "desc"),
                    )

    def test_nullable_columns_keep_blanks_last_in_both_directions(self):
        # Flipping direction should not drag the empty rows to the top.
        for direction in ("asc", "desc"):
            with self.subTest(direction=direction):
                response = self.get(
                    sort="category", direction=direction, page_size=100
                )

                names = [
                    book.category.name if book.category else None
                    for book in response.context["books"]
                ]

                filled = [i for i, name in enumerate(names) if name]
                blank = [i for i, name in enumerate(names) if not name]

                self.assertTrue(blank, "fixture should include blank rows")
                self.assertGreater(min(blank), max(filled))

    def test_unknown_sort_falls_back_to_the_default(self):
        response = self.get(sort="nonsense", direction="sideways")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sort"], "title")
        self.assertEqual(response.context["direction"], "asc")

    def test_sort_parameter_cannot_reach_arbitrary_fields(self):
        # Only whitelisted keys are accepted, so a crafted value can neither
        # traverse relations nor raise FieldError into a 500.
        for attempt in (
            "author__user__password",
            "cover_image",
            "author__id",
            "-title",
        ):
            with self.subTest(sort=attempt):
                response = self.get(sort=attempt, page_size=100)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["sort"], "title")

    def test_header_links_flip_the_active_column_and_reset_others(self):
        response = self.get(sort="title", direction="asc")

        columns = {
            column["key"]: column
            for column in response.context["columns"]
            if column["key"]
        }

        self.assertTrue(columns["title"]["active"])
        self.assertIn("direction=desc", columns["title"]["url"])

        self.assertFalse(columns["author"]["active"])
        self.assertIn("direction=asc", columns["author"]["url"])

    def test_header_links_drop_the_page_parameter(self):
        response = self.get(sort="title", page=3, page_size=10)

        for column in response.context["columns"]:
            if column["url"]:
                with self.subTest(column=column["key"]):
                    self.assertNotIn("page=", column["url"])

    def test_cover_column_is_not_sortable(self):
        response = self.get()

        cover = [
            column for column in response.context["columns"]
            if column["key"] is None
        ]

        self.assertEqual(len(cover), 1)
        self.assertEqual(cover[0]["url"], "")


class PageSizeTests(BookListTableTestCase):

    def test_default_page_size(self):
        response = self.get()

        self.assertEqual(response.context["page_size"], PAGE_SIZE)
        self.assertEqual(len(self.titles(response)), PAGE_SIZE)

    def test_each_offered_size_is_honoured(self):
        for size in PAGE_SIZE_CHOICES:
            with self.subTest(page_size=size):
                response = self.get(page_size=size)

                self.assertEqual(response.context["page_size"], size)
                self.assertEqual(
                    len(self.titles(response)),
                    min(size, response.context["paginator"].count),
                )

    def test_sizes_we_do_not_offer_fall_back_to_the_default(self):
        for value in (7, 1000, 0, -5, "abc", ""):
            with self.subTest(page_size=value):
                response = self.get(page_size=value)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["page_size"], PAGE_SIZE)

    def test_size_links_reset_to_page_one_and_keep_state(self):
        response = self.get(
            page_size=10, page=3, sort="author", direction="desc", search="Book"
        )

        for option in response.context["page_size_options"]:
            with self.subTest(size=option["value"]):
                self.assertNotIn("page=", option["url"])
                self.assertIn("sort=author", option["url"])
                self.assertIn("direction=desc", option["url"])
                self.assertIn("search=Book", option["url"])


class PaginationTests(BookListTableTestCase):

    def test_page_navigation(self):
        # 30 books, 10 per page.
        response = self.get(page_size=10)
        page = response.context["books"]

        self.assertEqual(page.paginator.count, 30)
        self.assertEqual(page.number, 1)
        self.assertEqual(page.paginator.num_pages, 3)
        self.assertFalse(page.has_previous())
        self.assertTrue(page.has_next())

        response = self.get(page_size=10, page=2)
        page = response.context["books"]

        self.assertEqual(page.number, 2)
        self.assertTrue(page.has_previous())
        self.assertTrue(page.has_next())

        response = self.get(page_size=10, page=3)
        page = response.context["books"]

        self.assertEqual(page.number, 3)
        self.assertTrue(page.has_previous())
        self.assertFalse(page.has_next())

    def test_out_of_range_and_junk_pages_do_not_error(self):
        for value in (999, 0, -1, "abc"):
            with self.subTest(page=value):
                response = self.get(page_size=10, page=value)

                self.assertEqual(response.status_code, 200)

    def test_pagination_query_carries_state_but_not_page(self):
        response = self.get(
            page_size=10, page=2, sort="author", direction="desc", search="Book"
        )

        query = response.context["pagination_query"]

        self.assertIn("page_size=10", query)
        self.assertIn("sort=author", query)
        self.assertIn("direction=desc", query)
        self.assertIn("search=Book", query)
        self.assertNotIn("page=", query)

    def test_paging_never_repeats_or_skips_a_row(self):
        # Sorting on a column with many ties needs a stable tiebreaker, or
        # rows shuffle between pages: the same book shows twice while
        # another is missed entirely.
        for sort in ("author", "category", "publisher", "title", "id"):
            with self.subTest(sort=sort):
                seen = []
                page_number = 1

                while True:
                    response = self.get(
                        sort=sort, direction="asc", page_size=10,
                        page=page_number,
                    )
                    page = response.context["books"]
                    seen.extend(book.id for book in page)

                    if not page.has_next():
                        break

                    page_number += 1

                total = response.context["paginator"].count

                self.assertEqual(len(seen), total)
                self.assertEqual(len(set(seen)), total)


class FilterIntegrationTests(BookListTableTestCase):

    def test_search_filters_and_reports_the_matching_total(self):
        response = self.get(search="Book 1")

        # Book 10-19 match; "Book 01" does not contain "Book 1".
        self.assertEqual(response.context["paginator"].count, 10)

    def test_filters_sorting_and_paging_compose(self):
        response = self.get(
            author=self.alpha.id,
            sort="id",
            direction="desc",
            page_size=10,
            page=2,
        )

        page = response.context["books"]

        self.assertEqual(response.context["paginator"].count, 15)
        self.assertEqual(page.number, 2)
        self.assertEqual(response.context["sort"], "id")
        self.assertEqual(response.context["direction"], "desc")

        ids = [book.id for book in page]
        self.assertEqual(ids, sorted(ids, reverse=True))

        for book in page:
            self.assertEqual(book.author_id, self.alpha.id)

    def test_combined_filters_narrow_the_total(self):
        response = self.get(
            author=self.alpha.id,
            category=self.category.id,
            page_size=100,
        )

        for book in response.context["books"]:
            self.assertEqual(book.author_id, self.alpha.id)
            self.assertEqual(book.category_id, self.category.id)

    def test_filter_labels_survive_a_reload(self):
        response = self.get(
            author=self.alpha.id,
            category=self.category.id,
            publisher=self.publisher.id,
        )

        self.assertEqual(response.context["author_name"], "Author Alpha")
        self.assertEqual(response.context["category_name"], "Cat Beta")
        self.assertEqual(response.context["publisher_name"], "Pub Gamma")
        self.assertTrue(response.context["has_filters"])

    def test_legacy_title_parameter_still_filters(self):
        # Links made before the parameter was renamed to `search`.
        response = self.get(title="Book 1")

        self.assertEqual(response.context["paginator"].count, 10)
        self.assertEqual(response.context["search"], "Book 1")

    def test_no_filters_reports_no_active_filters(self):
        self.assertFalse(self.get().context["has_filters"])

    def test_filter_form_carries_sort_and_page_size(self):
        # Otherwise pressing Search would silently reset the table.
        response = self.get(sort="author", direction="desc", page_size=50)

        self.assertContains(response, 'name="sort" value="author"')
        self.assertContains(response, 'name="direction" value="desc"')
        self.assertContains(response, 'name="page_size" value="50"')
