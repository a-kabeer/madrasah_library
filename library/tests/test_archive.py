"""Archiving a book and withdrawing a copy, without losing anything.

Two mechanisms, chosen to be as small as the system allows: a book gets one
new nullable timestamp, and a copy reuses a status value the
`check_copy_status` CHECK constraint already permits and the rest of the app
already treats as out of circulation. Nothing else gains an archive flag.
"""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from library.models import Book, BookCopy, BookVolume, Loan
from library.views import COPY_WITHDRAWN_STATUS
from library.tests.helpers import (
    make_author,
    make_book,
    make_borrower,
    make_copy,
    make_loan,
    make_shelf,
    make_user,
    make_volume,
)


class ArchiveBookTests(TestCase):

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.book = make_book(title="Kitab al-Kharaj")
        self.volume = make_volume(book=self.book, volume_number=1)
        self.shelf = make_shelf()
        self.copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="ARC-0001"
        )
        self.loan = make_loan(
            copy=self.copy,
            issue_date=timezone.now().date() - timedelta(days=60),
            due_date=timezone.now().date() - timedelta(days=46),
            return_date=timezone.now().date() - timedelta(days=50),
        )

    def archive(self):
        return self.client.post(reverse("book_archive", args=[self.book.id]))

    def test_a_book_starts_out_active(self):
        self.assertIsNone(self.book.archived_at)
        self.assertFalse(self.book.is_archived)

    def test_archiving_keeps_the_row_and_stamps_it(self):
        before = Book.objects.count()

        response = self.archive()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Book.objects.count(), before)

        self.book.refresh_from_db()
        self.assertIsNotNone(self.book.archived_at)
        self.assertTrue(self.book.is_archived)

    def test_archiving_keeps_volumes_copies_and_loans(self):
        self.archive()

        self.assertTrue(BookVolume.objects.filter(id=self.volume.id).exists())

        copy = BookCopy.objects.get(id=self.copy.id)
        self.assertEqual(copy.copy_code, "ARC-0001")
        self.assertEqual(copy.shelf_id, self.shelf.id)

        loan = Loan.objects.get(id=self.loan.id)
        self.assertEqual(loan.copy_id, self.copy.id)

    def test_restoring_clears_the_stamp(self):
        self.archive()

        response = self.client.post(
            reverse("book_restore", args=[self.book.id])
        )

        self.assertEqual(response.status_code, 302)
        self.book.refresh_from_db()
        self.assertIsNone(self.book.archived_at)

    def test_the_confirmation_page_says_what_is_kept(self):
        response = self.client.get(
            reverse("book_archive", args=[self.book.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Archive this book?")
        self.assertContains(response, "restore it at any time")

    def test_a_get_does_not_archive_anything(self):
        self.client.get(reverse("book_archive", args=[self.book.id]))

        self.book.refresh_from_db()
        self.assertIsNone(self.book.archived_at)

    def test_archiving_twice_does_not_move_the_stamp(self):
        self.archive()
        self.book.refresh_from_db()
        first = self.book.archived_at

        self.archive()
        self.book.refresh_from_db()

        self.assertEqual(self.book.archived_at, first)

    def test_it_is_refused_while_a_copy_is_out(self):
        make_loan(copy=self.copy)

        response = self.archive()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be archived")
        self.book.refresh_from_db()
        self.assertIsNone(self.book.archived_at)

    def test_the_refusal_says_how_many_are_out(self):
        make_loan(copy=self.copy)

        response = self.client.get(
            reverse("book_archive", args=[self.book.id])
        )

        self.assertContains(response, "out on loan")

    def test_a_book_with_no_copies_can_be_archived(self):
        empty = make_book(title="Nothing Yet", author=make_author("Nobody"))

        response = self.client.post(
            reverse("book_archive", args=[empty.id])
        )

        self.assertEqual(response.status_code, 302)
        empty.refresh_from_db()
        self.assertIsNotNone(empty.archived_at)


class ArchivedCatalogueTests(TestCase):

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.kept = make_book(title="Still Here")
        self.gone = make_book(
            title="Put Away", author=make_author("Another Author")
        )
        self.gone.archived_at = timezone.now()
        self.gone.save(update_fields=["archived_at"])

    def test_the_catalogue_leaves_archived_books_out(self):
        response = self.client.get(reverse("book_list"))

        self.assertContains(response, "Still Here")
        self.assertNotContains(response, "Put Away")

    def test_search_leaves_them_out_too(self):
        response = self.client.get(reverse("book_list") + "?search=Put Away")

        # The term itself comes back in the search box and the filter chip,
        # so the count and the rows are what say it was not found.
        self.assertEqual(response.context["paginator"].count, 0)
        self.assertEqual(list(response.context["books"]), [])
        self.assertContains(response, "No books found")

    def test_filtering_by_author_leaves_them_out(self):
        response = self.client.get(
            reverse("book_list") + "?mode=author&author=%d" % self.gone.author_id
        )

        self.assertNotContains(response, "Put Away")

    def test_the_archive_can_be_asked_for(self):
        response = self.client.get(reverse("book_list") + "?archived=1")

        self.assertContains(response, "Put Away")
        self.assertNotContains(response, "Still Here")
        self.assertTrue(response.context["show_archived"])

    def test_the_catalogue_offers_the_way_to_the_archive(self):
        response = self.client.get(reverse("book_list"))

        self.assertEqual(response.context["archived_total"], 1)
        self.assertContains(response, "archived")

    def test_search_still_works_inside_the_archive(self):
        response = self.client.get(
            reverse("book_list") + "?archived=1&search=Put"
        )

        self.assertContains(response, "Put Away")
        self.assertEqual(response.context["paginator"].count, 1)

    def test_an_archived_book_keeps_its_own_page(self):
        response = self.client.get(
            reverse("book_detail", args=[self.gone.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Put Away")
        self.assertContains(response, "Archived on")
        self.assertContains(response, reverse("book_restore", args=[self.gone.id]))

    def test_an_archived_book_is_not_offered_the_active_actions(self):
        response = self.client.get(
            reverse("book_detail", args=[self.gone.id])
        )

        self.assertNotContains(
            response, reverse("book_edit", args=[self.gone.id])
        )
        self.assertNotContains(response, reverse("book_volume_add"))

    def test_an_active_book_is(self):
        response = self.client.get(
            reverse("book_detail", args=[self.kept.id])
        )

        self.assertContains(
            response, reverse("book_edit", args=[self.kept.id])
        )
        self.assertContains(
            response, reverse("book_archive", args=[self.kept.id])
        )


class ArchivedCirculationTests(TestCase):

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.book = make_book(title="Out of Service")
        self.volume = make_volume(book=self.book, volume_number=1)
        self.copy = make_copy(
            volume=self.volume, shelf=make_shelf(), copy_code="ARC-1001"
        )

    def lookup(self):
        # The issue form lists nothing until a copy is searched for, so
        # asking for it by code is what says whether it is issuable. It
        # used to render every available copy in the library.
        #
        # Part of the code rather than all of it: a whole code is taken as
        # a scan now and added to the basket, which would answer with a
        # redirect instead of the list this reads.
        return self.client.get(reverse("loan_add"), {"q": "ARC-100"})

    def test_its_copies_are_offered_for_issue_while_it_is_active(self):
        response = self.lookup()

        self.assertEqual(
            [c.id for c in response.context["matches"]], [self.copy.id]
        )
        self.assertContains(response, "ARC-1001")

    def test_its_copies_are_not_offered_once_it_is_archived(self):
        self.book.archived_at = timezone.now()
        self.book.save(update_fields=["archived_at"])

        response = self.lookup()

        # Searched for by its exact code and still not found, so this says
        # something rather than passing because nothing is ever listed.
        self.assertEqual(list(response.context["matches"]), [])

    def test_restoring_puts_them_back(self):
        self.book.archived_at = timezone.now()
        self.book.save(update_fields=["archived_at"])
        self.client.post(reverse("book_restore", args=[self.book.id]))

        response = self.lookup()

        self.assertEqual(
            [c.id for c in response.context["matches"]], [self.copy.id]
        )


class WithdrawCopyTests(TestCase):

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.book = make_book(title="Weeding Candidate")
        self.volume = make_volume(book=self.book, volume_number=1)
        self.copy = make_copy(
            volume=self.volume, shelf=make_shelf(), copy_code="WD-0001"
        )
        self.history = make_loan(
            copy=self.copy,
            issue_date=timezone.now().date() - timedelta(days=90),
            due_date=timezone.now().date() - timedelta(days=76),
            return_date=timezone.now().date() - timedelta(days=80),
        )

    def withdraw(self):
        return self.client.post(
            reverse("book_copy_withdraw", args=[self.copy.id])
        )

    def test_it_reuses_a_status_the_constraint_already_allows(self):
        self.assertIn(
            COPY_WITHDRAWN_STATUS,
            [value for value, _ in BookCopy.STATUS_CHOICES],
        )

    def test_withdrawing_keeps_the_copy_and_marks_it(self):
        before = BookCopy.objects.count()

        response = self.withdraw()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(BookCopy.objects.count(), before)

        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, COPY_WITHDRAWN_STATUS)
        self.assertEqual(self.copy.copy_code, "WD-0001")

    def test_its_loan_history_survives(self):
        self.withdraw()

        loan = Loan.objects.get(id=self.history.id)
        self.assertEqual(loan.copy_id, self.copy.id)
        self.assertIsNotNone(loan.return_date)

    def test_a_withdrawn_copy_is_not_offered_for_issue(self):
        self.withdraw()

        response = self.client.get(reverse("loan_add"))

        self.assertNotContains(response, "WD-0001")

    def test_a_withdrawn_copy_cannot_be_issued_by_posting_its_id(self):
        self.withdraw()
        borrower = make_borrower()
        today = timezone.now().date()
        before = Loan.objects.count()

        response = self.client.post(reverse("loan_add"), {
            "borrower": borrower.id,
            "copies": [str(self.copy.id)],
            "issue_date": today.isoformat(),
            "due_date": (today + timedelta(days=14)).isoformat(),
            "notes": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Loan.objects.count(), before)

    def test_a_withdrawn_copy_does_not_count_as_available(self):
        self.withdraw()

        response = self.client.get(
            reverse("book_copy_list") + "?status=available"
        )

        self.assertNotContains(response, "WD-0001")

    def test_it_is_still_findable_by_its_code(self):
        self.withdraw()

        response = self.client.get(
            reverse("book_copy_list") + "?search=WD-0001"
        )

        self.assertContains(response, "WD-0001")

    def test_it_is_refused_while_the_copy_is_out(self):
        make_loan(copy=self.copy)

        response = self.withdraw()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be withdrawn")

        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Issued")

    def test_the_refusal_names_the_borrower_and_offers_the_return(self):
        loan = make_loan(copy=self.copy, borrower=make_borrower(name="Hafsa"))

        response = self.client.get(
            reverse("book_copy_withdraw", args=[self.copy.id])
        )

        self.assertContains(response, "Hafsa")
        self.assertContains(response, reverse("loan_return", args=[loan.id]))

    def test_the_active_loan_is_left_alone_by_a_refused_withdrawal(self):
        loan = make_loan(copy=self.copy)

        self.withdraw()

        loan.refresh_from_db()
        self.assertIsNone(loan.return_date)

    def test_a_get_does_not_withdraw_anything(self):
        self.client.get(reverse("book_copy_withdraw", args=[self.copy.id]))

        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Available")

    def test_a_copy_already_out_of_circulation_says_so(self):
        self.copy.status = "Lost"
        self.copy.save(update_fields=["status"])

        response = self.client.get(
            reverse("book_copy_withdraw", args=[self.copy.id])
        )

        self.assertContains(response, "Already out of circulation")

        self.withdraw()
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Lost")


class ArchivePermissionTests(TestCase):

    def setUp(self):
        make_user(username="assistant_u", password="pass12345", role="Assistant")
        self.client.login(username="assistant_u", password="pass12345")
        self.book = make_book(title="Hands Off")
        self.volume = make_volume(book=self.book, volume_number=1)
        self.copy = make_copy(volume=self.volume, copy_code="PERM-0001")

    def test_an_assistant_cannot_reach_archive(self):
        self.assertEqual(
            self.client.get(
                reverse("book_archive", args=[self.book.id])
            ).status_code,
            403,
        )

    def test_an_assistant_cannot_archive_by_posting(self):
        response = self.client.post(
            reverse("book_archive", args=[self.book.id])
        )

        self.assertEqual(response.status_code, 403)
        self.book.refresh_from_db()
        self.assertIsNone(self.book.archived_at)

    def test_an_assistant_cannot_restore(self):
        self.book.archived_at = timezone.now()
        self.book.save(update_fields=["archived_at"])

        response = self.client.post(
            reverse("book_restore", args=[self.book.id])
        )

        self.assertEqual(response.status_code, 403)
        self.book.refresh_from_db()
        self.assertIsNotNone(self.book.archived_at)

    def test_an_assistant_cannot_withdraw_a_copy(self):
        response = self.client.post(
            reverse("book_copy_withdraw", args=[self.copy.id])
        )

        self.assertEqual(response.status_code, 403)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Available")

    def test_an_assistant_can_still_read_the_book(self):
        self.assertEqual(
            self.client.get(
                reverse("book_detail", args=[self.book.id])
            ).status_code,
            200,
        )

    def test_an_assistant_is_offered_neither_action(self):
        response = self.client.get(
            reverse("book_detail", args=[self.book.id])
        )

        self.assertNotContains(
            response, reverse("book_archive", args=[self.book.id])
        )


class DeleteStillGuardedTests(TestCase):
    """The existing delete safety is untouched; archiving is the way out."""

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.book = make_book(title="Has History")
        self.volume = make_volume(book=self.book, volume_number=1)
        self.copy = make_copy(volume=self.volume, copy_code="DEL-0001")
        make_loan(
            copy=self.copy,
            issue_date=timezone.now().date() - timedelta(days=40),
            due_date=timezone.now().date() - timedelta(days=26),
            return_date=timezone.now().date() - timedelta(days=30),
        )

    def test_deleting_a_book_with_history_is_still_refused(self):
        response = self.client.post(
            reverse("book_delete", args=[self.book.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Book.objects.filter(id=self.book.id).exists())

    def test_the_refusal_offers_archiving_instead(self):
        response = self.client.get(
            reverse("book_delete", args=[self.book.id])
        )

        self.assertContains(response, "Archive instead")
        self.assertContains(
            response, reverse("book_archive", args=[self.book.id])
        )

    def test_deleting_a_book_with_nothing_attached_still_works(self):
        spare = make_book(title="Never Used", author=make_author("Nobody Much"))

        response = self.client.post(reverse("book_delete", args=[spare.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Book.objects.filter(id=spare.id).exists())

    def test_deleting_a_copy_with_history_is_still_refused(self):
        response = self.client.post(
            reverse("book_copy_delete", args=[self.copy.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(BookCopy.objects.filter(id=self.copy.id).exists())
