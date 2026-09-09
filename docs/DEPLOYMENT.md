# Deploying to Render (free tier)

This gets you a public HTTPS URL with no domain or paid hosting required —
good for a demo, not yet a real production setup (see the caveats at the
bottom).

## One-time setup

1. Sign up at [render.com](https://render.com) (GitHub OAuth login is
   fastest — it also connects your GitHub account in the same step).
2. From the Render dashboard: **New +** → **Blueprint**.
3. Select the `a-kabeer/madrasah_library` repo and the branch you want to
   deploy (e.g. `render-deployment-setup` while testing, `main` once merged).
4. Render reads `render.yaml` at the repo root and shows you a plan: one
   **Web Service** (`madrasah-library`) and one **PostgreSQL** database
   (`madrasah-library-db`), both on the free plan. Click **Apply**.
5. Render provisions the database first, then builds and deploys the web
   service. The build (see `render.yaml`) installs requirements, runs
   `migrate`, creates the cache table, compiles the translation
   catalogues and runs `collectstatic`; the start command is
   `gunicorn config.wsgi:application`. `SECRET_KEY` is auto-generated;
   all `DB_*` values are wired automatically from the database Render just
   created — you don't need to copy any of that by hand.

   `createcachetable` is not optional. The permission matrix, the
   dashboard counts and the login rate limiter all use `DatabaseCache`, so
   that every Gunicorn worker sees the same state; without the table the
   app raises on the first cached read.

   `compilemessages` is deliberately non-fatal — it needs GNU gettext in
   the build image, and a missing toolchain should degrade to untranslated
   rather than fail the deploy.
6. First deploy will fail health checks until the schema exists (see next
   section) — that's expected, not a broken build.

## Loading the schema (and optionally your data) onto Render's database

The app's models are all `managed = False` on an existing schema, so an
empty Render Postgres has no tables yet. From the Render dashboard, open
the `madrasah-library-db` database page and copy its **External Database
URL** (or the individual host/port/user/password/database fields) — then,
from your own machine:

```
# Full schema + your current data:
pg_restore --no-owner --no-privileges -h <render-host> -U <render-user> -d <render-database> backups/<latest>.dump

# Or just the app tables' schema, no data (empty demo):
psql -h <render-host> -U <render-user> -d <render-database> -f scripts/test_schema.sql
```

You'll be prompted for the password Render shows you (or set `PGPASSWORD`
in your shell first). Once this finishes, redeploy (or just wait for
Render's next health check) and the app should come up.

## Redeploys

Render auto-deploys on every push to whichever branch you connected. No
extra steps needed for future changes — just `git push`.

## Caveats — this is a demo setup, not production-grade

- **Free Postgres expires.** Render's free-tier database is deleted after
  a fixed period (currently 30 days) unless upgraded to a paid plan. Fine
  for a demo, not for anything long-lived.
- **Free web service sleeps.** After ~15 minutes of no traffic, the free
  web service spins down; the next visitor waits ~30–60s for a cold start.
- **Uploaded files do not survive a deploy.** `MEDIA_ROOT` is a directory
  on the instance's own disk, and that disk is replaced on every deploy and
  on every cold start. Book covers, the organisation's logo and its favicon
  are all uploads, so all three vanish — silently, since the database row
  still names a file that is no longer there. Anything beyond a demo needs
  either a Render persistent disk (a paid plan) or object storage
  (S3/Cloudflare R2 with `django-storages`), and that is a decision about
  the installation rather than something the code should make. Until then,
  treat every upload as temporary.

- **Uploaded files require a login.** `MEDIA_URL` is served by
  `django.views.static.serve`, an ordinary Django view, and
  `LoginRequiredMiddleware` is on globally — so a visitor following a link
  to `/media/…` is redirected to the sign-in page and the image renders
  broken. This is why the public catalogue deliberately links to none of
  it and shows a placeholder instead of a cover; there is a test holding
  that (`test_public_catalog.LayoutTests`). Publishing the media directory
  is part of the same storage decision above.

- **`django.views.static.serve` is not a web server.** It reads the file
  and streams it through Python on every request, with no caching. Fine for
  a handful of covers; not fine under load. Whatever storage decision gets
  made above should take the serving with it.

- **No custom domain / no automated backups** — out of scope for a free
  demo; see the main roadmap's Phase 7 for what real production hosting
  would still need (paid Postgres, scheduled backups, a real domain).
