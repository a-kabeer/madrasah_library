"""Refusing a second copy of the same book, without refusing similar ones.

The rule is deliberately narrow: same normalised title AND same author. The
`books` table has no ISBN, so a title and its author are the only identity
there is to compare, and anything looser would start refusing translations,
commentaries and other authors' works of the same name.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from library.models import Book
from library.views import find_duplicate_books, normalize_title
from library.tests.helpers import make_author, make_book, make_user


class NormalizeTitleTests(TestCase):

    def test_it_folds_the_accidents_of_typing(self):
        for raw in (
            "The Study Quran",
            "  The Study Quran  ",
            "The  Study   Quran",
            "THE STUDY QURAN",
            "\tThe Study\nQuran ",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_title(raw), "the study quran")

    def test_it_leaves_everything_else_alone(self):
        # Punctuation and diacritics tell real titles apart.
        self.assertNotEqual(
            normalize_title("Al-Muwatta"), normalize_title("Al Muwatta")
        )
        self.assertNotEqual(
            normalize_title("صحیح البخاری"), normalize_title("صحيح البخاري")
        )

    def test_it_survives_an_empty_value(self):
        self.assertEqual(normalize_title(""), "")
        self.assertEqual(normalize_title(None), "")


class FindDuplicateBooksTests(TestCase):

    def setUp(self):
        self.author = make_author("Imam Malik")
        self.other = make_author("Imam Shafi'i")
        self.book = make_book(title="Al-Muwatta", author=self.author)

    def test_the_same_title_and_author_is_found(self):
        found = find_duplicate_books("Al-Muwatta", self.author.id)

        self.assertEqual([b.id for b in found], [self.book.id])

    def test_case_and_spacing_do_not_hide_it(self):
        for typed in ("al-muwatta", "  AL-MUWATTA  ", "Al-Muwatta ", "AL-muwatta"):
            with self.subTest(typed=typed):
                self.assertEqual(
                    [b.id for b in find_duplicate_books(typed, self.author.id)],
                    [self.book.id],
                )

    def test_extra_spacing_inside_the_title_does_not_hide_it(self):
        spaced = make_book(title="Kitab  al   Umm", author=self.other)

        self.assertEqual(
            [b.id for b in find_duplicate_books("Kitab al Umm", self.other.id)],
            [spaced.id],
        )

    def test_the_same_title_by_a_different_author_is_not_a_duplicate(self):
        self.assertFalse(find_duplicate_books("Al-Muwatta", self.other.id))

    def test_a_similar_but_different_title_is_not_a_duplicate(self):
        for typed in ("Al Muwatta", "Al-Muwatta: An Edition", "Muwatta"):
            with self.subTest(typed=typed):
                self.assertFalse(find_duplicate_books(typed, self.author.id))

    def test_excluding_a_book_stops_it_matching_itself(self):
        self.assertFalse(
            find_duplicate_books(
                "Al-Muwatta", self.author.id, exclude_id=self.book.id
            )
        )

    def test_nothing_is_matched_without_a_title_or_an_author(self):
        self.assertFalse(find_duplicate_books("", self.author.id))
        self.assertFalse(find_duplicate_books("   ", self.author.id))
        self.assertFalse(find_duplicate_books("Al-Muwatta", None))
        self.assertFalse(find_duplicate_books("Al-Muwatta", ""))

    def test_it_is_one_query_that_returns_only_matches(self):
        for number in range(30):
            make_book(title="Filler %d" % number, author=self.author)

        with CaptureQueriesContext(connection) as ctx:
            found = list(find_duplicate_books("Al-Muwatta", self.author.id))

        self.assertEqual(len(ctx), 1)
        self.assertEqual([b.id for b in found], [self.book.id])

        # Narrowed on the author in SQL, so the normalising never runs over
        # the whole catalogue.
        self.assertIn("author_id", ctx.captured_queries[0]["sql"])

    def test_another_authors_catalogue_does_not_grow_the_work(self):
        with CaptureQueriesContext(connection) as before:
            find_duplicate_books("Al-Muwatta", self.author.id).count()

        for number in range(50):
            make_book(title="Al-Muwatta", author=make_author("Author %d" % number))

        with CaptureQueriesContext(connection) as after:
            found = list(find_duplicate_books("Al-Muwatta", self.author.id))

        self.assertEqual(len(after), len(before))
        self.assertEqual([b.id for b in found], [self.book.id])


class BookAddDuplicateTests(TestCase):

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.author = make_author("Imam Malik")
        self.other = make_author("Imam Shafi'i")
        self.book = make_book(title="Al-Muwatta", author=self.author)

    def post(self, **overrides):
        data = {
            "title": "Al-Muwatta",
            "author": self.author.id,
            "category": "",
            "publisher": "",
            "volume_mode": "single",
        }
        data.update(overrides)

        # The dialog: there is no Add Book page, and a plain POST
        # redirects rather than re-rendering the refusal these tests read.
        return self.client.post(
            reverse("book_add") + "?modal=1",
            data,
            headers={"HX-Request": "true"},
        )

    def test_a_duplicate_is_refused_and_nothing_is_created(self):
        before = Book.objects.count()

        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Book.objects.count(), before)

    def test_the_refusal_says_why_and_names_the_existing_book(self):
        response = self.post()

        self.assertContains(response, "already in the catalogue")
        self.assertContains(
            response, reverse("book_detail", args=[self.book.id])
        )
        self.assertEqual(
            [b.id for b in response.context["duplicates"]], [self.book.id]
        )

    def test_case_and_spacing_do_not_get_a_duplicate_past_it(self):
        before = Book.objects.count()

        response = self.post(title="  al-MUWATTA  ")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Book.objects.count(), before)

    def test_the_same_title_under_another_author_is_allowed(self):
        before = Book.objects.count()

        response = self.post(author=self.other.id)

        self.assertIn(response.status_code, (204, 302))
        self.assertEqual(Book.objects.count(), before + 1)
        self.assertTrue(
            Book.objects.filter(title="Al-Muwatta", author=self.other).exists()
        )

    def test_a_different_title_by_the_same_author_is_allowed(self):
        before = Book.objects.count()

        response = self.post(title="Al-Muwatta: The Cairo Edition")

        self.assertIn(response.status_code, (204, 302))
        self.assertEqual(Book.objects.count(), before + 1)
        self.assertTrue(
            Book.objects.filter(
                title="Al-Muwatta: The Cairo Edition", author=self.author
            ).exists()
        )

    def test_the_missing_fields_message_still_comes_first(self):
        response = self.post(title="")

        self.assertContains(response, "Enter the book&#x27;s title.")
        self.assertFalse(response.context["duplicates"])

    def test_the_empty_form_carries_no_duplicates(self):
        response = self.client.get(
            reverse("book_add") + "?modal=1",
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["duplicates"]), [])


class BookEditDuplicateTests(TestCase):

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.author = make_author("Imam Malik")
        self.other = make_author("Imam Shafi'i")
        self.book = make_book(title="Al-Muwatta", author=self.author)
        self.sibling = make_book(title="Kitab al-Umm", author=self.author)

    def post(self, book=None, **overrides):
        book = book or self.book
        data = {
            "title": book.title,
            "author": book.author_id,
            "category": "",
            "publisher": "",
        }
        data.update(overrides)

        return self.client.post(
            reverse("book_edit", args=[book.id]) + "?modal=1",
            data,
            headers={"HX-Request": "true"},
        )

    def test_saving_a_book_unchanged_is_not_a_self_collision(self):
        response = self.post()

        self.assertIn(response.status_code, (204, 302))
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, "Al-Muwatta")

    def test_an_ordinary_edit_still_saves(self):
        response = self.post(title="Al-Muwatta (revised)")

        self.assertIn(response.status_code, (204, 302))
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, "Al-Muwatta (revised)")

    def test_only_the_case_changing_is_not_a_self_collision(self):
        response = self.post(title="AL-MUWATTA")

        self.assertIn(response.status_code, (204, 302))
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, "AL-MUWATTA")

    def test_taking_a_siblings_title_is_refused(self):
        response = self.post(title="Kitab al-Umm")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already in the catalogue")

        self.book.refresh_from_db()
        self.assertEqual(self.book.title, "Al-Muwatta")

    def test_the_refusal_names_the_book_in_the_way(self):
        response = self.post(title="kitab  al-umm ")

        self.assertEqual(
            [b.id for b in response.context["duplicates"]], [self.sibling.id]
        )

    def test_moving_to_an_author_who_has_that_title_is_refused(self):
        theirs = make_book(title="Al-Muwatta", author=self.other)

        response = self.post(author=self.other.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [b.id for b in response.context["duplicates"]], [theirs.id]
        )
        self.book.refresh_from_db()
        self.assertEqual(self.book.author_id, self.author.id)

    def test_moving_to_an_author_who_does_not_is_allowed(self):
        response = self.post(author=self.other.id)

        self.assertIn(response.status_code, (204, 302))
        self.book.refresh_from_db()
        self.assertEqual(self.book.author_id, self.other.id)

    def test_the_form_costs_one_extra_query_at_most(self):
        with CaptureQueriesContext(connection) as ctx:
            self.post(title="Something Else Entirely")

        checks = [
            q for q in ctx.captured_queries
            if "regexp_replace" in q["sql"]
        ]

        self.assertEqual(len(checks), 1)
