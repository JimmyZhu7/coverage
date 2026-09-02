"""Avature connector — the RSS/XML job feed Avature career sites expose.

Avature-hosted career sites (careers.bain.com, recruitment.macquarie.com, …)
publish a standard RSS 2.0 feed at `.../SearchJobs/feed/`, paginated by a
`folderOffset` query param (20 items/page). Each `<item>` carries a real,
candidate-facing posting URL in `<link>`/`<guid>` — no fabricated identity.

Honesty limits, stated up front:

- The feed carries NO location field. `location` is left blank rather than
  guessed; a city that happens to sit in the title is left in the title.
- The feed carries no application deadline; `deadline` is always None.
- `verify()` does NOT re-read the feed — there is no per-firm feed registry
  here to look a single URL's board up in, only `board.feed_url` at fetch
  time. A recognized Avature URL always comes back `needs-verification`
  ("Avature liveness is resolved by the next feed fetch"); it never returns
  verified-open or closed. Closed-detection instead happens at `fetch()`
  time, board-diff style: a URL that stops appearing in the feed is what
  closes a row, the same mechanism every other board-diffed connector uses.
"""

from __future__ import annotations

import re
import urllib.parse

from .http import fetch_text, unreadable
from .models import AvatureBoard, FetchResult, Opportunity, VerificationResult

name = "avature"

# The feed's own envelope. An RSS 2.0 document always has a `<channel>`,
# with or without items, so its presence is what separates "this feed is
# empty" from "this is not the feed".
#
# THE TWO SHAPES THIS CATCHES, both HTTP-200-ish and both previously read as
# an empty board. Avature answers a rate-limited or not-yet-warm feed with
# **202 and an empty body** — urllib does not raise on a 2xx, so the old code
# parsed "" into zero items and returned `ok=True`. And a tenant that has put
# its careers site behind SSO serves a small **login stub** (~830 bytes of
# sign-in HTML) at the same URL, which likewise parses to zero items. Either
# one, on a firm whose feed normally carries a hundred postings, used to read
# as "this firm closed everything".
_CHANNEL_RE = re.compile(r"<channel\b", re.IGNORECASE)
_LOGIN_STUB_RE = re.compile(
    r"<input[^>]+type=[\"']password|name=[\"']password|/login|sign\s*in\b",
    re.IGNORECASE,
)

_ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>",
                       re.IGNORECASE | re.DOTALL)
_LINK_RE = re.compile(r"<link>\s*(.*?)\s*</link>", re.IGNORECASE | re.DOTALL)
_PAGE_SIZE = 20
_MAX_ITEMS = 400  # bound a saturated feed, same posture as the other connectors


def _feed_url(base: str, offset: int) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}folderRecordsPerPage={_PAGE_SIZE}&folderOffset={offset}"


def _parse_items(xml: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for block in _ITEM_RE.findall(xml):
        t = _TITLE_RE.search(block)
        l = _LINK_RE.search(block)
        if not t or not l:
            continue
        title = re.sub(r"\s+", " ", t.group(1)).strip()
        link = l.group(1).strip()
        if title and link.startswith("http"):
            out.append((title, link))
    return out


def _normalize(title: str, url: str, board: AvatureBoard) -> Opportunity:
    return Opportunity(firm=board.firm, title=title, location="", url=url, source="avature")


def fetch(board: AvatureBoard) -> FetchResult:
    """Page the RSS feed by folderOffset until a page repeats or empties,
    deduped by URL. A failed page fails the board (partial coverage would let
    closed-detection close live rows)."""
    seen: set[str] = set()
    opps: list[Opportunity] = []
    offset = 0
    # As in the other connectors: true only when the cap, not the feed, ended
    # the walk. Both breaks below are the feed saying it has no more.
    truncated = False
    try:
        while offset < _MAX_ITEMS:
            xml = fetch_text(_feed_url(board.feed_url, offset))
            if offset == 0 and not _CHANNEL_RE.search(xml):
                # Not an RSS document. Name which of the two known shapes it
                # is where we can — the operator's fix differs (wait/retry vs
                # find the tenant's new public feed URL).
                if not xml.strip():
                    what = "empty body (Avature answers 202 with one)"
                elif _LOGIN_STUB_RE.search(xml):
                    what = f"a {len(xml)}-byte login stub, not the feed"
                else:
                    what = f"{len(xml)} bytes with no <channel> element"
                return FetchResult(
                    board=board, ok=False, opportunities=[], raw_count=0,
                    error=unreadable(f"avature feed returned {what}"),
                )
            items = _parse_items(xml)
            fresh = [(t, u) for t, u in items if u not in seen]
            if not fresh:
                break  # empty page or all-duplicates (feed looped) -> done
            # Normalization stays inside this try — see greenhouse.py's
            # fetch() for why a malformed row must not raise past this
            # function and cost `fetch_many` every OTHER board's results.
            for title, url in fresh:
                seen.add(url)
                opps.append(_normalize(title, url, board))
            if len(items) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
        else:
            truncated = True
    except Exception as e:  # noqa: BLE001 — board-level failure, not fatal to a multi-board run
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opps,
                       raw_count=len(opps), truncated=truncated,
                       # A real `<channel>` with no `<item>` in it is the feed
                       # saying the board is empty, and by this point one has
                       # been seen.
                       empty_state=not opps)


def classify_url(url: str) -> dict | None:
    """`/jobs/` alone is not Avature's signature — iCIMS posting URLs
    (`https://{tenant}.icims.com/jobs/<id>/<slug>/job`) contain the exact
    same substring. Confirmed live: every one of 406 open icims-sourced
    rows' URLs matched this test, and because `avature` is dispatched
    before `icims` in CONNECTORS (see coverage_connectors/__init__.py),
    avature.verify() answered EVERY one of them first with a false
    "needs-verification" — icims's own working verify() (a real
    404/410-on-closed check) was never reached. No per-firm feed registry
    exists here (see module docstring) to check the host allowlist the way
    sitemap.py does, but icims.com hosts are never Avature's, and deferring
    to the connector that actually owns them is always correct."""
    u = url or ""
    if "/jobs/" not in u:
        return None
    if urllib.parse.urlparse(u).netloc.lower().endswith(".icims.com"):
        return None
    return {"url": u}


def verify(url: str) -> VerificationResult:
    """No per-firm feed registry, so verify is best-effort: unrecognized URLs
    are left for a re-fetch to settle."""
    if not classify_url(url):
        return VerificationResult("avature", url, "needs-verification",
                                   "not a recognized Avature job URL", [])
    return VerificationResult("avature", url, "needs-verification",
                              "Avature liveness is resolved by the next feed fetch", [])
