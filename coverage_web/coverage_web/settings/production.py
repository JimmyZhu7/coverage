"""
Production settings.

Usage: DJANGO_SETTINGS_MODULE=coverage_web.settings.production, set by the
PaaS environment (Render or Fly — undecided, see docs/build-plan.md §1). This
module intentionally has no defaults for the values that must never be
guessed (SECRET_KEY, ALLOWED_HOSTS): missing either raises
django.core.exceptions.ImproperlyConfigured at boot rather than silently
running insecurely.

"No default" is not the same as "no source". Three values below —
ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS and SITE_URL — fall back to
`RENDER_EXTERNAL_HOSTNAME`, the hostname Render injects into every service
it runs. That is not a guess: it is the host answering, and it is what makes
a first Blueprint apply boot before anyone has typed anything into the
dashboard. An explicit env var always wins, and off Render the fallback is
empty, so the fail-loud rule is unchanged where there is nothing to fall
back to. `manage.py deploy_preflight` (ops/) prints which source each one
resolved from.
"""

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# The host this service is actually reachable at, injected by Render on
# every service it runs (`coverage-web.onrender.com`). Read here so a first
# Blueprint apply BOOTS: `DJANGO_ALLOWED_HOSTS` is a `sync: false` var, which
# means blank until a human types it into the dashboard, and a blank one used
# to be an ImproperlyConfigured at import — the web service failing to start
# before anyone could see it had started. Falling back to the platform's own
# answer for "what host am I" is not a guess: it is the host, from the host.
# Empty everywhere that is not Render, where the explicit var stays the only
# source and the no-default rule below still applies.
RENDER_EXTERNAL_HOSTNAME = env("RENDER_EXTERNAL_HOSTNAME", default="").strip()

# Still no silent default: with neither the env var NOR a platform hostname,
# `env.list` raises ImproperlyConfigured at boot rather than serving requests
# for an unexpected Host header. The explicit var wins whenever it is set, so
# a custom domain (or a comma-separated pair) overrides the platform's.
if env.list("DJANGO_ALLOWED_HOSTS", default=[]):
    ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
elif RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS = [RENDER_EXTERNAL_HOSTNAME]
else:
    ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")  # raises, by design

# No default: refuse to boot with the insecure dev key from base.py.
SECRET_KEY = env("DJANGO_SECRET_KEY")

# Standard HTTPS hardening. The PaaS terminates TLS and forwards
# X-Forwarded-Proto, which is why SECURE_PROXY_SSL_HEADER is set below.
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# The health check is exempt from the redirect above. Render polls
# `healthCheckPath: /healthz` (render.yaml) and marks the service live on a
# 200; SecurityMiddleware answers any request that does not carry
# `X-Forwarded-Proto: https` with a 301 first, which is not a 200, so a
# probe that does not set that header leaves the service permanently
# unhealthy and the deploy never goes green. Whether Render's probe sets it
# cannot be established without a live deploy, so this closes the question
# instead of leaving a first deploy to discover it.
#
# Safe to exempt: `core.views.healthz` reads nothing, writes nothing, sets no
# cookie and touches no database (see its docstring) — there is no session or
# secret for a plaintext hop to leak. The pattern matches against
# `request.path` with the leading slash stripped, which is why there is none
# here.
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 7)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Django 4+ requires the scheme-qualified origin(s) for CSRF on unsafe methods
# behind an HTTPS proxy (admin login, all form POSTs). Set to your deployed
# origin(s), e.g. "https://coverage.onrender.com,https://app.coverage.app".
# Same platform fallback as ALLOWED_HOSTS above, for the same reason and with
# the same precedence: an explicit list always wins.
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[]) or (
    [f"https://{RENDER_EXTERNAL_HOSTNAME}"] if RENDER_EXTERNAL_HOSTNAME else []
)

# The origin the app builds absolute links at outside a request (the weekly
# digest's links, the trial-ended email's Settings link). base.py defaults it
# to localhost, which is what made every digest link on a real deploy point
# at a machine the recipient does not have — `SITE_URL` was not in
# render.yaml at all. Read here rather than inherited so the platform
# fallback applies, with the same precedence as the two above.
_site_url = env("SITE_URL", default="").strip().rstrip("/")
if _site_url:
    SITE_URL = _site_url
elif RENDER_EXTERNAL_HOSTNAME:
    SITE_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}"

# Outbound email (password resets today; digests/alerts later). EMAIL_URL is a
# standard django-environ URL, e.g. smtp+tls://user:pass@smtp.resend.com:587.
# Default is the console backend so an unconfigured deploy logs the reset link
# to Render's logs (recoverable) instead of 500ing on localhost SMTP.
vars().update(env.email_url("EMAIL_URL", default="consolemail://"))
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Coverage <no-reply@localhost>")

# Serve compressed, hashed static files via WhiteNoise. Requires a
# `collectstatic` at build time (the Dockerfile / render build step does this).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Optional error monitoring: only activates when a DSN is provided, so the app
# boots fine without Sentry configured. The package is an optional dependency —
# `uv pip install sentry-sdk` (or add it to deps) to use it; a set DSN with the
# package missing warns rather than crashing the boot.
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    try:
        import sentry_sdk

        sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.0, send_default_pii=False)
    except ImportError:
        import warnings

        warnings.warn("SENTRY_DSN is set but sentry-sdk is not installed; skipping.")
