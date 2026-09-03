"""Copy navigation, the copy list, copy details, editing and bulk moves.

Covers what a librarian sees about a physical copy — the state derived from
its loan and its shelf — and the guarantees that matter around it: loan
history untouched, codes unchanged and unique, the Book and Volume never
written to by copy-level editing, and a batch move that lands completely or
not at all.
"""

from datetime import date, timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library.models import BookCopy, Loan

from .helpers import (
    make_author,
    make_book,
    make_borrower,
    make_copy,
    make_loan,
    make_location,
    make_shelf,
    make_user,
    make_volume,
)


class CopyTestCase(TestCase):

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        self.author = make_author(name="Imam Nawawi")
        self.book = make_book(title="Riyad as-Salihin", author=self.author)

        # A single-volume book: volume 1 with no title, which is how the
        # guided Add Book flow records "this book has no volumes".
        self.volume = make_volume(book=self.book, volume_number=1, title="")

        self.location = make_location(name="Main Hall")
        self.shelf = make_shelf(location=self.location, shelf_code="A-1")

        self.other_location = make_location(name="Annexe")
        self.other_shelf = make_shelf(
            location=self.other_location,
            shelf_code="B-1",
        )

    def as_assistant(self):
        self.client.logout()
        make_user(username="assist", password="pass12345", role="Assistant")
        self.client.login(username="assist", password="pass12345")

    def copy_rows(self, response):
        return list(response.context["copies"])


class CopyStateTests(CopyTestCase):
    """The one word shown for a copy, worked out from the real data."""

    def state_of(self, copy):
        response = self.client.get(
            reverse("book_copy_detail", args=[copy.id])
        )
        return response.context["copy"].state

    def test_a_shelved_copy_with_no_loan_is_available(self):
        copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-900001"
        )

        self.assertEqual(self.state_of(copy), "available")

    def test_a_copy_with_no_shelf_is_unshelved(self):
        copy = make_copy(volume=self.volume, copy_code="LIB-900002")

        self.assertEqual(self.state_of(copy), "unshelved")

    def test_a_copy_on_loan_is_issued(self):
        copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-900003"
        )
        make_loan(copy=copy)

        self.assertEqual(self.state_of(copy), "issued")

    def test_a_copy_past_its_due_date_is_overdue(self):
        # Counted from the same clock the code uses. `date.today()` is
        # local and `timezone.now().date()` is UTC, so near midnight the
        # two disagree by a day and the count would be off by one.
        today = timezone.now().date()

        copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-900004"
        )
        make_loan(
            copy=copy,
            issue_date=today - timedelta(days=30),
            due_date=today - timedelta(days=16),
        )

        response = self.client.get(
            reverse("book_copy_detail", args=[copy.id])
        )

        self.assertEqual(response.context["copy"].state, "overdue")
        self.assertEqual(response.context["copy"].days_overdue, 16)

    def test_a_returned_loan_leaves_the_copy_available(self):
        copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-900005"
        )
        make_loan(
            copy=copy,
            issue_date=date(2020, 1, 1),
            due_date=date(2020, 1, 15),
            return_date=date(2020, 1, 10),
        )

        self.assertEqual(self.state_of(copy), "available")

    def test_a_damaged_copy_says_damaged(self):
        copy = make_copy(
            volume=self.volume,
            shelf=self.shelf,
            copy_code="LIB-900006",
            status="Damaged",
        )

        self.assertEqual(self.state_of(copy), "damaged")

    def test_a_stored_issued_with_no_loan_is_not_repeated(self):
        # The loan is the record of a book being out. Without one, the
        # column saying "Issued" is stale and the screen must not claim it.
        copy = make_copy(
            volume=self.volume,
            shelf=self.shelf,
            copy_code="LIB-900007",
            status="Issued",
        )

        self.assertEqual(self.state_of(copy), "available")

    def test_the_state_is_never_written_back_to_the_column(self):
        copy = make_copy(volume=self.volume, copy_code="LIB-900008")

        self.state_of(copy)
        copy.refresh_from_db()

        self.assertEqual(copy.status, "Available")
        self.assertIsNone(copy.shelf_id)


