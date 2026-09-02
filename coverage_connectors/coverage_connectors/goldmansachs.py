"""Goldman Sachs connector — the higher.gs.com careers GraphQL API.

Discovered live (2026-07-23) by extracting the `GetCampusRoles` operation
straight from higher.gs.com's own Next.js bundle (the SPA's GraphQL client
holds a private fetch reference, so a runtime hook can't see the request —
the query text and the `RoleSearchQueryInput` shape were read from the
minified chunk instead). Endpoint:
`https://api-higher.gs.com/gateway/api/v1/graphql`, a plain unauthenticated
POST (re-verified from a clean urllib client: `experiences:["CAMPUS"]`
returns 152 campus roles).

Fidelity notes:

- `jobTitle` is a pipe-delimited string GS builds itself:
  "YEAR | REGION | CITY | FUNCTION | ROLE TYPE". The connector keeps the
  FUNCTION and ROLE TYPE segments (the classifiable, human part) and drops
  the year/region/city prefix, which is duplicated in the location field.
- `roleId` (e.g. "180086_GS_CAMPUS") is the path segment for the
  candidate-facing URL `higher.gs.com/roles/{roleId}`.
- No deadline is exposed anywhere in the schema, so deadline is always None
  (never invented). `startDate` is an API bookkeeping timestamp, not a
  posted date, and is ignored.
"""

from __future__ import annotations

import json
import re
import urllib.error

from .http import fetch_json, unreadable
from .models import FetchResult, GoldmanSachsBoard, Opportunity, VerificationResult

name = "goldmansachs"

_ENDPOINT = "https://api-higher.gs.com/gateway/api/v1/graphql"
_PAGE_SIZE = 50
_MAX_ROLES = 400

_QUERY = (
    "query GetCampusRoles($searchQueryInput: RoleSearchQueryInput!) { "
    "roleSearch(searchQueryInput: $searchQueryInput) { totalCount items { "
    "roleId jobTitle corporateTitle division "
    "locations { city state country primary } } } }"
)

_URL_RE = re.compile(r"higher\.gs\.com/roles/([^/?#]+)", re.IGNORECASE)


def _post(page_number: int) -> dict:
    body = {
        "operationName": "GetCampusRoles",
        "query": _QUERY,
        "variables": {"searchQueryInput": {
            "page": {"pageSize": _PAGE_SIZE, "pageNumber": page_number},
            "experiences": ["CAMPUS"],
            "searchTerm": "",
        }},
    }
    data = fetch_json(
        _ENDPOINT,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method_label="POST",
    )
    if data.get("errors"):
        raise ValueError(f"GraphQL errors: {data['errors']}")
    return (data.get("data") or {}).get("roleSearch") or {}


def _title(job_title: str) -> str:
    """Drop the "YEAR | REGION | CITY" prefix, keep the FUNCTION | ROLE TYPE
    tail (which carries the words the classifier keys on)."""
    parts = [p.strip() for p in (job_title or "").split("|")]
    if len(parts) >= 4:
        return " — ".join(p for p in parts[3:] if p)
    return job_title or ""


def _location(item: dict) -> str:
    for loc in item.get("locations") or []:
        if loc.get("primary"):
            return loc.get("city") or loc.get("state") or loc.get("country") or ""
    locs = item.get("locations") or []
    if locs:
        loc = locs[0]
        return loc.get("city") or loc.get("state") or loc.get("country") or ""
    return ""


def _normalize(item: dict, board: GoldmanSachsBoard) -> Opportunity:
    rid = item.get("roleId") or ""
    return Opportunity(
        firm=board.firm,
        title=_title(item.get("jobTitle", "")),
        location=_location(item),
        url=f"https://higher.gs.com/roles/{rid}" if rid else "",
        source="goldmansachs",
        raw=item,
    )


