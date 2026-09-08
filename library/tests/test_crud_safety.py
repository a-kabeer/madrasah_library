"""Cross-entity CRUD safety regressions.

These tests deliberately exercise the destructive endpoints without relying
on the UI: a confirmation GET must never change data, and the existing
relationship guards must refuse deletion when history would be lost.
"""

from datetime import date

from django.test import TestCase
from django.urls import reverse

from library.models import Book, BookCopy, Category, Loan
from library.tests.helpers import (
    make_author,
    make_book,
    make_borrower,
    make_category,
    make_copy,
    make_loan,
    make_location,
    make_publisher,
    make_shelf,
    make_user,
    make_volume,
)


class CrudSafetyTestCase(TestCase):
    def setUp(self):
        self.admin = make_user(
            username="admin_u", password="pass12345", role="Admin"
        )
        self.client.login(username="admin_u", password="pass12345")

        self.author = make_author(name="Author One")
        self.category = make_category(name="Category One")
        self.publisher = make_publisher(name="Publisher One")
        self.book = make_book(
            title="Book One",
            author=self.author,
            category=self.category,
            publisher=self.publisher,
        )
        self.volume = make_volume(
            book=self.book,
            volume_number=1,
            title="",
        )
        self.location = make_location(name="Main Hall")
        self.shelf = make_shelf(
            location=self.location,
            shelf_code="A-1",
        )
        self.copy = make_copy(
            volume=self.volume,
            shelf=self.shelf,
            copy_code="SAFE-1",
        )
        self.borrower = make_borrower(
            name="Borrower One",
            phone="0300-0001",
            borrower_type="Student",
        )
        self.loan = make_loan(
            copy=self.copy,
            borrower=self.borrower,
            issue_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
        )

    def test_delete_confirmation_gets_never_delete_records(self):
        endpoints = (
            ("category_delete", self.category.id),
            ("author_delete", self.author.id),
            ("publisher_delete", self.publisher.id),
            ("book_delete", self.book.id),
            ("borrower_delete", self.borrower.id),
            ("loan_delete", self.loan.id),
            ("book_copy_delete", self.copy.id),
        )

        before = {
            "book": Book.objects.filter(id=self.book.id).exists(),
            "category": Category.objects.filter(id=self.category.id).exists(),
            "loan": Loan.objects.filter(id=self.loan.id).exists(),
        }

        for name, object_id in endpoints:
            with self.subTest(name=name):
                response = self.client.get(
                    reverse(name, args=[object_id])
                )
                self.assertIn(response.status_code, (200, 302))

        self.assertEqual(
            before,
            {
                "book": Book.objects.filter(id=self.book.id).exists(),
                "category": Category.objects.filter(id=self.category.id).exists(),
                "loan": Loan.objects.filter(id=self.loan.id).exists(),
            },
        )

    def test_book_delete_is_blocked_when_copy_history_exists(self):
        response = self.client.get(
            reverse("book_delete", args=[self.book.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Book.objects.filter(id=self.book.id).exists())

    def test_lookup_delete_is_blocked_when_book_still_uses_it(self):
        for name, obj in (
            ("category_delete", self.category),
            ("author_delete", self.author),
            ("publisher_delete", self.publisher),
        ):
            with self.subTest(name=name):
                response = self.client.get(
                    reverse(name, args=[obj.id]),
                    {"modal": "1"},
                    headers={"HX-Request": "true"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "cannot be deleted")
                self.assertTrue(
                    Book.objects.filter(id=self.book.id).exists()
                )

    def test_borrower_delete_is_blocked_by_returned_or_active_history(self):
        response = self.client.get(
            reverse("borrower_delete", args=[self.borrower.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "loan")
        self.assertTrue(
            Loan.objects.filter(id=self.loan.id).exists()
        )
