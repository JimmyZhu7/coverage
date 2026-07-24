"""Beisen (北森) browser connector — the JS-session ("browser") tier.

Beisen recruiting SPAs (e.g. CICC's cicc.zhiye.com) 403 on raw HTTP: the job
list only renders after the site's own JavaScript bootstraps a session. This
connector drives a headless Chromium (Playwright), navigates each
`/custom/<page>` board, and captures the site's OWN
`POST /api/Jobad/GetJobAdPageList` JSON response — so it reads exactly the
data the page reads, nothing scraped out of the DOM.

Unlike the pure-HTTP connectors this one launches a browser subprocess, so it
is heavier and slower; the scrape layer runs browser-tier boards sparingly
(see directory.ingest). It is still a pure fetch→normalize→return function:
it owns no files and no state, and Playwright is imported lazily so the rest
of the package keeps working when the browser tier isn't installed.
"""

from __future__ import annotations

from datetime import datetime

from .models import BeisenBoard, FetchResult, Opportunity, VerificationResult

name = "beisen"

_NAV_TIMEOUT_MS = 30000
_SETTLE_MS = 2500
_LIST_MARKER = "GetJobAdPageList"
# Beisen's "no deadline" sentinel is a year-2222 EndTime with EndTimeInt 0.
_SENTINEL_YEAR = 2200


def _capture(resp, holder: dict) -> None:
    if _LIST_MARKER in resp.url:
        try:
            holder["data"] = resp.json()
        except Exception:  # noqa: BLE001 — a non-JSON body just isn't the list
            pass


def _biggest_job_list(payload) -> list:
    """The GetJobAdPageList response wraps the job array a few levels deep and
    alongside filter/condition arrays; pick the largest list whose items look
    like jobs (carry `JobAdName`)."""
    best: list = []

    def rec(x) -> None:
        nonlocal best
        if isinstance(x, list):
            if x and isinstance(x[0], dict) and "JobAdName" in x[0] and len(x) > len(best):
                best = x
            for v in x:
                rec(v)
        elif isinstance(x, dict):
            for v in x.values():
                rec(v)

    rec(payload)
    return best


def _job_url(host: str, job: dict) -> str:
    jid = job.get("JobAdId") or job.get("Id")
    return f"https://{host}/custom/jobDetail?jobId={jid}" if jid else ""


def _deadline(job: dict) -> str | None:
    if not job.get("EndTimeInt"):
        return None
    try:
        d = datetime.fromisoformat(job.get("EndTime") or "")
    except ValueError:
        return None
    return None if d.year >= _SENTINEL_YEAR else d.date().isoformat()


def _normalize(job: dict, board: BeisenBoard) -> Opportunity:
    locs = job.get("LocNames")
    location = " / ".join(locs) if isinstance(locs, list) else (locs or "")
    return Opportunity(
        firm=board.firm,
        title=job.get("JobAdName", ""),
        location=location,
        url=_job_url(board.host, job),
        source="beisen",
        deadline=_deadline(job),
        posted_at=(job.get("PostDate") or "")[:10] or None,
        raw=job,
    )


def fetch(board: BeisenBoard) -> FetchResult:
    """Drive a headless browser across `board.pages`, capturing each board's
    GetJobAdPageList response, and return the deduped, normalized postings."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0,
                           error="playwright not installed — browser tier unavailable")

    raw_jobs: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                for slug in board.pages:
                    holder: dict = {"data": None}
                    page = browser.new_page()
                    page.on("response", lambda r, h=holder: _capture(r, h))
                    try:
                        page.goto(f"https://{board.host}/custom/{slug}?&hideMenu=1",
                                  timeout=_NAV_TIMEOUT_MS, wait_until="networkidle")
                        page.wait_for_timeout(_SETTLE_MS)
                    finally:
                        page.close()
                    raw_jobs.extend(_biggest_job_list(holder["data"]) if holder["data"] else [])
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 — a browser/nav failure is a board-level failure
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e)[:200])

    seen: set = set()
    uniq: list[dict] = []
    for j in raw_jobs:
        key = j.get("JobAdId") or j.get("Id")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(j)

    opportunities = [_normalize(j, board) for j in uniq]
    return FetchResult(board=board, ok=True, opportunities=opportunities, raw_count=len(uniq))


def classify_url(url: str) -> dict | None:
    return {"url": url} if url and ".zhiye.com" in url else None


def verify(url: str) -> VerificationResult:
    """Beisen postings are browser-gated; a per-URL liveness check would mean
    launching a browser inside reverify, which is too heavy to run at scale.
    Item URLs are verified at fetch time, so reverify treats them as
    needs-verification (same posture as the API tier's board-level checks)."""
    if classify_url(url) is None:
        return VerificationResult("beisen", url, "needs-verification",
                                  "URL is not a recognized beisen (zhiye.com) URL", [])
    return VerificationResult("beisen", url, "needs-verification",
                              "beisen postings are browser-gated; verified at fetch time", [])
