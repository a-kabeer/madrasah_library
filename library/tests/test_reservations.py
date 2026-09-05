"""Reservations: a queue on a book, and what circulation does about it.

Two properties carry most of this. A reservation holds no particular copy
- none is set aside and a return is never assigned - but it does decide
who the next copy goes to: while somebody is waiting, issuing that book to
anybody else is refused, and refused on the server rather than by a hidden
button. And the queue is ordered by when people asked, deterministically,
so it reads the same on every page load.

The rules that could be raced - one place per borrower per queue, and a
reservation ending exactly once - are held by the database, so the tests
that matter poke at them directly rather than only through the views.
"""

from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library import reservations as queue
from library.models import ActivityLog, Loan, Reservation

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


class ReservationTestCase(TestCase):

    def setUp(self):
        self.librarian = make_user(
            username="librarian_u", password="pass12345", role="Librarian"
        )
        self.client.login(username="librarian_u", password="pass12345")

        self.shelf = make_shelf(location=make_location(name="Main Hall"))

        self.author = make_author("Abu Yusuf")
        self.book = make_book(title="Kitab al-Kharaj", author=self.author)
        self.volume = make_volume(book=self.book, volume_number=1, title="")

        self.hafsa = make_borrower(name="Hafsa", phone="0388-1")
        self.bilal = make_borrower(name="Bilal", phone="0388-2")
        self.omar = make_borrower(name="Omar", phone="0388-3")

        self.today = timezone.now().date()

    def a_copy(self, code, status="Available", volume=None):
        return make_copy(
            volume=volume or self.volume,
            shelf=self.shelf,
            copy_code=code,
            status=status,
        )

    def reserve(self, borrower, book=None, **extra):
        """Join the queue the way the book page does."""

        payload = {
            "book": (book or self.book).id,
            "borrower": borrower.id,
        }
        payload.update(extra)

        return self.client.post(
            reverse("reservation_add"), payload, follow=True
        )

    def queued(self, book=None):
        return [
            (r.position, r.borrower.name)
            for r in queue.active_for_book(book or self.book)
        ]

    def issue(self, copies, borrower, **extra):
        payload = {
            "borrower": borrower.id,
            "copies": [copy.id for copy in copies],
            "issue_date": self.today.isoformat(),
            "due_date": (self.today + timedelta(days=7)).isoformat(),
        }
        payload.update(extra)

        return self.client.post(reverse("circulation_issue"), payload)

    def as_role(self, role):
        self.client.logout()
        make_user(username="v_%s" % role, password="pass12345", role=role)
        self.client.login(username="v_%s" % role, password="pass12345")


