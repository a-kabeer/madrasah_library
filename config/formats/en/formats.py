"""English formats, so an unfiltered date matches every filtered one.

`config/formats/ur/formats.py` and `config/formats/ar/formats.py` have been
here since the app was made translatable, along with the
`FORMAT_MODULE_PATH` that finds them, and they say what they are for. This
is the English sibling they never had, and it exists because of the
seventeen places that print a date with no `|date` filter at all.

37 templates write `|date:"j M Y"` and 11 write `j M Y, H:i`. The
seventeen wrote nothing. In Urdu and Arabic that was already fine - those
modules set `DATE_FORMAT = "j M Y"` - but English had no module here, so it
fell through to Django's own `en` locale format, `N j, Y`. A loan issued on
9 March therefore read "9 Mar 2026" on the loans list and "March 9, 2026"
on the book's own page, and the dashboard's activity feed disagreed with
the activity log page it links to, one click apart. The two right-to-left
languages were the only ones getting it right.

`DATE_FORMAT` in settings.py cannot fix that: formats are looked up in the
active locale's format module before the settings fall-back, and Django
ships one for `en`. A module here is the only place that wins. Adding it
also fixes the eighteenth unfiltered date, which a seventeenth `|date`
filter would not have.

The values are deliberately the ones the Urdu and Arabic modules already
chose, down to the ISO `SHORT_DATE_FORMAT`, so that all three languages
render a date identically apart from the translated month name - and so
that there is one convention here rather than three.
"""

# Identical to the ur and ar modules: one separator convention across every
# language, matching the copy codes and ISBNs printed beside the numbers.
NUMBER_GROUPING = 0
DECIMAL_SEPARATOR = "."
THOUSAND_SEPARATOR = ","

DATE_FORMAT = "j M Y"
DATETIME_FORMAT = "j M Y H:i"
SHORT_DATE_FORMAT = "Y-m-d"
SHORT_DATETIME_FORMAT = "Y-m-d H:i"

# What a date typed into a form may look like. ISO first, because that is
# what `<input type="date">` submits whatever the page language is.
DATE_INPUT_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
]
