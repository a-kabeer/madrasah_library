"""Editing or deleting a loan leaves the copies consistent, and names the right person.

Moving an open loan to a different copy writes three rows that only mean
anything together - the loan, the copy it leaves and the copy it goes to - and
deleting an open loan writes two. Both were done outside a transaction, so a
failure between them left a copy marked Available while it was actually out, or
marked Issued with no loan against it.

The edit also recorded the wrong person: the actor on the activity log was
`loan.issued_by`, whoever first issued the loan, rather than whoever made the
change. A log that names the wrong person is worse than one that names nobody.
"""

from datetime import date

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from library.models import ActivityLog, BookCopy, Loan
from library.tests.helpers import (
    make_author, make_book, make_borrower, make_category, make_copy, make_loan,
    make_location, make_publisher, make_shelf, make_user, make_volume,
)

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class LoanEditTests(TestCase):

    def setUp(self):
        self.issuer = make_user(username="issued_by_u", password="p", role="Librarian")
        self.editor = make_user(username="editor_u", password="p", role="Admin")
        self.client.force_login(self.editor)

        author = make_author(name="Edit Author")
        category = make_category(name="Edit Category")
        publisher = make_publisher(name="Edit Publisher")
        location = make_location(name="Edit Location")
        self.shelf = make_shelf(location=location, shelf_code="ED-1")
        book = make_book(
            title="Edited Book", author=author, category=category, publisher=publisher
        )
        volume = make_volume(book=book, volume_number=1)

        self.original = make_copy(
            volume=volume, shelf=self.shelf, copy_code="ED-00001", status="Issued"
        )
        self.replacement = make_copy(
            volume=volume, shelf=self.shelf, copy_code="ED-00002", status="Available"
        )
        self.borrower = make_borrower(name="Edit Borrower", phone="03004440001")
        self.loan = make_loan(
            copy=self.original, borrower=self.borrower, issued_by=self.issuer
        )
        cache.clear()

    def move_to(self, copy):
        return self.client.post(
            reverse("loan_edit", args=[self.loan.id]),
            {
                "borrower": str(self.borrower.id),
                "copy": str(copy.id),
                "issue_date": self.loan.issue_date.isoformat(),
                "due_date": self.loan.due_date.isoformat(),
                "notes": "",
            },
        )

    def test_moving_the_loan_moves_both_copy_statuses(self):
        self.move_to(self.replacement)

        self.assertEqual(Loan.objects.get(id=self.loan.id).copy_id, self.replacement.id)
        self.assertEqual(
            BookCopy.objects.get(id=self.original.id).status, "Available"
        )
        self.assertEqual(BookCopy.objects.get(id=self.replacement.id).status, "Issued")

    def test_the_log_names_whoever_made_the_change(self):
        self.move_to(self.replacement)

        entry = ActivityLog.objects.filter(
            entity_type="Loan", action="UPDATE", entity_id=self.loan.id
        ).latest("created_at")

        self.assertEqual(entry.user_id, self.editor.id)
        self.assertNotEqual(entry.user_id, self.issuer.id)

    def test_a_copy_that_is_already_out_is_refused(self):
        taken = make_copy(
            volume=self.original.volume,
            shelf=self.shelf,
            copy_code="ED-00003",
            status="Issued",
        )

        self.move_to(taken)

        # Unchanged: still on the copy it started on.
        self.assertEqual(Loan.objects.get(id=self.loan.id).copy_id, self.original.id)


@override_settings(CACHES=LOCMEM)
class LoanDeleteTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="del_admin", password="p", role="Admin")
        self.client.force_login(self.admin)
        author = make_author(name="Del Author")
        book = make_book(title="Deleted Loan Book", author=author)
        volume = make_volume(book=book, volume_number=1)
        self.copy = make_copy(volume=volume, copy_code="DL-00001", status="Issued")
        self.loan = make_loan(copy=self.copy, issued_by=self.admin)
        cache.clear()

    def test_deleting_an_open_loan_frees_its_copy(self):
        self.client.post(reverse("loan_delete", args=[self.loan.id]))

        self.assertFalse(Loan.objects.filter(id=self.loan.id).exists())
        self.assertEqual(BookCopy.objects.get(id=self.copy.id).status, "Available")

    def test_deleting_a_returned_loan_leaves_the_copy_alone(self):
        returned = make_loan(
            copy=self.copy,
            issued_by=self.admin,
            issue_date=date(2020, 1, 1),
            due_date=date(2020, 1, 15),
            return_date=date(2020, 1, 10),
        )

        # After the loan, not before: make_loan sets the copy's status
        # itself to keep its fixtures self-consistent.
        self.copy.status = "Damaged"
        self.copy.save(update_fields=["status"])

        self.client.post(reverse("loan_delete", args=[returned.id]))

        self.assertFalse(Loan.objects.filter(id=returned.id).exists())
        self.assertEqual(BookCopy.objects.get(id=self.copy.id).status, "Damaged")
