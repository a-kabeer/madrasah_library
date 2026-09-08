"""The Add / Edit / Delete Book dialogs and the guided add workflow.

Covers what `book_add` now creates in one go (book, volumes, copies, codes,
shelf placement), that a rejected form leaves nothing behind, that editing
a book never touches its inventory, and that deleting one is blocked when
the database's foreign keys would refuse it.
"""

from datetime import date

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from library.models import Book, BookCopy, BookVolume, Location, Shelf

from .helpers import (
    make_author,
    make_book,
    make_category,
    make_copy,
    make_loan,
    make_location,
    make_publisher,
    make_shelf,
    make_user,
    make_volume,
)


class BookWorkflowTestCase(TestCase):

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        self.author = make_author(name="Imam Bukhari")
        self.category = make_category(name="Hadith")
        self.publisher = make_publisher(name="Dar Ibn Kathir")

        self.location = make_location(name="Main Hall")
        self.shelf = make_shelf(location=self.location, shelf_code="A-1")

        # A second location with its own shelf, so "only this location's
        # shelves" can actually be told apart from "all shelves".
        self.other_location = make_location(name="Annexe")
        self.other_shelf = make_shelf(
            location=self.other_location,
            shelf_code="B-1",
        )

    # ---------------------------------------------------------- helpers

    def base_payload(self, **overrides):
        payload = {
            "title": "Sahih al-Bukhari",
            "author": self.author.id,
            "category": self.category.id,
            "publisher": self.publisher.id,
            "volume_mode": "single",
            "copies_mode": "skip",
            "code_mode": "auto",
        }
        payload.update(overrides)
        return payload

    def post_add(self, modal=True, **overrides):
        url = reverse("book_add")
        payload = self.base_payload(**overrides)

        if not modal:
            return self.client.post(url, payload)

        return self.client.post(
            url + "?modal=1",
            payload,
            headers={"HX-Request": "true"},
        )

    def get_modal(self, name, *args):
        return self.client.get(
            reverse(name, args=args) + "?modal=1",
            headers={"HX-Request": "true"},
        )


class AddBookDialogTests(BookWorkflowTestCase):

    def test_dialog_returns_only_the_form(self):
        response = self.get_modal("book_add")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response, "library/partials/book_add_modal.html"
        )
        self.assertNotContains(response, "<!DOCTYPE html>")

    def test_half_a_request_is_not_a_dialog(self):
        """The header and the parameter are both required.

        Branching on the header alone would make one URL answer with two
        different bodies and no Vary, which a cache can mix up. Neither
        half on its own is the dialog - and since the page is gone, what
        each gets instead is the list.
        """

        for label, response in (
            (
                "header only",
                self.client.get(
                    reverse("book_add"), headers={"HX-Request": "true"}
                ),
            ),
            (
                "parameter only",
                self.client.get(reverse("book_add") + "?modal=1"),
            ),
        ):
            with self.subTest(request=label):
                self.assertTemplateNotUsed(
                    response, "library/partials/book_add_modal.html"
                )
                self.assertRedirects(response, reverse("book_list"))

    def test_the_dialog_carries_the_whole_form(self):
        """Both halves of it: the book's details and its inventory.

        The Add Book page was a shell around these same two partials, so
        deleting it took nothing with it - and this is what says so.
        """

        response = self.get_modal("book_add")

        self.assertTemplateUsed(
            response, "library/partials/book_form_fields.html"
        )
        self.assertTemplateUsed(
            response, "library/partials/book_inventory_fields.html"
        )

    def test_assistant_cannot_open_the_dialog(self):
        self.client.logout()
        make_user(username="assist", password="pass12345", role="Assistant")
        self.client.login(username="assist", password="pass12345")

        self.assertEqual(self.get_modal("book_add").status_code, 403)


class SingleVolumeTests(BookWorkflowTestCase):

    def test_single_volume_book_gets_one_hidden_volume(self):
        response = self.post_add()

        self.assertEqual(response.status_code, 204)

        book = Book.objects.get(title="Sahih al-Bukhari")
        volumes = BookVolume.objects.filter(book=book)

        self.assertEqual(volumes.count(), 1)
        self.assertEqual(volumes.first().volume_number, 1)

        # Left empty on purpose: copying the book's title would give it a
        # second place to go stale when the title is edited.
        self.assertEqual(volumes.first().title, "")

    def test_success_reports_the_book_so_the_page_can_react(self):
        response = self.post_add()

        book = Book.objects.get(title="Sahih al-Bukhari")

        self.assertIn("bookSaved", response["HX-Trigger"])
        self.assertIn(str(book.id), response["HX-Trigger"])

    def test_skipping_copies_creates_none(self):
        self.post_add()

        self.assertEqual(BookCopy.objects.count(), 0)

    def test_full_page_post_still_redirects(self):
        response = self.post_add(modal=False)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Book.objects.filter(title="Sahih al-Bukhari").exists())


