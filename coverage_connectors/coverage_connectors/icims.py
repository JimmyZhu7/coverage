"""iCIMS connector — server-rendered career-portal search pages.

iCIMS portals ({tenant}.icims.com) serve their job list as plain HTML when
asked with `ss=1&in_iframe=1` — the widget variant the portals themselves
embed, which skips the JS shell the bare /jobs/search page bootstraps.
Each listing is an `iCIMS_JobCardItem` `<li>` whose title anchor carries a
canonical `/jobs/<id>/<slug>/job` URL (verified live against careers-sig
and careers-stifel, 2026-08-08). Pagination is `&pr=<page>`; the sweep
stops on the first page that yields no NEW ids, since iCIMS repeats the
last page for out-of-range `pr` values rather than 404ing.

The list page carries no location and no deadline — the detail page does
(title renders "<role> in <city>", JSON-LD carries `datePosted`), which is
exactly what the enrich pass reads later. `verify()` fetches the posting
page: a 404 is a definitive CLOSED (iCIMS removes filled reqs), JSON-LD
`datePosted` is returned as posted-date evidence.

Like every connector here this is the same unauthenticated request the
public site makes for any visitor. Bot-challenge detection mirrors
talnet.py: a challenge page is unreadable, not empty.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import urllib.error

from .http import BOT_BLOCK_PREFIX, bot_challenge_reason, fetch_text, unreadable
from .models import FetchResult, IcimsBoard, Opportunity, VerificationResult

name = "icims"

# Positive content, the same shape talnet.py's `_VACANCY_MARKUP_RE` takes: a
# page still rendering job cards while `_ANCHOR_RE` matches none of them has
# had its markup changed under us, and reporting that as "this tenant has no
# openings" is what the guard exists to prevent. Keyed on the card container
# and the row-level classes rather than on `iCIMS_Anchor` alone, because the
# anchor class is what the parser already looks for — a marker that can only
# be present when the parse succeeded would never fire.
_JOB_MARKUP_RE = re.compile(
    r"iCIMS_JobsTable|iCIMS_JobCardItem|iCIMS_JobListing|data-job-id=",
    re.IGNORECASE,
)
# iCIMS' own empty-results panel. A portal with nothing posted renders this
# and no cards, which is a statement, not a silence.
_NO_RESULTS_RE = re.compile(
    r"iCIMS_NoResults|no\s+jobs?\s+(?:were\s+)?found"
    r"|no\s+(?:matching\s+)?(?:jobs?|results?|openings?|positions?)"
    r"\s+(?:were\s+)?(?:found|match|available)"
    r"|there\s+are\s+no\s+(?:open\s+)?(?:jobs?|positions?)",
    re.IGNORECASE,
)

# Sane upper bound on the pr= sweep — 40 pages of ~12 rows is far above any
# single-tenant board seen live; this is a runaway guard, not a coverage cap.
_MAX_PAGES = 40

_ANCHOR_RE = re.compile(
    r'<a href="(?P<url>https://[^"]+/jobs/(?P<id>\d+)/[^"]+?/job)[^"]*"'
    r'[^>]*class="iCIMS_Anchor"[^>]*title="(?P<title>[^"]*)"',
    re.DOTALL,
)
# Anchor title renders "10966 - Accounting Internship: Summer 2027" — strip
# the leading req id so the row title matches what the page's <h3> shows.
_TITLE_PREFIX_RE = re.compile(r"^\d+\s*-\s*")
_JSONLD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)
_TITLE_TAG_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)
_JOB_URL_RE = re.compile(r"https://([\w-]+)\.icims\.com/jobs/\d+/[^/?#]+/job", re.IGNORECASE)


def _list_url(board: IcimsBoard, page: int) -> str:
    return (f"https://{board.tenant}.icims.com/jobs/search"
            f"?ss=1&in_iframe=1&pr={page}")


def _parse(html: str) -> list[dict]:
    rows = []
    for m in _ANCHOR_RE.finditer(html):
        title = html_mod.unescape(_TITLE_PREFIX_RE.sub("", m.group("title")).strip())
        if not title:
            continue
        # Drop the ?in_iframe=1 the widget appends — /jobs/<id>/<slug>/job is
        # the stable canonical posting URL.
        rows.append({"id": m.group("id"), "title": title, "url": m.group("url")})
    return rows


def fetch(board: IcimsBoard) -> FetchResult:
    seen: set[str] = set()
    rows: list[dict] = []
    # True when we run out of allowed pages while pages were still yielding
    # new rows — the board has more than we read.
    truncated = False
    empty_state = False
    try:
        for page in range(_MAX_PAGES):
            html = fetch_text(_list_url(board, page))
            challenge = bot_challenge_reason(html)
            if challenge:
                return FetchResult(
                    board=board, ok=False, opportunities=[], raw_count=0,
                    error=f"{BOT_BLOCK_PREFIX} ({challenge}) — board unreadable, not empty",
                )
            fresh = [r for r in _parse(html) if r["id"] not in seen]
            if not fresh and page == 0:
                # Nothing on the FIRST page. Three possibilities and they are
                # not interchangeable: the portal says it has no jobs, the
                # portal is rendering jobs this parser can no longer read, or
                # the response is not a job list at all. Only the first is an
                # empty board.
                if _NO_RESULTS_RE.search(html):
                    empty_state = True
                elif _JOB_MARKUP_RE.search(html):
                    return FetchResult(
                        board=board, ok=False, opportunities=[], raw_count=0,
                        error=unreadable(
                            f"icims tenant {board.tenant!r} renders job-card markup "
                            f"but no listing parsed — card layout changed"),
                    )
                else:
                    return FetchResult(
                        board=board, ok=False, opportunities=[], raw_count=0,
                        error=unreadable(
                            f"icims tenant {board.tenant!r} answered {len(html)} bytes "
                            f"with neither job cards nor a no-results panel"),
                    )
            if not fresh:
                break
            seen.update(r["id"] for r in fresh)
            rows.extend(fresh)
        else:
            # Every allowed page returned new rows and none was the last.
            truncated = True
        opportunities = [
            Opportunity(firm=board.firm, title=r["title"], location="",
                        url=r["url"], source="icims", raw=r)
            for r in rows
        ]
    except Exception as e:  # noqa: BLE001 — board-level failure, not fatal to the run
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities,
                       raw_count=len(rows), truncated=truncated,
                       empty_state=not opportunities and empty_state)


def classify_url(url: str) -> dict | None:
    m = _JOB_URL_RE.match(url or "")
    return {"tenant": m.group(1)} if m else None


def verify(url: str) -> VerificationResult:
    if not classify_url(url):
        return VerificationResult("icims", url, "needs-verification",
                                   "URL is not a recognized iCIMS posting URL", [])
    try:
        # The widget variant is the one carrying JSON-LD; the bare /job page
        # serves a JS shell with no structured data.
        html = fetch_text(url + ("&" if "?" in url else "?") + "in_iframe=1")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            # iCIMS answers 410 Gone (observed live) or 404 for filled and
            # expired reqs — either way the posting no longer exists.
            return VerificationResult("icims", url, "closed", f"HTTP {e.code}", [])
        return VerificationResult("icims", url, "unreachable", f"HTTP {e.code}", [])
    except Exception as e:  # noqa: BLE001
        return VerificationResult("icims", url, "unreachable", str(e)[:200], [])

    challenge = bot_challenge_reason(html)
    if challenge:
        return VerificationResult("icims", url, "unreachable",
                                   f"{BOT_BLOCK_PREFIX} ({challenge})", [])

    posted = None
    for m in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(html_mod.unescape(m.group(1)))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            posted = (data.get("datePosted") or "")[:10] or None
            break

    title_m = _TITLE_TAG_RE.search(html)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
    return VerificationResult("icims", url, "verified-open",
                               f'title="{title}"', [], posted_date=posted)
