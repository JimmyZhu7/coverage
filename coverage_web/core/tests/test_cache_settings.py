"""Guard: the rate-limit cache is shared when it needs to be.

`settings/base.py` picks the cache backend off REDIS_URL. That choice is not
a performance tuning knob — the cache is where django-allauth keeps its
failed-login, signup and password-reset counters, and where core/views.py and
billing/views.py keep the search and waitlist throttles. Django's fallback,
LocMemCache, is per-process, and the Dockerfile runs gunicorn with
`--workers 3`: three independent copies of every counter, each allowing the
full quota, all reset on the next deploy. "5 failed logins per 5 minutes"
silently becomes 15.

These tests import the settings module under a controlled environment rather
than asserting on source text, so they fail if the wiring is ever removed or
inverted.
"""

from __future__ import annotations

import importlib

import pytest

MODULE = "coverage_web.settings.base"

LOCMEM = "django.core.cache.backends.locmem.LocMemCache"
REDIS = "django.core.cache.backends.redis.RedisCache"


@pytest.fixture
def load(monkeypatch):
    """Import base settings with REDIS_URL set to exactly `value`.

    Reloading the module recomputes its constants; it does not touch the
    already-populated `django.conf.settings`, so nothing else in the suite
    sees a changed cache.
    """

    def _load(value: str | None):
        if value is None:
            monkeypatch.delenv("REDIS_URL", raising=False)
        else:
            monkeypatch.setenv("REDIS_URL", value)
        return importlib.reload(importlib.import_module(MODULE))

    yield _load
    # Leave the module holding the ambient environment's answer again.
    monkeypatch.undo()
    importlib.reload(importlib.import_module(MODULE))


def test_a_redis_url_makes_the_cache_shared_across_workers(load):
    base = load("redis://cache.internal:6379/0")
    assert base.CACHES["default"]["BACKEND"] == REDIS
    assert base.CACHES["default"]["LOCATION"] == "redis://cache.internal:6379/0"


def test_no_redis_url_leaves_local_development_on_the_in_memory_cache(load):
    """One process locally, nothing to share — and no Redis to install
    before `runserver` works."""
    base = load(None)
    assert base.CACHES["default"]["BACKEND"] == LOCMEM


def test_a_blank_redis_url_counts_as_absent(load):
    """`.env.example` ships `REDIS_URL=` with no value, so a developer who
    copies it must land on LocMem, not on a Redis client pointed at "".
    """
    base = load("")
    assert base.CACHES["default"]["BACKEND"] == LOCMEM


def test_the_redis_client_the_backend_needs_is_actually_installed():
    """Django ships RedisCache but imports redis-py lazily, inside the first
    cache operation. Without the dependency declared, a deploy that sets
    REDIS_URL boots clean and then 500s on the first login attempt."""
    importlib.import_module("redis")
