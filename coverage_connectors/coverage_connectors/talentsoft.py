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

from .http import fetch_text
from .models import FetchResult, Opportunity, TalentsoftBoard

name = "talentsoft"

_CARD = re.compile(
    r'ts-offer-card__title-link[^>]*href="(?P<href>[^"]+)"[^>]*>\s*'
    r'(?P<title>[^<]+?)\s*</a>.*?'
    r'ts-offer-card-content__list[^>]*>(?P<meta>.*?)</ul>',
    re.S)
_LI = re.compile(r"<li[^>]*>\s*([^<]*?)\s*</li>")


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
            raise ValueError("no ts-offer-card entries parsed — markup changed?")
        return FetchResult(board=board, ok=True, opportunities=opps,
                           raw_count=len(opps))
    except Exception as exc:  # noqa: BLE001 — one board must not sink the run
        return FetchResult(board=board, ok=False, opportunities=[],
                           raw_count=0, error=f"{type(exc).__name__}: {exc}")
