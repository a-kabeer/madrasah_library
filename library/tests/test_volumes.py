"""Adding and editing a volume, and the things the database would refuse.

`unique_book_volume` is a real UNIQUE constraint on (book_id, volume_number),
`volume_number` is an integer column, and `book_id` is a NOT NULL foreign key.
Each of those used to be reachable from the form as an unhandled 500. These
tests pin the friendly refusal in front of each one, and pin that nothing is
written when a form is refused.
"""

from django.test import TestCase
from django.urls import reverse

from library.models import BookVolume
from library.tests.helpers import (
    make_author,
    make_book,
    make_user,
    make_volume,
)


class VolumeAddTests(TestCase):

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.book = make_book(title="Sahih al-Bukhari")

    def post(self, **overrides):
        data = {
            "book": self.book.id,
            "volume_number": "1",
            "title": "Volume One",
        }
        data.update(overrides)
        return self.client.post(reverse("book_volume_add"), data)

    def test_a_valid_volume_is_created(self):
        response = self.post(volume_number="3", title="Third")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            BookVolume.objects.filter(book=self.book, volume_number=3).exists()
        )

    def test_a_valid_volume_returns_to_its_book(self):
        response = self.post(volume_number="4")

        self.assertRedirects(
            response,
            reverse("book_detail", args=[self.book.id]),
        )

    def test_a_repeated_volume_number_is_refused_not_a_500(self):
        make_volume(book=self.book, volume_number=2, title="Second")

        response = self.post(volume_number="2", title="Second Again")

        # The point of the whole change: a page, not a crash.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            BookVolume.objects.filter(book=self.book, volume_number=2).count(),
            1,
        )

    def test_the_refusal_names_the_book_and_the_number(self):
        make_volume(book=self.book, volume_number=2, title="Second")

        response = self.post(volume_number="2")

        self.assertContains(response, "already has a Volume 2")
        self.assertContains(response, "Sahih al-Bukhari")

    def test_the_refusal_names_the_volume_already_there(self):
        make_volume(book=self.book, volume_number=2, title="Kitab al-Iman")

        response = self.post(volume_number="2")

        self.assertContains(response, "Kitab al-Iman")

    def test_a_refusal_keeps_what_was_typed(self):
        make_volume(book=self.book, volume_number=2, title="Second")

        response = self.post(volume_number="2", title="My Typed Title")

        # Both the title and the book, so the form does not have to be
        # filled in a second time.
        self.assertContains(response, "My Typed Title")
        self.assertEqual(response.context["selected_book_id"], str(self.book.id))
        self.assertEqual(response.context["volume_number"], "2")

    def test_the_same_number_on_a_different_book_is_fine(self):
        other = make_book(
            title="Sahih Muslim",
            author=make_author("Imam Muslim"),
        )
        make_volume(book=other, volume_number=1, title="First")

        response = self.post(volume_number="1")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            BookVolume.objects.filter(book=self.book, volume_number=1).exists()
        )

    def test_a_volume_number_that_is_not_a_number_is_refused(self):
        response = self.post(volume_number="abc")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "whole number")
        self.assertFalse(BookVolume.objects.filter(book=self.book).exists())

    def test_a_decimal_volume_number_is_refused(self):
        response = self.post(volume_number="1.5")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "whole number")
        self.assertFalse(BookVolume.objects.filter(book=self.book).exists())

    def test_a_volume_number_below_one_is_refused(self):
        for number in ("0", "-3"):
            with self.subTest(number=number):
                response = self.post(volume_number=number)

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "1 or more")
                self.assertFalse(
                    BookVolume.objects.filter(book=self.book).exists()
                )

    def test_a_book_that_does_not_exist_is_refused(self):
        response = self.post(book=999999)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "choose a book")
        self.assertFalse(BookVolume.objects.exists())

    def test_a_book_that_is_not_a_number_is_refused(self):
        response = self.post(book="not-an-id")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(BookVolume.objects.exists())

    def test_a_non_numeric_book_in_the_url_does_not_break_the_page(self):
        # The back link resolves the book rather than trusting the id, so a
        # value that names no book falls back to the volume list instead of
        # raising NoReverseMatch.
        response = self.client.get(reverse("book_volume_add") + "?book=abc")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["selected_book"])
        self.assertContains(response, "Back to Volumes")

    def test_a_real_book_in_the_url_is_preselected_and_linked(self):
        response = self.client.get(
            reverse("book_volume_add") + "?book=%d" % self.book.id
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_book"], self.book)
        self.assertContains(response, "Back to Book")
        self.assertContains(
            response,
            reverse("book_detail", args=[self.book.id]),
        )

    def test_missing_fields_are_refused(self):
        for field in ("book", "volume_number", "title"):
            with self.subTest(field=field):
                response = self.post(**{field: ""})

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "fill in all required fields")
                self.assertFalse(BookVolume.objects.exists())

    def test_a_whitespace_only_title_is_refused(self):
        response = self.post(title="   ")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "fill in all required fields")
        self.assertFalse(BookVolume.objects.exists())


