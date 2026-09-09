import re
from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse

from library.models import Loan
from library.tests.helpers import (
    make_author,
    make_book,
    make_borrower,
    make_copy,
    make_loan,
    make_user,
    make_volume,
)


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

        # Return is a dialog now: a plain POST redirects and carries the
        # reason as a message. What matters is unchanged - nothing was
        # returned and the copy is still out.
        self.assertEqual(response.status_code, 302)
        copy.refresh_from_db()
        loan.refresh_from_db()
        self.assertEqual(copy.status, "Issued")
        self.assertIsNone(loan.return_date)

        # And the dialog says why, which is where a librarian reads it.
        in_dialog = self.client.post(
            reverse("loan_return", args=[loan.id]) + "?modal=1",
            {"return_date": "2026-01-01", "returned_to": "", "notes": ""},
            headers={"HX-Request": "true"},
        )

        self.assertEqual(in_dialog.status_code, 200)
        self.assertContains(in_dialog, "cannot be earlier")

    def test_invalid_return_date_format_is_rejected_not_crashed(self):
        loan = make_loan(issue_date=date(2026, 1, 1), due_date=date(2026, 1, 15))

        response = self.client.post(reverse("loan_return", args=[loan.id]), {
            "return_date": "not-a-date",
            "returned_to": "",
            "notes": "",
        })

        # Redirects rather than re-rendering a page that is gone; the
        # point is that it did not reach the database or raise.
        self.assertEqual(response.status_code, 302)


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

        # Asserted on the rows rather than on the empty-state wording,
        # which is a sentence and can be reworded; "no overdue loans" is
        # the actual property.
        self.assertEqual(list(response.context["loans"]), [])


class LoanListStateTests(TestCase):
    """Search, status, sort, page size and paging, used together."""

    def setUp(self):
        self.staff = make_user(
            username="loanlist_u", password="pass12345", role="Librarian"
        )
        self.client.login(username="loanlist_u", password="pass12345")

        # Two loans far enough apart to order unambiguously.
        #
        # Each copy gets a book of its own with its own author: make_book
        # falls back to make_author("Test Author") and `authors.name` is
        # unique, so two default copies in one test collide on it.
        self.old = make_loan(
            copy=make_copy(
                copy_code="LIB-OLD-1",
                volume=make_volume(
                    book=make_book(
                        title="Older Book", author=make_author("Older Writer")
                    )
                ),
            ),
            borrower=make_borrower(name="Aaqib Older", phone="1111111111"),
            issue_date=date.today() - timedelta(days=40),
        )
        self.new = make_loan(
            copy=make_copy(
                copy_code="LIB-NEW-1",
                volume=make_volume(
                    book=make_book(
                        title="Newer Book", author=make_author("Newer Writer")
                    )
                ),
            ),
            borrower=make_borrower(name="Zahra Newer", phone="2222222222"),
            issue_date=date.today() - timedelta(days=1),
        )

    def get(self, **params):
        return self.client.get(reverse("loan_list"), params)

    def test_an_unknown_sort_key_falls_back_instead_of_reaching_the_orm(self):
        """The whitelist is the point.

        Handing `sort` to order_by() unchecked would let a query string
        traverse relations - `borrower__user__password` and the like - or
        raise FieldError on anything else. An unknown key has to become the
        default silently.
        """

        response = self.get(sort="borrower__name__icontains")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sort"], "issue_date")

    def test_an_unknown_direction_falls_back_too(self):
        response = self.get(sort="due_date", direction="sideways")

        self.assertEqual(response.context["direction"], "desc")

    def test_the_default_is_newest_first(self):
        """A circulation desk wants the newest issue at the top.

        The list ordered `-issue_date` before it could be sorted at all,
        and `resolve_sort` defaults ascending - so the loan list passes its
        own default direction rather than inheriting the catalogue's.
        """

        response = self.get()

        self.assertEqual(response.context["sort"], "issue_date")
        self.assertEqual(response.context["direction"], "desc")

        ids = [loan.id for loan in response.context["loans"]]
        self.assertEqual(ids[0], self.new.id)

    def test_sorting_ascending_reverses_it(self):
        response = self.get(sort="issue_date", direction="asc")

        ids = [loan.id for loan in response.context["loans"]]
        self.assertEqual(ids[0], self.old.id)

    def test_every_sortable_column_is_accepted(self):
        # No "id": the Loan number column is gone, so is its sort.
        for key in ("copy", "book", "borrower", "issue_date", "due_date"):
            with self.subTest(column=key):
                response = self.get(sort=key)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["sort"], key)

    def test_page_size_is_honoured_and_whitelisted(self):
        self.assertEqual(self.get(page_size=10).context["page_size"], 10)

        # Not an offered size: falls back rather than letting a query
        # string ask for every row in the table.
        self.assertNotEqual(self.get(page_size=99999).context["page_size"], 99999)

    def test_the_status_links_keep_the_search(self):
        """The bug this refactor was for.

        Every status button used to be a bare `?status=x`, so choosing one
        threw away the search, the borrower and both date filters.
        """

        response = self.get(search="Zahra", page_size=10)

        for option in response.context["status_options"]:
            with self.subTest(status=option["value"]):
                self.assertIn("search=Zahra", option["url"])
                self.assertIn("page_size=10", option["url"])

    def test_the_status_filter_is_the_book_lists_control(self):
        """The same pills, so two lists read the same way.

        It was a segmented group of outlined buttons - a second control
        saying what the book list's mode pills already say.
        """

        body = self.get().content.decode()

        self.assertIn('class="nav nav-pills mb-3"', body)
        self.assertNotIn('class="btn-group flex-wrap"', body)

    def test_every_status_pill_carries_an_icon(self):
        for option in self.get().context["status_options"]:
            with self.subTest(status=option["value"]):
                self.assertTrue(option["icon"].startswith("bi-"))

    def test_the_chosen_pill_is_the_marked_one(self):
        response = self.get(status="overdue")

        chosen = [
            option for option in response.context["status_options"]
            if option["active"]
        ]

        self.assertEqual([option["value"] for option in chosen], ["overdue"])
        self.assertContains(response, 'aria-current="page"')

    def test_the_pills_still_filter_exactly_as_they_did(self):
        # The markup changed; what it selects did not.
        for status in ("", "active", "overdue", "due_today", "returned"):
            with self.subTest(status=status):

                response = self.get(status=status)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["status"], status)

    def test_the_sort_links_keep_the_status_and_the_search(self):
        response = self.get(status="active", search="Zahra")

        sortable = [c for c in response.context["columns"] if c["url"]]
        self.assertTrue(sortable)

        for column in sortable:
            with self.subTest(column=column["key"]):
                self.assertIn("status=active", column["url"])
                self.assertIn("search=Zahra", column["url"])

    def test_a_sort_link_never_carries_the_page(self):
        """Re-sorting from page 4 must not land on page 4 of a new order."""

        response = self.get(page=1, sort="due_date")

        for column in response.context["columns"]:
            if column["url"]:
                self.assertNotIn("page=", column["url"])

    def test_status_and_search_narrow_together(self):
        response = self.get(status="active", search="Zahra")

        names = [loan.borrower.name for loan in response.context["loans"]]

        self.assertIn("Zahra Newer", names)
        self.assertNotIn("Aaqib Older", names)


