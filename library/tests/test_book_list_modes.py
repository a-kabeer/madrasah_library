from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .helpers import (
    make_author,
    make_book,
    make_category,
    make_copy,
    make_location,
    make_publisher,
    make_shelf,
    make_user,
    make_volume,
)


class BookListModeTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.rumi = make_author(name="Jalaluddin Rumi")
        cls.ghazali = make_author(name="Imam Al-Ghazali")

        cls.poetry = make_category(name="Poetry")
        cls.fiqh = make_category(name="Fiqh")

        cls.publisher = make_publisher(name="Darul Ishaat")

        cls.masnavi = make_book(
            title="Masnavi", author=cls.rumi, category=cls.poetry,
            publisher=cls.publisher,
        )
        cls.divan = make_book(
            title="Divan e Shams", author=cls.rumi, category=cls.poetry,
        )
        cls.ihya = make_book(
            title="Ihya Ulum al-Din", author=cls.ghazali, category=cls.fiqh,
        )

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")
        self.url = reverse("book_list")

    def get(self, **params):
        return self.client.get(self.url, params)

    def titles(self, response):
        return sorted(book.title for book in response.context["books"])


class ModeTests(BookListModeTestCase):

    def test_default_mode_is_all_books(self):
        response = self.get()

        self.assertEqual(response.context["mode"], "all")

    def test_each_mode_is_accepted(self):
        for mode in ("all", "author", "category"):
            with self.subTest(mode=mode):
                response = self.get(mode=mode)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["mode"], mode)

    def test_unknown_mode_falls_back_to_all(self):
        for mode in ("publisher", "", "../etc", "ALL"):
            with self.subTest(mode=mode):
                response = self.get(mode=mode)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["mode"], "all")

    def test_mode_links_clear_the_other_modes_filters(self):
        response = self.get(
            mode="all", search="Masnavi", author=self.rumi.id, page=2
        )

        for link in response.context["mode_links"]:
            with self.subTest(mode=link["key"]):
                self.assertNotIn("search=", link["url"])
                self.assertNotIn("author=", link["url"])
                self.assertNotIn("category=", link["url"])
                self.assertNotIn("page=", link["url"])
                self.assertIn(f"mode={link['key']}", link["url"])

    def test_mode_links_keep_sorting_and_page_size(self):
        response = self.get(mode="all", sort="author", direction="desc", page_size=50)

        for link in response.context["mode_links"]:
            with self.subTest(mode=link["key"]):
                self.assertIn("sort=author", link["url"])
                self.assertIn("direction=desc", link["url"])
                self.assertIn("page_size=50", link["url"])

    def test_exactly_one_mode_is_active(self):
        response = self.get(mode="category")

        active = [
            link for link in response.context["mode_links"] if link["active"]
        ]

        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["key"], "category")

    def test_mode_survives_paging_and_sorting_links(self):
        response = self.get(mode="author", author=self.rumi.id, page_size=10)

        self.assertIn("mode=author", response.context["pagination_query"])

        for column in response.context["columns"]:
            with self.subTest(column=column["key"]):
                self.assertIn("mode=author", column["url"])

        self.assertIn("mode=author", response.context["page_size_hx_url"])


class SearchTests(BookListModeTestCase):

    def test_search_matches_book_title(self):
        response = self.get(search="Masnavi")

        self.assertEqual(self.titles(response), ["Masnavi"])

    def test_search_matches_partial_book_title(self):
        response = self.get(search="Ulum")

        self.assertEqual(self.titles(response), ["Ihya Ulum al-Din"])

    def test_search_does_not_match_the_author_name(self):
        # Title only: author has its own mode, so folding it in here would
        # make one control quietly overlap another.
        response = self.get(search="Jalaluddin Rumi")

        self.assertEqual(response.context["paginator"].count, 0)

    def test_search_does_not_match_category_or_publisher(self):
        for term in ("Poetry", "Fiqh", "Darul Ishaat"):
            with self.subTest(term=term):
                self.assertEqual(
                    self.get(search=term).context["paginator"].count, 0
                )

    def test_search_is_case_insensitive(self):
        for term in ("MASNAVI", "masnavi", "mAsNaVi"):
            with self.subTest(term=term):
                self.assertEqual(self.titles(self.get(search=term)), ["Masnavi"])

    def test_search_matches_a_title_containing_an_author_name(self):
        # "Rumi" appears in this title, so it matches on the title alone —
        # not because the author is also called Rumi.
        book = make_book(title="Rumi Anthology", author=self.ghazali)

        response = self.get(search="Rumi", page_size=100)

        ids = [b.id for b in response.context["books"]]

        self.assertEqual(ids, [book.id])

    def test_search_with_no_match_reports_zero(self):
        response = self.get(search="Nothing Like This")

        self.assertEqual(response.context["paginator"].count, 0)
        self.assertContains(response, "No books found.")


