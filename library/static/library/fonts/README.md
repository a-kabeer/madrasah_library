# Arabic-script fonts

Four files go here. They are not in the repository: ~1 MB of binary that
belongs with the deployment rather than the source.

    NotoNastaliqUrdu-Regular.woff2
    NotoNastaliqUrdu-Bold.woff2
    NotoNaskhArabic-Regular.woff2
    NotoNaskhArabic-Bold.woff2

Both families are from Google's Noto project and are licensed under the
SIL Open Font License. Download the TTFs from fonts.google.com
(Noto Nastaliq Urdu, Noto Naskh Arabic) and convert them to WOFF2, or take
the `.woff2` files straight from the Google Fonts CSS API.

Self-hosted rather than loaded from a CDN on purpose: a madrasah's network
may be local, and a font that only arrives when the internet does is a page
that sometimes renders in the wrong script.

The `@font-face` rules, the `unicode-range` that keeps Latin text and digits
on the system stack, and the line-height Nastaliq needs are all already in
`library/static/library/css/style.css` (see the ARABIC-SCRIPT TYPOGRAPHY
section). Until the files are here, Urdu and Arabic render in the system's
own Arabic-script face - legible, but not Nastaliq.
