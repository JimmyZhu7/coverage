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

# Serve static through WhiteNoise in dev too, with caching off.
#
# Without this, runserver's own static handler sends `Last-Modified` and no
# `Cache-Control`/`ETag`, which puts browsers into *heuristic* caching: they
# invent a freshness lifetime (commonly ~10% of the file's age) and keep
# serving the old file. Safari is aggressive about this, so an edit to
# coverage.css could go unseen for a long time — which looks exactly like a
# CSS bug that isn't there (it presented once as a wildly oversized avatar,
# because only the pre-avatar stylesheet had loaded).
#
# Production is unaffected and already correct: it uses WhiteNoise's
# CompressedManifestStaticFilesStorage (settings/production.py), which
# content-hashes filenames, so far-future caching there is safe by design.
# `runserver_nostatic` must come BEFORE django.contrib.staticfiles: with
# DEBUG=True, runserver otherwise wraps the app in Django's own
# StaticFilesHandler, which intercepts /static/ before WhiteNoise middleware
# ever runs — so the settings below would silently do nothing.
INSTALLED_APPS = ["whitenoise.runserver_nostatic"] + INSTALLED_APPS  # noqa: F405

WHITENOISE_USE_FINDERS = True   # serve straight from STATICFILES_DIRS, no collectstatic
WHITENOISE_AUTOREFRESH = True   # re-stat files per request instead of caching the scan
WHITENOISE_MAX_AGE = 0          # emits `Cache-Control: max-age=0, public`
