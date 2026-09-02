"""The one-shot transient retry on board fetches.

Measured need: Evercore's board timed out in 5 of 14 recent daily runs, and
each failure silently cost a day of that firm's freshness — a failed board
correctly closes nothing, but it also fetches nothing, and no one is told.

The retry is deliberately narrow: transient NETWORK failures only, one extra
attempt. A 404 is a fact about the board and does not improve seconds later.
"""

from __future__ import annotations

import coverage_connectors as cc
from coverage_connectors.models import FetchResult, GreenhouseBoard

BOARD = GreenhouseBoard(firm="Evercore", token="evercore")


def _result(ok: bool, error: str | None = None) -> FetchResult:
    return FetchResult(board=BOARD, ok=ok, opportunities=[], raw_count=0, error=error)


def _scripted_fetch(monkeypatch, outcomes: list[FetchResult]):
    calls = {"n": 0}

    def fake(board, **kwargs):
        # **kwargs so this stands in for the real `fetch(board, *,
        # banked_rows=0)`; the retry path passes the caller's row history
        # through and this helper does not care what it is.
        result = outcomes[min(calls["n"], len(outcomes) - 1)]
        calls["n"] += 1
        return result

    monkeypatch.setattr(cc, "fetch", fake)
    return calls


def test_a_timeout_gets_one_more_chance(monkeypatch):
    calls = _scripted_fetch(monkeypatch, [
        _result(False, "GET https://…: The read operation timed out"),
        _result(True),
    ])
    slept = []
    out = cc.fetch_with_retry(BOARD, _sleep=slept.append)
    assert out.ok
    assert calls["n"] == 2
    assert slept == [3.0], "one backoff, then the retry"


def test_a_hard_failure_is_not_retried(monkeypatch):
    """404 / auth / shape errors are facts about the board. Retrying them
    doubles the load and delays the honest error."""
    calls = _scripted_fetch(monkeypatch, [_result(False, "HTTP 404 Not Found")])
    out = cc.fetch_with_retry(BOARD, _sleep=lambda s: None)
    assert not out.ok
    assert calls["n"] == 1


def test_a_success_never_sleeps(monkeypatch):
    calls = _scripted_fetch(monkeypatch, [_result(True)])
    slept = []
    cc.fetch_with_retry(BOARD, _sleep=slept.append)
    assert calls["n"] == 1 and slept == []


def test_two_timeouts_stop_at_the_retry_ceiling(monkeypatch):
    """A courtesy retry, not a retry loop: two attempts is the ceiling, and
    the second failure is returned honestly."""
    calls = _scripted_fetch(monkeypatch, [
        _result(False, "connection reset by peer"),
        _result(False, "connection reset by peer"),
    ])
    out = cc.fetch_with_retry(BOARD, _sleep=lambda s: None)
    assert not out.ok
    assert calls["n"] == 2
