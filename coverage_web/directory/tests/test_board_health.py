"""Health reporting keyed on the BOARD, not the firm.

Every check in `health.py` used to key on the firm, and the catalog holds 127
boards under 110 slugs. Thirteen firms therefore had a board whose verdict was
averaged away behind a sibling's, and the audit found all three ways that
goes wrong on the live board at once:

- Moelis and Perella Weinberg each gained two tal.net campus boards on
  2026-09-01. Both firms already had a producing Workday board, so both new
  boards could fetch zero every night and appear nowhere.
- Sixth Street's Greenhouse token started 404ing two runs ago holding 20 open
  rows, and `repeat_failures` needs three consecutive runs before it speaks.
- Marshall Wace's board has answered `{"jobs":[]}` since its single row closed
  in August. Nothing is open, so no guard fires; nothing was ever said.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from coverage_connectors import GreenhouseBoard, TalnetBoard, WorkdayBoard

from directory import health
from directory.boards import BOARDS, board_key
from directory.models import Firm, Opportunity, ScrapeRun

pytestmark = pytest.mark.django_db


MOELIS_JOBS = TalnetBoard(
    firm="Moelis",
    board_url="https://moelis-careers.tal.net/vx/mobile-0/candidate/jobboard/vacancy/1/adv/")
MOELIS_EVENTS = TalnetBoard(
    firm="Moelis",
    board_url="https://moelis-careers.tal.net/vx/mobile-0/candidate/jobboard/vacancy/2/adv/")


def _firm(slug, name):
    return Firm.objects.create(slug=slug, name=name)


def _row(firm, source, url, *, status="open", bucket="internship"):
    return Opportunity.objects.create(firm=firm, url=url, title="2027 Summer Analyst",
                                      bucket=bucket, status=status, source=source)


def _run(boards, *, errors=(), ago_minutes=0):
    """A ScrapeRun carrying the per-board lines ingest now records."""
    return ScrapeRun.objects.create(
        connector="all",
        started=timezone.now() - timedelta(minutes=ago_minutes),
        status="ok",
        stats={"boards": list(boards), "errors": list(errors)},
    )


def _entry(firm, provider, board, *, ok=True, rows=0, error=""):
    return {"firm": firm, "slug": "", "provider": provider,
            "board": board_key(board), "ok": ok, "rows": rows,
            "empty_state": False, "truncated": False, "error": error}


# --------------------------------------------------------------- board keys

def test_every_catalog_board_has_a_unique_key():
    """The key is what joins a run's per-board line back to the catalog
    entry. Two boards sharing one would silently merge two verdicts — the
    very failure this module exists to end. 127 boards, 110 slugs."""
    keys = [(slug, board_key(board)) for slug, board in BOARDS]
    assert len(set(keys)) == len(keys), "two catalog boards share a key"


def test_a_key_is_readable_and_keeps_the_distinguishing_tail():
    """tal.net URLs run past 110 characters of shared prefix; the half that
    says which board it is sits at the end."""
    assert board_key(MOELIS_JOBS) == "moelis-careers.tal.net/…/vacancy/1/adv"
    assert board_key(MOELIS_EVENTS) == "moelis-careers.tal.net/…/vacancy/2/adv"
    assert board_key(GreenhouseBoard(firm="Sixth Street", token="sixthstreet")) == "sixthstreet"


# ------------------------------------------------------------ the two-board firm

def test_a_second_board_returning_zero_is_visible_behind_a_producing_one(monkeypatch):
    """THE MOELIS CASE. One firm, two providers, and the tal.net board's zero
    used to be invisible behind the Workday board's rows."""
    moelis = _firm("moelis", "Moelis")
    _row(moelis, "workday", "https://moelis.wd/1")
    workday = WorkdayBoard(firm="Moelis", tenant_host="moelis.wd1",
                           site="Experienced-Hires")
    monkeypatch.setattr(health, "BOARDS", [
        ("moelis", workday), ("moelis", MOELIS_JOBS), ("moelis", MOELIS_EVENTS)])
    _run([
        _entry("Moelis", "workday", workday, rows=37),
        _entry("Moelis", "talnet", MOELIS_JOBS, rows=0),
        _entry("Moelis", "talnet", MOELIS_EVENTS, rows=0),
    ])

    by_board = {r["board"]: r for r in health.board_health()}
    assert by_board[board_key(workday)]["state"] == "ok"
    assert by_board[board_key(MOELIS_JOBS)]["state"] == "empty"
    assert by_board[board_key(MOELIS_EVENTS)]["state"] == "empty"


