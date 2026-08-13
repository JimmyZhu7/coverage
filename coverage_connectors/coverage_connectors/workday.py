"""Workday connector — the `wday/cxs/.../jobs` paginated JSON endpoint.

Ported from the original `sources.py` (`_fetch_workday_page`,
`_fetch_workday`, `_ats_normalize`'s workday branch) and `verify_rows.py`
(`_verify_workday`, `_WORKDAY_RE`). This is the dominant provider in the
original's own board list (~25 of its ~37 configured boards), so the
pagination contract is preserved exactly:

- Workday's CxS endpoint 400s outright above `limit=20` — no silent
  server-side clamp (verified live against the original's own boards) — so
  every page request uses `limit=20` regardless of how many results exist.
- The first page's reported `total` drives how many further pages to
  fetch, capped at `_MAX_JOBS` (60 — three pages) so one saturated tenant
  can't turn into an unbounded fetch.
- `tenant_host` is "{tenant}.{datacenter}" (e.g. "citi.wd5"); the tenant
  used in the URL path is always its first dot-segment.

Two bugs found and fixed while re-verifying this connector live against a
real board (Citi, 2026-07-23), both **not** carried forward from the
original:

1. **Dead posting URLs.** The original's `_ats_normalize` built the
   clickable URL as `tenant_host + externalPath` — e.g.
   `https://citi.wd5.myworkdayjobs.com/job/London--United-Kingdom/Citi-London-Military-Insight-Day_26979549`
   — which 404s (confirmed live). The real career-site URL needs the
   `site` slug between the host and `/job/…`:
   `https://citi.wd5.myworkdayjobs.com/Citi_Early_Careers_Events_Site/job/…`
   (confirmed live, HTTP 200). This connector's `_normalize` includes it.
2. **False "closed" verdicts in the URL-based verifier.** `verify_rows.py`'s
   `_WORKDAY_RE` captured the job path as `[^/?#]+` — one path segment.
   Real Workday job paths are two segments
   (`{location-slug}/{title-slug}_{reqId}`), so the original's regex
   silently truncated the second segment off every job path it classified.
   Re-querying the CxS job-detail endpoint with the truncated path returns
   a 404-shaped "not found" body with no `jobPostingInfo` — which
   `_verify_workday` reads as "posting removed" and reports **closed**,
   even though the posting is live (confirmed live: the full two-segment
   path returns 200 with real `jobPostingInfo`; the truncated one-segment
   path the original's regex captures returns the not-found body). Given
   this package's whole purpose is an honest liveness signal, silently
   porting a bug that flips "live" to "closed" would defeat the point;
   `_WORKDAY_URL_RE` below captures the full remaining path instead of
   truncating at the first `/`.

Both are documented in the project report as deliberate deviations from a
byte-for-byte port, not silent drift.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse

from .http import fetch_json, fetch_text, post_json
from .models import FetchResult, Opportunity, VerificationResult, WorkdayBoard

name = "workday"

_PAGE_SIZE = 20   # Workday's CxS API 400s above this — verified live, no silent clamp.
_MAX_JOBS = 2500  # 125 pages/board. Was 1500 (raised from 60, then 500 — see
                  # history below), which itself ran out of headroom: TD
                  # Securities' board grew from the 1,421 it was measured at
                  # to 1,587 (confirmed live 2026-08-13/14), tripping
                  # `truncated=True` on every fetch that landed above 1,500
                  # and blocking ingest's pair-level auto-close for the whole
                  # (TD Securities, workday) pair — 210 open rows that could
                  # no longer be auto-closed, confirmed against ScrapeRun
                  # history for the 'all' connector (runs 124, 176, 179, 182).
                  # A fixed cap sized to today's largest board is a fix that
                  # re-breaks itself the next time that board grows, so this
                  # is raised with real headroom (2,500 vs. TD's observed
                  # 1,587) rather than to the exact current total.
                  #
                  # Earlier history, preserved because the reasoning still
                  # applies at the new value: 60 (three pages) was not a
                  # coverage compromise but a correctness one — ingest closes
                  # any open row a successful fetch didn't return, so on
                  # boards reporting 186-1,371 results the cap was marking
                  # hundreds of LIVE postings closed every night. Two rows
                  # sampled from that population — a Barclays 2027 summer
                  # internship and a PwC FY27 intern role — came back
                  # verified-open from the firms' own sites while sitting in
                  # the database as closed. 500 came first and still left
                  # eight boards partial.
                  #
                  # The cap stays because an unbounded fetch against a
                  # 10,000-posting tenant is nobody's friend, and truncation
                  # remains safe regardless: it is reported via
                  # FetchResult.truncated and ingest declines to close
                  # anything on a list it knows is partial.

_WORKDAY_URL_RE = re.compile(
    # job_path is `[^?#]+`, not `[^/?#]+` -- a real Workday job path is two
    # segments ("{location-slug}/{title-slug}_{reqId}") and must not be
    # truncated at the first internal slash. See module docstring, bug (2).
    r"https?://([\w.-]+)\.myworkdayjobs\.com/([^/?#]+)(?:/job/([^?#]+))?", re.IGNORECASE
)


def _jobs_url(tenant_host: str, site: str, tenant: str = "", domain: str = "myworkdayjobs.com") -> str:
    t = tenant or tenant_host.split(".")[0]
    return f"https://{tenant_host}.{domain}/wday/cxs/{t}/{site}/jobs"


def _fetch_page(tenant_host: str, site: str, search_text: str, offset: int,
                tenant: str = "", domain: str = "myworkdayjobs.com") -> dict:
    payload = {"appliedFacets": {}, "limit": _PAGE_SIZE, "offset": offset, "searchText": search_text}
    return post_json(_jobs_url(tenant_host, site, tenant, domain), payload)


def _fetch_all(tenant_host: str, site: str, search_text: str = "",
               tenant: str = "", domain: str = "myworkdayjobs.com") -> dict:
    """Page through offset 0/20/40 (up to `_MAX_JOBS`) using the first
    page's reported `total`. Returns a dict shaped like a single Workday
    response (`{"total": N, "jobPostings": [...]}`), with `jobPostings`
    concatenated across every page actually fetched — same contract as the
    original's `_fetch_workday`."""
    first = _fetch_page(tenant_host, site, search_text, 0, tenant, domain)
    total = first.get("total", len(first.get("jobPostings", [])))
    postings = list(first.get("jobPostings", []))
    offset = _PAGE_SIZE
    while offset < min(total, _MAX_JOBS):
        page = _fetch_page(tenant_host, site, search_text, offset, tenant, domain)
        postings.extend(page.get("jobPostings", []))
        offset += _PAGE_SIZE
    return {**first, "jobPostings": postings}


