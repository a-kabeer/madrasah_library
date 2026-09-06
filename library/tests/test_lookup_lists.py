"""Authors, Categories and Publishers: one table, three nouns.

The three pages are the same page with a different word in it, so the
tests are written once over all three and say which noun they are looking
at. What is worth checking is mostly about state surviving: a search that
keeps its sort, a sort that keeps its search, a page that keeps both. Each
of those is a separate query string built in a separate place, and any one
of them dropping a parameter is invisible until somebody uses the page.

The other half is the link on each name. It is not decoration - it is the
whole point of these pages - so the tests check the exact URL and then
follow it, because a link that is right in shape and wrong in effect
would pass the first check on its own.
"""

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import escape

from library.models import Author, Book, Category, Publisher
from library.views import PAGE_SIZE

from .helpers import (
    make_author,
    make_book,
    make_category,
    make_publisher,
    make_user,
)


# name, url name, the context key the page puts its page under, and the
# heading its one sortable name column carries.
LOOKUPS = (
    ("author", "author_list", "authors", "Author Name"),
    ("category", "category_list", "categories", "Category Name"),
    ("publisher", "publisher_list", "publishers", "Publisher Name"),
)

MODELS = {
    "author": Author,
    "category": Category,
    "publisher": Publisher,
}

# What the dialogs call each one.
LOOKUPS_LABEL = {
    "author": "Author",
    "category": "Category",
    "publisher": "Publisher",
}


class LookupListTestCase(TestCase):

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def make(self, kind, name):
        """One row of whichever lookup this is."""

        return {
            "author": make_author,
            "category": make_category,
            "publisher": make_publisher,
        }[kind](name=name)

    def book_under(self, kind, row, title="Some Book"):
        """A book filed under `row`, whichever field that means.

        Every book needs an author even when the author is not what is
        being counted, and author names are unique. subTests share one
        transaction, so the name has to be unique across all three of them
        as well - hence the kind in it.
        """

        fields = {"title": title, kind: row}

        if kind != "author":
            fields["author"] = make_author(
                name="Writer of %s %s" % (kind, title)
            )

        return make_book(**fields)

    def get(self, url_name, **params):
        return self.client.get(reverse(url_name), params)


class TheSharedTableTests(LookupListTestCase):

    def test_each_page_renders_the_shared_table(self):
        # One file behind all three, so a change to the table cannot reach
        # two of them and miss the third.
        for _kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                response = self.get(url_name)

                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(
                    response, "library/partials/lookup_list.html"
                )

    def test_each_page_names_its_own_column(self):
        for kind, url_name, _key, label in LOOKUPS:
            with self.subTest(page=url_name):

                self.make(kind, "Something")

                self.assertContains(self.get(url_name), label)

    def test_each_page_keeps_the_context_key_it_always_had(self):
        # `authors`, `categories`, `publishers` - the names the rest of the
        # project and its tests already ask the context for.
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.make(kind, "Named")

                response = self.get(url_name)

                self.assertEqual(len(response.context[key]), 1)
                self.assertEqual(
                    response.context[key].paginator.count, 1
                )

    def test_each_page_offers_the_rows_per_page_control(self):
        for _kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                response = self.get(url_name)

                self.assertContains(response, 'id="pageSize"')
                self.assertEqual(
                    response.context["page_size"], PAGE_SIZE
                )


