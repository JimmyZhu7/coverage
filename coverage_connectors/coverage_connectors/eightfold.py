"""Eightfold connector — the eightfold.ai talent-platform public jobs API.

Agent-identified while probing multi-strategy funds: Millennium serves its
careers site (career.mlp.com) on Eightfold. The `/api/apply/v2/jobs` endpoint
returns the full position list as JSON with no auth, keyed off a `domain`
query param (e.g. "mlp.com"); we page through `count` in fixed windows.
Titles, locations, and the canonical posting URL are read straight off
`positions[]` — nothing is inferred (same posture as the other connectors).
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlencode

from .http import fetch_json, fetch_text, unreadable
from .models import EightfoldBoard, FetchResult, Opportunity, VerificationResult

name = "eightfold"

_PAGE = 50       # positions per request
_MAX = 2000      # hard cap so a bad `count` can never loop forever
_URL = "https://{host}/api/apply/v2/jobs?{qs}"
# A live Eightfold posting URL looks like https://<tenant>.eightfold.ai/careers/job/<id>.
_JOB_MARK = ".eightfold.ai/careers/job/"


def _page_url(board: EightfoldBoard, start: int, num: int) -> str:
    qs = urlencode({"domain": board.domain, "start": start, "num": num, "sort_by": "relevance"})
    return _URL.format(host=board.host, qs=qs)


def _posted_at(pos: dict) -> str | None:
    ts = pos.get("t_update") or pos.get("t_create")
    if not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ts, tz=UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _normalize(pos: dict, board: EightfoldBoard) -> Opportunity:
    loc = pos.get("location") or ""
    if not loc:
        locs = pos.get("locations")
        if isinstance(locs, list) and locs:
            loc = locs[0]
    return Opportunity(
        firm=board.firm,
        title=pos.get("name", ""),
        location=loc,
        url=pos.get("canonicalPositionUrl") or "",
        source="eightfold",
        posted_at=_posted_at(pos),
        raw=pos,
    )


def fetch(board: EightfoldBoard) -> FetchResult:
    """Page the whole board: GET `num`-sized windows until we've collected
    `count` positions (or a page comes back empty). Eightfold paginates with
    `start`/`num`, so unlike Lever this needs a real loop."""
    positions: list[dict] = []
    start = 0
    # Set only when the CAP ends the walk. Both natural exits below (an empty
    # page, reaching the API's own `count`) mean we read the whole board.
    truncated = False
    stated_count: int | None = None
    try:
        while start < _MAX:
            data = fetch_json(_page_url(board, start, _PAGE))
            # `/api/apply/v2/jobs` always sends `positions`. A dict without
            # it, or a bare list, is not a jobs response — and the old
            # `isinstance(data, dict)` fallback turned every one of those into
            # an empty batch and a clean zero for the whole board.
            if not isinstance(data, dict) or "positions" not in data:
                shape = (f"{type(data).__name__}" if not isinstance(data, dict)
                         else f"keys {sorted(data)[:6]}")
                return FetchResult(
                    board=board, ok=False, opportunities=[], raw_count=0,
                    error=unreadable(
                        f"eightfold domain {board.domain!r} answered 200 with no "
                        f"'positions' key ({shape})"),
                )
            batch = data.get("positions") or []
            if stated_count is None and isinstance(data.get("count"), int):
                stated_count = data["count"]
            if not batch and start == 0 and stated_count:
                return FetchResult(
                    board=board, ok=False, opportunities=[], raw_count=0,
                    error=unreadable(
                        f"eightfold reported count={stated_count} and returned 0 "
                        f"positions"),
                )
            if not batch:
                break
            positions.extend(batch)
            total = data.get("count") if isinstance(data, dict) else None
            if isinstance(total, int) and len(positions) >= total:
                break
            # Advance by what the API actually returned — Eightfold silently
            # caps a page below the requested `num`, so a fixed stride would
            # skip rows.
            start += len(batch)
        else:
            # `while` fell through its condition rather than breaking: we
            # stopped at _MAX with the board still going. Absence from this
            # list is not absence from the board — see ingest's close guard.
            truncated = True
        # Kept inside the same try as the paging loop above — see
        # greenhouse.py's fetch() for why a normalization failure must not
        # propagate uncaught out of `fetch()`.
        opportunities = [_normalize(p, board) for p in positions]
    except Exception as e:  # noqa: BLE001
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities,
                       raw_count=len(positions), truncated=truncated,
                       empty_state=not opportunities and stated_count == 0)


def classify_url(url: str) -> dict | None:
    return {"url": url} if url and _JOB_MARK in url else None


def verify(url: str) -> VerificationResult:
    """Board-level liveness only. The jobs API keys off host+domain, not a
    single posting id, so — as with Lever — this is a reachability check on
    the posting page (200 = still served, 404 = gone), not an item-specific
    "is this requisition still open" guarantee."""
    if classify_url(url) is None:
        return VerificationResult(
            "eightfold", url, "needs-verification",
            "URL is not a recognized eightfold.ai job URL", [],
        )
    try:
        fetch_text(url)
    except Exception as e:  # noqa: BLE001
        return VerificationResult("eightfold", url, "unreachable", str(e)[:200], [])
    return VerificationResult(
        "eightfold", url, "verified-open",
        "posting page reachable (board-level check, not item-specific)", [],
    )
