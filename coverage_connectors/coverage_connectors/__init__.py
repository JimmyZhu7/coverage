"""coverage_connectors — the deterministic ATS fetch/verify engine.

A pure fetch → normalize → return library for three applicant-tracking
systems (Greenhouse, Lever, Workday), extracted from a single-user codebase
into a shared, multi-tenant-ready package. See `docs/build-plan.md` §1/§4
and `docs/existing-system.md` §5 for the design this ports.

What this package does NOT do, by design:

- **No board list of its own.** Every `fetch`/`fetch_many` call takes board
  configs (`GreenhouseBoard` / `LeverBoard` / `WorkdayBoard`) as an explicit
  argument. This package owns no `_ATS_BOARDS`-equivalent constant, reads no
  YAML/JSON firm directory, and does not know what a "target firm" is —
  that list lives in Coverage's own data (the `firms` table), not here.
- **No state, no dedup, no "what's new since last time."** Every call is a
  fresh network fetch; there is no `*_state.json`, no first-seen ledger, no
  on-disk cache. Persistence and diffing are Coverage's ingest layer's job.
- **No role/region/taxonomy filtering.** `fetch()` returns every posting
  the board reports. Nothing here knows what "early career" or "Hong Kong"
  means — a caller who wants that can filter the returned
  `list[Opportunity]` with its own predicate.
- **No absolute filesystem paths.** Nothing in this package reads or writes
  a file; there is nothing to point at a path.

Usage:

    from coverage_connectors import GreenhouseBoard, WorkdayBoard, fetch_many, verify

    boards = [
        GreenhouseBoard(firm="William Blair", token="williamblair"),
        WorkdayBoard(firm="Citi", tenant_host="citi.wd5",
                     site="Citi_Early_Careers_Events_Site"),
    ]
    results = fetch_many(boards)
    for result in results:
        if not result.ok:
            print(f"{result.board.firm}: fetch failed — {result.error}")
            continue
        for opp in result.opportunities:
            print(opp.firm, opp.title, opp.url)

    status = verify("https://boards-api.greenhouse.io/v1/boards/.../jobs/123")
    # or: status = verify(opp.url)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import avature, beisen, eightfold, goldmansachs, greenhouse, icims, lever, lumesse, mckinsey, oracle, phenom, sitemap, socgen, successfactors, talentgateway, talentsoft, talnet, workday
from .models import (
    BoardConfig,
    AvatureBoard,
    BeisenBoard,
    Connector,
    EightfoldBoard,
    FetchResult,
    GoldmanSachsBoard,
    GreenhouseBoard,
    IcimsBoard,
    LeverBoard, LumesseBoard, SocGenBoard, TalentsoftBoard,
    McKinseyBoard,
    Opportunity,
    OracleBoard,
    PhenomBoard,
    SitemapBoard,
    SuccessFactorsBoard,
    TalentGatewayBoard,
    TalnetBoard,
    VerificationResult,
    WorkdayBoard,
)

__all__ = [
    "BoardConfig",
    "AvatureBoard",
    "BeisenBoard",
    "Connector",
    "EightfoldBoard",
    "FetchResult",
    "GoldmanSachsBoard",
    "GreenhouseBoard",
    "IcimsBoard",
    "LeverBoard",
    "McKinseyBoard",
    "Opportunity",
    "OracleBoard",
    "PhenomBoard",
    "SitemapBoard",
    "SuccessFactorsBoard",
    "TalentGatewayBoard",
    "TalnetBoard",
    "VerificationResult",
    "WorkdayBoard",
    "CONNECTORS",
    "fetch",
    "fetch_many",
    "verify",
]

# Provider name -> connector module. A future connector (Oracle, tal.net, …)
# registers here and immediately works with fetch_many()/verify() below —
# nothing about this dispatch is Greenhouse/Lever/Workday-specific.
CONNECTORS: dict[str, Connector] = {
    greenhouse.name: greenhouse,
    lever.name: lever,
    workday.name: workday,
    oracle.name: oracle,
    talnet.name: talnet,
    sitemap.name: sitemap,
    mckinsey.name: mckinsey,
    phenom.name: phenom,
    goldmansachs.name: goldmansachs,
    talentgateway.name: talentgateway,
    eightfold.name: eightfold,
    beisen.name: beisen,
    avature.name: avature,
    lumesse.name: lumesse,
    icims.name: icims,
    socgen.name: socgen,
    talentsoft.name: talentsoft,
    successfactors.name: successfactors,
}


def fetch(board: BoardConfig) -> FetchResult:
    """Fetch one board, dispatching on `board.provider`."""
    connector = CONNECTORS.get(board.provider)
    if connector is None:
        raise ValueError(f"no connector registered for provider {board.provider!r}")
    return connector.fetch(board)


# Error text that marks a fetch worth retrying: the failure is about the
# NETWORK MOMENT, not the board. Measured need, not speculation — Evercore's
# board timed out in 5 of 14 recent daily runs and each one silently cost a
# day of that firm's freshness, because a failed board correctly closes
# nothing but also fetches nothing.
_TRANSIENT_MARKERS = (
    "timed out", "timeout", "connection reset", "connection aborted",
    "connection refused", "temporarily unavailable",
    " 502", " 503", " 504", "bad gateway", "service unavailable",
)


def _is_transient(error: str | None) -> bool:
    text = (error or "").lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


def fetch_with_retry(
    board: BoardConfig, *, retries: int = 1, backoff_s: float = 3.0,
    _sleep: Callable[[float], None] = time.sleep,
) -> FetchResult:
    """`fetch`, retried once on a transient network failure.

    Only transient errors retry: a 404 or an auth failure is a fact about
    the BOARD and will not improve on a second attempt seconds later, so
    retrying it would just double the load and delay the honest error.
    `_sleep` is injectable for tests; the backoff grows linearly because two
    attempts is the ceiling — this is a courtesy retry, not a retry loop.
    """
    result = fetch(board)
    attempt = 0
    while not result.ok and _is_transient(result.error) and attempt < retries:
        attempt += 1
        _sleep(backoff_s * attempt)
        result = fetch(board)
    return result


def fetch_many(boards: list[BoardConfig], *, max_workers: int = 8) -> list[FetchResult]:
    """Fetch every board concurrently (ported from `sources.py`'s
    `ats_candidates()`, which ran its ~37 boards through a
    `ThreadPoolExecutor(max_workers=8)` rather than 15s-timeout-each in
    strict sequence). `pool.map` preserves input order in its results, so
    the returned list lines up with `boards` even though the fetches
    themselves run out of order. Each fetch carries the one-shot transient
    retry — see `fetch_with_retry`."""
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(fetch_with_retry, boards))


def verify(url: str) -> VerificationResult:
    """Classify `url` by provider and run that provider's liveness check.
    Accepts a bare URL (e.g. `opportunity.url`) rather than requiring a
    board config, since a caller verifying a stored opportunity generally
    only has the URL persisted, not the original fetch-time board config."""
    for connector in CONNECTORS.values():
        classify: Callable[[str], dict | None] = connector.classify_url  # type: ignore[attr-defined]
        if classify(url) is not None:
            return connector.verify(url)
    return VerificationResult(
        provider="unknown", url=url, result="needs-verification",
        evidence="URL doesn't match any registered connector's pattern",
        deadline_dates=[],
    )