class LoanListRowTests(TestCase):
    """The row opens the dialog, and nothing inside it competes."""

    def setUp(self):
        self.staff = make_user(
            username="loanrow_u", password="pass12345", role="Librarian"
        )
        self.client.login(username="loanrow_u", password="pass12345")

        self.loan = make_loan(
            copy=make_copy(copy_code="LIB-ROW-1"),
            borrower=make_borrower(name="Row Borrower"),
        )

    def body(self):
        return self.client.get(reverse("loan_list")).content.decode()

    def test_the_row_carries_the_modal_wiring(self):
        """app.js reads these; a missing one leaves the row dead silently."""

        body = self.body()

        self.assertIn("data-loan-row", body)
        self.assertIn(
            "%s?modal=1" % reverse("loan_detail", args=[self.loan.id]), body
        )

    def test_the_row_is_reachable_from_the_keyboard(self):
        """Through a real link in the first cell, not through the row.

        The row used to carry `role="button"` and `tabindex="0"`, which was
        invalid markup: an element with a widget role may not contain
        focusable descendants, and this row contains Renew. axe-core
        reported it as `nested-interactive` on twelve pages - a screen
        reader was told the row was a button and then found a button inside
        it. So the keyboard path is an anchor to the same dialog, which is
        what a keyboard and a screen reader both already know how to use.
        """

        body = self.body()
        opens_the_loan = "%s?modal=1" % reverse(
            "loan_detail", args=[self.loan.id])

        self.assertNotIn('role="button"', body)
        self.assertNotIn('tabindex="0"', body)

        # An <a href> to the dialog, carrying the modal wiring, inside the
        # row - so Tab reaches it and Enter opens the same thing a click
        # anywhere else in the row does.
        self.assertRegex(
            body,
            r'<a[^>]*href="%s"[^>]*data-form-modal' % re.escape(
                opens_the_loan),
        )

    def test_the_actions_are_excluded_from_the_row_click(self):
        """Renew must keep its own behaviour.

        app.js ignores clicks inside [data-row-actions], so the cell has to
        carry it - otherwise clicking Renew would open the loan dialog
        behind it instead.
        """

        self.assertIn("data-row-actions", self.body())

    def test_the_actions_column_offers_renew_and_nothing_else(self):
        """Taking a book back belongs to the Return Books page now.

        That page returns a whole stack in one action, which a per-row
        button never could, so the row keeps the one action that is still
        about a single loan.
        """

        body = self.body()

        self.assertIn(reverse("loan_renew", args=[self.loan.id]), body)
        self.assertNotIn(reverse("loan_return", args=[self.loan.id]), body)

    def test_a_returned_loan_offers_nothing(self):
        # Unchanged: there is nothing to renew.
        self.loan.return_date = self.loan.due_date
        self.loan.save(update_fields=["return_date"])

        self.assertNotIn(
            reverse("loan_renew", args=[self.loan.id]), self.body()
        )

    def test_an_assistant_is_not_shown_a_button_that_refuses_them(self):
        """`loan_renew` is Admin and Librarian only.

        Return used to be the action an Assistant's rows had, and it is
        gone, so without this they would be looking at the single button
        their row has and getting a 403 from it. The view is still what
        enforces the rule - this only stops it being offered.
        """

        assistant = make_user(
            username="pill_assistant", password="pass12345", role="Assistant"
        )
        self.client.force_login(assistant)

        body = self.client.get(reverse("loan_list")).content.decode()

        self.assertNotIn(reverse("loan_renew", args=[self.loan.id]), body)

        # And the view refuses it even when asked directly.
        self.assertEqual(
            self.client.get(
                reverse("loan_renew", args=[self.loan.id])
            ).status_code,
            403,
        )

    def test_book_and_borrower_are_no_longer_competing_links(self):
        """They were two small targets inside a clickable row.

        Both are now in the dialog's footer instead, so a click anywhere in
        the row resolves to one action.
        """

        body = self.body()

        self.assertNotIn(reverse("book_detail", args=[self.loan.copy.volume.book.id]), body)
        self.assertNotIn(reverse("borrower_detail", args=[self.loan.borrower.id]), body)

    def test_the_redundant_view_action_is_gone(self):
        """The row itself is the way in now."""

        body = self.body()

        # The bare detail URL, with no ?modal=1, is what View used to be.
        self.assertNotIn(
            'href="%s"' % reverse("loan_detail", args=[self.loan.id]), body
        )