class CreatingTests(ReservationTestCase):

    def test_a_borrower_joins_the_queue(self):
        response = self.reserve(self.hafsa)

        self.assertEqual(response.status_code, 200)

        reservation = Reservation.objects.get()

        self.assertEqual(reservation.borrower, self.hafsa)
        self.assertEqual(reservation.book, self.book)
        self.assertTrue(reservation.is_active)
        self.assertIsNone(reservation.closed_at)

    def test_it_says_so(self):
        self.assertContains(self.reserve(self.hafsa), "is in the queue for")

    def test_the_same_borrower_cannot_queue_twice(self):
        self.reserve(self.hafsa)

        response = self.reserve(self.hafsa)

        self.assertContains(response, "already waiting")
        self.assertEqual(Reservation.objects.count(), 1)

    def test_the_database_is_what_refuses_the_second_one(self):
        # Not a check in Python, which two requests could both pass.
        queue.reserve(self.book, self.hafsa)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Reservation.objects.create(
                    book=self.book,
                    borrower=self.hafsa,
                    status=Reservation.STATUS_ACTIVE,
                    created_at=timezone.now(),
                )

    def test_two_simultaneous_requests_give_one_reservation(self):
        first, first_error = queue.reserve(self.book, self.hafsa)
        second, second_error = queue.reserve(self.book, self.hafsa)

        self.assertIsNotNone(first)
        self.assertEqual(first_error, "")
        self.assertIsNone(second)
        self.assertIn("already waiting", second_error)
        self.assertEqual(Reservation.objects.count(), 1)

    def test_a_borrower_may_queue_again_after_being_served(self):
        # The unique index is partial, over the active rows only.
        reservation, _ = queue.reserve(self.book, self.hafsa)
        queue.close(reservation, Reservation.STATUS_FULFILLED)

        again, error = queue.reserve(self.book, self.hafsa)

        self.assertIsNotNone(again)
        self.assertEqual(error, "")

    def test_different_borrowers_may_queue_for_the_same_book(self):
        self.reserve(self.hafsa)
        self.reserve(self.bilal)

        self.assertEqual(Reservation.objects.count(), 2)

    def test_one_borrower_may_queue_for_different_books(self):
        other = make_book(title="Al-Muwatta", author=make_author("Malik"))

        self.reserve(self.hafsa)
        self.reserve(self.hafsa, book=other)

        self.assertEqual(Reservation.objects.count(), 2)

    def test_reserving_is_allowed_when_copies_are_on_the_shelf(self):
        # Wanting to be next in line for a book that is in today is a
        # perfectly ordinary thing to want.
        self.a_copy("RES-0001")

        self.reserve(self.hafsa)

        self.assertEqual(Reservation.objects.count(), 1)

    def test_but_the_page_says_the_book_is_available(self):
        self.a_copy("RES-0002")

        response = self.client.get(
            reverse("book_detail", args=[self.book.id]),
            {"tab": "reservations"},
        )

        self.assertContains(response, "can be issued right now")

    def test_reserving_is_allowed_when_everything_is_out(self):
        copy = self.a_copy("RES-0003", status="Issued")
        make_loan(copy=copy, borrower=self.bilal)

        self.reserve(self.hafsa)

        self.assertEqual(Reservation.objects.count(), 1)

    def test_an_archived_book_cannot_be_reserved(self):
        self.book.archived_at = timezone.now()
        self.book.save(update_fields=["archived_at"])

        response = self.reserve(self.hafsa)

        self.assertContains(response, "has been archived")
        self.assertFalse(Reservation.objects.exists())

    def test_an_inactive_borrower_cannot_reserve(self):
        suspended = make_borrower(
            name="Suspended", phone="0388-9", is_active=False
        )

        response = self.reserve(suspended)

        self.assertContains(response, "is inactive")
        self.assertFalse(Reservation.objects.exists())

    def test_no_borrower_is_refused_cleanly(self):
        response = self.client.post(
            reverse("reservation_add"),
            {"book": self.book.id, "borrower": ""},
            follow=True,
        )

        self.assertContains(response, "Choose who is waiting")
        self.assertFalse(Reservation.objects.exists())

    def test_a_get_writes_nothing(self):
        self.client.get(reverse("reservation_add"))

        self.assertFalse(Reservation.objects.exists())