def fetch(board: GoldmanSachsBoard) -> FetchResult:
    seen: set[str] = set()
    items: list[dict] = []
    page = 0
    truncated = False
    while True:
        try:
            rs = _post(page)
        except Exception as e:  # noqa: BLE001
            return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
        # higher.gs.com's GraphQL layer answers a rejected or renamed query
        # with a 200 and a body carrying no `items` at all. This is Goldman's
        # entire campus board — one board, one firm — so a clean zero here
        # empties the firm.
        if "items" not in rs:
            return FetchResult(
                board=board, ok=False, opportunities=[], raw_count=0,
                error=unreadable(
                    f"higher.gs.com answered 200 with no 'items' key "
                    f"(got {sorted(rs)[:6]})"),
            )
        batch = rs.get("items", [])
        stated_total = rs.get("totalCount")
        if not batch and page == 0 and isinstance(stated_total, int) and stated_total > 0:
            return FetchResult(
                board=board, ok=False, opportunities=[], raw_count=0,
                error=unreadable(
                    f"higher.gs.com reported totalCount={stated_total} and "
                    f"returned 0 items"),
            )
        for it in batch:
            rid = it.get("roleId") or ""
            if not rid or rid in seen:
                continue
            seen.add(rid)
            items.append(it)
        total = int(rs.get("totalCount") or 0)
        page += 1
        if not batch or page * _PAGE_SIZE >= min(total, _MAX_ROLES):
            # `min(total, _MAX_ROLES)` hides which of the two ended the walk.
            # If the cap did, the board still has roles we did not read, and
            # ingest must not conclude anything from their absence.
            truncated = bool(batch) and total > _MAX_ROLES
            break
    try:
        # Its own try, separate from the per-page network try above — see
        # greenhouse.py's fetch() for why a normalization failure must not
        # propagate uncaught out of `fetch()`.
        opportunities = [o for o in (_normalize(i, board) for i in items) if o.url]
    except Exception as e:  # noqa: BLE001
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities,
                       raw_count=len(items), truncated=truncated,
                       empty_state=not opportunities and total == 0)


def classify_url(url: str) -> dict | None:
    m = _URL_RE.search(url or "")
    return {"role_id": m.group(1)} if m else None


def verify(url: str) -> VerificationResult:
    """Re-run the campus search and look for the URL's roleId. Present ->
    verified-open; network trouble -> unreachable.

    A clean search without it -> `needs-verification`, NOT closed: `_post`
    returns `{}` whenever `data.roleSearch` comes back null-but-error-free
    (a GraphQL response can do this without ever raising), and `{}.get(
    "totalCount") or 0` reads as `total=0` — which ends the loop after page 0
    having found nothing, for every single role in one sweep. That is not a
    "this role is gone" signal, it's "the API gave us an empty envelope",
    and `reverify.py` acts on "closed" with zero corroboration."""
    info = classify_url(url)
    if not info:
        return VerificationResult("goldmansachs", url, "needs-verification",
                                   "URL is not a recognized higher.gs.com role URL", [])
    target = info["role_id"]
    page = 0
    try:
        while page * _PAGE_SIZE < _MAX_ROLES:
            rs = _post(page)
            for it in rs.get("items", []):
                if (it.get("roleId") or "") == target:
                    return VerificationResult("goldmansachs", url, "verified-open",
                                               f'roleId {target} present in campus search', [])
            total = int(rs.get("totalCount") or 0)
            page += 1
            if page * _PAGE_SIZE >= total:
                break
    except urllib.error.HTTPError as e:
        return VerificationResult("goldmansachs", url, "unreachable", f"HTTP {e.code}", [])
    except Exception as e:  # noqa: BLE001
        return VerificationResult("goldmansachs", url, "unreachable", str(e)[:200], [])
    return VerificationResult(
        "goldmansachs", url, "needs-verification",
        f"roleId {target} not in campus search — not a confirmed closure "
        f"(an empty roleSearch envelope reads identically to a genuine "
        f"zero-result search)", [],
    )
