"""The scrape-health checks — the failures that used to happen in silence."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from directory import health
from directory.models import Firm, Opportunity, ScrapeRun

pytestmark = pytest.mark.django_db


def _run(errors, *, ago_minutes=0, connector="all"):
    return ScrapeRun.objects.create(
        connector=connector,
        started=timezone.now() - timedelta(minutes=ago_minutes),
        status="partial" if errors else "ok",
        stats={"errors": [{"firm": f, "error": "timed out"} for f in errors]},
    )


def test_a_firm_failing_every_recent_run_is_named():
    """Evercore's live pattern: 5 timeouts in 14 runs, invisible in a JSON
    blob. Three consecutive is the report's bar for 'down, not unlucky'."""
    _run(["Evercore", "UBS"], ago_minutes=180)
    _run(["Evercore"], ago_minutes=120)
    _run(["Evercore", "Nomura"], ago_minutes=60)
    assert health.repeat_failures() == ["Evercore"]


def test_an_intermittent_failure_is_not_named():
    """Failing once is bad luck; the report is for patterns."""
    _run(["UBS"], ago_minutes=180)
    _run([], ago_minutes=120)
    _run(["UBS"], ago_minutes=60)
    assert health.repeat_failures() == []


def test_too_few_runs_means_no_verdict():
    """Two runs of history can't establish a three-run pattern — better no
    answer than a premature one."""
    _run(["Evercore"], ago_minutes=120)
    _run(["Evercore"], ago_minutes=60)
    assert health.repeat_failures() == []


def test_reverify_runs_do_not_count():
    """reverify records its own ScrapeRun rows with different stats; mixing
    them in would dilute three scrape failures with liveness passes."""
    _run(["Evercore"], ago_minutes=200)
    _run(["Evercore"], ago_minutes=150)
    _run([], ago_minutes=100, connector="reverify")
    _run(["Evercore"], ago_minutes=50)
    assert health.repeat_failures() == ["Evercore"]


def test_a_never_yielding_board_that_fetches_cleanly_is_empty_not_broken(monkeypatch):
    """Jefferies' actual live state, hand-verified 2026-08-05: the board page
    serves fine and simply lists nothing. The report used to call every
    never-yielding board "a config bug, not a market fact" — for both of its
    real hits it was the market fact."""
    yielding = Firm.objects.create(slug="evercore", name="Evercore")
    Firm.objects.create(slug="jefferies", name="Jefferies")
    Opportunity.objects.create(firm=yielding, url="https://x/1", title="SA",
                               bucket="internship", status="open")
    monkeypatch.setattr(health, "BOARDS", [("evercore", object()), ("jefferies", object())])
    _run([], ago_minutes=60)   # latest run fetched everything cleanly

    assert health.boards_that_never_yield() == {"broken": [], "empty": ["jefferies"]}
    report = "\n".join(health.health_report())
    assert "fetches cleanly" in report
    assert "config bug" not in report


def test_a_never_yielding_board_that_also_errors_is_broken(monkeypatch):
    """The other half: zero rows ever AND the latest run recorded a fetch
    error for the firm — that one really is a config problem and keeps the
    loud warning."""
    Firm.objects.create(slug="jefferies", name="Jefferies")
    monkeypatch.setattr(health, "BOARDS", [("jefferies", object())])
    _run(["Jefferies"], ago_minutes=60)

    assert health.boards_that_never_yield() == {"broken": ["jefferies"], "empty": []}
    assert "fix the config" in "\n".join(health.health_report())


def test_the_report_is_empty_when_healthy(monkeypatch):
    firm = Firm.objects.create(slug="evercore", name="Evercore")
    Opportunity.objects.create(firm=firm, url="https://x/1", title="SA",
                               bucket="internship", status="open")
    monkeypatch.setattr(health, "BOARDS", [("evercore", object())])
    _run([], ago_minutes=120)
    _run([], ago_minutes=60)
    _run([], ago_minutes=30)
    assert health.health_report() == []