class FilterVisibilityTests(BookListModeTestCase):

    def test_publisher_still_filters_without_a_control(self):
        response = self.get(publisher=self.publisher.id)

        self.assertEqual(self.titles(response), ["Masnavi"])

    def test_active_filters_are_listed_so_none_is_invisible(self):
        response = self.get(
            mode="author",
            search="Masnavi",
            author=self.rumi.id,
            publisher=self.publisher.id,
        )

        labels = {
            item["label"]: item["value"]
            for item in response.context["active_filters"]
        }

        self.assertEqual(labels["Search"], "Masnavi")
        self.assertEqual(labels["Author"], "Jalaluddin Rumi")
        self.assertEqual(labels["Publisher"], "Darul Ishaat")

    def test_no_chips_when_nothing_is_filtered(self):
        self.assertEqual(self.get().context["active_filters"], [])

    def test_each_chip_removes_only_its_own_filter(self):
        response = self.get(search="Masnavi", author=self.rumi.id)

        chips = {
            item["label"]: item["url"]
            for item in response.context["active_filters"]
        }

        self.assertNotIn("search=", chips["Search"])
        self.assertIn("author=", chips["Search"])

        self.assertNotIn("author=", chips["Author"])
        self.assertIn("search=", chips["Author"])

    def test_search_chip_also_clears_the_legacy_title_alias(self):
        response = self.get(title="Masnavi")

        chip = response.context["active_filters"][0]

        self.assertNotIn("title=", chip["url"])

    def test_non_numeric_filter_values_do_not_error(self):
        # These go into a filter on an integer column, where a bad value
        # would otherwise raise ValueError and return a 500.
        for field in ("author", "category", "publisher"):
            for value in ("abc", "__ID__", "1; DROP TABLE books", "1.5"):
                with self.subTest(field=field, value=value):
                    response = self.get(**{field: value})

                    self.assertEqual(response.status_code, 200)
                    # Treated as no filter at all.
                    self.assertEqual(response.context["paginator"].count, 3)


class AuthorAndCategoryFilterTests(BookListModeTestCase):

    def test_selecting_an_author_filters_the_list(self):
        response = self.get(mode="author", author=self.rumi.id)

        self.assertEqual(
            self.titles(response), ["Divan e Shams", "Masnavi"]
        )
        self.assertEqual(response.context["author_name"], "Jalaluddin Rumi")

    def test_selecting_a_category_filters_the_list(self):
        response = self.get(mode="category", category=self.fiqh.id)

        self.assertEqual(self.titles(response), ["Ihya Ulum al-Din"])
        self.assertEqual(response.context["category_name"], "Fiqh")

    def test_filter_is_a_real_form_that_works_without_javascript(self):
        # The dropdown auto-submits from JS, but it must be a genuine form:
        # a URL assembled in JavaScript left the filter dead whenever that
        # script was stale or blocked.
        for mode, field in (("author", "author"), ("category", "category")):
            with self.subTest(mode=mode):
                response = self.get(mode=mode)
                html = response.content.decode()

                self.assertIn("data-filter-form", html)
                self.assertIn('method="get"', html)
                # A submit path exists even with no JS at all.
                self.assertIn('type="submit"', html)
                self.assertIn("data-combobox-submit", html)
                self.assertIn(f'name="{field}"', html)

    def test_filter_form_carries_the_table_state(self):
        response = self.get(
            mode="author", sort="author", direction="desc", page_size=50
        )

        self.assertContains(response, 'name="mode" value="author"')
        self.assertContains(response, 'name="sort" value="author"')
        self.assertContains(response, 'name="direction" value="desc"')
        self.assertContains(response, 'name="page_size" value="50"')

    def test_submitting_the_filter_form_filters_the_list(self):
        # Exactly what the browser sends when the form is submitted.
        response = self.get(
            mode="author", author=self.rumi.id, sort="title",
            direction="asc", page_size=25,
        )

        self.assertEqual(
            self.titles(response), ["Divan e Shams", "Masnavi"]
        )

    def test_clearing_the_filter_restores_the_full_list(self):
        response = self.get(mode="author", author="")

        self.assertEqual(response.context["paginator"].count, 3)
        self.assertEqual(response.context["active_filters"], [])

    def test_author_filter_survives_paging_sorting_and_page_size(self):
        for extra in ({"page": 1}, {"sort": "title", "direction": "desc"},
                      {"page_size": 50}):
            with self.subTest(**extra):
                response = self.get(
                    mode="author", author=self.rumi.id, **extra
                )

                self.assertEqual(
                    self.titles(response), ["Divan e Shams", "Masnavi"]
                )


