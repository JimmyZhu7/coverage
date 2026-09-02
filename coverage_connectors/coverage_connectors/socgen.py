"""Société Générale connector — the Quantum search API behind
careers.societegenerale.com, reached through the site's own PHP proxy.

Deferred in July as "3-step CSRF+token+proxy flow — complex but pure HTTP";
resolved 2026-08-07 against the live site. The three steps, each read out of
the site's global-quantum.js rather than guessed:

1. GET /en/search and read `"csrfToken":"…"` out of the drupalSettings JSON
   in the page markup. No cookies are needed — verified by calling step 2
   with the token and a fresh connection.
2. GET /sg-careers-offers/get-token with `X-Requested-With: XMLHttpRequest`
   and `X-CSRF-Token: {csrf}` → `{"token": <JWT>}`, cached server-side for
   ~an hour.
3. POST /search-proxy.php with `X-Proxy-URL:` naming the real Quantum
   endpoint and `Authorization-API: Bearer {token}` (that exact header —
   plain Authorization belongs to the proxy host, not the API). The payload
   is Quantum's advanced-query JSON; `sourcestr6 eq job` keeps documents
   that are jobs, `documentlanguages eq en` keeps one language variant per
   posting (every offer indexes twice, -en and -fr, and without the filter
   each job would ingest as two rows).

The payload is rich: `sourcestr7` location, `sourcestr8` a structured
contract type (INTERNSHIP / GRADUATE_JOB / VIE / STANDARD…), `url1` the
server-rendered posting page, `url3` the Taleo apply link, and
`sourcevarchar2` the description HTML — which rides into
`raw["detail_text"]` so the facts extractors read this board with no
detail-page fetching. No field is a proven deadline, so none is claimed:
`sourcedatetime2`'s meaning is unverified and it stays in raw as evidence.
"""

from __future__ import annotations

import re

from .http import fetch_json, fetch_text, post_json, unreadable
from .models import FetchResult, Opportunity, SocGenBoard, VerificationResult

name = "socgen"

_ORIGIN = "https://careers.societegenerale.com"
_QUANTUM = ("https://api.socgen.com/business-support/it-for-it-support/"
            "cognitive-service-knowledge/api/v1/search-profile")
_CSRF_RE = re.compile(r'"csrfToken":"([^"]+)"')
# Some documents carry sourcestr7 as display text ("Frankfurt am Main,
# Germany"), others as internal location codes ("GBR_A01, GBR") whose last
# token is ISO 3166-1 alpha-3. Decoding that trailing token is a standard,
# deterministic mapping — reading the provider's own field, not inferring —
# and a code we do not recognise passes through UNCHANGED rather than being
# guessed at. City-level detail inside the code is not decodable and is not
# invented; the country is what the code proves.
_CODE_RE = re.compile(r"^[A-Z]{3}[A-Z0-9_]*(?:,\s*(?P<iso>[A-Z]{3}))?$")
_ISO3 = {
    "FRA": "France", "GBR": "United Kingdom", "DEU": "Germany",
    "USA": "United States", "HKG": "Hong Kong", "SGP": "Singapore",
    "AUS": "Australia", "JPN": "Japan", "CHN": "China", "IND": "India",
    "ITA": "Italy", "ESP": "Spain", "CHE": "Switzerland", "LUX": "Luxembourg",
    "ROU": "Romania", "CZE": "Czech Republic", "MAR": "Morocco",
    "TUN": "Tunisia", "SEN": "Senegal", "CIV": "Ivory Coast",
    "CMR": "Cameroon", "GHA": "Ghana", "DZA": "Algeria", "BRA": "Brazil",
    "CAN": "Canada", "ARE": "United Arab Emirates", "POL": "Poland",
    "NLD": "Netherlands", "BEL": "Belgium", "IRL": "Ireland",
    "PRT": "Portugal", "AUT": "Austria", "SWE": "Sweden", "DNK": "Denmark",
    "NOR": "Norway", "FIN": "Finland", "GRC": "Greece", "TUR": "Turkey",
    "KOR": "South Korea", "TWN": "Taiwan", "THA": "Thailand",
    "MYS": "Malaysia", "IDN": "Indonesia", "VNM": "Vietnam",
    "MEX": "Mexico", "ARG": "Argentina", "CHL": "Chile", "COL": "Colombia",
    "ZAF": "South Africa", "EGY": "Egypt", "NGA": "Nigeria", "KEN": "Kenya",
    "MCO": "Monaco", "GEO": "Georgia", "MDG": "Madagascar",
    "BEN": "Benin", "BFA": "Burkina Faso", "GIN": "Guinea", "TCD": "Chad",
    "MRT": "Mauritania", "GNQ": "Equatorial Guinea", "MOZ": "Mozambique",
    "COG": "Congo", "TGO": "Togo", "NCL": "New Caledonia",
    "PYF": "French Polynesia",
}


def _location(text: str) -> str:
    text = (text or "").strip()
    m = _CODE_RE.match(text)
    if not m:
        return text
    iso = m.group("iso") or text[:3]
    return _ISO3.get(iso, text)
_TAGS = re.compile(r"<[^>]+>")
_PAGE = 100
_MAX_JOBS = 3000


