"""Talentsoft list-page connector — server-rendered `.ts-offer-card` markup.

Built for Crédit Agricole CIB (jobs.ca-cib.com), the last easy target on the
deferred-firms list, and named for the ATS rather than the bank because the
card classes are Talentsoft's own — any other tenant exposing the same
`list-of-all-jobs.aspx?all=1` page should ride this connector unchanged.

The `?all=1` parameter is what makes this a one-request connector: without
it the list pages through ASP.NET ViewState POSTs, which is exactly the
complexity the July research deferred this board over. With it the full set
arrives server-rendered (~100 offers, 260KB), each card carrying the title,
the offer URL, and a three-item list of contract type / country / city.

Regex over HTML is deliberate and bounded: the three fields read here sit in
stable, class-named containers, this package has no HTML-parser dependency,
and a markup change fails loudly as `ok=False` (zero cards is an error, not
an empty market — CA-CIB always has open roles).
"""

from __future__ import annotations

import html
import re

from .http import fetch_text, unreadable
from .models import FetchResult, Opportunity, TalentsoftBoard, VerificationResult

name = "talentsoft"

# Talentsoft's own posting-URL convention (verified against jobs.ca-cib.com,
# the tenant this connector was built for): "/job/<slug>_<digits>.aspx" — the
# trailing "_<numeric id>.aspx" is the ATS's own shape, not a CA-CIB-specific
# one, matching the docstring's claim that any Talentsoft tenant serving the
# same list markup rides this connector unchanged.
_JOB_URL_RE = re.compile(r"/job/[^/?#]*_\d+\.aspx", re.IGNORECASE)

_CARD = re.compile(
    r'ts-offer-card__title-link[^>]*href="(?P<href>[^"]+)"[^>]*>\s*'
    r'(?P<title>[^<]+?)\s*</a>.*?'
    r'ts-offer-card-content__list[^>]*>(?P<meta>.*?)</ul>',
    re.S)
_LI = re.compile(r"<li[^>]*>\s*([^<]*?)\s*</li>")

# Talentsoft's own "the list is empty" line, in the two languages this
# tenant serves (the platform is French; CA-CIB's site runs both). Its
# presence is what turns zero cards from a parse failure into an answer.
_EMPTY_STATE_RE = re.compile(
    r"ts-no-result|no-offer|aucune?\s+offre"
    r"|no\s+(?:offers?|results?|jobs?|vacancies)\s+(?:were\s+)?(?:found|match|available)"
    r"|0\s+(?:offers?|r[ée]sultats?)\b",
    re.IGNORECASE,
)


def _normalize(m: re.Match, board: TalentsoftBoard) -> Opportunity | None:
    title = html.unescape(m.group("title")).strip()
    href = m.group("href").strip()
    if not title or not href:
        return None
    items = [html.unescape(x).strip() for x in _LI.findall(m.group("meta"))]
    contract = items[0] if items else ""
    # City then country reads naturally ("Montrouge, France"); either half
    # may be absent and the join must not invent a comma for it.
    place = [p for p in (items[2] if len(items) > 2 else "",
                         items[1] if len(items) > 1 else "") if p]
    return Opportunity(
        firm=board.firm,
        title=title,
        location=", ".join(place),
        url=board.origin.rstrip("/") + href if href.startswith("/") else href,
        source="talentsoft",
        raw={"contract_type": contract},
    )


def fetch(board: TalentsoftBoard) -> FetchResult:
    try:
        page = fetch_text(board.list_url)
        seen: set[str] = set()
        opps: list[Opportunity] = []
        for m in _CARD.finditer(page):
            opp = _normalize(m, board)
            if opp is not None and opp.url not in seen:
                seen.add(opp.url)
                opps.append(opp)
        if not opps:
            # Zero cards, and the question is which zero. A tenant that has
            # genuinely emptied its list still renders the list container and
            # its own "no offer" line; a tenant whose card markup changed
            # renders offers this regex can no longer see. The old code raised
            # a bare ValueError for both, which reported a real empty board as
            # a broken one — the same conflation in the opposite direction to
            # the one the rest of this package is fixing.
            if _EMPTY_STATE_RE.search(page):
                return FetchResult(board=board, ok=True, opportunities=[],
                                   raw_count=0, empty_state=True)
            return FetchResult(
                board=board, ok=False, opportunities=[], raw_count=0,
                error=unreadable(
                    f"no ts-offer-card entries parsed from {len(page)} bytes and "
                    f"no empty-list message — markup changed?"),
            )
        return FetchResult(board=board, ok=True, opportunities=opps,
                           raw_count=len(opps))
    except Exception as exc:  # noqa: BLE001 — one board must not sink the run
        return FetchResult(board=board, ok=False, opportunities=[],
                           raw_count=0, error=f"{type(exc).__name__}: {exc}")


def classify_url(url: str) -> dict | None:
    return {"url": url} if _JOB_URL_RE.search(url or "") else None


def verify(url: str) -> VerificationResult:
    """`coverage_connectors.verify()`'s dispatch loop calls `classify_url`
    then `verify` on every registered connector in turn until one claims the
    URL — this module used to define neither, so reaching it without a
    prior match raised a bare AttributeError and killed the whole dispatch
    for any Talentsoft-shaped URL (reproduced live against a
    jobs.ca-cib.com posting). Same honest "needs-verification" contract as
    phenom.py and avature.py: no per-tenant registry exists to re-fetch one
    specific posting's own page reliably across every Talentsoft tenant, so
    closed-detection on the next board re-fetch remains the removal path
    for this provider."""
    if not classify_url(url):
        return VerificationResult("talentsoft", url, "needs-verification",
                                   "URL is not a recognized Talentsoft posting URL", [])
    return VerificationResult(
        "talentsoft", url, "needs-verification",
        "Talentsoft has no per-posting liveness endpoint verified yet — "
        "closed-detection on the next board re-fetch is the removal path", [],
    )
