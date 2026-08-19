"""SAP SuccessFactors RMK connector — the career site's server-rendered
`/search/` results table.

SuccessFactors Recruiting Marketing ("RMK") career sites — careers.ey.com and
the long tail of firms boards.py has been writing off as "SuccessFactors — no
connector" since 2026-07-24 (Janus Henderson) and 2026-08-08 (GIC) — serve
their job list as plain, complete HTML. No JS bootstrap, no session token, no
bot challenge: a single GET of

    {origin}/search/?q={keyword}&startrow={n}

returns a `<table>` whose `<tr class="data-row">` rows each carry the posting's
canonical URL, title and location, plus a `paginationLabel` reading
"Results 1 – 25 of 7479" that states the exact total. Verified live against
careers.ey.com 2026-08-19 (see the live smoke test).

Three properties of the platform this connector is shaped around:

1. **`startrow` is an offset, not a page number, and the page size is
   per-tenant, not platform-fixed.** careers.ey.com renders 25 rows per page;
   careers.gic.com.sg (added 2026-08-19) renders 20 — confirmed by its own
   pagination links stepping `startrow` by 20, not 25. `fetch()` therefore
   advances `startrow` by however many rows the last page actually returned,
   never by a hardcoded constant, and treats an empty page (not a short one)
   as the end of a keyword's walk; the label's stated total is what actually
   bounds the walk.

2. **`q=` is a relevance-ranked full-text search, not a filter.** `q=intern`
   on EY returns 3,852 rows because it also matches "Internal Audit" and
   "International Tax"; `q=internship` returns 88, of which the first two
   pages really are internships and the tail decays into loosely-related
   roles. That is the same posture boards.py already documents for Workday's
   `search_text` ("matches loosely — the classifier buckets the noise"), so
   the connector fetches what the keyword returns and classifies nothing
   itself. `keywords` is a TUPLE run as one search each and deduped by URL —
   the shape oracle.py already uses — because no single phrase covers
   "internship" and "trainee" and "student" on a global board.

3. **A removed posting does not 404.** RMK serves the search page instead,
   with HTTP 200. Confirmed live 2026-08-19 against a deliberately
   nonexistent requisition under EY's tenant: 200, no job-title property,
   and the search-results shell in the body. `verify()` therefore keys
   "closed" on that specific pair of signals and answers
   "needs-verification" — never "closed" — for any other unrecognized 200,
   the same conservatism workday.py's `verify()` documents for its
   200-with-no-jobPostingInfo case.

Every request here is the same unauthenticated GET the public career site
makes for any visitor.

Deployment note, recorded because it cost a debugging cycle: careers.ey.com's
certificate chains to Sectigo's newer "Public Server Authentication Root R46",
which is absent from macOS's bundled `/etc/ssl/cert.pem` (128 roots) and
present in `certifi`. On a machine with a stale system trust store the fetch
fails with CERTIFICATE_VERIFY_FAILED before any of this code runs. That is a
trust-store staleness issue on the client, not something about EY or this
connector — `SSL_CERT_FILE=$(python -c 'import certifi;print(certifi.where())')`
is the local fix, and a current container image needs nothing.
"""

from __future__ import annotations

import html as html_mod
import re
import urllib.error
import urllib.parse

from .http import BOT_BLOCK_PREFIX, bot_challenge_reason, fetch_text
from .models import FetchResult, Opportunity, SuccessFactorsBoard, VerificationResult

name = "successfactors"

# Per-keyword ceiling. A keyword this broad is a config mistake, not a board
# to page through: `q=intern` alone reports 3,852 rows on EY. The cap bounds
# a misconfigured board rather than capping a legitimate one — every keyword
# in the catalog today reports well under 100 — and a fetch that hits it
# reports `truncated`, which stops ingest closing rows it never saw.
_MAX_JOBS_PER_KEYWORD = 500

