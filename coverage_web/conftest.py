"""Web-suite defaults: loaded after the repo root's conftest.py, for every
test under coverage_web/ and nothing outside it.
"""

from __future__ import annotations

import datetime as _dt
import os
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# The simulated clock (WS-OPS-12).
#
# `audit-perf-tests.md §2` found no test that is red on a specific future
# date, but the finding was one reader's manual pass over 256 date literals in
# 40 test files, and three tests hardcoded today's date in a single night. A
# manual pass does not repeat; a command does.
#
#     COVERAGE_FAKE_TODAY=2026-12-24 pytest ...
#     COVERAGE_FAKE_TODAY=saturday   pytest ...
#
# Both dates are chosen. 2026-12-24 sits inside `crm.today.outreach_blackout`'s
# Dec 20 to Jan 2 window and a Saturday is the other half of that same rule, so
# those are the two calendar shapes the product behaves differently on.
# `saturday` resolves to the NEXT Saturday rather than a fixed date, so the
# command keeps meaning what it says next year.
#
# WHAT IT PATCHES, and therefore what it does not cover:
# `django.utils.timezone.now`, which `timezone.localdate` and
# `timezone.localtime` both reach through at call time — 154 of the 165 clock
# reads in non-test code. The other 11 are `datetime.date.today()`, a C-level
# builtin this cannot reach without a dependency (freezegun, time-machine)
# that this repo does not carry. Those 11 stay on the real clock under a
# simulated run, which is stated here rather than discovered later.
#
# `_ordinary_weekday` below still neutralises the blackout for every test that
# has not asked for it, so a simulated run does not turn a hundred Today tests
# red on Dec 24; what it exercises is every other date-dependent branch.
# ---------------------------------------------------------------------------
_FAKE_TODAY_ENV = "COVERAGE_FAKE_TODAY"


def _simulated_date():
    """The date `COVERAGE_FAKE_TODAY` asks for, or None."""
    raw = (os.environ.get(_FAKE_TODAY_ENV) or "").strip().lower()
    if not raw:
        return None
    if raw == "saturday":
        today = _dt.date.today()
        return today + _dt.timedelta(days=(5 - today.weekday()) % 7 or 7)
    try:
        return _dt.date.fromisoformat(raw)
    except ValueError:
        raise pytest.UsageError(
            f"{_FAKE_TODAY_ENV}={raw!r} is not an ISO date or the word "
            f"'saturday'"
        )


def pytest_report_header(config):
    """Say it in the header. A run whose clock is a lie must not look like an
    ordinary run in anyone's scrollback."""
    simulated = _simulated_date()
    if simulated:
        return (f"{_FAKE_TODAY_ENV}: running as if today were "
                f"{simulated.isoformat()} ({simulated:%A})")
    return None


@pytest.fixture(autouse=True)
def _simulated_clock():
    """No-op unless `COVERAGE_FAKE_TODAY` is set. See the block above.

    IT SHIFTS THE CLOCK, IT DOES NOT FREEZE IT. A frozen `now()` returns the
    same instant to every caller, and that is a different lie from the one
    this is trying to tell: two consecutive writes then share a timestamp, and
    "a run finished later than the one before it" becomes false. Two tests
    failed exactly that way on the first attempt (`ops/tests/test_tracking.py`
    on a strictly-increasing `finished_at`, `core/tests/test_textstyle.py` on
    a `timesince` that collapsed to zero). Returning `real now + a whole
    number of days` moves the calendar without stopping the second hand, which
    is what "run the suite as if it were December 24" actually means.
    """
    simulated = _simulated_date()
    if simulated is None:
        yield
        return
    from django.utils import timezone as dj_timezone

    real_now = dj_timezone.now
    offset = simulated - dj_timezone.localdate()
    with mock.patch("django.utils.timezone.now",
                    side_effect=lambda: real_now() + offset):
        yield


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
