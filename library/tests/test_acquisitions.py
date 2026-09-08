"""Suggested purchases: a note about a book, and what came of it.

Four properties carry these tests.

A suggestion is never a Book. Writing one creates no `Author`, no
`Publisher`, no `Category`, no `Book` and no `BookCopy` - checked by
counting all five before and after - and the only way into the catalogue is
`book_add`, with its own validation, its own duplicate check and its own
permissions, none of which this widens.

It moves one way. Pending to Approved or Rejected, Approved to Acquired,
and nowhere else ever. `acquisitions.advance` puts the state it must be
coming from into the WHERE clause of one UPDATE, so an arbitrary jump, a
reopened decision and the second of two identical POSTs all match no row -
which is checked here through the views and directly.

It is attributed to whoever is signed in. There is no field for a suggester
in the form and no parameter for one in `acquisitions.create`, so a posted
id has nowhere to go; the test posts one anyway.

And the catalogue link is explicit. A suggestion becomes Acquired only when
a book is saved through that suggestion's own shortcut - never because
somebody added a similar book, and never from Pending or Rejected.
"""

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from library import acquisitions
from library.models import (
    ActivityLog,
    AcquisitionSuggestion,
    Author,
    Book,
    BookCopy,
    BookVolume,
    Category,
    Notification,
    Publisher,
)

from .helpers import make_author, make_book, make_user


class SuggestionTestCase(TestCase):

    def setUp(self):
        self.admin = make_user(
            username="admin_s", password="pass12345", role="Admin"
        )
        self.librarian = make_user(
            username="librarian_s", password="pass12345", role="Librarian"
        )
        self.assistant = make_user(
            username="assistant_s", password="pass12345", role="Assistant"
        )

        self.sign_in("librarian_s")

    def sign_in(self, username):
        self.client.login(username=username, password="pass12345")

    def payload(self, **overrides):
        data = {
            "title": "Fath al-Bari",
            "author_name": "Ibn Hajar",
            "publisher_name": "Dar al-Salam",
            "isbn": "978-0000000001",
            "notes": "Three students have asked for it.",
        }
        data.update(overrides)

        return data

    def suggest(self, **overrides):
        return self.client.post(
            reverse("suggestion_add"), self.payload(**overrides)
        )

    def a_suggestion(self, title="Fath al-Bari", status=None, user=None,
                     **fields):
        """A row written straight in, for tests about what happens next."""

        suggestion = acquisitions.create(
            user=user or self.librarian, title=title, **fields
        )

        if status and status != AcquisitionSuggestion.STATUS_PENDING:
            acquisitions.advance(
                suggestion.id,
                AcquisitionSuggestion.STATUS_APPROVED,
                user=self.librarian,
            )

            if status != AcquisitionSuggestion.STATUS_APPROVED:
                AcquisitionSuggestion.objects.filter(
                    id=suggestion.id
                ).update(status=status)

            suggestion.refresh_from_db()

        return suggestion

    def catalogue_counts(self):
        return (
            Book.objects.count(),
            BookVolume.objects.count(),
            BookCopy.objects.count(),
            Author.objects.count(),
            Publisher.objects.count(),
            Category.objects.count(),
        )


# ==========================================================================
# CREATION
# ==========================================================================


