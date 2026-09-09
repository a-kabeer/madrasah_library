from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse

from library.models import Loan
from library.tests.helpers import (
    make_user, make_copy, make_borrower, make_loan, make_author, make_book, make_volume,
)


def make_copy_with_author(copy_code, status, author_name):
    """Create a copy with a unique author to avoid unique constraint violations."""
    author = make_author(name=author_name)
    book = make_book(author=author)
    volume = make_volume(book=book)
    return make_copy(copy_code=copy_code, status=status, volume=volume)


class CirculationDashboardTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_circulation_dashboard_accessible(self):
        response = self.client.get(reverse("circulation_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Circulation")
        self.assertContains(response, "Issue Books")
        self.assertContains(response, "Return Books")

    def test_circulation_dashboard_shows_counts(self):
        borrower = make_borrower()
        copy2 = make_copy_with_author("LIB-000002", "Issued", "Dashboard Author 1")
        copy3 = make_copy_with_author("LIB-000003", "Issued", "Dashboard Author 2")
        copy4 = make_copy_with_author("LIB-000004", "Issued", "Dashboard Author 3")

        make_loan(copy=copy2, borrower=borrower,
                  issue_date=date.today() - timedelta(days=5),
                  due_date=date.today() + timedelta(days=9))
        make_loan(copy=copy3, borrower=borrower,
                  issue_date=date.today() - timedelta(days=20),
                  due_date=date.today() - timedelta(days=6))
        make_loan(copy=copy4, borrower=borrower,
                  issue_date=date.today() - timedelta(days=14),
                  due_date=date.today())

        response = self.client.get(reverse("circulation_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_count"], 3)
        self.assertEqual(response.context["overdue_count"], 1)
        self.assertEqual(response.context["due_today_count"], 1)


class MultiCopyIssueTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")
        self.borrower = make_borrower()

    def test_issue_multiple_copies(self):
        copy1 = make_copy_with_author("LIB-000001", "Available", "Multi Issue Author 1")
        copy2 = make_copy_with_author("LIB-000002", "Available", "Multi Issue Author 2")
        copy3 = make_copy_with_author("LIB-000003", "Available", "Multi Issue Author 3")

        response = self.client.post(reverse("loan_add"), {
            "borrower": self.borrower.id,
            "copies": [copy1.id, copy2.id, copy3.id],
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=14)).isoformat(),
            "issued_by": "",
            "notes": "",
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Loan.objects.filter(borrower=self.borrower).count(), 3)

        for copy in [copy1, copy2, copy3]:
            copy.refresh_from_db()
            self.assertEqual(copy.status, "Issued")

    def test_issue_fails_if_any_copy_unavailable(self):
        copy1 = make_copy_with_author("LIB-000011", "Available", "Partial Fail Author 1")
        copy2 = make_copy_with_author("LIB-000012", "Issued", "Partial Fail Author 2")

        response = self.client.post(reverse("loan_add"), {
            "borrower": self.borrower.id,
            "copies": [copy1.id, copy2.id],
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=14)).isoformat(),
            "issued_by": "",
            "notes": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no longer available")
        copy1.refresh_from_db()
        self.assertEqual(copy1.status, "Available")
        self.assertEqual(Loan.objects.filter(copy=copy1).count(), 0)



    def test_already_issued_copy_rejected_in_creation(self):
        """Double-issue protection: issue an already-issued copy directly."""
        copy = make_copy_with_author("LIB-000013", "Available", "Double Issue Author")
        make_loan(copy=copy, borrower=self.borrower,
                  issue_date=date.today(),
                  due_date=date.today() + timedelta(days=14))
        copy.status = "Issued"
        copy.save()

        borrower2 = make_borrower(name="Second Borrower", phone="9876543210")
        response = self.client.post(reverse("loan_add"), {
            "borrower": borrower2.id,
            "copies": [copy.id],
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=14)).isoformat(),
            "issued_by": "",
            "notes": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no longer available")
        self.assertEqual(Loan.objects.filter(copy=copy, return_date__isnull=True).count(), 1)


