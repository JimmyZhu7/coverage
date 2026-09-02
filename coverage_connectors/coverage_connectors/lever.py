"""Lever connector — `api.lever.co` public JSON.

Ported from the original `sources.py` (`_ATS_URL["lever"]`,
`_ats_normalize`'s lever branch) and `verify_rows.py` (`_verify_lever`,
`_LEVER_RE`). Note on provenance: no firm in the original's `_ATS_BOARDS` list
actually used the `lever` provider — the normalize/verify code existed but
was never wired to a live board, so this connector is exercised against a
real Lever tenant for the first time as part of this extraction (see the
package's live-smoke tests, which use `palantir`, a real, currently-active
public Lever board — one of the largest found while probing for a live
example).

One factual addition beyond the original: the original's lever normalize set
`"posted": ""` unconditionally. Lever's postings actually carry a
`createdAt` epoch-millisecond timestamp; this connector converts it to an
ISO date for `posted_at` since it's a literal API value sitting unused in
the original's own `job` dict, not a guess.
"""

from __future__ import annotations

import re
import urllib.error
from datetime import UTC, datetime

from .http import fetch_json, unreadable
from .models import FetchResult, LeverBoard, Opportunity, VerificationResult

name = "lever"

_POSTINGS_URL = "https://api.lever.co/v0/postings/{org}?mode=json"
# Matches only the candidate-facing jobs.lever.co host, exactly like the
# original's _LEVER_RE -- api.lever.co's own path shape
# ("api.lever.co/v0/postings/{org}") puts the org three segments deep, not
# in the same position as jobs.lever.co/{org}, so a single pattern can't
# safely cover both without misparsing one of them.
_BOARD_URL_RE = re.compile(r"jobs\.lever\.co/([^/?#]+)", re.IGNORECASE)


def _posted_at(job: dict) -> str | None:
    created_ms = job.get("createdAt")
    if not isinstance(created_ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(created_ms / 1000, tz=UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _normalize(job: dict, board: LeverBoard) -> Opportunity:
    return Opportunity(
        firm=board.firm,
        title=job.get("text", ""),
        location=(job.get("categories") or {}).get("location", ""),
        url=job.get("hostedUrl", ""),
        source="lever",
        posted_at=_posted_at(job),
        raw=job,
    )


def fetch(board: LeverBoard) -> FetchResult:
    """One board fetch: GET every live posting for `board.org`. Lever's
    postings endpoint (unlike Greenhouse/Workday) is already the complete,
    unpaginated live list — no offset/limit contract to preserve."""
    url = _POSTINGS_URL.format(org=board.org)
    try:
        data = fetch_json(url)
        # `mode=json` on a live org returns a JSON ARRAY, always. Anything
        # else — an error object, a redirect body, a rate-limit envelope —
        # used to fall through `isinstance(data, list)` into an empty list and
        # a confident `ok=True, 0 rows`, which for Lever (whose whole list is
        # one unpaginated response) reads as "this org has closed every
        # posting". Nothing may be concluded from a shape we did not expect.
        if not isinstance(data, list):
            return FetchResult(
                board=board, ok=False, opportunities=[], raw_count=0,
                error=unreadable(
                    f"lever postings API returned {type(data).__name__}, not the "
                    f"documented array"),
            )
        jobs = data
        # Normalization stays inside this try — see greenhouse.py's fetch()
        # for why: `_normalize` does `(job.get("categories") or {}).get(
        # "location", "")`, which raises if `categories` ever arrives as
        # something other than a dict, and an uncaught exception here would
        # make `fetch_many` discard every OTHER board's results, not just
        # this one's.
        opportunities = [_normalize(j, board) for j in jobs]
    except Exception as e:  # noqa: BLE001
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities, raw_count=len(jobs))


def classify_url(url: str) -> dict | None:
    m = _BOARD_URL_RE.search(url or "")
    if not m:
        return None
    return {"org": m.group(1)}


def verify(url: str) -> VerificationResult:
    """Ported from `verify_rows.py`'s `_verify_lever`. Board-level only,
    exactly like the original: Lever's public postings API has no
    single-posting-by-id endpoint the original ever used, so this checks
    "does the board still list *any* live postings", not "is this specific
    requisition still open" — carried over as a known limitation, not
    silently upgraded, since a hidden behavior change here would be exactly
    the kind of undocumented divergence this extraction is told to avoid."""
    info = classify_url(url)
    if not info:
        return VerificationResult("lever", url, "needs-verification",
                                   "URL is not a recognized jobs.lever.co URL", [])
    org = info["org"]
    try:
        data = fetch_json(_POSTINGS_URL.format(org=org))
    except urllib.error.HTTPError as e:
        return VerificationResult("lever", url, "unreachable", f"HTTP {e.code} from Lever postings API", [])
    except Exception as e:  # noqa: BLE001
        return VerificationResult("lever", url, "unreachable", str(e)[:200], [])

    if not data:
        return VerificationResult(
            "lever", url, "closed",
            f"postings API lists 0 live postings for {org} (board-level check, not item-specific)", [],
        )
    titles = "; ".join(p.get("text", "") for p in data[:3])
    return VerificationResult(
        "lever", url, "verified-open",
        f"{len(data)} live posting(s) on the board (board-level, not item-specific): {titles}", [],
    )
