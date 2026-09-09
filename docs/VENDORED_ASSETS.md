# Vendored front-end assets

The files themselves are in `library/static/library/vendor/`. This note lives
here rather than beside them so that `collectstatic` does not publish it.

Bootstrap, Bootstrap Icons and htmx, served from this repository rather than
from a CDN. Nothing here is modified except as noted below.

## Why they are here and not on a CDN

`base.html` used to load all six of these from `cdn.jsdelivr.net` and
`unpkg.com`, with no `integrity` attribute on any of them. Two problems, one
of which is fatal:

* **Availability.** Without Bootstrap's CSS the app has no layout, and
  without htmx no link in the shell navigates - `hx-boost` is what turns
  every sidebar entry into a swap of `#mainContent`. A madrasah whose
  network is intermittent, filtered, or entirely local therefore does not
  get a degraded app; it gets an unusable one. The i18n plan already made
  this argument about the Urdu and Arabic fonts. It is stronger here.

* **Integrity.** With no subresource integrity, whatever those two hosts
  return is executed with the privileges of whoever is signed in. A pinned
  version number is not a pinned file.

Serving them from `static/` removes both: the files are in the repository,
WhiteNoise gives them a content-addressed name and a long cache lifetime
through `CompressedManifestStaticFilesStorage`, and there is nothing left to
verify because nothing is fetched from anywhere.

## What is here

| Path | Package | Version | Source |
|---|---|---|---|
| `bootstrap-5.3.8/bootstrap.min.css` | `bootstrap` | 5.3.8 | `dist/css/bootstrap.min.css` |
| `bootstrap-5.3.8/bootstrap.rtl.min.css` | `bootstrap` | 5.3.8 | `dist/css/bootstrap.rtl.min.css` |
| `bootstrap-5.3.8/bootstrap.bundle.min.js` | `bootstrap` | 5.3.8 | `dist/js/bootstrap.bundle.min.js` |
| `bootstrap-icons-1.13.1/bootstrap-icons.min.css` | `bootstrap-icons` | 1.13.1 | `font/bootstrap-icons.min.css` |
| `bootstrap-icons-1.13.1/fonts/bootstrap-icons.woff2` | `bootstrap-icons` | 1.13.1 | `font/fonts/` |
| `bootstrap-icons-1.13.1/fonts/bootstrap-icons.woff` | `bootstrap-icons` | 1.13.1 | `font/fonts/` |
| `htmx-2.0.8/htmx.min.js` | `htmx.org` | 2.0.8 | `dist/htmx.min.js` |

These are the same files the CDN URLs served: jsdelivr and unpkg both mirror
npm, and `https://unpkg.com/htmx.org@2.0.8` with no path resolves through the
package's `unpkg` field, which is `dist/htmx.min.js`.

The version is in the directory name so an upgrade is visible in the diff and
in the URL, and so two versions can sit side by side for the length of one.

`bootstrap-icons.min.css` refers to its fonts as `fonts/bootstrap-icons.woff2`,
which is why the `fonts/` directory sits beside it exactly as it does in the
package.

## The one modification

The trailing `sourceMappingURL` comment is removed from the three Bootstrap
files. The `.map` files it points at come to about 2.5 MB, and
`ManifestStaticFilesStorage` refuses to build a manifest that references a
file it cannot find - so the choice was to carry the maps or drop the
reference. Nothing else is touched: no reformatting, no re-minifying.

## Upgrading

    npm pack bootstrap@<version> bootstrap-icons@<version> htmx.org@<version>

then unpack, copy the files in the table above into a directory named for the
new version, strip the three `sourceMappingURL` comments, update the six
`{% static %}` paths in `library/templates/library/base.html` and
`library/templates/public/base.html`, delete the old directory, and run
`library/tests/test_no_external_assets.py` - it fails if any template starts
pointing at a CDN again, and if any path named in a template is missing from
this directory.
