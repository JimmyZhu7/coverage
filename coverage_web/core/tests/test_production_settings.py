"""Guard: production must not inherit a development secret.

`settings/production.py` states its own policy in its docstring — "no
defaults for the values that must never be guessed" — and enforces it for
SECRET_KEY and ALLOWED_HOSTS by reading them with no fallback, so an unset
env var raises ImproperlyConfigured at boot instead of serving insecurely.

These tests import the production settings module under a controlled
environment rather than asserting on source text, so they fail if either
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
    for key in BASE_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(importlib.import_module(MODULE))


@pytest.mark.parametrize("missing", ["DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS"])
def test_the_existing_no_guess_values_still_refuse_to_boot(monkeypatch, missing):
    env = {k: v for k, v in BASE_ENV.items() if k != missing}
    with pytest.raises(ImproperlyConfigured):
        _load(monkeypatch, env)
