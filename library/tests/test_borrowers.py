"""Borrower list, search, filters, profile, and deletion safety.

Covers the counts the list now carries and where they come from, that
overdue is derived from the loans rather than stored anywhere, that editing
a borrower never touches a loan, and that a borrower with history cannot be
deleted — but can be deactivated.
"""

from datetime import date, timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library.models import Borrower, Loan

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


class BorrowerTestCase(TestCase):

    def setUp(self):
        make_user(username="admin_u", password="pass12345", role="Admin")
        self.client.login(username="admin_u", password="pass12345")

        self.author = make_author(name="Imam Nawawi")
        self.book = make_book(title="Riyad as-Salihin", author=self.author)
        self.volume = make_volume(book=self.book, volume_number=1, title="")

        self.hall = make_location(name="Main Hall")
        self.shelf = make_shelf(location=self.hall, shelf_code="A-1")

        self.today = timezone.now().date()

    def a_copy(self, code):
        return make_copy(
            volume=self.volume, shelf=self.shelf, copy_code=code
        )

    def as_assistant(self):
        self.client.logout()
        make_user(username="assist", password="pass12345", role="Assistant")
        self.client.login(username="assist", password="pass12345")

    def dialog(self, url_name, *args, **params):
        """Fetch a borrower dialog the way the browser asks for one.

        `?modal=1` and the HX-Request header together - `is_form_modal_request`
        wants both, so that one URL never returns two different bodies.
        """

        return self.client.get(
            reverse(url_name, args=args) + "?modal=1",
            params,
            headers={"HX-Request": "true"},
        )

    def post_dialog(self, url_name, *args, **data):
        # `url_name` rather than `name`: these are called with a payload
        # splatted in, and a borrower's payload has a `name` field.
        return self.client.post(
            reverse(url_name, args=args) + "?modal=1",
            data,
            headers={"HX-Request": "true"},
        )