def _normalize(job: dict, board: WorkdayBoard) -> Opportunity:
    path = job.get("externalPath", "")
    # Must include board.site between the host and the job path -- see
    # module docstring, bug (1). tenant_host + path alone 404s (confirmed
    # live against a real Citi posting).
    url = f"https://{board.tenant_host}.{board.domain}/{board.site}{path}" if path else ""
    return Opportunity(
        firm=board.firm,
        title=job.get("title", ""),
        location=job.get("locationsText", ""),
        url=url,
        source="workday",
        posted_at=job.get("postedOn") or None,
        raw=job,
    )


def fetch(board: WorkdayBoard) -> FetchResult:
    try:
        data = _fetch_all(board.tenant_host, board.site, board.search_text,
                          board.tenant, board.domain)
        jobs = data.get("jobPostings", [])
        # Kept inside this try — see greenhouse.py's fetch() for why: a
        # normalization failure on one malformed job must not propagate
        # uncaught, which would make `fetch_many`'s `list(pool.map(...))`
        # discard every OTHER board's already-fetched results too.
        opportunities = [_normalize(j, board) for j in jobs]
    except Exception as e:  # noqa: BLE001
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    total = data.get("total", len(jobs))
    return FetchResult(board=board, ok=True, opportunities=opportunities,
                        raw_count=total,
                        # The board says there are more than we read. Told to
                        # ingest rather than logged, because it changes what
                        # may be concluded from a row's absence.
                        truncated=isinstance(total, int) and total > len(jobs))


def classify_url(url: str) -> dict | None:
    """{"tenant_host", "site", "job_path", "search_text"} if `url` is a
    recognized myworkdayjobs.com URL, else None. `search_text` is read from
    a `?q=` query param if present (the same convention the original's
    verify layer used to carry a requisition-number re-query through a
    plain job-board URL)."""
    m = _WORKDAY_URL_RE.search(url or "")
    if not m:
        return None
    tenant_host, site, job_path = m.group(1), m.group(2), m.group(3)
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    search_text = (qs.get("q") or [None])[0]
    return {"tenant_host": tenant_host, "site": site, "job_path": job_path, "search_text": search_text}


# Cloudflare guards the CxS job-detail endpoint on some tenants and can 403
# it outright ("S22 permission denied") while the plain posting PAGE — the
# same url stored on the row — stays reachable and unblocked. Confirmed live
# 2026-08-13/14: TD Securities requisition R_1498964-1 (Opportunity id=17403,
# stored status='open') 403s from the CxS API on every attempt, but its
# posting page fetches 200 and embeds Workday's own client bootstrap JSON,
# `window.workday = {...}`, whose `postingAvailable` flag is what actually
# drives the page's client-rendered "this posting doesn't exist" state. That
# page read `postingAvailable: false`; a live sibling TD posting (id=17024)
# fetched the same way in the same run read `postingAvailable: true`, ruling
# out a rate-limit or per-tenant template artifact. Without this fallback,
# `verify()` can only ever answer "unreachable" for a row behind a
# Cloudflare-guarded CxS endpoint — including one that is genuinely gone —
# and `reverify.py` never acts on "unreachable", so the row stays open
# indefinitely no matter how many times it is re-checked.
_POSTING_AVAILABLE_RE = re.compile(r'"postingAvailable"\s*:\s*(true|false)', re.IGNORECASE)


