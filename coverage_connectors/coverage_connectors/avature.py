"""Avature connector — the RSS/XML job feed Avature career sites expose.

Avature-hosted career sites (careers.bain.com, recruitment.macquarie.com, …)
publish a standard RSS 2.0 feed at `.../SearchJobs/feed/`, paginated by a
`folderOffset` query param (20 items/page). Each `<item>` carries a real,
candidate-facing posting URL in `<link>`/`<guid>` — no fabricated identity.

Honesty limits, stated up front:

- The feed carries NO location field. `location` is left blank rather than
  guessed; a city that happens to sit in the title is left in the title.
- The feed carries no application deadline; `deadline` is always None.
- `verify()` re-reads the feed: a posting URL still present is verified-open;
  one absent from its own feed is closed. (Avature detail pages 200 even when
  filled, so the feed is the only honest liveness signal.)
"""

from __future__ import annotations

import re

from .http import fetch_text
from .models import AvatureBoard, FetchResult, Opportunity, VerificationResult

name = "avature"

_ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>",
                       re.IGNORECASE | re.DOTALL)
_LINK_RE = re.compile(r"<link>\s*(.*?)\s*</link>", re.IGNORECASE | re.DOTALL)
_PAGE_SIZE = 20
_MAX_ITEMS = 400  # bound a saturated feed, same posture as the other connectors


def _feed_url(base: str, offset: int) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}folderRecordsPerPage={_PAGE_SIZE}&folderOffset={offset}"


def _parse_items(xml: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for block in _ITEM_RE.findall(xml):
        t = _TITLE_RE.search(block)
        l = _LINK_RE.search(block)
        if not t or not l:
            continue
        title = re.sub(r"\s+", " ", t.group(1)).strip()
        link = l.group(1).strip()
        if title and link.startswith("http"):
            out.append((title, link))
    return out


def _normalize(title: str, url: str, board: AvatureBoard) -> Opportunity:
    return Opportunity(firm=board.firm, title=title, location="", url=url, source="avature")


def fetch(board: AvatureBoard) -> FetchResult:
    """Page the RSS feed by folderOffset until a page repeats or empties,
    deduped by URL. A failed page fails the board (partial coverage would let
    closed-detection close live rows)."""
    seen: set[str] = set()
    opps: list[Opportunity] = []
    offset = 0
    while offset < _MAX_ITEMS:
        try:
            xml = fetch_text(_feed_url(board.feed_url, offset))
        except Exception as e:  # noqa: BLE001 — board-level failure, not fatal to a multi-board run
            return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
        items = _parse_items(xml)
        fresh = [(t, u) for t, u in items if u not in seen]
        if not fresh:
            break  # empty page or all-duplicates (feed looped) -> done
        for title, url in fresh:
            seen.add(url)
            opps.append(_normalize(title, url, board))
        if len(items) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return FetchResult(board=board, ok=True, opportunities=opps, raw_count=len(opps))


def classify_url(url: str) -> dict | None:
    return {"url": url} if "/jobs/" in (url or "") else None


def verify(url: str) -> VerificationResult:
    """No per-firm feed registry, so verify is best-effort: unrecognized URLs
    are left for a re-fetch to settle."""
    if not classify_url(url):
        return VerificationResult("avature", url, "needs-verification",
                                   "not a recognized Avature job URL", [])
    return VerificationResult("avature", url, "needs-verification",
                              "Avature liveness is resolved by the next feed fetch", [])