def test_a_failing_board_is_named_in_the_run_it_fails(monkeypatch):
    """THE SIXTH STREET CASE. 20 open rows behind a token that started 404ing,
    and `repeat_failures` would not have said so for another run."""
    firm = _firm("sixthstreet", "Sixth Street")
    board = GreenhouseBoard(firm="Sixth Street", token="sixthstreet")
    for i in range(20):
        _row(firm, "greenhouse", f"https://boards.greenhouse.io/sixthstreet/jobs/{i}")
    monkeypatch.setattr(health, "BOARDS", [("sixthstreet", board)])
    _run([_entry("Sixth Street", "greenhouse", board, ok=False,
                 error="HTTP Error 404: Not Found")])

    row = health.board_health()[0]
    assert row["state"] == "failed" and row["open_rows"] == 20
    line = next(l for l in health.health_report() if "boards failing" in l)
    assert "sixthstreet/sixthstreet" in line and "20 open rows" in line
    assert line.startswith("⚠")


def test_produced_before_and_zero_now_is_reported(monkeypatch):
    """THE MARSHALL WACE CASE, which the audit calls the wipe guard reading as
    healthy. Its one row closed on 2026-08-11, so there is nothing open for a
    guard to refuse to close, and the board has answered `{"jobs":[]}` ever
    since with no error and no line."""
    firm = _firm("marshallwace", "Marshall Wace")
    board = GreenhouseBoard(firm="Marshall Wace", token="marshallwace")
    _row(firm, "greenhouse", "https://boards.greenhouse.io/marshallwace/jobs/1",
         status="closed")
    monkeypatch.setattr(health, "BOARDS", [("marshallwace", board)])
    _run([_entry("Marshall Wace", "greenhouse", board, rows=0)])

    row = health.board_health()[0]
    assert row["state"] == "silent" and row["ever"] == 1 and row["open_rows"] == 0
    line = next(l for l in health.health_report() if "produced rows before" in l)
    assert "marshallwace/marshallwace" in line


def test_a_board_that_wipes_while_rows_are_open_is_an_alarm(monkeypatch):
    firm = _firm("hps", "HPS")
    board = GreenhouseBoard(firm="HPS", token="hps")
    _row(firm, "greenhouse", "https://boards.greenhouse.io/hps/jobs/1")
    monkeypatch.setattr(health, "BOARDS", [("hps", board)])
    _run([_entry("HPS", "greenhouse", board, rows=0)])

    assert health.board_health()[0]["state"] == "wiped"
    line = next(l for l in health.health_report() if "returned zero while the firm" in l)
    assert line.startswith("⚠") and "hps/hps" in line


def test_a_board_that_never_produced_is_quiet_not_alarming(monkeypatch):
    """Jefferies' Insight Days board is registered empty on purpose — its
    programmes post in November. An alarm every night on a board doing exactly
    what was expected is how a report stops being read."""
    _firm("jefferies", "Jefferies")
    board = TalnetBoard(
        firm="Jefferies",
        board_url="https://jefferies.tal.net/vx/mobile-0/candidate/jobboard/vacancy/2/adv/")
    monkeypatch.setattr(health, "BOARDS", [("jefferies", board)])
    _run([_entry("Jefferies", "talnet", board, rows=0)])

    assert health.board_health()[0]["state"] == "empty"
    assert not [l for l in health.health_report() if l.startswith("⚠")]


def test_a_wall_is_not_filed_as_a_fixable_failure(monkeypatch):
    """Same rule the firm-level report already keeps: the operator put the
    wall up, nothing here takes it down, and `walled_boards()` says it once."""
    firm = _firm("nomura", "Nomura")
    board = TalnetBoard(
        firm="Nomura",
        board_url="https://nomuracampus.tal.net/vx/mobile-0/candidate/jobboard/vacancy/1/adv/")
    _row(firm, "talnet", "https://nomuracampus.tal.net/x/1")
    monkeypatch.setattr(health, "BOARDS", [("nomura", board)])
    _run([_entry("Nomura", "talnet", board, ok=False,
                 error="blocked by bot protection (Oleeo Protect) — board unreadable, not empty")])

    assert health.board_health()[0]["state"] == "walled"
    assert not [l for l in health.health_report() if "boards failing" in l]


