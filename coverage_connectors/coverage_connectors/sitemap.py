"""Sitemap connector — for career sites that are JS shells with a plain
sitemap.xml.

Ported from the radar's `_fetch_hsbc_sitemap`, generalized to any site with
the same shape: the career site itself (e.g. apply.careers.hsbc.com, a SAP
SuccessFactors Career Site Builder shell) renders nothing without JS, but
its sitemap.xml is a plain `<urlset>` listing every posting URL. Rows are
kept only when the URL contains the board's `path_filter` — for HSBC that
is the dedicated "/emergingtalent/job/" path its campus Internship/Graduate
postings live under (verified live).

Honesty limits of this source, stated up front:

- The TITLE is reconstructed from the URL slug (sitemaps carry no titles),
  and slugs truncate long titles. What we show is what the URL says.
- There is no per-posting verify: the underlying page is a JS shell that
  returns 200 regardless, so `verify()` re-reads the sitemap — a URL still
  listed is verified-open; a URL missing from its own sitemap is closed.
"""

from __future__ import annotations

import html
import re
import urllib.parse

from .http import fetch_text
from .models import FetchResult, Opportunity, SitemapBoard, VerificationResult

name = "sitemap"

_LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
_SLUG_RE = re.compile(r"/job/([^/]+)/\d+/?$")

# Known sitemap URLs per host, for verify()'s re-read. Registered by the
# board catalog at import time via `register_sitemap`, so verify(url) can
# find the right sitemap for a stored posting URL.
_SITEMAPS: dict[str, str] = {}


def register_sitemap(host: str, sitemap_url: str) -> None:
    _SITEMAPS[host.lower()] = sitemap_url


def _rows(xml: str, path_filter: str) -> list[dict]:
    rows = []
    for loc in _LOC_RE.findall(xml):
        if path_filter not in loc:
            continue
        m = _SLUG_RE.search(loc)
        if not m:
            continue
        slug = urllib.parse.unquote(m.group(1))
        title = re.sub(r"\s+", " ", slug.replace("-", " ")).strip()
        rows.append({
            "url": loc,
            "title": title,
            # NEVER inferred from the slug (a "Hong" token used to stamp
            # "Hong Kong" here) — `models.py`'s `Opportunity.location` is
            # documented as "nothing here is inferred, guessed, or filled
            # in"; a sitemap carries no location field, so the honest answer
            # is blank, same as every other field this source doesn't have.
            "location": "",
        })
    return rows


def _sitemap_urls(xml: str) -> set[str]:
    """Every `<loc>` value in `xml`, HTML-entity-unescaped, as an exact set
    for MEMBERSHIP testing — not a raw substring scan (see `verify` below
    for the two bugs that produced).
    """
    return {html.unescape(loc) for loc in _LOC_RE.findall(xml)}


def fetch(board: SitemapBoard) -> FetchResult:
    host = urllib.parse.urlparse(board.sitemap_url).netloc
    if host:
        register_sitemap(host, board.sitemap_url)
    try:
        xml = fetch_text(board.sitemap_url)
        rows = _rows(xml, board.path_filter)
        # Kept inside this try — see greenhouse.py's fetch() for why a
        # normalization failure must not propagate uncaught out of
        # `fetch()`.
        opportunities = [
            Opportunity(firm=board.firm, title=r["title"], location=r["location"],
                        url=r["url"], source="sitemap", raw=r)
            for r in rows
        ]
    except Exception as e:  # noqa: BLE001 — board-level failure, not fatal to the run
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities, raw_count=len(rows))


def classify_url(url: str) -> dict | None:
    host = urllib.parse.urlparse(url or "").netloc.lower()
    if host and host in _SITEMAPS:
        return {"host": host}
    return None


def verify(url: str) -> VerificationResult:
    """Re-read the source sitemap: still listed -> verified-open; missing
    from its own sitemap -> closed. (The posting page itself is a JS shell
    that 200s regardless, so a page fetch proves nothing.)

    Exact `<loc>` membership (`_sitemap_urls`), not a raw `url in xml`
    substring test — that used to fail two ways: `.../job/X/123` is itself a
    string-prefix of `.../job/X/1234`, so a REMOVED posting could read as
    still-open just because a longer id happens to share its prefix; and
    sitemaps escape `&` as `&amp;`, so any URL with a query string (a `&`)
    could never match the raw, unescaped `url` and would read as closed even
    while genuinely still listed."""
    info = classify_url(url)
    if not info:
        return VerificationResult("sitemap", url, "needs-verification",
                                   "URL's host has no registered sitemap", [])
    sitemap_url = _SITEMAPS[info["host"]]
    try:
        xml = fetch_text(sitemap_url)
    except Exception as e:  # noqa: BLE001
        return VerificationResult("sitemap", url, "unreachable", str(e)[:200], [])
    if url in _sitemap_urls(xml):
        return VerificationResult("sitemap", url, "verified-open",
                                   "URL is still listed in the site's own sitemap", [])
    return VerificationResult("sitemap", url, "closed",
                               "URL is no longer listed in the site's own sitemap", [])
