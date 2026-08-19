"""
Base Django settings shared by every environment.

Environment-driven via django-environ. `.env.example` at the repo root lists
every variable this settings package reads, with a placeholder value and a
one-line explanation for each. Never commit a real `.env`.

Split rationale (see docs/build-plan.md, "1. Stack" and M0 in "7.
Sequencing"): `local.py` and `production.py` both import from this module and
override only what differs. `DJANGO_SETTINGS_MODULE` selects which one loads;
`manage.py` / `wsgi.py` / `asgi.py` default to `coverage_web.settings.local`
via `os.environ.setdefault`, so an already-set env var (CI, PaaS) always wins.
"""

import hashlib
from pathlib import Path

import environ

# This file lives at coverage_web/coverage_web/settings/base.py.
# parents[0] = settings/, [1] = coverage_web/ (the Django package dir, also
# BASE_DIR — where manage.py, core/, templates/, static/ all live).
BASE_DIR = Path(__file__).resolve().parents[2]

# Repo root, one level above the coverage_web package — where a local .env
# lives (see .env.example) and where the uv workspace root pyproject.toml is.
REPO_ROOT = BASE_DIR.parent

env = environ.Env()

# Loads a local .env file if one exists; harmless no-op otherwise (e.g. in CI,
# where real env vars are injected directly — see .github/workflows/ci.yml).
environ.Env.read_env(str(REPO_ROOT / ".env"))

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

# Insecure default so the app *boots* without a .env for a first `check`/
# `pytest` run; production.py requires the real env var with no fallback.
SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-only-secret-key-do-not-deploy")

DEBUG = env.bool("DJANGO_DEBUG", default=False)

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # ArrayField and friends (docs/build-plan.md §2's text[] columns).
    "django.contrib.postgres",
    # Required by django-allauth (SITE_ID-based).
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.apple",
    "allauth.socialaccount.providers.microsoft",
    "allauth.socialaccount.providers.linkedin_oauth2",
    "core",
    # docs/build-plan.md §2's multi-tenant data model, split by zone:
    "accounts",  # the custom User model (private zone's `users` table)
    "directory",  # shared zone: firms, opportunities, firm_dates, ...
    "crm",  # private zone: user_firms, contacts, touches, tasks
    "analytics",  # private zone: user_opportunities, fit_scores, product_events, imports
    "capture",  # Gmail Live's GmailConnection (capture/models.py) + apply
                # layer (capture/gmail.py) — the BCC/forward v1 pipeline this
                # app used to also hold was retired 2026-08-19

    "assistant",  # private zone: "Talk to Coverage" — the advisor page's conversations
    "billing",  # private zone: the credit ledger both assistant and capture spend from
                # (docs/credit-system-plan.md) — not folded into either app because
                # it's a third surface's dependency just as much as the first two's
]

# The scheme+host the app is reachable at, for links that have to be built
# outside a request (no `request.build_absolute_uri` to lean on) — today,
# only the weekly digest email's "open in Coverage" links (crm/digest.py,
# the send_weekly_digest command). Defaults to local dev; set the real
# deployed origin once one exists (see docs on deploy status).
SITE_URL = env("SITE_URL", default="http://localhost:8000")

# Anthropic API key for the LLM-backed extraction pass (directory/ai_extract.py)
# and any future AI feature (coffee-chat briefs, outreach drafts). Blank by
# default — every AI code path checks ai_extract.is_configured() first and
# no-ops rather than erroring, so the app boots and every test passes with
# nothing set. Costs real money per call once set; see ai_extract.py's
# module docstring before turning this on in production.
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")

