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
   service (`pip install -r requirements.txt && collectstatic`, then
   `gunicorn config.wsgi:application`). `SECRET_KEY` is auto-generated;
   all `DB_*` values are wired automatically from the database Render just
   created — you don't need to copy any of that by hand.
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
- **No custom domain / no automated backups** — out of scope for a free
  demo; see the main roadmap's Phase 7 for what real production hosting
  would still need (paid Postgres, scheduled backups, a real domain).