class VolumeEditTests(TestCase):

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.book = make_book(title="Sahih al-Bukhari")
        self.volume = make_volume(book=self.book, volume_number=1, title="First")
        self.sibling = make_volume(book=self.book, volume_number=2, title="Second")

    def post(self, **overrides):
        data = {
            "book": self.book.id,
            "volume_number": str(self.volume.volume_number),
            "title": self.volume.title,
        }
        data.update(overrides)
        return self.client.post(
            reverse("book_volume_edit", args=[self.volume.id]),
            data,
        )

    def test_a_valid_change_is_saved(self):
        response = self.post(title="Renamed")

        self.assertEqual(response.status_code, 302)
        self.volume.refresh_from_db()
        self.assertEqual(self.volume.title, "Renamed")

    def test_keeping_its_own_number_is_not_a_clash(self):
        # The volume must not be compared against itself.
        response = self.post(volume_number="1", title="Still First")

        self.assertEqual(response.status_code, 302)
        self.volume.refresh_from_db()
        self.assertEqual(self.volume.volume_number, 1)
        self.assertEqual(self.volume.title, "Still First")

    def test_taking_a_siblings_number_is_refused_not_a_500(self):
        response = self.post(volume_number="2")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already has a Volume 2")

        self.volume.refresh_from_db()
        self.sibling.refresh_from_db()
        self.assertEqual(self.volume.volume_number, 1)
        self.assertEqual(self.sibling.volume_number, 2)

    def test_a_refused_edit_writes_nothing(self):
        response = self.post(volume_number="2", title="Should Not Stick")

        self.assertEqual(response.status_code, 200)
        self.volume.refresh_from_db()
        self.assertEqual(self.volume.title, "First")

    def test_a_refused_edit_keeps_what_was_typed(self):
        response = self.post(volume_number="2", title="My Typed Title")

        self.assertContains(response, "My Typed Title")
        self.assertEqual(response.context["volume"].volume_number, "2")

    def test_moving_to_another_book_with_that_number_free_is_saved(self):
        other = make_book(
            title="Sahih Muslim",
            author=make_author("Imam Muslim"),
        )

        response = self.post(book=other.id, volume_number="1")

        self.assertEqual(response.status_code, 302)
        self.volume.refresh_from_db()
        self.assertEqual(self.volume.book_id, other.id)

    def test_moving_to_another_book_that_already_has_that_number_is_refused(self):
        other = make_book(
            title="Sahih Muslim",
            author=make_author("Imam Muslim"),
        )
        make_volume(book=other, volume_number=5, title="Theirs")

        response = self.post(book=other.id, volume_number="5")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sahih Muslim")
        self.volume.refresh_from_db()
        self.assertEqual(self.volume.book_id, self.book.id)
        self.assertEqual(self.volume.volume_number, 1)

    def test_a_volume_number_that_is_not_a_number_is_refused(self):
        response = self.post(volume_number="abc")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "whole number")
        self.volume.refresh_from_db()
        self.assertEqual(self.volume.volume_number, 1)

    def test_a_volume_number_below_one_is_refused(self):
        response = self.post(volume_number="0")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 or more")
        self.volume.refresh_from_db()
        self.assertEqual(self.volume.volume_number, 1)

    def test_a_blank_title_is_refused_and_says_so(self):
        # This used to re-render the form with nothing said at all, which
        # read as the Save button being broken.
        response = self.post(title="")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "fill in all required fields")
        self.volume.refresh_from_db()
        self.assertEqual(self.volume.title, "First")

    def test_a_book_that_does_not_exist_is_refused(self):
        response = self.post(book=999999)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "choose a book")
        self.volume.refresh_from_db()
        self.assertEqual(self.volume.book_id, self.book.id)

    def test_from_detail_returns_to_the_volume(self):
        response = self.post(title="Renamed", **{"from": "detail"})

        self.assertRedirects(
            response,
            reverse("book_volume_detail", args=[self.volume.id]),
        )

    def test_without_from_returns_to_the_list(self):
        response = self.post(title="Renamed")

        self.assertRedirects(response, reverse("book_volume_list"))


class VolumeFormPermissionTests(TestCase):

    def setUp(self):
        make_user(username="assistant_u", password="pass12345", role="Assistant")
        self.client.login(username="assistant_u", password="pass12345")
        self.book = make_book()
        self.volume = make_volume(book=self.book, volume_number=1)

    def test_assistant_cannot_reach_add(self):
        self.assertEqual(
            self.client.get(reverse("book_volume_add")).status_code, 403
        )

    def test_assistant_cannot_post_add(self):
        response = self.client.post(reverse("book_volume_add"), {
            "book": self.book.id,
            "volume_number": "9",
            "title": "Sneaked In",
        })

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            BookVolume.objects.filter(volume_number=9).exists()
        )

    def test_assistant_cannot_reach_edit(self):
        self.assertEqual(
            self.client.get(
                reverse("book_volume_edit", args=[self.volume.id])
            ).status_code,
            403,
        )

    def test_assistant_cannot_post_edit(self):
        response = self.client.post(
            reverse("book_volume_edit", args=[self.volume.id]),
            {"book": self.book.id, "volume_number": "1", "title": "Sneaked In"},
        )

        self.assertEqual(response.status_code, 403)
        self.volume.refresh_from_db()
        self.assertNotEqual(self.volume.title, "Sneaked In")
