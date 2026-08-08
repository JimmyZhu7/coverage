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


def test_enrich_and_extract_runs_do_not_smother_the_alarm():
    """Every refresh records an `enrich` and an `extract` row right after the
    scrape, neither carrying errors. Under the old exclude-reverify filter
    those filled the three-run window, the intersection was permanently
    empty, and this alarm could never fire — a dead alarm wearing a green
    light. Live pattern observed 2026-08-08."""
    for ago in (300, 200, 100):
        _run(["Evercore"], ago_minutes=ago)
        _run([], ago_minutes=ago - 20, connector="enrich")
        _run([], ago_minutes=ago - 40, connector="extract")
    assert health.repeat_failures() == ["Evercore"]


def test_a_scoped_run_does_not_reset_other_firms_streaks():
    """A `firm:hps` run says nothing about the other ninety boards — it must
    neither clear Evercore's failure streak nor read as 'the wall came
    down' for walled_boards."""
    _walled_run(["Evercore"], ago_minutes=300, others=["UBS"])
    _walled_run(["Evercore"], ago_minutes=200, others=["UBS"])
    _walled_run(["Evercore"], ago_minutes=100, others=["UBS"])
    _run([], ago_minutes=50, connector="firm:hps")
    assert health.repeat_failures() == ["UBS"]
    assert [w["firm"] for w in health.walled_boards()] == ["Evercore"]


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

    assert health.boards_that_never_yield() == {
        "broken": [], "walled": [], "empty": ["jefferies"]}
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

    assert health.boards_that_never_yield() == {
        "broken": ["jefferies"], "walled": [], "empty": []}
    assert "fix the config" in "\n".join(health.health_report())


# ---------------------------------------------------------------------------
# Bot-walled boards. Evercore + Jefferies live pattern, 2026-08-08: the tal.net
# tenant turned on Oleeo Protect, so every fetch "fails" by the operator's own
# design. A wall must stay visible without wearing the alarm styling that
# belongs to fixable breakage — and without reading as an empty board.
# ---------------------------------------------------------------------------

_WALL = "blocked by bot protection (Oleeo Protect) — board unreadable, not empty"


def _walled_run(firms, *, ago_minutes=0, others=()):
    errors = ([{"firm": f, "error": _WALL} for f in firms]
              + [{"firm": f, "error": "timed out"} for f in others])
    return ScrapeRun.objects.create(
        connector="all",
        started=timezone.now() - timedelta(minutes=ago_minutes),
        status="partial",
        stats={"errors": errors},
    )


def test_a_walled_board_does_not_trip_the_repeat_failure_alarm():
    """The wall fails every run by definition; letting it ring the stale-data
    bell daily trains the reader to ignore the bell."""
    _walled_run(["Evercore"], ago_minutes=180)
    _walled_run(["Evercore"], ago_minutes=120)
    _walled_run(["Evercore"], ago_minutes=60)
    assert health.repeat_failures() == []


def test_a_real_failure_still_rings_while_another_board_is_walled():
    _walled_run(["Evercore"], ago_minutes=180, others=["UBS"])
    _walled_run(["Evercore"], ago_minutes=120, others=["UBS"])
    _walled_run(["Evercore"], ago_minutes=60, others=["UBS"])
    assert health.repeat_failures() == ["UBS"]


def test_a_wall_is_dated_from_the_start_of_its_streak():
    firm = Firm.objects.create(slug="evercore", name="Evercore")
    for i in range(3):
        Opportunity.objects.create(firm=firm, url=f"https://x/{i}", title="SA",
                                   bucket="internship", status="open")
    _run(["Evercore"], ago_minutes=300)          # ordinary failure — not the streak
    first = _walled_run(["Evercore"], ago_minutes=240)
    _walled_run(["Evercore"], ago_minutes=120)
    _walled_run(["Evercore"], ago_minutes=60)

    walled = health.walled_boards()
    assert [w["firm"] for w in walled] == ["Evercore"]
    assert walled[0]["since"] == first.started
    assert walled[0]["is_new"] is False
    assert walled[0]["open_rows"] == 3


def test_a_new_wall_warns_once_then_becomes_a_standing_line():
    """⚠ on the streak's first run (the daily pipeline notifies on ⚠), then
    the quiet ·-line every run after."""
    _walled_run(["Jefferies"], ago_minutes=60)
    assert health.walled_boards()[0]["is_new"] is True
    report = "\n".join(health.health_report())
    assert "⚠ bot-walled" in report

    _walled_run(["Jefferies"], ago_minutes=0)
    assert health.walled_boards()[0]["is_new"] is False
    report = "\n".join(health.health_report())
    assert "· bot-walled" in report and "⚠ bot-walled" not in report


def test_a_never_yielding_walled_board_is_not_called_empty_or_broken(monkeypatch):
    """The exact lie this category exists to kill: jefferies.tal.net sat in
    'empty — plausible market fact' for weeks while Oleeo Protect was eating
    the page. Unreadable is not empty, and it is not a config bug either."""
    Firm.objects.create(slug="jefferies", name="Jefferies")
    monkeypatch.setattr(health, "BOARDS", [("jefferies", object())])
    _walled_run(["Jefferies"], ago_minutes=60)

    assert health.boards_that_never_yield() == {
        "broken": [], "walled": ["jefferies"], "empty": []}
    report = "\n".join(health.health_report())
    assert "bot-walled" in report
    assert "fix the config" not in report
    assert "market fact" not in report


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
