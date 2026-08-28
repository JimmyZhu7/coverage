# Coverage

Everyone tracks recruiting deadlines. Nobody tracks the relationship. Coverage
is a shared, centrally-scraped opportunities feed for campus recruiting
(consulting, finance) — free, never paywalled, ranked by deadline then
freshness — wrapped around a private, per-student networking CRM that scans a
student's own outreach and scores contacts and firms by estimated chance of
success, feeding that score back into a weekly, prioritized to-do list. The
feed is a commodity given away for trust; the defensible value is the captured
email activity a student feeds into their own relationship ledger. See
`docs/product-brief.md` for the full thesis and `docs/build-plan.md` for the
technical build plan this repo follows.

## Repo layout

A `uv` workspace with three Python packages:

- **`coverage_web/`** — the Django project: multi-tenant web app, Google
  sign-in (login-only scopes) + base templates, htmx-served views, the
  `healthz` endpoint. This is the only package with a runtime entry point
  (`manage.py`) and the only one that talks HTTP.
- **`coverage_domain/`** — ported pure-logic libraries: the contact/thread
  state machine, cadence engine, deterministic apply layer, and fit-score
  engine, lifted from the founder's existing single-user system. Framework-free
  — takes a DB connection, knows nothing about Django. *Owned by a separate
  workstream; this scaffold references it by name in the workspace config but
  does not create or modify anything under this directory.*
- **`coverage_connectors/`** — deterministic ATS/board scrapers (Greenhouse,
  Lever, Workday, Oracle Recruiting Cloud, company-specific fetchers) and the
  staleness/verification layer. Empty scaffold as of M0; real code lands in
  M1 (see `docs/build-plan.md`, "7. Sequencing").

Why three packages instead of one Django app: `coverage_domain` and
`coverage_connectors` are meant to be run from cron and imported as plain
Python, independent of Django request/response cycles — keeping them as
separate workspace members enforces that boundary at import time, not just by
convention.

## Local setup

Prerequisites: [uv](https://docs.astral.sh/uv/) (manages Python 3.13 for you
— no separate Python install needed) and a local Postgres server. There is no
SQLite fallback anywhere in this project, by design (see
`docs/build-plan.md`, "1. Stack": concurrent writers — web + scrape worker +
inbound-mail webhook — is precisely SQLite's weak spot, and testing against a
different engine than production would defeat the point of choosing
Postgres).

```bash
# 1. Install all workspace packages' dependencies (Django, psycopg, allauth,
#    django-environ, htmx is vendored as a static file so nothing to install
#    there, plus pytest/pytest-django/pytest-cov). Plain `uv sync` alone only
#    installs the workspace root's own deps — `--all-packages` is required to
#    pull in coverage_web's (and later coverage_domain's/coverage_connectors')
#    dependencies too.
uv sync --all-packages

# 2. Create the local Postgres role + database matching the default
#    DATABASE_URL (postgres://coverage:coverage@localhost:5432/coverage).
#    Adjust if your local Postgres uses different admin access.
psql postgres -c "CREATE USER coverage WITH PASSWORD 'coverage' CREATEDB;"
createdb -O coverage coverage

# 3. Copy the env template and adjust anything that doesn't match your setup
#    (the DATABASE_URL default above matches step 2 as-is).
cp .env.example .env

# 4. Apply migrations.
cd coverage_web
uv run python manage.py migrate

# 5. Run the dev server.
uv run python manage.py runserver
# -> http://127.0.0.1:8000/         placeholder home page
# -> http://127.0.0.1:8000/healthz  {"status": "ok"}

# 6. Run the test suite (from the repo root).
cd ..
uv run pytest
```

## Verifying a change by hand — use the demo account, not the shared tables

The `coverage` database above is a **shared, standing dev database** — every
worktree on a given machine points at the same one by default (only the
pytest database name is per-worktree, see `settings/base.py`'s Database
section), and it is also where the founder's own account lives. `uv run
pytest` / `pytest coverage_web/<app> -q` never touch it — they run against a
throwaway `test_coverage_*` database instead. Anything driven through a
running `manage.py runserver` or typed into `manage.py shell`, though, lands
in the real one.

That distinction has produced real leakage more than once: a `Firm` row with
a blank slug from a stray `manage.py shell` insert (see `Firm.slug`'s
docstring), four "ZZZ Smoke Test..." contacts left in the founder's own CRM
by smoke runs (`crm/management/commands/purge_test_contacts.py`), and a fake
"Verify J.P. Morgan" firm that rendered as a real card on the founder's own
Today page.

So: to click through a CRM feature by hand, sign in as
**`demo@coverage.local`** (password `demo1234`) — run **`manage.py
seed_demo`** first if it doesn't exist yet (idempotent, safe to run
anytime). It is tenant-isolated like every other account, so nothing you do
to its contacts/touches/firms can affect anyone else's data. Only reach for
`directory.Firm`/`FirmDate` directly when the thing under test IS the shared
directory itself, and prefer doing that against pytest's database over the
live one. Either way, run **`manage.py audit_fixtures`** before you finish —
it reports (never deletes) anything that still looks synthetic, so a session
ends with a check rather than a guess.

Google sign-in (`/accounts/google/login/`) is wired up via django-allauth but
needs a real OAuth client from Google Cloud Console
(`GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` in `.env`) to
complete a login — that's the founder's to create. **That client must request
only `openid email profile` scopes and never anything under `gmail.*`**; see
`docs/build-plan.md` §3 and the `coverage-gmail-oauth-setup` skill for why
this boundary matters. The app boots and the test suite passes with no
Google credentials configured at all.
