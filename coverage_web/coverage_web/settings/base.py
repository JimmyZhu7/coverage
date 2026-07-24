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
    "crm",  # private zone: user_firms, contacts, touches, capture_events, tasks
    "analytics",  # private zone: user_opportunities, fit_scores, product_events, imports
    "capture",  # inbound-email capture pipeline (no models; uses crm.CaptureEvent)
]

# Shared secret the inbound-email webhook uses to authenticate provider POSTs
# (build-plan.md §5). Placeholder locally; the PaaS/Postmark sets the real value.
CAPTURE_INBOUND_SECRET = env("CAPTURE_INBOUND_SECRET", default="local-dev-capture-secret")

# The domain of each user's capture address (u-<slug>@<domain>). Must match the
# domain whose inbound MX points at Postmark. Placeholder until DNS is set up.
CAPTURE_INBOUND_DOMAIN = env("CAPTURE_INBOUND_DOMAIN", default="in.coverage.app")

# The custom user model (docs/build-plan.md §2's `users` table). Set from
# the very first migration — see accounts/models.py for the model and
# README/the build task for why this must never be swapped after real
# data exists.
AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves collected static files in production (harmless in dev, where
    # runserver's staticfiles app handles them). Must sit right after
    # SecurityMiddleware per WhiteNoise's docs.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

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

LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/"
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