class QueueOrderTests(ReservationTestCase):

    def join(self, borrower, minutes_ago):
        reservation, _ = queue.reserve(self.book, borrower)

        Reservation.objects.filter(id=reservation.id).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago)
        )

        return reservation

    def test_the_queue_is_oldest_first(self):
        self.join(self.omar, 5)
        self.join(self.hafsa, 30)
        self.join(self.bilal, 20)

        self.assertEqual(
            self.queued(),
            [(1, "Hafsa"), (2, "Bilal"), (3, "Omar")],
        )

    def test_positions_start_at_one(self):
        self.join(self.hafsa, 10)

        self.assertEqual(self.queued(), [(1, "Hafsa")])

    def test_the_front_is_the_oldest(self):
        self.join(self.bilal, 5)
        self.join(self.hafsa, 50)

        self.assertEqual(queue.queue_front(self.book).borrower, self.hafsa)

    def test_ties_are_broken_deterministically(self):
        # Same instant, so only the tiebreak decides - and it must decide
        # the same way every read, or the queue reorders between two page
        # loads.
        moment = timezone.now()

        for borrower in (self.hafsa, self.bilal, self.omar):
            reservation, _ = queue.reserve(self.book, borrower)
            Reservation.objects.filter(id=reservation.id).update(
                created_at=moment
            )

        first = self.queued()
        second = self.queued()

        self.assertEqual(first, second)
        self.assertEqual([name for _, name in first],
                         ["Hafsa", "Bilal", "Omar"])

    def test_a_cancelled_reservation_leaves_no_gap(self):
        self.join(self.hafsa, 30)
        middle = self.join(self.bilal, 20)
        self.join(self.omar, 10)

        queue.close(middle, Reservation.STATUS_CANCELLED)

        self.assertEqual(self.queued(), [(1, "Hafsa"), (2, "Omar")])

    def test_a_fulfilled_reservation_leaves_no_gap_either(self):
        front = self.join(self.hafsa, 30)
        self.join(self.bilal, 20)

        queue.close(front, Reservation.STATUS_FULFILLED)

        self.assertEqual(self.queued(), [(1, "Bilal")])

    def test_the_count_is_the_active_ones(self):
        self.join(self.hafsa, 30)
        cancelled = self.join(self.bilal, 20)

        queue.close(cancelled, Reservation.STATUS_CANCELLED)

        self.assertEqual(queue.active_count(self.book), 1)

    def test_another_books_queue_is_separate(self):
        other = make_book(title="Al-Muwatta", author=make_author("Malik"))

        self.join(self.hafsa, 30)
        queue.reserve(other, self.bilal)

        self.assertEqual(self.queued(), [(1, "Hafsa")])
        self.assertEqual(self.queued(other), [(1, "Bilal")])

    def test_an_empty_queue_has_no_front(self):
        self.assertIsNone(queue.queue_front(self.book))


