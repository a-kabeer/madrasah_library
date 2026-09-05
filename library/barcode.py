"""Code 128 barcodes, drawn as SVG, with nothing to install.

A label needs a machine-readable mark. The value on it is the copy code -
the same `copy_code` the issue and return workflows already scan, not a
second identifier - so all this module does is turn that string into bars.

Code 128 because the scanner already reads it: it is the first format in
the list `app.js` hands to the browser's BarcodeDetector, it encodes the
full ASCII range so a code like `LIB-000123-2` needs no translation, and
Code Set C packs digit pairs into one symbol, which keeps a numeric code
narrow enough to fit a spine label.

SVG rather than a raster image, and generated here rather than by a
library, for three reasons that all point the same way: a printed barcode
has to be exact at the printer's resolution, not at whatever pixel grid an
image was rendered on; an SVG goes into the print page as markup, with no
file to write, serve, or clean up; and the encoding is a table lookup and a
weighted sum, which is less code than the wiring needed to depend on
someone else's implementation of it.

Not a general-purpose barcode library. It encodes what a copy code can
contain and refuses anything else rather than guessing.
"""


# The 107 symbols, as bar/space widths. Index is the Code 128 value; each
# entry is six digits giving the widths of bar, space, bar, space, bar,
# space in modules. Straight from the specification's symbol table - the
# one part of this that is data rather than logic.
PATTERNS = (
    "212222", "222122", "222221", "121223", "121322", "131222", "122213",
    "122312", "132212", "221213", "221312", "231212", "112232", "122132",
    "122231", "113222", "123122", "123221", "223211", "221132", "221231",
    "213212", "223112", "312131", "311222", "321122", "321221", "312212",
    "322112", "322211", "212123", "212321", "232121", "111323", "131123",
    "131321", "112313", "132113", "132311", "211313", "231113", "231311",
    "112133", "112331", "132131", "113123", "113321", "133121", "313121",
    "211331", "231131", "213113", "213311", "213131", "311123", "311321",
    "331121", "312113", "312311", "332111", "314111", "221411", "431111",
    "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114",
    "413111", "241112", "134111", "111242", "121142", "121241", "114212",
    "124112", "124211", "411212", "421112", "421211", "212141", "214121",
    "412121", "111143", "111341", "131141", "114113", "114311", "411113",
    "411311", "113141", "114131", "311141", "411131", "211412", "211214",
    "211232", "233111",
)

# Start codes and the stop pattern. Only B and C are used: B for anything
# with letters or punctuation in it, C for a run of digits.
START_B = 104
START_C = 105
STOP = "2331112"

# Code Set C is worth switching into at four digits and worth staying in
# for as long as the digits last: it halves the symbols, and each symbol
# costs eleven modules, so the saving is real on a narrow label. Below four
# the switch symbol costs more than it saves.
CODE_C_WORTH_IT = 4

# Every character Code Set B can encode, in value order: value 0 is a
# space, and ASCII 32-126 run consecutively from there.
SET_B_FIRST = 32
SET_B_LAST = 126

# The switch symbols, named rather than left as numbers at the call site.
SWITCH_TO_C = 99
SWITCH_TO_B = 100


class BarcodeError(ValueError):
    """The value cannot be encoded as Code 128."""


def encodable(value):
    """True when every character of `value` fits Code Set B.

    Copy codes are generated as `LIB-000123`, which does. A code typed in
    by hand could contain anything, so this is asked before a label is
    drawn rather than discovered while drawing it.
    """

    return bool(value) and all(
        SET_B_FIRST <= ord(character) <= SET_B_LAST for character in value
    )


def _digit_run(value, start):
    """How many digits `value` has in a row from `start`."""

    length = 0

    while start + length < len(value) and value[start + length].isdigit():
        length += 1

    return length


def code_values(value):
    """`value` as a list of Code 128 symbol values, checksum included.

    Switches between Code Set B and Code Set C wherever the digits make it
    worth doing, which is what keeps a mostly-numeric copy code short.
    """

    if not encodable(value):
        raise BarcodeError(
            "Code 128 cannot encode %r: it holds a character outside the "
            "printable ASCII range." % value
        )

    # A code that is entirely digits, in an even number of them, starts in
    # Code Set C and never leaves.
    run = _digit_run(value, 0)

    if run == len(value) and run % 2 == 0:
        values = [START_C]
        in_c = True

    else:
        values = [START_B]
        in_c = False

    index = 0

    while index < len(value):

        if in_c:

            remaining = _digit_run(value, index)

            if remaining >= 2:
                # Two digits, one symbol.
                values.append(int(value[index:index + 2]))
                index += 2
                continue

            # An odd digit left, or a letter: back to B for it.
            values.append(SWITCH_TO_B)
            in_c = False
            continue

        remaining = _digit_run(value, index)

        # Worth switching only for an even run, since C works in pairs.
        usable = remaining - (remaining % 2)

        if usable >= CODE_C_WORTH_IT:
            values.append(SWITCH_TO_C)
            in_c = True
            continue

        values.append(ord(value[index]) - SET_B_FIRST)
        index += 1

    # The check symbol: the start value plus each symbol weighted by its
    # position, modulo 103. The specification's own definition.
    checksum = values[0]

    for position, symbol in enumerate(values[1:], start=1):
        checksum += position * symbol

    values.append(checksum % 103)

    return values


def module_widths(value):
    """`value` as the run-length widths of its bars and spaces.

    The first run is a bar, and they alternate from there - which is all a
    renderer needs to know.
    """

    widths = []

    for symbol in code_values(value):
        widths.extend(int(width) for width in PATTERNS[symbol])

    widths.extend(int(width) for width in STOP)

    return widths


def svg(value, height=38, module=1.0, quiet_zone=10):
    """`value` as an SVG barcode, as a string of markup.

    `module` is the width of the narrowest bar in user units and `height`
    the bar height; the viewBox is sized from them, so the label's CSS
    decides the printed size and the shape stays exact at any of them.

    `quiet_zone` is the blank margin either side, in modules. The
    specification asks for ten, and without it a scanner reads the label's
    border as part of the code.

    One `<path>` rather than a rect per bar: it is the same picture in a
    fraction of the markup, which matters on a sheet of forty labels.
    """

    widths = module_widths(value)

    total = sum(widths) + 2 * quiet_zone

    segments = []
    position = quiet_zone
    is_bar = True

    for width in widths:

        if is_bar:
            segments.append(
                "M%g 0h%gv%gh-%gz"
                % (
                    position * module,
                    width * module,
                    height,
                    width * module,
                )
            )

        position += width
        is_bar = not is_bar

    return (
        '<svg class="label-barcode" xmlns="http://www.w3.org/2000/svg" '
        'viewBox="0 0 %g %g" preserveAspectRatio="none" '
        'role="img" aria-label="Barcode: %s">'
        '<path d="%s" fill="#000"/>'
        "</svg>"
    ) % (
        total * module,
        height,
        value,
        "".join(segments),
    )