class SearchTests(LookupListTestCase):

    def test_a_search_narrows_the_rows(self):
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.make(kind, "Ibn Kathir")
                self.make(kind, "Al-Ghazali")

                response = self.get(url_name, search="Ghaz")

                self.assertEqual(len(response.context[key]), 1)
                self.assertContains(response, "Al-Ghazali")
                self.assertNotContains(response, "Ibn Kathir")

    def test_the_search_is_matched_anywhere_in_the_name(self):
        self.make("author", "Muhammad ibn Idris")

        self.assertContains(
            self.get("author_list", search="ibn"), "Muhammad ibn Idris"
        )

    def test_the_box_keeps_what_was_typed(self):
        for _kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                response = self.get(url_name, search="Bukhari")

                self.assertEqual(response.context["search"], "Bukhari")
                self.assertContains(response, 'value="Bukhari"')

    def test_surrounding_space_is_not_part_of_the_search(self):
        self.make("category", "Fiqh")

        self.assertContains(self.get("category_list", search="  Fiqh  "), "Fiqh")

    def test_publishers_are_also_searched_by_city(self):
        # What this page has always done: a publisher is often remembered
        # by where it is.
        make_publisher(name="Dar al-Kutub", city="Beirut")
        make_publisher(name="Maktaba Rahmania", city="Lahore")

        response = self.get("publisher_list", search="Beirut")

        self.assertEqual(len(response.context["publishers"]), 1)
        self.assertContains(response, "Dar al-Kutub")

    def test_a_search_that_matches_nothing_says_so_and_offers_a_way_back(self):
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.make(kind, "Present")

                response = self.get(url_name, search="zzzz")

                self.assertEqual(len(response.context[key]), 0)
                self.assertContains(response, "Clear the search")

    def test_an_empty_list_says_something_different(self):
        # Nothing to clear, so nothing about clearing.
        for _kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                response = self.get(url_name)

                self.assertContains(response, "No %s yet" % key)
                self.assertNotContains(response, "Clear the search")

    def test_the_clear_button_appears_only_while_searching(self):
        self.assertNotContains(self.get("author_list"), "</i> Clear")
        self.assertContains(self.get("author_list", search="x"), "</i> Clear")


class SortingTests(LookupListTestCase):

    def names(self, response, key):
        return [row.name for row in response.context[key]]

    def test_rows_are_sorted_by_name_by_default(self):
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.make(kind, "Zubair")
                self.make(kind, "Anwar")
                self.make(kind, "Muneer")

                response = self.get(url_name)

                self.assertEqual(response.context["sort"], "name")
                self.assertEqual(response.context["direction"], "asc")
                self.assertEqual(
                    self.names(response, key), ["Anwar", "Muneer", "Zubair"]
                )

    def test_the_name_column_sorts_the_other_way(self):
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.make(kind, "Zubair")
                self.make(kind, "Anwar")

                response = self.get(url_name, sort="name", direction="desc")

                self.assertEqual(
                    self.names(response, key), ["Zubair", "Anwar"]
                )

    def test_the_book_count_is_sortable_too(self):
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                busy = self.make(kind, "Busy")
                quiet = self.make(kind, "Quiet")

                for i in range(3):
                    self.book_under(kind, busy, "Busy %d" % i)

                self.book_under(kind, quiet, "Quiet 0")

                response = self.get(url_name, sort="books", direction="desc")

                self.assertEqual(self.names(response, key), ["Busy", "Quiet"])

    def test_an_unknown_sort_key_falls_back_instead_of_failing(self):
        # The parameter reaches order_by(), so anything but a whitelisted
        # key would be a 500 - or a way to traverse relations.
        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.make(kind, "Row")

                response = self.get(url_name, sort="name; drop table")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["sort"], "name")

    def test_an_unknown_direction_falls_back(self):
        response = self.get("author_list", sort="name", direction="sideways")

        self.assertEqual(response.context["direction"], "asc")

    def test_the_active_column_offers_the_opposite_direction(self):
        response = self.get("author_list", sort="name", direction="asc")

        name = response.context["columns"][0]

        self.assertTrue(name["active"])
        self.assertIn("direction=desc", name["url"])

    def test_a_sort_link_keeps_the_search(self):
        # Sorting must not quietly widen the list back out again.
        for _kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                response = self.get(url_name, search="Dar")

                for column in response.context["columns"]:
                    self.assertIn("search=Dar", column["url"])

    def test_a_sort_link_returns_to_the_first_page(self):
        response = self.get("author_list", page="2")

        for column in response.context["columns"]:
            self.assertNotIn("page=", column["url"])

    def test_equal_rows_do_not_shuffle_between_pages(self):
        # Sorting by a count leaves ties, and ties in an unspecified order
        # let one row appear on two pages while another is skipped.
        for i in range(6):
            self.make("author", "Tied %d" % i)

        first = self.get("author_list", sort="books", page_size=10)
        again = self.get("author_list", sort="books", page_size=10)

        self.assertEqual(
            [row.id for row in first.context["authors"]],
            [row.id for row in again.context["authors"]],
        )