class CreationTests(SuggestionTestCase):

    def test_every_role_may_suggest(self):
        for username in ("admin_s", "librarian_s", "assistant_s"):
            with self.subTest(user=username):

                self.sign_in(username)

                response = self.suggest(title="Book by %s" % username)

                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    AcquisitionSuggestion.objects.filter(
                        title="Book by %s" % username
                    ).exists()
                )

    def test_it_lands_on_the_suggestion(self):
        response = self.suggest()

        suggestion = AcquisitionSuggestion.objects.get()

        self.assertRedirects(
            response, reverse("suggestion_detail", args=[suggestion.id])
        )

    def test_every_optional_field_is_saved(self):
        self.suggest()

        suggestion = AcquisitionSuggestion.objects.get()

        self.assertEqual(suggestion.title, "Fath al-Bari")
        self.assertEqual(suggestion.author_name, "Ibn Hajar")
        self.assertEqual(suggestion.publisher_name, "Dar al-Salam")
        self.assertEqual(suggestion.isbn, "978-0000000001")
        self.assertIn("Three students", suggestion.notes)

    def test_a_title_alone_is_enough(self):
        response = self.client.post(
            reverse("suggestion_add"), {"title": "Just a title"}
        )

        self.assertEqual(response.status_code, 302)

        suggestion = AcquisitionSuggestion.objects.get()

        self.assertEqual(suggestion.author_name, "")
        self.assertEqual(suggestion.publisher_name, "")
        self.assertEqual(suggestion.isbn, "")
        self.assertEqual(suggestion.notes, "")

    def test_a_title_is_required(self):
        response = self.client.post(
            reverse("suggestion_add"), self.payload(title="")
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "title is required")
        self.assertEqual(AcquisitionSuggestion.objects.count(), 0)

    def test_a_whitespace_title_is_not_a_title(self):
        self.client.post(reverse("suggestion_add"), self.payload(title="   "))

        self.assertEqual(AcquisitionSuggestion.objects.count(), 0)

    def test_a_get_writes_nothing(self):
        response = self.client.get(reverse("suggestion_add"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(AcquisitionSuggestion.objects.count(), 0)

    def test_it_starts_pending_and_unreviewed(self):
        self.suggest()

        suggestion = AcquisitionSuggestion.objects.get()

        self.assertTrue(suggestion.is_pending)
        self.assertIsNone(suggestion.reviewed_by)
        self.assertIsNone(suggestion.reviewed_at)

    def test_the_signed_in_user_is_the_suggester(self):
        self.sign_in("assistant_s")

        self.suggest()

        self.assertEqual(
            AcquisitionSuggestion.objects.get().suggested_by_id,
            self.assistant.id,
        )

    def test_a_posted_user_id_is_ignored(self):
        self.sign_in("assistant_s")

        self.client.post(
            reverse("suggestion_add"),
            self.payload(
                suggested_by=self.admin.id,
                suggested_by_id=self.admin.id,
                user=self.admin.id,
                reviewed_by=self.admin.id,
            ),
        )

        suggestion = AcquisitionSuggestion.objects.get()

        self.assertEqual(suggestion.suggested_by_id, self.assistant.id)
        self.assertIsNone(suggestion.reviewed_by_id)

    def test_a_posted_status_is_ignored(self):
        self.client.post(
            reverse("suggestion_add"), self.payload(status="Approved")
        )

        self.assertTrue(AcquisitionSuggestion.objects.get().is_pending)

    def test_nothing_is_added_to_the_catalogue(self):
        before = self.catalogue_counts()

        self.suggest()

        self.assertEqual(self.catalogue_counts(), before)
        self.assertEqual(before, (0, 0, 0, 0, 0, 0))

    def test_a_named_author_does_not_become_an_author_record(self):
        self.suggest(author_name="Somebody Entirely New")

        self.assertFalse(
            Author.objects.filter(name="Somebody Entirely New").exists()
        )

    def test_a_named_publisher_does_not_become_a_publisher_record(self):
        self.suggest(publisher_name="A Press Nobody Has Heard Of")

        self.assertFalse(
            Publisher.objects.filter(
                name="A Press Nobody Has Heard Of"
            ).exists()
        )

    def test_it_is_logged(self):
        self.suggest()

        self.assertEqual(
            ActivityLog.objects.filter(
                action="CREATE", entity_type="AcquisitionSuggestion"
            ).count(),
            1,
        )

    def test_the_database_refuses_an_unknown_status(self):
        suggestion = self.a_suggestion()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AcquisitionSuggestion.objects.filter(
                    id=suggestion.id
                ).update(status="Ordered")

    def test_the_database_refuses_a_pending_row_with_a_review_date(self):
        suggestion = self.a_suggestion()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AcquisitionSuggestion.objects.filter(
                    id=suggestion.id
                ).update(reviewed_at=timezone.now())


# ==========================================================================
# DUPLICATE AWARENESS
# ==========================================================================


class DuplicateAwarenessTests(SuggestionTestCase):
    """Task 4's own comparison, used to warn rather than to refuse."""

    def setUp(self):
        super().setUp()

        self.author = make_author("Ibn Hajar")
        self.book = make_book(title="Fath al-Bari", author=self.author)

    def test_a_matching_title_and_author_is_pointed_out(self):
        response = self.suggest()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already has")
        self.assertContains(response, "Fath al-Bari")
        self.assertEqual(AcquisitionSuggestion.objects.count(), 0)

    def test_it_links_to_the_existing_record(self):
        response = self.suggest()

        self.assertContains(
            response, reverse("book_detail", args=[self.book.id])
        )

    def test_the_match_ignores_case_and_spacing(self):
        # The same normalisation `book_add` refuses a duplicate on.
        response = self.suggest(title="  fath   AL-bari ")

        self.assertContains(response, "already has")

    def test_a_confirmation_lets_it_through(self):
        # Imperfect matching must not silently block a real suggestion.
        response = self.client.post(
            reverse("suggestion_add"),
            self.payload(confirm_duplicate="1"),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(AcquisitionSuggestion.objects.count(), 1)

    def test_the_confirmation_is_only_offered_after_the_warning(self):
        first = self.suggest()

        self.assertContains(first, "confirm_duplicate")

        # And is absent from a form nobody has been warned on.
        blank = self.client.get(reverse("suggestion_add"))

        self.assertNotContains(blank, "confirm_duplicate")

    def test_a_title_with_no_match_goes_straight_through(self):
        response = self.suggest(title="Something Else Entirely")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(AcquisitionSuggestion.objects.count(), 1)

    def test_a_different_catalogued_author_is_not_a_duplicate(self):
        # The same title under a different author is a different book -
        # exactly the line `find_duplicate_books` already draws. The author
        # has to be one the catalogue knows for that narrower comparison to
        # be the one used; an unknown name falls back to the title, which
        # the next test covers.
        make_author("Al-Nawawi")

        response = self.suggest(author_name="Al-Nawawi")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(AcquisitionSuggestion.objects.count(), 1)

    def test_an_unknown_author_falls_back_to_the_title(self):
        # Nobody of that name is catalogued, so the comparison is the same
        # normalised title with one side left off - not a looser matcher.
        response = self.suggest(author_name="Nobody Catalogued")

        self.assertContains(response, "already has")

    def test_no_author_at_all_still_matches_on_title(self):
        response = self.suggest(author_name="")

        self.assertContains(response, "already has")

    def test_the_warning_writes_nothing(self):
        before = self.catalogue_counts()

        self.suggest()

        self.assertEqual(self.catalogue_counts(), before)
        self.assertEqual(AcquisitionSuggestion.objects.count(), 0)

    def test_what_was_typed_survives_the_warning(self):
        response = self.suggest(notes="Three students have asked for it.")

        self.assertContains(response, "Three students have asked for it.")
        self.assertContains(response, "Ibn Hajar")


# ==========================================================================
# LISTING
# ==========================================================================


class ListTests(SuggestionTestCase):

    def setUp(self):
        super().setUp()

        self.url = reverse("suggestion_list")

    def test_pending_comes_first_then_newest(self):
        old_pending = self.a_suggestion(title="Old and pending")
        approved = self.a_suggestion(
            title="Approved", status=AcquisitionSuggestion.STATUS_APPROVED
        )
        new_pending = self.a_suggestion(title="New and pending")

        titles = [
            row.title
            for row in self.client.get(self.url).context["suggestions"]
        ]

        self.assertEqual(
            titles, ["New and pending", "Old and pending", "Approved"]
        )
        self.assertEqual(old_pending.status, "Pending")
        self.assertEqual(approved.status, "Approved")
        self.assertEqual(new_pending.status, "Pending")

    def test_ties_are_broken_by_id_descending(self):
        moment = timezone.now()

        for index in range(3):
            self.a_suggestion(title="Tied %d" % index)

        AcquisitionSuggestion.objects.update(created_at=moment)

        ids = [
            row.id
            for row in self.client.get(self.url).context["suggestions"]
        ]

        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_the_status_filter_narrows_it(self):
        self.a_suggestion(title="Still waiting")
        self.a_suggestion(
            title="Said yes", status=AcquisitionSuggestion.STATUS_APPROVED
        )

        titles = [
            row.title
            for row in self.client.get(
                self.url, {"status": "Approved"}
            ).context["suggestions"]
        ]

        self.assertEqual(titles, ["Said yes"])

    def test_an_unknown_status_is_read_as_no_filter(self):
        self.a_suggestion(title="Still waiting")

        response = self.client.get(self.url, {"status": "Ordered"})

        self.assertEqual(response.context["status"], "")
        self.assertEqual(len(response.context["suggestions"]), 1)

    def test_the_title_search_narrows_it(self):
        self.a_suggestion(title="Fath al-Bari")
        self.a_suggestion(title="Riyad as-Salihin")

        titles = [
            row.title
            for row in self.client.get(
                self.url, {"search": "riyad"}
            ).context["suggestions"]
        ]

        self.assertEqual(titles, ["Riyad as-Salihin"])

    def test_the_filters_combine(self):
        self.a_suggestion(title="Fath al-Bari")
        self.a_suggestion(
            title="Fath al-Qadir",
            status=AcquisitionSuggestion.STATUS_APPROVED,
        )

        titles = [
            row.title
            for row in self.client.get(
                self.url, {"search": "Fath", "status": "Approved"}
            ).context["suggestions"]
        ]

        self.assertEqual(titles, ["Fath al-Qadir"])

    def test_it_is_paginated(self):
        for index in range(30):
            self.a_suggestion(title="Suggestion %02d" % index)

        page = self.client.get(self.url).context["suggestions"]

        self.assertTrue(page.paginator.num_pages > 1)
        self.assertLess(len(page.object_list), 30)

    def test_paging_keeps_the_filters(self):
        for index in range(30):
            self.a_suggestion(title="Fath volume %02d" % index)

        response = self.client.get(
            self.url, {"search": "Fath", "status": "Pending"}
        )

        query = response.context["pagination_query"]

        self.assertIn("search=Fath", query)
        self.assertIn("status=Pending", query)
        self.assertNotIn("page=", query)

    def test_the_second_page_is_still_filtered(self):
        for index in range(30):
            self.a_suggestion(title="Fath volume %02d" % index)

        self.a_suggestion(title="Something else")

        rows = self.client.get(
            self.url, {"search": "Fath", "page": "2"}
        ).context["suggestions"]

        self.assertTrue(all("Fath" in row.title for row in rows))

    def test_an_empty_list_says_so(self):
        response = self.client.get(self.url)

        self.assertContains(response, "Nothing has been suggested yet")

    def test_an_empty_filter_result_says_something_else(self):
        self.a_suggestion(title="Fath al-Bari")

        response = self.client.get(self.url, {"search": "nothing like this"})

        self.assertContains(response, "Nothing matches that")


# ==========================================================================
# WORKFLOW
# ==========================================================================


class WorkflowTests(SuggestionTestCase):

    def setUp(self):
        super().setUp()

        self.suggestion = self.a_suggestion()

    def review(self, status, suggestion=None):
        return self.client.post(
            reverse(
                "suggestion_review",
                args=[(suggestion or self.suggestion).id],
            ),
            {"status": status},
        )

    def reload(self):
        self.suggestion.refresh_from_db()

        return self.suggestion

    def test_pending_to_approved(self):
        self.review("Approved")

        self.assertEqual(self.reload().status, "Approved")

    def test_pending_to_rejected(self):
        self.review("Rejected")

        self.assertEqual(self.reload().status, "Rejected")

    def test_approved_to_acquired(self):
        self.review("Approved")
        self.review("Acquired")

        self.assertEqual(self.reload().status, "Acquired")

    def test_the_reviewer_and_the_time_are_recorded(self):
        before = timezone.now()

        self.review("Approved")

        suggestion = self.reload()

        self.assertEqual(suggestion.reviewed_by_id, self.librarian.id)
        self.assertIsNotNone(suggestion.reviewed_at)
        self.assertGreaterEqual(suggestion.reviewed_at, before)

    def test_the_reviewer_is_whoever_is_signed_in(self):
        self.sign_in("admin_s")

        self.review("Approved")

        self.assertEqual(self.reload().reviewed_by_id, self.admin.id)

    def test_a_posted_reviewer_id_is_ignored(self):
        self.client.post(
            reverse("suggestion_review", args=[self.suggestion.id]),
            {"status": "Approved", "reviewed_by": self.admin.id},
        )

        self.assertEqual(self.reload().reviewed_by_id, self.librarian.id)

    def test_pending_cannot_jump_to_acquired(self):
        response = self.review("Acquired")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.reload().status, "Pending")
        self.assertIsNone(self.suggestion.reviewed_at)

    def test_a_rejected_suggestion_cannot_be_reopened(self):
        self.review("Rejected")

        for attempt in ("Pending", "Approved", "Acquired"):
            with self.subTest(to=attempt):

                self.review(attempt)

                self.assertEqual(self.reload().status, "Rejected")

    def test_an_acquired_suggestion_cannot_be_moved(self):
        self.review("Approved")
        self.review("Acquired")

        for attempt in ("Pending", "Approved", "Rejected"):
            with self.subTest(to=attempt):

                self.review(attempt)

                self.assertEqual(self.reload().status, "Acquired")

    def test_an_approved_suggestion_cannot_go_back_to_pending(self):
        self.review("Approved")
        self.review("Pending")

        self.assertEqual(self.reload().status, "Approved")

    def test_an_approved_suggestion_cannot_be_rejected(self):
        self.review("Approved")
        self.review("Rejected")

        self.assertEqual(self.reload().status, "Approved")

    def test_an_unknown_status_changes_nothing(self):
        self.review("Ordered")

        self.assertEqual(self.reload().status, "Pending")

    def test_a_double_post_moves_it_once(self):
        self.review("Approved")

        first = self.reload().reviewed_at

        self.review("Approved")

        self.assertEqual(self.reload().reviewed_at, first)

    def test_a_second_approval_does_not_move_the_timestamp(self):
        # The repeated one matches no row, so nothing - not even the
        # review time - creeps on a refresh.
        self.review("Approved")

        stamp = self.reload().reviewed_at

        for _ in range(3):
            self.review("Approved")

        self.assertEqual(self.reload().reviewed_at, stamp)

    def test_a_get_reviews_nothing(self):
        response = self.client.get(
            reverse("suggestion_review", args=[self.suggestion.id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.reload().status, "Pending")

    def test_the_database_is_what_refuses_an_illegal_move(self):
        # Straight at the function, the way two simultaneous requests
        # would arrive: only one of them writes.
        moved = [
            acquisitions.advance(
                self.suggestion.id, "Approved", user=self.librarian
            )
            for _ in range(3)
        ]

        self.assertEqual(moved, [True, False, False])

    def test_advancing_a_missing_row_is_harmless(self):
        self.assertFalse(
            acquisitions.advance(999999, "Approved", user=self.librarian)
        )

    def test_a_transition_is_logged(self):
        self.review("Approved")

        self.assertEqual(
            ActivityLog.objects.filter(
                action="UPDATE",
                entity_type="AcquisitionSuggestion",
                entity_id=self.suggestion.id,
            ).count(),
            1,
        )

    def test_a_refused_transition_is_not_logged(self):
        self.review("Acquired")

        self.assertEqual(
            ActivityLog.objects.filter(
                entity_type="AcquisitionSuggestion"
            ).count(),
            0,
        )


# ==========================================================================
# CATALOGUE INTEGRATION
# ==========================================================================


class CatalogueIntegrationTests(SuggestionTestCase):
    """The shortcut is `book_add`, prefilled. It is not a second workflow."""

    def setUp(self):
        super().setUp()

        self.author = make_author("Ibn Hajar")

        self.suggestion = self.a_suggestion(
            title="Fath al-Bari",
            author_name="Ibn Hajar",
            status=AcquisitionSuggestion.STATUS_APPROVED,
        )

    def shortcut(self, suggestion=None):
        return "%s?suggestion=%d" % (
            reverse("book_add"), (suggestion or self.suggestion).id
        )

    def open_shortcut(self, url=None):
        """The Add Book dialog the shortcut opens.

        There is no Add Book page any more, so the shortcut opens the
        same dialog every other Add Book link opens - with the suggestion
        still riding in the query string.
        """

        return self.client.get(
            (url or self.shortcut()) + "&modal=1",
            headers={"HX-Request": "true"},
        )

    def book_payload(self, **overrides):
        data = {
            "title": "Fath al-Bari",
            "author": self.author.id,
            "volume_mode": "single",
            "copies_mode": "skip",
        }
        data.update(overrides)

        return data

    def test_the_detail_page_offers_the_shortcut(self):
        response = self.client.get(
            reverse("suggestion_detail", args=[self.suggestion.id])
        )

        self.assertContains(response, "suggestion=%d" % self.suggestion.id)
        self.assertContains(response, "Add to catalogue")

    def test_the_shortcut_opens_the_ordinary_add_book_dialog(self):
        # The same dialog every other Add Book link opens, with the
        # suggestion riding in the query string - not a second form.
        response = self.client.get(
            self.shortcut() + "&modal=1", headers={"HX-Request": "true"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response, "library/partials/book_add_modal.html"
        )

    def test_the_title_is_prefilled(self):
        response = self.open_shortcut()

        self.assertEqual(
            response.context["form_data"]["title"], "Fath al-Bari"
        )

    def test_a_catalogued_author_is_prefilled(self):
        response = self.open_shortcut()

        self.assertEqual(
            response.context["form_data"]["author"], str(self.author.id)
        )
        self.assertEqual(
            response.context["form_data"]["author_name"], "Ibn Hajar"
        )

    def test_an_uncatalogued_author_is_left_blank(self):
        suggestion = self.a_suggestion(
            title="Something New",
            author_name="Nobody Catalogued",
            status=AcquisitionSuggestion.STATUS_APPROVED,
        )

        response = self.open_shortcut(self.shortcut(suggestion))

        self.assertEqual(response.context["form_data"]["author"], "")
        self.assertFalse(
            Author.objects.filter(name="Nobody Catalogued").exists()
        )

    def test_a_catalogued_publisher_is_prefilled(self):
        publisher = Publisher.objects.create(name="Dar al-Salam")

        suggestion = self.a_suggestion(
            title="Another Book",
            publisher_name="dar al-salam",
            status=AcquisitionSuggestion.STATUS_APPROVED,
        )

        response = self.open_shortcut(self.shortcut(suggestion))

        self.assertEqual(
            response.context["form_data"]["publisher"], str(publisher.id)
        )

    def test_opening_the_shortcut_creates_nothing(self):
        before = self.catalogue_counts()

        self.open_shortcut()

        self.assertEqual(self.catalogue_counts(), before)
        self.assertEqual(
            AcquisitionSuggestion.objects.get(id=self.suggestion.id).status,
            "Approved",
        )

    def test_saving_through_it_creates_the_book_and_marks_it_acquired(self):
        response = self.client.post(self.shortcut(), self.book_payload())

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Book.objects.filter(title="Fath al-Bari").exists())

        self.suggestion.refresh_from_db()

        self.assertEqual(self.suggestion.status, "Acquired")
        self.assertEqual(self.suggestion.reviewed_by_id, self.librarian.id)

    def test_add_book_validation_still_applies(self):
        response = self.client.post(
            self.shortcut() + "&modal=1",
            self.book_payload(author=""),
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        # Said under the Author field now, rather than as one sentence at
        # the top naming both fields when only one was missing.
        self.assertContains(response, "Choose the author.")
        self.assertEqual(Book.objects.count(), 0)

        self.suggestion.refresh_from_db()

        self.assertEqual(self.suggestion.status, "Approved")

    def test_add_books_duplicate_detection_still_applies(self):
        make_book(title="Fath al-Bari", author=self.author)

        response = self.client.post(
            self.shortcut() + "&modal=1",
            self.book_payload(),
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already in the catalogue")
        self.assertEqual(Book.objects.count(), 1)

        self.suggestion.refresh_from_db()

        self.assertEqual(self.suggestion.status, "Approved")

    def test_a_pending_suggestion_cannot_use_the_shortcut(self):
        pending = self.a_suggestion(title="Still waiting")

        response = self.open_shortcut(self.shortcut(pending))

        # The dialog opens - it is just Add Book - but nothing is
        # prefilled from a suggestion nobody has approved.
        self.assertEqual(response.context["form_data"]["title"], "")

        self.client.post(
            self.shortcut(pending), self.book_payload(title="Still waiting")
        )

        pending.refresh_from_db()

        self.assertEqual(pending.status, "Pending")

    def test_a_rejected_suggestion_cannot_use_the_shortcut(self):
        rejected = self.a_suggestion(title="Turned down")

        acquisitions.advance(rejected.id, "Rejected", user=self.librarian)

        self.client.post(
            self.shortcut(rejected), self.book_payload(title="Turned down")
        )

        rejected.refresh_from_db()

        self.assertEqual(rejected.status, "Rejected")

    def test_a_forged_suggestion_id_does_nothing(self):
        response = self.open_shortcut(
            "%s?suggestion=999999" % reverse("book_add")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form_data"]["title"], "")

    def test_a_nonsense_suggestion_parameter_does_nothing(self):
        response = self.open_shortcut(
            "%s?suggestion=not-a-number" % reverse("book_add")
        )

        self.assertEqual(response.status_code, 200)

    def test_the_parameter_cannot_carry_a_value_into_the_form(self):
        # Only the suggestion id is read from the URL; the title and the
        # author come off the stored row. A posted title still has to pass
        # `book_add`'s own checks.
        response = self.open_shortcut(
            "%s?suggestion=%d&title=Injected&author=%d"
            % (reverse("book_add"), self.suggestion.id, self.author.id)
        )

        self.assertEqual(
            response.context["form_data"]["title"], "Fath al-Bari"
        )

    def test_adding_a_similar_book_the_ordinary_way_marks_nothing(self):
        self.client.post(reverse("book_add"), self.book_payload())

        self.assertTrue(Book.objects.filter(title="Fath al-Bari").exists())

        self.suggestion.refresh_from_db()

        self.assertEqual(self.suggestion.status, "Approved")

    def test_ordinary_book_creation_is_unchanged(self):
        before = self.client.get(
            reverse("book_add") + "?modal=1",
            headers={"HX-Request": "true"},
        )

        self.assertEqual(before.context["form_data"]["title"], "")

        response = self.client.post(
            reverse("book_add"), self.book_payload(title="Unrelated Book")
        )

        self.assertRedirects(response, reverse("book_list"))
        self.assertTrue(Book.objects.filter(title="Unrelated Book").exists())

    def test_a_second_book_through_the_same_shortcut_changes_nothing(self):
        self.client.post(self.shortcut(), self.book_payload())

        self.suggestion.refresh_from_db()
        first = self.suggestion.reviewed_at

        self.client.post(
            self.shortcut(), self.book_payload(title="Fath al-Bari Vol 2")
        )

        self.suggestion.refresh_from_db()

        self.assertEqual(self.suggestion.status, "Acquired")
        self.assertEqual(self.suggestion.reviewed_at, first)

    def test_already_acquired_covers_a_book_that_came_another_way(self):
        self.client.post(
            reverse("suggestion_review", args=[self.suggestion.id]),
            {"status": "Acquired"},
        )

        self.suggestion.refresh_from_db()

        self.assertEqual(self.suggestion.status, "Acquired")
        self.assertEqual(Book.objects.count(), 0)


# ==========================================================================
# DETAIL PAGE
# ==========================================================================


class DetailPageTests(SuggestionTestCase):

    def setUp(self):
        super().setUp()

        self.suggestion = self.a_suggestion(
            title="Fath al-Bari",
            author_name="Ibn Hajar",
            publisher_name="Dar al-Salam",
            isbn="978-0000000001",
            notes="Three students have asked for it.",
        )

        self.url = reverse("suggestion_detail", args=[self.suggestion.id])

    def test_it_shows_what_was_asked_for(self):
        response = self.client.get(self.url)

        for expected in (
            "Fath al-Bari",
            "Ibn Hajar",
            "Dar al-Salam",
            "978-0000000001",
            "Three students have asked for it.",
            "Pending",
            self.librarian.full_name,
        ):
            with self.subTest(shows=expected):
                self.assertContains(response, expected)

    def test_a_pending_one_names_no_reviewer(self):
        response = self.client.get(self.url)

        self.assertNotContains(response, "Decided by")

    def test_a_decided_one_names_its_reviewer_and_date(self):
        acquisitions.advance(
            self.suggestion.id, "Approved", user=self.admin
        )

        response = self.client.get(self.url)

        self.assertContains(response, "Decided by")
        self.assertContains(response, self.admin.full_name)

    def test_a_finished_one_says_so(self):
        acquisitions.advance(
            self.suggestion.id, "Rejected", user=self.librarian
        )

        response = self.client.get(self.url)

        self.assertContains(response, "cannot be changed")
        self.assertNotContains(response, "Add to catalogue")

    def test_a_missing_suggestion_is_a_404(self):
        self.assertEqual(
            self.client.get(
                reverse("suggestion_detail", args=[999999])
            ).status_code,
            404,
        )


# ==========================================================================
# PERMISSIONS
# ==========================================================================


class PermissionTests(SuggestionTestCase):

    def setUp(self):
        super().setUp()

        self.suggestion = self.a_suggestion()

        self.detail = reverse(
            "suggestion_detail", args=[self.suggestion.id]
        )
        self.review = reverse(
            "suggestion_review", args=[self.suggestion.id]
        )

    # --- reading, which everybody may do --------------------------------

    def test_every_role_may_read_the_list(self):
        for username in ("admin_s", "librarian_s", "assistant_s"):
            with self.subTest(user=username):

                self.sign_in(username)

                self.assertEqual(
                    self.client.get(reverse("suggestion_list")).status_code,
                    200,
                )

    def test_every_role_may_read_a_suggestion(self):
        for username in ("admin_s", "librarian_s", "assistant_s"):
            with self.subTest(user=username):

                self.sign_in(username)

                self.assertEqual(
                    self.client.get(self.detail).status_code, 200
                )

    def test_every_role_may_open_the_form(self):
        for username in ("admin_s", "librarian_s", "assistant_s"):
            with self.subTest(user=username):

                self.sign_in(username)

                self.assertEqual(
                    self.client.get(reverse("suggestion_add")).status_code,
                    200,
                )

    # --- deciding, which only two may do --------------------------------

    def test_admin_and_librarian_may_decide(self):
        for username in ("admin_s", "librarian_s"):
            with self.subTest(user=username):

                suggestion = self.a_suggestion(title="For %s" % username)

                self.sign_in(username)

                self.client.post(
                    reverse("suggestion_review", args=[suggestion.id]),
                    {"status": "Approved"},
                )

                suggestion.refresh_from_db()

                self.assertEqual(suggestion.status, "Approved")

    def test_an_assistant_is_refused_the_review_endpoint(self):
        self.sign_in("assistant_s")

        response = self.client.post(self.review, {"status": "Approved"})

        self.suggestion.refresh_from_db()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.suggestion.status, "Pending")

    def test_an_assistant_is_refused_each_decision(self):
        self.sign_in("assistant_s")

        for status in ("Approved", "Rejected", "Acquired"):
            with self.subTest(status=status):

                response = self.client.post(self.review, {"status": status})

                self.suggestion.refresh_from_db()

                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.suggestion.status, "Pending")

    def test_an_assistant_is_offered_no_decision_buttons(self):
        self.sign_in("assistant_s")

        response = self.client.get(self.detail)

        self.assertNotContains(response, self.review)
        self.assertNotContains(response, "Approve")

    def test_a_librarian_is_offered_them(self):
        self.sign_in("librarian_s")

        response = self.client.get(self.detail)

        self.assertContains(response, self.review)

    # --- the catalogue shortcut, which book_add already governs ---------

    def test_an_assistant_is_offered_no_catalogue_shortcut(self):
        acquisitions.advance(
            self.suggestion.id, "Approved", user=self.librarian
        )

        self.sign_in("assistant_s")

        response = self.client.get(self.detail)

        self.assertNotContains(response, "Add to catalogue")
        self.assertContains(response, "librarian's job")

    def test_and_is_still_refused_book_add_directly(self):
        # The shortcut does not widen `book_add`; an Assistant could not
        # add a book before and cannot now.
        acquisitions.advance(
            self.suggestion.id, "Approved", user=self.librarian
        )

        self.sign_in("assistant_s")

        response = self.client.get(
            "%s?suggestion=%d" % (reverse("book_add"), self.suggestion.id)
        )

        self.assertEqual(response.status_code, 403)

    def test_an_assistant_posting_at_book_add_is_refused(self):
        acquisitions.advance(
            self.suggestion.id, "Approved", user=self.librarian
        )

        self.sign_in("assistant_s")

        response = self.client.post(
            "%s?suggestion=%d" % (reverse("book_add"), self.suggestion.id),
            {"title": "Fath al-Bari", "author": make_author("X").id},
        )

        self.suggestion.refresh_from_db()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Book.objects.count(), 0)
        self.assertEqual(self.suggestion.status, "Approved")

    # --- anonymous, which is unchanged ----------------------------------

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        self.client.logout()

        for url in (
            reverse("suggestion_list"),
            reverse("suggestion_add"),
            self.detail,
        ):
            with self.subTest(url=url):

                response = self.client.get(url)

                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])

    def test_an_anonymous_post_writes_nothing(self):
        self.client.logout()

        self.client.post(reverse("suggestion_add"), self.payload())
        self.client.post(self.review, {"status": "Approved"})

        self.suggestion.refresh_from_db()

        self.assertEqual(AcquisitionSuggestion.objects.count(), 1)
        self.assertEqual(self.suggestion.status, "Pending")


# ==========================================================================
# NAVIGATION
# ==========================================================================


class NavigationTests(SuggestionTestCase):

    def shell(self, username="librarian_s"):
        self.sign_in(username)

        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_sidebar_offers_it_to_every_role(self):
        for username in ("admin_s", "librarian_s", "assistant_s"):
            with self.subTest(user=username):

                self.assertIn(reverse("suggestion_list"), self.shell(username))

    def test_it_is_a_plain_link_like_every_other_entry(self):
        # No `data-nav-*` wiring: `/suggestions/` shares no prefix with
        # `/books/`, so longest-prefix matching keeps them apart by itself.
        import re

        link = re.search(
            r'<a[^>]*href="%s"[^>]*>' % reverse("suggestion_list"),
            self.shell(),
            re.S,
        )

        self.assertIsNotNone(link)
        self.assertIn("nav-link-custom", link.group(0))
        self.assertNotIn("data-nav-", link.group(0))
        self.assertNotIn("hx-", link.group(0))

    def test_it_cannot_be_confused_with_the_books_entry(self):
        # The property the active state depends on, asserted directly.
        self.assertFalse(
            reverse("suggestion_list").startswith(reverse("book_list"))
        )
        self.assertFalse(
            reverse("book_list").startswith(reverse("suggestion_list"))
        )

    def test_books_still_claims_volumes_and_contents(self):
        import re

        link = re.search(
            r'<a[^>]*href="%s"[^>]*>' % reverse("book_list"),
            self.shell(),
            re.S,
        )

        self.assertIsNotNone(link)
        self.assertIn("data-nav-prefix", link.group(0))
        self.assertIn(reverse("book_volume_list"), link.group(0))

    def test_each_page_answers_a_navigation_with_the_region_alone(self):
        suggestion = self.a_suggestion()

        pages = (
            reverse("suggestion_list"),
            reverse("suggestion_add"),
            reverse("suggestion_detail", args=[suggestion.id]),
        )

        for url in pages:
            with self.subTest(page=url):

                body = self.client.get(
                    url,
                    headers={
                        "hx-request": "true",
                        "hx-target": "mainContent",
                    },
                ).content.decode()

                self.assertNotIn("<!DOCTYPE", body.upper())
                self.assertIn('id="pageHeading"', body)

    def test_each_page_is_still_a_whole_page_without_the_headers(self):
        suggestion = self.a_suggestion()

        for url in (
            reverse("suggestion_list"),
            reverse("suggestion_add"),
            reverse("suggestion_detail", args=[suggestion.id]),
        ):
            with self.subTest(page=url):

                body = self.client.get(url).content.decode()

                self.assertIn("<!DOCTYPE", body.upper())
                self.assertIn("sidebar", body)


# ==========================================================================
# NOTIFICATIONS
# ==========================================================================


class NotificationTests(SuggestionTestCase):
    """Task 17's own mechanism, reused. Nothing new was built for this."""

    def submitted(self, recipient=None):
        rows = Notification.objects.filter(
            event_type=Notification.EVENT_SUGGESTION_SUBMITTED
        )

        if recipient is not None:
            rows = rows.filter(recipient=recipient)

        return rows

    def test_the_reviewers_are_told(self):
        self.sign_in("assistant_s")

        self.suggest()

        recipients = set(
            self.submitted().values_list("recipient__username", flat=True)
        )

        self.assertEqual(recipients, {"admin_s", "librarian_s"})

    def test_an_assistant_is_not_told(self):
        # There is nothing for them to do about it.
        self.sign_in("admin_s")

        self.suggest()

        self.assertEqual(self.submitted(self.assistant).count(), 0)

    def test_the_suggester_is_not_told_their_own_news(self):
        self.sign_in("librarian_s")

        self.suggest()

        self.assertEqual(self.submitted(self.librarian).count(), 0)
        self.assertEqual(self.submitted(self.admin).count(), 1)

    def test_it_names_the_book_and_who_asked(self):
        self.sign_in("assistant_s")

        self.suggest()

        note = self.submitted(self.admin).get()

        self.assertIn("Fath al-Bari", note.message)
        self.assertIn(self.assistant.full_name, note.message)

    def test_it_links_to_the_suggestion(self):
        self.sign_in("assistant_s")

        self.suggest()

        suggestion = AcquisitionSuggestion.objects.get()

        self.assertEqual(
            self.submitted(self.admin).get().url,
            reverse("suggestion_detail", args=[suggestion.id]),
        )

    def test_it_is_keyed_to_the_suggestion(self):
        self.sign_in("assistant_s")

        self.suggest()

        suggestion = AcquisitionSuggestion.objects.get()

        self.assertEqual(
            self.submitted(self.admin).get().event_key,
            "suggestion_submitted:%d" % suggestion.id,
        )

    def test_a_refused_suggestion_tells_nobody(self):
        self.client.post(reverse("suggestion_add"), self.payload(title=""))

        self.assertEqual(self.submitted().count(), 0)

    def test_reviewing_tells_nobody(self):
        # The decision is not news to the people who make it, and the
        # suggester has no notification of their own here - documented in
        # library/notifications.py.
        suggestion = self.a_suggestion()

        before = Notification.objects.count()

        self.client.post(
            reverse("suggestion_review", args=[suggestion.id]),
            {"status": "Approved"},
        )

        self.assertEqual(Notification.objects.count(), before)

    def test_reading_the_list_tells_nobody(self):
        self.a_suggestion()

        self.client.get(reverse("suggestion_list"))

        self.assertEqual(self.submitted().count(), 0)


# ==========================================================================
# QUERY EFFICIENCY
# ==========================================================================


class QueryCountTests(SuggestionTestCase):

    def many(self, count, status=None):
        now = timezone.now()

        AcquisitionSuggestion.objects.bulk_create(
            [
                AcquisitionSuggestion(
                    title="Bulk suggestion %04d" % index,
                    author_name="Author %04d" % index,
                    status=status or AcquisitionSuggestion.STATUS_PENDING,
                    suggested_by=(
                        self.librarian if index % 2 else self.assistant
                    ),
                    reviewed_by=self.admin if status else None,
                    reviewed_at=now if status else None,
                    created_at=now,
                    updated_at=now,
                )
                for index in range(count)
            ]
        )

    def count_for(self, url, params=None, **kwargs):
        with CaptureQueriesContext(connection) as captured:
            self.client.get(url, params or {}, **kwargs)

        return len(captured.captured_queries)

    def test_the_list_costs_the_same_with_three_and_with_a_thousand(self):
        self.many(3)

        few = self.count_for(reverse("suggestion_list"))

        self.many(1000)

        many = self.count_for(reverse("suggestion_list"))

        self.assertEqual(few, many)

    def test_a_filtered_search_costs_the_same(self):
        self.many(3)

        few = self.count_for(
            reverse("suggestion_list"),
            {"search": "Bulk", "status": "Pending"},
        )

        self.many(1000)

        many = self.count_for(
            reverse("suggestion_list"),
            {"search": "Bulk", "status": "Pending"},
        )

        self.assertEqual(few, many)

    def test_naming_the_suggester_costs_no_query_per_row(self):
        # A full page of rows, each with a different suggester and a
        # reviewer, joined in the one query that fetched them.
        self.many(40, status=AcquisitionSuggestion.STATUS_APPROVED)

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("suggestion_list"))

            body = response.content.decode()

        # The page really did render the names, so this is not passing by
        # rendering nothing.
        self.assertIn(self.librarian.full_name, body)
        self.assertIn(self.assistant.full_name, body)

        user_queries = [
            query["sql"]
            for query in captured.captured_queries
            if 'FROM "users"' in query["sql"]
        ]

        # The signed-in user, and nothing per row.
        self.assertLessEqual(len(user_queries), 2)

    def test_the_detail_page_is_flat(self):
        first = self.a_suggestion(title="One")

        acquisitions.advance(first.id, "Approved", user=self.admin)

        before = self.count_for(
            reverse("suggestion_detail", args=[first.id])
        )

        self.many(1000)

        after = self.count_for(
            reverse("suggestion_detail", args=[first.id])
        )

        self.assertEqual(before, after)

    def test_the_catalogue_list_is_unaffected(self):
        author = make_author("Ibn Hajar")

        for index in range(20):
            make_book(title="Book %02d" % index, author=author)

        before = self.count_for(reverse("book_list"))

        self.many(500)

        after = self.count_for(reverse("book_list"))

        self.assertEqual(before, after)

    def test_add_book_costs_no_more_without_a_suggestion(self):
        suggestion = self.a_suggestion(
            title="Fath al-Bari",
            status=AcquisitionSuggestion.STATUS_APPROVED,
        )

        plain = self.count_for(reverse("book_add"))

        with_shortcut = self.count_for(
            reverse("book_add"), {"suggestion": suggestion.id}
        )

        # The ordinary page gained nothing; the shortcut pays for the
        # suggestion and the two name lookups, and no more.
        self.assertLessEqual(with_shortcut - plain, 3)

    def test_the_duplicate_warning_is_one_query(self):
        author = make_author("Ibn Hajar")
        make_book(title="Fath al-Bari", author=author)

        with CaptureQueriesContext(connection) as captured:
            self.client.post(reverse("suggestion_add"), self.payload())

        book_queries = [
            query["sql"]
            for query in captured.captured_queries
            if 'FROM "books"' in query["sql"]
        ]

        # One lookup for the author name, one for the books. The catalogue
        # is not searched twice for the same answer.
        self.assertEqual(len(book_queries), 1)