class BookDetailModalTests(BookListModeTestCase):

    def setUp(self):
        super().setUp()
        self.detail_url = reverse("book_detail", args=[self.masnavi.id])

    def test_plain_request_returns_the_full_page(self):
        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "library/book_detail.html")
        self.assertContains(response, "<html")

    def test_modal_request_returns_only_the_fragment(self):
        response = self.client.get(
            self.detail_url, {"modal": "1"}, headers={"HX-Request": "true"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response, "library/partials/book_detail_modal.html"
        )
        self.assertNotContains(response, "<html")

    def test_the_header_alone_does_not_return_a_fragment(self):
        # Branching on the header alone would give one URL two bodies, which
        # a cache could then serve to the wrong kind of request.
        response = self.client.get(
            self.detail_url, headers={"HX-Request": "true"}
        )

        self.assertTemplateUsed(response, "library/book_detail.html")

    def test_the_parameter_alone_does_not_return_a_fragment(self):
        response = self.client.get(self.detail_url, {"modal": "1"})

        self.assertTemplateUsed(response, "library/book_detail.html")

    def modal(self, book):
        return self.client.get(
            reverse("book_detail", args=[book.id]),
            {"modal": "1"},
            headers={"HX-Request": "true"},
        )

    def test_modal_shows_the_real_book_fields(self):
        response = self.modal(self.masnavi)

        self.assertContains(response, "Masnavi")
        self.assertContains(response, "Jalaluddin Rumi")
        self.assertContains(response, "Poetry")
        self.assertContains(response, "Darul Ishaat")

    def test_modal_handles_missing_category_and_publisher(self):
        response = self.modal(self.divan)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not set")

    def test_modal_lists_volumes_and_copies_with_shelf_and_location(self):
        volume = make_volume(book=self.masnavi, volume_number=1, title="Book One")
        location = make_location(name="Main Hall")
        shelf = make_shelf(location=location, shelf_code="A1")
        make_copy(volume=volume, shelf=shelf, copy_code="MSN-001")

        response = self.modal(self.masnavi)

        self.assertContains(response, "Volume 1")
        self.assertContains(response, "Book One")
        self.assertContains(response, "MSN-001")
        self.assertContains(response, "Main Hall")
        self.assertContains(response, "A1")

    def test_modal_handles_a_copy_with_no_shelf(self):
        volume = make_volume(book=self.masnavi, volume_number=1)
        # make_copy leaves shelf NULL unless one is passed.
        make_copy(volume=volume, copy_code="MSN-002")

        response = self.modal(self.masnavi)

        self.assertContains(response, "MSN-002")
        self.assertContains(response, "no shelf assigned")

    def test_modal_shows_a_volume_that_has_no_copies(self):
        # Driving the list from copies alone would hide this volume entirely.
        make_volume(book=self.masnavi, volume_number=7, title="Unstocked")

        response = self.modal(self.masnavi)

        self.assertContains(response, "Volume 7")
        self.assertContains(response, "Unstocked")

    def test_modal_query_count_does_not_grow_with_the_number_of_copies(self):
        # Absolute counts are not asserted: they include incidental session,
        # auth and branding queries. The property that matters is that the
        # count does not change as rows are added.
        volume = make_volume(book=self.masnavi, volume_number=1)
        location = make_location(name="Main Hall")
        shelf = make_shelf(location=location, shelf_code="A1")

        make_copy(volume=volume, shelf=shelf, copy_code="C-1")

        with CaptureQueriesContext(connection) as one_copy:
            self.modal(self.masnavi)

        for index in range(2, 12):
            make_copy(volume=volume, shelf=shelf, copy_code=f"C-{index}")

        with CaptureQueriesContext(connection) as eleven_copies:
            self.modal(self.masnavi)

        self.assertEqual(len(one_copy), len(eleven_copies))

        # And the copies arrive in a single joined query, not one per row.
        copy_queries = [
            q for q in eleven_copies.captured_queries
            if "book_copies" in q["sql"]
        ]

        self.assertEqual(len(copy_queries), 1)
        self.assertIn("shelves", copy_queries[0]["sql"])
        self.assertIn("locations", copy_queries[0]["sql"])

    def test_a_multi_volume_page_does_not_pay_for_the_copies_query(self):
        # It lists volumes and their counts, not the copies themselves, so
        # it must not fetch them. (A single-volume book deliberately does
        # show its copies — see the copy navigation tests.)
        for number in (1, 2):
            volume = make_volume(book=self.masnavi, volume_number=number)
            make_copy(volume=volume, copy_code="C-%d" % number)

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(self.detail_url)

        rows = [
            q for q in ctx.captured_queries
            if "book_copies" in q["sql"]
        ]

        # The per-volume count joins book_copies, but only to count them.
        self.assertEqual(len(rows), 1)
        self.assertIn("COUNT", rows[0]["sql"].upper())


class BookListRowTests(BookListModeTestCase):

    def test_whole_row_carries_the_details_url(self):
        # The row itself is the control, so a click anywhere in it resolves
        # to one action.
        response = self.get()

        self.assertContains(response, "data-book-row")
        self.assertContains(
            response,
            f'data-book-url="{reverse("book_detail", args=[self.masnavi.id])}?modal=1"',
        )
        self.assertContains(response, 'role="button"')

    def test_row_contains_no_competing_links(self):
        # Anything clickable inside the row other than Edit/Delete would give
        # part of the row a different behaviour from the rest.
        response = self.get(page_size=100)
        html = response.content.decode()

        self.assertNotIn("author-name-link", html)
        self.assertNotIn("book-name-link", html)
        # The author name is plain text now, not a filter link.
        self.assertNotIn(f'?mode=author&amp;author={self.rumi.id}"', html)

    def test_actions_are_only_edit_and_delete_and_are_excluded_from_the_row(self):
        response = self.get()
        html = response.content.decode()

        self.assertIn(reverse("book_edit", args=[self.masnavi.id]), html)
        self.assertIn(reverse("book_delete", args=[self.masnavi.id]), html)

        # No View button.
        self.assertNotIn("btn-outline-info", html)

        # Marked so the row's click handler leaves them alone.
        self.assertIn("data-row-actions", html)

    def test_every_mode_gets_clickable_rows(self):
        for mode, params in (
            ("all", {}),
            ("author", {"author": self.rumi.id}),
            ("category", {"category": self.poetry.id}),
        ):
            with self.subTest(mode=mode):
                response = self.get(mode=mode, **params)

                self.assertContains(response, "data-book-url")
                self.assertContains(response, "data-book-row")

    def test_rows_expose_their_own_cover_for_the_hover_preview(self):
        # The URL is on the row, so the preview needs no extra request and
        # cannot show another book's cover.
        from django.core.files.uploadedfile import SimpleUploadedFile
        import io

        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (40, 60), (10, 80, 200)).save(buffer, format="PNG")

        self.masnavi.cover_image.save(
            "masnavi.png",
            SimpleUploadedFile("masnavi.png", buffer.getvalue()),
            save=True,
        )

        try:
            response = self.get(page_size=100)

            self.assertContains(response, "data-cover-url")
            self.assertContains(response, self.masnavi.cover_image.url)

            # Books with no cover carry no attribute, which is how the
            # preview skips them rather than showing a broken image.
            html = response.content.decode()
            self.assertEqual(html.count("data-cover-url"), 1)

        finally:
            self.masnavi.cover_image.delete(save=True)

    def test_list_query_count_is_flat_regardless_of_page_size(self):
        for _ in range(20):
            make_book(title="Filler", author=self.rumi, category=self.poetry)

        with CaptureQueriesContext(connection) as ten_rows:
            self.get(page_size=10)

        with CaptureQueriesContext(connection) as hundred_rows:
            self.get(page_size=100)

        # Ten times the rows, same number of queries: select_related covers
        # author, category and publisher, so no row triggers its own lookup.
        self.assertEqual(len(ten_rows), len(hundred_rows))

        # And the rows come from one query that joins all three relations.
        book_queries = [
            q for q in hundred_rows.captured_queries
            if 'FROM "books"' in q["sql"] and "COUNT(*)" not in q["sql"]
        ]

        self.assertEqual(len(book_queries), 1)

        for table in ("authors", "categories", "publishers"):
            with self.subTest(table=table):
                self.assertIn(table, book_queries[0]["sql"])


