"""Urdu formats, with Latin digits kept.

Django's own `ur` locale renders numbers with Eastern Arabic digits
(٠١٢٣٤٥٦٧٨٩) and dates in a different order. For most applications that is
right. For a library it is not: a copy code, an ISBN, a loan number and a
shelf code are *identifiers*. They are read off a spine, typed into a
search box, and scanned by a barcode reader that only knows Latin digits -
so they must look the same in every language.

`USE_THOUSAND_SEPARATOR` is off project-wide, so nothing here needs to undo
grouping; what these two lines undo is the digit substitution and the date
order.

The dates are written in the same order the English pages use, so a report
exported in Arabic and one exported in English sort and compare the same
way.
"""

# The one that matters: no Eastern Arabic digits.
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
