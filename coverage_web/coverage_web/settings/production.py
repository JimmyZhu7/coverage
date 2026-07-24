"""
Production settings.

Usage: DJANGO_SETTINGS_MODULE=coverage_web.settings.production, set by the
PaaS environment (Render or Fly — undecided, see docs/build-plan.md §1). This
module intentionally has no defaults for the values that must never be
guessed (SECRET_KEY, ALLOWED_HOSTS): missing either raises
django.core.exceptions.ImproperlyConfigured at boot rather than silently
running insecurely.
"""

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# No default: an unset DJANGO_ALLOWED_HOSTS must fail loudly, not serve
# requests for an unexpected Host header.
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# No default: refuse to boot with the insecure dev key from base.py.
SECRET_KEY = env("DJANGO_SECRET_KEY")

# Standard HTTPS hardening. The PaaS terminates TLS and forwards
# X-Forwarded-Proto, which is why SECURE_PROXY_SSL_HEADER is set below.
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 7)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Django 4+ requires the scheme-qualified origin(s) for CSRF on unsafe methods
# behind an HTTPS proxy (admin login, all form POSTs). Set to your deployed
# origin(s), e.g. "https://coverage.onrender.com,https://app.coverage.app".
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

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
