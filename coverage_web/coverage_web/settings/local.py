"""
Local development settings.

Usage: DJANGO_SETTINGS_MODULE=coverage_web.settings.local (the default — see
manage.py). Still requires a real Postgres DATABASE_URL; there is no SQLite
fallback (see base.py's Database section for why).
"""

from .base import *  # noqa: F401,F403

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# Print outgoing mail to the console instead of sending it anywhere real.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
