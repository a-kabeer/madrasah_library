"""The searchable borrower control and the barcode scan button.

The scanner itself is the browser's own BarcodeDetector, so what can be
tested server-side is what the page offers: the suggestion endpoint the
borrower control reads, that the scan button is present but hidden until
the script decides the device can use it, and that neither change touched
the manual search or the POST contract.
"""

from django.test import TestCase
from django.urls import reverse

from library.tests.helpers import (
    make_author,
    make_book,
    make_borrower,
    make_copy,
    make_shelf,
    make_user,
    make_volume,
)


COMBOBOX = {"HTTP_HX_REQUEST": "true"}


class BorrowerSuggestionTests(TestCase):
    """`borrower_list` now answers combobox searches, like the book lists."""

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.url = reverse("borrower_list")

        self.hafsa = make_borrower(name="Hafsa Rahmani", phone="03451110001")
        self.hafsa.registration_no = "DM-2001"
        self.hafsa.save(update_fields=["registration_no"])

        self.junaid = make_borrower(name="Junaid Baloch", phone="03452220002")
        self.junaid.registration_no = "DM-2002"
        self.junaid.save(update_fields=["registration_no"])

    def suggest(self, term):
        return self.client.get(
            self.url,
            {"search": term, "combobox": "1", "allow_create": "0"},
            **COMBOBOX
        )

    def test_it_returns_the_options_fragment_not_the_page(self):
        response = self.suggest("Hafsa")

        self.assertTemplateUsed(
            response, "library/partials/combobox_options.html"
        )
        self.assertTemplateNotUsed(response, "library/borrower_list.html")

    def test_it_finds_a_borrower_by_name(self):
        response = self.suggest("Hafsa")

        self.assertContains(response, "Hafsa Rahmani")
        self.assertNotContains(response, "Junaid Baloch")

    def test_it_finds_a_borrower_by_registration_number(self):
        response = self.suggest("DM-2002")

        self.assertContains(response, "Junaid Baloch")
        self.assertNotContains(response, "Hafsa Rahmani")

    def test_it_finds_a_borrower_by_phone(self):
        response = self.suggest("03451110001")

        self.assertContains(response, "Hafsa Rahmani")

    def test_the_option_carries_the_id_and_the_name(self):
        response = self.suggest("Hafsa")

        self.assertContains(response, 'data-id="%d"' % self.hafsa.id)
        self.assertContains(response, 'data-name="Hafsa Rahmani"')

    def test_the_option_shows_what_tells_two_people_apart(self):
        response = self.suggest("Hafsa")

        self.assertContains(response, "DM-2001")
        self.assertContains(response, "03451110001")

    def test_an_inactive_borrower_is_never_suggested(self):
        self.junaid.is_active = False
        self.junaid.save(update_fields=["is_active"])

        self.assertNotContains(self.suggest("Junaid"), "Junaid Baloch")

    def test_an_empty_search_still_offers_a_starting_list(self):
        response = self.suggest("")

        self.assertContains(response, "Hafsa Rahmani")
        self.assertContains(response, "Junaid Baloch")

    def test_creating_a_borrower_is_never_offered_from_here(self):
        response = self.suggest("Someone Not On File")

        self.assertNotContains(response, "combobox-create")

    def test_the_ordinary_page_is_unaffected(self):
        response = self.client.get(self.url)

        self.assertTemplateUsed(response, "library/borrower_list.html")
        self.assertContains(response, "Hafsa Rahmani")
        self.assertEqual(response.context["paginator"].count, 2)

    def test_the_header_alone_does_not_return_a_fragment(self):
        # Same belt-and-braces rule the other lists use: one URL must not
        # answer with two different bodies off a header alone.
        response = self.client.get(self.url, {"search": "Hafsa"}, **COMBOBOX)

        self.assertTemplateUsed(response, "library/borrower_list.html")