# ------------------------------------------------------- historical runs

def test_a_run_recorded_before_per_board_stats_falls_back_to_the_firm(monkeypatch):
    """Every ScrapeRun already in the table predates `stats["boards"]`. An
    honest "one of this firm's boards on this provider failed" beats printing
    nothing at all until the next full run lands, and the row says which
    attribution it used."""
    firm = _firm("ey", "EY")
    board = GreenhouseBoard(firm="EY", token="ey")
    _row(firm, "greenhouse", "https://boards.greenhouse.io/ey/jobs/1")
    monkeypatch.setattr(health, "BOARDS", [("ey", board)])
    ScrapeRun.objects.create(
        connector="all", started=timezone.now(), status="partial",
        stats={"errors": [{"firm": "EY", "provider": "greenhouse",
                           "error": "SSL: CERTIFICATE_VERIFY_FAILED"}]})

    row = health.board_health()[0]
    assert row["attribution"] == "firm"
    assert row["state"] == "failed" and "CERTIFICATE_VERIFY_FAILED" in row["error"]


def test_a_historical_wipe_guard_reads_as_a_wipe_not_a_generic_failure(monkeypatch):
    """The old runs already have a name for "fetched clean, returned zero,
    rows still open" — the wipe guard's own message. A historical run should
    classify the same way the next one will."""
    firm = _firm("hps", "HPS")
    board = GreenhouseBoard(firm="HPS", token="hps")
    _row(firm, "greenhouse", "https://boards.greenhouse.io/hps/jobs/1")
    monkeypatch.setattr(health, "BOARDS", [("hps", board)])
    ScrapeRun.objects.create(
        connector="all", started=timezone.now(), status="partial",
        stats={"errors": [{"firm": "HPS", "provider": "greenhouse",
                           "error": "fetch ok but 0 rows while postings are open; "
                                    "skipped auto-close (suspected shape change)"}]})

    assert health.board_health()[0]["state"] == "wiped"


def test_no_full_run_yet_reports_nothing_rather_than_guessing():
    assert health.board_health() == []
    assert health.board_health_table() == "no full scrape recorded yet"


# --------------------------------------------------- no campus board marker

def test_a_firm_whose_registered_board_is_experienced_hire_is_named():
    """689 rows across eight firms, none of them ever campus. The rows are
    real; they are simply all for people who already have careers, and the
    firm page said nothing about it."""
    firm = _firm("ares", "Ares")
    for i in range(3):
        _row(firm, "workday", f"https://ares.wd/{i}", bucket="other")

    out = health.firms_without_campus_board()
    assert [f["slug"] for f in out if f["slug"] == "ares"] == ["ares"]
    assert next(f for f in out if f["slug"] == "ares")["open_rows"] == 3
    line = next(l for l in health.health_report() if "no campus board registered" in l)
    assert "ares" in line and line.startswith("·")


def test_the_marker_stops_reporting_once_a_campus_row_appears():
    """The marker states a fact about the catalog, and the fact is checked
    against the database every run. A firm that starts producing campus rows
    drops off with no edit — a stale "no campus board" would be its own quiet
    lie."""
    firm = _firm("ares", "Ares")
    _row(firm, "workday", "https://ares.wd/1", bucket="internship")

    assert [f["slug"] for f in health.firms_without_campus_board()] == []


def test_moelis_and_pwp_are_not_marked():
    """Both got tal.net student boards on 2026-09-01, so the sentence this
    marker prints — "no campus board is registered" — is not true of them any
    more. Their boards returning zero today is the per-board table's business,
    not this one's."""
    from directory.boards import NO_CAMPUS_BOARD

    assert "moelis" not in NO_CAMPUS_BOARD
    assert "pwp" not in NO_CAMPUS_BOARD


def test_every_marked_slug_exists_in_the_catalog():
    """A typo here would silently mark nothing at all."""
    from directory.boards import NO_CAMPUS_BOARD

    catalog = {slug for slug, _ in BOARDS}
    assert set(NO_CAMPUS_BOARD) <= catalog