class BookCountTests(LookupListTestCase):

    def test_each_row_counts_the_books_filed_under_it(self):
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                row = self.make(kind, "Counted")

                for i in range(4):
                    self.book_under(kind, row, "Book %d" % i)

                counted = self.get(url_name).context[key][0]

                self.assertEqual(counted.book_count, 4)

    def test_a_row_with_no_books_says_so(self):
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.make(kind, "Unused")

                response = self.get(url_name)

                self.assertEqual(response.context[key][0].book_count, 0)
                self.assertContains(response, "No books")

    def test_archived_books_are_not_counted(self):
        # The count has to agree with what the link opens, and the book
        # list shows the active catalogue.
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                row = self.make(kind, "Half Archived")

                self.book_under(kind, row, "Still Here")
                gone = self.book_under(kind, row, "Put Away")

                Book.objects.filter(id=gone.id).update(
                    archived_at=timezone.now()
                )

                self.assertEqual(
                    self.get(url_name).context[key][0].book_count, 1
                )

    def test_a_book_filed_elsewhere_is_not_counted(self):
        wanted = make_author(name="Wanted")
        make_author(name="Other")

        make_book(title="Theirs", author=wanted)

        rows = {
            row.name: row.book_count
            for row in self.get("author_list").context["authors"]
        }

        self.assertEqual(rows, {"Wanted": 1, "Other": 0})


class TheLinkOnTheNameTests(LookupListTestCase):

    def test_each_name_links_to_the_book_list_filtered_to_it(self):
        expected = {
            "author": "mode=author&author=%s"
                      "&sort=title&direction=asc&page_size=25",
            "category": "mode=category&category=%s"
                        "&sort=title&direction=asc&page_size=25",
            "publisher": "mode=all&publisher=%s&availability="
                         "&sort=title&direction=asc&page_size=25",
        }

        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                row = self.make(kind, "Linked")

                response = self.get(url_name)

                url = "%s?%s" % (
                    reverse("book_list"), expected[kind] % row.id
                )

                self.assertEqual(response.context[key][0].books_url, url)

                # And it is actually on the page. `&` is escaped in HTML,
                # so the rendered form is what has to be looked for.
                self.assertContains(response, 'href="%s"' % escape(url))

    def test_the_whole_row_goes_there_too(self):
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.make(kind, "Clickable")

                response = self.get(url_name)
                url = response.context[key][0].books_url

                self.assertContains(
                    response, 'data-row-url="%s"' % escape(url)
                )

    def test_following_the_link_shows_that_row_s_books_and_no_others(self):
        # The link being the right shape is not the same as it working.
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                mine = self.make(kind, "Mine")
                theirs = self.make(kind, "Theirs")

                self.book_under(kind, mine, "Kept")
                self.book_under(kind, theirs, "Excluded")

                url = self.get(url_name).context[key][0].books_url

                books = self.client.get(url)

                self.assertEqual(books.status_code, 200)
                self.assertEqual(
                    [book.title for book in books.context["books"]], ["Kept"]
                )

    def test_the_link_lands_on_a_mode_whose_control_shows_the_filter(self):
        # Author and Category have a browsing control on the book list;
        # Publisher does not, so its link browses everything and relies on
        # the filter chips to say what is in force.
        author = make_author(name="Shown")
        category = make_category(name="Shown")
        publisher = make_publisher(name="Shown", city="Nowhere")

        for kind, obj, mode in (
            ("author", author, "author"),
            ("category", category, "category"),
            ("publisher", publisher, "all"),
        ):
            with self.subTest(kind=kind):

                url = "%s?%s" % (
                    reverse("book_list"),
                    {
                        "author": "mode=author&author=",
                        "category": "mode=category&category=",
                        "publisher": "mode=all&publisher=",
                    }[kind] + str(obj.id),
                )

                response = self.client.get(url)

                self.assertEqual(response.context["mode"], mode)
                self.assertContains(response, "Shown")


