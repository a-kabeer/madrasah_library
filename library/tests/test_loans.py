from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse

from library.models import BookCopy, Loan
from library.tests.helpers import make_user, make_copy, make_borrower, make_loan


class LoanIssueTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")
        self.borrower = make_borrower()

    def test_issuing_a_loan_marks_copy_as_issued(self):
        copy = make_copy(status="Available")

        response = self.client.post(reverse("loan_add"), {
            "copies": [copy.id],
            "borrower": self.borrower.id,
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=14)).isoformat(),
            "issued_by": "",
            "notes": "",
        })

        self.assertEqual(response.status_code, 302)
        copy.refresh_from_db()
        self.assertEqual(copy.status, "Issued")
        self.assertTrue(Loan.objects.filter(copy=copy, return_date__isnull=True).exists())

    def test_due_date_before_issue_date_is_rejected(self):
        copy = make_copy(status="Available")

        response = self.client.post(reverse("loan_add"), {
            "copies": [copy.id],
            "borrower": self.borrower.id,
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() - timedelta(days=1)).isoformat(),
            "issued_by": "",
            "notes": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be earlier")
        copy.refresh_from_db()
        self.assertEqual(copy.status, "Available")
        self.assertFalse(Loan.objects.filter(copy=copy).exists())

    def test_already_issued_copy_does_not_appear_in_available_choices(self):
        issued_copy = make_copy(copy_code="COPY-ISSUED", status="Issued")

        response = self.client.get(reverse("loan_add"))

        self.assertNotContains(response, issued_copy.copy_code)

    def test_cannot_double_issue_a_copy_via_direct_post(self):
        # Simulates a stale form: the copy was Available when the page
        # loaded but got issued to someone else before this POST landed.
        copy = make_copy(status="Issued")

        response = self.client.post(reverse("loan_add"), {
            "copies": [copy.id],
            "borrower": self.borrower.id,
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=14)).isoformat(),
            "issued_by": "",
            "notes": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no longer available")
        self.assertEqual(Loan.objects.filter(copy=copy).count(), 0)


class LoanReturnTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_returning_a_loan_makes_copy_available_again(self):
        copy = make_copy(status="Issued")
        loan = make_loan(copy=copy, issue_date=date(2026, 1, 1), due_date=date(2026, 1, 15))

        response = self.client.post(reverse("loan_return", args=[loan.id]), {
            "return_date": "2026-01-10",
            "returned_to": "",
            "notes": "",
        })

        self.assertEqual(response.status_code, 302)
        copy.refresh_from_db()
        loan.refresh_from_db()
        self.assertEqual(copy.status, "Available")
        self.assertEqual(loan.return_date.isoformat(), "2026-01-10")

    def test_return_date_before_issue_date_is_rejected(self):
        copy = make_copy(status="Issued")
        loan = make_loan(copy=copy, issue_date=date(2026, 1, 10), due_date=date(2026, 1, 24))

        response = self.client.post(reverse("loan_return", args=[loan.id]), {
            "return_date": "2026-01-01",
            "returned_to": "",
            "notes": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be earlier")
        copy.refresh_from_db()
        loan.refresh_from_db()
        self.assertEqual(copy.status, "Issued")
        self.assertIsNone(loan.return_date)

    def test_invalid_return_date_format_is_rejected_not_crashed(self):
        loan = make_loan(issue_date=date(2026, 1, 1), due_date=date(2026, 1, 15))

        response = self.client.post(reverse("loan_return", args=[loan.id]), {
            "return_date": "not-a-date",
            "returned_to": "",
            "notes": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid return date")


class OverdueCalculationTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_active_loan_past_due_date_shows_as_overdue(self):
        make_loan(
            issue_date=date.today() - timedelta(days=30),
            due_date=date.today() - timedelta(days=5),
        )

        response = self.client.get(reverse("loan_list") + "?status=overdue")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Overdue")

    def test_returned_loan_is_not_overdue(self):
        make_loan(
            issue_date=date.today() - timedelta(days=30),
            due_date=date.today() - timedelta(days=20),
            return_date=date.today() - timedelta(days=25),
        )

        response = self.client.get(reverse("loan_list") + "?status=overdue")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No loans found")
