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


# ---------------------------------------------------------------------------
# HSTS: the header must not claim something the origin has not earned.
# ---------------------------------------------------------------------------
def test_hsts_preload_is_off_by_default(monkeypatch):
    """`preload` is a CLAIM, not a request: it asserts the origin meets
    hstspreload.org's bar, which is a max-age of at least one year plus
    subdomains plus an HTTP redirect. Production shipped it beside a
    SEVEN-DAY max-age, so the header advertised a qualification the domain
    could not have been granted. Getting onto that list is also a one-way
    door — removal takes months and ships with a browser release — which
    makes it the founder's call after a clean HTTPS deploy, not a default.
    See docs/deploy.md §5b for the opt-in order.
    """
    module = _load(monkeypatch, BASE_ENV)
    assert module.SECURE_HSTS_PRELOAD is False


def test_the_preload_claim_can_be_turned_on_from_the_environment(monkeypatch):
    module = _load(monkeypatch, {**BASE_ENV, "DJANGO_SECURE_HSTS_PRELOAD": "true"})
    assert module.SECURE_HSTS_PRELOAD is True


def test_hsts_and_subdomains_are_still_on(monkeypatch):
    """Turning off the unearned claim must not have turned off the real
    protection underneath it."""
    module = _load(monkeypatch, BASE_ENV)
    assert module.SECURE_HSTS_SECONDS >= 60 * 60 * 24 * 7
    assert module.SECURE_HSTS_INCLUDE_SUBDOMAINS is True
    assert module.SECURE_SSL_REDIRECT is True


# ---------------------------------------------------------------------------
# Email verification: a standing founder decision, written down where the
# setting is, so the next reader does not have to rediscover the risk.
# ---------------------------------------------------------------------------
def test_optional_email_verification_carries_its_risk_in_writing():
    """`ACCOUNT_EMAIL_VERIFICATION = "optional"` means an unverified account
    is a fully working account, which allows pre-registration squatting on a
    school address the squatter cannot read. It stays "optional" because
    "mandatory" makes signup depend on outbound mail, and mail sending is
    deferred paid setup — but that is a decision, and a decision nobody
    wrote down is indistinguishable from an oversight six months later.
    """
    from pathlib import Path

    from django.conf import settings

    source = Path(settings.BASE_DIR) / "coverage_web" / "settings" / "base.py"
    text = source.read_text()
    before = text.split('ACCOUNT_EMAIL_VERIFICATION = "optional"', 1)[0]
    note = before[-2000:]
    assert "squatting" in note
    assert "mandatory" in note
