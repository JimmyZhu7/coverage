"""IBM BrassRing TalentGateway connector ("TgNewUI").

Cracked live (2026-07-24) on UBS's graduate board. The platform looked
session-gated — its `/Search/Ajax/HomeSearch` endpoint 403s / returns only
screening questions — but the actual job results are **embedded in the
initial server HTML**: a hidden `<input id="searchResults">` whose value is
the HTML-entity-encoded `{"HotJobs":{"Job":[...]}}` JSON. So a plain GET of
the board page, unescape, parse — no browser, no token, no session dance.

Contract:
- `partner_id` + `site_id` identify the board (UBS graduate = 25008 / 5131).
  A firm can run several boards (experienced vs graduate); pass the campus
  one.
- Each job's fields live in a `Questions` array of {QuestionName, Value}
  pairs. `jobtitle` is the title, `formtext23` the location (country/office),
  `reqid` the requisition id, and the top-level `Link` the candidate URL.
- No reliable deadline field (`NoOfDaysToExpire` is 0/unset on most rows),
  so deadline is always None — never invented.

SCOPE, stated honestly: the embedded `searchResults` holds the board's
FEATURED subset (the first ~10 "hot jobs"), not the full paginated list —
every page-size/pagination URL param the server ignores, always returning
that same featured block. The complete board (89 rows on UBS grad at the
time of writing) lives behind the session-token Ajax results endpoint,
which needs a browser-in-the-loop to mint the token. So this connector
returns the real, honest featured slice; a later browser-bootstrap tier
would return the full board. Better a true subset than nothing.

This is generic to any IBM BrassRing TalentGateway tenant, not UBS-specific.
"""

from __future__ import annotations

import html as _html
import json
import re

from .http import fetch_text, unreadable
from .models import FetchResult, Opportunity, TalentGatewayBoard, VerificationResult

name = "talentgateway"

_HOME = "https://jobs.ubs.com/TGnewUI/Search/Home/Home?partnerid={pid}&siteid={sid}"
_MAX_PAGES = 20
_URL_RE = re.compile(r"jobs\.ubs\.com/TGnewUI/.*?jobid=(\d+)", re.IGNORECASE)


class TalentGatewayShapeError(ValueError):
    """The board page came back without the embedded results payload this
    connector reads. Its own class so `fetch()` can call that unreadable
    rather than empty — every `return []` below used to be indistinguishable
    from a genuinely empty featured block."""


def _parse_page(html_text: str) -> list[dict]:
    """Extract the embedded `<input id="searchResults" value="...">` JSON.
    Uses a find-based slice (not a regex) because the value is one long
    entity-encoded string with no raw quote until its closing `"`, and a
    non-greedy regex mis-terminates on it.

    Raises `TalentGatewayShapeError` when the input, its value, or the JSON
    inside it is missing or unparseable. Returning `[]` for those is what let
    a BrassRing page change quietly close a firm's whole board: the payload
    IS the board, so its absence is never evidence of an empty one. An
    element that parses to an empty `HotJobs.Job` list is a real empty
    board and still returns `[]`."""
    i = html_text.find('id="searchResults"')
    if i < 0:
        raise TalentGatewayShapeError(
            f"no id=\"searchResults\" element on the board page "
            f"({len(html_text)} bytes)")
    vi = html_text.find('value="', i)
    if vi < 0:
        raise TalentGatewayShapeError("searchResults element carries no value= attribute")
    vi += len('value="')
    vj = html_text.find('"', vi)
    if vj < 0:
        raise TalentGatewayShapeError("searchResults value= attribute is unterminated")
    try:
        data = json.loads(_html.unescape(html_text[vi:vj]))
    except (ValueError, TypeError) as e:
        raise TalentGatewayShapeError(f"searchResults payload is not JSON: {e}") from e
    if not isinstance(data, dict) or "HotJobs" not in data:
        raise TalentGatewayShapeError(
            "searchResults payload carries no 'HotJobs' block")
    return (data.get("HotJobs") or {}).get("Job") or []