class PagingTests(LookupListTestCase):

    def rows(self, count, kind="author"):
        for i in range(count):
            self.make(kind, "Row %03d" % i)

    def test_a_page_holds_the_default_number_of_rows(self):
        self.rows(PAGE_SIZE + 3)

        response = self.get("author_list")

        self.assertEqual(len(response.context["authors"]), PAGE_SIZE)

    def test_the_rest_is_on_the_next_page(self):
        self.rows(PAGE_SIZE + 3)

        response = self.get("author_list", page="2")

        self.assertEqual(len(response.context["authors"]), 3)

    def test_a_bigger_page_size_is_honoured(self):
        self.rows(60)

        response = self.get("author_list", page_size="50")

        self.assertEqual(response.context["page_size"], 50)
        self.assertEqual(len(response.context["authors"]), 50)

    def test_a_page_size_that_is_not_offered_falls_back(self):
        response = self.get("author_list", page_size="7")

        self.assertEqual(response.context["page_size"], PAGE_SIZE)

    def test_a_page_size_that_is_not_a_number_falls_back(self):
        response = self.get("author_list", page_size="lots")

        self.assertEqual(response.context["page_size"], PAGE_SIZE)

    def test_an_out_of_range_page_does_not_fail(self):
        for _kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.assertEqual(
                    self.get(url_name, page="9999").status_code, 200
                )

    def test_a_page_that_is_not_a_number_does_not_fail(self):
        for _kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.assertEqual(
                    self.get(url_name, page="abc").status_code, 200
                )

    def test_a_paging_link_keeps_the_search_and_the_sorting(self):
        self.rows(PAGE_SIZE + 3)

        query = self.get(
            "author_list",
            search="Row",
            sort="name",
            direction="desc",
            page_size="25",
        ).context["pagination_query"]

        self.assertIn("search=Row", query)
        self.assertIn("sort=name", query)
        self.assertIn("direction=desc", query)
        self.assertIn("page_size=25", query)
        self.assertNotIn("page=", query)

    def test_a_paging_link_states_the_sorting_even_when_the_url_did_not(self):
        # It applies either way, and a link that says so is still right
        # when somebody copies it out of the address bar.
        query = self.get("author_list").context["pagination_query"]

        self.assertIn("sort=name", query)
        self.assertIn("direction=asc", query)

    def test_the_rows_per_page_url_keeps_the_search_and_drops_the_page(self):
        url = self.get(
            "author_list", search="Dar", page="3", page_size="50"
        ).context["page_size_url"]

        self.assertIn("search=Dar", url)
        self.assertNotIn("page=", url)
        self.assertNotIn("page_size=", url)

    def test_the_rows_per_page_url_is_never_a_bare_question_mark(self):
        # htmx appends to it, and appending to nothing gives "?&page_size=".
        for _kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                url = self.get(url_name).context["page_size_url"]

                self.assertNotEqual(url, "?")
                self.assertTrue(url.startswith("?sort="))

    def test_the_total_is_the_whole_list_not_the_page(self):
        self.rows(PAGE_SIZE + 3)

        response = self.get("author_list")

        self.assertEqual(
            response.context["authors"].paginator.count, PAGE_SIZE + 3
        )

    def test_paging_controls_are_absent_on_a_single_page(self):
        self.rows(3)

        self.assertNotContains(self.get("author_list"), "Author list pages")

    def test_and_present_on_more_than_one(self):
        self.rows(PAGE_SIZE + 3)

        self.assertContains(self.get("author_list"), "Author list pages")


