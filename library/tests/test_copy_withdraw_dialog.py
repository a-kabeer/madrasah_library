"""Withdrawing a copy, in the dialog the row's other actions already use.

Withdraw used to be a navigation. Clicking it in the copy list swapped a
confirmation *page* into `#mainContent`, which threw the list away - and
with it the search, the filters, the page and the scroll position the
librarian was working in. Measured before this changed: `#copyResults`
went from present to absent, five rows to none, and the address bar from
`?search=WD&page=2` to `/book-copies/26/withdraw/?partial=results` - the
stray parameter arriving from the results container's own `hx-vals`.

Its Cancel was worse. It pointed at `book_copy_detail`, which is a
dialog-only URL, so cancelling opened the copy's details dialog *on top of
the withdraw page* rather than going back to anything.

None of the business logic moved. `book_copy_withdraw` decides exactly what
it decided before - the same status, the same refusal while the copy is
out, the same activity-log entry, the same answer to a request without
JavaScript - and these tests cover the shape of the answers a dialog needs
on top of that: the fragment, the 204-and-an-event on success, and the
re-rendered dialog on a refusal.
"""

import json
from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse

from library.models import ActivityLog, BookCopy

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

HX = {"HX-Request": "true"}


class WithdrawDialogTestCase(TestCase):

    def setUp(self):
        make_user(username="wd_admin", password="pass12345", role="Admin")
        self.client.login(username="wd_admin", password="pass12345")

        self.book = make_book(
            title="Riyad as-Salihin",
            author=make_author(name="Imam Nawawi"),
        )
        self.volume = make_volume(book=self.book, volume_number=1, title="")
        self.shelf = make_shelf(location=make_location(name="Main Hall"))

        self.copy = make_copy(
            volume=self.volume, shelf=self.shelf, copy_code="WD-0001"
        )

    def url(self):
        return reverse("book_copy_withdraw", args=[self.copy.id])

    def dialog(self):
        """The question, the way the browser asks for it.

        `?modal=1` in the path and the HX-Request header together, because
        `is_form_modal_request` wants both - one URL must not answer with
        two different bodies.
        """

        return self.client.get(self.url() + "?modal=1", headers=HX)

    def confirm(self):
        return self.client.post(self.url() + "?modal=1", headers=HX)

    def triggered(self, response):
        """The HX-Trigger event on a response, as a dict, or {}."""

        header = response.headers.get("HX-Trigger")

        return json.loads(header) if header else {}


class TheRowOpensADialog(WithdrawDialogTestCase):

    def row(self):
        return self.client.get(reverse("book_copy_list")).content.decode()

    def test_the_withdraw_action_is_wired_like_its_four_siblings(self):
        """The same five attributes Edit, Move and Delete carry."""

        body = self.row()

        marker = 'hx-get="%s?modal=1"' % self.url()

        self.assertIn(marker, body)
        self.assertIn('data-form-modal', body)

        opening = body[body.index(marker) - 400:body.index(marker) + 400]

        self.assertIn('hx-target="#formModal .modal-body"', opening)
        self.assertIn('hx-swap="innerHTML"', opening)

    def test_it_no_longer_swaps_the_page_out_from_under_the_list(self):
        """`hx-target="#mainContent"` on this action was the whole bug."""

        body = self.row()

        marker = 'href="%s"' % self.url()
        around = body[body.index(marker):body.index(marker) + 600]

        self.assertNotIn('hx-target="#mainContent"', around)

    def test_opening_it_does_not_rewrite_the_address_bar(self):
        """#copyResults declares `hx-push-url="true"` for its own fragment
        requests, and that is inherited. Left alone, opening the dialog
        pushed the withdraw URL - so Cancel, which only closes the dialog,
        left the reader on a list whose address no longer described it.
        """

        body = self.row()

        marker = 'hx-get="%s?modal=1"' % self.url()
        around = body[body.index(marker) - 400:body.index(marker) + 400]

        self.assertIn('hx-push-url="false"', around)

    def test_the_href_is_still_the_real_url(self):
        """Middle-click, Open in new tab, and no JavaScript at all."""

        self.assertIn('href="%s"' % self.url(), self.row())

    def test_a_withdrawn_copy_is_offered_nothing(self):
        self.copy.status = "Transferred"
        self.copy.save(update_fields=["status"])

        self.assertNotIn(self.url(), self.row())


