# Deploying Coverage

Target: **Render** (managed Postgres + built-in cron for the 6-hourly scrape). The
`Dockerfile` is host-agnostic, so Fly.io or any container host works too — only
the platform steps differ. Nothing here is destructive; take it one section at a
time.

Prerequisites you create (Claude can't — they need your accounts/payment):
a Render account, a domain you control (for the capture address), a Postmark
account (inbound email), and a Google Cloud project (sign-in). Rough cost at
this scale: Render web + Postgres ≈ low-tens of dollars/month; Postmark has a
free inbound tier; Google OAuth and the domain are cheap/free.

---

## 1. First deploy (Render Blueprint)

1. Push this repo to GitHub (it already has a sensible `.gitignore`; the real
   `.env` is ignored — never commit it).
2. Render → **New → Blueprint** → pick the repo. Render reads `render.yaml` and
   proposes a **web service**, a **Postgres database**, and a **cron job**.
3. It will ask you to fill the `sync: false` env vars (they can't live in git).
   Set these on the **web service** — leave them blank for now where noted and
   come back after the later sections:
   - `DJANGO_ALLOWED_HOSTS` = your Render hostname, e.g. `coverage-web.onrender.com`
     (add your custom domain too once you attach one, comma-separated).
   - `DJANGO_CSRF_TRUSTED_ORIGINS` = `https://coverage-web.onrender.com`
     (scheme included; add the custom domain's origin too).
   - `CAPTURE_INBOUND_DOMAIN` = the subdomain you'll point at Postmark, e.g.
     `in.coverage.app` (section 3).
   - `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` (section 4).
   - `APPLE_OAUTH_*`, `MICROSOFT_OAUTH_*`, `LINKEDIN_OAUTH_*` — optional; leave
     blank and those sign-in buttons show "Setup Needed" until you fill them.
   - `SENTRY_DSN` — optional; leave blank to disable.
   - `DJANGO_SECRET_KEY` and `CAPTURE_INBOUND_SECRET` are `generateValue: true`
     — Render creates and stores them; the cron shares the same values.
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
uv run --package coverage-web python coverage_web/manage.py seed_directory   # 68 firms + firm dates
uv run --package coverage-web python coverage_web/manage.py scrape            # first opportunities pull
```

Now `/admin/` accepts your login and `/opportunities/` shows live openings. The
cron service runs the full `refresh` pass every 6 hours (00/06/12/18 UTC;
change `schedule` in `render.yaml`). The command exits non-zero when any stage
fails **or** when a pass ends with zero open roles, so turn on cron-failure
notifications (Render → the cron service → Settings → Notifications) and a
broken scrape emails you instead of silently serving stale deadlines.

## 3. Inbound email — the capture address (Postmark)

This is what makes the CRM real; it needs no Google review.

1. Pick a subdomain you'll dedicate to inbound, e.g. `in.coverage.app`, and set
   `CAPTURE_INBOUND_DOMAIN` to it (section 1). It must be a subdomain you can set
   MX records on.
2. Postmark → add an **Inbound** stream. Postmark gives you an inbound address /
   server and DNS records:
   - **MX** record on `in.coverage.app` pointing at Postmark's inbound host.
   - **SPF** and **DKIM** as Postmark instructs (so forwarded mail isn't
     spam-foldered).
3. Point Postmark's inbound webhook at:
   `https://<your-host>/capture/inbound/?token=<CAPTURE_INBOUND_SECRET>`
   (or set the `X-Capture-Token` header to the secret if the console allows —
   preferred, keeps the secret out of access logs). Read the generated
   `CAPTURE_INBOUND_SECRET` value from the web service's Environment tab.
4. Test: from any mail client, send a message and BCC
   `u-<your-capture-slug>@in.coverage.app` (your slug is shown in
   `/welcome/` onboarding and `/capture/health/`). Within a minute
   `/capture/health/` should show "last received" update and a touch should
   appear on the matching contact.

Note: raw-MIME 30-day retention (build-plan §10) is **not** wired yet — the
webhook keeps only Postmark's message id, not the raw body. Add a retention blob
store before scaling if you want the raw source kept.

## 4. Google sign-in (login-only scopes)

The `coverage-gmail-oauth-setup` skill in this repo walks the Cloud Console
clicks. The one rule that matters: **request only `openid`, `email`, `profile` —
never a `gmail.*` scope.** Login OAuth is unrestricted; adding a Gmail scope
would drag you into Google's restricted-scope verification (the CASA gate). If
you ever add real Gmail reading, it uses a *separate* consent flow so a stall
can't break sign-in.

1. Cloud Console → APIs & Services → **OAuth consent screen** (External),
   add your email as a test user while unverified.
2. **Credentials → Create OAuth client → Web application.** Authorized redirect
   URI: `https://<your-host>/accounts/google/login/callback/`.
3. Put the client id/secret into `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET` on the web service; redeploy.
4. Also add the Google **Social Application** in Django admin
   (`/admin/socialaccount/socialapp/`) if allauth doesn't pick it up from env:
   provider Google, the same client id/secret, and attach it to the site.

## 5. Custom domain (optional, when ready)

Attach your domain to the Render web service, add it to `DJANGO_ALLOWED_HOSTS`
and `DJANGO_CSRF_TRUSTED_ORIGINS`, and update the Google redirect URI. Keep the
inbound subdomain (`in.coverage.app`) separate — it belongs to Postmark's MX,
not the web host.

---

## Fly.io instead of Render

The `Dockerfile` is portable. `fly launch` (don't deploy yet), then: add a
managed Postgres (`fly postgres create` + `fly postgres attach`), set the same
env vars via `fly secrets set`, add a `[deploy] release_command` running
`migrate`, and add a scheduled machine (or an external cron hitting a management
command) for the daily scrape. Render's built-in cron is the only reason it's
the recommended default; everything else is equivalent.

## What still isn't automated (by design)

- Real Postmark DNS + the first webhook test (section 3) — needs your domain.
- The Google OAuth client (section 4) — needs your Cloud project.
- Raw-MIME retention, and the `firm_boards` DB table for ATS tokens (currently
  in `directory/boards.py`) — noted in the build follow-ups, not blockers.
- Billing — deliberately out of v1.
