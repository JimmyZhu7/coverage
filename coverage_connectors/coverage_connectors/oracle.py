"""Oracle Recruiting Cloud connector — public, unauthenticated REST.

Ported from the radar's `sources.py` (`_ORACLE_URL`, `_fetch_oracle`,
`_ats_normalize`'s oracle branch) and `verify_rows.py` (`_verify_oracle`,
the oracle arm of `_URL_RES`). Same public trust tier as the
Greenhouse/Lever/Workday boards-api calls: GET with the keyword in the
finder expression, no auth, no cookies.

Fetch contract: one search per configured keyword (Oracle's finder has no
OR-of-terms syntax that reliably widens results — the career site's own
search box behaves the same way), deduplicated by requisition Id across
searches.

CONFIRMED DEFECT, fixed here for J.P. Morgan only (2026-08-29): `PostingEndDate`
off the SEARCH endpoint (`recruitingCEJobRequisitions`) is documented above (in
the original module docstring) as "a GENUINE deadline when present" — that was
wrong. It is ALWAYS null. Verified live against Oracle's own API for two J.P.
Morgan requisitions:

    210778140: search PostingEndDate=null | details ExternalPostedEndDate=
               2026-09-07T15:59:00+00:00 — matches the live posting page's
               "Apply Before 09/07/2026, 08:59 AM" exactly (tz offset
               accounted for)
    210747420: details ExternalPostedEndDate=2026-09-30T15:59:00+00:00 —
               matches the live page's "Apply Before 09/30/2026" exactly

The real "Apply Before" date lives on the DETAILS endpoint
(`recruitingCEJobRequisitionDetails`) under `ExternalPostedEndDate`. This
connector now makes one extra per-requisition DETAILS request to read it —
but ONLY for firms in `_EXTERNAL_DEADLINE_HOSTS` below. For every other
Oracle firm (Lazard, Schroders as of this writing), Oracle's API populates
`ExternalPostedEndDate` too, but a human check of each firm's own live
posting page found no deadline shown to candidates there — the field reads
as internal/administrative for those two, not a real application deadline.
See `_EXTERNAL_DEADLINE_HOSTS` for the full reasoning; do not extend that set
without the same live-page confirmation. `PostedDate` remains evidence-only
everywhere.
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
_DETAILS_URL = (
    "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
    "?onlyData=true&expand=requisitionList&finder=ById;Id=%22{rid}%22,siteNumber={site}"
)
_JOB_URL = "https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}"

# Oracle tenant hosts confirmed BY A HUMAN, on the firm's own live candidate
# posting page, to actually display `ExternalPostedEndDate` (read off the
# DETAILS endpoint) as the "Apply Before" deadline. Oracle's API returns this
# field for Lazard ("icbpjb.fa.ocs.oraclecloud.com") and Schroders
# ("ekbq.fa.em2.oraclecloud.com") too, but a human check of both firms' live
# posting pages found NO deadline displayed to candidates there — the field
# reads as internal/administrative for those two, not a real application
# deadline. Putting an unverified, possibly-internal date on a card as "the
# deadline" would be worse than showing no deadline at all (this is a trust
# product).
#
# Each host below belongs to exactly one firm (see directory/boards.py's own
# board registrations), so gating on host is equivalent to gating on firm —
# and it is the only firm-identifying signal `verify()` has, since it takes a
# bare URL rather than a `board.firm` (`fetch()` gates the identical set the
# same way, off `board.host`, so the two entry points can never drift apart).
#
# DO NOT add another host here "because it's obviously the same field" — the
# field being populated in Oracle's API is NOT sufficient evidence that it's
# candidate-facing. Extend this set only after a human has confirmed, on
# that firm's own live posting page, that `ExternalPostedEndDate` is
# genuinely shown to candidates as a deadline there.
_EXTERNAL_DEADLINE_HOSTS = {"jpmc.fa.oraclecloud.com"}  # J.P. Morgan only

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


def _fetch_details(host: str, site: str, rid: str) -> dict | None:
    """Best-effort fetch of one requisition's DETAILS payload — the only
    place `ExternalPostedEndDate` (the real candidate-facing "Apply Before"
    date) lives; the SEARCH endpoint's own `PostingEndDate` is always null.
    Returns None on any fetch/parse failure or a shape `_search` would also
    treat as "can't tell" — a missing deadline enrichment is never worth
    failing the whole board fetch or verify call over, so callers use this
    as a pure add-on, never a signal of anything else."""
    url = _DETAILS_URL.format(host=host, site=site, rid=urllib.parse.quote(str(rid)))
    try:
        data = fetch_json(url)
    except Exception:  # noqa: BLE001 — best-effort deadline enrichment only
        return None
    items = data.get("items", [])
    if not items:
        return None
    reqs = items[0].get("requisitionList", [])
    return reqs[0] if reqs else None


def _normalize(req: dict, board: OracleBoard) -> Opportunity:
    rid = str(req.get("Id") or "")
    # `_details_deadline` is stashed onto `req` by `fetch()`, only for hosts
    # in `_EXTERNAL_DEADLINE_HOSTS`, from the DETAILS endpoint's
    # `ExternalPostedEndDate` — the field `PostingEndDate` (below) never
    # actually carries (see module docstring). Everywhere else this key is
    # absent and behavior is exactly what it was before this fix.
    deadline = req.get("_details_deadline") or req.get("PostingEndDate") or None
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
            if board.host in _EXTERNAL_DEADLINE_HOSTS:
                # One extra request per requisition — paid only for the
                # single firm this has been human-confirmed for (see
                # `_EXTERNAL_DEADLINE_HOSTS`). `_fetch_details` swallows its
                # own errors, so a transient failure here costs this one row
                # its deadline, never the whole board fetch.
                details = _fetch_details(board.host, board.site_number, rid)
                if details and details.get("ExternalPostedEndDate"):
                    req = {**req, "_details_deadline": details["ExternalPostedEndDate"]}
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
    # PostingEndDate off the SEARCH result is always null (see module
    # docstring) — kept as the fallback field name/label so behavior for
    # every firm outside `_EXTERNAL_DEADLINE_HOSTS` is byte-for-byte
    # unchanged. Only for a host in that set do we pay for the extra
    # DETAILS request and prefer its `ExternalPostedEndDate` instead; this
    # is the same reverify path, so without this gate applying here too, a
    # reverify pass could never self-heal a previously-null deadline.
    end_field = "PostingEndDate"
    end = (match.get("PostingEndDate") or "")[:10]
    if host in _EXTERNAL_DEADLINE_HOSTS:
        details = _fetch_details(host, site, job_id)
        if details:
            details_end = (details.get("ExternalPostedEndDate") or "")[:10]
            if details_end:
                end = details_end
                end_field = "ExternalPostedEndDate"
    # PostedDate is a POSTED date, never a deadline — evidence-only.
    # end (PostingEndDate, or ExternalPostedEndDate where in-scope) IS a
    # genuine deadline and feeds deadline_dates.
    return VerificationResult(
        "oracle", url, "verified-open",
        f'title="{title}" PostedDate={posted or "unstated"} {end_field}={end or "unstated"}',
        [end] if end else [],
        posted_date=posted or None,
    )
