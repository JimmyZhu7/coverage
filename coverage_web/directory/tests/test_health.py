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


def _enrich_run(*, ago_days=0, queued=50, fetched=48, unreachable=2):
    return ScrapeRun.objects.create(
        connector="enrich",
        started=timezone.now() - timedelta(days=ago_days),
        finished=timezone.now() - timedelta(days=ago_days),
        status="ok",
        stats={"queued": queued, "fetched": fetched, "unreachable": unreachable},
    )


def test_the_report_is_empty_when_healthy(monkeypatch):
    firm = Firm.objects.create(slug="evercore", name="Evercore")
    Opportunity.objects.create(firm=firm, url="https://x/1", title="SA",
                               bucket="internship", status="open")
    monkeypatch.setattr(health, "BOARDS", [("evercore", object())])
    _run([], ago_minutes=120)
    _run([], ago_minutes=60)
    _run([], ago_minutes=30)
    _enrich_run()
    assert health.health_report() == []


# ---------------------------------------------------------------------------
# The detail-page pass. It earns its own checks because its failure is
# invisible in every other signal: the scrape still succeeds, the counts still
# look right, the roles still list — what quietly stops is the deadlines. An
# entire enrichment run was once lost to an overnight scrape and nothing
# anywhere went red.
# ---------------------------------------------------------------------------

def test_a_board_that_has_never_enriched_says_so():
    assert any("never run" in line for line in health.enrichment_health())


def test_a_silent_enrichment_pass_is_reported():
    _enrich_run(ago_days=5)
    assert any("has not run in 5 days" in line for line in health.enrichment_health())


def test_a_recent_enrichment_pass_is_quiet():
    _enrich_run(ago_days=1)
    assert health.enrichment_health() == []


def test_a_pass_that_read_nothing_it_queued_is_not_a_healthy_pass():
    """The failure mode that exits zero and looks green: a run that queues
    hundreds of pages, is blocked on every one, writes nothing, and reports
    "0 pages read"."""
    _enrich_run(queued=300, fetched=0, unreachable=300)
    assert any("read none" in line for line in health.enrichment_health())


def test_a_pass_failing_more_than_it_reads_is_reported():
    _enrich_run(queued=100, fetched=20, unreachable=80)
    assert any("unreachable" in line for line in health.enrichment_health())
