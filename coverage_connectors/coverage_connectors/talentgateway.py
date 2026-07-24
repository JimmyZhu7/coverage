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

from .http import fetch_text
from .models import FetchResult, Opportunity, TalentGatewayBoard, VerificationResult

name = "talentgateway"

_HOME = "https://jobs.ubs.com/TGnewUI/Search/Home/Home?partnerid={pid}&siteid={sid}"
_MAX_PAGES = 20
_URL_RE = re.compile(r"jobs\.ubs\.com/TGnewUI/.*?jobid=(\d+)", re.IGNORECASE)


def _parse_page(html_text: str) -> list[dict]:
    """Extract the embedded `<input id="searchResults" value="...">` JSON.
    Uses a find-based slice (not a regex) because the value is one long
    entity-encoded string with no raw quote until its closing `"`, and a
    non-greedy regex mis-terminates on it."""
    i = html_text.find('id="searchResults"')
    if i < 0:
        return []
    vi = html_text.find('value="', i)
    if vi < 0:
        return []
    vi += len('value="')
    vj = html_text.find('"', vi)
    if vj < 0:
        return []
    try:
        data = json.loads(_html.unescape(html_text[vi:vj]))
    except (ValueError, TypeError):
        return []
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
    except Exception as e:  # noqa: BLE001
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    jobs = _parse_page(html_text)
    seen: set[str] = set()
    unique = []
    for j in jobs:
        rid = _fields(j).get("reqid") or ""
        if rid and rid not in seen:
            seen.add(rid)
            unique.append(j)
    opportunities = [o for o in (_normalize(j, board) for j in unique) if o.url and o.title]
    return FetchResult(board=board, ok=True, opportunities=opportunities, raw_count=len(unique))


def classify_url(url: str) -> dict | None:
    m = _URL_RE.search(url or "")
    return {"reqid": m.group(1)} if m else None


def verify(url: str) -> VerificationResult:
    """Re-fetch the board and look for the URL's reqid across its pages.
    Present -> verified-open; absent from a clean read -> closed."""
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
    return VerificationResult("talentgateway", url, "closed",
                               f"reqid {target} not found on the board — likely closed", [])
