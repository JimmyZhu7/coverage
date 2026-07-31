"""Guard: production must not inherit a development secret.

`settings/production.py` states its own policy in its docstring — "no
defaults for the values that must never be guessed" — and enforces it for
SECRET_KEY and ALLOWED_HOSTS by reading them with no fallback, so an unset
env var raises ImproperlyConfigured at boot instead of serving insecurely.

CAPTURE_INBOUND_SECRET was missing from that list. It guards
`/capture/inbound/`, which is `@csrf_exempt`, carries no session auth, and
WRITES — it runs the ingest pipeline over whatever JSON it receives. Its
only gate is that shared secret. base.py gives it a development fallback so
the app runs locally out of the box; inheriting that fallback in production
would have published the key, because the fallback is committed to this
repository.

These tests import the production settings module under a controlled
environment rather than asserting on source text, so they fail if the
`env(...)` call ever regains a default.
"""

from __future__ import annotations

import importlib

import pytest
from django.core.exceptions import ImproperlyConfigured

MODULE = "coverage_web.settings.production"

# The minimum environment production needs before it will boot at all.
BASE_ENV = {
    "DJANGO_SECRET_KEY": "x" * 60,
    "DJANGO_ALLOWED_HOSTS": "coverage.example.com",
    "DATABASE_URL": "postgres://u:p@localhost:5432/db",
}


def _load(monkeypatch, env: dict[str, str]):
    """Import production settings with exactly `env` set."""
    for key in (*BASE_ENV, "CAPTURE_INBOUND_SECRET"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(importlib.import_module(MODULE))


@pytest.mark.parametrize("missing", ["DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS"])
def test_the_existing_no_guess_values_still_refuse_to_boot(monkeypatch, missing):
    """The policy this module already enforced. Pinned so the new case below
    is understood as joining a rule, not inventing one."""
    env = {k: v for k, v in BASE_ENV.items() if k != missing}
    env["CAPTURE_INBOUND_SECRET"] = "real-secret"
    with pytest.raises(ImproperlyConfigured):
        _load(monkeypatch, env)


def test_production_refuses_to_boot_without_the_capture_secret(monkeypatch):
    """The bug: unset, this used to silently inherit base.py's committed
    development fallback and leave the write webhook open to anyone who had
    read the source."""
    with pytest.raises(ImproperlyConfigured):
        _load(monkeypatch, dict(BASE_ENV))


def test_production_never_serves_the_development_fallback(monkeypatch):
    """Belt and braces: even set, it must be the value from the environment
    and never the string base.py ships."""
    settings = _load(
        monkeypatch, {**BASE_ENV, "CAPTURE_INBOUND_SECRET": "a-real-deploy-secret"}
    )
    assert settings.CAPTURE_INBOUND_SECRET == "a-real-deploy-secret"
    assert "local-dev" not in settings.CAPTURE_INBOUND_SECRET
