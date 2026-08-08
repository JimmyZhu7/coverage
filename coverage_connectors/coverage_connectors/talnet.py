"""tal.net connector — server-rendered HTML job/event boards.

Ported from the radar's `sources.py` (`_talnet_parse`, `_talnet_normalize`)
and `verify_rows.py` (`_verify_talnet`). tal.net serves plain HTML tables
(one `<tr class="opp_<id>" data-title="…">` per listing), so this is regex
parsing over markup rather than a JSON API — brittle by nature, which is
why the parser reads the board's own `<th>` header row and zips it against
each row's cells instead of assuming a fixed column set (jobs boards carry
a City column; events boards carry Event Date + Registration Deadline).

Two deliberate differences from the radar original:

- **No region filtering.** The radar dropped rows naming out-of-scope
  cities (its single-user HK/US policy). This package's contract is
  "report everything the board lists" — region relevance is the caller's
  policy, not the connector's.
- **Registration Deadline -> `deadline`.** tal.net's dd/mm/yyyy deadline
  column is a genuine act-by date; it is normalized to ISO and returned as
  the posting's deadline. Event Date is NOT a deadline (it's when the event
  happens) and is deliberately not stored as one.

Operational note carried from the radar: morganstanley.tal.net has a
Content-Length/Transfer-Encoding quirk that breaks some HTTP clients;
plain urllib (this package's http layer) is unaffected — verified live.
"""

from __future__ import annotations

import html as html_mod
import re
import urllib.error
import urllib.parse

from .http import bot_challenge_reason, fetch_text
from .models import FetchResult, Opportunity, TalnetBoard, VerificationResult

name = "talnet"

_ROW_RE = re.compile(
    r'<tr class="opp_(?P<id>\d+)[^"]*"[^>]*data-title="(?P<data_title>[^"]*)"[^>]*>(?P<body>.*?)</tr>',
    re.DOTALL,
)
_LINK_RE = re.compile(r'<a class="subject" href="([^"]+)">(.*?)</a>', re.DOTALL)
_TD_RE = re.compile(r'<td class="comm_list_tbody">(.*?)</td>', re.DOTALL)
_HEADER_RE = re.compile(r'<th class="comm_list_thead">([^<]*)</th>')
_TAG_RE = re.compile(r"<[^>]+>")
_SLUG_RE = re.compile(r"/opp/\d+-([^/]+)/")
_URL_RE = re.compile(r"([\w-]+)\.tal\.net/", re.IGNORECASE)
# Board columns render "23 Jul 2026" (with "Sept" for September); posting
# pages' meta descriptions render "23/07/2026". Both are act-by dates.
_DMY_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DMY_NAME_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,4})\.?\s+(\d{4})$")
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}

_CLOSED_RE = re.compile(
    r"closed to applications|no longer accepting applications|registration is closed|"
    r"this (?:vacancy|opportunity) (?:has closed|is closed)|applications are now closed",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)
_META_DESC_RE = re.compile(r'<meta[^>]*name="description"[^>]*content="([^"]*)"', re.IGNORECASE)
_DESC_EVENT_RE = re.compile(r"Event Date:\s*(\d{1,2})/(\d{1,2})/(\d{4})")
_DESC_DEADLINE_RE = re.compile(r"Registration Deadline:\s*(\d{1,2})/(\d{1,2})/(\d{4})")


