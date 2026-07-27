"""Phenom People connector — the `/widgets` refineSearch JSON POST that
Phenom-platform career sites (careers.bcg.com and many others) serve their
own search UI from.

Discovered live (2026-07-23) by watching careers.bcg.com/search-results'
network traffic and re-verified from a clean urllib client: an
unauthenticated POST with the standard refineSearch payload returns
{refineSearch: {totalHits, data: {jobs: [...]}}}. Same public trust tier as
the other providers — it is exactly the request the public site makes for
any visitor, keyed by keyword instead of a board id.

The API exposes `dateCreated` (posted, evidence-only) and no deadline.
Verify() is deliberately `needs-verification`: Phenom job URLs point at
per-audience subdomains (studenttalent./experiencedtalent.) with no
deterministic per-job liveness endpoint verified yet — scrape's
closed-detection (row missing from a successful re-fetch) remains the
removal path, which is already how undated providers stay honest.
"""

from __future__ import annotations

import re

from .http import post_json
from .models import FetchResult, Opportunity, PhenomBoard, VerificationResult

name = "phenom"

_PAGE_SIZE = 50
_MAX_JOBS = 300

_KNOWN_HOSTS_RE = re.compile(r"\.bcg\.com/", re.IGNORECASE)


def _payload(board: PhenomBoard, start: int) -> dict:
    return {
        "lang": board.locale, "deviceType": "desktop", "country": "global",
        "pageName": "search-results", "ddoKey": "refineSearch", "sortBy": "",
        "subsearch": "", "from": start, "jobs": True, "counts": True,
        "all_fields": ["category", "country", "city"],
        "size": _PAGE_SIZE, "clearAll": False, "jdsource": "facets",
        "isSliderEnable": False, "pageId": "page10", "siteType": "external",
        "keywords": board.keywords, "global": True,
        "selected_fields": {}, "locationData": {},
    }


def _normalize(job: dict, board: PhenomBoard) -> Opportunity:
    city = job.get("city") or ""
    country = job.get("country") or ""
    location = ", ".join(p for p in (city, country) if p)
    return Opportunity(
        firm=board.firm,
        title=job.get("title", ""),
        location=location,
        url=(job.get("applyUrl") or "").split("?")[0],
        source="phenom",
        posted_at=(job.get("dateCreated") or "")[:10] or None,
        raw=job,
    )


def fetch(board: PhenomBoard) -> FetchResult:
    seen: set[str] = set()
    jobs: list[dict] = []
    start = 0
    while True:
        try:
            data = post_json(f"https://{board.host}/widgets", _payload(board, start))
        except Exception as e:  # noqa: BLE001
            return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
        block = data.get("refineSearch", {})
        batch = (block.get("data") or {}).get("jobs", [])
        for job in batch:
            jid = str(job.get("jobId") or job.get("jobSeqNo") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            jobs.append(job)
        total = int(block.get("totalHits") or 0)
        start += _PAGE_SIZE
        if not batch or start >= min(total, _MAX_JOBS):
            break
    try:
        # Its own try, separate from the per-page network try above — see
        # greenhouse.py's fetch() for why a normalization failure must not
        # propagate uncaught out of `fetch()`.
        opportunities = [o for o in (_normalize(j, board) for j in jobs) if o.url]
    except Exception as e:  # noqa: BLE001
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities, raw_count=len(jobs))


def classify_url(url: str) -> dict | None:
    return {"host": "bcg"} if _KNOWN_HOSTS_RE.search(url or "") else None


def verify(url: str) -> VerificationResult:
    info = classify_url(url)
    if not info:
        return VerificationResult("phenom", url, "needs-verification",
                                   "URL is not a recognized Phenom-platform job URL", [])
    return VerificationResult(
        "phenom", url, "needs-verification",
        "Phenom exposes no verified per-job liveness endpoint — closed-detection on "
        "re-fetch is the removal path for this provider", [],
    )