class CancellingTests(ReservationTestCase):

    def setUp(self):
        super().setUp()
        self.reservation, _ = queue.reserve(self.book, self.hafsa)

    def cancel(self, reservation=None):
        return self.client.post(
            reverse(
                "reservation_cancel",
                args=[(reservation or self.reservation).id],
            ),
            follow=True,
        )

    def test_it_closes_the_reservation(self):
        self.cancel()

        self.reservation.refresh_from_db()

        self.assertEqual(
            self.reservation.status, Reservation.STATUS_CANCELLED
        )
        self.assertIsNotNone(self.reservation.closed_at)

    def test_it_leaves_the_queue(self):
        self.cancel()

        self.assertEqual(self.queued(), [])
        self.assertEqual(queue.active_count(self.book), 0)

    def test_cancelling_twice_is_safe(self):
        self.cancel()
        self.reservation.refresh_from_db()
        first = self.reservation.closed_at

        response = self.cancel()

        self.reservation.refresh_from_db()

        self.assertEqual(self.reservation.closed_at, first)
        self.assertContains(response, "already been closed")

    def test_a_get_cancels_nothing(self):
        self.client.get(
            reverse("reservation_cancel", args=[self.reservation.id])
        )

        self.reservation.refresh_from_db()
        self.assertTrue(self.reservation.is_active)

    def test_a_fulfilled_one_cannot_be_cancelled_after_the_fact(self):
        queue.close(self.reservation, Reservation.STATUS_FULFILLED)

        self.cancel()

        self.reservation.refresh_from_db()
        self.assertEqual(
            self.reservation.status, Reservation.STATUS_FULFILLED
        )

    def test_the_database_refuses_an_active_row_with_a_closing_time(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Reservation.objects.filter(id=self.reservation.id).update(
                    closed_at=timezone.now()
                )

    def test_the_database_refuses_a_closed_row_with_no_time(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Reservation.objects.filter(id=self.reservation.id).update(
                    status=Reservation.STATUS_CANCELLED
                )


class IssueIntegrationTests(ReservationTestCase):

    def setUp(self):
        super().setUp()

        self.copy = self.a_copy("RES-1001")

        self.front, _ = queue.reserve(self.book, self.hafsa)
        Reservation.objects.filter(id=self.front.id).update(
            created_at=timezone.now() - timedelta(hours=2)
        )

        self.behind, _ = queue.reserve(self.book, self.bilal)

    def test_issuing_to_the_front_fulfils_their_reservation(self):
        response = self.issue([self.copy], self.hafsa)

        self.assertEqual(response.status_code, 302)

        self.front.refresh_from_db()

        self.assertEqual(self.front.status, Reservation.STATUS_FULFILLED)
        self.assertIsNotNone(self.front.closed_at)

    def test_and_only_theirs(self):
        self.issue([self.copy], self.hafsa)

        self.behind.refresh_from_db()

        self.assertTrue(self.behind.is_active)

    def test_someone_behind_the_front_is_refused(self):
        response = self.issue([self.copy], self.bilal)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "first in the queue")
        self.assertFalse(
            Loan.objects.filter(copy=self.copy).exists()
        )

    def test_a_refused_issue_leaves_both_reservations_alone(self):
        self.issue([self.copy], self.bilal)

        self.front.refresh_from_db()
        self.behind.refresh_from_db()

        self.assertTrue(self.front.is_active)
        self.assertTrue(self.behind.is_active)
        self.assertEqual(queue.queue_front(self.book).borrower, self.hafsa)

    def test_someone_not_in_the_queue_at_all_is_refused_too(self):
        # The rule is about the front of the queue, not about membership.
        response = self.issue([self.copy], self.omar)

        self.assertContains(response, "first in the queue")
        self.assertFalse(Loan.objects.filter(copy=self.copy).exists())

    def test_the_refusal_names_who_is_waiting(self):
        self.assertContains(self.issue([self.copy], self.omar), "Hafsa")

    def test_a_direct_post_is_refused_the_same_way(self):
        # No GET first, no form state: the rule is on the server, not on
        # the button.
        response = self.client.post(
            reverse("circulation_issue"),
            {
                "borrower": self.omar.id,
                "copies": [self.copy.id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "first in the queue")
        self.assertFalse(Loan.objects.exists())

    def test_one_blocked_book_refuses_the_whole_basket(self):
        # All or nothing, like every other refusal on this form.
        free = make_book(title="Al-Muwatta", author=make_author("Malik"))
        free_volume = make_volume(book=free, volume_number=1, title="")
        spare = self.a_copy("RES-1010", volume=free_volume)

        self.issue([spare, self.copy], self.omar)

        self.assertFalse(Loan.objects.exists())

    def test_a_book_nobody_is_waiting_for_is_unaffected(self):
        free = make_book(title="Al-Muwatta", author=make_author("Malik"))
        free_volume = make_volume(book=free, volume_number=1, title="")
        spare = self.a_copy("RES-1011", volume=free_volume)

        response = self.issue([spare], self.omar)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Loan.objects.filter(copy=spare).exists())

    def test_the_form_says_so_before_it_is_tried(self):
        response = self.client.get(
            reverse("circulation_issue"),
            {"copies": [self.copy.id], "borrower": self.bilal.id},
        )

        self.assertContains(response, "first in the queue")
        self.assertContains(response, "Hafsa")
        self.assertEqual(len(response.context["queue_blocks"]), 1)

    def test_and_the_issue_button_is_disabled(self):
        response = self.client.get(
            reverse("circulation_issue"),
            {"copies": [self.copy.id], "borrower": self.bilal.id},
        )

        self.assertContains(response, "disabled")

    def test_it_does_not_block_when_the_front_is_the_one_being_served(self):
        response = self.client.get(
            reverse("circulation_issue"),
            {"copies": [self.copy.id], "borrower": self.hafsa.id},
        )

        self.assertEqual(list(response.context["queue_blocks"]), [])

    def test_serving_the_front_lets_the_next_one_through(self):
        # The queue advances: Bilal was refused a moment ago and is now
        # first, so the same request succeeds.
        self.issue([self.copy], self.hafsa)

        self.assertEqual(queue.queue_front(self.book).borrower, self.bilal)

        second = self.a_copy("RES-1020")
        response = self.issue([second], self.bilal)

        self.assertEqual(response.status_code, 302)

        self.behind.refresh_from_db()
        self.assertEqual(self.behind.status, Reservation.STATUS_FULFILLED)

    def test_cancelling_the_front_lets_the_next_one_through(self):
        queue.close(self.front, Reservation.STATUS_CANCELLED)

        self.assertEqual(queue.queue_front(self.book).borrower, self.bilal)

        response = self.issue([self.copy], self.bilal)

        self.assertEqual(response.status_code, 302)

    def test_a_cancelled_reservation_blocks_nobody(self):
        queue.close(self.front, Reservation.STATUS_CANCELLED)
        queue.close(self.behind, Reservation.STATUS_CANCELLED)

        response = self.issue([self.copy], self.omar)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Loan.objects.filter(borrower=self.omar).exists())

    def test_a_fulfilled_reservation_blocks_nobody_either(self):
        queue.close(self.front, Reservation.STATUS_FULFILLED)
        queue.close(self.behind, Reservation.STATUS_FULFILLED)

        self.assertEqual(
            self.issue([self.copy], self.omar).status_code, 302
        )

    def test_an_unreserved_book_is_issued_to_anybody(self):
        free = make_book(title="Unwanted", author=make_author("Nobody"))
        free_volume = make_volume(book=free, volume_number=1, title="")
        spare = self.a_copy("RES-1030", volume=free_volume)

        self.assertEqual(
            queue.refuse_issue(queue.queue_front(free), self.omar.id), ""
        )
        self.assertEqual(self.issue([spare], self.omar).status_code, 302)

    def test_nothing_is_fulfilled_when_the_issue_is_refused(self):
        # A refused issue rolls back, and the reservation goes with it.
        withdrawn = self.a_copy("RES-1002", status="Transferred")

        self.issue([withdrawn], self.hafsa)

        self.front.refresh_from_db()

        self.assertTrue(self.front.is_active)

    def test_a_second_issue_cannot_fulfil_it_again(self):
        self.issue([self.copy], self.hafsa)

        self.front.refresh_from_db()
        closed = self.front.closed_at

        second = self.a_copy("RES-1003")
        self.issue([second], self.hafsa)

        self.front.refresh_from_db()

        self.assertEqual(self.front.closed_at, closed)
        self.assertEqual(
            Reservation.objects.filter(
                status=Reservation.STATUS_FULFILLED
            ).count(),
            1,
        )


class ReturnIntegrationTests(ReservationTestCase):

    def setUp(self):
        super().setUp()

        self.copy = self.a_copy("RES-2001", status="Issued")
        self.loan = make_loan(copy=self.copy, borrower=self.omar)

        self.waiting, _ = queue.reserve(self.book, self.hafsa)

    def test_the_return_page_says_someone_is_waiting(self):
        response = self.client.get(
            reverse("loan_return", args=[self.loan.id])
        )

        self.assertContains(response, "waiting for this book")
        self.assertContains(response, "Hafsa")

    def test_it_says_the_copy_is_not_being_set_aside(self):
        response = self.client.get(
            reverse("loan_return", args=[self.loan.id])
        )

        self.assertContains(response, "not being set aside")

    def test_the_returned_copy_is_simply_available(self):
        self.client.post(
            reverse("loan_return", args=[self.loan.id]),
            {"return_date": self.today.isoformat()},
        )

        self.copy.refresh_from_db()

        self.assertEqual(self.copy.status, "Available")

    def test_the_reservation_is_untouched_by_the_return(self):
        self.client.post(
            reverse("loan_return", args=[self.loan.id]),
            {"return_date": self.today.isoformat()},
        )

        self.waiting.refresh_from_db()

        self.assertTrue(self.waiting.is_active)

    def test_nothing_is_assigned_to_anybody(self):
        self.client.post(
            reverse("loan_return", args=[self.loan.id]),
            {"return_date": self.today.isoformat()},
        )

        # No new loan, and the copy is on the shelf for whoever comes in.
        self.assertEqual(
            Loan.objects.filter(copy=self.copy, return_date__isnull=True)
            .count(),
            0,
        )

    def test_a_book_nobody_wants_says_nothing(self):
        queue.close(self.waiting, Reservation.STATUS_CANCELLED)

        response = self.client.get(
            reverse("loan_return", args=[self.loan.id])
        )

        self.assertNotContains(response, "waiting for this book")

    def test_the_return_itself_is_unchanged(self):
        response = self.client.post(
            reverse("loan_return", args=[self.loan.id]),
            {"return_date": self.today.isoformat()},
        )

        self.assertEqual(response.status_code, 302)

        self.loan.refresh_from_db()

        self.assertEqual(self.loan.return_date, self.today)
        self.assertEqual(self.loan.returned_to, self.librarian)


class DisplayTests(ReservationTestCase):

    def test_the_book_page_shows_the_count_and_the_queue(self):
        queue.reserve(self.book, self.hafsa)
        queue.reserve(self.book, self.bilal)

        response = self.client.get(
            reverse("book_detail", args=[self.book.id]),
            {"tab": "reservations"},
        )

        self.assertEqual(response.context["reservation_count"], 2)
        self.assertContains(response, "Hafsa")
        self.assertContains(response, "Bilal")
        self.assertContains(response, "Waiting, in order")

    def test_the_book_page_offers_a_way_to_join(self):
        response = self.client.get(
            reverse("book_detail", args=[self.book.id]),
            {"tab": "reservations"},
        )

        self.assertContains(response, reverse("reservation_add"))

    def test_the_borrower_page_shows_what_they_wait_for(self):
        queue.reserve(self.book, self.hafsa)

        response = self.client.get(
            reverse("borrower_detail", args=[self.hafsa.id])
        )

        self.assertContains(response, "Reservations")
        self.assertContains(response, "Kitab al-Kharaj")
        self.assertContains(response, "Next in line")

    def test_it_shows_their_place_in_the_queue(self):
        first, _ = queue.reserve(self.book, self.hafsa)
        Reservation.objects.filter(id=first.id).update(
            created_at=timezone.now() - timedelta(hours=1)
        )
        queue.reserve(self.book, self.bilal)

        response = self.client.get(
            reverse("borrower_detail", args=[self.bilal.id])
        )

        theirs = response.context["reservations"][0]

        self.assertEqual(theirs.ahead, 1)

    def test_a_borrower_waiting_for_nothing_says_so(self):
        response = self.client.get(
            reverse("borrower_detail", args=[self.omar.id])
        )

        self.assertContains(response, "not waiting for anything")

    def test_the_borrower_page_offers_cancellation(self):
        reservation, _ = queue.reserve(self.book, self.hafsa)

        response = self.client.get(
            reverse("borrower_detail", args=[self.hafsa.id])
        )

        self.assertContains(
            response, reverse("reservation_cancel", args=[reservation.id])
        )

    def test_the_list_page_shows_everyone_waiting(self):
        queue.reserve(self.book, self.hafsa)

        response = self.client.get(reverse("reservation_list"))

        self.assertContains(response, "Hafsa")
        self.assertContains(response, "Kitab al-Kharaj")
        self.assertEqual(response.context["active_total"], 1)

    def test_the_list_can_show_the_closed_ones(self):
        reservation, _ = queue.reserve(self.book, self.hafsa)
        queue.close(reservation, Reservation.STATUS_CANCELLED)

        active = self.client.get(reverse("reservation_list"))
        cancelled = self.client.get(
            reverse("reservation_list"), {"status": "Cancelled"}
        )

        self.assertEqual(list(active.context["reservations"]), [])
        self.assertEqual(len(cancelled.context["reservations"]), 1)


class LoggingTests(ReservationTestCase):

    def entries(self, action):
        return ActivityLog.objects.filter(action=action)

    def test_creating_is_logged(self):
        self.reserve(self.hafsa)

        log = self.entries("RESERVE").get()

        self.assertEqual(log.entity_type, "Reservation")
        self.assertEqual(log.entity_id, self.book.id)
        self.assertIn("Hafsa", log.description)
        self.assertEqual(log.user, self.librarian)

    def test_cancelling_is_logged(self):
        reservation, _ = queue.reserve(self.book, self.hafsa)

        self.client.post(
            reverse("reservation_cancel", args=[reservation.id])
        )

        self.assertIn("cancelled", self.entries("CANCEL").get().description)

    def test_fulfilling_is_logged(self):
        queue.reserve(self.book, self.hafsa)

        self.issue([self.a_copy("RES-3001")], self.hafsa)

        log = self.entries("FULFIL").get()

        self.assertIn("Hafsa", log.description)
        self.assertIn("fulfilled", log.description)

    def test_a_cancel_that_did_nothing_is_not_logged(self):
        reservation, _ = queue.reserve(self.book, self.hafsa)
        queue.close(reservation, Reservation.STATUS_CANCELLED)

        self.client.post(
            reverse("reservation_cancel", args=[reservation.id])
        )

        self.assertEqual(self.entries("CANCEL").count(), 0)

    def test_it_reuses_the_existing_log(self):
        # No second history table anywhere.
        self.reserve(self.hafsa)

        self.assertTrue(
            ActivityLog.objects.filter(entity_type="Reservation").exists()
        )


class PermissionTests(ReservationTestCase):

    def test_every_circulation_role_may_manage_a_queue(self):
        # Reservations are a desk job, and all three roles work the desk:
        # each can already issue and return.
        for role in ("Admin", "Librarian", "Assistant"):
            with self.subTest(role=role):
                self.as_role(role)

                self.assertEqual(
                    self.client.get(reverse("reservation_list")).status_code,
                    200,
                )

                borrower = make_borrower(
                    name="P %s" % role, phone="0399-%s" % role[0]
                )

                self.reserve(borrower)

                self.assertTrue(
                    Reservation.objects.filter(borrower=borrower).exists()
                )

    def test_every_role_may_cancel(self):
        reservation, _ = queue.reserve(self.book, self.hafsa)

        self.as_role("Assistant")

        self.client.post(
            reverse("reservation_cancel", args=[reservation.id])
        )

        reservation.refresh_from_db()
        self.assertFalse(reservation.is_active)

    def test_a_signed_out_visitor_can_do_none_of_it(self):
        reservation, _ = queue.reserve(self.book, self.hafsa)

        self.client.logout()

        for name, args in (
            ("reservation_list", []),
            ("reservation_cancel", [reservation.id]),
        ):
            with self.subTest(view=name):
                response = self.client.get(reverse(name, args=args))

                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])

    def test_a_signed_out_post_reserves_nothing(self):
        self.client.logout()

        self.client.post(
            reverse("reservation_add"),
            {"book": self.book.id, "borrower": self.hafsa.id},
        )

        self.assertFalse(Reservation.objects.exists())

    def test_the_restricted_parts_of_circulation_stay_restricted(self):
        copy = self.a_copy("RES-4001", status="Issued")
        loan = make_loan(copy=copy, borrower=self.omar)

        self.as_role("Assistant")

        for name in ("loan_renew", "loan_delete"):
            with self.subTest(view=name):
                self.assertEqual(
                    self.client.get(
                        reverse(name, args=[loan.id])
                    ).status_code,
                    403,
                )