class BorrowerListTests(BorrowerTestCase):

    def setUp(self):
        super().setUp()

        self.quiet = make_borrower(
            name="Quiet Reader", phone="0300-1", borrower_type="Student"
        )

        self.holder = make_borrower(
            name="Current Holder", phone="0300-2", borrower_type="Teacher"
        )
        make_loan(copy=self.a_copy("BR-1"), borrower=self.holder)
        make_loan(copy=self.a_copy("BR-2"), borrower=self.holder)

        self.late = make_borrower(
            name="Late Reader", phone="0300-3", borrower_type="Staff"
        )
        make_loan(
            copy=self.a_copy("BR-3"),
            borrower=self.late,
            issue_date=self.today - timedelta(days=40),
            due_date=self.today - timedelta(days=6),
        )

        self.retired = make_borrower(
            name="Retired Reader", phone="0300-4",
            borrower_type="Other", is_active=False,
        )

        self.url = reverse("borrower_list")

    def get(self, **params):
        return self.client.get(self.url, params)

    def names(self, response):
        return sorted(b.name for b in response.context["borrowers"])

    def counts(self, response):
        return {
            b.name: (b.active_loans, b.overdue_loans)
            for b in response.context["borrowers"]
        }

    def test_it_renders_on_the_shared_layout(self):
        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "library/base.html")

    def test_loan_and_overdue_counts_are_right(self):
        self.assertEqual(
            self.counts(self.get()),
            {
                "Current Holder": (2, 0),
                "Late Reader": (1, 1),
                "Quiet Reader": (0, 0),
                "Retired Reader": (0, 0),
            },
        )

    def test_a_returned_loan_is_not_counted_as_out(self):
        make_loan(
            copy=self.a_copy("BR-9"),
            borrower=self.quiet,
            issue_date=date(2020, 1, 1),
            due_date=date(2020, 1, 15),
            return_date=date(2020, 1, 10),
        )

        self.assertEqual(self.counts(self.get())["Quiet Reader"], (0, 0))

    def test_search_covers_the_fields_that_exist(self):
        self.late.registration_no = "REG-77"
        self.late.department = "Hadith Studies"
        self.late.save()

        for term in ("Late", "0300-3", "REG-77", "Hadith Studies"):
            with self.subTest(term=term):
                self.assertEqual(self.names(self.get(search=term)),
                                 ["Late Reader"])

    def test_the_status_filter_uses_the_existing_field(self):
        self.assertNotIn("Retired Reader", self.names(self.get(status="active")))
        self.assertEqual(self.names(self.get(status="inactive")),
                         ["Retired Reader"])

    def test_the_type_filter_still_works(self):
        self.assertEqual(self.names(self.get(borrower_type="Teacher")),
                         ["Current Holder"])

    def test_the_loan_activity_filters(self):
        for activity, expected in (
            ("has_loans", ["Current Holder", "Late Reader"]),
            ("no_loans", ["Quiet Reader", "Retired Reader"]),
            ("overdue", ["Late Reader"]),
            ("no_overdue", ["Current Holder", "Quiet Reader",
                            "Retired Reader"]),
        ):
            with self.subTest(activity=activity):
                self.assertEqual(self.names(self.get(activity=activity)),
                                 expected)

    def test_borrower_status_and_loan_activity_are_separate_filters(self):
        # An inactive borrower can still be holding something, and the two
        # questions must not be answered by one control.
        make_loan(copy=self.a_copy("BR-8"), borrower=self.retired)

        response = self.get(status="inactive", activity="has_loans")

        self.assertEqual(self.names(response), ["Retired Reader"])

    def test_an_unknown_activity_value_is_ignored(self):
        self.assertEqual(len(self.names(self.get(activity="nonsense"))), 4)

    def test_it_offers_a_way_to_the_overdue_borrowers(self):
        response = self.get()

        self.assertEqual(response.context["overdue_borrowers"], 1)
        self.assertContains(response, "?activity=overdue")

    def test_the_rows_per_page_select_shows_what_is_in_force(self):
        """It showed the first option whatever the size actually was.

        `page_size_options` marks the active one by comparing against the
        value it is handed, and it was handed the request.
        """

        response = self.get(page_size=10)

        self.assertEqual(response.context["page_size"], 10)
        self.assertEqual(
            [o["value"] for o in response.context["page_size_options"] if o["active"]],
            [10],
        )

    def test_each_row_opens_the_profile(self):
        # `data-borrower-url`, not `data-row-url`: the row opens the
        # profile dialog now rather than navigating to a page, and app.js
        # reads a different attribute for each of those two behaviours.
        self.assertContains(
            self.get(),
            'data-borrower-url="%s?modal=1"' % reverse(
                "borrower_detail", args=[self.holder.id]
            ),
        )

    def test_the_action_buttons_are_kept_out_of_the_row_click(self):
        self.assertContains(self.get(), "data-row-actions")

    def test_no_database_ids_are_shown_as_text(self):
        body = self.get().content.decode()

        self.assertNotIn("<td>%d</td>" % self.holder.id, body)

    def test_it_paginates(self):
        for index in range(30):
            make_borrower(name="Extra %02d" % index, phone="055-%d" % index)

        response = self.get()

        self.assertEqual(response.context["paginator"].count, 34)
        self.assertEqual(len(response.context["borrowers"]), 25)

    def test_the_page_costs_the_same_however_many_borrowers(self):
        # The counts are annotated, not asked for per row.
        with CaptureQueriesContext(connection) as few:
            self.get()

        for index in range(15):
            extra = make_borrower(name="More %02d" % index, phone="066-%d" % index)
            make_loan(copy=self.a_copy("BRX-%d" % index), borrower=extra)

        with CaptureQueriesContext(connection) as many:
            self.get()

        self.assertEqual(len(few), len(many))

    def test_the_counts_are_live_not_cached(self):
        # They used to come from a five-minute cache, which would have
        # shown a stale count of what someone is holding.
        self.assertEqual(self.counts(self.get())["Quiet Reader"], (0, 0))

        make_loan(copy=self.a_copy("BR-FRESH"), borrower=self.quiet)

        self.assertEqual(self.counts(self.get())["Quiet Reader"], (1, 0))

    def test_an_assistant_is_not_offered_deletion(self):
        self.as_assistant()

        response = self.get()

        self.assertFalse(response.context["can_delete"])
        self.assertNotContains(
            response, reverse("borrower_delete", args=[self.holder.id])
        )

        # But may still add and edit — the existing permission model.
        self.assertContains(response, reverse("borrower_add"))
        self.assertContains(
            response, reverse("borrower_edit", args=[self.holder.id])
        )


