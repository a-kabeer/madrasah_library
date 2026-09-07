# Arabic-script fonts

Four WOFF2 faces, self-hosted:

    NotoNastaliqUrdu-Regular.woff2    172 KB
    NotoNastaliqUrdu-Bold.woff2       171 KB
    NotoNaskhArabic-Regular.woff2      69 KB
    NotoNaskhArabic-Bold.woff2         70 KB

481 KB in total. Both families are from Google's Noto project and are
licensed under the SIL Open Font License; the licence text for each sits
beside the files as `OFL-Noto_Nastaliq_Urdu.txt` and
`OFL-Noto_Naskh_Arabic.txt`, because the licence requires the copyright
notice to travel with the fonts.

Nastaliq for Urdu and Naskh for Arabic, which is not interchangeable: Urdu
is conventionally set in Nastaliq's sloping, cursive style, and Naskh reads
as a foreign typographic convention to an Urdu reader even though every
letter is legible. Arabic is the reverse.

## Where they came from

Downloaded from fonts.google.com as TTF (variable font plus static
weights) and converted here to WOFF2 - the same glyph data under Brotli
compression, about 66% smaller with nothing else changed:

    pip install fonttools brotli
    python -c "from fontTools.ttLib import TTFont; \
        f = TTFont('NotoNastaliqUrdu-Regular.ttf'); \
        f.flavor = 'woff2'; f.save('NotoNastaliqUrdu-Regular.woff2')"

`fonttools` is a build-time tool and is deliberately **not** in
`requirements.txt` - nothing at runtime needs it.

The static Regular and Bold are used rather than the variable font,
because `style.css` declares two discrete weights and a variable font
would ship every weight in between to render two of them.

## Why these are committed rather than fetched

There is no build step that could download them. `compilemessages` can
rebuild the translation catalogues on deploy, so `.mo` files are gitignored
- but a font has nothing to be rebuilt from, so a missing file here means
the deployed app silently falls back to whatever Arabic face the visitor's
device happens to have.

Self-hosted rather than loaded from a CDN on purpose: a madrasah's network
may be local, and a font that only arrives when the internet does is a page
that sometimes renders in the wrong script.

## The CSS side

Already in `library/static/library/css/style.css`, in the ARABIC-SCRIPT
TYPOGRAPHY section:

* four `@font-face` rules naming the files above
* a `unicode-range` limited to the Arabic blocks, so Latin text and all
  digits stay on the system stack - which is what keeps a copy code or an
  ISBN readable and in Latin numerals
* the extra `line-height` Nastaliq needs, since its descenders are deep
  enough to collide with the next line at ordinary leading