class NavigatingWithoutAReloadTests(LookupListTestCase):
    """These pages swap the main-content region rather than reloading.

    Not by carrying HTMX attributes of their own - the search form, the
    sort links and the paging links are ordinary GET markup that the
    shell's `hx-boost` picks up. So what is worth testing is the server
    half: that a request aimed at the main-content region comes back as
    that region and nothing else, and that it still honours the table's
    parameters when it does.

    The rows-per-page <select> is the exception, because a select is not a
    link. It names the region itself, which produces exactly the request
    below.
    """

    NAV = {"HX-Request": "true", "HX-Target": "mainContent"}

    def test_the_region_comes_back_on_its_own(self):
        for _kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                response = self.client.get(reverse(url_name), headers=self.NAV)
                body = response.content.decode()

                self.assertEqual(response.status_code, 200)
                self.assertNotIn("<!DOCTYPE html>", body)
                self.assertNotIn("sidebar-nav", body)

    def test_the_rows_per_page_request_is_answered_the_same_way(self):
        # What the <select> sends: the region as its target, the size as
        # its own value, and the table's other state in the URL already.
        for kind, url_name, key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                for i in range(12):
                    self.make(kind, "%s %02d" % (kind, i))

                response = self.client.get(
                    reverse(url_name),
                    {"sort": "name", "direction": "asc", "page_size": "10"},
                    headers=self.NAV,
                )

                self.assertEqual(response.context["page_size"], 10)
                self.assertEqual(len(response.context[key]), 10)
                self.assertNotIn(
                    "<!DOCTYPE html>", response.content.decode()
                )

    def test_a_search_still_works_without_the_headers(self):
        # The same URL, requested the ordinary way, is a whole page. Direct
        # links, refresh and a browser with no script all land here.
        self.make("author", "Reachable")

        body = self.client.get(
            reverse("author_list"), {"search": "Reach"}
        ).content.decode()

        self.assertIn("<!DOCTYPE html>", body)
        self.assertIn("Reachable", body)

    def test_the_sort_links_carry_no_htmx_of_their_own(self):
        # They must not: an hx-get here would be answered with a fragment
        # and swapped into the link itself. Boosting is the shell's job.
        self.make("author", "Sortable")

        body = self.client.get(reverse("author_list")).content.decode()

        table = body[body.index("<thead"):body.index("</thead>")]

        self.assertNotIn("hx-get", table)


