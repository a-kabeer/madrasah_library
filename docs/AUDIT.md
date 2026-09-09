# System audit — September 2026

A twenty-part review of the application, carried out with the instruction to
fix rather than only report. This is the record of what changed, and — more
usefully — of what is still outstanding and what needs deciding.

Commits `4133504` and `20ee668` through `8323b10`.

## What was wrong, in one page

Almost everything found here has one of three shapes.

**A thing was written and never connected.** `--bs-link-color` was aliased to
the brand colour, and Bootstrap 5.3 reads `--bs-link-color-rgb` — so every
link in the app was Bootstrap's blue whatever colour an organisation had set.
`ACTIVITY_LOG_CACHE_KEY` was deleted on every write with nothing ever stored
under it. Four trigram search indexes were built on bare columns while Django
compiles every `icontains` with `UPPER()` around the column, so not one of
them could ever be used. `numeric_param` existed, with a docstring saying it
prevents a 500, and five call sites did not use it. `config/formats/en` was
the one language module missing, so English was the only language printing
dates in the wrong format. Nothing failed in any of these cases, which is why
they lasted.

**A thing was true and stopped being true.** The modal conversion changed
what views return and left 196 test failures behind. `docs/DATABASE.md` still
described `role_required` and claimed "no further indexing work is needed
here". `docs/DEPLOYMENT.md` described a build command that no longer existed.
Two thousand lines of view code and thirteen templates were unreachable.

**A thing was never looked at.** No test had ever run JavaScript, so the
htmx layer, the dialogs, the layout, the contrast and the accessibility of
the entire application were unverified — 374 WCAG failures, a table that made
a 375px viewport into a 598px document, and a search box printing
`{% translate '…' %}` to the reader all passed `manage.py check`, the
template-compile sweep and the whole existing suite. Nobody had run
`manage.py check --deploy`, which reported an error. Nobody had loaded
`scripts/test_schema.sql` into an empty database, which fails.

## What changed

| Step | Outcome |
|---|---|
| 1 Repository & architecture | Django's admin removed — `User` has no `is_staff`, so the site could never be entered, and routing it published a second login form that bypassed the rate limiter. HSTS subdomain and preload flags decoupled from the max-age. |
| 2 Dashboard | 15 cards and 4 quick actions gated, so the dashboard stops offering doors that answer 403. 12 COUNTs collapsed into 3 aggregates. |
| 3 Navigation | Every link on 23 pages walked as each of three roles; no page offers anything the viewer cannot open. |
| 4 CRUD | Every refused delete now says why. A GET mutates nothing — 16 destructive routes checked. |
| 5 Tables & lists | Verified; no change needed. Five apparent findings were mine, not the code's. |
| 6 Forms & modals | 12 dialogs open with the cursor in the first editable field. |
| 7 Search / filter / sort / pagination | Verified against the edge cases; no change needed. |
| 8 Actions & business logic | Two multi-row writes in circulation made atomic. 36 anonymous audit entries given their actor back. |
| 9 Security | The boundary exercised rather than read: anonymous access, role escalation, object access, sessions, passwords, injection, uploads. One latent XSS closed. |
| 10 Backend & code quality | 2,078 unreachable lines and 13 templates deleted; `django.contrib.admin` dropped. The deletion silently removed five `@feature_required` decorators, found by an AST diff of every surviving function. |
| 11 Database & performance | No N+1 anywhere (34 pages, two dataset sizes, identical query counts). Eleven search indexes rebuilt to match the SQL the ORM sends. Eight list filters stopped turning a hand-edited URL into a 500. |
| 12 JavaScript & CSS | Bootstrap, Bootstrap Icons and htmx vendored — no CDN, no missing subresource integrity, works on a local-only network. Two templates were printing their own syntax to the reader. |
| 13 Mobile | 38 pages at three widths; one real overflow, caused by a `visually-hidden` label escaping a scroll container. |
| 14 UX consistency | One date format everywhere. One style per action. |
| 15 Accessibility | 374 axe-core violations to zero, in both themes. Fifteen templates' clickable rows made valid. |
| 16 Testing | 44 new tests; the 196 pre-existing failures characterised and left as the recommended next task. |
| 17 Production readiness | The schema file now loads. `check --deploy` clean but for two deliberate warnings. Errors reach a log. |
| 18 Documentation | A README, which there was not. Three docs corrected where they had gone stale or were actively misleading. |
| 19 Cross-system | Every feature gates something. Every write reaches the audit trail. The schema file and the database agree both ways. |
| 20 Final verification | Full suite before and after: one pre-existing failure fixed, none added. |