def _posting_available_from_page(url: str) -> bool | None:
    """True/False from the posting page's own `postingAvailable` flag, or
    None when the page itself couldn't be read or doesn't carry the flag —
    never a guess."""
    try:
        page = fetch_text(url)
    except Exception:  # noqa: BLE001 — the page is unreachable too; let the caller fall back
        return None
    m = _POSTING_AVAILABLE_RE.search(page)
    if not m:
        return None
    return m.group(1).lower() == "true"


def verify(url: str) -> VerificationResult:
    """Ported from `verify_rows.py`'s `_verify_workday`: hits the CxS
    job-detail endpoint when the URL carries a job path, else re-runs a
    `searchText` query keyed off the URL's own `?q=<reqnum>` — 0 results
    means closed. Workday's CxS API exposes no deadline field at all, so
    `deadline_dates` is always `[]` here, matching the original."""
    info = classify_url(url)
    if not info:
        return VerificationResult("workday", url, "needs-verification",
                                   "URL is not a recognized myworkdayjobs.com URL", [])
    tenant_host, site = info["tenant_host"], info["site"]
    job_path, search_text = info.get("job_path"), info.get("search_text")
    try:
        if job_path:
            tenant = tenant_host.split(".")[0]
            detail_url = f"https://{tenant_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{job_path}"
            data = fetch_json(detail_url)
            posting = data.get("jobPostingInfo", {})
            title = posting.get("title", "")
            if not title:
                # An HTTP 200 with no `jobPostingInfo.title` is NOT a
                # positive "this posting is gone" signal — it is exactly as
                # consistent with a WAF/interstitial page, a rate-limit
                # envelope, a maintenance page, or Workday renaming a key,
                # all of which return 200 with a shape this code doesn't
                # recognize. Reporting "closed" here made this the
                # fallthrough for "I don't understand the response", and
                # `reverify.py` acts on "closed" with zero corroboration —
                # a one-shot deletion from the feed for the wrong reason.
                # Only a genuine gone-signal (a real 404, or a detail
                # endpoint that positively says "not found") may close a
                # row; an unrecognised-but-200 response must ask again
                # later instead.
                return VerificationResult("workday", url, "needs-verification",
                                           "job-detail endpoint returned no jobPostingInfo — "
                                           "unrecognized response shape, not a confirmed removal", [])
            posted = posting.get("postedOn", "")
            return VerificationResult("workday", url, "verified-open",
                                       f'title="{title}" postedOn={posted}', [], posted_date=posted or None)
        if search_text:
            data = _fetch_all(tenant_host, site, search_text)
            postings = data.get("jobPostings", [])
            total = data.get("total", len(postings))
            if total == 0:
                return VerificationResult("workday", url, "closed",
                                           f'searchText="{search_text}" returned 0 postings — no longer listed', [])
            titles = "; ".join(p.get("title", "") for p in postings[:2])
            dates = [p.get("postedOn", "") for p in postings if p.get("postedOn")]
            posted_date = "; ".join(dates)
            return VerificationResult("workday", url, "verified-open",
                                       f'searchText="{search_text}" -> {total} result(s): {titles}', [],
                                       posted_date=posted_date or None)
        return VerificationResult(
            "workday", url, "needs-verification",
            f"workday tenant {tenant_host!r}/{site!r} but no job path or ?q= requisition number in the URL", [],
        )
    except urllib.error.HTTPError as e:
        if job_path:
            # The CxS API itself is blocked -- fall back to the posting page
            # the row's own url already points at. See _posting_available_from_page.
            available = _posting_available_from_page(url)
            if available is False:
                return VerificationResult(
                    "workday", url, "closed",
                    f"CxS job-detail HTTP {e.code}; posting page's own "
                    "postingAvailable flag reads false", [])
            if available is True:
                return VerificationResult(
                    "workday", url, "verified-open",
                    f"CxS job-detail HTTP {e.code}; posting page's own "
                    "postingAvailable flag reads true", [])
        return VerificationResult("workday", url, "unreachable", f"HTTP {e.code} from Workday CxS", [])
    except Exception as e:  # noqa: BLE001
        return VerificationResult("workday", url, "unreachable", str(e)[:200], [])