def _fields(job: dict) -> dict:
    return {q.get("QuestionName"): q.get("Value") for q in job.get("Questions", [])}


def _normalize(job: dict, board: TalentGatewayBoard) -> Opportunity:
    f = _fields(job)
    # Field VALUES are entity-encoded a second time inside the JSON string
    # ("Digital Adoption &amp; ...") — unescape once more for display.
    return Opportunity(
        firm=board.firm,
        title=_html.unescape((f.get("jobtitle") or "").strip()),
        location=_html.unescape((f.get("formtext23") or "").strip()),
        url=job.get("Link", ""),
        source="talentgateway",
        raw=job,
    )


def fetch(board: TalentGatewayBoard) -> FetchResult:
    """Return the board's embedded featured jobs (see SCOPE in the module
    docstring). A single GET — the server ignores pagination params, so
    there's nothing to walk."""
    url = _HOME.format(pid=board.partner_id, sid=board.site_id)
    try:
        html_text = fetch_text(url)
        jobs = _parse_page(html_text)
        seen: set[str] = set()
        unique = []
        for j in jobs:
            rid = _fields(j).get("reqid") or ""
            if rid and rid not in seen:
                seen.add(rid)
                unique.append(j)
        # Normalization stays inside this try — see greenhouse.py's fetch()
        # for why a malformed row must not raise past this function.
        opportunities = [o for o in (_normalize(j, board) for j in unique) if o.url and o.title]
    except TalentGatewayShapeError as e:
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0,
                           error=unreadable(f"talentgateway {board.site_id}: {e}"))
    except Exception as e:  # noqa: BLE001
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities,
                       raw_count=len(unique),
                       # The payload parsed and its Job list was empty: the
                       # board's own answer, not a parse that found nothing.
                       empty_state=not opportunities)


def classify_url(url: str) -> dict | None:
    m = _URL_RE.search(url or "")
    return {"reqid": m.group(1)} if m else None


def verify(url: str) -> VerificationResult:
    """Re-fetch the board and look for the URL's reqid. Present ->
    verified-open. Absent -> `needs-verification`, NEVER `closed`: per the
    module docstring's SCOPE note, this server ignores pagination params
    entirely and always returns the same ~10-job featured slice (89 rows on
    the live UBS grad board at time of writing) — looping `&page=N` here
    re-fetches that identical featured block up to `_MAX_PAGES` times
    without ever seeing the other ~79. A reqid outside the featured slice is
    indistinguishable from a reqid that's still open but simply not
    "hot" — absence here is a coverage gap in this connector's own
    fetch, not evidence the posting closed, and `reverify.py` treats
    "closed" as a one-shot deletion from the feed with no corroboration."""
    info = classify_url(url)
    if not info:
        return VerificationResult("talentgateway", url, "needs-verification",
                                   "URL is not a recognized TalentGateway job URL", [])
    target = info["reqid"]
    # UBS graduate board is the only registered TG tenant; reuse its ids.
    base = _HOME.format(pid=25008, sid=5131)
    try:
        for page in range(1, _MAX_PAGES + 1):
            html_text = fetch_text(base + (f"&page={page}" if page > 1 else ""))
            jobs = _parse_page(html_text)
            if not jobs:
                break
            if any((_fields(j).get("reqid") or "") == target for j in jobs):
                return VerificationResult("talentgateway", url, "verified-open",
                                           f"reqid {target} present on the board", [])
    except Exception as e:  # noqa: BLE001
        return VerificationResult("talentgateway", url, "unreachable", str(e)[:200], [])
    return VerificationResult(
        "talentgateway", url, "needs-verification",
        f"reqid {target} not found in the featured slice this connector can "
        f"see — the board ignores pagination, so this is not a confirmed "
        f"removal", [],
    )
