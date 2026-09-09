# Database — madrasah_library

This document describes the real Postgres schema backing the app. All Django
models in `library/models.py` are declared with `managed = False` — Django
never creates or alters these tables. The schema below is the source of
truth; `models.py` is a read/write mapping onto it, not the other way around.

## Setup

1. Copy `.env.example` to `.env` and fill in real values (DB password,
   `SECRET_KEY`, etc.). `.env` is git-ignored — never commit it.
2. Install dependencies: `pip install -r requirements.txt`
3. Create the database and load the schema. Every model is
   `managed = False`, so `migrate` will not create the app's tables:
   `scripts/test_schema.sql` is what does, and it is the source of truth.
   It creates the `pg_trgm` extension itself, which the search indexes
   need and which requires a superuser (or Render's provided role) the
   first time only.

   ```
   createdb madrasah_library
   psql -d madrasah_library -f scripts/test_schema.sql
   python manage.py migrate            # records them; the SQL did the work
   python manage.py createcachetable
   ```

   `createcachetable` is not optional: the permission matrix, the dashboard
   counts and the login rate limiter all use `DatabaseCache`, so that every
   Gunicorn worker sees the same state.
4. `python manage.py check` to confirm settings load correctly.

Django reads `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, and all `DB_*` values
from the environment via `python-decouple` (see `config/settings.py`).
`scripts/manual_checks/db_connection.py` (the standalone psycopg helper the
checks beside it use) reads the same `DB_*` variables.

## Entity-relationship overview

```mermaid
erDiagram
    AUTHORS ||--o{ BOOKS : writes
    CATEGORIES |o--o{ BOOKS : classifies
    PUBLISHERS |o--o{ BOOKS : publishes
    BOOKS ||--o{ BOOK_VOLUMES : has
    BOOK_VOLUMES ||--o{ BOOK_CONTENTS : has
    BOOK_CONTENTS |o--o{ BOOK_CONTENTS : "parent/children"
    BOOK_VOLUMES ||--o{ BOOK_COPIES : has
    LOCATIONS ||--o{ SHELVES : contains
    SHELVES |o--o{ BOOK_COPIES : holds
    BOOK_COPIES ||--o{ LOANS : "loaned as"
    BORROWERS ||--o{ LOANS : borrows
    USERS |o--o{ LOANS : "issues (issued_by)"
    USERS |o--o{ LOANS : "receives (returned_to)"
    USERS |o--o{ ACTIVITY_LOGS : performs
    BORROWERS ||--o{ RESERVATIONS : requests
    BOOKS ||--o{ RESERVATIONS : "queued for"
    LOCATIONS |o--o{ INVENTORY_SESSIONS : "scoped to"
    SHELVES |o--o{ INVENTORY_SESSIONS : "scoped to"
    INVENTORY_SESSIONS ||--o{ INVENTORY_SCANS : records
    BOOK_COPIES |o--o{ INVENTORY_SCANS : "scanned as"
    USERS |o--o{ INVENTORY_SESSIONS : "starts (started_by)"
    USERS |o--o{ ROLE_FEATURES : "last changed (updated_by)"
    USERS |o--o{ NOTIFICATIONS : "addressed to (recipient_id)"
    USERS |o--o{ ACQUISITION_SUGGESTIONS : "suggests / reviews"
```

`ROLE_FEATURES` has no foreign key to anything but `USERS`: it is keyed by
`(role, feature_key)` as plain text, because the features it names live in
`library/features.py` rather than in a table. A row for a feature that no
longer exists is ignored rather than an error, so retiring one needs no
migration.

(`||--o{` = required one-to-many, `|o--o{` = optional/nullable one-to-many.)

## Tables

| Table | Purpose | Key columns |
|---|---|---|
| `authors` | Book authors | `name` (unique) |
| `categories` | Book categories | `name` (unique) |
| `publishers` | Book publishers | `name` (unique), `city` |
| `books` | Titles | `title`, `author_id` (required), `category_id`/`publisher_id` (optional) |
| `book_volumes` | Physical/logical volumes of a book | `book_id`, `volume_number` (unique per book), `title` |
| `book_contents` | Table-of-contents entries per volume, self-nesting via `parent_id` | `volume_id`, `parent_id`, `title`, `content_type`, `page_number`, `sort_order` |
| `locations` | Physical library areas | `name` (unique) |
| `shelves` | Shelves within a location | `location_id`, `shelf_code` (unique per location) |
| `book_copies` | Physical copies of a volume | `volume_id`, `shelf_id` (optional), `copy_code` (unique), `status` |
| `borrowers` | People who can borrow books | `name`, `phone`, `borrower_type`, `registration_no` (unique when set), `is_active` |
| `users` | Staff/librarian accounts (not wired to Django auth — see note below) | `username` (unique), `role`, `is_active` |
| `loans` | Issue/return records | `copy_id`, `borrower_id`, `issue_date`, `due_date`, `return_date`, `issued_by`, `returned_to` |
| `activity_logs` | Audit trail of CREATE/UPDATE/DELETE/ISSUE/RETURN actions | `user_id` (optional), `action`, `entity_type`, `entity_id`, `description`, `created_at` |
| `acquisition_suggestions` | Books suggested for purchase, and what came of each | `title`, `author_name`/`publisher_name` (free text), `isbn`, `status`, `suggested_by`, `reviewed_by`, `reviewed_at` |
| `notifications` | Actionable in-app messages addressed to a staff user | `recipient_id`, `event_type`, `event_key` (unique per recipient), `title`, `url`, `created_at`, `read_at` |
| `organization_settings` | Single-row branding, institution metadata and borrowing policy for this installation | `name`, `logo`, `favicon`, `primary_color`/`secondary_color`/`accent_color`, `contact_email`, `contact_phone`, `footer_text`, `name_arabic`, `institution_type`, `address`, `website`, `updated_at` |
| `reservations` | A borrower's place in the queue for a book that is out | `borrower_id`, `book_id`, `status`, `created_at`, `closed_at` |
| `inventory_sessions` | One stock check: what was counted, over what scope, and whether it finished | `name`, `scope`, `location_id`/`shelf_id` (optional), `status`, `started_by`, `started_at`, `completed_at` |
| `inventory_scans` | Each copy scanned during a session, and what the scan meant | `session_id`, `copy_id` (optional), `copy_code`, `outcome`, `scanned_by`, `scanned_at` |
| `role_features` | Menu-permission overrides. **Only overrides** — no row means the code's own default for that role | `role`, `feature_key` (unique together), `allowed`, `updated_at`, `updated_by` |

> **`organization_settings` is a singleton.** A `CHECK (id = 1)` constraint
> means there can only ever be one row. `OrganizationSettings.load()` returns
> an unsaved instance carrying defaults when the row does not exist yet, so a
> fresh install renders correctly before anything is configured, and
> `save()` pins the primary key. Every column is optional — blank values fall
> back to defaults in the model's `display_*` properties.
>
> **`users.theme_preference`** (added at the same time) stores each user's
> Light/Dark/System appearance choice. It is nullable with no default, so
> adding it did not rewrite existing rows; `NULL` is read as "system".

> **`users` is now Django's `AUTH_USER_MODEL`** (`library.User`, extending
> `AbstractBaseUser`). `password_hash` stores a real Django password hash
> (via `set_password()`/`check_password()`), and `last_login` was added as
> a nullable column specifically to support this. Login/logout live at
> `/library/login/` and `/library/logout/`; `LoginRequiredMiddleware` blocks
> all anonymous access.
>
> **`role` is enforced by `@feature_required` in `library/permissions.py`**,
> which replaced the earlier `role_required` on the views behind a menu
> entry and does two jobs where that did one. The decorator names a feature
> and, per view, a *ceiling* — the roles that may ever reach it, stated in
> code. The ceiling is checked **before** the `role_features` toggle, which
> is the whole safety argument: no row in that table can let a role past
> what the code allows, so a mistaken toggle cannot become a privilege
> escalation. Turning a feature on for a role the ceiling excludes changes
> nothing.
>
> `library/features.py` lists the 22 switchable features, each with its
> ceiling, its day-one default and whether it can be switched off at all.
> Hiding a menu entry is never the boundary — the decorator is — so typing
> the URL of a feature switched off for you gets the same 403 as typing the
> URL of a view your role never had.

## Indexes

Every foreign key has a plain b-tree index, the hot query shapes have
partial indexes, and the columns the app searches have `pg_trgm` GIN indexes
— **on `UPPER(column)`, not on the column**, which is the part that matters
and the part this section used to get wrong.

- **Every FK column** (`books.author_id/category_id/publisher_id`,
  `book_volumes.book_id`, `book_contents.volume_id/parent_id`,
  `book_copies.volume_id/shelf_id`, `shelves.location_id`,
  `loans.copy_id/borrower_id/issued_by`, `activity_logs.user_id`,
  `reservations.borrower_id/book_id`, `inventory_scans.session_id`) has a
  dedicated b-tree index.

- **Trigram (`gin_trgm_ops`) expression indexes** on `UPPER(authors.name)`,
  `UPPER(books.title)`, `UPPER(book_contents.title)`,
  `UPPER(borrowers.name/phone/registration_no/department)`.

  Why the `UPPER()`: Django's PostgreSQL backend compiles a
  case-insensitive lookup with the *column* wrapped in a function —
  `name__icontains` becomes `UPPER("borrowers"."name"::text) LIKE
  UPPER(%s)` — and an index on `name` cannot satisfy a predicate on
  `UPPER(name)`. The schema originally indexed the bare columns, so not one
  of those four indexes could ever be used by any query the app issues:
  they collected writes and served nothing. Measured on 40,000 borrowers, a
  one-column search took 13.7 ms with the index present and 0.6 ms once it
  was on `UPPER(name)`. Migration
  `0013_case_insensitive_search_indexes` rebuilt them.

  Note that PostgreSQL needs an index for **every branch** of an `OR` before
  it will use any of them: the borrower search reads
  `Q(name) | Q(phone) | Q(registration_no) | Q(department)`, and with only
  `UPPER(name)` indexed it still scanned (33.9 ms). With all four it was
  0.45 ms. That is why those four go together.

- **B-tree expression indexes** on `UPPER(book_copies.copy_code)`,
  `UPPER(book_copies.status)` and `UPPER(authors.name)`, for the `iexact`
  lookups — which a GIN trigram index cannot serve, since `gin_trgm_ops`
  supports `LIKE` and similarity but not `=`. `copy_code__iexact` is the
  barcode behind issue, return and the return lookup, and it is also what
  `_next_copy_code` calls `.exists()` on in a loop while it hunts for a
  free code: 9.1 ms per call on 40,000 copies, 0.036 ms indexed. The
  author one is the per-row dedupe the book import runs for every line of
  the workbook.

- **Partial indexes** for the hottest query shapes in the app:
  - `idx_loans_active_due` — `loans(due_date) WHERE return_date IS NULL`,
    matching the overdue-loan query in `dashboard`/`loan_list`.
  - `unique_active_loan_per_copy` — a **unique** partial index on
    `loans(copy_id) WHERE return_date IS NULL`, which makes "double-issuing"
    a copy impossible even if the application-level check in
    `loan_add`/`loan_edit` were ever bypassed.
  - `unique_active_reservation` — one active reservation per
    borrower-and-book, and `idx_reservations_book_queue` for reading the
    queue in order.
  - `idx_books_archived_at`, `idx_notifications_unread`,
    `unique_found_copy_per_session`, `idx_acquisition_suggestions_queue` —
    the same shape for archived books, unread notifications, a session's
    found copies and the suggestion queue.

### What no index can fix

Most of the list searches `OR` across a **join**:

```python
Q(copy_code__icontains=term) | Q(volume__book__title__icontains=term)
```

PostgreSQL cannot turn an `OR` spanning two tables into a bitmap `OR`, so it
joins and filters every row — and this was measured with expression indexes
on every column involved, unchanged: book list 14.5 ms, copy list 44.2, loan
list 52.7, at 20,000 books / 40,000 copies / 15,000 loans. Adding an index
for one of these is wasted work.

The known remedy is a change to the query rather than to the schema: search
each side on its own and combine by id, e.g.

```python
Q(copy_code__icontains=term) | Q(volume_id__in=BookVolume.objects
    .filter(book__title__icontains=term).values("id"))
```

which makes each half a single-table search the indexes above can serve. It
is eight call sites and has not been done.

### Before adding an index

Check whether the column already has one above, and check the SQL Django
actually sends — `qs.query.sql_with_params()` — before assuming the shape.
The four dead trigram indexes above were added in good faith by someone
reading the ORM rather than the SQL.

## Constraints

**CHECK constraints** (enforced by Postgres regardless of what the Django
views validate):

| Table | Constraint | Rule |
|---|---|---|
| `book_copies` | `check_copy_status` | `status` ∈ {Available, Issued, Lost, Damaged, Missing, Transferred} |
| `borrowers` | `check_borrower_type` | `borrower_type` ∈ {Student, Teacher, Staff, Other} |
| `users` | `check_user_role` | `role` ∈ {Admin, Librarian, Assistant} |
| `loans` | `check_due_date_not_before_issue` | `due_date >= issue_date` |
| `loans` | `check_return_date_not_before_issue` | `return_date IS NULL OR return_date >= issue_date` |

(`models.py` now declares matching `choices=` for `BookCopy.status`,
`Borrower.borrower_type`, and `User.role` so Django forms/admin validate the
same set before a query ever reaches Postgres — belt and suspenders.)

**Uniqueness**: `authors.name`, `categories.name`, `publishers.name`,
`locations.name`, `users.username`, `book_copies.copy_code`,
`(book_volumes.book_id, volume_number)`, `(shelves.location_id, shelf_code)`,
`borrowers.registration_no` (only when non-blank — a partial unique index),
and `loans.copy_id` while `return_date IS NULL` (see above).

**Foreign keys — known Django/DB mismatch:** `models.py` declares
`Book.category` and `Book.publisher` as `on_delete=SET_NULL`, but the *real*
Postgres FK constraints for both are `ON DELETE NO ACTION` (i.e. deleting a
`Category`/`Publisher` that still has books attached raises a foreign-key
violation, it does **not** null out the book's reference the way the Django
model implies). `publisher_delete` already guards against this with an
`.exists()` check before deleting; **`category_delete` does not** and will
throw an unhandled `IntegrityError` (HTTP 500) the first time someone
deletes a category that has books — this is a Phase 3 view-layer fix, noted
here because it was only discoverable by checking the live schema.

All other FKs (`book_contents.volume_id/parent_id`, `book_volumes.book_id`,
`shelves.location_id`) are genuinely `ON DELETE CASCADE` at the DB level,
matching their Django declarations. Everything else (`book_copies.*`,
`loans.*`, `activity_logs.user_id`) is `NO ACTION`/`DO_NOTHING` on both
sides — deletes are blocked at the DB level unless the corresponding view
checks for dependents first (most do; see the `*_delete` functions across
`library/views/`).

## Acquisition suggestions

`acquisition_suggestions` holds books somebody thinks the library should
have. It is **not** a Book and **not** a purchase order.

| Column | Purpose |
|---|---|
| `title` | What was suggested. The only required field. |
| `author_name`, `publisher_name` | **Free text, no foreign key.** |
| `isbn` | As copied off a cover. Not validated as an ISBN. |
| `notes` | Why the book is wanted. |
| `status` | Pending / Approved / Rejected / Acquired. |
| `suggested_by`, `reviewed_by` | Nullable `users` references. |
| `reviewed_at`, `created_at`, `updated_at` | Timestamps. |

**Free text is deliberate.** Resolving "ibn kathir" to an `authors` row on
the strength of a suggestion would put a record in the catalogue that
nobody checked, so nothing in this feature creates an `Author`, a
`Publisher`, a `Category`, a `Book` or a `BookCopy`. The only route into
the catalogue is `book_add`, with its own validation, its own duplicate
check and its own `@role_required("Admin", "Librarian")` — the suggestion
shortcut at most fills that form in.

**There is no supplier, quotation, price, invoice or receiving step.** Those
are an accounting system; this is a list of books worth looking for.

**States move one way:**

```
Pending ──▶ Approved ──▶ Acquired
    └────▶ Rejected
```

`check_acquisition_status` pins the set, and
`check_acquisition_reviewed` keeps the status and the review timestamp from
coming apart (Pending has none; everything else has one) — the same shape as
`check_reservation_closed`. **Which** state may follow which is not in the
database: `library/acquisitions.py` names the source state in the `WHERE`
clause of one conditional `UPDATE`, so an arbitrary jump, a reopened
decision and the second of two identical POSTs all match no row and write
nothing — including the review timestamp, which would otherwise creep on
every refresh.

**`suggested_by` and `reviewed_by` are nullable**, matching
`loans.issued_by`, `inventory_sessions.started_by` and
`activity_logs.user_id` rather than `notifications.recipient_id`. An account
can be removed, and a suggestion outliving its author is better than a
delete that fails.

**One index**: `idx_acquisition_suggestions_queue` on
`(status, created_at DESC, id DESC)` — every list this table serves is "the
suggestions in this state, newest first", with `id` breaking ties so a page
cannot reshuffle between loads. There is deliberately **no** index on
`suggested_by`/`reviewed_by`: nothing looks a suggestion up by either (they
are only joined outwards to `users` by primary key), and an index nothing
reads is a write nobody needed.

**No link column to `books`.** A suggestion records that it *was* acquired,
not which row satisfied it. A foreign key would be `NO ACTION` like every
other FK here and would therefore start blocking `book_delete` — a change to
existing behaviour bought for a line of display text.

Migration `0010` also **widens** `check_notification_event_type` to admit
`suggestion_submitted`. That constraint enumerates the notification kinds
the application can render, so adding a kind means naming it there;
widening a CHECK cannot invalidate an existing row.

## Institution metadata

`organization_settings` also carries who the installation belongs to. It is
the same single row as the branding and the borrowing policy — there is no
`institutions` table, no branch or campus hierarchy, and no second place to
configure any of this.

Four columns were added for it (migration `0009`), and deliberately only
four:

| Column | Purpose |
|---|---|
| `name_arabic` | The institution's name in Arabic, shown beside the Latin one. |
| `institution_type` | Free text — Madrasah, Jamia, School, … See below. |
| `address` | Postal address, as typed (a textarea, so multi-line). |
| `website` | The institution's own web address. |

**The institution's name, email and phone were already here.** The name is
`name` — the column the sidebar, the page titles, the login page, the
report headers and the label sheets have read since `0003` — and the
contact details are `contact_email` and `contact_phone`. Adding
`institution_name`/`institution_email`/`institution_phone` beside them
would have given the library two answers to each of three questions, with
nothing to say which was right, so they are reused rather than duplicated.

**`institution_type` is plain text, not a CHECK-constrained enumeration**,
unlike every status column in this schema. Nothing branches on it — it is
printed and never tested — so a list would buy no correctness and would
need a migration the first time an institution described itself in a way
the list did not anticipate. The settings form offers the common answers as
a `<datalist>`, which suggests without restricting.

**All four are `NOT NULL DEFAULT ''`**, matching `name`, `contact_email`,
`contact_phone` and `footer_text` above them. On PostgreSQL 11+ a column
added with a constant default rewrites no rows, so an existing install
gains four empty strings and reads exactly as it did. Blank stays valid
forever: every display reads a property on the model
(`display_address`, `print_contact_line`, `has_institution_details`) that
returns `""`/`False` when nothing has been set, so an unconfigured
installation renders what it always rendered rather than an empty label.

**No query was added anywhere.** The row is already loaded once per request
and cached by `library.context_processors.get_branding()`, and everything
that displays this metadata — the sidebar, the report letterhead, the label
sheet footer — reads that same `branding` object. HTMX fragments, combobox
endpoints and partial renders gained nothing.

## Notifications

`notifications` holds one actionable message for one member of staff. It is
deliberately **not** a generic event table and **not** a second audit trail:

| Column | Purpose |
|---|---|
| `recipient_id` | The `users` row it is addressed to. Required — see below. |
| `event_type` | An explicit type, constrained to the list the model knows (`reservation_ready`, `stock_check_missing`). |
| `event_key` | Names the one-time transition it is about, e.g. `reservation_ready:41`. |
| `title` / `message` / `url` | What it says and where it goes, written out at creation. |
| `created_at` / `read_at` | When it arrived, and when it was read (`NULL` = unread). |

**Recipients are staff users only.** This schema has no relationship at all
between `borrowers` and `users` — a borrower is a name, a phone number and a
type, with no account — and the whole application sits behind
`LoginRequiredMiddleware`. So a borrower cannot be a recipient, and no
accounts are created for them. Events that read as borrower-facing are
addressed to the desk instead, phrased as the thing the desk can do
("Hafsa is first in the queue for Al-Bidaya, and a copy is on the shelf"),
and events with nothing for the desk to do — a cancelled reservation, whose
only interested party is the borrower — are not notifications at all. Those
stay on the reservation list and in `activity_logs`, which is the existing
staff workflow for them.

**Everything a notification displays is copied into the row.** It does not
join to the reservation or session that caused it, on purpose: a
notification is a record of what was said at the time, so it has to keep
saying it after the reservation is fulfilled, the book is archived or the
session is deleted — and reading a page of them is one query with no joins.

**Uniqueness / idempotency**: `unique_notification_event` is a unique index
on `(recipient_id, event_key)`. Creation goes through
`bulk_create(..., ignore_conflicts=True)`, i.e. `INSERT ... ON CONFLICT DO
NOTHING`, so a refreshed page, a retried POST, a double-clicked Return
button, a queue that advances twice or two returns landing together all
leave exactly one row — decided by the database, not by a check in Python
that two concurrent requests could both pass.

**Indexes**, one per question the application asks:

- `unique_notification_event` — `(recipient_id, event_key)`, the idempotency
  rule above.
- `idx_notifications_recipient_newest` —
  `(recipient_id, created_at DESC, id DESC)`, the panel and the list page.
  The `id` is part of it because a fan-out writes several rows in one
  statement, and a list that reshuffles between two page loads is not a list.
- `idx_notifications_unread` — the same columns `WHERE read_at IS NULL`,
  for the shell's unread badge and the list's unread filter. Partial, so it
  holds only the small part and stays small however long the history grows.

**CHECK**: `check_notification_event_type` mirrors
`Notification.EVENT_CHOICES`, so a row nothing can render cannot be written.

`activity_logs` and `notifications` stay separate systems: the log is audit
and history, a notification is a message to a person. Nothing here is
generated by polling or parsing the log, and nothing here is copied back
into it.

## Backup & restore

`python manage.py backup_db` (see `library/management/commands/backup_db.py`)
runs `pg_dump` in custom format and writes a timestamped `.dump` file into
`backups/` (path configurable via `BACKUP_DIR` in `.env`; `PGDUMP_PATH` in
`.env` points at `pg_dump.exe` if it isn't on `PATH`). `backups/` is
git-ignored — dumps contain real data and must never be committed.

To restore into a fresh/empty database:

```
pg_restore --clean --if-exists -h <host> -U <user> -d madrasah_library backups/<file>.dump
```

`--clean --if-exists` drops existing objects first, so this is safe to run
against a database that already has the (possibly stale) schema in it. There
is deliberately no automated "restore" management command — restoring is a
destructive operation onto whatever database you point it at, so it should
always be a conscious, manual step.

Run `python manage.py backup_db` before any manual schema change (adding a
migration-equivalent SQL script, editing constraints by hand, etc.) — there
is currently no other backup schedule (no cron/Task Scheduler job configured
yet; that's still an open follow-up for a real deployment).
