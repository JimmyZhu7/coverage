"""McKinsey connector — the careers gateway's public JSON search.

Discovered live (2026-07-23) by watching mckinsey.com/careers/search-jobs's
own network traffic: the page calls
`gateway.mckinsey.com/apigw-x0cceuow60/v1/api/jobs/search` — plain GET,
no auth, no cookies (re-verified from a clean urllib client). Same public
trust tier as the other providers' boards APIs; the `q=` keyword parameter
matches the search box's behavior (verified: q=intern narrows 595 -> 44).

Notes on fidelity:

- `friendlyURL` is the candidate-facing posting URL; rows without one are
  skipped (no stable identity).
- Cities can list dozens of offices per role; location is reported as the
  first city plus an honest "+N more", never a fabricated single office.
- The API exposes no deadline anywhere (checked every field) — deadline is
  always None, per the never-invent rule.
"""

from __future__ import annotations

import re
import urllib.parse

from .http import fetch_json, unreadable
from .models import FetchResult, McKinseyBoard, Opportunity, VerificationResult

name = "mckinsey"

_SEARCH_URL = (
    "https://gateway.mckinsey.com/apigw-x0cceuow60/v1/api/jobs/search"
    "?pageSize={size}&start={start}&lang=en"
)
_PAGE_SIZE = 50
_MAX_JOBS = 300  # bound a saturated query, same posture as workday's cap

_URL_RE = re.compile(r"mckinsey\.com/careers/search-jobs/jobs/([^/?#]+)", re.IGNORECASE)


def _page(keyword: str, start: int) -> dict:
    url = _SEARCH_URL.format(size=_PAGE_SIZE, start=start)
    if keyword:
        url += "&q=" + urllib.parse.quote(keyword)
    return fetch_json(url)


def _location(doc: dict) -> str:
    # Sorted before formatting: the API gives no ordering guarantee on
    # `cities`, and `location` feeds `directory.ingest`'s `content_hash` —
    # an unsorted array that comes back in a different order on a later
    # fetch (same set of cities, same posting) would flip `changed`/
    # `content_hash` for a role nothing actually changed about.
    cities = sorted(doc.get("cities") or [])
    if not cities:
        return ""
    if len(cities) == 1:
        return cities[0]
    return f"{cities[0]} +{len(cities) - 1} more"


def _job_url(doc: dict) -> str:
    friendly = (doc.get("friendlyURL") or "").strip()
    if not friendly:
        return ""
    if friendly.startswith("http"):
        return friendly
    return f"https://www.mckinsey.com/careers/search-jobs/jobs/{friendly.lstrip('/')}"


def _normalize(doc: dict, board: McKinseyBoard) -> Opportunity:
    return Opportunity(
        firm=board.firm,
        title=doc.get("title", ""),
        location=_location(doc),
        url=_job_url(doc),
        source="mckinsey",
        raw=doc,
    )


def fetch(board: McKinseyBoard) -> FetchResult:
    """One search per configured keyword, deduped by jobID, paginated up to
    the cap. A failed keyword fails the board (partial coverage would let
    closed-detection close live rows)."""
    seen: set[str] = set()
    docs: list[dict] = []
    every_keyword_said_zero = True
    for kw in board.keywords or ("",):
        start = 1
        while True:
            try:
                data = _page(kw, start)
            except Exception as e:  # noqa: BLE001
                return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
            # `.get("docs") or []`, never `.get("docs", [])`: the gateway
            # returns the key PRESENT with value `null` on a legitimate
            # zero-hit page, and `.get`'s default only fires when the key is
            # missing — `for doc in None` would raise an uncaught TypeError
            # out here (outside the network try above) and kill the whole
            # board's fetch. Same root cause as `verify`'s envelope guard.
            # `docs` present-but-null is a legitimate zero-hit page (see
            # above). `docs` ABSENT is a different envelope entirely — the
            # gateway's error body, or a shape change — and folding it into
            # the same empty list is how a whole board reads as closed.
            if "docs" not in data:
                return FetchResult(
                    board=board, ok=False, opportunities=[], raw_count=0,
                    error=unreadable(
                        f"mckinsey gateway answered 200 with no 'docs' key "
                        f"(got {sorted(data)[:6]})"),
                )
            batch = data.get("docs") or []
            stated_total = data.get("numFound")
            if not batch and start == 1 and isinstance(stated_total, int) and stated_total > 0:
                return FetchResult(
                    board=board, ok=False, opportunities=[], raw_count=0,
                    error=unreadable(
                        f"mckinsey reported numFound={stated_total} and returned "
                        f"0 docs"),
                )
            every_keyword_said_zero = every_keyword_said_zero and not batch and stated_total == 0
            for doc in batch:
                jid = str(doc.get("jobID") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                docs.append(doc)
            total = int(data.get("numFound") or 0)
            start += _PAGE_SIZE
            if not batch or start > min(total, _MAX_JOBS):
                break
    try:
        # Its own try, separate from the per-page network try above — see
        # greenhouse.py's fetch() for why a normalization failure must not
        # propagate uncaught out of `fetch()`.
        opportunities = [o for o in (_normalize(d, board) for d in docs) if o.url]
    except Exception as e:  # noqa: BLE001
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities,
                       raw_count=len(docs),
                       empty_state=not opportunities and every_keyword_said_zero)


def classify_url(url: str) -> dict | None:
    m = _URL_RE.search(url or "")
    return {"slug": m.group(1)} if m else None


def verify(url: str) -> VerificationResult:
    """Search the API for the URL's slug tokens and require the friendlyURL
    to round-trip. Found -> verified-open; network trouble -> unreachable.

    A clean search with no match -> `needs-verification`, NOT closed: this
    only checks ONE 50-row page (`_page(keyword, 1)`, no pagination loop like
    `fetch`'s), and `keyword` is derived by lopping the slug's FIRST token off
    (`slug.split("-", 1)[-1]`) — a crude derivation that can easily miss the
    words that actually rank the real posting, especially past page 1 of a
    saturated query. `data.get("docs") or []` also silently normalizes a
    missing/renamed envelope key AND a present-but-`null` "docs" value (both
    observed live, on a legitimate zero-hit single-keyword search) to `[]`,
    indistinguishable from a genuine zero matches — the `or []` guards
    against `for doc in None` crashing on that null-but-present case, not
    just the missing-key case a bare `dict.get` default already covered.
    None of that is a positive "this posting is gone" signal, and
    `reverify.py` acts on "closed" with zero corroboration."""
    info = classify_url(url)
    if not info:
        return VerificationResult("mckinsey", url, "needs-verification",
                                   "URL is not a recognized McKinsey careers job URL", [])
    slug = info["slug"]
    keyword = slug.split("-", 1)[-1].replace("-", " ")[:80]
    try:
        data = _page(keyword, 1)
    except Exception as e:  # noqa: BLE001
        return VerificationResult("mckinsey", url, "unreachable", str(e)[:200], [])
    for doc in data.get("docs") or []:
        if slug.lower() in (doc.get("friendlyURL") or "").lower():
            return VerificationResult("mckinsey", url, "verified-open",
                                       f'title="{doc.get("title", "")}" matched by friendlyURL', [])
    return VerificationResult(
        "mckinsey", url, "needs-verification",
        f'slug {slug!r} not found on page 1 of a single-keyword search — '
        f'not a confirmed closure', [],
    )
