"""The dialogs have to hold still.

Three things moved, and all three moved for the same reason: the box was
sized by whatever happened to be in it.

  * Opening one from a row put a spinner in the body, showed the dialog,
    then swapped a whole record in - so the dialog grew, and because it
    was centred it grew in both directions at once. The header and the
    tabs travelled upwards away from the cursor that had just clicked.

  * Switching a tab swapped a two-line pane for a forty-line one, with the
    same result: the tab strip moved out from under the pointer, so a
    second click landed on a different tab than the one aimed at.

  * A long record in #globalModal, which was not scrollable, stretched the
    dialog past the viewport - taking the header, its close button and the
    footer's Close with it, leaving no way out but Escape.

The fix is in three places and this holds all three: the shape of the two
shared dialogs (base.html), one height for the tab panes (`.detail-tabs`,
shared through `--detail-pane`), and one class for a fragment's own
buttons (`.modal-actions`) so they stay reachable in a body that scrolls.

No browser needed - it reads the templates and the stylesheet.
"""

import pathlib
import re

from django.test import SimpleTestCase

TEMPLATES = pathlib.Path("library/templates/library")
PARTIALS = TEMPLATES / "partials"
STYLESHEET = pathlib.Path("library/static/library/css/style.css")

# The dialog element of each shared container, as it appears in base.html.
DIALOGS = re.compile(
    r'id="(?P<id>globalModal|formModal)".*?'
    r'<div class="(?P<classes>modal-dialog[^"]*)"',
    re.DOTALL,
)


def fragments():
    """Every modal fragment - the things served into a dialog's body."""
    return sorted(PARTIALS.glob("*modal*.html"))


class TheSharedDialogsAreTheSameStableShape(SimpleTestCase):

    def setUp(self):
        self.base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        self.dialogs = {
            m.group("id"): m.group("classes")
            for m in DIALOGS.finditer(self.base)
        }

    def test_both_containers_are_still_there(self):
        """The fix changed their classes, not which dialogs exist."""
        self.assertEqual(
            sorted(self.dialogs), ["formModal", "globalModal"]
        )

    def test_both_scroll_their_body(self):
        """Which is what keeps the header and footer on screen: the box is
        capped at the viewport and the body takes the scrollbar."""
        for name, classes in self.dialogs.items():
            with self.subTest(dialog=name):
                self.assertIn("modal-dialog-scrollable", classes)

    def test_neither_is_centred(self):
        """Centred, a height change moves the top edge. Anchored, it moves
        only the bottom one - so nothing the reader is looking at, or
        about to click, travels."""
        for name, classes in self.dialogs.items():
            with self.subTest(dialog=name):
                self.assertNotIn("modal-dialog-centered", classes)

    def test_both_are_the_same_width(self):
        """The borrower and copy fragments open into either container,
        depending on what was clicked. At two different widths the same
        record read as two different dialogs."""
        self.assertIn("modal-lg", self.dialogs["globalModal"])
        self.assertIn("modal-lg", self.dialogs["formModal"])

    def test_the_header_close_and_the_footer_close_survive(self):
        """Scrollable pins both; this is what would notice them being
        dropped from the shell."""
        self.assertIn('class="btn-close"', self.base)
        self.assertIn('class="modal-footer"', self.base)


class EveryTabbedDialogGivesItsPanesOneHeight(SimpleTestCase):

    def test_no_tabbed_fragment_leaves_its_panes_to_size_themselves(self):
        offenders = []

        for path in fragments():
            body = path.read_text(encoding="utf-8")

            if 'class="tab-content' not in body:
                continue

            if "detail-tabs" not in body:
                offenders.append(path.name)

        self.assertEqual(
            offenders,
            [],
            "tab panes without `detail-tabs`, so the dialog resizes on "
            "every tab change: " + ", ".join(offenders),
        )

    def test_the_height_is_named_once_and_shared(self):
        """`.detail-tabs` and the loading box have to agree, or the
        fragment landing resizes the dialog the spinner had sized. One
        custom property, read by both."""
        css = STYLESHEET.read_text(encoding="utf-8")

        self.assertIn("--detail-pane:", css)
        self.assertIn("height: var(--detail-pane)", css)
        self.assertIn("min-height: var(--detail-pane)", css)

    def test_a_pane_that_overflows_scrolls_inside_its_own_box(self):
        css = STYLESHEET.read_text(encoding="utf-8")

        detail_tabs = css.split(".detail-tabs {", 1)[1].split("}", 1)[0]

        self.assertIn("overflow-y: auto", detail_tabs)


class EveryFragmentPutsItsButtonsInTheOneRow(SimpleTestCase):

    def test_a_fragment_with_buttons_has_an_actions_row(self):
        """There were five sets of utilities doing this job, differing in
        whether they wrapped, justified or drew a rule. One class now, so
        the buttons sit in the same place in every dialog - and stick to
        the bottom of a body that scrolls instead of going under it."""
        offenders = []

        for path in fragments():
            body = path.read_text(encoding="utf-8")

            has_buttons = (
                'type="submit"' in body
                or 'data-bs-dismiss="modal"' in body
            )

            if has_buttons and "modal-actions" not in body:
                offenders.append(path.name)

        self.assertEqual(
            offenders,
            [],
            "fragments whose buttons are not in a `modal-actions` row: "
            + ", ".join(offenders),
        )

    def test_no_fragment_carries_a_modal_footer_of_its_own(self):
        """`.modal-footer` is part of a dialog's shell and has to be a
        sibling of the body to stay pinned. Inside the body it is a
        footer-shaped block that scrolls away with everything else."""
        offenders = [
            path.name
            for path in fragments()
            if "modal-footer" in path.read_text(encoding="utf-8")
        ]

        self.assertEqual(offenders, [], ", ".join(offenders))

    def test_the_row_only_becomes_a_sticky_footer_inside_a_dialog(self):
        """These fragments are also what a plain request gets back, with
        no dialog around them. A bar stuck to the bottom of the window
        there would be a bar in the middle of nothing."""
        css = STYLESHEET.read_text(encoding="utf-8")

        self.assertIn(".modal-body .modal-actions {", css)

        sticky = css.split(".modal-body .modal-actions {", 1)[1]
        sticky = sticky.split("}", 1)[0]

        self.assertIn("position: sticky", sticky)

        plain = css.split("\n.modal-actions {", 1)[1].split("}", 1)[0]

        self.assertNotIn("position: sticky", plain)


class ADialogsScrollIsItsOwn(SimpleTestCase):

    def test_reaching_the_end_does_not_scroll_the_page_behind_it(self):
        css = STYLESHEET.read_text(encoding="utf-8")

        self.assertIn("overscroll-behavior: contain", css)