class BorrowerProfileTests(BorrowerTestCase):

    def setUp(self):
        super().setUp()

        self.borrower = make_borrower(
            name="Ahmad Ali", phone="0300-9", borrower_type="Student"
        )
        self.borrower.registration_no = "REG-1"
        self.borrower.department = "Fiqh"
        self.borrower.save()

        self.out = make_loan(
            copy=self.a_copy("PR-1"), borrower=self.borrower
        )

        self.late = make_loan(
            copy=self.a_copy("PR-2"),
            borrower=self.borrower,
            issue_date=self.today - timedelta(days=30),
            due_date=self.today - timedelta(days=9),
        )

        self.returned = make_loan(
            copy=self.a_copy("PR-3"),
            borrower=self.borrower,
            issue_date=date(2021, 1, 1),
            due_date=date(2021, 1, 15),
            return_date=date(2021, 1, 12),
        )

    def get(self, **params):
        # The profile is a dialog now; there is no page to fetch.
        return self.dialog("borrower_detail", self.borrower.id, **params)

    def test_it_shows_the_basic_information(self):
        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ahmad Ali")
        self.assertContains(response, "0300-9")
        self.assertContains(response, "REG-1")
        self.assertContains(response, "Fiqh")

    def test_it_counts_what_is_out_and_what_is_late(self):
        response = self.get()

        self.assertEqual(response.context["active_count"], 2)
        self.assertEqual(response.context["overdue_count"], 1)

    def test_the_overdue_days_come_from_the_due_date(self):
        response = self.get()

        late = next(
            loan for loan in response.context["current_loans"]
            if loan.id == self.late.id
        )

        self.assertEqual(late.days_overdue, 9)

    def test_current_loans_show_the_book_copy_and_dates(self):
        response = self.get()

        codes = [loan.copy.copy_code for loan in response.context["current_loans"]]

        self.assertEqual(sorted(codes), ["PR-1", "PR-2"])
        self.assertContains(response, "Riyad as-Salihin")
        self.assertContains(response, "PR-1")

    def test_a_returned_loan_is_not_in_current_loans(self):
        codes = [
            loan.copy.copy_code
            for loan in self.get().context["current_loans"]
        ]

        self.assertNotIn("PR-3", codes)

    def test_the_history_holds_everything_newest_first(self):
        response = self.get()

        self.assertEqual(response.context["history_count"], 3)

        issued = [loan.issue_date for loan in response.context["history"]]

        self.assertEqual(issued, sorted(issued, reverse=True))

    def test_the_history_is_the_recent_ten_and_says_how_many_there_are(self):
        """It was paginated; a dialog cannot page without a second copy of
        the pagination partial that knows how to swap a modal body.

        So it shows the ten most recent and says the total, and the whole
        history is one button away on the loan list - which pages already.
        """

        for index in range(30):
            make_loan(
                copy=self.a_copy("PRX-%d" % index),
                borrower=self.borrower,
                issue_date=date(2019, 1, 1),
                due_date=date(2019, 1, 15),
                return_date=date(2019, 1, 10),
            )

        response = self.get()

        self.assertEqual(response.context["history_count"], 33)
        self.assertEqual(len(response.context["history"]), 10)
        self.assertContains(response, "most recent of")

    def test_the_page_costs_the_same_however_long_the_history(self):
        with CaptureQueriesContext(connection) as few:
            self.get()

        for index in range(20):
            make_loan(
                copy=self.a_copy("PRY-%d" % index),
                borrower=self.borrower,
                issue_date=date(2018, 1, 1),
                due_date=date(2018, 1, 15),
                return_date=date(2018, 1, 10),
            )

        with CaptureQueriesContext(connection) as many:
            self.get()

        self.assertEqual(len(few), len(many))

    def test_the_book_and_the_copy_are_named_without_being_links(self):
        """Inside a dialog a link is a trap: following it throws the
        dialog away, and with it whatever the reader was in the middle of.

        So the tables name the book and the copy in plain text, and the
        one way onward is the footer's loan list.
        """

        response = self.get()

        self.assertContains(response, "Riyad as-Salihin")
        self.assertContains(response, "PR-1")

        self.assertNotContains(
            response, reverse("book_copy_detail", args=[self.out.copy_id])
        )
        self.assertNotContains(
            response, reverse("loan_detail", args=[self.out.id])
        )

    def test_the_whole_history_is_one_button_away(self):
        response = self.get()

        self.assertContains(
            response,
            "%s?borrower=%d" % (reverse("loan_list"), self.borrower.id),
        )

    def test_a_plain_request_lands_on_the_list(self):
        """Several pages link to a borrower; a stale link should land."""

        response = self.client.get(
            reverse("borrower_detail", args=[self.borrower.id])
        )

        self.assertRedirects(response, reverse("borrower_list"))

    def test_status_and_activity_are_shown_apart(self):
        response = self.get()

        # The badge is the borrower's standing; the tabs are the loans.
        self.assertContains(response, "Active")
        self.assertContains(response, "On loan")
        self.assertContains(response, "overdue")

    def test_a_borrower_with_no_loans_says_so(self):
        empty = make_borrower(name="Nobody", phone="0300-0")

        response = self.dialog("borrower_detail", empty.id)

        self.assertEqual(response.context["active_count"], 0)
        self.assertContains(response, "never borrowed anything")

    def test_an_assistant_is_not_offered_deletion(self):
        self.as_assistant()

        response = self.get()

        self.assertFalse(response.context["can_delete"])
        self.assertNotContains(
            response, reverse("borrower_delete", args=[self.borrower.id])
        )

    def test_what_is_out_is_only_this_borrower_s(self):
        other = make_borrower(name="Someone Else", phone="0301-7")
        theirs = make_loan(copy=self.a_copy("PR-9"), borrower=other)

        response = self.get()

        self.assertEqual(
            {loan.id for loan in response.context["current_loans"]},
            {self.out.id, self.late.id},
        )
        self.assertNotIn(
            theirs.id,
            {loan.id for loan in response.context["current_loans"]},
        )

    def test_the_history_is_only_this_borrower_s(self):
        other = make_borrower(name="Someone Else", phone="0301-8")
        theirs = make_loan(copy=self.a_copy("PR-8"), borrower=other)

        response = self.get()

        listed = {loan.id for loan in response.context["history"]}

        self.assertEqual(
            listed, {self.out.id, self.late.id, self.returned.id}
        )
        self.assertNotIn(theirs.id, listed)


