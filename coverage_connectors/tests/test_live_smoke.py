"""Live smoke tests — one real network fetch per provider.

Skipped by default; set RUN_LIVE=1 to actually hit the network:

    RUN_LIVE=1 uv run pytest tests/test_live_smoke.py -v -m live

Board identifiers:

- Greenhouse: "williamblair" and "tpgcareers" — both taken directly from
  the original codebase's own `_ATS_BOARDS` config (`sources.py`).
  williamblair's board points `absolute_url` at the firm's own custom
  domain (a documented verify() limitation, see greenhouse.py); tpgcareers
  uses the vanilla job-boards.greenhouse.io host, so it's used for the
  fetch->verify round trip instead.
- Workday: "citi.wd5" / "Citi_Early_Careers_Events_Site" — taken directly
  from the original's own `_ATS_BOARDS` config.
- Lever: "palantir" — the original's `_ATS_BOARDS` never actually wired up
  a live Lever board (the normalize/verify code existed but was dead), so
  there is no "the original's own Lever org" to reuse. This is a real,
  currently-active public Lever board found by probing a shortlist of
  known Lever-using companies for one that returns real postings (most
  candidates tried returned 404 or an empty board — see the extraction
  report for the full probe list).

These are a handful of read-only GET/POST calls against public,
unauthenticated APIs, run once per test session at most — not a load test.
"""

from __future__ import annotations

import os

import pytest

from coverage_connectors import GreenhouseBoard, LeverBoard, WorkdayBoard, fetch, verify

RUN_LIVE = os.environ.get("RUN_LIVE") == "1"
pytestmark = pytest.mark.skipif(not RUN_LIVE, reason="set RUN_LIVE=1 to run live network smoke tests")


@pytest.mark.live
def test_greenhouse_live_fetch():
    board = GreenhouseBoard(firm="William Blair", token="williamblair")
    result = fetch(board)

    assert result.ok, f"live greenhouse fetch failed: {result.error}"
    assert result.raw_count > 0
    assert len(result.opportunities) == result.raw_count
    opp = result.opportunities[0]
    assert opp.source == "greenhouse"
    assert opp.title
    assert opp.url.startswith("https://www.williamblair.com/")


@pytest.mark.live
def test_lever_live_fetch():
    board = LeverBoard(firm="Palantir", org="palantir")
    result = fetch(board)

    assert result.ok, f"live lever fetch failed: {result.error}"
    assert result.raw_count > 0
    opp = result.opportunities[0]
    assert opp.source == "lever"
    assert opp.title
    assert opp.url.startswith("https://jobs.lever.co/palantir/")


@pytest.mark.live
def test_workday_live_fetch():
    board = WorkdayBoard(firm="Citi", tenant_host="citi.wd5", site="Citi_Early_Careers_Events_Site")
    result = fetch(board)

    assert result.ok, f"live workday fetch failed: {result.error}"
    assert result.raw_count > 0
    opp = result.opportunities[0]
    assert opp.source == "workday"
    assert opp.title
    assert opp.url.startswith("https://citi.wd5.myworkdayjobs.com/Citi_Early_Careers_Events_Site/")


@pytest.mark.live
def test_greenhouse_live_verify_open():
    # tpgcareers, not williamblair: williamblair's absolute_url points at
    # the firm's own custom domain, which verify() cannot classify by
    # design (see greenhouse.py's module docstring on this known
    # limitation, ported unchanged from the original).
    board = GreenhouseBoard(firm="TPG", token="tpgcareers")
    fetched = fetch(board)
    assert fetched.ok and fetched.opportunities

    status = verify(fetched.opportunities[0].url)
    assert status.result == "verified-open", status.evidence


@pytest.mark.live
def test_greenhouse_live_verify_needs_verification_on_custom_domain():
    board = GreenhouseBoard(firm="William Blair", token="williamblair")
    fetched = fetch(board)
    assert fetched.ok and fetched.opportunities

    status = verify(fetched.opportunities[0].url)
    assert status.result == "needs-verification", status.evidence


@pytest.mark.live
def test_workday_live_verify_open():
    board = WorkdayBoard(firm="Citi", tenant_host="citi.wd5", site="Citi_Early_Careers_Events_Site")
    fetched = fetch(board)
    assert fetched.ok and fetched.opportunities

    status = verify(fetched.opportunities[0].url)
    assert status.result == "verified-open", status.evidence
