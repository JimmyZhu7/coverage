"""Guard: production must not inherit a development secret, must not boot
without a host it can name, and must not 301 its own health check.

`settings/production.py` states its own policy in its docstring — "no
defaults for the values that must never be guessed" — and enforces it for
SECRET_KEY and ALLOWED_HOSTS by refusing to resolve them from nothing.

"No default" is not the same as "no source", and the second half of this
file is about that distinction: three values fall back to
`RENDER_EXTERNAL_HOSTNAME`, the hostname Render injects into every service
it runs. That is not a guess — it is the host answering — and it is what
makes a first Blueprint apply boot before anyone has typed anything into the
dashboard. Off Render the variable is absent and the fail-loud rule is
exactly what it was.

These tests import the production settings module under a controlled
environment rather than asserting on source text, so they fail if either
`env(...)` call ever regains a real default.
"""

from __future__ import annotations

import importlib
import os
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured

MODULE = "coverage_web.settings.production"

# The minimum environment production needs before it will boot at all.
BASE_ENV = {
    "DJANGO_SECRET_KEY": "x" * 60,
    "DJANGO_ALLOWED_HOSTS": "coverage.example.com",
    "DATABASE_URL": "postgres://u:p@localhost:5432/db",
}

# Cleared before every load. `RENDER_EXTERNAL_HOSTNAME` is not part of
# BASE_ENV — production does not need it — but it IS now read, so a test
# that means "nothing to fall back to" has to say so.
_CLEARED = tuple(BASE_ENV) + (
    "RENDER_EXTERNAL_HOSTNAME",
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "SITE_URL",
)


def _load(monkeypatch, env: dict[str, str]):
    """Import production settings with exactly `env` set."""
    for key in _CLEARED:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(importlib.import_module(MODULE))


@pytest.fixture(autouse=True)
def _restore_module():
    """Every test here reloads the module under a doctored environment.
    Leaving the last one's version installed would hand whatever runs next a
    settings module built from a monkeypatched env.

    The restore supplies BASE_ENV itself rather than trusting the ambient
    one: this repo's `.env` does not set DJANGO_ALLOWED_HOSTS (production is
    the only module that requires it), so a plain reload after a test that
    deleted it would raise ImproperlyConfigured in teardown.
    """
    yield
    with mock.patch.dict(os.environ, BASE_ENV):
        importlib.reload(importlib.import_module(MODULE))


@pytest.mark.parametrize("missing", ["DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS"])
def test_the_existing_no_guess_values_still_refuse_to_boot(monkeypatch, missing):
    env = {k: v for k, v in BASE_ENV.items() if k != missing}
    with pytest.raises(ImproperlyConfigured):
        _load(monkeypatch, env)


# ---------------------------------------------------------------------------
# The platform fallback. Item 1 on the audit's list of what a first deploy
# would break, in order: "web service fails to boot until
# DJANGO_ALLOWED_HOSTS is typed in (no default)" — for a `sync: false` var
# that is blank until a human fills it in.
# ---------------------------------------------------------------------------
def test_the_render_hostname_is_enough_to_boot(monkeypatch):
    env = {k: v for k, v in BASE_ENV.items() if k != "DJANGO_ALLOWED_HOSTS"}
    env["RENDER_EXTERNAL_HOSTNAME"] = "coverage-web.onrender.com"

    module = _load(monkeypatch, env)

    assert module.ALLOWED_HOSTS == ["coverage-web.onrender.com"]


def test_an_explicit_host_list_always_wins_over_the_platform(monkeypatch):
    """A custom domain is typed in precisely because the platform hostname is
    not the whole answer."""
    env = dict(BASE_ENV)
    env["DJANGO_ALLOWED_HOSTS"] = "coverage.app,www.coverage.app"
    env["RENDER_EXTERNAL_HOSTNAME"] = "coverage-web.onrender.com"

    module = _load(monkeypatch, env)

    assert module.ALLOWED_HOSTS == ["coverage.app", "www.coverage.app"]


def test_neither_source_still_refuses_to_boot(monkeypatch):
    """The fail-loud rule is unchanged where there is nothing to fall back
    to — this is not a silent default, it is a second source."""
    env = {k: v for k, v in BASE_ENV.items() if k != "DJANGO_ALLOWED_HOSTS"}

    with pytest.raises(ImproperlyConfigured):
        _load(monkeypatch, env)


def test_the_csrf_origin_falls_back_to_the_render_host_with_a_scheme(monkeypatch):
    env = {k: v for k, v in BASE_ENV.items() if k != "DJANGO_ALLOWED_HOSTS"}
    env["RENDER_EXTERNAL_HOSTNAME"] = "coverage-web.onrender.com"

    module = _load(monkeypatch, env)

    assert module.CSRF_TRUSTED_ORIGINS == ["https://coverage-web.onrender.com"]


def test_site_url_falls_back_to_the_render_host(monkeypatch):
    """`SITE_URL` was absent from render.yaml entirely, so every weekly
    digest link on a real deploy pointed at http://localhost:8000 — base.py's
    dev default, i.e. the reader's own machine."""
    env = dict(BASE_ENV)
    env["RENDER_EXTERNAL_HOSTNAME"] = "coverage-web.onrender.com"

    module = _load(monkeypatch, env)

    assert module.SITE_URL == "https://coverage-web.onrender.com"


def test_an_explicit_site_url_is_never_overwritten(monkeypatch):
    env = dict(BASE_ENV)
    env["RENDER_EXTERNAL_HOSTNAME"] = "coverage-web.onrender.com"
    env["SITE_URL"] = "https://coverage.app"

    module = _load(monkeypatch, env)

    assert module.SITE_URL == "https://coverage.app"


# ---------------------------------------------------------------------------
# The health check. Item 2 on the same list.
# ---------------------------------------------------------------------------
def test_healthz_is_exempt_from_the_ssl_redirect(monkeypatch):
    """Render polls `healthCheckPath: /healthz` and marks the service live on
    a 200. SecurityMiddleware answers any request without
    `X-Forwarded-Proto: https` with a 301 first, which is not a 200 — so a
    probe that does not set that header leaves the service permanently
    unhealthy. Safe to exempt: `core.views.healthz` reads nothing, writes
    nothing, sets no cookie and touches no database."""
    module = _load(monkeypatch, dict(BASE_ENV))

    assert module.SECURE_SSL_REDIRECT is True
    assert any("healthz" in pattern for pattern in module.SECURE_REDIRECT_EXEMPT)


def test_the_exempt_pattern_matches_the_path_django_actually_tests(monkeypatch):
    """SecurityMiddleware matches against `request.path` with the leading
    slash STRIPPED, so a pattern written as `^/healthz$` would silently
    never match and the check would keep 301ing."""
    import re

    module = _load(monkeypatch, dict(BASE_ENV))

    patterns = [re.compile(p) for p in module.SECURE_REDIRECT_EXEMPT]
    assert any(p.search("healthz") for p in patterns)
    # And it must not exempt the whole site by accident.
    assert not any(p.search("app/") for p in patterns)
    assert not any(p.search("admin/login/") for p in patterns)