class NavigationTests(ReservationTestCase):

    def sidebar(self):
        body = self.client.get(reverse("dashboard")).content.decode()
        start = body.index('class="sidebar-nav"')

        return body[start:body.index("</nav>", start)]

    def test_reservations_is_under_circulation(self):
        nav = self.sidebar()

        circulation = nav.index("CIRCULATION")
        catalog = nav.index("CATALOG")
        entry = nav.index(reverse("reservation_list"))

        self.assertLess(circulation, entry)
        self.assertLess(entry, catalog)

    def test_book_copies_is_under_inventory(self):
        nav = self.sidebar()

        inventory = nav.index("INVENTORY")
        administration = nav.index("ADMINISTRATION")
        entry = nav.index(reverse("book_copy_list"))

        self.assertLess(inventory, entry)
        self.assertLess(entry, administration)

    def test_both_are_plain_links_so_the_shell_boosts_them(self):
        nav = self.sidebar()

        import re

        for name in ("reservation_list", "book_copy_list"):
            with self.subTest(entry=name):
                link = re.search(
                    r'<a[^>]*href="%s"[^>]*>' % reverse(name), nav, re.S
                )

                self.assertIsNotNone(link)
                self.assertNotIn("hx-get", link.group(0))

    def test_the_reservations_page_answers_a_navigation_with_a_fragment(self):
        response = self.client.get(
            reverse("reservation_list"),
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="mainContent",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<!DOCTYPE html>", response.content.decode())


class QueryTests(ReservationTestCase):

    def test_the_queue_does_not_cost_a_query_per_place(self):
        for number in range(3):
            queue.reserve(
                self.book,
                make_borrower(name="P%d" % number, phone="0344-%d" % number),
            )

        with CaptureQueriesContext(connection) as few:
            self.client.get(
                reverse("book_detail", args=[self.book.id]),
                {"tab": "reservations"},
            )

        for number in range(3, 15):
            queue.reserve(
                self.book,
                make_borrower(name="P%d" % number, phone="0344-%d" % number),
            )

        with CaptureQueriesContext(connection) as many:
            response = self.client.get(
                reverse("book_detail", args=[self.book.id]),
                {"tab": "reservations"},
            )

        self.assertEqual(response.context["reservation_count"], 15)
        self.assertEqual(len(few), len(many))

    def test_the_positions_are_worked_out_in_one_query(self):
        for number in range(6):
            queue.reserve(
                self.book,
                make_borrower(name="Q%d" % number, phone="0345-%d" % number),
            )

        with CaptureQueriesContext(connection) as queries:
            listed = list(queue.active_for_book(self.book))

        self.assertEqual([r.position for r in listed], [1, 2, 3, 4, 5, 6])
        self.assertEqual(len(queries), 1)

    def test_the_borrower_page_does_not_grow_with_their_reservations(self):
        queue.reserve(self.book, self.hafsa)

        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("borrower_detail", args=[self.hafsa.id]))

        for number in range(10):
            book = make_book(
                title="B%d" % number, author=make_author("A%d" % number)
            )
            queue.reserve(book, self.hafsa)

        with CaptureQueriesContext(connection) as many:
            response = self.client.get(
                reverse("borrower_detail", args=[self.hafsa.id])
            )

        self.assertEqual(len(response.context["reservations"]), 11)
        self.assertEqual(len(few), len(many))

    def test_the_issue_form_reads_the_queues_in_one_query(self):
        first = self.a_copy("RES-5001")
        second = self.a_copy("RES-5002")

        queue.reserve(self.book, self.hafsa)

        with CaptureQueriesContext(connection) as queries:
            self.client.get(
                reverse("circulation_issue"),
                {"copies": [first.id, second.id]},
            )

        self.assertEqual(
            len([
                q for q in queries.captured_queries
                if 'FROM "reservations"' in q["sql"]
            ]),
            1,
        )

    def test_the_list_page_does_not_query_per_row(self):
        queue.reserve(self.book, self.hafsa)

        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("reservation_list"))

        for number in range(10):
            book = make_book(
                title="L%d" % number, author=make_author("LA%d" % number)
            )
            queue.reserve(book, self.hafsa)

        with CaptureQueriesContext(connection) as many:
            response = self.client.get(reverse("reservation_list"))

        self.assertEqual(len(response.context["reservations"]), 11)
        self.assertEqual(len(few), len(many))
