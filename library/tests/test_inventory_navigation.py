"""Locations -> Location -> Shelf -> Copy navigation, and the counts on it.

Covers that the trail is walkable, that every count is right and costs a
fixed number of queries, that unshelved copies are reachable and stop being
unshelved once placed, and that the existing delete guards still explain
themselves rather than crashing.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from library.models import BookCopy, Location, Shelf

from .helpers import (
    make_author,
    make_book,
    make_copy,
    make_loan,
    make_location,
    make_shelf,
    make_user,
    make_volume,
)


class InventoryTestCase(TestCase):

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        self.author = make_author(name="Imam Malik")
        self.book = make_book(title="Al-Muwatta", author=self.author)
        self.volume = make_volume(book=self.book, volume_number=1, title="")

        self.hall = make_location(name="Main Hall")
        self.a1 = make_shelf(location=self.hall, shelf_code="A-1")
        self.a2 = make_shelf(location=self.hall, shelf_code="A-2")

        self.annexe = make_location(name="Annexe")
        self.b1 = make_shelf(location=self.annexe, shelf_code="B-1")

        self.empty = make_location(name="Empty Room")

        # 3 on A-1, 1 on A-2, 1 on B-1, 2 with no shelf at all.
        for index in range(3):
            make_copy(volume=self.volume, shelf=self.a1,
                      copy_code="INV-A1-%d" % index)

        make_copy(volume=self.volume, shelf=self.a2, copy_code="INV-A2-0")
        make_copy(volume=self.volume, shelf=self.b1, copy_code="INV-B1-0")

        self.loose = [
            make_copy(volume=self.volume, copy_code="INV-LOOSE-0"),
            make_copy(volume=self.volume, copy_code="INV-LOOSE-1"),
        ]

    def as_assistant(self):
        self.client.logout()
        make_user(username="assist", password="pass12345", role="Assistant")
        self.client.login(username="assist", password="pass12345")


class LocationListTests(InventoryTestCase):

    def get(self, **params):
        return self.client.get(reverse("location_list"), params)

    def counts(self, response):
        return {
            location.name: (location.shelf_count, location.copy_count)
            for location in response.context["locations"]
        }

    def test_it_renders_on_the_shared_layout(self):
        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "library/base.html")

    def test_shelf_and_copy_counts_are_right(self):
        # Two joins that multiply each other's rows, so this is where a
        # missing `distinct` would show up as shelves counted per copy.
        self.assertEqual(
            self.counts(self.get()),
            {
                "Annexe": (1, 1),
                "Empty Room": (0, 0),
                "Main Hall": (2, 4),
            },
        )

    def test_unshelved_copies_are_counted_and_offered(self):
        response = self.get()

        self.assertEqual(response.context["unshelved_count"], 2)
        self.assertContains(
            response, reverse("book_copy_list") + "?status=unshelved"
        )
        self.assertContains(response, "have arrived")

    def test_the_unshelved_banner_reads_correctly_for_one_copy(self):
        BookCopy.objects.filter(copy_code="INV-LOOSE-1").delete()

        response = self.get()

        self.assertEqual(response.context["unshelved_count"], 1)
        self.assertContains(response, "has arrived")
        self.assertContains(response, "is not on a shelf")
        self.assertNotContains(response, "have arrived")

    def test_each_row_opens_its_location(self):
        response = self.get()

        self.assertContains(
            response,
            'data-row-url="%s"' % reverse(
                "location_detail", args=[self.hall.id]
            ),
        )

    def test_the_action_buttons_are_kept_out_of_the_row_click(self):
        self.assertContains(self.get(), "data-row-actions")

    def test_no_database_ids_are_shown_as_text(self):
        response = self.get()
        body = response.content.decode()

        # Ids appear in hrefs, which is unavoidable; they must not be a
        # column the librarian has to read.
        self.assertNotIn("<td>%d</td>" % self.hall.id, body)

    def test_search_narrows_the_list(self):
        response = self.get(search="Annexe")

        self.assertEqual(list(self.counts(response)), ["Annexe"])

    def test_the_page_costs_the_same_however_many_locations(self):
        with CaptureQueriesContext(connection) as few:
            self.get()

        for index in range(10):
            location = make_location(name="Extra %d" % index)
            shelf = make_shelf(location=location, shelf_code="S-%d" % index)
            make_copy(volume=self.volume, shelf=shelf,
                      copy_code="INV-X-%d" % index)

        with CaptureQueriesContext(connection) as many:
            self.get()

        self.assertEqual(len(few), len(many))

    def test_an_assistant_is_not_offered_management_actions(self):
        self.as_assistant()

        response = self.get()

        self.assertFalse(response.context["can_edit"])
        self.assertNotContains(response, reverse("location_add"))
        self.assertNotContains(
            response, reverse("location_delete", args=[self.hall.id])
        )


class LocationDetailTests(InventoryTestCase):

    def get(self, location=None):
        return self.client.get(
            reverse("location_detail", args=[(location or self.hall).id])
        )

    def test_it_shows_its_totals(self):
        response = self.get()

        self.assertEqual(response.context["shelf_count"], 2)
        self.assertEqual(response.context["copy_count"], 4)

    def test_each_shelf_carries_its_own_count(self):
        response = self.get()

        self.assertEqual(
            {s.shelf_code: s.copy_count for s in response.context["shelves"]},
            {"A-1": 3, "A-2": 1},
        )

    def test_it_lists_shelves_not_individual_copies(self):
        # A location can hold thousands; the page must not grow with them.
        response = self.get()

        self.assertNotIn("copies", response.context)
        self.assertNotContains(response, "INV-A1-0")

    def test_each_shelf_row_opens_the_shelf(self):
        response = self.get()

        self.assertContains(
            response,
            'data-row-url="%s"' % reverse("shelf_detail", args=[self.a1.id]),
        )

    def test_it_offers_the_way_through_to_all_its_copies(self):
        response = self.get()

        self.assertContains(
            response,
            reverse("book_copy_list") + "?location=%d" % self.hall.id,
        )

    def test_it_leads_back_to_the_locations_list(self):
        self.assertContains(self.get(), reverse("location_list"))

    def test_an_empty_location_says_so(self):
        response = self.get(self.empty)

        self.assertEqual(response.context["shelf_count"], 0)
        self.assertContains(response, "no shelves yet")

    def test_the_page_costs_the_same_however_many_shelves(self):
        with CaptureQueriesContext(connection) as few:
            self.get()

        for index in range(10):
            shelf = make_shelf(location=self.hall, shelf_code="A-%d" % (index + 5))
            make_copy(volume=self.volume, shelf=shelf,
                      copy_code="INV-M-%d" % index)

        with CaptureQueriesContext(connection) as many:
            self.get()

        self.assertEqual(len(few), len(many))


class ShelfListTests(InventoryTestCase):

    def get(self, **params):
        return self.client.get(reverse("shelf_list"), params)

    def codes(self, response):
        return {
            shelf.shelf_code: shelf.copy_count
            for shelf in response.context["shelves"]
        }

    def test_it_shows_each_shelf_with_its_location_and_count(self):
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.codes(self.get()), {"A-1": 3, "A-2": 1, "B-1": 1})

    def test_each_row_opens_its_shelf(self):
        self.assertContains(
            self.get(),
            'data-row-url="%s"' % reverse("shelf_detail", args=[self.a1.id]),
        )

    def test_the_existing_search_still_works(self):
        self.assertEqual(list(self.codes(self.get(search="B-1"))), ["B-1"])
        self.assertEqual(list(self.codes(self.get(search="Annexe"))), ["B-1"])

    def test_the_existing_location_filter_still_works(self):
        self.assertEqual(
            sorted(self.codes(self.get(location=self.hall.id))),
            ["A-1", "A-2"],
        )

    def test_unshelved_copies_are_offered_here_too(self):
        response = self.get()

        self.assertEqual(response.context["unshelved_count"], 2)
        self.assertContains(
            response, reverse("book_copy_list") + "?status=unshelved"
        )

    def test_the_page_costs_the_same_however_many_shelves(self):
        with CaptureQueriesContext(connection) as few:
            self.get(search="A")

        for index in range(10):
            shelf = make_shelf(location=self.hall, shelf_code="A-%d" % (index + 5))
            make_copy(volume=self.volume, shelf=shelf,
                      copy_code="INV-S-%d" % index)

        with CaptureQueriesContext(connection) as many:
            self.get(search="A")

        self.assertEqual(len(few), len(many))

    def test_an_assistant_is_not_offered_management_actions(self):
        self.as_assistant()

        response = self.get()

        self.assertFalse(response.context["can_edit"])
        self.assertNotContains(response, reverse("shelf_add"))


class ShelfDetailTests(InventoryTestCase):

    def get(self, shelf=None, **params):
        return self.client.get(
            reverse("shelf_detail", args=[(shelf or self.a1).id]), params
        )

    def codes(self, response):
        return sorted(c.copy_code for c in response.context["copies"])

    def test_it_lists_the_copies_on_that_shelf_only(self):
        self.assertEqual(
            self.codes(self.get()),
            ["INV-A1-0", "INV-A1-1", "INV-A1-2"],
        )

    def test_every_copy_carries_its_derived_status(self):
        # From the same helper the copy list uses, so the word is the same.
        make_loan(copy=BookCopy.objects.get(copy_code="INV-A1-0"))

        states = {
            copy.copy_code: copy.state
            for copy in self.get().context["copies"]
        }

        self.assertEqual(states["INV-A1-0"], "issued")
        self.assertEqual(states["INV-A1-1"], "available")

    def test_each_copy_opens_the_existing_copy_detail(self):
        copy = BookCopy.objects.get(copy_code="INV-A1-0")

        self.assertContains(
            self.get(),
            'data-copy-url="%s?modal=1"' % reverse(
                "book_copy_detail", args=[copy.id]
            ),
        )

    def test_it_groups_the_titles_on_the_shelf(self):
        other = make_book(title="Al-Mudawwana", author=self.author)
        other_volume = make_volume(book=other, volume_number=1, title="")
        make_copy(volume=other_volume, shelf=self.a1, copy_code="INV-A1-9")

        response = self.get()

        self.assertEqual(
            {t["volume__book__title"]: t["copies"]
             for t in response.context["titles"]},
            {"Al-Mudawwana": 1, "Al-Muwatta": 3},
        )
        self.assertEqual(response.context["title_count"], 2)

    def test_a_grouped_title_opens_the_existing_book_page(self):
        self.assertContains(
            self.get(), reverse("book_detail", args=[self.book.id])
        )

    def test_search_by_copy_code(self):
        self.assertEqual(self.codes(self.get(search="A1-1")), ["INV-A1-1"])

    def test_search_by_book_title(self):
        self.assertEqual(len(self.codes(self.get(search="Muwatta"))), 3)

    def test_search_that_matches_nothing_says_so(self):
        response = self.get(search="zzzz")

        self.assertEqual(self.codes(response), [])
        self.assertContains(response, "matches that")

    def test_the_copies_are_paginated(self):
        for index in range(30):
            make_copy(volume=self.volume, shelf=self.a1,
                      copy_code="INV-A1-P%d" % index)

        response = self.get()

        self.assertEqual(response.context["paginator"].count, 33)
        self.assertEqual(len(response.context["copies"]), 25)

    def test_the_page_costs_the_same_however_many_copies(self):
        with CaptureQueriesContext(connection) as few:
            self.get()

        for index in range(30):
            make_copy(volume=self.volume, shelf=self.a1,
                      copy_code="INV-A1-Q%d" % index)

        with CaptureQueriesContext(connection) as many:
            self.get()

        self.assertEqual(len(few), len(many))

    def test_it_leads_back_up_the_trail(self):
        response = self.get()

        self.assertContains(response, reverse("location_list"))
        self.assertContains(
            response, reverse("location_detail", args=[self.hall.id])
        )

    def test_an_empty_shelf_says_so(self):
        empty = make_shelf(location=self.hall, shelf_code="A-99")

        response = self.get(empty)

        self.assertContains(response, "This shelf is empty")

    def test_an_assistant_is_not_offered_management_actions(self):
        self.as_assistant()

        response = self.get()

        self.assertFalse(response.context["can_edit"])
        self.assertNotContains(response, reverse("shelf_edit", args=[self.a1.id]))


class UnshelvedCopyTests(InventoryTestCase):

    def test_they_are_reachable_and_correct(self):
        response = self.client.get(
            reverse("book_copy_list"), {"status": "unshelved"}
        )

        self.assertEqual(
            sorted(c.copy_code for c in response.context["copies"]),
            ["INV-LOOSE-0", "INV-LOOSE-1"],
        )

    def test_placing_one_moves_it_onto_the_shelf(self):
        loose = self.loose[0]

        response = self.client.post(
            reverse("book_copy_bulk_move"),
            {
                "copy": [str(loose.id)],
                "location": self.hall.id,
                "shelf": self.a1.id,
            },
        )

        self.assertEqual(response.status_code, 302)

        loose.refresh_from_db()
        self.assertEqual(loose.shelf_id, self.a1.id)

        # Gone from Unshelved...
        unshelved = self.client.get(
            reverse("book_copy_list"), {"status": "unshelved"}
        )
        self.assertEqual(
            [c.copy_code for c in unshelved.context["copies"]],
            ["INV-LOOSE-1"],
        )

        # ...and on the shelf it was moved to.
        shelf = self.client.get(reverse("shelf_detail", args=[self.a1.id]))
        self.assertIn(
            loose.copy_code,
            [c.copy_code for c in shelf.context["copies"]],
        )

        # And the counts followed it.
        self.assertEqual(
            self.client.get(
                reverse("location_detail", args=[self.hall.id])
            ).context["copy_count"],
            5,
        )


class DeleteSafetyTests(InventoryTestCase):
    """The guards that were already here, confirmed still in force."""

    def test_a_location_with_shelves_is_refused_not_crashed(self):
        response = self.client.post(
            reverse("location_delete", args=[self.hall.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Location.objects.filter(id=self.hall.id).exists())
        self.assertEqual(Shelf.objects.filter(location=self.hall).count(), 2)

    def test_an_empty_location_can_be_deleted(self):
        response = self.client.post(
            reverse("location_delete", args=[self.empty.id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Location.objects.filter(id=self.empty.id).exists())

    def test_a_shelf_with_copies_is_refused_not_crashed(self):
        response = self.client.post(
            reverse("shelf_delete", args=[self.a1.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Shelf.objects.filter(id=self.a1.id).exists())
        self.assertEqual(BookCopy.objects.filter(shelf=self.a1).count(), 3)

    def test_the_refused_shelf_offers_a_way_to_move_the_copies(self):
        response = self.client.get(reverse("shelf_delete", args=[self.a1.id]))

        self.assertContains(
            response, reverse("book_copy_list") + "?shelf=%d" % self.a1.id
        )

    def test_an_empty_shelf_can_be_deleted(self):
        empty = make_shelf(location=self.hall, shelf_code="A-98")

        response = self.client.post(reverse("shelf_delete", args=[empty.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Shelf.objects.filter(id=empty.id).exists())

    def test_deleting_a_shelf_never_touches_copies_elsewhere(self):
        empty = make_shelf(location=self.hall, shelf_code="A-97")

        self.client.post(reverse("shelf_delete", args=[empty.id]))

        self.assertEqual(BookCopy.objects.count(), 7)
        self.assertEqual(BookCopy.objects.filter(shelf=self.a1).count(), 3)

    def test_an_assistant_cannot_delete_either(self):
        self.as_assistant()

        for name, args in (
            ("location_delete", [self.empty.id]),
            ("shelf_delete", [self.a1.id]),
        ):
            with self.subTest(view=name):
                self.assertEqual(
                    self.client.post(reverse(name, args=args)).status_code,
                    403,
                )


class InventoryFormTests(InventoryTestCase):
    """The add/edit workflows, unchanged but now on the shared layout."""

    def test_every_inventory_page_uses_the_shared_layout(self):
        pages = [
            reverse("location_list"),
            reverse("location_detail", args=[self.hall.id]),
            reverse("location_add"),
            reverse("location_edit", args=[self.hall.id]),
            reverse("location_delete", args=[self.empty.id]),
            reverse("shelf_list"),
            reverse("shelf_detail", args=[self.a1.id]),
            reverse("shelf_add"),
            reverse("shelf_edit", args=[self.a1.id]),
            reverse("shelf_delete", args=[self.a1.id]),
        ]

        for url in pages:
            with self.subTest(page=url):
                response = self.client.get(url)

                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "library/base.html")

    def test_adding_a_location_still_works(self):
        response = self.client.post(
            reverse("location_add"),
            {"name": "New Wing", "description": "Upstairs"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Location.objects.filter(name="New Wing").exists())

    def test_editing_a_shelf_still_works(self):
        self.client.post(
            reverse("shelf_edit", args=[self.a1.id]),
            {
                "location": self.hall.id,
                "shelf_code": "A-1b",
                "description": "",
            },
        )

        self.a1.refresh_from_db()

        self.assertEqual(self.a1.shelf_code, "A-1b")
        # And its copies stayed where they were.
        self.assertEqual(BookCopy.objects.filter(shelf=self.a1).count(), 3)

    def test_the_shelf_edit_form_preselects_its_location(self):
        response = self.client.get(reverse("shelf_edit", args=[self.a1.id]))

        self.assertContains(
            response, 'value="%d"\n                                        selected'
            % self.hall.id
        )