class BookListPermissionTests(BookListModeTestCase):

    def test_assistant_can_still_browse_every_mode(self):
        self.client.logout()
        make_user(username="assistant_u", password="pass12345", role="Assistant")
        self.client.login(username="assistant_u", password="pass12345")

        for mode in ("all", "author", "category"):
            with self.subTest(mode=mode):
                self.assertEqual(self.get(mode=mode).status_code, 200)

    def test_assistant_can_open_the_details_modal(self):
        self.client.logout()
        make_user(username="assistant_u", password="pass12345", role="Assistant")
        self.client.login(username="assistant_u", password="pass12345")

        response = self.client.get(
            reverse("book_detail", args=[self.masnavi.id]),
            {"modal": "1"},
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)

    def test_edit_and_delete_still_enforce_roles(self):
        for role, expected in (
            ("Admin", 200), ("Librarian", 200), ("Assistant", 403),
        ):
            with self.subTest(role=role):
                self.client.logout()
                make_user(
                    username=f"user_{role}", password="pass12345", role=role
                )
                self.client.login(
                    username=f"user_{role}", password="pass12345"
                )

                for name in ("book_edit", "book_delete"):
                    response = self.client.get(
                        reverse(name, args=[self.masnavi.id])
                    )

                    self.assertEqual(response.status_code, expected)