class LoanDialogTests(TestCase):
    """The dialog is the loan page's view, in a different template."""

    def setUp(self):
        self.staff = make_user(
            username="loandlg_u", password="pass12345", role="Librarian"
        )
        self.client.login(username="loandlg_u", password="pass12345")

        self.loan = make_loan(
            copy=make_copy(copy_code="LIB-DLG-1"),
            borrower=make_borrower(name="Dialog Borrower"),
        )

    def dialog(self):
        return self.client.get(
            reverse("loan_detail", args=[self.loan.id]) + "?modal=1",
            headers={"HX-Request": "true"},
        )

    def test_it_answers_the_fragment_when_asked(self):
        response = self.dialog()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response, "library/partials/loan_detail_modal.html"
        )

    def test_without_the_parameter_it_redirects_to_the_list(self):
        """There is no loan page any more - the dialog is all of it.

        The URL still has to answer, because several pages link to it and
        somebody may have bookmarked one, so a plain GET lands on the list
        rather than 404ing.
        """

        response = self.client.get(reverse("loan_detail", args=[self.loan.id]))

        self.assertRedirects(response, reverse("loan_list"))

    def test_it_shows_the_four_sections(self):
        response = self.dialog()

        for heading in ("Loan", "Book", "Borrower", "Staff"):
            with self.subTest(section=heading):
                self.assertContains(response, heading)

    def test_it_offers_the_two_ways_out(self):
        """The book and the borrower. There is no loan page to offer."""

        response = self.dialog()

        self.assertContains(
            response, reverse("book_detail", args=[self.loan.copy.volume.book.id])
        )
        self.assertContains(
            response, reverse("borrower_detail", args=[self.loan.borrower.id])
        )

    def test_it_does_not_offer_a_full_page_that_no_longer_exists(self):
        self.assertNotContains(self.dialog(), "Full loan details")

    def test_the_copy_code_is_in_it(self):
        self.assertContains(self.dialog(), "LIB-DLG-1")