class MultipleVolumeTests(BookWorkflowTestCase):

    def test_multiple_volumes_are_created_in_order(self):
        self.post_add(
            volume_mode="multiple",
            volume_number=["1", "2", "3"],
            volume_title=["Kitab al-Iman", "", "Kitab al-Salah"],
        )

        book = Book.objects.get(title="Sahih al-Bukhari")
        volumes = BookVolume.objects.filter(book=book).order_by("volume_number")

        self.assertEqual(
            [(v.volume_number, v.title) for v in volumes],
            [(1, "Kitab al-Iman"), (2, ""), (3, "Kitab al-Salah")],
        )

    def test_blank_rows_are_dropped_rather_than_rejected(self):
        # Removing a row is what an emptied row means.
        self.post_add(
            volume_mode="multiple",
            volume_number=["1", "", "2"],
            volume_title=["", "", ""],
        )

        book = Book.objects.get(title="Sahih al-Bukhari")

        self.assertEqual(BookVolume.objects.filter(book=book).count(), 2)

    def test_repeated_volume_number_is_rejected(self):
        response = self.post_add(
            volume_mode="multiple",
            volume_number=["1", "1"],
            volume_title=["", ""],
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "listed twice")
        self.assertFalse(Book.objects.filter(title="Sahih al-Bukhari").exists())

    def test_zero_volume_number_is_rejected(self):
        response = self.post_add(
            volume_mode="multiple",
            volume_number=["0"],
            volume_title=[""],
        )

        self.assertContains(response, "volume number of 1 or more")
        self.assertFalse(Book.objects.filter(title="Sahih al-Bukhari").exists())

    def test_multiple_with_no_rows_is_rejected(self):
        response = self.post_add(volume_mode="multiple")

        self.assertContains(response, "at least one volume")
        self.assertFalse(Book.objects.filter(title="Sahih al-Bukhari").exists())


class CopyQuantityTests(BookWorkflowTestCase):

    def test_one_copy(self):
        self.post_add(
            copies_mode="add",
            copy_qty=["1"],
            location=self.location.id,
            shelf=self.shelf.id,
        )

        self.assertEqual(BookCopy.objects.count(), 1)

    def test_several_copies_of_a_single_volume(self):
        self.post_add(
            copies_mode="add",
            copy_qty=["5"],
            location=self.location.id,
            shelf=self.shelf.id,
        )

        copies = BookCopy.objects.all()

        self.assertEqual(copies.count(), 5)
        self.assertEqual(
            {copy.shelf_id for copy in copies},
            {self.shelf.id},
        )
        self.assertEqual(
            {copy.status for copy in copies},
            {BookCopy.STATUS_AVAILABLE},
        )

    def test_a_different_quantity_for_each_volume(self):
        self.post_add(
            volume_mode="multiple",
            volume_number=["1", "2", "3"],
            volume_title=["", "", ""],
            copies_mode="add",
            copy_qty=["5", "3", "2"],
            location=self.location.id,
            shelf=self.shelf.id,
        )

        book = Book.objects.get(title="Sahih al-Bukhari")

        counts = {
            volume.volume_number: BookCopy.objects.filter(
                volume=volume
            ).count()
            for volume in BookVolume.objects.filter(book=book)
        }

        self.assertEqual(counts, {1: 5, 2: 3, 3: 2})

    def test_a_volume_can_be_given_no_copies(self):
        self.post_add(
            volume_mode="multiple",
            volume_number=["1", "2"],
            volume_title=["", ""],
            copies_mode="add",
            copy_qty=["2", "0"],
            location=self.location.id,
            shelf=self.shelf.id,
        )

        self.assertEqual(BookCopy.objects.count(), 2)

    def test_asking_for_copies_but_none_at_all_is_rejected(self):
        response = self.post_add(
            copies_mode="add",
            copy_qty=["0"],
            location=self.location.id,
            shelf=self.shelf.id,
        )

        self.assertContains(response, "how many copies")
        self.assertFalse(Book.objects.filter(title="Sahih al-Bukhari").exists())

    def test_a_silly_quantity_is_rejected(self):
        response = self.post_add(
            copies_mode="add",
            copy_qty=["100000"],
            location=self.location.id,
            shelf=self.shelf.id,
        )

        self.assertContains(response, "At most")
        self.assertEqual(BookCopy.objects.count(), 0)

    def test_non_numeric_quantity_is_rejected(self):
        response = self.post_add(
            copies_mode="add",
            copy_qty=["five"],
            location=self.location.id,
            shelf=self.shelf.id,
        )

        self.assertContains(response, "whole number")

    def test_copies_cost_a_fixed_number_of_queries_each(self):
        # Two statements per copy: the insert, then the code, which needs
        # the id the insert allocated. What matters is that it stays linear
        # rather than growing per copy.
        def queries_for(quantity, title):
            with CaptureQueriesContext(connection) as captured:
                self.post_add(
                    title=title,
                    copies_mode="add",
                    copy_qty=[str(quantity)],
                    location=self.location.id,
                    shelf=self.shelf.id,
                )

            return len(captured)

        one = queries_for(1, "Counted One")
        five = queries_for(5, "Counted Five")

        self.assertEqual(BookCopy.objects.count(), 6)

        # Four extra copies, at two statements plus one code-uniqueness
        # check each. A per-copy re-read of anything else would blow this.
        self.assertLessEqual(five - one, 4 * 3)