class ProfileQuickActionTests(BorrowerTestCase):
    """Edit, issue, and the loans list - through the routes that exist.

    None of these is a second way of doing the thing: each is a link into a
    workflow that already owns it, carrying the borrower along.
    """

    def setUp(self):
        super().setUp()

        self.borrower = make_borrower(name="Bilal", phone="0302-1")
        self.copy = self.a_copy("QA-1")

    def get(self, borrower=None):
        return self.dialog(
            "borrower_detail", (borrower or self.borrower).id
        )

    def test_the_profile_offers_editing(self):
        self.assertContains(
            self.get(), reverse("borrower_edit", args=[self.borrower.id])
        )

    def test_the_issue_link_carries_this_borrower(self):
        self.assertContains(
            self.get(),
            "%s?borrower=%d"
            % (reverse("circulation_issue"), self.borrower.id),
        )

    def test_following_it_arrives_with_the_borrower_chosen(self):
        response = self.client.get(
            reverse("circulation_issue"), {"borrower": self.borrower.id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["borrower_id"], str(self.borrower.id)
        )
        self.assertEqual(response.context["borrower_name"], "Bilal")

    def test_their_loans_are_one_button_away_whether_or_not_they_have_any(self):
        """It used to appear only once there was something to see, which
        meant the footer changed shape between two borrowers.

        The button is always there now and goes to the loan list filtered
        to them - which says "no loans found" perfectly well on its own.
        """

        for stage in ("before", "after"):
            with self.subTest(stage=stage):
                self.assertContains(
                    self.get(),
                    "%s?borrower=%d"
                    % (reverse("loan_list"), self.borrower.id),
                )

            if stage == "before":
                make_loan(copy=self.copy, borrower=self.borrower)

    def test_the_active_loans_link_shows_only_this_borrower(self):
        mine = make_loan(copy=self.copy, borrower=self.borrower)

        other = make_borrower(name="Not Bilal", phone="0302-2")
        theirs = make_loan(copy=self.a_copy("QA-2"), borrower=other)

        response = self.client.get(
            reverse("loan_list"),
            {"borrower": self.borrower.id, "status": "active"},
        )

        listed = {loan.id for loan in response.context["loans"]}

        self.assertIn(mine.id, listed)
        self.assertNotIn(theirs.id, listed)

    def test_an_assistant_is_offered_the_same_three(self):
        # All three roles issue and return, and all three edit a borrower,
        # so nothing here is hidden from an Assistant. The check is that the
        # page and the views agree - an offered action that answers 403 is
        # the failure this guards.
        make_loan(copy=self.copy, borrower=self.borrower)
        self.as_assistant()

        response = self.get()

        # Edit is a dialog, so "reachable" means reachable as one; the
        # other two are still pages. Either way the point is unchanged:
        # every action the footer offers actually answers an Assistant.
        for label, url, opened in (
            (
                "edit",
                reverse("borrower_edit", args=[self.borrower.id]),
                self.dialog("borrower_edit", self.borrower.id),
            ),
            (
                "issue",
                reverse("circulation_issue"),
                self.client.get(reverse("circulation_issue")),
            ),
            (
                "loans",
                reverse("loan_list"),
                self.client.get(reverse("loan_list")),
            ),
        ):
            with self.subTest(action=label):
                self.assertContains(response, url)
                self.assertEqual(opened.status_code, 200)


class InactiveBorrowerIssueTests(BorrowerTestCase):
    """An inactive borrower stays out of the issue workflow."""

    def setUp(self):
        super().setUp()

        self.borrower = make_borrower(
            name="Suspended", phone="0303-1", is_active=False
        )
        self.copy = self.a_copy("IN-1")

    def test_the_profile_says_so_and_offers_no_issue_link(self):
        response = self.dialog("borrower_detail", self.borrower.id)

        self.assertContains(response, "Inactive")
        self.assertContains(response, "cannot receive new loans")
        self.assertNotContains(
            response,
            "%s?borrower=%d"
            % (reverse("circulation_issue"), self.borrower.id),
        )

    def test_a_link_cannot_preselect_them_either(self):
        # The button is not offered, but the URL can still be typed or kept
        # from before the borrower was deactivated. Nothing is chosen, so
        # the form cannot be filled in and then refused at the last step.
        response = self.client.get(
            reverse("circulation_issue"), {"borrower": self.borrower.id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["borrower_id"], "")
        self.assertEqual(response.context["borrower_name"], "")

    def test_and_the_view_still_refuses_the_loan_itself(self):
        # The rule this all follows from, unchanged.
        response = self.client.post(
            reverse("circulation_issue"),
            {
                "borrower": self.borrower.id,
                "copies": [self.copy.id],
                "issue_date": self.today.isoformat(),
                "due_date": (self.today + timedelta(days=7)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "inactive borrower")
        self.assertFalse(
            Loan.objects.filter(borrower=self.borrower).exists()
        )

    def test_an_active_borrower_is_preselected_normally(self):
        active = make_borrower(name="Allowed", phone="0303-2")

        response = self.client.get(
            reverse("circulation_issue"), {"borrower": active.id}
        )

        self.assertEqual(response.context["borrower_name"], "Allowed")

    def test_a_nonsense_borrower_parameter_is_not_a_crash(self):
        for value in ("abc", "", "-1", "999999"):
            with self.subTest(value=value):

                response = self.client.get(
                    reverse("circulation_issue"), {"borrower": value}
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["borrower_name"], "")


class AddAndEditTests(BorrowerTestCase):

    def payload(self, **overrides):
        data = {
            "name": "New Reader",
            "phone": "0311-1",
            "borrower_type": "Student",
            "registration_no": "",
            "department": "",
            "address": "",
            "notes": "",
        }
        data.update(overrides)
        return data

    def test_a_valid_borrower_is_created(self):
        response = self.client.post(
            reverse("borrower_add"), self.payload()
        )

        self.assertEqual(response.status_code, 302)

        borrower = Borrower.objects.get(name="New Reader")

        self.assertEqual(borrower.phone, "0311-1")
        self.assertTrue(borrower.is_active)

    def test_the_required_fields_are_enforced(self):
        # Each message sits under its own field now, rather than one
        # sentence above the form naming both when one was missing.
        for field, message in (
            ("name", "Enter the borrower&#x27;s name."),
            ("phone", "Enter a phone number."),
        ):
            with self.subTest(field=field):
                response = self.post_dialog(
                    "borrower_add", **self.payload(**{field: ""})
                )

                self.assertContains(response, message)
                self.assertIn(field, response.context["errors"])
                self.assertFalse(
                    Borrower.objects.filter(name="New Reader").exists()
                )

    def test_an_invalid_type_is_refused(self):
        response = self.post_dialog(
            "borrower_add", **self.payload(borrower_type="Wizard")
        )

        self.assertContains(response, "what kind of borrower")
        self.assertFalse(Borrower.objects.filter(name="New Reader").exists())

    def test_a_field_longer_than_its_column_is_refused_not_a_crash(self):
        """`phone` is varchar(30); reaching it with more raised DataError,
        which is a 500 rather than an answer."""

        response = self.post_dialog(
            "borrower_add", **self.payload(phone="9" * 40)
        )

        self.assertContains(response, "Shorten it to 30")
        self.assertFalse(Borrower.objects.filter(name="New Reader").exists())

    def test_a_repeated_phone_is_refused_and_not_merged(self):
        make_borrower(name="Already Here", phone="0311-1")

        response = self.post_dialog("borrower_add", **self.payload())

        # It names the record it matched, and does not merge them: two
        # people can share a phone, so the choice is the librarian's.
        self.assertContains(response, "Already Here")
        self.assertContains(response, "already has this phone number")
        self.assertEqual(Borrower.objects.filter(phone="0311-1").count(), 1)

    def test_a_repeated_registration_number_is_refused(self):
        # `registration_no` carries a partial UNIQUE index, so this would
        # otherwise reach the database as an IntegrityError.
        existing = make_borrower(name="Already Here", phone="0311-9")
        existing.registration_no = "REG-DUP"
        existing.save()

        response = self.post_dialog(
            "borrower_add", **self.payload(registration_no="reg-dup")
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already has this registration number")
        self.assertFalse(Borrower.objects.filter(name="New Reader").exists())

    def test_a_blank_registration_number_is_never_a_duplicate(self):
        make_borrower(name="One", phone="0322-1")
        make_borrower(name="Two", phone="0322-2")

        response = self.client.post(
            reverse("borrower_add"), self.payload(registration_no="")
        )

        self.assertEqual(response.status_code, 302)

    def test_what_was_typed_survives_a_rejected_form(self):
        response = self.post_dialog(
            "borrower_add",
            **self.payload(name="", department="Tafsir", notes="Keep me")
        )

        self.assertEqual(response.context["form_data"]["department"], "Tafsir")
        self.assertEqual(response.context["form_data"]["notes"], "Keep me")
        self.assertContains(response, "Tafsir")

    def test_editing_changes_the_same_record(self):
        borrower = make_borrower(name="Before", phone="0333-1")

        self.client.post(
            reverse("borrower_edit", args=[borrower.id]),
            self.payload(name="After", phone="0333-1"),
        )

        borrower.refresh_from_db()

        self.assertEqual(borrower.name, "After")
        self.assertEqual(Borrower.objects.count(), 1)

    def test_editing_never_touches_a_loan(self):
        borrower = make_borrower(name="Holder", phone="0344-1")

        out = make_loan(copy=self.a_copy("ED-1"), borrower=borrower)
        done = make_loan(
            copy=self.a_copy("ED-2"),
            borrower=borrower,
            issue_date=date(2020, 1, 1),
            due_date=date(2020, 1, 15),
            return_date=date(2020, 1, 12),
        )

        self.client.post(
            reverse("borrower_edit", args=[borrower.id]),
            self.payload(name="Renamed", phone="0344-1"),
        )

        out.refresh_from_db()
        done.refresh_from_db()

        self.assertEqual(out.borrower_id, borrower.id)
        self.assertIsNone(out.return_date)
        self.assertEqual(out.copy.copy_code, "ED-1")

        self.assertEqual(done.return_date, date(2020, 1, 12))
        self.assertEqual(Loan.objects.filter(borrower=borrower).count(), 2)

    def test_a_borrower_may_keep_its_own_registration_number(self):
        borrower = make_borrower(name="Keeper", phone="0355-1")
        borrower.registration_no = "REG-KEEP"
        borrower.save()

        response = self.client.post(
            reverse("borrower_edit", args=[borrower.id]),
            self.payload(
                name="Keeper", phone="0355-1", registration_no="REG-KEEP"
            ),
        )

        self.assertEqual(response.status_code, 302)

    def test_an_assistant_may_still_add_and_edit(self):
        # The existing permission model, unchanged by this task.
        self.as_assistant()

        self.assertEqual(self.dialog("borrower_add").status_code, 200)

        borrower = make_borrower(name="Someone", phone="0366-1")

        self.assertEqual(
            self.dialog("borrower_edit", borrower.id).status_code, 200
        )


class DeactivateAndDeleteTests(BorrowerTestCase):

    def test_a_borrower_with_no_loans_can_be_deleted(self):
        borrower = make_borrower(name="Never Borrowed", phone="0377-1")

        response = self.client.post(
            reverse("borrower_delete", args=[borrower.id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Borrower.objects.filter(id=borrower.id).exists())

    def test_a_borrower_with_an_active_loan_is_refused_not_crashed(self):
        borrower = make_borrower(name="Holding", phone="0377-2")
        loan = make_loan(copy=self.a_copy("DL-1"), borrower=borrower)

        response = self.post_dialog("borrower_delete", borrower.id)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Borrower.objects.filter(id=borrower.id).exists())

        loan.refresh_from_db()
        self.assertEqual(loan.borrower_id, borrower.id)

        # And a plain POST refuses just as firmly, with the reason as a
        # message rather than a page that no longer exists.
        plain = self.client.post(
            reverse("borrower_delete", args=[borrower.id])
        )

        self.assertEqual(plain.status_code, 302)
        self.assertTrue(Borrower.objects.filter(id=borrower.id).exists())

    def test_a_borrower_with_only_returned_loans_is_still_refused(self):
        # The history is the point: it says who had which book and when.
        borrower = make_borrower(name="Past Reader", phone="0377-3")
        make_loan(
            copy=self.a_copy("DL-2"),
            borrower=borrower,
            issue_date=date(2020, 1, 1),
            due_date=date(2020, 1, 15),
            return_date=date(2020, 1, 10),
        )

        response = self.post_dialog("borrower_delete", borrower.id)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be deleted")
        self.assertEqual(Loan.objects.filter(borrower=borrower).count(), 1)

    def test_the_refusal_says_what_is_in_the_way_and_offers_deactivating(self):
        borrower = make_borrower(name="Holding", phone="0377-4")
        make_loan(copy=self.a_copy("DL-3"), borrower=borrower)
        make_loan(
            copy=self.a_copy("DL-4"),
            borrower=borrower,
            issue_date=date(2020, 1, 1),
            due_date=date(2020, 1, 15),
            return_date=date(2020, 1, 10),
        )

        response = self.dialog("borrower_delete", borrower.id)

        # The counts are in the sentence rather than in the context: one
        # of two loans is still out, and that is what the refusal says.
        self.assertContains(response, "1 of their 2 loans")
        self.assertContains(response, "Deactivate instead")

        # Deactivating is the Edit dialog's switch, so there is one
        # control for that field rather than two.
        self.assertContains(
            response, reverse("borrower_edit", args=[borrower.id])
        )

    def test_a_refused_delete_never_removes_a_loan(self):
        borrower = make_borrower(name="Holding", phone="0377-5")
        make_loan(copy=self.a_copy("DL-5"), borrower=borrower)

        before = Loan.objects.count()

        self.client.post(reverse("borrower_delete", args=[borrower.id]))

        self.assertEqual(Loan.objects.count(), before)

    def test_deactivating_keeps_everything_and_flips_the_flag(self):
        borrower = make_borrower(name="Holding", phone="0377-6")
        loan = make_loan(copy=self.a_copy("DL-6"), borrower=borrower)

        response = self.client.post(
            reverse("borrower_toggle_active", args=[borrower.id])
        )

        self.assertEqual(response.status_code, 302)

        borrower.refresh_from_db()
        loan.refresh_from_db()

        self.assertFalse(borrower.is_active)
        self.assertEqual(loan.borrower_id, borrower.id)
        self.assertIsNone(loan.return_date)

    def test_reactivating_works_the_same_way(self):
        borrower = make_borrower(
            name="Returning", phone="0377-7", is_active=False
        )

        self.client.post(
            reverse("borrower_toggle_active", args=[borrower.id])
        )

        borrower.refresh_from_db()
        self.assertTrue(borrower.is_active)

    def test_deactivating_is_logged_against_the_user(self):
        from library.models import ActivityLog

        borrower = make_borrower(name="Logged", phone="0377-8")

        self.client.post(
            reverse("borrower_toggle_active", args=[borrower.id])
        )

        log = ActivityLog.objects.filter(
            entity_type="Borrower", entity_id=borrower.id
        ).order_by("-id").first()

        self.assertIsNotNone(log.user)
        self.assertEqual(log.user.username, "admin_u")
        self.assertIn("deactivated", log.description)

    def test_an_inactive_borrower_is_not_offered_for_a_new_loan(self):
        # The existing behaviour `is_active` already drives; confirmed here
        # because it is what makes deactivating a real alternative.
        borrower = make_borrower(
            name="Suspended", phone="0377-9", is_active=False
        )

        # The issue form asks `borrower_list` for suggestions as you type
        # rather than loading every borrower, so this is where "not
        # offered" is now decided.
        response = self.client.get(
            reverse("borrower_list"),
            {"search": "Suspended", "combobox": "1", "allow_create": "0"},
            HTTP_HX_REQUEST="true",
        )

        # The search term is echoed back in the fragment, so the option's
        # own attributes are what say they were not suggested.
        self.assertNotContains(response, 'data-id="%d"' % borrower.id)
        self.assertNotContains(response, 'data-name="Suspended"')

    def test_an_assistant_cannot_delete(self):
        self.as_assistant()

        borrower = make_borrower(name="Safe", phone="0388-1")

        response = self.client.post(
            reverse("borrower_delete", args=[borrower.id])
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Borrower.objects.filter(id=borrower.id).exists())


class BorrowerFormPageTests(BorrowerTestCase):
    """One page and four fragments, and each is only ever itself.

    The list is the page. The profile, Add, Edit and Delete are dialogs -
    a fragment with no shell around it, which is what lets it be swapped
    into a modal body. A fragment that arrived wrapped in base.html would
    put a whole second application inside the dialog.
    """

    def test_the_list_is_the_page(self):
        response = self.client.get(reverse("borrower_list"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "library/base.html")

    def test_the_four_dialogs_are_fragments(self):
        borrower = make_borrower(name="Someone", phone="0399-1")

        for name, args in (
            ("borrower_detail", [borrower.id]),
            ("borrower_add", []),
            ("borrower_edit", [borrower.id]),
            ("borrower_delete", [borrower.id]),
        ):
            with self.subTest(dialog=name):
                response = self.dialog(name, *args)

                self.assertEqual(response.status_code, 200)
                self.assertTemplateNotUsed(response, "library/base.html")

    def test_asking_for_one_as_a_page_lands_on_the_list(self):
        borrower = make_borrower(name="Someone", phone="0399-3")

        for name, args in (
            ("borrower_detail", [borrower.id]),
            ("borrower_add", []),
            ("borrower_edit", [borrower.id]),
            ("borrower_delete", [borrower.id]),
        ):
            with self.subTest(page=name):
                response = self.client.get(reverse(name, args=args))

                self.assertRedirects(response, reverse("borrower_list"))

    def test_the_forms_mark_their_required_fields(self):
        response = self.dialog("borrower_add")

        self.assertContains(response, "text-danger")
        self.assertContains(response, "required")

    def test_the_edit_form_arrives_filled_in(self):
        borrower = make_borrower(name="Filled In", phone="0399-2")
        borrower.department = "Usul"
        borrower.save()

        response = self.dialog("borrower_edit", borrower.id)

        self.assertContains(response, "Filled In")
        self.assertContains(response, "0399-2")
        self.assertContains(response, "Usul")
