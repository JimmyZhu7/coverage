"""Web-suite defaults: loaded after the repo root's conftest.py, for every
test under coverage_web/ and nothing outside it.
"""

from __future__ import annotations

from unittest import mock

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "outreach_blackout: run on the real calendar. Without it the suite "
        "sees an ordinary weekday (see `_ordinary_weekday` in "
        "coverage_web/conftest.py).",
    )
    config.addinivalue_line(
        "markers",
        "robots_live: let core.robots run its own robots.txt fetch path. "
        "Without it no test reaches the network for one (see "
        "`_no_live_robots_fetches` in coverage_web/conftest.py).",
    )


@pytest.fixture(autouse=True)
def _no_live_robots_fetches(request):
    """No test fetches a real robots.txt, and none inherits a cached one.

    WHY THIS EXISTS: `enrich_postings` and `fetch_firm_logos` check
    `core.robots.is_allowed` before every outbound request. Their tests fake
    the POSTING fetch (`monkeypatch.setattr(enrich_mod.requests, "get", ...)`)
    but the robots check goes out through `urllib`, not `requests`, so without
    this fixture those tests would quietly start reaching higher.gs.com and
    a dozen real careers hosts — slow, flaky, and rude, from a suite whose
    whole point is that it runs offline.

    Patched at `_fetch_parser`, which is the one seam between "what do the
    rules say" and "go and read them": `is_allowed` still runs for real, and
    a None parser means "rules unreadable", which the module documents as
    ALLOW. So the default under test is exactly today's behaviour — every
    URL permitted — and a test that wants a refusal seeds `_CACHE` with a
    parser of its own.

    `_CACHE` is cleared on both sides because it is module-level state that
    outlives a test: one test seeding a host would otherwise decide the
    answer for every later test that touches it.

    This is a DEFAULT, not a lock, the same convention as `_ordinary_weekday`
    below. A test ABOUT the fetch path itself opts out with

        @pytest.mark.robots_live

    and fakes `urllib.request.urlopen` instead.
    """
    from core import robots as core_robots

    core_robots.reset_cache()
    if request.node.get_closest_marker("robots_live"):
        yield
        core_robots.reset_cache()
        return
    with mock.patch.object(core_robots, "_fetch_parser", return_value=None):
        yield
    core_robots.reset_cache()


@pytest.fixture(autouse=True)
def _ordinary_weekday(request):
    """Every test sees a working weekday unless it says otherwise.

    WHY THIS EXISTS: `crm.today.outreach_blackout` holds the Today plan to
    confirmed deadlines on weekends and from Dec 20 to Jan 2, and roughly a
    hundred tests across crm/, capture/ and assistant/ render that plan on
    the real clock and assert on what it holds. Without this fixture the
    suite is red two days in seven and two weeks a year, not because
    anything broke but because the calendar did what the product now says
    it should. A suite whose colour depends on the day it runs is a suite
    people learn to ignore.

    The patch sits at the one seam the feature hangs off. The helper is
    pure, and both `crm.today._cockpit_context` and
    `crm.digest.assemble_digest` look it up at call time, so nothing else
    about the clock changes: `timezone.now` is real, business-day maths is
    real, only the calendar rule is answered "no".

    This is a DEFAULT, not a lock, the same convention as the root
    conftest's `_no_live_anthropic_calls`. A test that is ABOUT the blackout
    opts back onto the real calendar with

        @pytest.mark.outreach_blackout

    and pins its own date with `mock.patch("django.utils.timezone.now")`,
    the pattern `crm/tests/test_today_timezone.py` established.
    """
    if request.node.get_closest_marker("outreach_blackout"):
        yield
        return
    with mock.patch("crm.today.outreach_blackout", return_value=None):
        yield