# ---------------------------------------------------------------------------
# "Talk to Coverage" — the advisor page (assistant/).
#
# Two tiers, keyed by accounts.User.plan (assistant/plans.py reads these).
#
# The model is the real product difference between the tiers. This page is
# multi-step reasoning over a student's actual CRM with eight tools and
# judgement as the output — exactly the job the cheaper tier is worst at, so
# Free (Haiku) gives a genuinely useful but shallower advisor, more inclined
# to summarise the data back than to weigh which firm is worth the week, and
# Pro (Sonnet) gives the real thing. That gap is honest and observable, which
# is what makes it a reason to pay rather than a feature gate.
#
# The daily cap is a spend ceiling, not a product opinion: each message can
# fan out to several tool round-trips, and this is the only surface where one
# click costs an unbounded-ish amount. Free's is deliberately low; Pro's (60)
# is far past a real day's use and well short of a runaway loop.
#
# All four overridable by env so a tier can be retuned without a deploy.
ASSISTANT_PLANS = {
    "free": {
        "model": env("ASSISTANT_FREE_MODEL", default="claude-haiku-4-5-20251001"),
        "daily_cap": env.int("ASSISTANT_FREE_DAILY_CAP", default=15),
    },
    "pro": {
        "model": env("ASSISTANT_PRO_MODEL", default="claude-sonnet-5"),
        "daily_cap": env.int("ASSISTANT_PRO_DAILY_CAP", default=60),
    },
}
# `daily_cap` above is no longer what stops a student mid-plan — the credit
# system below (docs/credit-system-plan.md) replaced it as the actual
# enforcement in assistant/agent.py. It stays defined here, and
# core/views.py's pricing-page context still reads it, because
# templates/core/pricing.html's own copy names it and that page is out of
# scope for this change (another pass owns pricing-page content).

# ---------------------------------------------------------------------------
# The credit system (docs/credit-system-plan.md) — one pool metering BOTH AI
# surfaces that spend real model money: "Talk to Coverage" chat messages
# (assistant/agent.py, billing/credits.py's "spend_chat") and a Gmail Live
# rescan's Haiku residue classification (capture/gmail_residue.py via
# capture/gmail_live.py::run_rescan, "spend_rescan"). Replaces the old
# per-plan daily MESSAGE cap above as the thing that actually gates a turn;
# `daily_burst` below is the new abuse backstop — see the plan doc's §4 for
# why a monthly pool needs one at all (a runaway loop or a shared password
# burning a month in an hour).
#
# 1 credit ≈ $0.02 of model spend ≈ one Haiku chat message (§1). Sonnet
# costs 3x that in real API pricing, hence Pro's message_cost=3. Every
# number below is set from the margin math in §3 of the plan doc, not a
# round number for its own sake — see that section before changing any of
# them.
CREDIT_PLANS = {
    "free": {
        "monthly_grant": env.int("CREDIT_FREE_MONTHLY_GRANT", default=60),
        "message_cost": env.int("CREDIT_FREE_MESSAGE_COST", default=1),
        "daily_burst": env.int("CREDIT_FREE_DAILY_BURST", default=15),
    },
    "pro": {
        "monthly_grant": env.int("CREDIT_PRO_MONTHLY_GRANT", default=180),
        "message_cost": env.int("CREDIT_PRO_MESSAGE_COST", default=3),
        "daily_burst": env.int("CREDIT_PRO_DAILY_BURST", default=45),
    },
}
# One credit buys a classification pass over this many rescan residue
# threads, rounded up — a maxed-out 100-thread pass costs 10 credits (§1).
CREDIT_RESCAN_THREADS_PER_CREDIT = env.int("CREDIT_RESCAN_THREADS_PER_CREDIT", default=10)

# ---------------------------------------------------------------------------
# Web Push (deadline alerts — accounts.push, send_deadline_push_alerts).
#
# VAPID (RFC 8292) identifies this server to the browsers' own push relays
# (Chrome/Firefox/Safari's infrastructure, not a third-party push SaaS) with
# a self-generated EC keypair — `python manage.py generate_vapid_keys`
# prints a public/private pair in exactly the format these three settings
# want. All three blank by default, same posture as every other optional
# integration on this page: the app boots, every test passes, and the
# Settings toggle simply renders as unavailable until real values land (see
# accounts.push.is_configured()). No paid service and no ongoing cost either
# way — the relay is free, built into the browser.
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")
# The "sub" claim VAPID requires — a contact address the push services can
# reach if this server's traffic ever needs throttling or blocking. Stored
# without the "mailto:" prefix; accounts.push adds it, so this setting reads
# as a plain address wherever else it might be surfaced.
VAPID_CLAIM_EMAIL = env("VAPID_CLAIM_EMAIL", default="")