class InPlaceUpdateTests(BookListModeTestCase):
    """Nothing on the book list reloads the page.

    Every action asks for a fragment by naming it in `partial`, alongside
    the HTMX header, and the browser swaps the answer into place:

      partial=results — search, filter, sort, page, page size
      partial=browser — a change of browsing mode, which also replaces the
                        mode pills and the filter control
    """

    def fragment(self, **params):
        params.setdefault("partial", "results")

        return self.client.get(
            self.url, params, headers={"HX-Request": "true"}
        )

    def test_fragment_request_returns_only_the_results(self):
        response = self.fragment(search="Masnavi")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response, "library/partials/book_list_results.html"
        )
        self.assertTemplateNotUsed(response, "library/book_list.html")

        body = response.content.decode()

        # The table is there; the page and the controls around it are not.
        self.assertIn("Masnavi", body)
        self.assertNotIn("nav-pills", body)
        self.assertNotIn('id="bookResults"', body)
        self.assertNotIn('id="filterSearch"', body)

    def test_fragment_applies_the_same_filtering_as_the_page(self):
        response = self.fragment(mode="author", author=self.ghazali.id)

        self.assertEqual(self.titles(response), ["Ihya Ulum al-Din"])

    def test_browser_fragment_returns_the_controls_and_the_results(self):
        # A mode change: the pills and the filter card move too, because
        # which control is shown is the point of the mode.
        response = self.client.get(
            self.url,
            {"mode": "category", "partial": "browser"},
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response, "library/partials/book_list_browser.html"
        )
        self.assertTemplateUsed(
            response, "library/partials/book_list_results.html"
        )
        self.assertTemplateNotUsed(response, "library/book_list.html")

        body = response.content.decode()

        # The pills, the Category control and the table — but not the page.
        self.assertIn("nav-pills", body)
        self.assertIn('id="filterCategory"', body)
        self.assertIn('id="bookResults"', body)
        self.assertNotIn('id="bookBrowser"', body)

    def test_browser_fragment_shows_the_control_for_its_mode(self):
        for mode, present, absent in (
            ("all", 'id="filterSearch"', 'id="filterAuthor"'),
            ("author", 'id="filterAuthor"', 'id="filterSearch"'),
            ("category", 'id="filterCategory"', 'id="filterAuthor"'),
        ):
            with self.subTest(mode=mode):
                response = self.client.get(
                    self.url,
                    {"mode": mode, "partial": "browser"},
                    headers={"HX-Request": "true"},
                )

                self.assertContains(response, present)
                self.assertNotContains(response, absent)

    def test_browser_fragment_gives_its_dropdown_a_search_url(self):
        # The URL was once assigned in book_list.html, outside the fragment,
        # so a mode change rendered hx-get="" and the dropdown searched the
        # book list instead of the authors — filling its menu with a table.
        for mode, url_name in (("author", "author_list"),
                               ("category", "category_list")):
            with self.subTest(mode=mode):
                response = self.client.get(
                    self.url,
                    {"mode": mode, "partial": "browser"},
                    headers={"HX-Request": "true"},
                )

                self.assertContains(
                    response, 'hx-get="%s"' % reverse(url_name)
                )
                self.assertNotContains(response, 'hx-get=""')

    def test_dropdown_search_does_not_inherit_the_fragment_flag(self):
        # HTMX merges hx-vals down from every ancestor and hx-disinherit
        # does not cover it, so the filter form — which encloses the
        # dropdown — must carry `partial` in its URL instead. Getting this
        # wrong sent the flag along with the dropdown's own search, which
        # then came back holding a table.
        response = self.get(mode="author")
        body = response.content.decode()

        form = body[body.index("<form"):body.index(">", body.index("<form"))]

        self.assertIn("?partial=results", form)
        self.assertNotIn("hx-vals", form)

        # And the dropdown blanks it, whatever encloses it.
        self.assertContains(response, 'partial: ""')

    def test_unknown_fragment_name_returns_the_full_page(self):
        response = self.client.get(
            self.url, {"partial": "nonsense"}, headers={"HX-Request": "true"}
        )

        self.assertTemplateUsed(response, "library/book_list.html")

    def test_header_without_the_parameter_returns_the_full_page(self):
        # One URL must not return two different bodies: a cache keyed on the
        # URL alone would otherwise be free to mix them up.
        response = self.client.get(
            self.url, {"search": "Masnavi"}, headers={"HX-Request": "true"}
        )

        self.assertTemplateUsed(response, "library/book_list.html")

    def test_parameter_without_the_header_returns_the_full_page(self):
        for name in ("results", "browser"):
            with self.subTest(partial=name):
                response = self.client.get(self.url, {"partial": name})

                self.assertTemplateUsed(response, "library/book_list.html")

    def test_fragment_pushes_the_url_without_the_partial_flag(self):
        response = self.fragment(mode="author", author=self.rumi.id)

        pushed = response["HX-Push-Url"]

        self.assertNotIn("partial", pushed)
        self.assertIn("mode=author", pushed)
        self.assertIn(f"author={self.rumi.id}", pushed)
        self.assertTrue(pushed.startswith(self.url))

    def test_mode_change_pushes_the_url_without_the_partial_flag(self):
        response = self.client.get(
            self.url,
            {"mode": "author", "partial": "browser"},
            headers={"HX-Request": "true"},
        )

        pushed = response["HX-Push-Url"]

        self.assertNotIn("partial", pushed)
        self.assertIn("mode=author", pushed)

    def test_pushed_url_is_bare_when_nothing_is_selected(self):
        response = self.fragment()

        self.assertEqual(response["HX-Push-Url"], self.url)

    def test_links_in_the_fragment_never_carry_the_partial_flag(self):
        # Otherwise the flag would be pushed into the address bar the moment
        # anyone clicked a sort header, and shared as part of the URL.
        response = self.fragment(mode="author", author=self.rumi.id)

        for column in response.context["columns"]:
            with self.subTest(column=column["key"]):
                self.assertNotIn("partial", column["url"])

        self.assertNotIn("partial", response.context["pagination_query"])
        self.assertNotIn("partial", response.context["page_size_hx_url"])

        for filter_ in response.context["active_filters"]:
            with self.subTest(filter=filter_["label"]):
                self.assertNotIn("partial", filter_["url"])

    def test_fragment_costs_no_more_queries_than_the_page(self):
        with CaptureQueriesContext(connection) as fragment:
            self.fragment(search="Masnavi")

        with CaptureQueriesContext(connection) as page:
            self.get(search="Masnavi")

        self.assertLessEqual(len(fragment), len(page))
