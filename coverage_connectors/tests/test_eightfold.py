"""Offline unit tests for the Eightfold connector: normalization against a
synthetic /api/apply/v2/jobs response, plus a pagination test that exercises
the advance-by-actual-count contract. Eightfold silently caps a page below
the requested `num`, so the connector must step `start` by what came back,
not a fixed stride, or it skips rows. No network; `fetch_json`/`fetch_text`
are monkeypatched."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from coverage_connectors import eightfold
from coverage_connectors.models import EightfoldBoard

BOARD = EightfoldBoard(firm="Millennium", host="career.mlp.com", domain="mlp.com")


def _pos(i: int) -> dict:
    return {
        "name": f"Role {i}",
        "location": "New York, New York, United States of America",
        "canonicalPositionUrl": f"https://mlp.eightfold.ai/careers/job/{1000 + i}",
        "t_update": 1784505600,
        "display_job_id": f"REQ-{i}",
    }


def test_fetch_normalizes(monkeypatch):
    resp = {"count": 2, "positions": [_pos(0), _pos(1)]}
    monkeypatch.setattr(eightfold, "fetch_json", lambda url, **kw: resp)

    result = eightfold.fetch(BOARD)

    assert result.ok and result.error is None
    assert result.raw_count == 2
    opp = result.opportunities[0]
    assert opp.firm == "Millennium"
    assert opp.source == "eightfold"
    assert opp.title == "Role 0"
    assert opp.location.startswith("New York")
    assert opp.url == "https://mlp.eightfold.ai/careers/job/1000"
    assert opp.posted_at and opp.posted_at.startswith("20")  # ISO date off t_update
    assert opp.deadline is None  # Eightfold's listing exposes no deadline field


def test_location_falls_back_to_locations_list(monkeypatch):
    pos = {"name": "Trader", "location": "", "locations": ["London, UK"],
           "canonicalPositionUrl": "https://mlp.eightfold.ai/careers/job/9"}
    monkeypatch.setattr(eightfold, "fetch_json", lambda url, **kw: {"count": 1, "positions": [pos]})
    opp = eightfold.fetch(BOARD).opportunities[0]
    assert opp.location == "London, UK"


def test_pagination_advances_by_returned_count(monkeypatch):
    """25 total, but the API caps each page at 10 regardless of the requested
    `num`. The connector must step `start` by the actual page size (10), not
    the requested stride, and collect all 25 with no skips or dupes."""
    total = 25
    all_pos = [_pos(i) for i in range(total)]
    seen_starts: list[int] = []

    def fake_fetch_json(url, **kw):
        start = int(parse_qs(urlparse(url).query)["start"][0])
        seen_starts.append(start)
        return {"count": total, "positions": all_pos[start:start + 10]}  # cap at 10

    monkeypatch.setattr(eightfold, "fetch_json", fake_fetch_json)
    result = eightfold.fetch(BOARD)

    assert result.raw_count == total
    assert seen_starts == [0, 10, 20]  # stepped by 10, not the requested 50
    assert [o.title for o in result.opportunities] == [f"Role {i}" for i in range(total)]


def test_fetch_stops_on_empty_page(monkeypatch):
    """A count that overshoots the real data must not loop forever — an empty
    page ends the walk."""
    def fake_fetch_json(url, **kw):
        start = int(parse_qs(urlparse(url).query)["start"][0])
        return {"count": 999, "positions": [_pos(0)] if start == 0 else []}

    monkeypatch.setattr(eightfold, "fetch_json", fake_fetch_json)
    result = eightfold.fetch(BOARD)
    assert result.raw_count == 1


def test_fetch_reports_error(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(eightfold, "fetch_json", boom)
    result = eightfold.fetch(BOARD)
    assert not result.ok
    assert result.opportunities == []
    assert "network down" in result.error


def test_classify_url():
    url = "https://mlp.eightfold.ai/careers/job/755957515806"
    assert eightfold.classify_url(url) == {"url": url}
    assert eightfold.classify_url("https://boards.greenhouse.io/x/jobs/1") is None
    assert eightfold.classify_url("") is None


def test_verify(monkeypatch):
    monkeypatch.setattr(eightfold, "fetch_text", lambda url, **kw: "<html>live</html>")
    v = eightfold.verify("https://mlp.eightfold.ai/careers/job/1")
    assert v.result == "verified-open"

    v2 = eightfold.verify("https://example.com/not-a-job")
    assert v2.result == "needs-verification"

    def boom(url, **kw):
        raise RuntimeError("timeout")

    monkeypatch.setattr(eightfold, "fetch_text", boom)
    v3 = eightfold.verify("https://mlp.eightfold.ai/careers/job/1")
    assert v3.result == "unreachable"


# ---------------------------------------------------------------------------
# Truncation. Every capped connector has to distinguish "the source said it
# was done" from "I hit my own limit", because ingest closes any stored row a
# successful fetch did not return. Workday's cap silently marked hundreds of
# live roles closed before this contract existed; these tests keep the same
# bug from reaching the other capped connectors.
# ---------------------------------------------------------------------------


def test_a_complete_walk_is_not_truncated(monkeypatch):
    """The API's own `count` is reached, so the board was read whole."""
    resp = {"count": 2, "positions": [_pos(0), _pos(1)]}
    monkeypatch.setattr(eightfold, "fetch_json", lambda url, **kw: resp)
    assert eightfold.fetch(BOARD).truncated is False


def test_hitting_the_cap_reports_truncated(monkeypatch):
    """A board that never runs out inside `_MAX` positions must say so."""
    monkeypatch.setattr(eightfold, "_MAX", 40)
    monkeypatch.setattr(eightfold, "_PAGE", 20)

    def _page(url, **kw):
        # Always a full page and a count far beyond the cap: the feed never
        # ends, so only the cap can stop the walk.
        start = int(parse_qs(urlparse(url).query).get("start", ["0"])[0])
        return {"count": 10_000, "positions": [_pos(start + i) for i in range(20)]}

    monkeypatch.setattr(eightfold, "fetch_json", _page)
    result = eightfold.fetch(BOARD)
    assert result.ok is True
    assert result.truncated is True
    assert len(result.opportunities) == 40
