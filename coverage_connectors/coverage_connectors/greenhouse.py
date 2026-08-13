"""Greenhouse connector — `boards-api.greenhouse.io` public JSON.

Ported from the original `sources.py` (`_ATS_URL["greenhouse"]`,
`_ats_normalize`'s greenhouse branch) and `verify_rows.py`
(`_verify_greenhouse`, `_GREENHOUSE_RE`). No state, no filtering: `fetch()`
returns every posting currently on the board, normalized; the caller decides
what's relevant (role/region filtering is deliberately not this package's
job — see the package README).

One factual update beyond the original, found live while re-verifying this
board (2026-07-23): Greenhouse's `boards-api` now returns an
`application_deadline` field on every job, including in the list endpoint —
`null` when the firm hasn't set one (true for every job checked on
williamblair's board today), populated when they have. The original's
comment that "Greenhouse exposes no deadline field at all" was accurate when
written; this connector reads the field where the original couldn't, since
it's a literal API value, not a guess.

Known limitation, carried over unchanged from the original: `verify()`
classifies the candidate-facing `job-boards.greenhouse.io` /
`boards.greenhouse.io` URL shape, plus a short list of known custom-domain
embeds (see `_CUSTOM_DOMAIN_TOKENS` below) — never the internal
`boards-api.greenhouse.io` JSON endpoint `fetch()` itself calls, and never an
UNLISTED custom domain. Some firms point Greenhouse's `absolute_url` at their
own site with the job id recoverable only from a `gh_jid=` query param
(confirmed live: williamblair's board does this — all 48 open rows use
`https://www.williamblair.com/Careers/job-description?gh_jid=<id>`, and every
one of them classified as `provider='unknown'` before the mapping below
existed, which meant `reverify`'s single-URL liveness backstop — the layer
that catches a posting dying in a way `scrape`'s board-diff closed-detection
never sees — could never usefully check a single William Blair row). A
custom domain carries no token of its own to read out of the URL, so this is
only closeable for domains this package already knows the token for (from
`directory/boards.py`'s own board registration) — guessing a token from an
arbitrary third-party domain would be worse than "needs-verification".
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse

from .http import fetch_json
from .models import FetchResult, GreenhouseBoard, Opportunity, VerificationResult

name = "greenhouse"

_BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
_JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"

# Matches both the legacy "boards.greenhouse.io" and current
# "job-boards.greenhouse.io" hosts, same as the original's _GREENHOUSE_RE.
_BOARD_URL_RE = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)(?:/jobs/(\d+))?", re.IGNORECASE
)

# Firms whose own site embeds Greenhouse's board under a custom domain, with
# the job id recoverable only from a `gh_jid=` query param and the token
# nowhere in the url. Each entry here has to be confirmed against a real
# board registration in `directory/boards.py`, not guessed — see module
# docstring.
_CUSTOM_DOMAIN_TOKENS = {
    "www.williamblair.com": "williamblair",
    "williamblair.com": "williamblair",
}
_GH_JID_RE = re.compile(r"[?&]gh_jid=(\d+)")


def _normalize(job: dict, board: GreenhouseBoard) -> Opportunity:
    deadline = job.get("application_deadline") or None
    return Opportunity(
        firm=board.firm,
        title=job.get("title", ""),
        location=(job.get("location") or {}).get("name", ""),
        url=job.get("absolute_url", ""),
        source="greenhouse",
        deadline=deadline[:10] if deadline else None,
        posted_at=(job.get("updated_at") or "")[:10] or None,
        raw=job,
    )


def fetch(board: GreenhouseBoard) -> FetchResult:
    """One board fetch: GET the board's full jobs list, normalize every
    row. Ported from `sources.py`'s generic branch in `_fetch_board`
    (`data = _fetch(_ATS_URL[provider].format(t=board["token"]))`)."""
    url = _BOARD_URL.format(token=board.token)
    try:
        data = fetch_json(url)
        jobs = data.get("jobs", data if isinstance(data, list) else [])
        # Normalization lives INSIDE this try, not after it: `_normalize`
        # does `(job.get("location") or {}).get("name", "")`, which raises
        # AttributeError the moment a single job's `location` ever arrives
        # as a string instead of a dict. Outside the try, that exception
        # would propagate uncaught out of `fetch()`, and `fetch_many`'s
        # `list(pool.map(fetch, boards))` re-raises the first exception it
        # sees — discarding every OTHER board's already-fetched results too,
        # not just this one's. One malformed row must cost this board a
        # clean `ok=False`, never the whole run.
        opportunities = [_normalize(j, board) for j in jobs]
    except Exception as e:  # noqa: BLE001 — board-level failure, not fatal to a multi-board run
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities, raw_count=len(jobs))


def classify_url(url: str) -> dict | None:
    """{"token", "job_id"} if `url` is a recognized Greenhouse board/job
    URL, else None. `job_id` is None for a bare board URL with no
    `/jobs/<id>` path."""
    m = _BOARD_URL_RE.search(url or "")
    if m:
        return {"token": m.group(1), "job_id": m.group(2)}
    token = _CUSTOM_DOMAIN_TOKENS.get(urllib.parse.urlsplit(url or "").netloc.lower())
    if not token:
        return None
    jid = _GH_JID_RE.search(url or "")
    return {"token": token, "job_id": jid.group(1)} if jid else None


def verify(url: str) -> VerificationResult:
    """Ported from `verify_rows.py`'s `_verify_greenhouse`: a job-detail
    fetch by id. 404 means the posting is gone; any other fetch failure is
    "unreachable" rather than "closed" (an ambiguous network error should
    never be reported as a confident negative)."""
    info = classify_url(url)
    if not info:
        return VerificationResult("greenhouse", url, "needs-verification",
                                   "URL is not a recognized greenhouse board/job URL", [])
    token, job_id = info["token"], info["job_id"]
    if not job_id:
        return VerificationResult(
            "greenhouse", url, "needs-verification",
            f"greenhouse token {token!r} but no /jobs/<id> in the URL — can't target a requisition", [],
        )
    try:
        data = fetch_json(_JOB_URL.format(token=token, job_id=job_id))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return VerificationResult("greenhouse", url, "closed",
                                       f"boards-api 404 for job {job_id} — no longer listed", [])
        return VerificationResult("greenhouse", url, "unreachable", f"HTTP {e.code} from boards-api", [])
    except Exception as e:  # noqa: BLE001
        return VerificationResult("greenhouse", url, "unreachable", str(e)[:200], [])

    title = data.get("title", "")
    updated = (data.get("updated_at") or "")[:10]
    deadline = data.get("application_deadline") or None
    # updated_at is a last-MODIFIED timestamp, not a deadline -- evidence-only,
    # never a "changed" comparator (matches the original's "Date-type
    # awareness" rule). application_deadline, when the firm sets it, IS a
    # genuine deadline and feeds deadline_dates.
    deadline_dates = [deadline[:10]] if deadline else []
    evidence = f'title="{title}" updated_at={updated}'
    if deadline:
        evidence += f" application_deadline={deadline[:10]}"
    return VerificationResult("greenhouse", url, "verified-open", evidence, deadline_dates,
                               posted_date=updated or None)