class ReturnLifecycleTests(TestCase):
    """Test the full lifecycle: issue -> return -> reissue."""

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")
        self.borrower1 = make_borrower(name="Borrower One", phone="1111111111")
        self.borrower2 = make_borrower(name="Borrower Two", phone="2222222222")
        self.copy = make_copy_with_author("LIB-000041", "Available", "Lifecycle Author")

    def test_return_then_reissue(self):
        response = self.client.post(reverse("loan_add"), {
            "borrower": self.borrower1.id,
            "copies": [self.copy.id],
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=14)).isoformat(),
            "issued_by": "",
            "notes": "",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Loan.objects.filter(copy=self.copy, return_date__isnull=True).count(), 1
        )

        loan = Loan.objects.get(copy=self.copy, return_date__isnull=True)

        response = self.client.post(reverse("loan_return", args=[loan.id]), {
            "return_date": date.today().isoformat(),
            "returned_to": "",
            "notes": "",
        })
        self.assertEqual(response.status_code, 302)
        loan.refresh_from_db()
        self.assertIsNotNone(loan.return_date)
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Available")
        self.assertEqual(Loan.objects.filter(copy=self.copy).count(), 1)

        response = self.client.post(reverse("loan_add"), {
            "borrower": self.borrower2.id,
            "copies": [self.copy.id],
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=14)).isoformat(),
            "issued_by": "",
            "notes": "",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Loan.objects.filter(copy=self.copy).count(), 2)
        self.assertEqual(
            Loan.objects.filter(copy=self.copy, return_date__isnull=True).count(), 1
        )
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Issued")


class DoubleIssueProtectionTests(TestCase):
    """Verify the same copy cannot have two active Loans."""

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")
        self.borrower1 = make_borrower(name="Borrower A", phone="1111111111")
        self.borrower2 = make_borrower(name="Borrower B", phone="2222222222")
        self.copy = make_copy_with_author("LIB-000051", "Available", "Double Issue Author")

    def test_same_copy_two_borrowers_only_one_loan(self):
        """Issue the same copy to borrower A, then try to issue to borrower B."""
        response = self.client.post(reverse("loan_add"), {
            "borrower": self.borrower1.id,
            "copies": [self.copy.id],
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=14)).isoformat(),
            "issued_by": "",
            "notes": "",
        })
        self.assertEqual(response.status_code, 302)

        response = self.client.post(reverse("loan_add"), {
            "borrower": self.borrower2.id,
            "copies": [self.copy.id],
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=14)).isoformat(),
            "issued_by": "",
            "notes": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no longer available")
        self.assertEqual(
            Loan.objects.filter(copy=self.copy, return_date__isnull=True).count(), 1
        )





class ReturnByCopyCodeTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_return_lookup_finds_loan(self):
        borrower = make_borrower()
        copy = make_copy_with_author("LIB-000021", "Issued", "Return Lookup Author")
        loan = make_loan(copy=copy, borrower=borrower,
                         issue_date=date.today() - timedelta(days=5),
                         due_date=date.today() + timedelta(days=9))

        response = self.client.get(reverse("circulation_return_lookup"),
                                   {"copy_code": "LIB-000021"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["loan"])
        self.assertEqual(response.context["loan"].id, loan.id)

    def test_return_lookup_invalid_copy_code(self):
        response = self.client.get(reverse("circulation_return_lookup"),
                                   {"copy_code": "INVALID-CODE"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["loan"])


class LoanRenewalTests(TestCase):

    def setUp(self):
        self.admin = make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

    def test_renew_active_loan(self):
        borrower = make_borrower()
        copy = make_copy_with_author("LIB-000031", "Issued", "Renewal Author")
        original_due_date = date.today() + timedelta(days=5)
        loan = make_loan(copy=copy, borrower=borrower,
                         issue_date=date.today() - timedelta(days=9),
                         due_date=original_due_date)

        response = self.client.post(reverse("loan_renew", args=[loan.id]))

        self.assertEqual(response.status_code, 302)
        loan.refresh_from_db()
        expected_due_date = original_due_date + timedelta(days=14)
        self.assertEqual(loan.due_date, expected_due_date)

    def test_cannot_renew_returned_loan(self):
        borrower = make_borrower()
        copy = make_copy_with_author("LIB-000032", "Available", "Returned Renewal Author")
        loan = make_loan(copy=copy, borrower=borrower,
                         issue_date=date.today() - timedelta(days=14),
                         due_date=date.today(),
                         return_date=date.today())

        # Renew is a dialog now. The refusal is unchanged; a plain POST
        # redirects with it as a message, and the dialog shows it.
        response = self.client.post(reverse("loan_renew", args=[loan.id]))
        self.assertEqual(response.status_code, 302)

        in_dialog = self.client.get(
            reverse("loan_renew", args=[loan.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )
        self.assertContains(in_dialog, "already been returned")

        loan.refresh_from_db()
        self.assertEqual(loan.due_date, date.today())
