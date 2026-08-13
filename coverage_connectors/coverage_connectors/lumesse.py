"""Lumesse TalentLink FO-REST connector — the widget API behind
`*.recruitmentplatform.com` career sites (BOCI's board, ~126 postings).

Deferred in July as "needs a new connector + live URL verification"; both
halves resolved 2026-08-07 against the live board:

- **Auth is two literal request headers, not HTTP Basic.** The widget bundle's
  `withGuestCredentials` sends `username: {techId}:guest:FO` and
  `password: guest` as plain headers; a Basic Authorization built from the
  same strings gets a bare 403 error page. Read out of
  talentportal-widgets-namespaced.js, not guessed.
- **Per-job apply URLs exist**: `jobFields.applicationUrl` is a complete
  apply-form link carrying the tech id and job id.

The prize in this payload is `DPOSTINGEND` — a structured posting-end
timestamp on every job. That is a real provider deadline field, the thing
list APIs almost never carry, and it flows through `Opportunity.deadline` at
provider confidence like Greenhouse's `application_deadline` does. The epoch
is converted in the board's own `POSTINGTIMEZONE` when given: the timestamp
1806533999000 is 23:59:59 in Europe/London and already 06:59 the NEXT DAY in
UTC+8, so a UTC conversion would report every closing date one day late for
exactly the market this board serves.

Job descriptions ride in `customFields` (titled HTML blocks); they are
joined into `raw["detail_text"]` so the facts extractors read this board's
postings without `enrich_postings` ever needing to fetch a detail page.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .http import fetch_json
from .models import FetchResult, LumesseBoard, Opportunity, VerificationResult

name = "lumesse"

# jobFields.applicationUrl — what actually gets stored as Opportunity.url —
# points at a Lumesse TalentLink apply form on a `{region}-apply.
# lumessetalentlink.com` host (verified against BOCI's live board:
# "https://au01-apply.lumessetalentlink.com/apply-app/pages/
# application-form?jobId=..."), the same domain family as the board's own
# `host` config (e.g. "au01-foc.lumessetalentlink.com"). No per-firm
# registry exists to reconstruct a board's `host`/`tech_id` from a bare URL
# the way sitemap.py's `_SITEMAPS` does, so verify() below cannot re-query
# the FO-REST API for a specific job — only recognize the URL as ours.
_HOST_RE = re.compile(r"\.lumessetalentlink\.com", re.IGNORECASE)

_PAGE = 50
_MAX_JOBS = 1000
_TAGS = re.compile(r"<[^>]+>")


def _guest_headers(board: LumesseBoard) -> dict[str, str]:
    return {
        "username": f"{board.tech_id}:guest:FO",
        "password": "guest",
        "Accept": "application/json",
    }


def _page_url(board: LumesseBoard, first: int) -> str:
    return (f"https://{board.host}/fo/rest/jobs"
            f"?firstResult={first}&maxResults={_PAGE}"
            f"&sortBy=sJobTitle&sortOrder=asc")


def _deadline(fields: dict) -> str | None:
    ms = fields.get("DPOSTINGEND")
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    try:
        tz = ZoneInfo(fields.get("POSTINGTIMEZONE") or "UTC")
    except Exception:
        tz = timezone.utc
    return datetime.fromtimestamp(ms / 1000, tz).date().isoformat()


def _description(job: dict) -> str:
    parts = []
    for block in job.get("customFields") or []:
        title = (block.get("title") or "").strip()
        content = _TAGS.sub(" ", block.get("content") or "").strip()
        if content:
            parts.append(f"{title}: {content}" if title else content)
    return re.sub(r"\s+", " ", " ".join(parts))[:20_000]


def _normalize(job: dict, board: LumesseBoard) -> Opportunity | None:
    fields = job.get("jobFields") or {}
    title = (fields.get("jobTitle") or "").strip()
    url = (fields.get("applicationUrl") or "").strip()
    if not title or not url.startswith("http"):
        return None
    raw = {
        "id": fields.get("id"),
        "jobNumber": fields.get("jobNumber"),
        "division": fields.get("SLOVLIST3") or "",
    }
    text = _description(job)
    if text:
        raw["detail_text"] = text
        raw["detail_source"] = "payload"
    return Opportunity(
        firm=board.firm,
        title=title,
        location="",           # the list payload states none; never inferred
        url=url,
        source="lumesse",
        deadline=_deadline(fields),
        raw=raw,
    )


def fetch(board: LumesseBoard) -> FetchResult:
    """Page firstResult until the board's own jobsCount is reached. A failed
    page fails the board — partial coverage would let closed-detection close
    live rows, same contract as every connector here."""
    opps: list[Opportunity] = []
    seen: set[str] = set()
    try:
        first = 0
        total = None
        while first < (_MAX_JOBS if total is None else min(total, _MAX_JOBS)):
            data = fetch_json(_page_url(board, first),
                              headers=_guest_headers(board))
            if total is None:
                total = int((data.get("globals") or {}).get("jobsCount") or 0)
            jobs = data.get("jobs") or []
            if not jobs:
                break
            for job in jobs:
                opp = _normalize(job, board)
                if opp is not None and opp.url not in seen:
                    seen.add(opp.url)
                    opps.append(opp)
            first += _PAGE
        return FetchResult(board=board, ok=True, opportunities=opps,
                           raw_count=total or len(opps))
    except Exception as exc:  # noqa: BLE001 — one board must not sink the run
        return FetchResult(board=board, ok=False, opportunities=[],
                           raw_count=0, error=f"{type(exc).__name__}: {exc}")


def classify_url(url: str) -> dict | None:
    return {"url": url} if _HOST_RE.search(url or "") else None


def verify(url: str) -> VerificationResult:
    """`coverage_connectors.verify()`'s dispatch loop calls `classify_url`
    then `verify` on every registered connector in turn until one of them
    claims the URL — this module used to define neither, so reaching it
    without a prior match raised a bare AttributeError and killed the whole
    dispatch for any BOCI-shaped URL. Same honest "needs-verification"
    contract as phenom.py and avature.py: no per-firm registry exists here
    (see `_HOST_RE`'s note) to re-query the FO-REST API for one specific
    job, so closed-detection on the next board re-fetch remains the removal
    path for this provider."""
    if not classify_url(url):
        return VerificationResult("lumesse", url, "needs-verification",
                                   "URL is not a recognized Lumesse TalentLink URL", [])
    return VerificationResult(
        "lumesse", url, "needs-verification",
        "Lumesse has no per-posting liveness endpoint verified yet — "
        "closed-detection on the next board re-fetch is the removal path", [],
    )