class CopyCodeTests(BookWorkflowTestCase):

    def add_copies(self, quantity=3, **overrides):
        return self.post_add(
            copies_mode="add",
            copy_qty=[str(quantity)],
            location=self.location.id,
            shelf=self.shelf.id,
            **overrides
        )

    def test_generated_codes_are_unique_and_carry_the_house_prefix(self):
        self.add_copies(5)

        codes = list(BookCopy.objects.values_list("copy_code", flat=True))

        self.assertEqual(len(codes), 5)
        self.assertEqual(len(set(codes)), 5)

        for code in codes:
            self.assertTrue(code.startswith("LIB-"), code)
            self.assertTrue(code[4:].isdigit(), code)

    def test_a_generated_code_matches_its_own_copy(self):
        self.add_copies(3)

        for copy in BookCopy.objects.all():
            self.assertEqual(copy.copy_code, "LIB-%06d" % copy.id)

    def test_a_deleted_code_is_not_handed_to_the_next_copy(self):
        self.add_copies(2)

        retired = BookCopy.objects.order_by("-id").first()
        retired_code = retired.copy_code
        retired.delete()

        self.post_add(
            title="Another Book",
            copies_mode="add",
            copy_qty=["1"],
            location=self.location.id,
            shelf=self.shelf.id,
        )

        fresh = BookCopy.objects.order_by("-id").first()

        self.assertNotEqual(fresh.copy_code, retired_code)

    def test_a_generated_code_survives_retitling_the_book(self):
        self.add_copies(1)

        copy = BookCopy.objects.get()
        before = copy.copy_code
        book = Book.objects.get(title="Sahih al-Bukhari")

        self.client.post(
            reverse("book_edit", args=[book.id]),
            {"title": "Completely Different", "author": self.author.id},
        )

        copy.refresh_from_db()

        self.assertEqual(copy.copy_code, before)

    def test_a_generated_code_steps_aside_from_a_hand_typed_one(self):
        # Someone may already have written this exact code on a book.
        volume = make_volume(book=make_book(title="Older", author=self.author))
        next_id = BookCopy.objects.count() + 1
        make_copy(
            volume=volume,
            shelf=self.shelf,
            copy_code="LIB-%06d" % (next_id + 1),
        )

        self.add_copies(2)

        codes = list(BookCopy.objects.values_list("copy_code", flat=True))

        self.assertEqual(len(codes), len(set(codes)))

    def test_manual_codes_are_used_as_typed(self):
        self.add_copies(
            2,
            code_mode="manual",
            copy_code=["ACC-11", "ACC-12"],
        )

        self.assertEqual(
            sorted(BookCopy.objects.values_list("copy_code", flat=True)),
            ["ACC-11", "ACC-12"],
        )

    def test_manual_codes_are_matched_to_volumes_in_order(self):
        self.post_add(
            volume_mode="multiple",
            volume_number=["1", "2"],
            volume_title=["", ""],
            copies_mode="add",
            copy_qty=["1", "2"],
            code_mode="manual",
            copy_code=["V1-A", "V2-A", "V2-B"],
            location=self.location.id,
            shelf=self.shelf.id,
        )

        book = Book.objects.get(title="Sahih al-Bukhari")

        by_volume = {
            volume.volume_number: sorted(
                BookCopy.objects.filter(volume=volume)
                .values_list("copy_code", flat=True)
            )
            for volume in BookVolume.objects.filter(book=book)
        }

        self.assertEqual(by_volume, {1: ["V1-A"], 2: ["V2-A", "V2-B"]})

    def test_too_few_manual_codes_is_rejected(self):
        response = self.add_copies(
            3,
            code_mode="manual",
            copy_code=["ONE", "TWO"],
        )

        self.assertContains(response, "copy code for each")
        self.assertEqual(BookCopy.objects.count(), 0)

    def test_repeated_manual_codes_are_rejected(self):
        response = self.add_copies(
            2,
            code_mode="manual",
            copy_code=["SAME", "SAME"],
        )

        self.assertContains(response, "not all different")
        self.assertEqual(BookCopy.objects.count(), 0)

    def test_a_manual_code_already_in_use_is_rejected(self):
        make_copy(shelf=self.shelf, copy_code="TAKEN-1")

        response = self.add_copies(
            1,
            code_mode="manual",
            copy_code=["TAKEN-1"],
        )

        self.assertContains(response, "already in use")
        self.assertEqual(BookCopy.objects.count(), 1)

    def test_a_manual_code_clashing_only_in_case_is_rejected(self):
        make_copy(shelf=self.shelf, copy_code="Taken-2")

        response = self.add_copies(
            1,
            code_mode="manual",
            copy_code=["TAKEN-2"],
        )

        self.assertContains(response, "already in use")


