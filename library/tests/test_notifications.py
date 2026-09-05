"""In-app notifications: what is sent, to whom, and once.

Three properties carry most of this.

A notification is written at an authoritative state transition and nowhere
else. Nothing is created by looking at a page, and drawing the same page
twice creates nothing at all - so the tests below check both halves: that
the transition sends one, and that everything around it sends none.

It is sent exactly once. `unique_notification_event` over
(recipient_id, event_key) is a unique index in the database, not a check in
Python, so the tests that matter about duplication poke at it directly as
well as through the views: a repeated POST, a queue that advances twice, two
returns landing together.

And it belongs to one person. Every view here starts its query from
`request.user`, so a forged id is indistinguishable from a missing one, and
mark-all-read cannot reach across accounts. That is checked by making two
accounts and trying.
"""

from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library import notifications as notify
from library import reservations as queue
from library.models import (
    ActivityLog,
    InventorySession,
    Notification,
    Reservation,
)

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


class NotificationTestCase(TestCase):
    """One librarian at the desk, one book, three borrowers waiting."""

    def setUp(self):
        self.librarian = make_user(
            username="librarian_n", password="pass12345", role="Librarian"
        )

        self.login_librarian()

        self.location = make_location(name="Main Hall")
        self.shelf = make_shelf(location=self.location, shelf_code="N1")

        self.author = make_author("Ibn Kathir")
        self.book = make_book(title="Al-Bidaya", author=self.author)
        self.volume = make_volume(book=self.book, volume_number=1, title="")

        self.hafsa = make_borrower(name="Hafsa", phone="0399-1")
        self.bilal = make_borrower(name="Bilal", phone="0399-2")
        self.omar = make_borrower(name="Omar", phone="0399-3")

        self.today = timezone.now().date()

    def login_librarian(self):
        self.client.login(username="librarian_n", password="pass12345")

    def a_copy(self, code, status="Available", volume=None):
        return make_copy(
            volume=volume or self.volume,
            shelf=self.shelf,
            copy_code=code,
            status=status,
        )

    def ready_notifications(self, recipient=None):
        rows = Notification.objects.filter(
            event_type=Notification.EVENT_RESERVATION_READY
        )

        if recipient is not None:
            rows = rows.filter(recipient=recipient)

        return rows

    def return_the_loan(self, loan, when=None):
        return self.client.post(
            reverse("loan_return", args=[loan.id]),
            {"return_date": (when or self.today).isoformat()},
        )


# ==========================================================================
# CREATION AND IDEMPOTENCY
# ==========================================================================


