from datetime import date

from django.test import TestCase
from django.urls import reverse

from library.models import Category, Author, Publisher, Location, Shelf, Borrower, BookCopy
from library.tests.helpers import (
    make_user, make_category, make_author, make_publisher, make_book,
    make_location, make_shelf, make_copy, make_volume, make_borrower, make_loan,
)


class DeleteProtectionTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_category_delete_blocked_when_books_exist(self):
        category = make_category()
        make_book(category=category)

        response = self.client.post(
            reverse("category_delete", args=[category.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Category.objects.filter(id=category.id).exists())

    def test_category_delete_succeeds_when_no_books(self):
        category = make_category()

        response = self.client.post(
            reverse("category_delete", args=[category.id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Category.objects.filter(id=category.id).exists())

    def test_author_delete_blocked_when_books_exist(self):
        author = make_author()
        make_book(author=author)

        response = self.client.post(reverse("author_delete", args=[author.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Author.objects.filter(id=author.id).exists())

    def test_publisher_delete_blocked_when_books_exist(self):
        publisher = make_publisher()
        make_book(publisher=publisher)

        response = self.client.post(reverse("publisher_delete", args=[publisher.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Publisher.objects.filter(id=publisher.id).exists())

    def test_location_delete_blocked_when_shelves_exist(self):
        location = make_location()
        make_shelf(location=location)

        response = self.client.post(reverse("location_delete", args=[location.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Location.objects.filter(id=location.id).exists())

    def test_shelf_delete_blocked_when_copies_exist(self):
        shelf = make_shelf()
        make_copy(shelf=shelf)

        response = self.client.post(reverse("shelf_delete", args=[shelf.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Shelf.objects.filter(id=shelf.id).exists())

    def test_borrower_delete_blocked_when_active_loan_exists(self):
        borrower = make_borrower()
        make_loan(borrower=borrower)

        response = self.client.post(reverse("borrower_delete", args=[borrower.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Borrower.objects.filter(id=borrower.id).exists())

    def test_borrower_delete_still_blocked_once_loan_is_returned(self):
        # loans.borrower_id -> borrowers.id is a NO ACTION FK at the DB
        # level: it blocks deletion for ANY referencing loan, not just
        # active ones, since deleting the borrower would destroy loan
        # history. A returned loan must still block the delete.
        borrower = make_borrower()
        make_loan(
            borrower=borrower,
            issue_date=date(2020, 1, 1),
            due_date=date(2020, 1, 15),
            return_date=date(2020, 1, 15),
        )

        response = self.client.post(reverse("borrower_delete", args=[borrower.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Borrower.objects.filter(id=borrower.id).exists())

    def test_borrower_delete_succeeds_with_no_loan_history_at_all(self):
        borrower = make_borrower()

        response = self.client.post(reverse("borrower_delete", args=[borrower.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Borrower.objects.filter(id=borrower.id).exists())

    def test_book_copy_delete_blocked_when_active_loan_exists(self):
        copy = make_copy()
        make_loan(copy=copy)

        response = self.client.post(reverse("book_copy_delete", args=[copy.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(BookCopy.objects.filter(id=copy.id).exists())

    def test_book_copy_delete_still_blocked_once_loan_is_returned(self):
        # Same NO ACTION FK situation as borrowers: loans.copy_id ->
        # book_copies.id blocks deletion for any referencing loan record.
        copy = make_copy(status="Issued")
        make_loan(
            copy=copy,
            issue_date=date(2020, 1, 1),
            due_date=date(2020, 1, 15),
            return_date=date(2020, 1, 15),
        )

        response = self.client.post(reverse("book_copy_delete", args=[copy.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(BookCopy.objects.filter(id=copy.id).exists())

    def test_book_copy_delete_succeeds_with_no_loan_history_at_all(self):
        copy = make_copy()

        response = self.client.post(reverse("book_copy_delete", args=[copy.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(BookCopy.objects.filter(id=copy.id).exists())