class IssueFormControlTests(TestCase):

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.url = reverse("loan_add")
        self.borrower = make_borrower(name="Hafsa Rahmani")
        self.copy = make_copy(
            volume=make_volume(book=make_book(title="Kitab al-Kharaj")),
            shelf=make_shelf(),
            copy_code="LIB-800001",
        )

    def test_the_borrower_field_is_the_searchable_control(self):
        response = self.client.get(self.url)

        self.assertContains(response, "data-combobox")
        self.assertContains(response, reverse("borrower_list"))

    def test_it_still_posts_a_plain_borrower_id(self):
        response = self.client.get(self.url)

        self.assertContains(response, 'name="borrower"')

    def test_the_page_no_longer_loads_every_borrower(self):
        for number in range(20):
            make_borrower(
                name="Filler %02d" % number,
                phone="0345900%04d" % number,
            )

        response = self.client.get(self.url)

        self.assertNotContains(response, "Filler 05")
        self.assertNotIn("borrowers", response.context)

    def test_a_chosen_borrower_is_shown_back(self):
        response = self.client.get(self.url, {"borrower": self.borrower.id})

        self.assertEqual(response.context["borrower_name"], "Hafsa Rahmani")
        self.assertContains(response, "Hafsa Rahmani")

    def test_the_scan_button_is_present_but_hidden(self):
        response = self.client.get(self.url)

        self.assertContains(response, "data-scan-open")
        self.assertContains(response, 'data-scan-target="q"')
        # `hidden` until the script decides the device can scan, which is
        # what keeps the desktop unchanged.
        self.assertContains(response, "data-scan-open")
        self.assertContains(response, "hidden")

    def test_the_scanner_dialog_is_included_once(self):
        body = self.client.get(self.url).content.decode()

        self.assertEqual(body.count('id="barcodeScanner"'), 1)
        self.assertEqual(body.count("data-scan-video"), 1)

    def test_the_manual_find_button_and_field_are_untouched(self):
        response = self.client.get(self.url)

        self.assertContains(response, 'name="q"')
        self.assertContains(response, "Find")

    def test_the_manual_search_still_finds_a_copy(self):
        # A partial code searches; a whole one is added straight away.
        response = self.client.get(self.url, {"q": "800001"})

        self.assertEqual(
            [c.id for c in response.context["matches"]], [self.copy.id]
        )

    def test_a_scanned_value_lands_the_same_way_a_typed_one_does(self):
        # The scanner writes into `q` and submits the form it belongs to,
        # so what reaches the server is this request - and a whole code now
        # comes back as the copy added, whether it arrived with the
        # scanner's trailing space or without it.
        typed = self.client.get(self.url, {"q": "LIB-800001"})
        scanned = self.client.get(self.url, {"q": " LIB-800001 "})

        self.assertEqual(typed.status_code, 302)
        self.assertEqual(scanned["Location"], typed["Location"])
        self.assertIn("copies=%d" % self.copy.id, scanned["Location"])

    def test_the_copy_search_field_is_inside_a_form(self):
        # The scanner submits `target.form`; a field outside a form would
        # leave it with nothing to submit.
        body = self.client.get(self.url).content.decode()

        before = body.split('id="q"')[0]

        self.assertGreater(before.rfind("<form"), before.rfind("</form>"))


class ReturnLookupControlTests(TestCase):

    def setUp(self):
        make_user(username="librarian_u", password="pass12345", role="Librarian")
        self.client.login(username="librarian_u", password="pass12345")
        self.url = reverse("circulation_return_lookup")

    def test_the_scan_button_targets_the_copy_code_field(self):
        response = self.client.get(self.url)

        self.assertContains(response, "data-scan-open")
        self.assertContains(response, 'data-scan-target="copy_code"')

    def test_it_reuses_the_same_scanner_dialog(self):
        body = self.client.get(self.url).content.decode()

        self.assertEqual(body.count('id="barcodeScanner"'), 1)
        self.assertIn("data-scan-video", body)

    def test_the_manual_field_and_find_button_are_untouched(self):
        response = self.client.get(self.url)

        self.assertContains(response, 'name="copy_code"')
        self.assertContains(response, "Find")

    def test_the_manual_lookup_still_works(self):
        copy = make_copy(
            volume=make_volume(book=make_book(title="Something")),
            shelf=make_shelf(),
            copy_code="LIB-800002",
        )
        from library.tests.helpers import make_loan
        make_loan(copy=copy)

        response = self.client.get(self.url, {"copy_code": "LIB-800002"})

        self.assertIsNotNone(response.context["loan"])

    def test_the_copy_code_field_is_inside_a_form(self):
        body = self.client.get(self.url).content.decode()

        before = body.split('id="copy_code"')[0]

        self.assertGreater(before.rfind("<form"), before.rfind("</form>"))

    def test_every_role_can_reach_it(self):
        for role in ("Admin", "Assistant"):
            make_user(
                username="%s_scan" % role.lower(), password="pass12345",
                role=role,
            )
            self.client.login(
                username="%s_scan" % role.lower(), password="pass12345"
            )

            with self.subTest(role=role):
                self.assertEqual(self.client.get(self.url).status_code, 200)
