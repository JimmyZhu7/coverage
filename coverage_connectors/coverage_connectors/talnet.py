"""tal.net connector — server-rendered HTML job/event boards.

Ported from the radar's `sources.py` (`_talnet_parse`, `_talnet_normalize`)
and `verify_rows.py` (`_verify_talnet`). tal.net serves server-rendered
HTML, so this is regex parsing over markup rather than a JSON API —
brittle by nature, which is why each parser reads the board's own field
labels and zips them against each listing's values instead of assuming a
fixed column set (jobs boards carry a City column; events boards carry
Event Date + Registration Deadline).

**Two layouts, both permanently supported.** Oleeo renders the same board
two ways, and which one a tenant serves is a per-tenant setting, not a
platform migration:

- **Table** — `<tr class="opp_<id>" data-title="…">` rows under a
  `<th class="comm_list_thead">` header row. Bank of America, Morgan
  Stanley, Nomura and Evercore all serve this; it is the original and
  the common case.
- **Card grid** — `<li class="col-md-6 opp-container">` tiles, each
  holding `candidate-opp-field-N` divs whose label lives in a nested
  `<span class="candidate-opp-field-label">`. Jefferies serves this.
  There is not a single `<tr>` on such a page, so the table regex yields
  nothing and the connector used to report a clean, successful, empty
  board — Jefferies ingested zero rows for its entire history while
  serving 50+ live vacancies.

The card branch runs only when the table branch finds nothing, so the
four table tenants above cannot be affected by it.

That year of silent zeroes is also why `fetch()` carries a zero-rows
guard: a page that plainly contains vacancy markup but yields no parsed
listing is a broken parser, not an empty market, and it is reported as
`ok=False` — the same "unreadable, not empty" contract the bot-challenge
check already uses. A board that is honestly empty (Jefferies' events
board serves `no_results_message` and zero vacancy markup) still reports
zero cleanly and must never trip the guard.

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

from .http import BOT_BLOCK_PREFIX, bot_challenge_reason, fetch_text
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

# --- card layout (see module docstring) -------------------------------------
# A tile is one <li> carrying the opp-container class. Cards never nest a
# <li>, so the non-greedy </li> is a safe terminator — the same bet the
# table regex makes on </tr>.
_CARD_RE = re.compile(
    r'<li[^>]*\bclass="[^"]*\bopp-container\b[^"]*"(?P<attrs>[^>]*)>(?P<body>.*?)</li>',
    re.DOTALL | re.IGNORECASE,
)
_CARD_ID_RE = re.compile(r'\bdata-oppid="(\d+)"')
_CARD_DATA_TITLE_RE = re.compile(r'\bdata-title="([^"]*)"')
# One labelled fact per tile. The wrapper element varies (the ID field is a
# <div>, the title field an <h3>), so the closing tag is a backreference
# rather than a hardcoded </div>.
_CARD_FIELD_RE = re.compile(
    r'<(?P<tag>\w+)[^>]*\bclass="[^"]*\bcandidate-opp-field-\d+\b[^"]*"[^>]*>'
    r'(?P<inner>.*?)</(?P=tag)>',
    re.DOTALL | re.IGNORECASE,
)
_CARD_FIELD_LABEL_RE = re.compile(
    r'<span[^>]*\bclass="[^"]*\bcandidate-opp-field-label\b[^"]*"[^>]*>'
    r'(?P<label>.*?)</span>(?P<value>.*)',
    re.DOTALL | re.IGNORECASE,
)
# Card boards page at 50 tiles and advertise the rest with this nav. The
# table boards do not paginate (Morgan Stanley returns 1,076 rows in one
# response), so following it is deliberately confined to the card branch —
# the four working table tenants issue exactly the requests they always did.
_NEXT_PAGE_RE = re.compile(
    r'<span[^>]*\bclass="[^"]*\bnext_links\b[^"]*"[^>]*>\s*<a[^>]*\bhref="([^"]+)"',
    re.DOTALL | re.IGNORECASE,
)
_MAX_CARD_PAGES = 20

# "This page is showing vacancies" — the evidence that separates a parser
# that has stopped reading the page from a board that genuinely has nothing
# on it. Deliberately excludes the bare `candidate-opp-tile` class: it also
# appears inside a jQuery selector in the page's own scripts, including on
# Jefferies' empty events board, so keying off it would flag a correctly
# empty board as broken.
_VACANCY_MARKUP_RE = re.compile(
    r'<tr class="opp_\d+'
    r'|class="[^"]*\bopp-container\b'
    r'|<a class="subject"'
    r'|candidate-opp-field-label',
    re.IGNORECASE,
)

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


def _text(fragment: str) -> str:
    """Markup fragment -> the plain text it renders, unescaped."""
    return html_mod.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", fragment)).strip())


def _parse_table(html: str) -> list[dict]:
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


def _parse_cards(html: str) -> list[dict]:
    """Card-grid equivalent of `_parse_table`, returning the identical
    `{id, title, url, cols}` shape so `_normalize` — and every date, slug
    and canonical-URL helper it leans on — is reused verbatim.

    `cols` is keyed by each tile's own `candidate-opp-field-label` text
    (trailing colon stripped) rather than by field position, because the
    label order is not guaranteed stable across Oleeo tenants and a
    positional read would silently mis-assign a column the day a tenant
    reorders one. A field with no label span carries no key — on Jefferies
    that is the title field, whose value is read from its `<a class=
    "subject">` link like the table path does."""
    rows = []
    for m in _CARD_RE.finditer(html):
        body = m.group("body")
        link_m = _LINK_RE.search(body)
        if not link_m:
            continue
        url = link_m.group(1)
        id_m = _CARD_ID_RE.search(m.group("attrs")) or _CARD_ID_RE.search(body)
        title_attr_m = _CARD_DATA_TITLE_RE.search(body) or _CARD_DATA_TITLE_RE.search(m.group("attrs"))
        title = (_text(link_m.group(2))
                 or html_mod.unescape((title_attr_m.group(1) if title_attr_m else "").strip())
                 or _slug_title(url))
        if not title:
            continue
        cols = {}
        for f in _CARD_FIELD_RE.finditer(body):
            lm = _CARD_FIELD_LABEL_RE.search(f.group("inner"))
            if not lm:
                continue  # unlabelled field (the title tile) — not a column
            label = _text(lm.group("label")).rstrip(":").strip()
            if label:
                cols[label] = _text(lm.group("value"))
        rows.append({"id": id_m.group(1) if id_m else "", "title": title,
                     "url": url, "cols": cols})
    return rows


def _fetch_card_pages(first_url: str, first_html: str) -> tuple[list[dict], bool]:
    """Walk a card board's `next_links` nav, returning (rows, truncated).

    Card boards render 50 tiles a page, so a single fetch of a 51-vacancy
    board is a successful read of an incomplete list — and ingest infers
    "closed" from "absent from the fetch", so returning that list without
    saying so would auto-close a live posting. Bounded at
    `_MAX_CARD_PAGES`; hitting the bound (or being handed a nav that loops
    back to a page already seen) returns `truncated=True` rather than
    pretending the list is whole."""
    rows: list[dict] = []
    seen_urls: set[str] = set()
    seen_pages = {first_url}
    page_url, page_html = first_url, first_html
    for page_no in range(1, _MAX_CARD_PAGES + 1):
        for row in _parse_cards(page_html):
            if row["url"] in seen_urls:
                continue
            seen_urls.add(row["url"])
            rows.append(row)
        nxt_m = _NEXT_PAGE_RE.search(page_html)
        if not nxt_m:
            return rows, False
        if page_no == _MAX_CARD_PAGES:
            return rows, True
        nxt = urllib.parse.urljoin(page_url, html_mod.unescape(nxt_m.group(1)))
        if nxt in seen_pages:
            # The nav points at something already read: stop, and say the
            # list may be short rather than looping on it.
            return rows, True
        seen_pages.add(nxt)
        page_url, page_html = nxt, fetch_text(nxt)
    return rows, True


#: The board's own header text for the location column, in the order it is
#: trusted. Oleeo lets each tenant name its own columns, and they do not
#: agree: Bank of America, Morgan Stanley and Evercore ship "City";
#: nomuracampus ships "Location". Reading only "City" cost every Nomura row
#: its location — measured 2026-09-01 on the founder's data, 56 rows carried
#: a non-empty "Location" cell and a blank `location` field, and were charged
#: `W_REGION_UNKNOWN` for our own ignorance rather than the board's silence.
#: The worst of them was "2027 - Discover Nomura Programme - Insight
#: Programme", the founder's number one pick at 90 points, sitting at region
#: "" while its own row said London.
#:
#: This is a read of the board's own labelled column, not a guess: nothing
#: here infers a location from prose, and a board that ships neither label
#: still yields "" (P1 — the events boards carry no location column at all
#: and must keep saying nothing).
_LOCATION_COL_LABELS = ("City", "Location")


def _location(cols: dict) -> str:
    """The location the board itself stated, under whichever label it uses.

    "City" stays first so the four tenants that ship it are bit-for-bit
    unchanged; a tenant shipping both would still read as it does today."""
    for label in _LOCATION_COL_LABELS:
        value = (cols.get(label) or "").strip()
        if value:
            return value
    return ""


def _normalize(row: dict, board: TalnetBoard) -> Opportunity:
    cols = row.get("cols", {})
    return Opportunity(
        firm=board.firm,
        title=row["title"],
        location=_location(cols),
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
                error=f"{BOT_BLOCK_PREFIX} ({challenge}) — board unreadable, not empty",
            )
        # Table first, always: it is the layout four working tenants serve,
        # and the card branch only ever sees a page it found nothing in.
        rows = _parse_table(html)
        truncated = False
        if not rows:
            rows, truncated = _fetch_card_pages(board.board_url, html)
        # Kept inside this try — see greenhouse.py's fetch() for why a
        # normalization failure must not propagate uncaught out of
        # `fetch()`.
        opportunities = [o for o in (_normalize(r, board) for r in rows) if o.url]
    except Exception as e:  # noqa: BLE001 — board-level failure, not fatal to the run
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    # Zero rows off a page that is visibly listing vacancies means the
    # parser stopped reading this layout, not that the firm stopped
    # hiring. Reporting that as ok=True/0-rows is what let Jefferies sit
    # at zero for a year, and it is the ok flag that stops ingest
    # auto-closing a firm's entire open set off a fetch that read nothing.
    if not opportunities and _VACANCY_MARKUP_RE.search(html):
        return FetchResult(
            board=board, ok=False, opportunities=[], raw_count=0,
            error="board carries vacancy markup but no listing parsed "
                  "(neither table rows nor cards) — layout changed, "
                  "board unreadable, not empty",
        )
    return FetchResult(board=board, ok=True, opportunities=opportunities,
                       raw_count=len(rows), truncated=truncated,
                       # Past the markup guard above, so a zero here is a
                       # board page carrying no vacancy markup at all — the
                       # empty events board's own answer (Jefferies' Insight
                       # Days board is registered empty on purpose).
                       empty_state=not opportunities)


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
                                   f"{BOT_BLOCK_PREFIX} ({challenge})", [])

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
