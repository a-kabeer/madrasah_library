# Madrasah Library

A library management system for a madrasah: the catalogue, the copies on the
shelves, who has borrowed what, and the reports that answer for it
afterwards. Django 6.1 on PostgreSQL, with a public read-only catalogue
alongside the staff application.

Three languages (English, Urdu, Arabic), light and dark themes, and an
Admin-editable menu-permission matrix so a librarian's reach can be changed
without a deploy.

## The one thing to know before changing anything

**Every model is `managed = False`, and the PostgreSQL schema is the source
of truth.** `scripts/test_schema.sql` is that schema. Django's autodetector
will happily write an `AddField` that changes project state and never touches
the database, so migrations here pair explicit SQL with the matching state
operation:

```python
migrations.SeparateDatabaseAndState(
    database_operations=[migrations.RunSQL(
        sql="ALTER TABLE users ADD COLUMN IF NOT EXISTS …;",
        reverse_sql="ALTER TABLE users DROP COLUMN IF EXISTS …;",
    )],
    state_operations=[migrations.AddField(…)],
)
```

`IF NOT EXISTS` throughout, because the test database is a clone of the
schema reused with `--keepdb` rather than something built by running
migrations. An index needs no state operation at all - no `Meta.indexes` is
declared - so those migrations are plain `RunSQL`. See
`library/migrations/0012_language_preference.py` for the column shape and
`0013_case_insensitive_search_indexes.py` for the index shape.

`docs/DATABASE.md` documents the tables, the constraints and why each one is
where it is.

## Getting started

Requires PostgreSQL with the `pg_trgm` extension (developed against 16) and
Python 3.12 or 3.13 — `render.yaml` pins 3.12.7 for the deploy, and 3.13 is
what this was last developed on.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit DB_PASSWORD and SECRET_KEY

createdb madrasah_library
psql -d madrasah_library -f scripts/test_schema.sql

python manage.py migrate      # records the migrations; the SQL above did the work
python manage.py createcachetable
python manage.py runserver
```

The cache table is not optional: the feature-permission matrix, the dashboard
counts and the login rate limiter all use `DatabaseCache`, so that every
Gunicorn worker sees the same state.

## Running the tests

```bash
python manage.py test library --keepdb
```

Two things will bite you.

**One process at a time.** `--keepdb` means every process shares one test
database, and two of them deadlock. Run one.

**The browser tests need Playwright and Chromium**, and skip cleanly without
them - so a green run does not necessarily mean they ran.

```bash
pip install playwright && playwright install chromium
python manage.py test library.tests.test_browser_pages --keepdb
```

They are worth having. They are what caught a search box printing
`{% translate '…' %}` at the reader, two report pages printing
`{{ heading }}` as their own heading, a phone-width table making a 375px
viewport into a 598px document, and 374 WCAG contrast failures. None of those
raise, none of them fail a view test, and `manage.py check` passes with all
of them present.

`library/tests/test_browser_accessibility.py` also runs the whole of
axe-core when you point it at one:

```bash
npm pack axe-core && tar xzf axe-core-*.tgz
AXE_CORE_PATH=package/axe.min.js python manage.py test \
    library.tests.test_browser_accessibility --keepdb
```

## How the application is put together

**Navigation is htmx, not pages.** `.app-wrapper` in
`library/templates/library/base.html` carries `hx-boost="true"` and
`hx-target="#mainContent"`, so an ordinary link inside the shell is fetched
in the background and swapped into the content region. Every page writes
`{% extends layout %}`, and the server answers such a request with that
region alone: one URL, one view, one template, two amounts of page. Direct
URLs, refresh, Back and middle-click all still work, because it is only an
interception.

That makes one browser session one document, which is what
`library/static/library/js/app.js` is organised around: `setupEach` keeps a
`WeakSet` of elements it has already wired, `setupOnce` installs one
delegated listener the first time its section appears, and 25 of the 67
listeners are delegated from `document.body`. A listener attached to an
element a swap replaces would otherwise be attached twice.

**Adding, editing and deleting happen in a shared dialog.** A control carries
`data-form-modal` plus `hx-get="…?modal=1"` targeting `#formModal
.modal-body`; the view answers a fragment for a modal request and a whole
page otherwise, and replies to a successful POST with `204` plus
`HX-Redirect`, or `HX-Trigger` to refresh the list in place.

**Permissions have a ceiling in code and a switch in the database.**
`library/features.py` names 22 features, each with the roles that may *ever*
reach it; `library/permissions.py`'s `@feature_required("books", "Admin",
"Librarian")` checks that ceiling **before** the database toggle, so no row
in `role_features` can let a role past what the code allows. A missing row
means the code's default. Hiding a menu entry is never the boundary - the
decorator is - and typing the URL of a switched-off feature gets the same 403
as typing the URL of a view your role never had.

**Front-end assets are vendored, not fetched.** Bootstrap, Bootstrap Icons
and htmx live under `library/static/library/vendor/`. See
`docs/VENDORED_ASSETS.md` for why (a madrasah's network may be local-only,
and a CDN with no subresource integrity runs whatever it returns as whoever
is signed in) and for how to upgrade them.

## Layout

| Path | What is in it |
|---|---|
| `library/models.py` | Every table, all `managed = False` |
| `library/views/` | The web layer, split by area; `common.py` is what two or more of them share |
| `library/features.py` | The 22 switchable features and their ceilings |
| `library/permissions.py` | `feature_required`, `role_required`, and the UI-gating predicates |
| `library/policy.py`, `reservations.py`, `inventory.py`, `notifications.py` | Business rules that are not about HTTP |
| `library/analytics.py`, `reports.py` | The queries behind Analytics and the six reports |
| `library/excel.py`, `barcode.py` | Workbook import/export and Code 128 labels |
| `library/public_views.py` | The anonymous catalogue, mounted at `/catalog/` |
| `library/templates/library/` | The staff application; `partials/` holds the fragments |
| `library/templates/public/` | The public catalogue |
| `library/static/library/css/style.css` | The whole theme, as design tokens |
| `config/formats/` | Date and number formats per language - one format, Latin digits |
| `scripts/test_schema.sql` | The schema, which is the source of truth |
| `locale/` | Urdu and Arabic catalogues (`.mo` files are gitignored; run `compilemessages`) |

## State of the translations

The scaffolding is complete - `LocaleMiddleware`, a per-user stored language,
RTL layout, the switcher, and per-language number and date formats that keep
the digits Latin so a copy code reads the same in every language. The
catalogues themselves are largely unfilled, so Urdu and Arabic currently
render the English source strings in a right-to-left layout.

`compilemessages` runs in the deploy build (see `render.yaml`) and is
deliberately non-fatal: it needs GNU gettext, and a missing toolchain should
degrade to untranslated rather than fail a deploy.

## Where the rest of the documentation is

| Document | What is in it |
|---|---|
| `docs/DATABASE.md` | The tables, the constraints, the indexing strategy and why each index has the shape it has |
| `docs/DEPLOYMENT.md` | Render, and the caveats that matter before anything real goes on it |
| `docs/VENDORED_ASSETS.md` | Where Bootstrap, Bootstrap Icons and htmx come from, and how to upgrade them |
| `docs/AUDIT.md` | The September 2026 review: what changed, and what is still outstanding |

## Deployment

`docs/DEPLOYMENT.md`. Read the caveats at the bottom of it before putting
anything real on the free tier - in particular what happens to uploaded book
covers.