## What is still outstanding

### Needs a decision before it can be done

**Uploaded files (CRITICAL).** `MEDIA_ROOT` is a directory on the instance's
disk, and on Render's free tier that disk is replaced on every deploy and
every cold start. Book covers, the logo and the favicon are all uploads, and
all three vanish silently — the database row still names a file that is no
longer there. Separately, `MEDIA_URL` is served by a Django view behind
`LoginRequiredMiddleware`, so even before the disk is replaced a visitor
following a link to `/media/…` is redirected to the sign-in page. A Render
persistent disk (paid) or object storage (S3 / Cloudflare R2 with
`django-storages`) are the two answers; they differ in cost and in
operational shape, so this is the deployer's call. `docs/DEPLOYMENT.md`
carries the detail.

**Session lifetime.** `SESSION_COOKIE_AGE` is unset, so a session lasts a
fortnight. A librarian's own laptop and the shared terminal in the reading
room want different answers, and the second is the one a fortnight is wrong
for.

**Overdue as a real boundary.** `loans.overdue` is declared `menu_only`: it
decides whether the Overdue entry is offered and nothing more, because an
overdue loan is an active loan and anyone with `loans.active` reaches the
same rows by sorting. Making it a boundary means the loans list must stop
showing overdue rows to a role without the key — a change to what the list
returns, not a decorator.

**`--brand-secondary` and `--brand-accent`.** Both are settings on the
Branding page and no rule reads either, so setting them changes nothing.
Either give them something to drive or take the fields off the page. The
theme re-skin already on the roadmap will decide every token role anyway, so
this belongs with it.

### Known work, no decision needed

**The 196 test failures (largest single item).** 151 tests across 29 modules,
one cause: the modal conversion changed what views return and the tests were
not changed with it. 70 assert on a status code, 35 on text that now lives in
a fragment, 23 read a context key a modal view does not supply. None is
flaky. While they are red the suite cannot tell a regression from the
backlog — every verification in this audit had to be done by comparing
failure sets rather than by reading a result, which works and which nobody
will do routinely. A day or more of reading and rewriting. **This is the
recommended next task.**

**Searches that OR across a join.** Most list searches read
`Q(a__icontains=t) | Q(join__b__icontains=t)`, and PostgreSQL cannot turn an
OR spanning two tables into a bitmap OR — measured with every column indexed
and unchanged: book list 14.5 ms, copy list 44.2, loan list 52.7 at 20,000
books and 40,000 copies. The remedy is a query-shape change at eight call
sites (search each side separately and combine by id), not an index.
`docs/DATABASE.md` has the shape.

**Eleven functions over 150 lines**, `loan_add` at 423. Real, and not
something to do in the same commit as a two-thousand-line deletion.

**No Content-Security-Policy.** Django ships none, so it needs `django-csp`
or a small middleware, a nonce for the pre-paint theme script, and the four
inline handlers moved.

**Smaller, recorded in full in the working notes:** 23 forms show only
top-level validation errors; no unsaved-changes warning; the paginator's
`count(*)` on the activity log; the book list living in `copies.py`.

## Method, for whoever picks this up

Three things earned their keep and are worth repeating.

**Comparing failure sets, not reading results.** With 196 tests permanently
red, "did the suite pass" is unanswerable. Every step here captured the
failing set before a change and after it, and asserted the difference was
empty. Two regressions of my own were caught that way and would have been
invisible otherwise.

**Exercising rather than reading.** "No page offers a link that 403s" was
established by visiting 23 pages as three roles and following every link, not
by reading decorators — and the first version of that sweep gave a false
clean because it followed the sign-out link and ran the rest of the walk
logged out. Reading the code would not have found five missing
`@feature_required` decorators either: absent looks intentional.

**A browser.** Four user-visible bugs, 374 accessibility failures and a
mobile layout fault, none of which fail a view test. `library/tests/browser.py`
is the harness; it skips cleanly where Playwright is absent, which the README
warns about, because a green run that skipped them looks identical to one
that did not.

And one caution. Several of my own findings were wrong and were withdrawn
after measurement — a "missing" pagination that was in a shared helper,
"duplicate" row actions that were the row-click implementation itself, a
filter I tested with the public catalogue's vocabulary against a staff page,
and a claim about `FORMAT_MODULE_PATH` being unset when it was on the line
after `LOCALE_PATHS`. Measure before writing it down, and correct the record
rather than quietly dropping it.