class CreationTests(NotificationTestCase):
    """Written at the transition, and only at the transition."""

    def setUp(self):
        super().setUp()

        self.copy = self.a_copy("NOT-1001", status="Issued")
        self.loan = make_loan(copy=self.copy, borrower=self.omar)

        self.waiting, _ = queue.reserve(self.book, self.hafsa)

    def test_nothing_before_the_return(self):
        # The reservation itself is not news: the book is out, and there is
        # nothing anybody at the desk can do about that.
        self.assertEqual(self.ready_notifications().count(), 0)

    def test_looking_at_the_return_page_creates_nothing(self):
        self.client.get(reverse("loan_return", args=[self.loan.id]))

        self.assertEqual(self.ready_notifications().count(), 0)

    def test_looking_at_the_book_page_creates_nothing(self):
        self.client.get(
            reverse("book_detail", args=[self.book.id]),
            {"tab": "reservations"},
        )

        self.assertEqual(self.ready_notifications().count(), 0)

    def test_looking_at_the_reservation_list_creates_nothing(self):
        self.client.get(reverse("reservation_list"))

        self.assertEqual(self.ready_notifications().count(), 0)

    def test_the_return_sends_one(self):
        self.return_the_loan(self.loan)

        self.assertEqual(self.ready_notifications(self.librarian).count(), 1)

    def test_it_names_the_borrower_and_the_book(self):
        self.return_the_loan(self.loan)

        note = self.ready_notifications(self.librarian).get()

        self.assertIn("Hafsa", note.message)
        self.assertIn("Al-Bidaya", note.message)

    def test_it_arrives_unread(self):
        self.return_the_loan(self.loan)

        self.assertIsNone(self.ready_notifications(self.librarian).get().read_at)

    def test_it_is_keyed_to_the_reservation(self):
        self.return_the_loan(self.loan)

        self.assertEqual(
            self.ready_notifications(self.librarian).get().event_key,
            "reservation_ready:%d" % self.waiting.id,
        )

    def test_a_return_with_nobody_waiting_sends_nothing(self):
        queue.close(self.waiting, Reservation.STATUS_CANCELLED)

        self.return_the_loan(self.loan)

        self.assertEqual(self.ready_notifications().count(), 0)

    def test_a_failed_return_sends_nothing(self):
        response = self.client.post(
            reverse("loan_return", args=[self.loan.id]),
            {"return_date": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ready_notifications().count(), 0)

    def test_a_get_on_the_return_sends_nothing(self):
        self.client.get(reverse("loan_return", args=[self.loan.id]))

        self.loan.refresh_from_db()

        self.assertIsNone(self.loan.return_date)
        self.assertEqual(self.ready_notifications().count(), 0)

    def test_a_borrowing_policy_refusal_sends_nothing(self):
        # A form that was refused is not a transition. Nothing changed, so
        # there is nothing to tell anybody about.
        spare = self.a_copy("NOT-1002")

        self.client.post(
            reverse("loan_add"),
            {
                "borrower": self.bilal.id,   # not the front of the queue
                "copies": [spare.id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        self.assertEqual(self.ready_notifications().count(), 0)

    def test_the_activity_log_is_not_a_source(self):
        # Notifications are not made by reading the audit trail, and the
        # return's own log entry is still written exactly once.
        self.return_the_loan(self.loan)

        self.assertEqual(
            ActivityLog.objects.filter(
                action="RETURN", entity_id=self.copy.id
            ).count(),
            1,
        )

        self.assertEqual(self.ready_notifications(self.librarian).count(), 1)

    def test_and_the_notification_is_not_copied_into_it(self):
        self.return_the_loan(self.loan)

        self.assertEqual(
            ActivityLog.objects.filter(
                entity_type="Notification"
            ).count(),
            0,
        )


class IdempotencyTests(NotificationTestCase):
    """One transition, one notification, whatever happens afterwards."""

    def setUp(self):
        super().setUp()

        self.copy = self.a_copy("NOT-2001", status="Issued")
        self.loan = make_loan(copy=self.copy, borrower=self.omar)

        self.waiting, _ = queue.reserve(self.book, self.hafsa)

    def test_returning_twice_sends_one(self):
        self.return_the_loan(self.loan)
        self.return_the_loan(self.loan)

        self.assertEqual(self.ready_notifications(self.librarian).count(), 1)

    def test_the_second_return_changes_nothing_else_either(self):
        self.return_the_loan(self.loan)

        first = self.ready_notifications(self.librarian).get()

        self.return_the_loan(self.loan, when=self.today - timedelta(days=1))

        self.loan.refresh_from_db()

        # The loan keeps the date the first return wrote.
        self.assertEqual(self.loan.return_date, self.today)
        self.assertEqual(
            self.ready_notifications(self.librarian).get().id, first.id
        )

    def test_revisiting_the_returned_loan_sends_nothing_more(self):
        self.return_the_loan(self.loan)

        self.client.get(reverse("loan_return", args=[self.loan.id]))
        self.client.get(reverse("loan_detail", args=[self.loan.id]))

        self.assertEqual(self.ready_notifications(self.librarian).count(), 1)

    def test_two_copies_coming_back_send_one(self):
        second = self.a_copy("NOT-2002", status="Issued")
        second_loan = make_loan(copy=second, borrower=self.bilal)

        self.return_the_loan(self.loan)
        self.return_the_loan(second_loan)

        # Both returns made the same reservation actionable. It was already
        # announced, so the second says nothing.
        self.assertEqual(self.ready_notifications(self.librarian).count(), 1)

    def test_calling_announce_twice_writes_once(self):
        self.copy.status = "Available"
        self.copy.save()

        self.assertIsNotNone(notify.announce_ready(self.book))
        self.assertIsNone(notify.announce_ready(self.book))

        self.assertEqual(self.ready_notifications(self.librarian).count(), 1)

    def test_the_database_is_what_refuses_the_second_one(self):
        # Not the code above it. Written straight at the table, the way two
        # simultaneous requests would arrive.
        for _ in range(2):
            row = Notification(
                recipient=self.librarian,
                event_type=Notification.EVENT_RESERVATION_READY,
                event_key="reservation_ready:%d" % self.waiting.id,
                title="Reserved book is available",
                created_at=timezone.now(),
            )

            try:
                with transaction.atomic():
                    row.save()
            except IntegrityError:
                pass

        self.assertEqual(self.ready_notifications(self.librarian).count(), 1)

    def test_a_second_recipient_gets_their_own(self):
        # The key is unique per recipient, not globally: two people at the
        # desk both need telling.
        assistant = make_user(
            username="assistant_n", password="pass12345", role="Assistant"
        )

        self.return_the_loan(self.loan)

        self.assertEqual(self.ready_notifications(self.librarian).count(), 1)
        self.assertEqual(self.ready_notifications(assistant).count(), 1)

    def test_an_inactive_account_gets_none(self):
        make_user(
            username="retired_n",
            password="pass12345",
            role="Librarian",
            is_active=False,
        )

        self.return_the_loan(self.loan)

        self.assertEqual(
            self.ready_notifications().filter(
                recipient__username="retired_n"
            ).count(),
            0,
        )


# ==========================================================================
# RESERVATIONS — the queue itself
# ==========================================================================


class ReservationEventTests(NotificationTestCase):
    """Task 16 decides who is at the front; this only reads the answer."""

    def setUp(self):
        super().setUp()

        self.copy = self.a_copy("NOT-3001", status="Issued")
        self.loan = make_loan(copy=self.copy, borrower=self.omar)

        self.first, _ = queue.reserve(self.book, self.hafsa)
        self.second, _ = queue.reserve(self.book, self.bilal)

    def test_only_the_front_is_announced(self):
        self.return_the_loan(self.loan)

        keys = set(
            self.ready_notifications(self.librarian).values_list(
                "event_key", flat=True
            )
        )

        self.assertEqual(keys, {"reservation_ready:%d" % self.first.id})

    def test_cancelling_the_front_announces_the_next(self):
        self.return_the_loan(self.loan)

        self.client.post(
            reverse("reservation_cancel", args=[self.first.id])
        )

        keys = set(
            self.ready_notifications(self.librarian).values_list(
                "event_key", flat=True
            )
        )

        self.assertEqual(
            keys,
            {
                "reservation_ready:%d" % self.first.id,
                "reservation_ready:%d" % self.second.id,
            },
        )

    def test_cancelling_does_not_recreate_the_front_notification(self):
        self.return_the_loan(self.loan)

        before = self.ready_notifications(self.librarian).get(
            event_key="reservation_ready:%d" % self.first.id
        )

        self.client.post(
            reverse("reservation_cancel", args=[self.first.id])
        )

        after = self.ready_notifications(self.librarian).get(
            event_key="reservation_ready:%d" % self.first.id
        )

        self.assertEqual(before.id, after.id)
        self.assertEqual(before.created_at, after.created_at)

    def test_cancelling_twice_announces_the_next_once(self):
        self.return_the_loan(self.loan)

        self.client.post(reverse("reservation_cancel", args=[self.first.id]))
        self.client.post(reverse("reservation_cancel", args=[self.first.id]))

        self.assertEqual(
            self.ready_notifications(self.librarian).filter(
                event_key="reservation_ready:%d" % self.second.id
            ).count(),
            1,
        )

    def test_cancelling_with_nothing_on_the_shelf_announces_nobody(self):
        # The copy is still out. Somebody moving up a place in a queue for
        # a book that is not in is not something the desk can act on.
        self.client.post(reverse("reservation_cancel", args=[self.first.id]))

        self.assertEqual(self.ready_notifications().count(), 0)

    def test_no_notification_is_sent_about_the_cancellation_itself(self):
        self.client.post(reverse("reservation_cancel", args=[self.first.id]))

        # Nothing at all, of any type: the only person it is news to is the
        # borrower, who has no account here.
        self.assertEqual(Notification.objects.count(), 0)

        # And the existing staff record of it is untouched.
        self.assertEqual(
            ActivityLog.objects.filter(
                action="CANCEL", entity_type="Reservation"
            ).count(),
            1,
        )

    def test_issuing_to_the_front_announces_the_next_when_a_copy_remains(self):
        self.return_the_loan(self.loan)

        spare = self.a_copy("NOT-3002")

        self.client.post(
            reverse("loan_add"),
            {
                "borrower": self.hafsa.id,
                "copies": [self.copy.id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        self.first.refresh_from_db()
        self.assertEqual(self.first.status, Reservation.STATUS_FULFILLED)
        self.assertEqual(spare.copy_code, "NOT-3002")

        self.assertEqual(
            self.ready_notifications(self.librarian).filter(
                event_key="reservation_ready:%d" % self.second.id
            ).count(),
            1,
        )

    def test_issuing_the_last_copy_announces_nobody(self):
        self.return_the_loan(self.loan)

        self.client.post(
            reverse("loan_add"),
            {
                "borrower": self.hafsa.id,
                "copies": [self.copy.id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        self.assertEqual(
            self.ready_notifications(self.librarian).filter(
                event_key="reservation_ready:%d" % self.second.id
            ).count(),
            0,
        )

    def test_fulfilment_does_not_recreate_the_front_notification(self):
        self.return_the_loan(self.loan)

        before = self.ready_notifications(self.librarian).get(
            event_key="reservation_ready:%d" % self.first.id
        )

        self.client.post(
            reverse("loan_add"),
            {
                "borrower": self.hafsa.id,
                "copies": [self.copy.id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        self.assertEqual(
            self.ready_notifications(self.librarian).get(
                event_key="reservation_ready:%d" % self.first.id
            ).id,
            before.id,
        )

    def test_and_the_copy_is_returned_again_later(self):
        # The whole round trip: out, back, out, back. The first
        # reservation is announced once and never again, and the person
        # behind them is announced when their own turn comes.
        self.return_the_loan(self.loan)

        self.client.post(
            reverse("loan_add"),
            {
                "borrower": self.hafsa.id,
                "copies": [self.copy.id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        second_loan = self.copy.loan_set.get(return_date__isnull=True)

        self.return_the_loan(second_loan)

        counts = {
            key: self.ready_notifications(self.librarian).filter(
                event_key=key
            ).count()
            for key in (
                "reservation_ready:%d" % self.first.id,
                "reservation_ready:%d" % self.second.id,
            )
        }

        self.assertEqual(
            counts,
            {
                "reservation_ready:%d" % self.first.id: 1,
                "reservation_ready:%d" % self.second.id: 1,
            },
        )

    def test_the_destination_is_the_books_reservation_queue(self):
        self.return_the_loan(self.loan)

        note = self.ready_notifications(self.librarian).get()

        self.assertEqual(
            note.url,
            "%s?tab=reservations" % reverse("book_detail", args=[self.book.id]),
        )

    def test_the_queue_itself_is_untouched(self):
        self.return_the_loan(self.loan)

        self.first.refresh_from_db()
        self.second.refresh_from_db()

        self.assertTrue(self.first.is_active)
        self.assertTrue(self.second.is_active)

        # And no copy has been set aside for anybody.
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Available")


# ==========================================================================
# INVENTORY
# ==========================================================================


class StockCheckTests(NotificationTestCase):
    """A finished stock check with copies unaccounted for."""

    def setUp(self):
        super().setUp()

        self.admin = make_user(
            username="admin_n", password="pass12345", role="Admin"
        )
        self.assistant = make_user(
            username="assistant_n", password="pass12345", role="Assistant"
        )

        self.copy = self.a_copy("NOT-4001")

        self.session = InventorySession.objects.create(
            name="Autumn count",
            scope=InventorySession.SCOPE_SHELF,
            shelf=self.shelf,
            status=InventorySession.STATUS_IN_PROGRESS,
            started_by=self.librarian,
            started_at=timezone.now(),
        )

    def missing_notifications(self, recipient=None):
        rows = Notification.objects.filter(
            event_type=Notification.EVENT_STOCK_CHECK_MISSING
        )

        if recipient is not None:
            rows = rows.filter(recipient=recipient)

        return rows

    def complete(self):
        return self.client.post(
            reverse("inventory_session_complete", args=[self.session.id])
        )

    def test_nothing_while_it_is_open(self):
        self.client.get(
            reverse("inventory_session_detail", args=[self.session.id])
        )

        self.assertEqual(self.missing_notifications().count(), 0)

    def test_completing_with_a_copy_unfound_sends_one(self):
        self.complete()

        self.assertEqual(self.missing_notifications(self.librarian).count(), 1)

    def test_it_names_the_session_and_the_number(self):
        self.complete()

        note = self.missing_notifications(self.librarian).get()

        self.assertIn("Autumn count", note.message)
        self.assertIn("1", note.message)

    def test_completing_twice_sends_one(self):
        self.complete()
        self.complete()

        self.assertEqual(self.missing_notifications(self.librarian).count(), 1)

    def test_a_complete_count_sends_nothing(self):
        self.client.post(
            reverse("inventory_session_scan", args=[self.session.id]),
            {"copy_code": self.copy.copy_code},
        )

        self.complete()

        self.assertEqual(self.missing_notifications().count(), 0)

    def test_only_the_roles_that_may_open_the_report(self):
        self.complete()

        recipients = set(
            self.missing_notifications().values_list(
                "recipient__username", flat=True
            )
        )

        self.assertEqual(recipients, {"librarian_n", "admin_n"})
        self.assertNotIn("assistant_n", recipients)

    def test_the_destination_is_the_session(self):
        self.complete()

        self.assertEqual(
            self.missing_notifications(self.librarian).get().url,
            reverse("inventory_session_detail", args=[self.session.id]),
        )


# ==========================================================================
# READ STATE AND PRIVACY
# ==========================================================================


class ReadStateTests(NotificationTestCase):

    def setUp(self):
        super().setUp()

        self.other = make_user(
            username="other_n", password="pass12345", role="Librarian"
        )

        self.mine = Notification.objects.create(
            recipient=self.librarian,
            event_type=Notification.EVENT_RESERVATION_READY,
            event_key="reservation_ready:9001",
            title="Reserved book is available",
            message="Hafsa is first in the queue for Al-Bidaya.",
            url=reverse("book_detail", args=[self.book.id]),
            created_at=timezone.now(),
        )

        self.theirs = Notification.objects.create(
            recipient=self.other,
            event_type=Notification.EVENT_RESERVATION_READY,
            event_key="reservation_ready:9001",
            title="Reserved book is available",
            created_at=timezone.now(),
        )

    def panel(self):
        return self.client.get(
            reverse("notification_panel"), headers={"hx-request": "true"}
        )

    def test_the_unread_count_is_mine_alone(self):
        self.assertEqual(notify.unread_count(self.librarian), 1)
        self.assertEqual(notify.unread_count(self.other), 1)

        notify.mark_read(self.librarian, self.mine.id)

        self.assertEqual(notify.unread_count(self.librarian), 0)
        self.assertEqual(notify.unread_count(self.other), 1)

    def test_the_shell_shows_the_count(self):
        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, 'data-unread-count="1"')

    def test_the_shell_shows_no_badge_with_nothing_unread(self):
        notify.mark_read(self.librarian, self.mine.id)

        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, "data-unread-count")

    def test_marking_one_read(self):
        response = self.client.post(
            reverse("notification_read", args=[self.mine.id])
        )

        self.mine.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(self.mine.read_at)

    def test_marking_read_twice_is_harmless(self):
        self.client.post(reverse("notification_read", args=[self.mine.id]))

        self.mine.refresh_from_db()
        first = self.mine.read_at

        response = self.client.post(
            reverse("notification_read", args=[self.mine.id])
        )

        self.mine.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.mine.read_at, first)

    def test_a_get_marks_nothing_read(self):
        self.client.get(reverse("notification_read", args=[self.mine.id]))

        self.mine.refresh_from_db()

        self.assertIsNone(self.mine.read_at)

    def test_marking_all_read(self):
        extra = Notification.objects.create(
            recipient=self.librarian,
            event_type=Notification.EVENT_STOCK_CHECK_MISSING,
            event_key="stock_check_missing:7",
            title="Stock check finished with copies not found",
            created_at=timezone.now(),
        )

        self.client.post(reverse("notification_read_all"))

        self.mine.refresh_from_db()
        extra.refresh_from_db()

        self.assertIsNotNone(self.mine.read_at)
        self.assertIsNotNone(extra.read_at)

    def test_marking_all_read_twice_is_harmless(self):
        self.client.post(reverse("notification_read_all"))

        self.mine.refresh_from_db()
        first = self.mine.read_at

        response = self.client.post(reverse("notification_read_all"))

        self.mine.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.mine.read_at, first)

    def test_marking_all_read_leaves_other_accounts_alone(self):
        self.client.post(reverse("notification_read_all"))

        self.theirs.refresh_from_db()

        self.assertIsNone(self.theirs.read_at)

    def test_a_get_marks_nothing_at_all_read(self):
        self.client.get(reverse("notification_read_all"))

        self.mine.refresh_from_db()

        self.assertIsNone(self.mine.read_at)


class PrivacyTests(NotificationTestCase):
    """One account can neither see nor touch another's."""

    def setUp(self):
        super().setUp()

        self.other = make_user(
            username="other_n", password="pass12345", role="Librarian"
        )

        self.theirs = Notification.objects.create(
            recipient=self.other,
            event_type=Notification.EVENT_RESERVATION_READY,
            event_key="reservation_ready:9100",
            title="A message for somebody else",
            message="Fatima is first in the queue for a private matter.",
            url=reverse("book_detail", args=[self.book.id]),
            created_at=timezone.now(),
        )

    def test_the_panel_shows_none_of_theirs(self):
        response = self.client.get(
            reverse("notification_panel"), headers={"hx-request": "true"}
        )

        self.assertNotContains(response, "A message for somebody else")
        self.assertNotContains(response, "Fatima")

    def test_the_list_shows_none_of_theirs(self):
        response = self.client.get(reverse("notification_list"))

        self.assertNotContains(response, "A message for somebody else")

    def test_a_forged_id_marks_nothing_read(self):
        self.client.post(
            reverse("notification_read", args=[self.theirs.id])
        )

        self.theirs.refresh_from_db()

        self.assertIsNone(self.theirs.read_at)

    def test_and_says_nothing_about_whether_it_exists(self):
        # A real id belonging to somebody else and an id belonging to
        # nobody have to be indistinguishable, or the response is an
        # oracle for "does notification N exist".
        real = self.client.post(
            reverse("notification_read", args=[self.theirs.id])
        )

        missing = self.client.post(
            reverse("notification_read", args=[self.theirs.id + 100000])
        )

        self.assertEqual(real.status_code, missing.status_code)
        self.assertEqual(real["Location"], missing["Location"])

    def test_the_same_holds_for_the_htmx_answer(self):
        real = self.client.post(
            reverse("notification_read", args=[self.theirs.id]),
            headers={"hx-request": "true"},
        )

        missing = self.client.post(
            reverse("notification_read", args=[self.theirs.id + 100000]),
            headers={"hx-request": "true"},
        )

        self.assertEqual(real.status_code, missing.status_code)
        self.assertEqual(real.content, missing.content)

    def test_signed_out_cannot_reach_the_panel(self):
        self.client.logout()

        response = self.client.get(reverse("notification_panel"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_signed_out_cannot_reach_the_list(self):
        self.client.logout()

        response = self.client.get(reverse("notification_list"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_signed_out_cannot_mark_anything_read(self):
        self.client.logout()

        response = self.client.post(
            reverse("notification_read", args=[self.theirs.id])
        )

        self.theirs.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.theirs.read_at)

    def test_signed_out_cannot_mark_everything_read(self):
        self.client.logout()

        response = self.client.post(reverse("notification_read_all"))

        self.theirs.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.theirs.read_at)

    def test_the_shell_offers_no_bell_when_signed_out(self):
        self.client.logout()

        response = self.client.get(reverse("login"))

        self.assertNotContains(response, "notificationToggle")


# ==========================================================================
# NAVIGATION AND PERMISSIONS
# ==========================================================================


class DestinationTests(NotificationTestCase):
    """The link goes somewhere real, and is still subject to its own rules."""

    def setUp(self):
        super().setUp()

        self.assistant = make_user(
            username="assistant_n", password="pass12345", role="Assistant"
        )

        self.copy = self.a_copy("NOT-5001", status="Issued")
        self.loan = make_loan(copy=self.copy, borrower=self.omar)

        queue.reserve(self.book, self.hafsa)

    def test_the_panel_offers_the_link(self):
        self.return_the_loan(self.loan)

        response = self.client.get(
            reverse("notification_panel"), headers={"hx-request": "true"}
        )

        self.assertContains(
            response,
            "%s?tab=reservations" % reverse("book_detail", args=[self.book.id]),
        )

    def test_the_destination_opens(self):
        self.return_the_loan(self.loan)

        note = self.ready_notifications(self.librarian).get()

        response = self.client.get(note.safe_destination)

        self.assertEqual(response.status_code, 200)

    def test_an_assistant_reaches_the_reservation_destination_too(self):
        # Which is why the reservation notification goes to every role:
        # each of them can act on it.
        self.return_the_loan(self.loan)

        self.client.login(username="assistant_n", password="pass12345")

        note = self.ready_notifications(self.assistant).get()

        self.assertEqual(
            self.client.get(note.safe_destination).status_code, 200
        )

    def test_reading_a_notification_does_not_open_a_closed_door(self):
        # Manufactured on purpose: an Assistant is never sent one of these,
        # and even holding one gets them no further than typing the URL.
        session = InventorySession.objects.create(
            name="Autumn count",
            scope=InventorySession.SCOPE_LIBRARY,
            status=InventorySession.STATUS_IN_PROGRESS,
            started_by=self.librarian,
            started_at=timezone.now(),
        )

        note = Notification.objects.create(
            recipient=self.assistant,
            event_type=Notification.EVENT_STOCK_CHECK_MISSING,
            event_key="stock_check_missing:%d" % session.id,
            title="Stock check finished with copies not found",
            url=reverse("inventory_session_detail", args=[session.id]),
            created_at=timezone.now(),
        )

        self.client.login(username="assistant_n", password="pass12345")

        # The notification itself is theirs and readable.
        self.assertContains(
            self.client.get(reverse("notification_list")),
            "Stock check finished",
        )

        # The page behind it is not.
        self.assertEqual(
            self.client.get(note.safe_destination).status_code, 403
        )

    def test_a_destination_that_is_not_a_local_path_is_not_offered(self):
        note = Notification.objects.create(
            recipient=self.librarian,
            event_type=Notification.EVENT_RESERVATION_READY,
            event_key="reservation_ready:9200",
            title="Somewhere else entirely",
            url="https://example.com/phish",
            created_at=timezone.now(),
        )

        self.assertEqual(note.safe_destination, "")

        response = self.client.get(reverse("notification_list"))

        self.assertContains(response, "Somewhere else entirely")
        self.assertNotContains(response, "example.com/phish")

    def test_a_protocol_relative_destination_is_not_offered_either(self):
        note = Notification.objects.create(
            recipient=self.librarian,
            event_type=Notification.EVENT_RESERVATION_READY,
            event_key="reservation_ready:9201",
            title="Also somewhere else",
            url="//example.com/phish",
            created_at=timezone.now(),
        )

        self.assertEqual(note.safe_destination, "")

    def test_a_deleted_destination_leaves_the_notification_readable(self):
        # A notification is a historical record: it has to keep saying what
        # it said after the thing it points at has gone.
        self.return_the_loan(self.loan)

        note = self.ready_notifications(self.librarian).get()

        Reservation.objects.all().delete()
        self.copy.loan_set.all().delete()
        self.copy.delete()
        self.volume.bookcontent_set.all().delete()
        self.volume.delete()
        self.book.delete()

        response = self.client.get(reverse("notification_list"))

        self.assertContains(response, note.title)
        self.assertContains(response, "Hafsa")

        # And following it answers 404 rather than breaking the list.
        self.assertEqual(
            self.client.get(note.safe_destination).status_code, 404
        )


class ShellTests(NotificationTestCase):
    """What the application shell offers, and to whom."""

    def test_the_bell_is_in_the_topbar(self):
        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, 'id="notificationToggle"')
        self.assertContains(response, reverse("notification_panel"))

    def test_every_role_gets_one(self):
        for role in ("Admin", "Librarian", "Assistant"):
            with self.subTest(role=role):

                make_user(
                    username="shell_%s" % role.lower(),
                    password="pass12345",
                    role=role,
                )

                self.client.login(
                    username="shell_%s" % role.lower(), password="pass12345"
                )

                self.assertContains(
                    self.client.get(reverse("dashboard")),
                    'id="notificationToggle"',
                )

    def test_every_role_may_open_their_own_list(self):
        for role in ("Admin", "Librarian", "Assistant"):
            with self.subTest(role=role):

                make_user(
                    username="list_%s" % role.lower(),
                    password="pass12345",
                    role=role,
                )

                self.client.login(
                    username="list_%s" % role.lower(), password="pass12345"
                )

                self.assertEqual(
                    self.client.get(reverse("notification_list")).status_code,
                    200,
                )

    def test_the_panel_states_its_own_swap_target(self):
        # The whole shell is boosted towards #mainContent; without this the
        # panel would be swapped into the page instead of into the dropdown.
        response = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn('hx-target="#notificationPanelBody"', response)

    def test_the_list_page_is_navigable_like_every_other(self):
        response = self.client.get(
            reverse("notification_list"),
            headers={"hx-request": "true", "hx-target": "mainContent"},
        )

        body = response.content.decode()

        self.assertNotIn("<!DOCTYPE", body.upper())
        self.assertIn('id="pageHeading"', body)


# ==========================================================================
# LIST QUERIES
# ==========================================================================


class ListTests(NotificationTestCase):

    def setUp(self):
        super().setUp()

        now = timezone.now()

        self.rows = [
            Notification.objects.create(
                recipient=self.librarian,
                event_type=Notification.EVENT_RESERVATION_READY,
                event_key="reservation_ready:%d" % index,
                title="Notification %d" % index,
                created_at=now - timedelta(minutes=index),
                read_at=now if index % 2 else None,
            )
            for index in range(30)
        ]

    def test_newest_first(self):
        response = self.client.get(reverse("notification_list"))

        titles = [
            note.title for note in response.context["notifications"]
        ]

        self.assertEqual(titles[0], "Notification 0")
        self.assertEqual(titles[1], "Notification 1")

    def test_ties_are_broken_by_id_descending(self):
        moment = timezone.now()

        Notification.objects.filter(
            id__in=[row.id for row in self.rows[:3]]
        ).update(created_at=moment)

        response = self.client.get(reverse("notification_list"))

        ids = [note.id for note in response.context["notifications"]][:3]

        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_it_is_paginated(self):
        response = self.client.get(reverse("notification_list"))

        page = response.context["notifications"]

        self.assertTrue(page.paginator.num_pages > 1)
        self.assertLess(len(page.object_list), 30)

    def test_the_unread_filter_is_applied_in_the_database(self):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(
                reverse("notification_list"), {"unread": "1"}
            )

        self.assertTrue(
            all(
                note.read_at is None
                for note in response.context["notifications"]
            )
        )

        self.assertTrue(
            any(
                "read_at" in query["sql"] and "IS NULL" in query["sql"]
                for query in captured.captured_queries
            )
        )

    def test_the_panel_is_bounded(self):
        response = self.client.get(
            reverse("notification_panel"), headers={"hx-request": "true"}
        )

        self.assertEqual(
            len(response.context["notifications"]), notify.PANEL_LIMIT
        )

    def test_the_unread_count_is_capped(self):
        Notification.objects.filter(recipient=self.librarian).update(
            read_at=None
        )

        moment = timezone.now()

        Notification.objects.bulk_create(
            [
                Notification(
                    recipient=self.librarian,
                    event_type=Notification.EVENT_RESERVATION_READY,
                    event_key="capped:%d" % index,
                    title="Capped %d" % index,
                    created_at=moment,
                )
                for index in range(notify.UNREAD_CAP + 40)
            ]
        )

        self.assertEqual(
            notify.unread_count(self.librarian), notify.UNREAD_CAP
        )


class QueryCountTests(NotificationTestCase):
    """Cheap with nothing, and just as cheap with a great deal."""

    made = 0

    def many(self, count, recipient=None, read=False):
        """Add `count` more, with keys that never collide with the last lot."""

        moment = timezone.now()
        first = self.made
        self.made += count

        Notification.objects.bulk_create(
            [
                Notification(
                    recipient=recipient or self.librarian,
                    event_type=Notification.EVENT_RESERVATION_READY,
                    event_key="bulk:%d" % (first + index),
                    title="Bulk %d" % index,
                    message="A copy of Al-Bidaya is on the shelf.",
                    url=reverse("book_detail", args=[self.book.id]),
                    created_at=moment - timedelta(seconds=index),
                    read_at=moment if read else None,
                )
                for index in range(count)
            ]
        )

    def count_for(self, url, **kwargs):
        with CaptureQueriesContext(connection) as captured:
            self.client.get(url, **kwargs)

        return len(captured.captured_queries)

    def test_a_normal_page_costs_the_same_with_none_and_with_many(self):
        empty = self.count_for(reverse("dashboard"))

        self.many(400)

        loaded = self.count_for(reverse("dashboard"))

        self.assertEqual(empty, loaded)

    def test_the_shell_adds_exactly_one_query(self):
        # The indicator is a template tag rather than a context processor
        # precisely so this is one query on a full page load, and none on
        # the HTMX navigations in between.
        self.many(50)

        whole = self.count_for(reverse("dashboard"))

        region = self.count_for(
            reverse("dashboard"),
            headers={"hx-request": "true", "hx-target": "mainContent"},
        )

        self.assertEqual(whole - region, 1)

    def test_the_panel_costs_the_same_with_ten_and_with_a_thousand(self):
        self.many(10)

        few = self.count_for(
            reverse("notification_panel"), headers={"hx-request": "true"}
        )

        self.many(1000)

        many = self.count_for(
            reverse("notification_panel"), headers={"hx-request": "true"}
        )

        self.assertEqual(few, many)

    def test_the_list_costs_the_same_with_ten_and_with_a_thousand(self):
        self.many(10)

        few = self.count_for(reverse("notification_list"))

        self.many(1000)

        many = self.count_for(reverse("notification_list"))

        self.assertEqual(few, many)

    def test_the_list_joins_to_nothing(self):
        # Every notification carries its own title, message and
        # destination, so a page of them is one query for the rows and no
        # query per row - there is no linked record to fetch.
        self.many(60)

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("notification_list"))

            list(response.context["notifications"])

        row_queries = [
            query["sql"]
            for query in captured.captured_queries
            if "notifications" in query["sql"]
        ]

        for sql in row_queries:
            self.assertNotIn("books", sql)
            self.assertNotIn("reservations", sql)
            self.assertNotIn("borrowers", sql)

    def test_marking_all_read_is_one_statement_however_many_there_are(self):
        self.many(500)

        with CaptureQueriesContext(connection) as captured:
            notify.mark_all_read(self.librarian)

        updates = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].strip().upper().startswith("UPDATE")
        ]

        self.assertEqual(len(updates), 1)

    def test_an_unrelated_page_is_untouched(self):
        # Nothing was added to other pages to prefetch notifications: the
        # borrower page costs what it did.
        self.many(200)

        before = self.count_for(
            reverse("borrower_detail", args=[self.hafsa.id]),
            headers={"hx-request": "true", "hx-target": "mainContent"},
        )

        self.many(200, read=True)

        after = self.count_for(
            reverse("borrower_detail", args=[self.hafsa.id]),
            headers={"hx-request": "true", "hx-target": "mainContent"},
        )

        self.assertEqual(before, after)


# ==========================================================================
# CONCURRENCY
# ==========================================================================


class ConcurrencyTests(NotificationTestCase):
    """The duplicate transitions that actually happen at a library desk."""

    def setUp(self):
        super().setUp()

        self.copy = self.a_copy("NOT-6001", status="Issued")
        self.loan = make_loan(copy=self.copy, borrower=self.omar)

        self.waiting, _ = queue.reserve(self.book, self.hafsa)

    def test_two_attempts_to_make_the_same_reservation_actionable(self):
        self.copy.status = "Available"
        self.copy.save()

        # What two simultaneous returns come down to: both find the same
        # front with a copy on the shelf, and both try to announce it.
        for _ in range(2):
            notify.announce_ready(self.book)

        self.assertEqual(
            Notification.objects.filter(
                event_key="reservation_ready:%d" % self.waiting.id
            ).count(),
            1,
        )

    def test_a_double_clicked_return(self):
        for _ in range(2):
            self.return_the_loan(self.loan)

        self.loan.refresh_from_db()

        self.assertEqual(self.loan.return_date, self.today)

        self.assertEqual(
            Notification.objects.filter(
                event_key="reservation_ready:%d" % self.waiting.id
            ).count(),
            1,
        )

        # And the return was logged once, not twice.
        self.assertEqual(
            ActivityLog.objects.filter(
                action="RETURN", entity_id=self.copy.id
            ).count(),
            1,
        )

    def test_a_repeated_completion(self):
        session = InventorySession.objects.create(
            name="Autumn count",
            scope=InventorySession.SCOPE_SHELF,
            shelf=self.shelf,
            status=InventorySession.STATUS_IN_PROGRESS,
            started_by=self.librarian,
            started_at=timezone.now(),
        )

        for _ in range(3):
            self.client.post(
                reverse("inventory_session_complete", args=[session.id])
            )

        self.assertEqual(
            Notification.objects.filter(
                event_key="stock_check_missing:%d" % session.id
            ).count(),
            1,
        )

    def test_repeated_mark_read(self):
        self.return_the_loan(self.loan)

        note = Notification.objects.get(recipient=self.librarian)

        moved = [
            notify.mark_read(self.librarian, note.id) for _ in range(3)
        ]

        note.refresh_from_db()

        self.assertEqual(moved, [True, False, False])
        self.assertIsNotNone(note.read_at)

    def test_repeated_mark_all_read(self):
        self.return_the_loan(self.loan)

        moved = [notify.mark_all_read(self.librarian) for _ in range(3)]

        self.assertEqual(moved, [1, 0, 0])
