# Deploying Coverage

Target: **Render** (managed Postgres + built-in cron for the 6-hourly scrape). The
`Dockerfile` is host-agnostic, so Fly.io or any container host works too — only
the platform steps differ. Nothing here is destructive; take it one section at a
time.

Prerequisites you create (Claude can't — they need your accounts/payment):
a Render account and a Google Cloud project (sign-in, and separately, Gmail Live
if you want it). Rough cost at this scale: Render web + Postgres ≈ low-tens of
dollars/month; Google OAuth is free.

---

## 1. First deploy (Render Blueprint)

1. Push this repo to GitHub (it already has a sensible `.gitignore`; the real
   `.env` is ignored — never commit it).
2. Render → **New → Blueprint** → pick the repo. Render reads `render.yaml` and
   proposes a **web service**, a **Postgres database**, and several **cron
   jobs/workers**.
3. It will ask you to fill the `sync: false` env vars (they can't live in git).
   Set these on the **web service** — leave them blank for now where noted and
   come back after the later sections:
   - `DJANGO_ALLOWED_HOSTS` = your Render hostname, e.g. `coverage-web.onrender.com`
     (add your custom domain too once you attach one, comma-separated).
   - `DJANGO_CSRF_TRUSTED_ORIGINS` = `https://coverage-web.onrender.com`
     (scheme included; add the custom domain's origin too).
   - `REDIS_URL` — Render → **New → Key Value**, then paste that store's
     *internal* connection string here. Do this before real students sign up.
     This cache holds the failed-login and password-reset counters; blank
     means each of the three gunicorn workers keeps its own copy, so the
     "5 failed logins per 5 minutes" limit is really 15 and resets on every
     deploy. Blank is fine while you are the only user.
   - `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` (section 3).
   - `GMAIL_LIVE_*` (five keys) — optional; leave blank until you want real-time
     Gmail (section 4, `docs/gmail-live-setup.md`).
   - `APPLE_OAUTH_*`, `MICROSOFT_OAUTH_*`, `LINKEDIN_OAUTH_*` — optional; leave
     blank and those sign-in buttons show "Setup Needed" until you fill them.
   - `SENTRY_DSN` — optional; leave blank to disable.
   - `DJANGO_SECRET_KEY` is `generateValue: true` — Render creates and stores
     it; the cron jobs and workers share the same value.
4. Apply. Render builds the image, runs `collectstatic` at build time, runs
   `migrate` as the **preDeploy** step (once, before traffic — a failed
   migration blocks the release instead of half-applying), then starts gunicorn.
5. Health check: Render polls `/healthz`. When the service is green, open
   `https://<your-host>/` — the home page and `/opportunities/` should load
   (the feed is empty until you seed + scrape, section 2).

## 2. Create the admin + seed data (Render Shell)

On the web service's **Shell** tab:

```bash
uv run --package coverage-web python coverage_web/manage.py createsuperuser
uv run --package coverage-web python coverage_web/manage.py seed_directory   # 71 firm rows + SA 2028 firm dates
uv run --package coverage-web python coverage_web/manage.py scrape            # first opportunities pull
uv run --package coverage-web python coverage_web/manage.py seed_logo_domains  # firm front doors, for logos
uv run --package coverage-web python coverage_web/manage.py seed_mail_domains  # the domains bankers email FROM
```

Every seed file these commands read is **tracked in git and ships inside the
`directory` app** — `directory/seeds/*.yaml` for the firms and firm dates,
`directory/_logo_domains.py` and `directory/_mail_domains.py` for the two
domain maps. None of them reads `data/`, which is gitignored (it holds the
founder's private research and would not exist on Render anyway). Until
2026-08-25 `seed_directory` read `data/seeds/firms.yaml`, so this section's
first deploy would have printed "firms file not found" and left you with an
empty directory; if you are following an older copy of these instructions,
that is the bug.

Order matters. `seed_mail_domains` runs **after** `scrape` because it appends
to firms the catalog has already created and never invents a connector firm,
whereas `seed_directory` *replaces* each firm's `domains` list from the YAML —
run it last and it would drop everything the connectors and the two domain
commands had added. Without `seed_mail_domains` specifically, `capture.discovery`
matches almost nothing: the domains a board connector stores are career-site
hosts (`careers.bcg.com`, `jobs.rbc.com`), and nobody sends mail from one.

Now `/admin/` accepts your login and `/opportunities/` shows live openings. The
cron service runs the full `refresh` pass every 6 hours (00/06/12/18 UTC;
change `schedule` in `render.yaml`). The command exits non-zero when any stage
fails **or** when a pass ends with zero open roles, so turn on cron-failure
notifications (Render → the cron service → Settings → Notifications) and a
broken scrape emails you instead of silently serving stale deadlines.

## 3. Google sign-in (login-only scopes)

The `coverage-gmail-oauth-setup` skill in this repo walks the Cloud Console
clicks. The one rule that matters: **request only `openid`, `email`, `profile` —
never a `gmail.*` scope.** Login OAuth is unrestricted; adding a Gmail scope
would drag you into Google's restricted-scope verification (the CASA gate).
Gmail Live (section 4) uses a *separate* consent flow for exactly this reason,
so a verification stall on that client can never break sign-in.

1. Cloud Console → APIs & Services → **OAuth consent screen** (External),
   add your email as a test user while unverified.
2. **Credentials → Create OAuth client → Web application.** Authorized redirect
   URI: `https://<your-host>/accounts/google/login/callback/`.
3. Put the client id/secret into `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET` on the web service; redeploy.
4. Also add the Google **Social Application** in Django admin
   (`/admin/socialaccount/socialapp/`) if allauth doesn't pick it up from env:
   provider Google, the same client id/secret, and attach it to the site.

## 4. Gmail Live — real-time reply/bounce/invite detection (optional)

This is how Coverage's CRM actually fills itself in: connect a Gmail account
and touches log themselves, no habit change required. Full walkthrough,
including the Google Cloud Console clicks (a SEPARATE OAuth client from
section 3 — never reuse it) and the Pub/Sub setup, lives in
`docs/gmail-live-setup.md`. Skip this section entirely if you're not ready for
it yet — the app runs fine without it; the Settings page simply shows nothing
extra until `GMAIL_LIVE_*` is set.

### 4b. The daily Gmail sync (an older, still-useful path)

A separate, simpler route: for a mailbox already being scanned outside
Coverage by hand (an agent searching Gmail and emitting typed findings), apply
that same batch here — one search serves both systems. This needs no Google
review of its own; it just applies findings someone else already gathered.

```bash
DAYS=$(manage.py capture_gmail --email you@example.com --window)   # size the search
# ...the sync searches `newer_than:${DAYS}d` and writes findings.json...
manage.py capture_gmail --email you@example.com --findings findings.json --dry-run
manage.py capture_gmail --email you@example.com --findings findings.json
```

Always `--dry-run` first when wiring up a new findings source: it runs every
match, ratchet and dedup decision and writes nothing, and a mis-shaped batch
that silently archives contacts as bounced is tedious to unpick.

## 5. Custom domain (optional, when ready)

Attach your domain to the Render web service, add it to `DJANGO_ALLOWED_HOSTS`
and `DJANGO_CSRF_TRUSTED_ORIGINS`, and update both Google redirect URIs
(sign-in and, if connected, Gmail Live).

---

## Fly.io instead of Render

The `Dockerfile` is portable. `fly launch` (don't deploy yet), then: add a
managed Postgres (`fly postgres create` + `fly postgres attach`), set the same
env vars via `fly secrets set`, add a `[deploy] release_command` running
`migrate`, and add a scheduled machine (or an external cron hitting a management
command) for the daily scrape. Render's built-in cron is the only reason it's
the recommended default; everything else is equivalent.

## What still isn't automated (by design)

- The Google OAuth clients (sections 3 and 4) — need your Cloud project.
- The `firm_boards` DB table for ATS tokens (currently in
  `directory/boards.py`) — noted in the build follow-ups, not a blocker.
- Billing — deliberately out of v1.
