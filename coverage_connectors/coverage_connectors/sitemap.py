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

import re
import urllib.parse

from .http import fetch_text
from .models import FetchResult, Opportunity, SitemapBoard, VerificationResult

name = "sitemap"

_LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
_SLUG_RE = re.compile(r"/job/([^/]+)/\d+/?$")
# HSBC slugs truncate "…-Hong-Kong" to "…-Hong"; a bare "Hong" token still
# reliably identifies a Hong Kong posting in that feed (verified: no
# Singapore/UK row carries it), hence token- rather than phrase-matching.
_HK_TOKEN_RE = re.compile(r"\bhong\b", re.IGNORECASE)

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
            "location": "Hong Kong" if _HK_TOKEN_RE.search(slug) else "",
        })
    return rows


def fetch(board: SitemapBoard) -> FetchResult:
    host = urllib.parse.urlparse(board.sitemap_url).netloc
    if host:
        register_sitemap(host, board.sitemap_url)
    try:
        xml = fetch_text(board.sitemap_url)
    except Exception as e:  # noqa: BLE001 — board-level failure, not fatal to the run
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    rows = _rows(xml, board.path_filter)
    opportunities = [
        Opportunity(firm=board.firm, title=r["title"], location=r["location"],
                    url=r["url"], source="sitemap", raw=r)
        for r in rows
    ]
    return FetchResult(board=board, ok=True, opportunities=opportunities, raw_count=len(rows))


def classify_url(url: str) -> dict | None:
    host = urllib.parse.urlparse(url or "").netloc.lower()
    if host and host in _SITEMAPS:
        return {"host": host}
    return None


def verify(url: str) -> VerificationResult:
    """Re-read the source sitemap: still listed -> verified-open; missing
    from its own sitemap -> closed. (The posting page itself is a JS shell
    that 200s regardless, so a page fetch proves nothing.)"""
    info = classify_url(url)
    if not info:
        return VerificationResult("sitemap", url, "needs-verification",
                                   "URL's host has no registered sitemap", [])
    sitemap_url = _SITEMAPS[info["host"]]
    try:
        xml = fetch_text(sitemap_url)
    except Exception as e:  # noqa: BLE001
        return VerificationResult("sitemap", url, "unreachable", str(e)[:200], [])
    if url in xml:
        return VerificationResult("sitemap", url, "verified-open",
                                   "URL is still listed in the site's own sitemap", [])
    return VerificationResult("sitemap", url, "closed",
                               "URL is no longer listed in the site's own sitemap", [])
