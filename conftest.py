"""Suite-wide test isolation, applied before any test in any package runs.

This file sits next to `pyproject.toml` so pytest loads it once for the whole
run — `coverage_web`, `coverage_domain` and `coverage_connectors` alike —
ahead of the narrower `conftest.py` files nested under individual test
packages.
"""

from __future__ import annotations

import pytest


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