# The custom user model (docs/build-plan.md §2's `users` table). Set from
# the very first migration — see accounts/models.py for the model and
# README/the build task for why this must never be swapped after real
# data exists.
AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # First in the list = last to touch the response = compresses everything
    # below it. The feed serves ~2MB of HTML uncompressed — seconds on
    # cellular for the page most likely to be opened on a phone. (Django's
    # docs note gzip's BREACH caveat; its own CSRF masking is the standing
    # mitigation, and no other secret is reflected into responses here.)
    "django.middleware.gzip.GZipMiddleware",
    # Serves collected static files in production (harmless in dev, where
    # runserver's staticfiles app handles them). Must sit right after
    # SecurityMiddleware per WhiteNoise's docs.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Activates the signed-in user's own timezone for the rest of the request,
    # so "today" means their day. Must sit AFTER AuthenticationMiddleware —
    # it reads request.user, which does not exist before that.
    #
    # Without it every date boundary in the product is UTC's: the cadence
    # engine's business-day math, the pace ring's week start, and the "closing
    # soon" window all roll over at 08:00 for a Hong Kong student, a third of
    # the way into their working day. See accounts/middleware.py.
    "accounts.middleware.TimezoneMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # django-allauth requires this in addition to AuthenticationMiddleware.
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "coverage_web.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.social_providers",
            ],
        },
    },
]

WSGI_APPLICATION = "coverage_web.wsgi.application"
ASGI_APPLICATION = "coverage_web.asgi.application"

# ---------------------------------------------------------------------------
# Database — Postgres only, deliberately no SQLite fallback.
#
# docs/build-plan.md is explicit: concurrent writers (web + scrape worker +
# inbound-mail webhook) is precisely SQLite's weak spot, and testing against a
# different engine than production would defeat the point of choosing
# Postgres. `psycopg` (v3) is used transparently through Django's built-in
# `django.db.backends.postgresql` backend, which has supported psycopg 3
# natively since Django 4.2 — no custom ENGINE setting needed.
#
# Local default (documented, not secret): a `coverage`/`coverage` role and
# database on localhost. See README.md "Local setup" for the matching
# `createdb`/`createuser` commands.
# ---------------------------------------------------------------------------
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://coverage:coverage@localhost:5432/coverage",
    ),
}
# Multiple git worktrees (background agents, most days) share this one local
# Postgres server, and Django's default test-DB name ("test_<NAME>") doesn't
# know that — two worktrees running pytest at once each drop-and-recreate
# the SAME `test_coverage`, and whichever one loses the race sees mid-DDL
# tables it didn't create: hundreds of spurious errors that look like a
# suite-wide failure but are really just two test runs sharing one database.
# Keying the name off BASE_DIR gives every worktree (a distinct checkout
# path) its own test database automatically, with no env var an agent has
# to remember to set.
DATABASES["default"]["TEST"] = {
    "NAME": f"test_coverage_{hashlib.sha1(str(BASE_DIR).encode()).hexdigest()[:10]}"
}

# ---------------------------------------------------------------------------
# Persistent connections.
#
# Django's default is CONN_MAX_AGE=0: open a new Postgres connection at the
# start of every request and close it at the end. Measured against the local
# database (loopback, no TLS) that handshake costs a median 8.8ms while an
# actual `SELECT 1` on an already-open connection costs 0.07ms — the setup is
# ~125x the query. In production it is strictly worse: the connection crosses
# a real network AND completes a TLS handshake, which is where the usual
# 30-80ms figure for a managed Postgres comes from. That cost is paid once per
# request, on every page, before any of the app's own work begins.
#
# SAFE HERE because the connection ceiling is small and known: the Dockerfile
# runs gunicorn with `--workers 3` (sync workers, one request at a time each),
# so the web service holds at most 3 connections, plus one per worker/cron
# process — far inside even the smallest managed Postgres' limit.
#
# CONN_HEALTH_CHECKS is the pairing that makes reuse safe: Django pings a
# pooled connection before handing it to a request, so one killed server-side
# (a DB restart, an idle-timeout reaper) is transparently replaced rather than
# surfacing as an InterfaceError on a user's page load.
#
# Forced back to 0 under pytest by the repo-root conftest.py — Django does
# NOT do this itself (verified: CONN_MAX_AGE reads as 60 inside a test
# without that hook), and a connection held open across tests is exactly what
# makes teardown fail with "database is being accessed by other users".
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DJANGO_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
# Fail fast if the database is unreachable rather than tying up a gunicorn
# worker for the OS-default TCP timeout (minutes) — the worker's own 60s
# timeout would kill the request anyway, with less useful logging.
DATABASES["default"].setdefault("OPTIONS", {})
DATABASES["default"]["OPTIONS"].setdefault("connect_timeout", 5)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# A bare "linkedin.com/in/x" in the contact form gets https, not http (also
# silences the Django 6 URLField default-scheme deprecation warning).
FORMS_URLFIELD_ASSUME_HTTPS = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# User-uploaded avatars (accounts.User.avatar). Local disk storage: fine at
# this project's local-only, pre-launch stage (docs/product-brief.md), but
# Render's filesystem is ephemeral — swap MEDIA_ROOT for S3-compatible
# storage (e.g. django-storages) before real users' avatars need to survive
# a redeploy. Served by Django itself only in DEBUG (see urls.py); production
# has no MEDIA serving wired up yet because there is no durable store to
# serve it from.
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Auth — django-allauth, Google sign-in with LOGIN-ONLY scopes.
#
# docs/build-plan.md §3: "The login client must never request any `gmail.*`
# scope. If a Gmail capture provider ever ships, it uses a separate
# incremental-consent flow, so a verification stall can never break sign-in."
# Login-only Google OAuth (openid/email/profile) is unrestricted — it never
# touches the CASA/restricted-scope question at all. Do not add scopes here.
# ---------------------------------------------------------------------------
SITE_ID = 1

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# /app/, not "/". Signing in used to land on the marketing homepage — the
# pitch the person just accepted, with the product a further unlabeled click
# away. Signup already goes to /welcome/ (the wizard); sign-IN goes to Today,
# which handles both populations: onboarded users get their queue, fresh ones
# get the setup nudge that links back into the wizard.
LOGIN_REDIRECT_URL = "/app/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/"
# Suppresses allauth's own "signed in as x" / "you have signed out" flashes
# (pure noise on every session) while leaving its other messages (password
# changed, etc.) intact. See accounts/adapter.py.
ACCOUNT_ADAPTER = "accounts.adapter.CoverageAccountAdapter"
# Brand-new accounts land in the onboarding wizard, not on the marketing page.
ACCOUNT_SIGNUP_REDIRECT_URL = "/welcome/"