class TheDialogAsksAboutOneCopy(WithdrawDialogTestCase):

    def test_it_answers_with_the_fragment_and_not_a_page(self):
        response = self.dialog()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response, "library/partials/copy_withdraw_modal.html"
        )
        self.assertTemplateNotUsed(
            response, "library/book_copy_withdraw.html"
        )

    def test_it_names_the_copy_it_is_about(self):
        """Opened from a row in a table of near-identical codes, so the
        one thing the reader must be sure of is which one it holds."""

        response = self.dialog()

        self.assertContains(response, "WD-0001")
        self.assertContains(response, "Riyad as-Salihin")
        self.assertContains(response, self.shelf.shelf_code)

    def test_it_offers_withdraw_and_a_way_out(self):
        response = self.dialog()

        self.assertContains(response, "Withdraw Copy")
        self.assertContains(response, 'data-bs-dismiss="modal"')

    def test_the_way_out_closes_the_dialog_rather_than_navigating(self):
        """Cancel used to point at `book_copy_detail`, a dialog-only URL,
        which opened a second dialog instead of going back."""

        body = self.dialog().content.decode()

        # Matched as a whole href: the details URL is a prefix of every
        # other action's, so a bare substring test is always true.
        self.assertNotIn(
            'href="%s"' % reverse("book_copy_detail", args=[self.copy.id]),
            body,
        )

    def test_a_second_click_cannot_post_a_second_withdrawal(self):
        self.assertContains(
            self.dialog(), 'hx-disabled-elt="find button[type=submit]"'
        )

    def test_a_refusal_re_renders_in_the_dialogs_own_body(self):
        """So a copy issued between opening the dialog and pressing the
        button says so here, rather than the dialog closing on nothing."""

        body = self.dialog().content.decode()

        self.assertIn('hx-target="#copyWithdrawBody"', body)
        self.assertIn('id="copyWithdrawBody"', body)

    def test_it_sets_the_dialogs_title(self):
        self.assertContains(self.dialog(), 'hx-swap-oob="true"')
        self.assertContains(self.dialog(), "Withdraw Copy?")

    def test_asking_for_it_is_not_answering_it(self):
        self.dialog()

        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Available")


class ConfirmingWithdrawsIt(WithdrawDialogTestCase):

    def test_it_withdraws_the_copy(self):
        before = BookCopy.objects.count()

        self.confirm()

        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Transferred")
        self.assertEqual(BookCopy.objects.count(), before)
        self.assertEqual(self.copy.copy_code, "WD-0001")

    def test_it_answers_with_nothing_to_swap(self):
        """204: the dialog closes and the list re-requests its own
        results, so there is no body for this response to carry."""

        response = self.confirm()

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")

    def test_it_says_what_happened_and_to_which_copy(self):
        event = self.triggered(self.confirm())

        self.assertIn("copyWithdrawn", event)
        self.assertEqual(event["copyWithdrawn"]["code"], "WD-0001")

    def test_it_is_its_own_event_rather_than_saved_or_deleted(self):
        """Neither of those says what happened: the copy was not deleted -
        keeping it and its loans is the point - and "saved" is not what a
        librarian just did."""

        event = self.triggered(self.confirm())

        self.assertNotIn("recordSaved", event)
        self.assertNotIn("recordDeleted", event)

    def test_it_is_logged_once(self):
        self.confirm()

        self.assertEqual(
            ActivityLog.objects.filter(
                action="WITHDRAW",
                entity_type="BookCopy",
                entity_id=self.copy.id,
            ).count(),
            1,
        )

    def test_posting_twice_withdraws_once(self):
        self.confirm()
        second = self.confirm()

        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Transferred")

        # The second post finds it already out of circulation, so it says
        # so in the dialog rather than writing again or firing the event.
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.triggered(second), {})
        self.assertEqual(
            ActivityLog.objects.filter(action="WITHDRAW").count(), 1
        )