def _token() -> str:
    page = fetch_text(f"{_ORIGIN}/en/search")
    m = _CSRF_RE.search(page)
    if not m:
        raise ValueError("no csrfToken in careers page markup")
    data = fetch_json(
        f"{_ORIGIN}/sg-careers-offers/get-token",
        headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": m.group(1)},
    )
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise ValueError("get-token returned no token")
    return token


def _query(skip_from: int) -> dict:
    return {
        "profile": "ces_profile_sgcareers",
        "query": {
            "advanced": [
                {"type": "simple", "name": "sourcestr6", "op": "eq", "value": "job"},
                {"type": "simple", "name": "documentlanguages", "op": "eq", "value": "en"},
            ],
            "skipFrom": skip_from,
            "skipCount": _PAGE,
        },
        "responseType": "SearchResult",
    }


def _normalize(doc: dict, board: SocGenBoard) -> Opportunity | None:
    title = (doc.get("title") or "").strip()
    url = (doc.get("url1") or "").strip()
    if not title or not url.startswith("http"):
        return None
    raw = {
        "job_id": doc.get("sourcestr4") or "",
        "contract_type": doc.get("sourcestr8") or "",
        "job_family": doc.get("sourcestr10") or "",
        "business_unit": doc.get("sourcestr15") or "",
        "apply_url": doc.get("url3") or "",
        "location_code": (doc.get("sourcestr7") or "").strip(),
        # Semantics unproven; kept as evidence, never claimed as a deadline.
        "sourcedatetime2": doc.get("sourcedatetime2") or "",
    }
    text = _TAGS.sub(" ", doc.get("sourcevarchar2") or "").strip()
    if text:
        raw["detail_text"] = re.sub(r"\s+", " ", text)[:20_000]
        raw["detail_source"] = "payload"
    return Opportunity(
        firm=board.firm,
        title=title,
        location=_location(doc.get("sourcestr7") or ""),
        url=url,
        source="socgen",
        raw=raw,
    )


def fetch(board: SocGenBoard) -> FetchResult:
    opps: list[Opportunity] = []
    seen: set[str] = set()
    try:
        token = _token()
        headers = {
            "X-Proxy-URL": _QUANTUM,
            "Authorization-API": f"Bearer {token}",
        }
        skip = 0
        total = None
        while skip < (_MAX_JOBS if total is None else min(total, _MAX_JOBS)):
            data = post_json(f"{_ORIGIN}/search-proxy.php", _query(skip),
                             headers=headers)
            # The proxy answers 200 for a rejected token or a changed query
            # shape as readily as for a real search, so the envelope is what
            # says whether this is a result set at all. `Result` absent means
            # the response is not one, and `(data.get("Result") or {})` used
            # to fold that into a clean zero — on a board that carries ~640
            # postings and drives closed-detection for all of them.
            if "Result" not in data:
                return FetchResult(
                    board=board, ok=False, opportunities=[], raw_count=0,
                    error=unreadable(
                        f"socgen proxy answered 200 with no 'Result' block "
                        f"(got {sorted(data)[:6]})"),
                )
            if total is None:
                total = int(data.get("TotalCount") or 0)
            docs = (data.get("Result") or {}).get("Docs") or []
            if not docs and skip == 0 and total:
                return FetchResult(
                    board=board, ok=False, opportunities=[], raw_count=0,
                    error=unreadable(
                        f"socgen reported TotalCount={total} and returned 0 docs"),
                )
            if not docs:
                break
            for doc in docs:
                opp = _normalize(doc, board)
                if opp is not None and opp.url not in seen:
                    seen.add(opp.url)
                    opps.append(opp)
            skip += _PAGE
        return FetchResult(board=board, ok=True, opportunities=opps,
                           raw_count=total or len(opps),
                           # Quantum answering TotalCount=0 is the search
                           # saying nothing matched, which is a fact.
                           empty_state=not opps and total == 0)
    except Exception as exc:  # noqa: BLE001 — one board must not sink the run
        return FetchResult(board=board, ok=False, opportunities=[],
                           raw_count=0, error=f"{type(exc).__name__}: {exc}")


def classify_url(url: str) -> dict | None:
    return {"url": url} if (url or "").startswith(_ORIGIN) else None


def verify(url: str) -> VerificationResult:
    """`coverage_connectors.verify()`'s dispatch loop calls `classify_url`
    then `verify` on every registered connector in turn until one claims the
    URL — this module used to define neither, so reaching it without a
    prior match raised a bare AttributeError and killed the whole dispatch
    for any SocGen-shaped URL. Same honest "needs-verification" contract as
    phenom.py and avature.py: re-running the three-step CSRF+token+proxy
    flow (see module docstring) to check ONE posting has not been verified
    live, so closed-detection on the next board re-fetch remains the
    removal path for this provider."""
    if not classify_url(url):
        return VerificationResult("socgen", url, "needs-verification",
                                   "URL is not a recognized Société Générale careers URL", [])
    return VerificationResult(
        "socgen", url, "needs-verification",
        "SocGen has no per-posting liveness endpoint verified yet — "
        "closed-detection on the next board re-fetch is the removal path", [],
    )