class LocationAndShelfTests(BookWorkflowTestCase):

    def test_copies_need_somewhere_to_go(self):
        response = self.post_add(copies_mode="add", copy_qty=["2"])

        self.assertContains(response, "location and a shelf")
        self.assertEqual(BookCopy.objects.count(), 0)

    def test_a_shelf_from_another_location_is_rejected(self):
        # The dropdowns only offer matching pairs, but that is the browser's
        # word for it.
        response = self.post_add(
            copies_mode="add",
            copy_qty=["1"],
            location=self.location.id,
            shelf=self.other_shelf.id,
        )

        self.assertContains(response, "not in the location you chose")
        self.assertEqual(BookCopy.objects.count(), 0)

    def test_shelf_options_are_limited_to_one_location(self):
        response = self.client.get(
            reverse("shelf_list"),
            {"options": "1", "location": self.location.id},
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A-1")
        self.assertNotContains(response, "B-1")

    def test_shelf_options_ask_for_a_location_first(self):
        response = self.client.get(
            reverse("shelf_list"),
            {"options": "1"},
            headers={"HX-Request": "true"},
        )

        self.assertContains(response, "Choose a location first")
        self.assertNotContains(response, "A-1")

    def test_shelf_list_page_is_unaffected(self):
        response = self.client.get(reverse("shelf_list"))

        self.assertTemplateUsed(response, "library/shelf_list.html")

    def test_quick_add_location_returns_options_with_it_selected(self):
        response = self.client.post(
            reverse("location_add"),
            {"options": "1", "name": "Reading Room"},
            headers={"HX-Request": "true"},
        )

        created = Location.objects.get(name="Reading Room")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reading Room")
        self.assertContains(response, 'value="%d"\n        selected' % created.id)
        self.assertIn("quickAddDone", response["HX-Trigger"])
        self.assertIn('"entity": "location"', response["HX-Trigger"])

    def test_quick_add_location_reuses_an_existing_name(self):
        response = self.client.post(
            reverse("location_add"),
            {"options": "1", "name": "main hall"},
            headers={"HX-Request": "true"},
        )

        # `locations.name` is UNIQUE, so this used to be a 500.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Location.objects.filter(name="main hall").count(), 0)
        self.assertIn(str(self.location.id), response["HX-Trigger"])

    def test_quick_add_location_needs_a_name(self):
        response = self.client.post(
            reverse("location_add"),
            {"options": "1", "name": "  "},
            headers={"HX-Request": "true"},
        )

        # The response body can only be <option> elements, so the reason
        # comes back in the event instead.
        self.assertNotContains(response, "Reading Room")
        self.assertIn("quickAddFailed", response["HX-Trigger"])
        self.assertIn("name is required", response["HX-Trigger"])

    def test_quick_add_shelf_belongs_to_the_chosen_location(self):
        response = self.client.post(
            reverse("shelf_add"),
            {
                "options": "1",
                "shelf_code": "A-2",
                "location": self.location.id,
            },
            headers={"HX-Request": "true"},
        )

        created = Shelf.objects.get(shelf_code="A-2")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(created.location_id, self.location.id)
        self.assertContains(response, "A-2")
        self.assertNotContains(response, "B-1")

        # Left chosen, so the librarian carries straight on.
        self.assertContains(
            response, 'value="%d"\n        selected' % created.id
        )

        # And named as a shelf, so the page does not then reload the list
        # and throw that selection away.
        self.assertIn('"entity": "shelf"', response["HX-Trigger"])

    def test_quick_add_shelf_needs_a_location(self):
        response = self.client.post(
            reverse("shelf_add"),
            {"options": "1", "shelf_code": "Orphan"},
            headers={"HX-Request": "true"},
        )

        self.assertIn("quickAddFailed", response["HX-Trigger"])
        self.assertIn("Choose a location", response["HX-Trigger"])
        self.assertFalse(Shelf.objects.filter(shelf_code="Orphan").exists())

    def test_quick_add_shelf_reuses_a_code_already_on_that_location(self):
        response = self.client.post(
            reverse("shelf_add"),
            {
                "options": "1",
                "shelf_code": "a-1",
                "location": self.location.id,
            },
            headers={"HX-Request": "true"},
        )

        # (location, shelf_code) is UNIQUE, so this used to be a 500.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Shelf.objects.filter(location=self.location).count(), 1)

    def test_the_same_shelf_code_is_fine_in_another_location(self):
        self.client.post(
            reverse("shelf_add"),
            {
                "options": "1",
                "shelf_code": "A-1",
                "location": self.other_location.id,
            },
            headers={"HX-Request": "true"},
        )

        self.assertEqual(Shelf.objects.filter(shelf_code="A-1").count(), 2)

    def test_quick_add_needs_the_editor_role(self):
        self.client.logout()
        make_user(username="assist", password="pass12345", role="Assistant")
        self.client.login(username="assist", password="pass12345")

        for name, payload in (
            ("location_add", {"options": "1", "name": "Sneaky"}),
            ("shelf_add", {"options": "1", "shelf_code": "S", "location": 1}),
        ):
            with self.subTest(view=name):
                response = self.client.post(
                    reverse(name), payload, headers={"HX-Request": "true"}
                )

                self.assertEqual(response.status_code, 403)


    def test_option_responses_are_nothing_but_options(self):
        # They are swapped into a <select>, where the browser's parser moves
        # or drops any other tag — which is what made an out-of-band error
        # target vanish and HTMX complain.
        for response in (
            self.client.get(
                reverse("shelf_list"),
                {"options": "1", "location": self.location.id},
                headers={"HX-Request": "true"},
            ),
            self.client.post(
                reverse("location_add"),
                {"options": "1", "name": "Options Only"},
                headers={"HX-Request": "true"},
            ),
        ):
            body = response.content.decode()

            self.assertIn("<option", body)

            for tag in ("<div", "<span", "hx-swap-oob"):
                self.assertNotIn(tag, body)


class RollbackTests(BookWorkflowTestCase):

    def test_nothing_survives_a_rejected_form(self):
        before = (
            Book.objects.count(),
            BookVolume.objects.count(),
            BookCopy.objects.count(),
        )

        response = self.post_add(
            volume_mode="multiple",
            volume_number=["1", "2"],
            volume_title=["", ""],
            copies_mode="add",
            copy_qty=["2", "2"],
            code_mode="manual",
            copy_code=["A", "A", "B", "C"],
            location=self.location.id,
            shelf=self.shelf.id,
        )

        self.assertContains(response, "not all different")
        self.assertEqual(
            (
                Book.objects.count(),
                BookVolume.objects.count(),
                BookCopy.objects.count(),
            ),
            before,
        )

    def test_a_missing_title_leaves_no_volumes_behind(self):
        response = self.post_add(
            title="",
            volume_mode="multiple",
            volume_number=["1"],
            volume_title=["Something"],
        )

        self.assertContains(response, "Enter the book&#x27;s title.")
        self.assertEqual(BookVolume.objects.count(), 0)

    def test_the_form_comes_back_with_the_work_still_in_it(self):
        response = self.post_add(
            volume_mode="multiple",
            volume_number=["1", "1"],
            volume_title=["Kitab al-Iman", "Kitab al-Salah"],
        )

        self.assertContains(response, "Sahih al-Bukhari")
        self.assertContains(response, "Kitab al-Iman")
        self.assertContains(response, "Kitab al-Salah")


class EditBookDialogTests(BookWorkflowTestCase):

    def setUp(self):
        super().setUp()

        self.book = make_book(
            title="Original Title",
            author=self.author,
            category=self.category,
        )
        self.volume = make_volume(
            book=self.book, volume_number=1, title="Vol One"
        )
        self.copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-999001"
        )

    def test_dialog_returns_only_the_form(self):
        response = self.get_modal("book_edit", self.book.id)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response, "library/partials/book_edit_modal.html"
        )
        self.assertNotContains(response, "<!DOCTYPE html>")

    def test_existing_values_are_prefilled(self):
        response = self.get_modal("book_edit", self.book.id)

        self.assertContains(response, "Original Title")
        self.assertContains(response, self.author.name)
        self.assertContains(response, self.category.name)

    def test_a_successful_update_reports_it_and_saves(self):
        response = self.client.post(
            reverse("book_edit", args=[self.book.id]) + "?modal=1",
            {"title": "Renamed", "author": self.author.id},
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn("bookSaved", response["HX-Trigger"])

        self.book.refresh_from_db()
        self.assertEqual(self.book.title, "Renamed")

    def test_a_validation_error_keeps_the_dialog_open(self):
        response = self.client.post(
            reverse("book_edit", args=[self.book.id]) + "?modal=1",
            {"title": "", "author": self.author.id},
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter the book&#x27;s title.")
        self.assertNotIn("HX-Trigger", response)

        self.book.refresh_from_db()
        self.assertEqual(self.book.title, "Original Title")

    def test_editing_leaves_the_inventory_completely_alone(self):
        self.client.post(
            reverse("book_edit", args=[self.book.id]) + "?modal=1",
            {
                "title": "Renamed Again",
                "author": self.author.id,
                "publisher": self.publisher.id,
            },
            headers={"HX-Request": "true"},
        )

        self.volume.refresh_from_db()
        self.copy.refresh_from_db()

        self.assertEqual(BookVolume.objects.filter(book=self.book).count(), 1)
        self.assertEqual(self.volume.volume_number, 1)
        self.assertEqual(self.volume.title, "Vol One")

        self.assertEqual(BookCopy.objects.filter(volume=self.volume).count(), 1)
        self.assertEqual(self.copy.copy_code, "LIB-999001")
        self.assertEqual(self.copy.shelf_id, self.shelf.id)
        self.assertEqual(self.copy.status, BookCopy.STATUS_AVAILABLE)

    def test_editing_does_not_disturb_an_active_loan(self):
        loan = make_loan(copy=self.copy)

        self.client.post(
            reverse("book_edit", args=[self.book.id]) + "?modal=1",
            {"title": "Renamed Once More", "author": self.author.id},
            headers={"HX-Request": "true"},
        )

        loan.refresh_from_db()
        self.copy.refresh_from_db()

        self.assertIsNone(loan.return_date)
        self.assertEqual(loan.copy_id, self.copy.id)
        self.assertEqual(self.copy.status, "Issued")


class DeleteBookDialogTests(BookWorkflowTestCase):

    def setUp(self):
        super().setUp()
        self.book = make_book(title="Removable", author=self.author)

    def test_dialog_returns_only_the_confirmation(self):
        response = self.get_modal("book_delete", self.book.id)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response, "library/partials/book_delete_modal.html"
        )
        self.assertContains(response, "Removable")
        self.assertNotContains(response, "<!DOCTYPE html>")

    def test_a_get_never_deletes_anything(self):
        self.get_modal("book_delete", self.book.id)

        self.assertTrue(Book.objects.filter(id=self.book.id).exists())

    def test_a_book_with_nothing_attached_can_go(self):
        response = self.client.post(
            reverse("book_delete", args=[self.book.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn("bookDeleted", response["HX-Trigger"])
        self.assertFalse(Book.objects.filter(id=self.book.id).exists())

    def test_volumes_go_with_the_book(self):
        make_volume(book=self.book, volume_number=1, title="Only")

        self.client.post(
            reverse("book_delete", args=[self.book.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )

        self.assertFalse(Book.objects.filter(id=self.book.id).exists())
        self.assertEqual(BookVolume.objects.count(), 0)

    def test_a_book_with_copies_is_blocked_not_crashed(self):
        # book_copies.volume_id is a NO ACTION foreign key, so the cascade
        # from books to book_volumes used to abort here with a 500.
        volume = make_volume(book=self.book, volume_number=1)
        make_copy(volume=volume, shelf=self.shelf, copy_code="LIB-900001")

        response = self.client.post(
            reverse("book_delete", args=[self.book.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "still has 1 physical copy")
        self.assertTrue(Book.objects.filter(id=self.book.id).exists())
        self.assertEqual(BookCopy.objects.count(), 1)

    def test_an_issued_copy_says_so(self):
        volume = make_volume(book=self.book, volume_number=1)
        copy = make_copy(volume=volume, shelf=self.shelf, copy_code="LIB-900002")
        make_loan(copy=copy)

        response = self.client.post(
            reverse("book_delete", args=[self.book.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )

        self.assertContains(response, "currently issued")
        self.assertTrue(Book.objects.filter(id=self.book.id).exists())

    def test_loan_history_alone_still_blocks_the_delete(self):
        # loans.copy_id is NO ACTION too, so a returned loan pins its copy.
        volume = make_volume(book=self.book, volume_number=1)
        copy = make_copy(volume=volume, shelf=self.shelf, copy_code="LIB-900003")
        make_loan(
            copy=copy,
            issue_date=date(2020, 1, 1),
            due_date=date(2020, 1, 15),
            return_date=date(2020, 1, 10),
        )

        response = self.client.post(
            reverse("book_delete", args=[self.book.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )

        self.assertContains(response, "loan history")
        self.assertTrue(Book.objects.filter(id=self.book.id).exists())

    def test_the_blocked_dialog_offers_no_delete_button(self):
        volume = make_volume(book=self.book, volume_number=1)
        make_copy(volume=volume, shelf=self.shelf, copy_code="LIB-900004")

        response = self.get_modal("book_delete", self.book.id)

        self.assertNotContains(response, "book_delete")

    def test_a_scriptless_delete_is_blocked_too(self):
        """There is no Delete Book page left, so it redirects with the
        reason as a message. What matters is unchanged: the copies still
        block it and the book is still there."""

        volume = make_volume(book=self.book, volume_number=1)
        make_copy(volume=volume, shelf=self.shelf, copy_code="LIB-900005")

        response = self.client.post(
            reverse("book_delete", args=[self.book.id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Book.objects.filter(id=self.book.id).exists())


class BookListIntegrationTests(BookWorkflowTestCase):

    def test_the_list_still_renders_and_carries_the_dialog_hooks(self):
        make_book(title="On The List", author=self.author)

        response = self.client.get(reverse("book_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "On The List")
        self.assertContains(response, "data-form-modal")
        self.assertContains(response, "bookListRefresh")

    def test_the_dialog_links_are_still_real_urls(self):
        book = make_book(title="Clickable", author=self.author)

        response = self.client.get(reverse("book_list"))

        # Middle-click, and life without JavaScript, both still work.
        self.assertContains(response, reverse("book_add"))
        self.assertContains(response, reverse("book_edit", args=[book.id]))
        self.assertContains(response, reverse("book_delete", args=[book.id]))

    def test_the_dialog_buttons_do_not_rewrite_the_address_bar(self):
        # They sit inside #bookResults, which sets hx-push-url="true" and
        # hx-vals for the sort and paging links. Inheriting either would put
        # the edit URL in the address bar just for opening a dialog, and
        # send `partial` to a view that has no use for it.
        book = make_book(title="Inherits Nothing", author=self.author)

        response = self.client.get(reverse("book_list"))
        body = response.content.decode()

        for name in ("book_edit", "book_delete"):
            with self.subTest(view=name):
                anchor = body.index(reverse(name, args=[book.id]) + "?modal=1")
                tag = body[body.rindex("<a", 0, anchor):body.index(">", anchor)]

                self.assertIn('hx-push-url="false"', tag)
                self.assertIn('"partial": ""', tag)

    def test_a_background_refresh_does_not_push_history(self):
        response = self.client.get(
            reverse("book_list"),
            {"partial": "results", "refresh": "1"},
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Push-Url", response)

    def test_an_ordinary_fragment_request_still_pushes_history(self):
        response = self.client.get(
            reverse("book_list"),
            {"partial": "results", "search": "On"},
            headers={"HX-Request": "true"},
        )

        self.assertIn("HX-Push-Url", response)

    def test_the_refresh_url_keeps_the_current_list_state(self):
        response = self.client.get(
            reverse("book_list"),
            {"search": "On", "sort": "author", "direction": "desc",
             "page_size": "50"},
        )

        body = response.content.decode()
        refresh = body[body.index('id="bookListRefresh"'):]
        refresh = refresh[:refresh.index("></div>")]

        for expected in ("search=On", "sort=author", "direction=desc",
                         "page_size=50"):
            self.assertIn(expected, refresh)


class BookFieldValidationTests(BookWorkflowTestCase):
    """What the form refuses, and what it says about it.

    Both views call `validate_book_details`, so these hold for Add and
    Edit alike - which the last test here checks by asking both.
    """

    def setUp(self):
        super().setUp()

        self.book = make_book(
            title="Something Already Here", author=self.author
        )

    def edit(self, **overrides):
        payload = self.base_payload(**overrides)

        return self.client.post(
            reverse("book_edit", args=[self.book.id]) + "?modal=1",
            payload,
            headers={"HX-Request": "true"},
        )

    # ------------------------------------------- the two that were 500s

    def test_an_author_id_that_is_not_a_number_is_refused_not_a_crash(self):
        """This reached `create(author_id="abc")` and raised ValueError.

        Not exotic: the author is a hidden field the combobox fills in, so
        a stale page or a blocked script can post anything at all.
        """

        before = Book.objects.count()

        response = self.post_add(author="not-a-number")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "was not recognised")
        self.assertEqual(Book.objects.count(), before)

    def test_a_title_longer_than_the_column_is_refused_not_a_crash(self):
        """500 characters is the column; 501 raised DataError."""

        before = Book.objects.count()

        response = self.post_add(title="x" * 501)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Shorten it to 500")
        self.assertEqual(Book.objects.count(), before)

    def test_a_title_of_exactly_the_limit_is_accepted(self):
        """The boundary belongs to the valid side."""

        response = self.post_add(title="y" * 500)

        # A dialog answers a save with 204 and an event, not a redirect.
        self.assertEqual(response.status_code, 204)
        self.assertTrue(Book.objects.filter(title="y" * 500).exists())

    def test_an_author_that_no_longer_exists_is_refused(self):
        before = Book.objects.count()

        response = self.post_add(author=9999999)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no longer exists")
        self.assertEqual(Book.objects.count(), before)

    def test_a_category_that_is_not_a_number_is_refused(self):
        # Optional, but not a licence to write nonsense into the column.
        before = Book.objects.count()

        response = self.post_add(category="abc")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "category was not recognised")
        self.assertEqual(Book.objects.count(), before)

    def test_a_publisher_that_no_longer_exists_is_refused(self):
        before = Book.objects.count()

        response = self.post_add(publisher=9999999)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no longer exists")
        self.assertEqual(Book.objects.count(), before)

    def test_an_empty_category_is_still_allowed(self):
        """Optional means optional; only a wrong value is refused."""

        response = self.post_add(category="", publisher="")

        self.assertEqual(response.status_code, 204)

        saved = Book.objects.get(title="Sahih al-Bukhari")

        self.assertIsNone(saved.category_id)
        self.assertIsNone(saved.publisher_id)

    # -------------------------------------------- said next to the field

    def test_each_message_names_only_the_field_it_is_about(self):
        """"Title and Author are required." named both when one was.

        A missing author should not accuse the title the librarian just
        typed.
        """

        response = self.post_add(author="")

        self.assertContains(response, "Choose the author.")
        self.assertNotContains(response, "Enter the book&#x27;s title.")

    def test_the_errors_come_back_keyed_by_field(self):
        response = self.post_add(title="", author="")

        self.assertEqual(
            sorted(response.context["errors"]), ["author", "title"]
        )

    def test_the_field_is_marked_invalid_for_a_screen_reader_too(self):
        response = self.post_add(title="")

        self.assertContains(response, "is-invalid")
        self.assertContains(response, "invalid-feedback")

    def test_a_refused_form_keeps_what_was_typed_into_it(self):
        response = self.post_add(
            title="A Title Worth Keeping", author=""
        )

        self.assertEqual(
            response.context["form_data"]["title"], "A Title Worth Keeping"
        )
        self.assertContains(response, "A Title Worth Keeping")

    def test_the_alert_counts_the_problems_without_repeating_them(self):
        one = self.post_add(title="")
        two = self.post_add(title="", author="")

        self.assertContains(one, "problem with one of the fields")
        self.assertContains(two, "problems with 2 of the fields")

    # ------------------------------------------------ add and edit agree

    def test_add_and_edit_refuse_the_same_input_the_same_way(self):
        """The point of the change, checked rather than assumed.

        Both views used to state these rules themselves. They happened to
        agree, and were one edit from not doing.
        """

        cases = (
            ("no title", {"title": ""}),
            ("no author", {"author": ""}),
            ("bad author", {"author": "not-a-number"}),
            ("absent author", {"author": 9999999}),
            ("long title", {"title": "x" * 501}),
            ("bad category", {"category": "abc"}),
        )

        for label, payload in cases:
            with self.subTest(case=label):

                added = self.post_add(**payload)
                edited = self.edit(**payload)

                self.assertEqual(added.status_code, 200)
                self.assertEqual(edited.status_code, 200)

                self.assertEqual(
                    sorted(added.context["errors"].items()),
                    sorted(edited.context["errors"].items()),
                )

    def test_an_edit_refused_for_a_field_changes_nothing(self):
        response = self.edit(title="x" * 501)

        self.assertEqual(response.status_code, 200)

        self.book.refresh_from_db()
        self.assertEqual(self.book.title, "Something Already Here")

    def test_a_scriptless_post_is_refused_out_loud_not_dropped(self):
        """There is no page to re-render, so it redirects with a message.

        What matters is that it is still refused and still writes
        nothing - a form posted without htmx must not quietly succeed.
        """

        before = Book.objects.count()

        response = self.client.post(
            reverse("book_add"), self.base_payload(author="")
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Book.objects.count(), before)
