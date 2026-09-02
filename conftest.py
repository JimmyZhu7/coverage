"""Suite-wide test isolation, applied before any test in any package runs.

This file sits next to `pyproject.toml` so pytest loads it once for the whole
run — `coverage_web`, `coverage_domain` and `coverage_connectors` alike —
ahead of the narrower `conftest.py` files nested under individual test
packages.
"""

from __future__ import annotations

import pytest


def pytest_configure(config):
    """Pin CONN_MAX_AGE to 0 for the whole suite.

    `settings/base.py` turns on persistent database connections (default 60s)
    because reconnecting per request costs a TCP+TLS handshake on every page
    load in production. Django does NOT override that under test — a plain
    `pytest.mark.django_db` test reads CONN_MAX_AGE as 60 — and a connection
    still held open when a test finishes is precisely what makes teardown
    fail with `database "test_coverage" is being accessed by other users`.

    This hook runs before pytest-django creates the test database, so the
    value is already 0 by the time any connection is opened. Production and
    dev are unaffected.
    """
    from django.conf import settings

    for alias in settings.DATABASES:
        settings.DATABASES[alias]["CONN_MAX_AGE"] = 0


def pytest_collection_modifyitems(items):
    """Apply `stress` and `slow` by shape, so nobody has to remember to.

    Both markers exist to let a developer run `-m "not slow and not stress"`
    for a fast inner loop (README.md), and both are DERIVED rather than hand-
    written, because a hand-written marker is a marker that goes stale: a new
    `test_stress_*` module or a new page-render test would silently land in
    the fast run and make it slow again, and nothing would ever say so.

    `stress` — the file is named `test_stress_*`. That is already this repo's
    naming convention for a generated matrix over a pure function, and the
    audit that motivated this counted 4,006 such cases (43% of the suite).

    `slow` — the test takes the `client` fixture, i.e. it renders a page
    through the Django test client with a fixture world built per test. That
    is the suite's actual clock: the ≥100 ms bucket is 1,369 tests and 251 s
    of a 421 s run, and it is very nearly the same set. `fixturenames` is the
    resolved closure, so a test reaching the client through a wrapper fixture
    (`logged_in`, `world`) is caught too — which is the point.

    Marks are ADDED, never replaced: a test that already carries `slow` by
    hand keeps it, and pytest ignores a duplicate.
    """
    for item in items:
        if item.path is not None and item.path.name.startswith("test_stress_"):
            item.add_marker(pytest.mark.stress)
        if "client" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.slow)


@pytest.fixture(autouse=True)
def _no_live_anthropic_calls(settings):
    """Blank `ANTHROPIC_API_KEY` for every test in the suite.

    WHY THIS EXISTS: `settings/base.py` reads the key from the environment via
    `env("ANTHROPIC_API_KEY", default="")`, and `.env` at the repo root now
    holds a REAL key (added so the live API could be exercised by hand). Django
    loads `.env` in tests exactly as it does in production, so without this
    fixture that real key would be visible to `assistant.client.is_configured()`
    and `directory.ai_extract.is_configured()` during a plain `pytest` run.

    That breaks an invariant the whole codebase is written against: every
    optional integration is dark by default, and each AI module's docstring
    promises the app boots and the full suite passes with nothing configured.
    A live key silently flips those gates to True, so any test that renders a
    page touching the model — `crm:week` builds the daily brief on every full
    load, and it is hit by ~80 tests — would make a real, billed, nondeterministic
    API call and assert against whatever text the model happened to return.

    Blanking the key here restores "no key set in tests" as a property of the
    suite rather than of whichever machine it runs on, independent of `.env`.

    This is a DEFAULT, not a lock. A test that needs the configured path still
    gets it by declaring one explicitly, which is the established convention in
    `crm/tests/test_ai_brief.py` and `directory/tests/test_ai_extract.py`:

        @override_settings(ANTHROPIC_API_KEY="sk-test-key")

    A function-level `override_settings` wraps the test body, so it applies
    after this fixture and wins. Tests that need the dark path assert it with a
    fake client or by patching `is_configured`, and are unaffected either way.
    """
    settings.ANTHROPIC_API_KEY = ""