# Google is the primary sign-in path (see docs/build-plan.md §3); email/
# password signup via allauth's own forms is left at its defaults for now.
ACCOUNT_EMAIL_VERIFICATION = "optional"

# accounts.User has no `username` field at all (email is USERNAME_FIELD —
# see accounts/models.py). allauth defaults to assuming a `username`
# field exists unless told otherwise; without these three settings its
# own login/signup forms 500 with `FieldDoesNotExist: User has no field
# named 'username'` (allauth.utils.get_username_max_length introspects
# whatever ACCOUNT_USER_MODEL_USERNAME_FIELD names, "username" by
# default). ACCOUNT_SIGNUP_FIELDS / ACCOUNT_LOGIN_METHODS are allauth's
# current (65.x) settings — the "*" suffix marks a field required.
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]

# The email page is a CHANGE flow, not an address collection. Without this,
# allauth's account_email view is its multi-address manager — add as many
# addresses as you like, mark one primary — which is a mental model no
# consumer product ships (GitHub/Linear/Notion all model "your email, and a
# verified flow to replace it"). With it, adding a new address + verifying it
# atomically replaces the old one, and the old address keeps working until
# the new one is confirmed. Sign-in stays email-only, so the sign-in
# identifier and this flow can never disagree.
ACCOUNT_CHANGE_EMAIL = True
# The ceiling the change flow needs: the current address plus the one
# awaiting verification. Not user-facing inventory.
ACCOUNT_MAX_EMAIL_ADDRESSES = 2

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        # LOGIN-ONLY. Never add a scope starting with "gmail" here — see the
        # module docstring above and docs/build-plan.md §3 / the Auth note.
        "SCOPE": ["openid", "email", "profile"],
        "AUTH_PARAMS": {"access_type": "online"},
        "OAUTH_PKCE_ENABLED": True,
        "APP": {
            "client_id": env("GOOGLE_OAUTH_CLIENT_ID", default=""),
            "secret": env("GOOGLE_OAUTH_CLIENT_SECRET", default=""),
            "key": "",
        },
    },
    # Same login-only posture as Google: identity scopes only, never mailbox
    # scopes. Each provider stays dark until its credentials are supplied.
    "apple": {
        "APP": {
            "client_id": env("APPLE_OAUTH_CLIENT_ID", default=""),
            "secret": env("APPLE_OAUTH_KEY_ID", default=""),
            "key": env("APPLE_OAUTH_TEAM_ID", default=""),
            "settings": {"certificate_key": env("APPLE_OAUTH_PRIVATE_KEY", default="")},
        },
    },
    "microsoft": {
        "APP": {
            "client_id": env("MICROSOFT_OAUTH_CLIENT_ID", default=""),
            "secret": env("MICROSOFT_OAUTH_CLIENT_SECRET", default=""),
            "key": "",
        },
    },
    "linkedin_oauth2": {
        # LinkedIn retired r_liteprofile/r_emailaddress; OpenID Connect is the
        # supported sign-in surface now.
        "SCOPE": ["openid", "profile", "email"],
        "APP": {
            "client_id": env("LINKEDIN_OAUTH_CLIENT_ID", default=""),
            "secret": env("LINKEDIN_OAUTH_CLIENT_SECRET", default=""),
            "key": "",
        },
    },
}