class CopyListTests(CopyTestCase):

    def setUp(self):
        super().setUp()

        self.available = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-800001"
        )
        self.unshelved = make_copy(
            volume=self.volume, copy_code="LIB-800002"
        )
        self.issued = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-800003"
        )
        make_loan(copy=self.issued)

        self.overdue = make_copy(
            volume=self.volume, shelf=self.other_shelf,
            copy_code="LIB-800004",
        )
        make_loan(
            copy=self.overdue,
            borrower=make_borrower(name="Late Reader", phone="555"),
            issue_date=date.today() - timedelta(days=40),
            due_date=date.today() - timedelta(days=5),
        )

    def get(self, **params):
        return self.client.get(reverse("book_copy_list"), params)

    def codes(self, response):
        return sorted(copy.copy_code for copy in self.copy_rows(response))

    def test_the_list_renders_on_the_shared_layout(self):
        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "library/base.html")
        self.assertTemplateUsed(
            response, "library/partials/copy_list_results.html"
        )

    def test_every_copy_is_described(self):
        response = self.get()

        states = {
            copy.copy_code: copy.state
            for copy in self.copy_rows(response)
        }

        self.assertEqual(states["LIB-800001"], "available")
        self.assertEqual(states["LIB-800002"], "unshelved")
        self.assertEqual(states["LIB-800003"], "issued")
        self.assertEqual(states["LIB-800004"], "overdue")

    def test_an_unshelved_copy_is_named_not_left_blank(self):
        response = self.get(status="unshelved")

        self.assertContains(response, "Unshelved")
        self.assertContains(response, "Not placed")

    def test_search_matches_a_copy_code(self):
        self.assertEqual(self.codes(self.get(search="800002")), ["LIB-800002"])

    def test_search_matches_a_book_title(self):
        other = make_book(title="Bulugh al-Maram", author=self.author)
        make_copy(
            volume=make_volume(book=other, volume_number=1, title=""),
            shelf=self.shelf,
            copy_code="LIB-800009",
        )

        self.assertEqual(self.codes(self.get(search="Bulugh")), ["LIB-800009"])

    def test_the_old_copy_code_parameter_still_searches(self):
        self.assertEqual(
            self.codes(self.get(copy_code="800001")), ["LIB-800001"]
        )

    def test_filtering_by_each_state(self):
        for state, expected in (
            ("available", ["LIB-800001"]),
            ("unshelved", ["LIB-800002"]),
            ("issued", ["LIB-800003", "LIB-800004"]),
            ("overdue", ["LIB-800004"]),
        ):
            with self.subTest(state=state):
                self.assertEqual(self.codes(self.get(status=state)), expected)

    def test_a_capitalised_status_from_an_older_link_still_filters(self):
        self.assertEqual(
            self.codes(self.get(status="Available")), ["LIB-800001"]
        )

    def test_filtering_by_location_and_shelf(self):
        self.assertEqual(
            self.codes(self.get(location=self.other_location.id)),
            ["LIB-800004"],
        )
        self.assertEqual(
            self.codes(self.get(shelf=self.shelf.id)),
            ["LIB-800001", "LIB-800003"],
        )

    def test_filtering_by_book_and_volume_still_works(self):
        # These arrive as links from a book or a volume page. All four
        # fixture copies belong to this book's only volume, so a second
        # book is needed to show the filter doing anything.
        other = make_book(title="Something Else", author=self.author)
        other_volume = make_volume(book=other, volume_number=1, title="")
        make_copy(
            volume=other_volume, shelf=self.shelf, copy_code="LIB-800099"
        )

        self.assertEqual(len(self.copy_rows(self.get())), 5)
        self.assertEqual(len(self.copy_rows(self.get(book=self.book.id))), 4)
        self.assertEqual(
            len(self.copy_rows(self.get(volume=self.volume.id))), 4
        )
        self.assertEqual(
            self.codes(self.get(volume=other_volume.id)), ["LIB-800099"]
        )

    def test_every_active_filter_gets_a_chip(self):
        response = self.get(
            search="LIB",
            status="available",
            book=self.book.id,
            volume=self.volume.id,
            location=self.location.id,
        )

        labels = [f["label"] for f in response.context["active_filters"]]

        self.assertEqual(
            labels, ["Search", "Status", "Book", "Volume", "Location"]
        )

    def test_sorting_by_each_column(self):
        for key in ("code", "book", "volume", "location", "shelf", "status"):
            with self.subTest(column=key):
                response = self.get(sort=key, direction="desc")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["sort"], key)

    def test_unshelved_copies_sort_last_rather_than_first(self):
        response = self.get(sort="shelf", direction="asc")

        codes = [copy.copy_code for copy in self.copy_rows(response)]

        self.assertEqual(codes[-1], "LIB-800002")

    def test_the_page_costs_the_same_however_many_copies_exist(self):
        # It used to load every active loan in the library on every
        # request, so the cost grew with circulation.
        with CaptureQueriesContext(connection) as few:
            self.get(page_size=10)

        for index in range(20):
            copy = make_copy(
                volume=self.volume,
                shelf=self.shelf,
                copy_code="LIB-7000%02d" % index,
            )
            make_loan(
                copy=copy,
                borrower=make_borrower(name="R%d" % index, phone="1"),
            )

        with CaptureQueriesContext(connection) as many:
            self.get(page_size=10)

        self.assertLessEqual(len(many), len(few) + 2)

    def test_the_fragment_returns_only_the_results(self):
        response = self.client.get(
            reverse("book_copy_list"),
            {"partial": "results"},
            headers={"HX-Request": "true"},
        )

        self.assertTemplateUsed(
            response, "library/partials/copy_list_results.html"
        )
        self.assertTemplateNotUsed(response, "library/book_copy_list.html")
        self.assertIn("HX-Push-Url", response)

    def test_a_background_refresh_does_not_push_history(self):
        response = self.client.get(
            reverse("book_copy_list"),
            {"partial": "results", "refresh": "1"},
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Push-Url", response)

    def test_the_parameter_alone_returns_the_full_page(self):
        response = self.client.get(
            reverse("book_copy_list"), {"partial": "results"}
        )

        self.assertTemplateUsed(response, "library/book_copy_list.html")


class CopyDetailTests(CopyTestCase):

    def setUp(self):
        super().setUp()
        self.copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-700001"
        )

    def modal(self):
        return self.client.get(
            reverse("book_copy_detail", args=[self.copy.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )

    def test_the_dialog_returns_only_the_fragment(self):
        response = self.modal()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response, "library/partials/copy_detail_modal.html"
        )
        self.assertNotContains(response, "<!DOCTYPE html>")

    def test_the_header_alone_returns_the_full_page(self):
        response = self.client.get(
            reverse("book_copy_detail", args=[self.copy.id]),
            headers={"HX-Request": "true"},
        )

        self.assertTemplateUsed(response, "library/book_copy_detail.html")

    def test_the_dialog_shows_where_the_copy_is(self):
        response = self.modal()

        self.assertContains(response, "LIB-700001")
        self.assertContains(response, "Riyad as-Salihin")
        self.assertContains(response, "Main Hall")
        self.assertContains(response, "A-1")
        self.assertContains(response, "Available")

    def test_a_single_volume_book_is_not_labelled_volume_1(self):
        # The volume exists because a copy must hang off one. Naming it
        # would put that plumbing back in front of the librarian.
        response = self.modal()

        self.assertNotContains(response, "Volume 1")

    def test_a_real_volume_is_named_and_linked(self):
        volume = make_volume(book=self.book, volume_number=2, title="Part Two")
        copy = make_copy(
            volume=volume, shelf=self.shelf, copy_code="LIB-700002"
        )

        response = self.client.get(
            reverse("book_copy_detail", args=[copy.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )

        self.assertContains(response, "Volume 2")
        self.assertContains(response, "Part Two")
        self.assertContains(
            response, reverse("book_volume_detail", args=[volume.id])
        )

    def test_the_dialog_shows_the_borrower_and_links_to_the_loan(self):
        borrower = make_borrower(name="Ahmad", phone="0300")
        loan = make_loan(copy=self.copy, borrower=borrower)

        response = self.modal()

        self.assertContains(response, "Ahmad")
        self.assertContains(response, reverse("loan_detail", args=[loan.id]))

    def test_the_dialog_does_not_repeat_the_loan_history(self):
        make_loan(
            copy=self.copy,
            issue_date=date(2019, 1, 1),
            due_date=date(2019, 1, 15),
            return_date=date(2019, 1, 10),
        )

        response = self.modal()

        self.assertNotContains(response, "Loan History")

    def test_the_full_page_does_show_the_loan_history(self):
        make_loan(
            copy=self.copy,
            issue_date=date(2019, 1, 1),
            due_date=date(2019, 1, 15),
            return_date=date(2019, 1, 10),
        )

        response = self.client.get(
            reverse("book_copy_detail", args=[self.copy.id])
        )

        self.assertContains(response, "Loan History")
        self.assertEqual(len(response.context["loan_history"]), 1)

    def test_an_assistant_is_not_offered_actions_it_cannot_take(self):
        self.as_assistant()

        response = self.modal()

        self.assertFalse(response.context["can_edit"])
        self.assertNotContains(
            response, reverse("book_copy_edit", args=[self.copy.id])
        )


class CopyEditTests(CopyTestCase):

    def setUp(self):
        super().setUp()
        self.copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-600001"
        )

    def post(self, **overrides):
        payload = {
            "location": self.location.id,
            "shelf": self.shelf.id,
            "status": "Available",
            "acquisition_date": "",
            "notes": "",
        }
        payload.update(overrides)

        return self.client.post(
            reverse("book_copy_edit", args=[self.copy.id]), payload
        )

    def test_the_form_opens_on_the_copy_s_current_place(self):
        response = self.client.get(
            reverse("book_copy_edit", args=[self.copy.id])
        )

        self.assertEqual(
            response.context["form_data"]["location_id"],
            str(self.location.id),
        )
        self.assertContains(response, "A-1")
        self.assertNotContains(response, "B-1")

    def test_the_copy_code_is_read_only(self):
        response = self.client.get(
            reverse("book_copy_edit", args=[self.copy.id])
        )

        self.assertContains(response, "readonly")

    def test_a_posted_copy_code_is_ignored(self):
        # The code is printed on the book and quoted in its history, so the
        # view does not read it from the form at all.
        self.post(copy_code="TAMPERED")

        self.copy.refresh_from_db()

        self.assertEqual(self.copy.copy_code, "LIB-600001")

    def test_a_posted_volume_is_ignored(self):
        # Re-filing a copy under another book would silently change what a
        # past loan appears to have been for.
        other = make_volume(book=self.book, volume_number=9, title="Nine")

        self.post(volume=other.id)

        self.copy.refresh_from_db()

        self.assertEqual(self.copy.volume_id, self.volume.id)

    def test_moving_the_copy_to_another_shelf(self):
        self.post(
            location=self.other_location.id, shelf=self.other_shelf.id
        )

        self.copy.refresh_from_db()

        self.assertEqual(self.copy.shelf_id, self.other_shelf.id)

    def test_clearing_the_shelf_leaves_the_copy_unshelved(self):
        # A valid state: the column is nullable and the list can find them.
        self.post(location="", shelf="")

        self.copy.refresh_from_db()

        self.assertIsNone(self.copy.shelf_id)

    def test_a_shelf_from_another_location_is_refused(self):
        response = self.post(
            location=self.location.id, shelf=self.other_shelf.id
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not in the location you chose")

        self.copy.refresh_from_db()
        self.assertEqual(self.copy.shelf_id, self.shelf.id)

    def test_editing_a_copy_never_touches_the_book_or_the_volume(self):
        self.post(notes="Spine repaired")

        self.book.refresh_from_db()
        self.volume.refresh_from_db()

        self.assertEqual(self.book.title, "Riyad as-Salihin")
        self.assertEqual(self.book.author_id, self.author.id)
        self.assertEqual(self.volume.volume_number, 1)
        self.assertEqual(self.volume.title, "")

    def test_editing_a_copy_never_touches_another_copy(self):
        sibling = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-600002"
        )

        self.post(location=self.other_location.id, shelf=self.other_shelf.id)

        sibling.refresh_from_db()

        self.assertEqual(sibling.shelf_id, self.shelf.id)
        self.assertEqual(sibling.copy_code, "LIB-600002")

    def test_an_issued_copy_keeps_its_status(self):
        make_loan(copy=self.copy)
        self.copy.refresh_from_db()

        self.post(status="Lost")

        self.copy.refresh_from_db()

        self.assertEqual(self.copy.status, "Issued")

    def test_an_edit_is_logged_against_the_user_who_made_it(self):
        from library.models import ActivityLog

        self.post(notes="Checked")

        log = ActivityLog.objects.filter(
            entity_type="BookCopy", entity_id=self.copy.id
        ).order_by("-id").first()

        self.assertIsNotNone(log.user)
        self.assertEqual(log.user.username, "admin_u")

    def test_an_assistant_cannot_edit_a_copy(self):
        self.as_assistant()

        self.assertEqual(
            self.client.get(
                reverse("book_copy_edit", args=[self.copy.id])
            ).status_code,
            403,
        )


class BulkMoveTests(CopyTestCase):

    def setUp(self):
        super().setUp()

        self.one = make_copy(volume=self.volume, copy_code="LIB-500001")
        self.two = make_copy(volume=self.volume, copy_code="LIB-500002")
        self.three = make_copy(
            volume=self.volume, shelf=self.other_shelf, copy_code="LIB-500003"
        )

    def move(self, ids, location=None, shelf=None, htmx=True, **extra):
        payload = {
            "copy": [str(i) for i in ids],
            "location": location if location is not None else self.location.id,
            "shelf": shelf if shelf is not None else self.shelf.id,
        }
        payload.update(extra)

        headers = {"HX-Request": "true"} if htmx else {}

        return self.client.post(
            reverse("book_copy_bulk_move"), payload, headers=headers
        )

    def test_moving_one_copy(self):
        response = self.move([self.one.id])

        self.assertEqual(response.status_code, 204)
        self.assertIn("copiesMoved", response["HX-Trigger"])

        self.one.refresh_from_db()
        self.assertEqual(self.one.shelf_id, self.shelf.id)

    def test_moving_several_copies_at_once(self):
        response = self.move([self.one.id, self.two.id, self.three.id])

        self.assertEqual(response.status_code, 204)
        self.assertIn('"count": 3', response["HX-Trigger"])

        for copy in (self.one, self.two, self.three):
            copy.refresh_from_db()
            self.assertEqual(copy.shelf_id, self.shelf.id)

    def test_placing_unshelved_copies_on_a_shelf(self):
        self.assertIsNone(self.one.shelf_id)

        self.move([self.one.id, self.two.id])

        self.one.refresh_from_db()
        self.two.refresh_from_db()

        self.assertEqual(self.one.shelf_id, self.shelf.id)
        self.assertEqual(self.two.shelf_id, self.shelf.id)

    def test_copies_already_there_are_not_moved_again(self):
        self.move([self.one.id])

        response = self.move([self.one.id])

        self.assertIn('"count": 0', response["HX-Trigger"])

    def test_a_shelf_from_another_location_is_refused(self):
        response = self.move(
            [self.one.id],
            location=self.location.id,
            shelf=self.other_shelf.id,
        )

        self.assertIn("copiesMoveFailed", response["HX-Trigger"])

        self.one.refresh_from_db()
        self.assertIsNone(self.one.shelf_id)

    def test_no_selection_is_refused(self):
        response = self.move([])

        self.assertIn("copiesMoveFailed", response["HX-Trigger"])

    def test_no_shelf_is_refused(self):
        response = self.move([self.one.id], shelf="")

        self.assertIn("copiesMoveFailed", response["HX-Trigger"])

        self.one.refresh_from_db()
        self.assertIsNone(self.one.shelf_id)

    def test_a_refused_move_changes_nothing_at_all(self):
        before = {
            copy.id: copy.shelf_id
            for copy in BookCopy.objects.all()
        }

        self.move(
            [self.one.id, self.two.id],
            location=self.location.id,
            shelf=self.other_shelf.id,
        )

        after = {
            copy.id: copy.shelf_id
            for copy in BookCopy.objects.all()
        }

        self.assertEqual(before, after)

    def test_a_failure_part_way_through_rolls_the_whole_batch_back(self):
        from unittest import mock

        original = BookCopy.save
        calls = {"n": 0}

        def explode(self, *args, **kwargs):
            calls["n"] += 1

            if calls["n"] == 2:
                raise RuntimeError("database went away")

            return original(self, *args, **kwargs)

        # The client would otherwise re-raise, and the traceback that comes
        # with it cannot be pickled back to the parallel test runner. The
        # 500 is the same event, minus that.
        self.client.raise_request_exception = False

        with mock.patch.object(BookCopy, "save", explode):
            response = self.move([self.one.id, self.two.id, self.three.id])

        self.assertEqual(response.status_code, 500)
        self.assertEqual(calls["n"], 2)

        # The first save did happen — inside the transaction — so it went
        # back with the one that failed. Nothing is half-moved.
        for copy in (self.one, self.two):
            copy.refresh_from_db()
            self.assertIsNone(copy.shelf_id)

        self.three.refresh_from_db()
        self.assertEqual(self.three.shelf_id, self.other_shelf.id)

    def test_a_move_is_logged_per_copy_against_the_user(self):
        from library.models import ActivityLog

        self.move([self.one.id, self.two.id])

        logs = ActivityLog.objects.filter(
            entity_type="BookCopy",
            entity_id__in=[self.one.id, self.two.id],
        )

        self.assertEqual(logs.count(), 2)

        for log in logs:
            self.assertEqual(log.user.username, "admin_u")
            self.assertIn("moved", log.description)

    def test_an_issued_copy_moves_like_any_other(self):
        # The existing single-copy move has always allowed this, and the
        # shelf records where the copy belongs, not where it physically is.
        make_loan(copy=self.three)

        self.move([self.three.id])

        self.three.refresh_from_db()

        self.assertEqual(self.three.shelf_id, self.shelf.id)
        self.assertEqual(self.three.status, "Issued")

    def test_a_move_leaves_loan_history_alone(self):
        loan = make_loan(
            copy=self.three,
            issue_date=date(2021, 1, 1),
            due_date=date(2021, 1, 15),
            return_date=date(2021, 1, 12),
        )

        self.move([self.three.id])

        loan.refresh_from_db()

        self.assertEqual(loan.copy_id, self.three.id)
        self.assertEqual(loan.issue_date, date(2021, 1, 1))
        self.assertEqual(loan.return_date, date(2021, 1, 12))

    def test_a_move_leaves_copy_codes_alone(self):
        codes = sorted(BookCopy.objects.values_list("copy_code", flat=True))

        self.move([self.one.id, self.two.id, self.three.id])

        self.assertEqual(
            sorted(BookCopy.objects.values_list("copy_code", flat=True)),
            codes,
        )

    def test_without_javascript_it_redirects_back_where_it_was(self):
        target = reverse("book_copy_list") + "?status=unshelved&page=1"

        response = self.move(
            [self.one.id], htmx=False, next=target
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], target)

        self.one.refresh_from_db()
        self.assertEqual(self.one.shelf_id, self.shelf.id)

    def test_a_get_does_nothing(self):
        response = self.client.get(reverse("book_copy_bulk_move"))

        self.assertEqual(response.status_code, 400)

    def test_an_assistant_cannot_move_copies(self):
        self.as_assistant()

        response = self.move([self.one.id])

        self.assertEqual(response.status_code, 403)

        self.one.refresh_from_db()
        self.assertIsNone(self.one.shelf_id)

    def test_an_assistant_is_not_shown_the_move_controls(self):
        self.as_assistant()

        response = self.client.get(reverse("book_copy_list"))

        self.assertFalse(response.context["can_edit"])
        self.assertNotContains(response, "copyMoveForm")
        self.assertNotContains(response, "data-copy-checkbox")


class CopyNavigationTests(CopyTestCase):

    def test_a_single_volume_book_lists_its_copies_directly(self):
        make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-400001"
        )

        response = self.client.get(
            reverse("book_detail", args=[self.book.id])
        )

        self.assertIsNotNone(response.context["single_volume"])
        self.assertEqual(len(response.context["volume_copies"]), 1)

        # No hop through a volume page, and no mention of the volume that
        # only exists to hang the copy off.
        self.assertContains(response, "LIB-400001")
        self.assertContains(response, "single volume")

    def test_a_multi_volume_book_shows_a_copy_count_per_volume(self):
        second = make_volume(book=self.book, volume_number=2, title="Two")
        third = make_volume(book=self.book, volume_number=3, title="Three")

        for index in range(3):
            make_copy(
                volume=self.volume, shelf=self.shelf,
                copy_code="LIB-3000%d" % index,
            )
        for index in range(2):
            make_copy(
                volume=second, shelf=self.shelf,
                copy_code="LIB-3100%d" % index,
            )

        response = self.client.get(
            reverse("book_detail", args=[self.book.id])
        )

        counts = {
            volume.volume_number: volume.copy_count
            for volume in response.context["volumes"]
        }

        self.assertEqual(counts, {1: 3, 2: 2, 3: 0})
        self.assertIsNone(response.context["single_volume"])

        # And the count is a way through to those copies.
        self.assertContains(
            response, reverse("book_copy_list") + "?volume=%d" % third.id
        )

    def test_the_counts_cost_one_query_however_many_volumes(self):
        for number in range(2, 12):
            make_volume(book=self.book, volume_number=number, title="V")

        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("book_detail", args=[self.book.id]))

        for number in range(12, 32):
            make_volume(book=self.book, volume_number=number, title="V")

        with CaptureQueriesContext(connection) as many:
            self.client.get(reverse("book_detail", args=[self.book.id]))

        self.assertEqual(len(many), len(few))

    def test_a_volume_page_describes_its_copies(self):
        copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-200001"
        )
        make_loan(copy=copy)

        response = self.client.get(
            reverse("book_volume_detail", args=[self.volume.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "library/base.html")
        self.assertContains(response, "LIB-200001")
        self.assertContains(response, "Issued")
        self.assertContains(
            response, reverse("book_copy_list") + "?volume=%d" % self.volume.id
        )


class CopyDeletionTests(CopyTestCase):
    """The existing protection, confirmed still in force.

    Nothing in this task adds a way to delete a copy; these guard the rule
    that was already there, because a copy is what loan history hangs off.
    """

    def test_a_copy_with_loan_history_still_cannot_be_deleted(self):
        copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-100001"
        )
        make_loan(
            copy=copy,
            issue_date=date(2020, 1, 1),
            due_date=date(2020, 1, 15),
            return_date=date(2020, 1, 10),
        )

        response = self.client.post(
            reverse("book_copy_delete", args=[copy.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(BookCopy.objects.filter(id=copy.id).exists())
        self.assertEqual(Loan.objects.filter(copy_id=copy.id).count(), 1)

    def test_a_copy_that_was_never_issued_can_still_be_deleted(self):
        copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="LIB-100002"
        )

        response = self.client.post(
            reverse("book_copy_delete", args=[copy.id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(BookCopy.objects.filter(id=copy.id).exists())