def _dmy_to_iso(raw: str) -> str | None:
    s = (raw or "").strip()
    m = _DMY_SLASH_RE.match(s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    m = _DMY_NAME_RE.match(s)
    if m:
        d, mon, y = m.groups()
        num = _MONTHS.get(mon.lower())
        if num:
            return f"{y}-{num:02d}-{int(d):02d}"
    return None


# tal.net embeds a per-session "xf-<hex>" segment in every listing URL, so
# the same posting gets a different URL on every board fetch. Stripping it
# yields a stable, still-resolving URL (verified live against
# morganstanley.tal.net, 2026-07-23) — without this, every scrape would
# close all prior rows and mint duplicates, wrecking dedup AND
# closed-detection.
_XF_RE = re.compile(r"/xf-[0-9a-f]+")


def canonical_url(url: str) -> str:
    return _XF_RE.sub("", url or "")


def _slug_title(url: str) -> str:
    m = _SLUG_RE.search(url)
    return m.group(1).replace("-", " ") if m else ""


def _parse(html: str) -> list[dict]:
    """[{id, title, url, cols}] for every listing row. `cols` keys come from
    the board's own header text, so a board with a different column set
    still parses instead of silently returning nothing."""
    headers = [h.strip() for h in _HEADER_RE.findall(html)]
    rows = []
    for m in _ROW_RE.finditer(html):
        body = m.group("body")
        link_m = _LINK_RE.search(body)
        if not link_m:
            continue
        url = link_m.group(1)
        link_text = re.sub(r"\s+", " ", _TAG_RE.sub("", link_m.group(2))).strip()
        title = link_text or m.group("data_title").strip() or _slug_title(url)
        if not title:
            continue
        # tal.net HTML-escapes text ("Q&amp;A"); store the real characters.
        title = html_mod.unescape(title)
        tds = [html_mod.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", td)).strip())
               for td in _TD_RE.findall(body)]
        cols = dict(zip(headers[1:], tds[1:])) if len(headers) > 1 and len(tds) > 1 else {}
        rows.append({"id": m.group("id"), "title": title, "url": url, "cols": cols})
    return rows


def _normalize(row: dict, board: TalnetBoard) -> Opportunity:
    cols = row.get("cols", {})
    return Opportunity(
        firm=board.firm,
        title=row["title"],
        location=cols.get("City", ""),
        url=canonical_url(row["url"]),
        source="talnet",
        deadline=_dmy_to_iso(cols.get("Registration Deadline", "")),
        raw=row,
    )


def fetch(board: TalnetBoard) -> FetchResult:
    try:
        html = fetch_text(board.board_url)
        # A tenant behind a bot check serves 200 + a challenge page. Parsing
        # it yields zero rows, which is indistinguishable from a shape change
        # unless we say so here.
        challenge = bot_challenge_reason(html)
        if challenge:
            return FetchResult(
                board=board, ok=False, opportunities=[], raw_count=0,
                error=f"blocked by bot protection ({challenge}) — board unreadable, not empty",
            )
        rows = _parse(html)
        # Kept inside this try — see greenhouse.py's fetch() for why a
        # normalization failure must not propagate uncaught out of
        # `fetch()`.
        opportunities = [o for o in (_normalize(r, board) for r in rows) if o.url]
    except Exception as e:  # noqa: BLE001 — board-level failure, not fatal to the run
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities, raw_count=len(rows))


def classify_url(url: str) -> dict | None:
    m = _URL_RE.search(url or "")
    if not m:
        return None
    return {"tenant": m.group(1), "has_opp": "/opp/" in (url or "")}


def verify(url: str) -> VerificationResult:
    """Ported from `_verify_talnet`: fetch the posting page, read its
    closed-language markers and the Event Date / Registration Deadline in
    the meta description. tal.net serves a 200 page even for closed
    listings, so the CLOSED verdict comes from the page's own words, never
    from a status code."""
    info = classify_url(url)
    if not info:
        return VerificationResult("talnet", url, "needs-verification",
                                   "URL is not a recognized tal.net URL", [])
    if not info["has_opp"]:
        return VerificationResult(
            "talnet", url, "needs-verification",
            "generic candidate-center URL (no /opp/<id> posting path) — can't verify this "
            "specific item deterministically", [],
        )
    try:
        html = fetch_text(url)
    except urllib.error.HTTPError as e:
        return VerificationResult("talnet", url, "unreachable", f"HTTP {e.code}", [])
    except Exception as e:  # noqa: BLE001
        return VerificationResult("talnet", url, "unreachable", str(e)[:200], [])

    # Must precede the closed-language check: a challenge page carries no
    # closed language, so without this it reads as "verified-open" and every
    # posting on a bot-walled tenant gets rubber-stamped live forever.
    challenge = bot_challenge_reason(html)
    if challenge:
        return VerificationResult("talnet", url, "unreachable",
                                   f"blocked by bot protection ({challenge})", [])

    title_m = _TITLE_RE.search(html)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
    desc = (_META_DESC_RE.search(html) or [None, ""])[1] if _META_DESC_RE.search(html) else ""

    # Event Date and Registration Deadline are both act-by dates for a
    # candidate — both feed deadline_dates. tal.net has no posted-date
    # concept, so posted_date stays None.
    dates: list[str] = []
    for rx in (_DESC_EVENT_RE, _DESC_DEADLINE_RE):
        m = rx.search(desc)
        if m:
            d, mo, y = m.groups()
            dates.append(f"{y}-{int(mo):02d}-{int(d):02d}")

    if _CLOSED_RE.search(html):
        return VerificationResult("talnet", url, "closed",
                                   f'page carries closed-language (title="{title}")', dates)
    return VerificationResult("talnet", url, "verified-open",
                               f'title="{title}"' + (f" desc-dates={dates}" if dates else ""), dates)