_ROW_RE = re.compile(r'<tr class="data-row">(.*?)</tr>', re.DOTALL)
# Attribute order differs between the desktop and the phone copy of the same
# anchor (RMK renders both), so href and class are matched by lookahead
# rather than by position.
_ANCHOR_RE = re.compile(
    r'<a\b(?=[^>]*\bclass="jobTitle-link")(?=[^>]*\bhref="([^"]+)")[^>]*>(.*?)</a>',
    re.DOTALL,
)
_LOCATION_RE = re.compile(r'<span class="jobLocation">(.*?)</span>', re.DOTALL)
# "Results 1 – 25 of 7479" — the trailing number is the only exact total the
# page states. Read from the label rather than counted from rows, so a short
# final page is not mistaken for the end of a longer board.
_PAGINATION_LABEL_RE = re.compile(r'class="paginationLabel"[^>]*>(.*?)</span>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
# The "+1 more…" chip is pagination chrome inside the location cell, not part
# of the place. Dropped rather than shown to a student as if it were a city.
_MORE_CHIP_RE = re.compile(r"<small[^>]*>.*?</small>", re.DOTALL | re.IGNORECASE)

# {origin}/{tenant}/job/{title-slug}/{jobId}/ — RMK's canonical posting URL.
# The trailing all-digits segment is what keeps this from claiming Workday's
# /job/{location}/{title}_{reqId} URLs, whose last segment is never numeric.
_JOB_URL_RE = re.compile(
    r"^https?://([\w.-]+)/([\w-]+)/job/([^/?#]+)/(\d+)/?(?:[?#]|$)", re.IGNORECASE
)
_TITLE_PROPERTY_RE = re.compile(
    r'data-careersite-propertyid="title"[^>]*>(.*?)</span>', re.DOTALL
)
_SEARCH_SHELL_RE = re.compile(r'id="search-results"|searchResultsShell', re.IGNORECASE)


def _text(fragment: str) -> str:
    """Tag-stripped, entity-decoded, whitespace-collapsed cell text."""
    return re.sub(r"\s+", " ", html_mod.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _search_url(origin: str, keyword: str, startrow: int) -> str:
    q = urllib.parse.urlencode({"q": keyword, "startrow": startrow})
    return f"{origin.rstrip('/')}/search/?{q}"


def _stated_total(html: str) -> int | None:
    """The exact total from the page's own pagination label, or None when the
    label is absent (an empty result set renders no label). Never guessed
    from the row count — a short page is ambiguous between "last page" and
    "this board is small"."""
    m = _PAGINATION_LABEL_RE.search(html)
    if not m:
        return None
    numbers = re.findall(r"\d[\d,]*", _text(m.group(1)))
    return int(numbers[-1].replace(",", "")) if numbers else None


def _parse(html: str, origin: str) -> list[dict]:
    rows: list[dict] = []
    for block in _ROW_RE.findall(html):
        anchor = _ANCHOR_RE.search(block)
        if not anchor:
            continue
        title = _text(anchor.group(2))
        if not title:
            continue
        location = _LOCATION_RE.search(block)
        rows.append({
            "title": title,
            "url": urllib.parse.urljoin(origin, html_mod.unescape(anchor.group(1))),
            "location": _text(_MORE_CHIP_RE.sub("", location.group(1))) if location else "",
        })
    return rows


def fetch(board: SuccessFactorsBoard) -> FetchResult:
    """One `startrow` walk per keyword (or a single unfiltered walk when
    `keywords` is empty), deduped by posting URL across keywords."""
    seen: set[str] = set()
    rows: list[dict] = []
    truncated = False
    try:
        for keyword in board.keywords or ("",):
            startrow = 0
            while True:
                html = fetch_text(_search_url(board.origin, keyword, startrow))
                challenge = bot_challenge_reason(html)
                if challenge:
                    return FetchResult(
                        board=board, ok=False, opportunities=[], raw_count=0,
                        error=f"{BOT_BLOCK_PREFIX} ({challenge}) — board unreadable, not empty",
                    )
                page = _parse(html, board.origin)
                for row in page:
                    if row["url"] in seen:
                        continue
                    seen.add(row["url"])
                    rows.append(row)
                total = _stated_total(html)
                # An empty page is the end of THIS keyword's result set
                # regardless of what the label said.
                if not page:
                    break
                startrow += len(page)
                if total is None:
                    break
                if startrow >= _MAX_JOBS_PER_KEYWORD:
                    truncated = total > startrow
                    break
                if startrow >= total:
                    break
        # Normalization stays inside this try — see greenhouse.py's fetch()
        # for why a malformed row must not raise past this function and cost
        # `fetch_many` every OTHER board's already-fetched results.
        opportunities = [
            Opportunity(firm=board.firm, title=r["title"], location=r["location"],
                        url=r["url"], source="successfactors", raw=r)
            for r in rows
        ]
    except Exception as e:  # noqa: BLE001 — board-level failure, not fatal to the run
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    return FetchResult(board=board, ok=True, opportunities=opportunities,
                       raw_count=len(rows), truncated=truncated)


def classify_url(url: str) -> dict | None:
    m = _JOB_URL_RE.match(url or "")
    if not m:
        return None
    return {"host": m.group(1), "tenant": m.group(2), "slug": m.group(3), "job_id": m.group(4)}


def verify(url: str) -> VerificationResult:
    """Fetch the posting page. RMK answers 200 for a removed requisition and
    serves the search page instead of the posting, so "closed" requires BOTH
    the absence of the job-title property AND the presence of the search
    shell; anything else unrecognized comes back needs-verification rather
    than closing a row on a response this connector doesn't understand."""
    if not classify_url(url):
        return VerificationResult("successfactors", url, "needs-verification",
                                   "URL is not a recognized SuccessFactors RMK posting URL", [])
    try:
        html = fetch_text(url)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return VerificationResult("successfactors", url, "closed", f"HTTP {e.code}", [])
        return VerificationResult("successfactors", url, "unreachable", f"HTTP {e.code}", [])
    except Exception as e:  # noqa: BLE001
        return VerificationResult("successfactors", url, "unreachable", str(e)[:200], [])

    challenge = bot_challenge_reason(html)
    if challenge:
        return VerificationResult("successfactors", url, "unreachable",
                                   f"{BOT_BLOCK_PREFIX} ({challenge})", [])

    title_m = _TITLE_PROPERTY_RE.search(html)
    if title_m:
        return VerificationResult("successfactors", url, "verified-open",
                                   f'title="{_text(title_m.group(1))}"', [])
    if _SEARCH_SHELL_RE.search(html):
        return VerificationResult(
            "successfactors", url, "closed",
            "posting page served the search results page with no job title — "
            "RMK's response for a removed requisition", [])
    return VerificationResult(
        "successfactors", url, "needs-verification",
        "HTTP 200 with neither a job-title property nor the search shell — "
        "unrecognized response shape, not a confirmed removal", [])