# Which social providers to surface on the auth pages. A provider only renders
# a button when its client_id is set, so an unconfigured provider is simply
# absent rather than a button that dead-ends.
ENABLED_SOCIAL_PROVIDERS = [
    p for p in ("google", "apple", "microsoft", "linkedin_oauth2")
    if SOCIALACCOUNT_PROVIDERS.get(p, {}).get("APP", {}).get("client_id")
]

# ---------------------------------------------------------------------------
# Gmail Live (docs/build-plan.md §5's "v2" — see capture/gmail_live.py).
#
# DELIBERATELY A SEPARATE OAUTH CLIENT from `SOCIALACCOUNT_PROVIDERS["google"]`
# above. §3's rule is absolute: the sign-in client must never carry a
# `gmail.*` scope, so a Google verification stall on THIS client can never
# break the ability to log in. Create a second OAuth client in the Google
# Cloud Console for this — do not point it at the login client's credentials.
#
# All four values are blank by default, which is the feature's actual
# off-switch: `gmail_live.is_configured()` gates every entry point on all four
# being set, so an unconfigured deploy exposes no connect button and runs no
# watch/poll commands, rather than 500ing on a missing credential.
# ---------------------------------------------------------------------------
GMAIL_LIVE_CLIENT_ID = env("GMAIL_LIVE_CLIENT_ID", default="")
GMAIL_LIVE_CLIENT_SECRET = env("GMAIL_LIVE_CLIENT_SECRET", default="")
# The Pub/Sub topic `users.watch()` publishes change notifications to, as
# "projects/<project>/topics/<topic>" — created once by hand in Cloud Console
# (see docs/gmail-live-setup.md), not managed by this app.
GMAIL_LIVE_PUBSUB_TOPIC = env("GMAIL_LIVE_PUBSUB_TOPIC", default="")
# A PULL subscription on that topic, "projects/<project>/subscriptions/<sub>".
# Pull, not push: a push subscription needs a public HTTPS endpoint, which
# means "deploy Coverage" becomes a precondition. Pull means
# `gmail_pubsub_listen` can run against this project long before that.
GMAIL_LIVE_PUBSUB_SUBSCRIPTION = env("GMAIL_LIVE_PUBSUB_SUBSCRIPTION", default="")
# Fernet key encrypting `GmailConnection.refresh_token_encrypted` at rest.
# Generate once with `Fernet.generate_key()` and never rotate it casually —
# rotating without re-encrypting every row first makes every stored refresh
# token unreadable, which reads to a user as "Coverage silently disconnected
# my Gmail."
GMAIL_LIVE_TOKEN_KEY = env("GMAIL_LIVE_TOKEN_KEY", default="")
# Requested at connect time. Kept to exactly what capture/gmail_live.py's
# deterministic classifiers read (headers, an .ics attachment's own fields,
# a short snippet for bounce-pattern matching) — narrower than full message
# bodies, which matters both for the privacy story and because a narrower
# scope is the easier one to justify if this ever goes through Google's
# verification (see the CASA discussion in build-plan.md's risk register).
GMAIL_LIVE_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# ---------------------------------------------------------------------------
# Stripe (docs/credit-system-plan.md's "Stripe later" note — see
# billing/stripe_gateway.py). Pay-as-you-go credit top-ups, Checkout
# Sessions built with ad-hoc `price_data` rather than pre-created Stripe
# Price IDs, so nothing here depends on Dashboard configuration existing
# yet.
#
# Both blank by default, which is the feature's actual off-switch, exactly
# like GMAIL_LIVE_* above: `stripe_gateway.is_configured()` gates every
# entry point on both being set, so an unconfigured deploy shows the "Buy
# more credits" block as a disabled "Coming soon" state instead of a
# dead-end checkout button, and the webhook 400s cleanly rather than
# verifying a signature against a secret that doesn't exist.
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")