class TheDialogsTests(LookupListTestCase):
    """Add, Edit and Delete are dialogs, and there are no pages behind them.

    What is worth testing is the seam. The dialog asks with `?modal=1` and
    the HX-Request header and gets a fragment; a request without them has
    no page to be given and goes to the list instead. And a successful
    write answers with no content and an event rather than a redirect,
    because the dialog has to close and the list behind it has to redraw -
    a redirect would replace the whole page and lose the point of the
    dialog.
    """

    MODAL = {"HX-Request": "true"}

    def open(self, url_name, *args):
        return self.client.get(
            reverse(url_name, args=args), {"modal": "1"}, headers=self.MODAL
        )

    def submit(self, url_name, *args, **data):
        return self.client.post(
            reverse(url_name, args=args) + "?modal=1",
            data,
            headers=self.MODAL,
        )

    # ------------------------------------------------------------- Add

    def test_the_add_dialog_is_a_fragment(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                response = self.open("%s_add" % kind)

                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(
                    response, "library/partials/lookup_form_modal.html"
                )
                self.assertNotContains(response, "<!DOCTYPE html>")
                self.assertContains(response, "Add %s" % LOOKUPS_LABEL[kind])

    def test_adding_answers_with_an_event_and_no_content(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                response = self.submit("%s_add" % kind, name="New %s" % kind)

                self.assertEqual(response.status_code, 204)
                self.assertIn("recordSaved", response["HX-Trigger"])
                self.assertIn("New %s" % kind, response["HX-Trigger"])

    def test_and_the_record_exists(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                self.submit("%s_add" % kind, name="Made %s" % kind)

                self.assertTrue(
                    MODELS[kind].objects.filter(
                        name="Made %s" % kind
                    ).exists()
                )

    def test_a_blank_name_keeps_the_dialog_open_with_the_reason(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                response = self.submit("%s_add" % kind, name="  ")

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "name is required")
                self.assertFalse(MODELS[kind].objects.exists())

    def test_a_name_already_taken_is_refused_in_the_dialog(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                self.make(kind, "Taken")

                response = self.submit("%s_add" % kind, name="taken")

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "already exists")
                self.assertEqual(MODELS[kind].objects.count(), 1)

    def test_only_the_publisher_dialog_asks_for_a_city(self):
        self.assertContains(self.open("publisher_add"), 'name="city"')
        self.assertNotContains(self.open("author_add"), 'name="city"')
        self.assertNotContains(self.open("category_add"), 'name="city"')

    def test_a_publisher_keeps_the_city_it_was_given(self):
        self.submit("publisher_add", name="Dar al-Fikr", city="Damascus")

        self.assertEqual(
            Publisher.objects.get(name="Dar al-Fikr").city, "Damascus"
        )

    def test_a_blank_city_is_stored_as_nothing_rather_than_empty(self):
        self.submit("publisher_add", name="No City", city="  ")

        self.assertIsNone(Publisher.objects.get(name="No City").city)

    # ------------------------------------------------------------- Edit

    def test_the_edit_dialog_arrives_filled_in(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                row = self.make(kind, "Existing %s" % kind)

                response = self.open("%s_edit" % kind, row.id)

                self.assertTemplateUsed(
                    response, "library/partials/lookup_form_modal.html"
                )
                self.assertContains(response, 'value="Existing %s"' % kind)
                self.assertContains(response, "Edit %s" % LOOKUPS_LABEL[kind])

    def test_a_rename_answers_with_an_event(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                row = self.make(kind, "Before %s" % kind)

                response = self.submit(
                    "%s_edit" % kind, row.id, name="After %s" % kind
                )

                self.assertEqual(response.status_code, 204)
                self.assertIn("recordSaved", response["HX-Trigger"])

                row.refresh_from_db()
                self.assertEqual(row.name, "After %s" % kind)

    def test_renaming_onto_another_name_is_refused_rather_than_a_500(self):
        # The name column is unique, so this used to reach the database
        # and come back as a server error.
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                first = self.make(kind, "First %s" % kind)
                second = self.make(kind, "Second %s" % kind)

                response = self.submit(
                    "%s_edit" % kind, second.id, name="first %s" % kind
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "already exists")

                second.refresh_from_db()
                self.assertEqual(second.name, "Second %s" % kind)
                self.assertEqual(first.name, "First %s" % kind)

    def test_saving_a_name_unchanged_is_not_a_duplicate(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                row = self.make(kind, "Same %s" % kind)

                response = self.submit(
                    "%s_edit" % kind, row.id, name="Same %s" % kind
                )

                self.assertEqual(response.status_code, 204)

    def test_a_publishers_city_can_be_changed_and_cleared(self):
        publisher = make_publisher(name="Movable", city="Lahore")

        self.submit("publisher_edit", publisher.id,
                    name="Movable", city="Karachi")
        publisher.refresh_from_db()
        self.assertEqual(publisher.city, "Karachi")

        self.submit("publisher_edit", publisher.id, name="Movable", city="")
        publisher.refresh_from_db()
        self.assertIsNone(publisher.city)

    # ------------------------------------------------------------ Delete

    def test_the_delete_dialog_names_what_it_will_delete(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                row = self.make(kind, "Doomed %s" % kind)

                response = self.open("%s_delete" % kind, row.id)

                self.assertTemplateUsed(
                    response, "library/partials/lookup_delete_modal.html"
                )
                self.assertContains(response, "Doomed %s" % kind)
                self.assertContains(response, "cannot be undone")

    def test_deleting_answers_with_an_event(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                row = self.make(kind, "Going %s" % kind)

                response = self.submit("%s_delete" % kind, row.id)

                self.assertEqual(response.status_code, 204)
                self.assertIn("recordDeleted", response["HX-Trigger"])
                self.assertFalse(
                    MODELS[kind].objects.filter(id=row.id).exists()
                )

    def test_the_dialog_refuses_one_that_books_are_filed_under(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                row = self.make(kind, "Busy %s" % kind)
                self.book_under(kind, row, "Filed under %s" % kind)

                response = self.open("%s_delete" % kind, row.id)

                self.assertContains(response, "cannot be deleted")
                self.assertNotContains(response, "cannot be undone")

    def test_and_refuses_the_post_as_well(self):
        # Hiding the button is not the rule; the view is.
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                row = self.make(kind, "Guarded %s" % kind)
                self.book_under(kind, row, "Holding %s" % kind)

                response = self.submit("%s_delete" % kind, row.id)

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "cannot be deleted")
                self.assertTrue(
                    MODELS[kind].objects.filter(id=row.id).exists()
                )

    def test_an_archived_book_still_holds_its_lookups(self):
        # The foreign key is still there, and the book comes back if it is
        # restored.
        author = make_author(name="Archived Only")
        book = make_book(title="Put Away", author=author)
        Book.objects.filter(id=book.id).update(archived_at=timezone.now())

        response = self.submit("author_delete", author.id)

        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Author.objects.filter(id=author.id).exists())

    # ------------------------------------------------- no pages behind them

    def test_asking_for_any_of_them_as_a_page_lands_on_the_list(self):
        for kind, url_name, _key, _label in LOOKUPS:

            row = self.make(kind, "Listed %s" % kind)

            for route, args in (
                ("%s_add" % kind, ()),
                ("%s_edit" % kind, (row.id,)),
                ("%s_delete" % kind, (row.id,)),
            ):
                with self.subTest(route=route):

                    response = self.client.get(reverse(route, args=args))

                    self.assertRedirects(response, reverse(url_name))

    def test_the_detail_routes_are_gone(self):
        for kind, _url, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                with self.assertRaises(NoReverseMatch):
                    reverse("%s_detail" % kind, args=[1])

    def test_a_plain_post_still_writes_and_returns_to_the_list(self):
        # No dialog, so no event to send: the endpoint behaves like any
        # other form post.
        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                response = self.client.post(
                    reverse("%s_add" % kind), {"name": "Plain %s" % kind}
                )

                self.assertRedirects(response, reverse(url_name))
                self.assertTrue(
                    MODELS[kind].objects.filter(
                        name="Plain %s" % kind
                    ).exists()
                )

    def test_a_plain_post_that_fails_says_why_on_the_list(self):
        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                response = self.client.post(
                    reverse("%s_add" % kind), {"name": ""}, follow=True
                )

                self.assertContains(response, "name is required")


class TheListOpensThemTests(LookupListTestCase):
    """The list's three controls point at the dialogs, not at pages."""

    def test_add_edit_and_delete_all_open_the_shared_dialog(self):
        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                row = self.make(kind, "Openable")

                body = self.get(url_name).content.decode()

                for route, args in (
                    ("%s_add" % kind, ()),
                    ("%s_edit" % kind, (row.id,)),
                    ("%s_delete" % kind, (row.id,)),
                ):
                    url = reverse(route, args=args)

                    self.assertIn('hx-get="%s?modal=1"' % url, body)

                self.assertIn('hx-target="#formModal .modal-body"', body)

    def test_every_control_is_still_a_real_link(self):
        # The dialog is an interception, so the href has to say where it
        # would otherwise go.
        row = self.make("author", "Linked")

        body = self.get("author_list").content.decode()

        self.assertIn('href="%s"' % reverse("author_add"), body)
        self.assertIn(
            'href="%s"' % reverse("author_edit", args=[row.id]), body
        )

    def test_there_is_no_view_action_any_more(self):
        # The name is the way in. A second control to the same place was
        # the only thing the detail page offered.
        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                self.make(kind, "Once Viewable")

                response = self.get(url_name)

                self.assertNotContains(response, 'title="Open"')
                self.assertNotContains(response, "bi-box-arrow-up-right")

    def test_the_list_redraws_itself_when_a_dialog_reports_a_change(self):
        for _kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                response = self.get(url_name)

                self.assertContains(response, 'id="lookupListRefresh"')
                self.assertContains(response, "listChanged from:body")

    def test_an_assistant_gets_no_dialogs_and_no_actions_column(self):
        self.client.logout()
        make_user(username="u_view", password="pass12345", role="Assistant")
        self.client.login(username="u_view", password="pass12345")

        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(kind=kind):

                self.make(kind, "Read Only")

                response = self.get(url_name)

                self.assertNotContains(response, "data-form-modal")
                self.assertNotContains(response, ">Actions<")


class QueryCountTests(LookupListTestCase):

    def test_a_page_of_rows_costs_the_same_however_many_there_are(self):
        # The book count is annotated on the list's own query. Worked out
        # per row it would be a query each, which is invisible on a test
        # fixture and ruinous on a real catalogue.
        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                one = self.make(kind, "First")
                self.book_under(kind, one, "%s one" % kind)

                with CaptureQueriesContext(connection) as few:
                    self.get(url_name)

                for i in range(14):
                    row = self.make(kind, "Row %02d" % i)
                    self.book_under(kind, row, "%s %02d" % (kind, i))

                with CaptureQueriesContext(connection) as many:
                    self.get(url_name)

                self.assertEqual(len(many), len(few))


class PermissionTests(LookupListTestCase):

    def as_role(self, role):
        self.client.logout()
        make_user(username="u_%s" % role, password="pass12345", role=role)
        self.client.login(username="u_%s" % role, password="pass12345")

    def test_an_assistant_may_read_the_lists(self):
        self.as_role("Assistant")

        for _kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.assertEqual(self.get(url_name).status_code, 200)

    def test_an_assistant_is_offered_no_way_to_change_them(self):
        # UI gating only - `role_required` on the add, edit and delete
        # views is what actually refuses an Assistant.
        self.as_role("Assistant")

        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                row = self.make(kind, "Read Only")
                response = self.get(url_name)

                self.assertFalse(response.context["can_edit"])
                self.assertNotContains(response, reverse("%s_add" % kind))
                self.assertNotContains(
                    response, reverse("%s_edit" % kind, args=[row.id])
                )
                self.assertNotContains(
                    response, reverse("%s_delete" % kind, args=[row.id])
                )

    def test_a_librarian_is(self):
        self.as_role("Librarian")

        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                row = self.make(kind, "Editable")

                response = self.get(url_name)

                self.assertTrue(response.context["can_edit"])
                self.assertContains(
                    response, reverse("%s_edit" % kind, args=[row.id])
                )
                self.assertContains(
                    response, reverse("%s_delete" % kind, args=[row.id])
                )

    def test_signing_in_is_required(self):
        self.client.logout()

        for _kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.assertEqual(self.get(url_name).status_code, 302)


class TheComboboxStillWorksTests(LookupListTestCase):
    """These views also feed the searchable dropdowns on Add/Edit Book."""

    def ask(self, url_name, **params):
        params["combobox"] = "1"

        return self.client.get(
            reverse(url_name), params, headers={"HX-Request": "true"}
        )

    def test_it_answers_with_options_rather_than_a_table(self):
        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                self.make(kind, "Suggested")

                response = self.ask(url_name)

                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(
                    response, "library/partials/combobox_options.html"
                )
                self.assertContains(response, "Suggested")

    def test_it_searches(self):
        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                # Not "Unwanted": the search is case-insensitive and
                # matches anywhere, so that would match too.
                self.make(kind, "Wanted")
                self.make(kind, "Ignored Other")

                response = self.ask(url_name, search="Wanted")

                self.assertContains(response, "Wanted")
                self.assertEqual(len(response.context["items"]), 1)

    def test_the_table_s_own_parameters_do_not_reach_it(self):
        # A dropdown asks for suggestions, not for page two of them.
        for kind, url_name, _key, _label in LOOKUPS:
            with self.subTest(page=url_name):

                for i in range(3):
                    self.make(kind, "Row %d" % i)

                response = self.ask(url_name, page="2", page_size="10")

                self.assertEqual(response.context["total_count"], 3)
