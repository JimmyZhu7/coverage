"""Live smoke tests — one real network fetch per provider.

Skipped by default; set RUN_LIVE=1 to actually hit the network:

    RUN_LIVE=1 uv run pytest tests/test_live_smoke.py -v -m live

Board identifiers:

- Greenhouse: "williamblair" and "tpgcareers" — both taken directly from
  the original codebase's own `_ATS_BOARDS` config (`sources.py`).
  williamblair's board points `absolute_url` at the firm's own custom
  domain; `classify_url()` resolves it via the known `_CUSTOM_DOMAIN_TOKENS`
  mapping (see greenhouse.py), so it's used for the fetch->verify round
  trip against a known custom domain, while tpgcareers uses the vanilla
  job-boards.greenhouse.io host and covers the plain round trip instead.
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

from coverage_connectors import (
    AvatureBoard, GreenhouseBoard, LeverBoard, SuccessFactorsBoard, WorkdayBoard, fetch, verify,
)

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
    # tpgcareers, not williamblair: this covers the plain
    # job-boards.greenhouse.io round trip. The known-custom-domain round
    # trip (williamblair) is covered separately below.
    board = GreenhouseBoard(firm="TPG", token="tpgcareers")
    fetched = fetch(board)
    assert fetched.ok and fetched.opportunities

    status = verify(fetched.opportunities[0].url)
    assert status.result == "verified-open", status.evidence


@pytest.mark.live
def test_greenhouse_live_verify_open_on_known_custom_domain():
    # williamblair's absolute_url points at the firm's own custom domain
    # (www.williamblair.com?gh_jid=<id>), not job-boards.greenhouse.io.
    # classify_url() resolves it via the known `_CUSTOM_DOMAIN_TOKENS`
    # mapping (see greenhouse.py), so this verifies live same as any other
    # recognized board -- it's a known custom domain, not an unlisted one.
    # (Unlisted custom domains, which classify_url() cannot resolve and
    # verify() reports as "needs-verification", are covered by the offline
    # test_verify_needs_verification_on_unlisted_custom_domain_url in
    # test_greenhouse.py -- that's pure regex/dict-lookup logic with no
    # live board to hit.)
    board = GreenhouseBoard(firm="William Blair", token="williamblair")
    fetched = fetch(board)
    assert fetched.ok and fetched.opportunities

    status = verify(fetched.opportunities[0].url)
    assert status.result == "verified-open", status.evidence


@pytest.mark.live
def test_workday_live_verify_open():
    board = WorkdayBoard(firm="Citi", tenant_host="citi.wd5", site="Citi_Early_Careers_Events_Site")
    fetched = fetch(board)
    assert fetched.ok and fetched.opportunities

    status = verify(fetched.opportunities[0].url)
    assert status.result == "verified-open", status.evidence


# --------------------------------------------------------------------------
# Boards added 2026-08-19 (Haitong / Accenture / Deloitte / EY). Each one is
# smoke-tested here against the real site, because this catalog's standing
# rule is that a board earns its place by returning real rows — see
# directory/boards.py's own provenance note.
# --------------------------------------------------------------------------

@pytest.mark.live
def test_haitong_workday_live_fetch():
    """Haitong International's HK/NY board is small (single digits off
    season), so this asserts the board is READABLE, not that it is busy —
    an empty board and an unreachable one must not look the same."""
    board = WorkdayBoard(firm="Haitong International", tenant_host="htisec.wd3",
                         site="hti_careers")
    result = fetch(board)

    assert result.ok, f"live haitong fetch failed: {result.error}"
    for opp in result.opportunities:
        assert opp.source == "workday"
        assert opp.title
        assert opp.url.startswith("https://htisec.wd3.myworkdayjobs.com/hti_careers/")


@pytest.mark.live
def test_accenture_workday_search_text_scopes_the_board():
    """Accenture's tenant reports a hard ceiling of total=2000 for any
    search broad enough to reach it — INCLUDING the unfiltered board — and a
    fetch that reads exactly `total` rows reports truncated=False, so a
    board scoped past the ceiling would look complete while being a
    truncation. The catalog's keywords must stay under it."""
    board = WorkdayBoard(firm="Accenture", tenant_host="accenture.wd103",
                         site="AccentureCareers", search_text="internship")
    result = fetch(board)

    assert result.ok, f"live accenture fetch failed: {result.error}"
    assert 0 < result.raw_count < 2000, (
        f"raw_count={result.raw_count} is at or past the tenant's 2000 ceiling — "
        "re-pick the search_text rather than shipping a silent truncation"
    )
    assert any("intern" in o.title.lower() for o in result.opportunities)


@pytest.mark.live
def test_deloitte_avature_entry_level_facet_is_honoured():
    """The `3_5_3` facet is what makes this board worth having: unfiltered,
    the feed's 20 most-recent rows are all experienced-hire reqs. The
    en_US path honours it; the bare /careers/ path silently ignores every
    query param (verified live 2026-08-19)."""
    board = AvatureBoard(
        firm="Deloitte",
        feed_url="https://apply.deloitte.com/en_US/careers/SearchJobs/feed/?3_5_3=477%2C478%2C480",
    )
    result = fetch(board)

    assert result.ok, f"live deloitte fetch failed: {result.error}"
    assert result.raw_count > 0
    opp = result.opportunities[0]
    assert opp.source == "avature"
    assert opp.url.startswith("https://apply.deloitte.com/")


@pytest.mark.live
def test_successfactors_live_fetch_and_verify():
    board = SuccessFactorsBoard(firm="EY", origin="https://careers.ey.com",
                                keywords=("internship",))
    result = fetch(board)

    assert result.ok, f"live successfactors fetch failed: {result.error}"
    assert result.raw_count > 0
    opp = result.opportunities[0]
    assert opp.source == "successfactors"
    assert opp.title and opp.location
    assert opp.url.startswith("https://careers.ey.com/ey/job/")

    status = verify(opp.url)
    assert status.result == "verified-open", status.evidence


@pytest.mark.live
def test_successfactors_live_fetch_janus_henderson():
    """A different RMK tenant than EY's — also confirms fetch() doesn't
    depend on the page size being 25 (this tenant's is 25 too, but GIC's
    below is 20, and both must walk correctly)."""
    board = SuccessFactorsBoard(firm="Janus Henderson", origin="https://jobs.janushenderson.com",
                                keywords=("internship", "graduate"))
    result = fetch(board)

    assert result.ok, f"live successfactors fetch failed: {result.error}"
    assert result.raw_count > 0
    opp = result.opportunities[0]
    assert opp.source == "successfactors"
    assert opp.title and opp.location
    assert opp.url.startswith("https://jobs.janushenderson.com/")


@pytest.mark.live
def test_successfactors_live_fetch_gic_20_row_page_size():
    """GIC's tenant renders 20 rows per page, not EY's 25 — the live case
    that caught fetch() hardcoding _PAGE_SIZE=25 for the startrow walk."""
    board = SuccessFactorsBoard(firm="GIC", origin="https://careers.gic.com.sg",
                                keywords=("associate", "analyst"))
    result = fetch(board)

    assert result.ok, f"live successfactors fetch failed: {result.error}"
    assert result.raw_count > 20, "walk must not stop after the first 20-row page"
    opp = result.opportunities[0]
    assert opp.source == "successfactors"
    assert opp.title and opp.location
    assert opp.url.startswith("https://careers.gic.com.sg/")
