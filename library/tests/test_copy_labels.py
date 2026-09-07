"""Printable labels, from the copy code the scanner already reads.

The point of these is that the label and the scanner agree. The value
encoded is `copy_code` and nothing else, so a label printed here is read
back by the issue and return workflows as the copy it names - which is why
several of these tests decode the barcode rather than trusting that it was
drawn.

The encoder itself is `library/barcode.py`: pure Code 128, no dependency,
and checked below against the specification's own arithmetic rather than
against its own output.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from library import barcode
from library.models import BookCopy

from .helpers import (
    main_content,
    make_author,
    make_book,
    make_copy,
    make_location,
    make_shelf,
    make_user,
    make_volume,
)


def decode(widths):
    """Read a Code 128 run-length sequence back into its symbol values.

    The inverse of `barcode.module_widths`, so a test can ask what a
    printed barcode actually says instead of comparing it against the thing
    that drew it.
    """

    patterns = {pattern: value for value, pattern in enumerate(barcode.PATTERNS)}

    # Six runs per symbol; the stop pattern is seven and is not one.
    symbol_runs = len(widths) - len(barcode.STOP)

    assert symbol_runs % 6 == 0, "not a whole number of symbols"
    assert (
        "".join(str(width) for width in widths[symbol_runs:])
        == barcode.STOP
    ), "no stop pattern"

    return [
        patterns["".join(str(w) for w in widths[index:index + 6])]
        for index in range(0, symbol_runs, 6)
    ]


def decoded_text(value):
    """What a scanner reading `barcode.module_widths(value)` would report."""

    symbols = decode(barcode.module_widths(value))

    in_c = symbols[0] == barcode.START_C
    out = []

    for symbol in symbols[1:-1]:      # not the start symbol, not the check

        # Which value means "switch" depends on the set you are in: 99 is
        # the digit pair "99" inside Code Set C, and only means switch-to-C
        # while reading Code Set B.
        if in_c:

            if symbol == barcode.SWITCH_TO_B:
                in_c = False
                continue

            out.append("%02d" % symbol)
            continue

        if symbol == barcode.SWITCH_TO_C:
            in_c = True
            continue

        out.append(chr(symbol + barcode.SET_B_FIRST))

    return "".join(out)


class LabelTestCase(TestCase):

    def setUp(self):
        self.admin = make_user(
            username="admin_u", password="pass12345", role="Admin"
        )
        self.client.login(username="admin_u", password="pass12345")

        self.location = make_location(name="Main Hall")
        self.shelf = make_shelf(location=self.location, shelf_code="A-1")

        self.author = make_author("Abu Yusuf")
        self.book = make_book(title="Kitab al-Kharaj", author=self.author)
        self.volume = make_volume(book=self.book, volume_number=1, title="")

        self.copy = self.a_copy("LIB-500001")

        self.url = reverse("book_copy_labels")

    def a_copy(self, code, volume=None, status="Available"):
        return make_copy(
            volume=volume or self.volume,
            shelf=self.shelf,
            copy_code=code,
            status=status,
        )

    def sheet(self, **params):
        return self.client.get(self.url, params)

    def as_role(self, role):
        self.client.logout()
        make_user(username="l_%s" % role, password="pass12345", role=role)
        self.client.login(username="l_%s" % role, password="pass12345")


class EncoderTests(TestCase):
    """The barcode itself, checked against Code 128 rather than itself."""

    def test_the_checksum_matches_the_specification(self):
        # "CODE128" in Code Set B: 104 + 1*35 + 2*47 + 3*36 + 4*37 + 5*17
        # + 6*18 + 7*24 = 850, and 850 mod 103 is 26.
        self.assertEqual(barcode.code_values("CODE128")[-1], 26)

    def test_a_run_of_digits_uses_code_set_c(self):
        # Six digits become three symbols, not six.
        values = barcode.code_values("000123")

        self.assertEqual(values[0], barcode.START_C)
        self.assertEqual(values[1:-1], [0, 1, 23])

    def test_a_short_run_of_digits_is_not_worth_switching_for(self):
        # The switch symbol costs more than the pairing saves.
        self.assertNotIn(barcode.SWITCH_TO_C, barcode.code_values("A12"))

    def test_a_generated_copy_code_reads_back_as_itself(self):
        self.assertEqual(decoded_text("LIB-000123"), "LIB-000123")

    def test_codes_of_every_shape_read_back_as_themselves(self):
        for code in (
            "LIB-000001",
            "LIB-999999",
            "LIB-000123-2",
            "A1",
            "000123",
            "SHELF/A-1 #7",
        ):
            with self.subTest(code=code):
                self.assertEqual(decoded_text(code), code)

    def test_a_code_it_cannot_hold_is_refused_not_mangled(self):
        with self.assertRaises(barcode.BarcodeError):
            barcode.svg("عربي")

    def test_the_svg_carries_the_code_for_a_screen_reader(self):
        self.assertIn('aria-label="Barcode: LIB-000123"',
                      barcode.svg("LIB-000123"))


class SingleLabelTests(LabelTestCase):

    def test_one_copy_prints_one_label(self):
        response = self.sheet(copy=self.copy.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [c.id for c in response.context["copies"]], [self.copy.id]
        )

    def test_the_copy_page_offers_it(self):
        response = self.client.get(
            reverse("book_copy_detail", args=[self.copy.id])
        )

        self.assertContains(
            response, "%s?copy=%d" % (self.url, self.copy.id)
        )

    def test_the_label_carries_what_it_has_to(self):
        response = self.sheet(copy=self.copy.id)

        for expected in (
            "Madrasah Library",     # institution
            "Kitab al-Kharaj",      # title
            "Abu Yusuf",            # author
            "LIB-500001",           # copy code, as text
        ):
            with self.subTest(shows=expected):
                self.assertContains(response, expected)

    def test_a_volume_is_named_when_there_is_one(self):
        volume = make_volume(
            book=self.book, volume_number=3, title="Land Tax"
        )
        copy = self.a_copy("LIB-500002", volume=volume)

        response = self.sheet(copy=copy.id)

        self.assertContains(response, "Vol 3")
        self.assertContains(response, "Land Tax")

    def test_a_single_volume_book_says_nothing_about_volumes(self):
        self.assertNotContains(self.sheet(copy=self.copy.id), "Vol 1")

    def test_the_barcode_encodes_the_copy_code_exactly(self):
        response = self.sheet(copy=self.copy.id)

        printed = response.context["copies"][0]

        self.assertEqual(
            printed.label_barcode, barcode.svg("LIB-500001")
        )
        self.assertEqual(decoded_text("LIB-500001"), "LIB-500001")

    def test_the_value_on_the_label_is_the_value_the_scanner_looks_up(self):
        # The identity this whole task rests on: one code, both directions.
        from library.views import copy_for_code

        printed = self.sheet(copy=self.copy.id).context["copies"][0]

        self.assertEqual(
            copy_for_code(decoded_text(printed.copy_code)).id, self.copy.id
        )

    def test_a_code_the_encoder_cannot_hold_still_prints_a_label(self):
        copy = self.a_copy("LIB-عربي")

        response = self.sheet(copy=copy.id)

        self.assertIsNone(response.context["copies"][0].label_barcode)
        self.assertContains(response, "LIB-عربي")


class BulkLabelTests(LabelTestCase):

    def setUp(self):
        super().setUp()

        self.second = self.a_copy("LIB-500010")
        self.third = self.a_copy("LIB-500011")

    def test_several_copies_print_together(self):
        response = self.sheet(copy=[self.copy.id, self.third.id])

        self.assertEqual(
            [c.copy_code for c in response.context["copies"]],
            ["LIB-500001", "LIB-500011"],
        )

    def test_the_sheet_is_ordered_by_code_whatever_order_it_was_asked_in(self):
        response = self.sheet(
            copy=[self.third.id, self.copy.id, self.second.id]
        )

        self.assertEqual(
            [c.copy_code for c in response.context["copies"]],
            ["LIB-500001", "LIB-500010", "LIB-500011"],
        )

    def test_the_list_offers_it_for_a_selection(self):
        response = self.client.get(reverse("book_copy_list"))

        self.assertContains(response, self.url)
        self.assertContains(response, "Print Labels")

    def test_the_list_offers_it_for_the_whole_result_set(self):
        response = self.client.get(reverse("book_copy_list"))

        self.assertContains(response, "%s?all=1" % self.url)

    def test_selecting_all_visible_prints_all_visible(self):
        # What the select-all checkbox amounts to on the server: every id
        # on the page, ticked.
        listed = self.client.get(reverse("book_copy_list"))
        ids = [c.id for c in listed.context["copies"]]

        response = self.sheet(copy=ids)

        self.assertEqual(len(response.context["copies"]), len(ids))

    def test_the_filtered_set_prints_what_the_filter_shows(self):
        other_book = make_book(title="Al-Muwatta", author=make_author("Malik"))
        other_volume = make_volume(book=other_book, volume_number=1, title="")
        self.a_copy("LIB-500020", volume=other_volume)

        listed = self.client.get(
            reverse("book_copy_list"), {"search": "Muwatta"}
        )
        response = self.sheet(all="1", search="Muwatta")

        self.assertEqual(
            [c.copy_code for c in response.context["copies"]],
            ["LIB-500020"],
        )
        self.assertEqual(
            [c.copy_code for c in listed.context["copies"]],
            ["LIB-500020"],
        )

    def test_a_status_filter_prints_the_same_copies_the_list_shows(self):
        self.a_copy("LIB-500030", status="Transferred")

        listed = self.client.get(
            reverse("book_copy_list"), {"status": "transferred"}
        )
        response = self.sheet(all="1", status="transferred")

        self.assertEqual(
            {c.id for c in response.context["copies"]},
            {c.id for c in listed.context["copies"]},
        )

    def test_neither_a_selection_nor_a_filter_prints_nothing(self):
        # Rather than quietly printing the whole catalogue.
        response = self.sheet()

        self.assertEqual(list(response.context["copies"]), [])
        self.assertContains(response, "Nothing to print")

    def test_a_very_large_sheet_is_capped_and_says_so(self):
        from library.views import LABEL_LIMIT

        for number in range(LABEL_LIMIT + 5):
            self.a_copy("LIB-51%04d" % number)

        response = self.sheet(all="1")

        self.assertEqual(len(response.context["copies"]), LABEL_LIMIT)
        self.assertTrue(response.context["capped"])
        self.assertContains(response, "matched, and the first")


class LabelValidationTests(LabelTestCase):
    """Every id is read back from the database, whatever was sent."""

    def test_an_unknown_id_prints_nothing_for_itself(self):
        response = self.sheet(copy=[self.copy.id, 999999])

        self.assertEqual(
            [c.id for c in response.context["copies"]], [self.copy.id]
        )

    def test_a_non_numeric_id_is_ignored(self):
        response = self.sheet(copy=[self.copy.id, "abc", "1; DROP TABLE"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [c.id for c in response.context["copies"]], [self.copy.id]
        )

    def test_a_repeated_id_prints_one_label(self):
        response = self.sheet(copy=[self.copy.id, self.copy.id])

        self.assertEqual(len(response.context["copies"]), 1)

    def test_nothing_but_nonsense_prints_nothing(self):
        response = self.sheet(copy=["abc", "-1"])

        self.assertEqual(list(response.context["copies"]), [])

    def test_the_code_printed_is_the_stored_one_not_the_submitted_one(self):
        # There is no way to have this render a code of the caller's
        # choosing: only ids go in, and the code comes from the row.
        response = self.sheet(copy=self.copy.id, copy_code="FORGED-1")

        sheet = main_content(response.content.decode())

        self.assertIn("LIB-500001", sheet)

        # Checked against the sheet rather than the whole response: the
        # topbar's language form carries `next="{{ request.get_full_path }}"`,
        # so any query string the caller sent is echoed back in a hidden
        # input. Harmless - it is escaped, and it is not what gets printed -
        # but it means the whole document is no longer the right place to
        # ask what the label says.
        self.assertNotIn("FORGED-1", sheet)


class LabelPermissionTests(LabelTestCase):

    def test_the_roles_that_manage_copies_may_print(self):
        for role in ("Admin", "Librarian"):
            with self.subTest(role=role):
                self.as_role(role)

                self.assertEqual(
                    self.sheet(copy=self.copy.id).status_code, 200
                )

    def test_an_assistant_may_not(self):
        self.as_role("Assistant")

        self.assertEqual(self.sheet(copy=self.copy.id).status_code, 403)

    def test_an_assistant_is_not_offered_it_on_the_copy_page(self):
        self.as_role("Assistant")

        response = self.client.get(
            reverse("book_copy_detail", args=[self.copy.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.url)

    def test_an_assistant_is_not_offered_it_on_the_list(self):
        self.as_role("Assistant")

        response = self.client.get(reverse("book_copy_list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.url)

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        self.client.logout()

        response = self.sheet(copy=self.copy.id)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])


class LabelQueryTests(LabelTestCase):

    def test_a_sheet_of_many_costs_no_more_queries_than_a_sheet_of_one(self):
        with CaptureQueriesContext(connection) as one:
            self.sheet(copy=self.copy.id)

        ids = [self.copy.id]

        for number in range(40):
            ids.append(self.a_copy("LIB-52%04d" % number).id)

        with CaptureQueriesContext(connection) as many:
            response = self.sheet(copy=ids)

        self.assertEqual(len(response.context["copies"]), 41)
        self.assertEqual(len(one), len(many))

    def test_the_labels_render_without_a_query_each(self):
        for number in range(20):
            self.a_copy("LIB-53%04d" % number)

        with CaptureQueriesContext(connection) as queries:
            body = self.sheet(all="1").content.decode()

        # The book, the author and the shelf all arrive with the copy.
        self.assertIn("Kitab al-Kharaj", body)
        self.assertIn("Abu Yusuf", body)

        self.assertEqual(
            len([
                q for q in queries.captured_queries
                if 'FROM "book_copies"' in q["sql"]
            ]),
            2,      # one COUNT for the cap notice, one for the rows
        )


class PrintLayoutTests(LabelTestCase):
    """What the paper gets, and what it does not."""

    def test_the_screen_controls_are_marked_to_be_hidden(self):
        response = self.sheet(copy=self.copy.id)

        self.assertContains(response, "print-hidden")

    def test_the_sheet_itself_is_not_hidden(self):
        body = self.sheet(copy=self.copy.id).content.decode()

        sheet = body.index('class="label-sheet"')
        controls = body.index("print-hidden")

        # The controls are above and marked; the sheet below is not.
        self.assertLess(controls, sheet)
        self.assertNotIn(
            "print-hidden",
            body[sheet:body.index("label-footer")],
        )

    def test_the_paper_says_when_and_by_whom_it_was_printed(self):
        response = self.sheet(copy=self.copy.id)

        self.assertContains(response, "print-only")
        self.assertContains(response, "printed")

    def test_the_stylesheet_hides_the_chrome_and_keeps_labels_whole(self):
        import io
        import os

        from django.conf import settings

        css = io.open(
            os.path.join(
                settings.BASE_DIR,
                "library", "static", "library", "css", "style.css",
            ),
            encoding="utf-8",
        ).read()

        printing = css[css.index("@media print"):]

        for rule in (".sidebar", ".topbar", ".print-hidden"):
            with self.subTest(hidden=rule):
                self.assertIn(rule, printing)

        self.assertIn("break-inside: avoid", css)
        self.assertIn("page-break-inside: avoid", css)


class CopyListUnchangedTests(LabelTestCase):
    """The list still behaves exactly as it did."""

    def test_the_filters_still_filter(self):
        other = make_book(title="Al-Muwatta", author=make_author("Malik"))
        volume = make_volume(book=other, volume_number=1, title="")
        wanted = self.a_copy("LIB-540001", volume=volume)

        response = self.client.get(
            reverse("book_copy_list"), {"search": "Muwatta"}
        )

        self.assertEqual(
            [c.id for c in response.context["copies"]], [wanted.id]
        )

    def test_the_results_fragment_is_still_a_fragment(self):
        response = self.client.get(
            reverse("book_copy_list") + "?partial=results",
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="copyResults",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<!DOCTYPE html>", response.content.decode())

    def test_a_book_filter_from_another_page_still_works(self):
        response = self.client.get(
            reverse("book_copy_list"), {"book": self.book.id}
        )

        self.assertIn(
            self.copy.id, [c.id for c in response.context["copies"]]
        )
