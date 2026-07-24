"""Offline unit tests for the Lever connector: normalization/parsing against
a captured real response (tests/fixtures/lever_postings.json — saved from a
live fetch of api.lever.co/v0/postings/palantir on 2026-07-23; no firm in
the original codebase's board list ever wired up a live Lever board, so
this is the first live exercise of this normalize/verify logic). No network
access; `coverage_connectors.http` is monkeypatched."""

from __future__ import annotations

import urllib.error

from coverage_connectors import lever
from coverage_connectors.models import LeverBoard


def test_fetch_normalizes_real_fixture(monkeypatch, lever_postings_fixture):
    monkeypatch.setattr(lever, "fetch_json", lambda url, **kw: lever_postings_fixture)

    board = LeverBoard(firm="Palantir", org="palantir")
    result = lever.fetch(board)

    assert result.ok
    assert result.error is None
    assert result.raw_count == len(lever_postings_fixture)
    assert len(result.opportunities) == result.raw_count

    opp = result.opportunities[0]
    raw = lever_postings_fixture[0]
    assert opp.firm == "Palantir"
    assert opp.source == "lever"
    assert opp.title == raw["text"]
    assert opp.url == raw["hostedUrl"]
    assert opp.location == raw["categories"]["location"]
    assert opp.deadline is None  # Lever's postings API exposes no deadline field, ever
    assert opp.status == "open"


def test_posted_at_converts_created_at_epoch(monkeypatch, lever_postings_fixture):
    monkeypatch.setattr(lever, "fetch_json", lambda url, **kw: lever_postings_fixture)
    board = LeverBoard(firm="Palantir", org="palantir")
    result = lever.fetch(board)

    raw = lever_postings_fixture[0]
    opp = result.opportunities[0]
    assert isinstance(raw["createdAt"], (int, float))
    # createdAt is epoch milliseconds; posted_at must be an ISO date, not the
    # raw millisecond integer and not "" (the original's unconditional gap).
    assert opp.posted_at is not None
    assert len(opp.posted_at) == 10 and opp.posted_at.count("-") == 2


def test_fetch_handles_empty_board(monkeypatch):
    monkeypatch.setattr(lever, "fetch_json", lambda url, **kw: [])
    board = LeverBoard(firm="Empty Co", org="emptyco")
    result = lever.fetch(board)

    assert result.ok
    assert result.opportunities == []
    assert result.raw_count == 0


def test_fetch_reports_board_level_failure(monkeypatch):
    def raise_404(url, **kw):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(lever, "fetch_json", raise_404)
    board = LeverBoard(firm="Nonexistent Co", org="nonexistentco")
    result = lever.fetch(board)

    assert not result.ok
    assert result.opportunities == []


def test_classify_url():
    assert lever.classify_url("https://jobs.lever.co/palantir/abc-123") == {"org": "palantir"}
    assert lever.classify_url("https://example.com") is None


def test_classify_url_does_not_match_api_host():
    # api.lever.co's path shape ("api.lever.co/v0/postings/{org}") puts the
    # org three segments deep -- matching it as if it were jobs.lever.co/{org}
    # would silently misparse "v0" as the org. verify()/classify_url() only
    # recognize the candidate-facing jobs.lever.co URL, exactly like the
    # original's _LEVER_RE, and Opportunity.url is always the jobs.lever.co
    # hostedUrl (see _normalize), so this is the URL shape verify() actually
    # needs to handle in practice.
    assert lever.classify_url("https://api.lever.co/v0/postings/palantir?mode=json") is None


def test_verify_open_from_fixture(monkeypatch, lever_postings_fixture):
    monkeypatch.setattr(lever, "fetch_json", lambda url, **kw: lever_postings_fixture)
    result = lever.verify("https://jobs.lever.co/palantir/abc-123")

    assert result.provider == "lever"
    assert result.result == "verified-open"
    assert "live posting" in result.evidence
    assert result.deadline_dates == []


def test_verify_closed_on_empty_board(monkeypatch):
    monkeypatch.setattr(lever, "fetch_json", lambda url, **kw: [])
    result = lever.verify("https://jobs.lever.co/emptyco/abc-123")

    assert result.result == "closed"


def test_verify_unreachable_on_http_error(monkeypatch):
    def raise_500(url, **kw):
        raise urllib.error.HTTPError(url, 500, "boom", None, None)

    monkeypatch.setattr(lever, "fetch_json", raise_500)
    result = lever.verify("https://jobs.lever.co/palantir/abc-123")

    assert result.result == "unreachable"
