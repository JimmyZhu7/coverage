"""Oracle Recruiting Cloud connector — public, unauthenticated REST.

Ported from the radar's `sources.py` (`_ORACLE_URL`, `_fetch_oracle`,
`_ats_normalize`'s oracle branch) and `verify_rows.py` (`_verify_oracle`,
the oracle arm of `_URL_RES`). Same public trust tier as the
Greenhouse/Lever/Workday boards-api calls: GET with the keyword in the
finder expression, no auth, no cookies.

Fetch contract: one search per configured keyword (Oracle's finder has no
OR-of-terms syntax that reliably widens results — the career site's own
search box behaves the same way), deduplicated by requisition Id across
searches. `PostingEndDate` is a GENUINE deadline when present — the only
one of the ported providers besides Greenhouse that exposes one — and
`PostedDate` is evidence-only.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse

from .http import fetch_json
from .models import FetchResult, Opportunity, OracleBoard, VerificationResult

name = "oracle"

_SEARCH_URL = (
    "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    "?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber={site},limit=25,keyword={kw}"
)
_JOB_URL = "https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}"

# Same shape as the radar's oracle _URL_RES arm: candidate-facing job URLs.
_URL_RE = re.compile(
    r"([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/en/sites/([^/?#]+)(?:/job/(\d+))?",
    re.IGNORECASE,
)


def _search(host: str, site: str, keyword: str) -> tuple[list[dict], int | None]:
    """(matched requisitions, provider-reported total for this keyword).

    `total` is Oracle's own `TotalJobsCount` — the count of every
    requisition matching this keyword, which can be (and, for JPM's
    "insight", genuinely is: 1,631 live) far more than the `limit=25` this
    connector ever asks for or gets back per search. `None` when the
    envelope carries no such field (missing/malformed — the same case
    `verify()` already treats as "can't tell", never as evidence)."""
    url = _SEARCH_URL.format(host=host, site=site, kw=urllib.parse.quote(keyword))
    data = fetch_json(url)
    items = data.get("items", [])
    if not items:
        return [], None
    block = items[0]
    total = block.get("TotalJobsCount")
    return block.get("requisitionList", []), (total if isinstance(total, int) else None)


def _normalize(req: dict, board: OracleBoard) -> Opportunity:
    rid = str(req.get("Id") or "")
    deadline = req.get("PostingEndDate") or None
    posted = req.get("PostedDate") or None
    return Opportunity(
        firm=board.firm,
        title=req.get("Title", ""),
        location=req.get("PrimaryLocation", ""),
        url=_JOB_URL.format(host=board.host, site=board.site_number, rid=rid) if rid else "",
        source="oracle",
        deadline=deadline[:10] if deadline else None,
        posted_at=posted[:10] if posted else None,
        raw=req,
    )


def fetch(board: OracleBoard) -> FetchResult:
    """One keyword search per configured term, deduped by requisition Id.
    A single failed keyword fails the whole board (partial keyword coverage
    would silently shrink closed-detection's 'seen' set and close live
    rows)."""
    seen: set[str] = set()
    reqs: list[dict] = []
    # True when ANY keyword's own search under-returned against Oracle's
    # reported total — e.g. JPM's "insight" search: TotalJobsCount=1631
    # against this connector's hardcoded limit=25, with no pagination.
    # Unlike workday.py/eightfold.py/avature.py/icims.py/goldmansachs.py,
    # this connector never set `truncated` at all, so ingest.py's
    # truncated-pair exemption from closed-detection could never engage —
    # every under-returned oracle fetch ran the normal close-on-absence
    # path unguarded. Confirmed the mechanism live: JPM opportunity 4731
    # (Id 210765240), freshly posted 2026-08-10 and genuinely still open,
    # fell outside the top-25-by-relevancy "insight" results and was
    # falsely closed in the same batch as 3 other JPM oracle rows.
    truncated = False
    for kw in board.keywords:
        try:
            found, total = _search(board.host, board.site_number, kw)
        except Exception as e:  # noqa: BLE001 — board-level failure, not fatal to the run
            return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
        if total is not None and total > len(found):
            truncated = True
        for req in found:
            rid = str(req.get("Id") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            reqs.append(req)
    try:
        # Kept in its own try, separate from the per-keyword network try
        # above — see greenhouse.py's fetch() for why a normalization
        # failure must not propagate uncaught out of `fetch()`.
        opportunities = [o for o in (_normalize(r, board) for r in reqs) if o.url]
    except Exception as e:  # noqa: BLE001
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities, raw_count=len(reqs),
                        truncated=truncated)


def classify_url(url: str) -> dict | None:
    m = _URL_RE.search(url or "")
    if not m:
        return None
    return {"host": m.group(1), "site": m.group(2), "job_id": m.group(3)}


def verify(url: str) -> VerificationResult:
    """Ported from `_verify_oracle`: keyword-search the site for the
    requisition Id itself (Oracle's keyword search matches Ids). Found ->
    verified-open with its real dates; network trouble is unreachable.

    An Id the search can't find is `needs-verification`, NOT closed:
    `_search` returns `[]` both for a genuine "no results" AND for a missing
    or renamed envelope key (`data.get("items", [])` / `items[0].get(
    "requisitionList", [])`) — the two are indistinguishable at this call
    site, and `reverify.py` acts on "closed" with zero corroboration. Only a
    positive signal (the requisition present, or absent from a response we
    can positively confirm parsed correctly) may close a row; this connector
    has no way to make that distinction today, so it must not close."""
    info = classify_url(url)
    if not info:
        return VerificationResult("oracle", url, "needs-verification",
                                   "URL is not a recognized Oracle Recruiting Cloud job URL", [])
    host, site, job_id = info["host"], info["site"], info["job_id"]
    if not job_id:
        return VerificationResult("oracle", url, "needs-verification",
                                   f"oracle site {site!r} but no requisition Id in the URL", [])
    try:
        reqs, _total = _search(host, site, job_id)
    except urllib.error.HTTPError as e:
        return VerificationResult("oracle", url, "unreachable",
                                   f"HTTP {e.code} from Oracle Recruiting Cloud", [])
    except Exception as e:  # noqa: BLE001
        return VerificationResult("oracle", url, "unreachable", str(e)[:200], [])

    match = next((r for r in reqs if str(r.get("Id")) == str(job_id)), None)
    if match is None:
        return VerificationResult(
            "oracle", url, "needs-verification",
            f"requisition Id {job_id} not found via keyword search — "
            f"indistinguishable here from a malformed/renamed response "
            f"envelope, so not a confirmed closure", [],
        )
    title = match.get("Title", "")
    posted = (match.get("PostedDate") or "")[:10]
    end = (match.get("PostingEndDate") or "")[:10]
    # PostedDate is a POSTED date, never a deadline — evidence-only.
    # PostingEndDate IS a genuine deadline and feeds deadline_dates.
    return VerificationResult(
        "oracle", url, "verified-open",
        f'title="{title}" PostedDate={posted or "unstated"} PostingEndDate={end or "unstated"}',
        [end] if end else [],
        posted_date=posted or None,
    )