class ARefusalStaysInTheDialog(WithdrawDialogTestCase):

    def setUp(self):
        super().setUp()

        self.loan = make_loan(
            copy=self.copy, borrower=make_borrower(name="Hafsa")
        )

    def test_the_dialog_explains_why_and_offers_no_withdraw(self):
        response = self.dialog()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be withdrawn")
        self.assertContains(response, "Hafsa")
        self.assertNotContains(response, 'type="submit"')

    def test_it_offers_this_loans_own_return(self):
        """The way out of the refusal, in this same dialog: taking the
        book back is what makes withdrawing it possible."""

        self.assertContains(
            self.dialog(), reverse("loan_return", args=[self.loan.id])
        )

    def test_a_refused_post_answers_with_the_dialog_and_no_event(self):
        """200 and a body, so htmx swaps the reason in. No HX-Trigger, so
        nothing closes the dialog or claims the copy was withdrawn."""

        response = self.confirm()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be withdrawn")
        self.assertEqual(self.triggered(response), {})

    def test_the_copy_and_the_loan_are_left_alone(self):
        self.confirm()

        self.copy.refresh_from_db()
        self.loan.refresh_from_db()

        self.assertEqual(self.copy.status, "Issued")
        self.assertIsNone(self.loan.return_date)
        self.assertFalse(ActivityLog.objects.filter(action="WITHDRAW"))


class ACopyAlreadyOutOfCirculation(WithdrawDialogTestCase):

    def setUp(self):
        super().setUp()

        self.copy.status = "Lost"
        self.copy.save(update_fields=["status"])

    def test_the_dialog_says_so_and_offers_no_withdraw(self):
        response = self.dialog()

        self.assertContains(response, "already marked Lost")
        self.assertNotContains(response, 'type="submit"')

    def test_it_points_at_where_the_status_is_changed(self):
        self.assertContains(
            self.dialog(), reverse("book_copy_edit", args=[self.copy.id])
        )

    def test_posting_anyway_changes_nothing(self):
        response = self.confirm()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.triggered(response), {})

        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Lost")


class TheDeleteDialogHandsOverWithoutStacking(WithdrawDialogTestCase):

    def setUp(self):
        super().setUp()

        # A returned loan on record pins the copy: `loans.copy_id` is NO
        # ACTION, so any loan at all blocks the delete - and this is the
        # case the button is for, where the history is what would be lost
        # and withdrawing is what to do instead.
        make_loan(
            copy=self.copy,
            borrower=make_borrower(name="Bilal"),
            issue_date=date.today() - timedelta(days=90),
            due_date=date.today() - timedelta(days=76),
            return_date=date.today() - timedelta(days=80),
        )

    def test_withdraw_instead_replaces_the_dialog_rather_than_leaving(self):
        response = self.client.get(
            reverse("book_copy_delete", args=[self.copy.id]) + "?modal=1",
            headers=HX,
        )

        body = response.content.decode()

        self.assertIn(self.url(), body)
        self.assertIn('hx-get="%s?modal=1"' % self.url(), body)
        self.assertIn('hx-target="#formModal .modal-body"', body)


class WithoutJavaScriptItIsStillAPage(WithdrawDialogTestCase):
    """The fragment is what htmx asks for; a plain request still gets a
    page, and the business logic answers both the same way."""

    def test_a_plain_get_renders_the_page(self):
        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "library/book_copy_withdraw.html")

    def test_a_plain_post_withdraws_and_redirects_to_the_list(self):
        response = self.client.post(self.url())

        self.assertRedirects(response, reverse("book_copy_list"))

        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "Transferred")

    def test_the_page_no_longer_opens_a_dialog_over_itself(self):
        """Its Cancel and its Back both pointed at `book_copy_detail`, a
        dialog-only URL, so both opened a dialog on top of this page."""

        body = self.client.get(self.url()).content.decode()

        self.assertNotIn(
            'href="%s"' % reverse("book_copy_detail", args=[self.copy.id]),
            body,
        )
        self.assertIn('href="%s"' % reverse("book_copy_list"), body)
