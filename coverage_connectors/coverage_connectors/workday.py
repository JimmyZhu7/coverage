"""Workday connector — the `wday/cxs/.../jobs` paginated JSON endpoint.

Ported from the original `sources.py` (`_fetch_workday_page`,
`_fetch_workday`, `_ats_normalize`'s workday branch) and `verify_rows.py`
(`_verify_workday`, `_WORKDAY_RE`). This is the dominant provider in the
original's own board list (~25 of its ~37 configured boards), so the
pagination contract is preserved exactly:

- Workday's CxS endpoint 400s outright above `limit=20` — no silent
  server-side clamp (verified live against the original's own boards) — so
  every page request uses `limit=20` regardless of how many results exist.
- The first page's reported `total` drives how many further pages to
  fetch, capped at `_MAX_JOBS` (60 — three pages) so one saturated tenant
  can't turn into an unbounded fetch.
- `tenant_host` is "{tenant}.{datacenter}" (e.g. "citi.wd5"); the tenant
  used in the URL path is always its first dot-segment.

Two bugs found and fixed while re-verifying this connector live against a
real board (Citi, 2026-07-23), both **not** carried forward from the
original:

1. **Dead posting URLs.** The original's `_ats_normalize` built the
   clickable URL as `tenant_host + externalPath` — e.g.
   `https://citi.wd5.myworkdayjobs.com/job/London--United-Kingdom/Citi-London-Military-Insight-Day_26979549`
   — which 404s (confirmed live). The real career-site URL needs the
   `site` slug between the host and `/job/…`:
   `https://citi.wd5.myworkdayjobs.com/Citi_Early_Careers_Events_Site/job/…`
   (confirmed live, HTTP 200). This connector's `_normalize` includes it.
2. **False "closed" verdicts in the URL-based verifier.** `verify_rows.py`'s
   `_WORKDAY_RE` captured the job path as `[^/?#]+` — one path segment.
   Real Workday job paths are two segments
   (`{location-slug}/{title-slug}_{reqId}`), so the original's regex
   silently truncated the second segment off every job path it classified.
   Re-querying the CxS job-detail endpoint with the truncated path returns
   a 404-shaped "not found" body with no `jobPostingInfo` — which
   `_verify_workday` reads as "posting removed" and reports **closed**,
   even though the posting is live (confirmed live: the full two-segment
   path returns 200 with real `jobPostingInfo`; the truncated one-segment
   path the original's regex captures returns the not-found body). Given
   this package's whole purpose is an honest liveness signal, silently
   porting a bug that flips "live" to "closed" would defeat the point;
   `_WORKDAY_URL_RE` below captures the full remaining path instead of
   truncating at the first `/`.

Both are documented in the project report as deliberate deviations from a
byte-for-byte port, not silent drift.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
from datetime import date

import tenacity

from .http import FetchError, fetch_json, fetch_text, post_json, unreadable
from .models import FetchResult, Opportunity, VerificationResult, WorkdayBoard

name = "workday"

_PAGE_SIZE = 20   # Workday's CxS API 400s above this — verified live, no silent clamp.
_MAX_JOBS = 2500  # 125 pages/board. Was 1500 (raised from 60, then 500 — see
                  # history below), which itself ran out of headroom: TD
                  # Securities' board grew from the 1,421 it was measured at
                  # to 1,587 (confirmed live 2026-08-13/14), tripping
                  # `truncated=True` on every fetch that landed above 1,500
                  # and blocking ingest's pair-level auto-close for the whole
                  # (TD Securities, workday) pair — 210 open rows that could
                  # no longer be auto-closed, confirmed against ScrapeRun
                  # history for the 'all' connector (runs 124, 176, 179, 182).
                  # A fixed cap sized to today's largest board is a fix that
                  # re-breaks itself the next time that board grows, so this
                  # is raised with real headroom (2,500 vs. TD's observed
                  # 1,587) rather than to the exact current total.
                  #
                  # Earlier history, preserved because the reasoning still
                  # applies at the new value: 60 (three pages) was not a
                  # coverage compromise but a correctness one — ingest closes
                  # any open row a successful fetch didn't return, so on
                  # boards reporting 186-1,371 results the cap was marking
                  # hundreds of LIVE postings closed every night. Two rows
                  # sampled from that population — a Barclays 2027 summer
                  # internship and a PwC FY27 intern role — came back
                  # verified-open from the firms' own sites while sitting in
                  # the database as closed. 500 came first and still left
                  # eight boards partial.
                  #
                  # The cap stays because an unbounded fetch against a
                  # 10,000-posting tenant is nobody's friend, and truncation
                  # remains safe regardless: it is reported via
                  # FetchResult.truncated and ingest declines to close
                  # anything on a list it knows is partial.

_WORKDAY_URL_RE = re.compile(
    # job_path is `[^?#]+`, not `[^/?#]+` -- a real Workday job path is two
    # segments ("{location-slug}/{title-slug}_{reqId}") and must not be
    # truncated at the first internal slash. See module docstring, bug (2).
    r"https?://([\w.-]+)\.myworkdayjobs\.com/([^/?#]+)(?:/job/([^?#]+))?", re.IGNORECASE
)

# The CxS *list* endpoint (`_fetch_all`, used by `fetch()`) genuinely carries
# no deadline field, as the module docstring says. But the job-DETAIL
# endpoint's `jobPostingInfo.jobDescription` (used by `verify()`'s job_path
# branch) is the firm's own HTML posting body, and some firms bake a real,
# reposting-updated deadline into it as literal text -- e.g. BMO's postings
# read "Application Deadline:</span></p>...08/30/2026" verbatim. Confirmed
# live 2026-08-14 against four BMO requisitions whose stored `deadline` was
# frozen at first-ingest values 21-82 days stale while their live
# jobDescription stated a deadline 16-21 days in the future.
#
# Keyword-gated and window-bounded, same conservative shape as the prose
# deadline extractor the rest of Coverage uses (`directory.classify
# .extract_deadline_from_text`) -- not reused directly because this package
# has no dependency on Coverage's Django app (see the package docstring's
# "no state" contract). MM/DD/YYYY is the numeric US form Workday's own
# templates emit; a bare date elsewhere in a lengthy description is NOT
# treated as a deadline without the keyword immediately preceding it.
_TAG_RE = re.compile(r"<[^>]+>")
_DEADLINE_KEYWORD_RE = re.compile(r"application\s+deadline", re.IGNORECASE)
_DATE_MDY_NUMERIC_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(20\d{2})")


def _deadline_from_description(html_description: str) -> str | None:
    """An ISO date, only when "Application Deadline" sits within ~120
    characters (HTML tags stripped first) before a fully-specified
    MM/DD/YYYY date. `None` otherwise -- including on any date that fails
    real-calendar validation, which is skipped rather than invented."""
    text = _TAG_RE.sub(" ", html_description or "")
    keyword = _DEADLINE_KEYWORD_RE.search(text)
    if not keyword:
        return None
    window = text[keyword.end():keyword.end() + 120]
    m = _DATE_MDY_NUMERIC_RE.search(window)
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _jobs_url(tenant_host: str, site: str, tenant: str = "", domain: str = "myworkdayjobs.com") -> str:
    t = tenant or tenant_host.split(".")[0]
    return f"https://{tenant_host}.{domain}/wday/cxs/{t}/{site}/jobs"


def _fetch_page(tenant_host: str, site: str, search_text: str, offset: int,
                tenant: str = "", domain: str = "myworkdayjobs.com") -> dict:
    payload = {"appliedFacets": {}, "limit": _PAGE_SIZE, "offset": offset, "searchText": search_text}
    return post_json(_jobs_url(tenant_host, site, tenant, domain), payload)


def _fetch_all(tenant_host: str, site: str, search_text: str = "",
               tenant: str = "", domain: str = "myworkdayjobs.com") -> dict:
    """Page through offset 0/20/40 (up to `_MAX_JOBS`) using the first
    page's reported `total`. Returns a dict shaped like a single Workday
    response (`{"total": N, "jobPostings": [...]}`), with `jobPostings`
    concatenated across every page actually fetched — same contract as the
    original's `_fetch_workday`."""
    first = _fetch_page(tenant_host, site, search_text, 0, tenant, domain)
    total = first.get("total", len(first.get("jobPostings", [])))
    postings = list(first.get("jobPostings", []))
    offset = _PAGE_SIZE
    while offset < min(total, _MAX_JOBS):
        page = _fetch_page(tenant_host, site, search_text, offset, tenant, domain)
        postings.extend(page.get("jobPostings", []))
        offset += _PAGE_SIZE
    return {**first, "jobPostings": postings}


# The five Workday tenants documented (coverage-firms-backlog memory,
# 2026-08-08/09) as intermittently serving the CxS endpoint's HTML app shell
# instead of its JSON in the minutes after a clean fetch, then recovering on
# their own within the day both times it was observed — TD Securities, MUFG,
# CIBC, DBS, and Santander (`boards.py`'s td/mufg/cibc/dbs/santander
# entries). A short, jittered retry buys back the freshness a single blip
# would otherwise cost this specific quintet, without adding retry weight
# (or the latency it costs on a genuine 404/config error) to the ~140 other
# boards that have never shown this failure mode.
_FLAKY_TENANT_HOSTS = frozenset({
    "td.wd3", "mufgub.wd3", "cibc.wd3", "dbs.wd3", "santander.wd3",
})


def _is_transient_workday_error(exc: BaseException) -> bool:
    """Transient only: a dropped connection, a timeout, a 5xx, or the CxS
    endpoint answering with its HTML shell instead of JSON -- the specific
    shape this quintet of tenants has been seen to flip to and back from
    within the same day (see `_FLAKY_TENANT_HOSTS` above). A definitive 4xx
    (auth, not found) is a fact about the request, not the network moment --
    retrying it would only delay an honest failure by however long the
    backoff runs.
    """
    if isinstance(exc, ValueError):
        # fetch_json's "expected JSON object/array" -- exactly what a CxS
        # call gets back when Workday serves the SPA shell instead.
        return True
    cause = exc.cause if isinstance(exc, FetchError) else exc
    if isinstance(cause, urllib.error.HTTPError):
        return cause.code >= 500
    return isinstance(cause, (urllib.error.URLError, TimeoutError, ConnectionError))


@tenacity.retry(
    retry=tenacity.retry_if_exception(_is_transient_workday_error),
    stop=tenacity.stop_after_attempt(4),
    wait=tenacity.wait_exponential_jitter(initial=1, max=15),
    reraise=True,
)
def _fetch_all_retrying(tenant_host: str, site: str, search_text: str = "",
                        tenant: str = "", domain: str = "myworkdayjobs.com") -> dict:
    """`_fetch_all`, with up to 4 attempts and exponential-plus-jitter
    backoff on a transient failure. Only ever called for
    `_FLAKY_TENANT_HOSTS` (see `fetch()` below) -- every other board still
    goes through plain `_fetch_all`, unchanged."""
    return _fetch_all(tenant_host, site, search_text, tenant, domain)


# A house number followed by a word, at the very start: "890 Herron Road,
# Montreal, Quebec", "1060-1068 Stelton Road, Piscataway, New Jersey".
# Anchored and bounded so a real place that merely contains digits
# ("2 Locations", "Milano Bicocca Calendario 3") is never touched.
_STREET_HEAD = re.compile(r"^\d{1,6}(?:-\d{1,6})?[ -]\S")
_SLOT_GAP = re.compile(r"\s{2,}")


def normalize_locations_text(text: str) -> str:
    """Punctuate Workday's `locationsText` run so it reads as a place.

    Workday joins City / State / Country with single spaces and leaves the
    slot EMPTY when it has no value, which is why Citi's Hong Kong roles
    arrive as "Hong Kong  Hong Kong" (city / no state / country) and the
    London ones as "London  United Kingdom". The doubled name is not a
    duplicated token — "Kowloon  Hong Kong" has the same shape with a
    different city — so deduplicating repeated words would fix a bug that
    does not exist and would mangle "New York New York United States", where
    the city really is also the state.

    The genuine defect is that the run reaches the student unpunctuated. A
    2+ space gap is Workday's own slot boundary, so it is the one place the
    string can be segmented without guessing: it becomes a comma. Runs joined
    by single spaces carry no boundary information and are left exactly as
    they arrived rather than invented into.

    Second rule, same posture: a leading street address is dropped when a
    PLACE NAME follows it. "890 Herron Road, Montreal, Quebec" tells a student
    scanning a feed nothing "Montreal, Quebec" does not, and a suite number
    reads as leaked source data. What survives has to look like a place,
    though — "2121 N Pearl St, Suite 500 212" is an address in both halves,
    and trimming it to "Suite 500 212" would be a worse string than the one it
    replaced, so that row keeps what the board gave it. Dropping detail is
    honest; inventing it is not, and the raw `locationsText` is still kept
    verbatim in `Opportunity.raw` either way.
    """
    if not text:
        return text
    parts = [p.strip(" ,;") for p in _SLOT_GAP.split(text.strip())]
    joined = ", ".join(p for p in parts if p)
    fields = [f.strip() for f in joined.split(",") if f.strip()]
    if (len(fields) >= 2
            and _STREET_HEAD.match(fields[0])
            and any(not any(ch.isdigit() for ch in f) for f in fields[1:])):
        fields = fields[1:]
    return ", ".join(fields)


def _normalize(job: dict, board: WorkdayBoard) -> Opportunity:
    path = job.get("externalPath", "")
    # Must include board.site between the host and the job path -- see
    # module docstring, bug (1). tenant_host + path alone 404s (confirmed
    # live against a real Citi posting).
    url = f"https://{board.tenant_host}.{board.domain}/{board.site}{path}" if path else ""
    return Opportunity(
        firm=board.firm,
        title=job.get("title", ""),
        # The board's raw run is still kept verbatim in `raw` (raw=job below),
        # so normalizing here loses nothing and repairs what the student reads.
        location=normalize_locations_text(job.get("locationsText", "")),
        url=url,
        source="workday",
        posted_at=job.get("postedOn") or None,
        raw=job,
    )


def fetch(board: WorkdayBoard) -> FetchResult:
    try:
        # See `_FLAKY_TENANT_HOSTS` above: only this documented quintet pays
        # for the extra retry attempts and backoff.
        fetch_all = (_fetch_all_retrying if board.tenant_host in _FLAKY_TENANT_HOSTS
                     else _fetch_all)
        data = fetch_all(board.tenant_host, board.site, board.search_text,
                         board.tenant, board.domain)
        # CxS always sends `jobPostings`, empty or not. Its absence means the
        # response is not a CxS search result — the SPA shell, an error
        # envelope, a WAF page that happened to parse as JSON — and
        # `.get("jobPostings", [])` read every one of those as "this site has
        # no postings". Workday fails loudly on a renamed site (404) or a
        # wrong tenant (422), so the case left over is precisely the one no
        # status code covers.
        if "jobPostings" not in data:
            return FetchResult(
                board=board, ok=False, opportunities=[], raw_count=0,
                error=unreadable(
                    f"workday response for site {board.site!r} carries no "
                    f"'jobPostings' key (got {sorted(data)[:6]})"),
            )
        jobs = data.get("jobPostings", [])
        # Kept inside this try — see greenhouse.py's fetch() for why: a
        # normalization failure on one malformed job must not propagate
        # uncaught, which would make `fetch_many`'s `list(pool.map(...))`
        # discard every OTHER board's already-fetched results too.
        opportunities = [_normalize(j, board) for j in jobs]
    except Exception as e:  # noqa: BLE001
        return FetchResult(board=board, ok=False, opportunities=[], raw_count=0, error=str(e))
    total = data.get("total", len(jobs))
    # A stated total above zero with nothing in the array is not an empty
    # board; the list was filtered or the page came back wrong. (The reverse —
    # `total: 0` alongside real rows — is a known Workday defect on non-zero
    # offsets and is NOT read here as anything: `_fetch_all` only ever asks
    # offset 0 for its total.)
    if not jobs and isinstance(total, int) and total > 0:
        return FetchResult(
            board=board, ok=False, opportunities=[], raw_count=0,
            error=unreadable(
                f"workday reported total={total} and returned 0 jobPostings"),
        )
    return FetchResult(board=board, ok=True, opportunities=opportunities,
                        raw_count=total,
                        # The board says there are more than we read. Told to
                        # ingest rather than logged, because it changes what
                        # may be concluded from a row's absence.
                        truncated=isinstance(total, int) and total > len(jobs),
                        # `{"total": 0, "jobPostings": []}` is Workday's own
                        # honest empty result — the one 200-with-zero-rows
                        # case the platform research could reproduce
                        # deliberately.
                        empty_state=not opportunities and total == 0)


def classify_url(url: str) -> dict | None:
    """{"tenant_host", "site", "job_path", "search_text"} if `url` is a
    recognized myworkdayjobs.com URL, else None. `search_text` is read from
    a `?q=` query param if present (the same convention the original's
    verify layer used to carry a requisition-number re-query through a
    plain job-board URL).

    `job_path` is capped to its first two segments before it is returned.
    A real Workday job path is exactly two segments
    ("{location-slug}/{title-slug}_{reqId}" — see `_WORKDAY_URL_RE`'s own
    comment), but some callers store the URL with extra UI-route segments
    tacked on past the reqId — e.g. `phenom.py`'s `_normalize` stores a BMO
    posting's `url` as the feed's own `applyUrl`, which ends in a trailing
    `/apply` UI-route segment (confirmed live 2026-08-14 on
    bmo.wd3.myworkdayjobs.com: id=9514's stored URL carries one).
    `_WORKDAY_URL_RE`'s job_path group is greedy and swallows that suffix
    too, so the job-detail fetch `verify()` builds from it is
    `.../job/{location}/{title}_{reqId}/apply`, which 422s outright on
    Workday's real CxS endpoint every time — confirmed live 2026-08-14: four
    of five BMO rows sampled for the frozen-deadline defect
    (ids 9359/9490/9446/9504) carry a `/apply`-suffixed url and, uncapped,
    verify() reports "unreachable" for all four rather than ever reaching
    the deadline text the job-detail endpoint actually carries. Capping to
    the first two segments matches the documented shape and drops any
    trailing suffix regardless of what a given board happens to append —
    not just `/apply` specifically."""
    m = _WORKDAY_URL_RE.search(url or "")
    if not m:
        return None
    tenant_host, site, job_path = m.group(1), m.group(2), m.group(3)
    if job_path:
        job_path = "/".join(job_path.split("/")[:2])
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    search_text = (qs.get("q") or [None])[0]
    return {"tenant_host": tenant_host, "site": site, "job_path": job_path, "search_text": search_text}


# Cloudflare guards the CxS job-detail endpoint on some tenants and can 403
# it outright ("S22 permission denied") while the plain posting PAGE — the
# same url stored on the row — stays reachable and unblocked. Confirmed live
# 2026-08-13/14: TD Securities requisition R_1498964-1 (Opportunity id=17403,
# stored status='open') 403s from the CxS API on every attempt, but its
# posting page fetches 200 and embeds Workday's own client bootstrap JSON,
# `window.workday = {...}`, whose `postingAvailable` flag is what actually
# drives the page's client-rendered "this posting doesn't exist" state. That
# page read `postingAvailable: false`; a live sibling TD posting (id=17024)
# fetched the same way in the same run read `postingAvailable: true`, ruling
# out a rate-limit or per-tenant template artifact. Without this fallback,
# `verify()` can only ever answer "unreachable" for a row behind a
# Cloudflare-guarded CxS endpoint — including one that is genuinely gone —
# and `reverify.py` never acts on "unreachable", so the row stays open
# indefinitely no matter how many times it is re-checked.
# The key is a bare JS object literal on the real page (`postingAvailable:
# false,`), not JSON (`"postingAvailable": false`) -- confirmed live against
# id=17403's actual bootstrap script. A quoted-only pattern silently never
# matches, which is exactly how this fallback still returned "unreachable"
# after the FetchError-vs-HTTPError fix: the exception got caught correctly,
# but the flag it went looking for never matched. The quotes are optional
# here for that reason, not for defensiveness.
_POSTING_AVAILABLE_RE = re.compile(r'"?postingAvailable"?\s*:\s*(true|false)', re.IGNORECASE)


def _posting_available_from_page(url: str) -> bool | None:
    """True/False from the posting page's own `postingAvailable` flag, or
    None when the page itself couldn't be read or doesn't carry the flag —
    never a guess."""
    try:
        page = fetch_text(url)
    except Exception:  # noqa: BLE001 — the page is unreachable too; let the caller fall back
        return None
    m = _POSTING_AVAILABLE_RE.search(page)
    if not m:
        return None
    return m.group(1).lower() == "true"


def verify(url: str) -> VerificationResult:
    """Ported from `verify_rows.py`'s `_verify_workday`: hits the CxS
    job-detail endpoint when the URL carries a job path, else re-runs a
    `searchText` query keyed off the URL's own `?q=<reqnum>` — 0 results
    means closed. There is no *structured* deadline field anywhere in the
    CxS API, but the job-path branch's `jobPostingInfo.jobDescription` is
    the firm's own posting HTML, and `_deadline_from_description` reads a
    stated "Application Deadline" out of it when present — see that
    function's docstring. `deadline_dates` stays `[]` whenever the
    description carries no such text (most tenants), or when verification
    falls through the searchText branch (which has no description at all)."""
    info = classify_url(url)
    if not info:
        return VerificationResult("workday", url, "needs-verification",
                                   "URL is not a recognized myworkdayjobs.com URL", [])
    tenant_host, site = info["tenant_host"], info["site"]
    job_path, search_text = info.get("job_path"), info.get("search_text")
    try:
        if job_path:
            tenant = tenant_host.split(".")[0]
            detail_url = f"https://{tenant_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{job_path}"
            data = fetch_json(detail_url)
            posting = data.get("jobPostingInfo", {})
            title = posting.get("title", "")
            if not title:
                # An HTTP 200 with no `jobPostingInfo.title` is NOT a
                # positive "this posting is gone" signal — it is exactly as
                # consistent with a WAF/interstitial page, a rate-limit
                # envelope, a maintenance page, or Workday renaming a key,
                # all of which return 200 with a shape this code doesn't
                # recognize. Reporting "closed" here made this the
                # fallthrough for "I don't understand the response", and
                # `reverify.py` acts on "closed" with zero corroboration —
                # a one-shot deletion from the feed for the wrong reason.
                # Only a genuine gone-signal (a real 404, or a detail
                # endpoint that positively says "not found") may close a
                # row; an unrecognised-but-200 response must ask again
                # later instead.
                return VerificationResult("workday", url, "needs-verification",
                                           "job-detail endpoint returned no jobPostingInfo — "
                                           "unrecognized response shape, not a confirmed removal", [])
            posted = posting.get("postedOn", "")
            deadline = _deadline_from_description(posting.get("jobDescription", ""))
            evidence = f'title="{title}" postedOn={posted}'
            if deadline:
                evidence += f" deadline(from description)={deadline}"
            return VerificationResult("workday", url, "verified-open", evidence,
                                       [deadline] if deadline else [], posted_date=posted or None)
        if search_text:
            data = _fetch_all(tenant_host, site, search_text)
            postings = data.get("jobPostings", [])
            total = data.get("total", len(postings))
            if total == 0:
                return VerificationResult("workday", url, "closed",
                                           f'searchText="{search_text}" returned 0 postings — no longer listed', [])
            titles = "; ".join(p.get("title", "") for p in postings[:2])
            dates = [p.get("postedOn", "") for p in postings if p.get("postedOn")]
            posted_date = "; ".join(dates)
            return VerificationResult("workday", url, "verified-open",
                                       f'searchText="{search_text}" -> {total} result(s): {titles}', [],
                                       posted_date=posted_date or None)
        return VerificationResult(
            "workday", url, "needs-verification",
            f"workday tenant {tenant_host!r}/{site!r} but no job path or ?q= requisition number in the URL", [],
        )
    except (urllib.error.HTTPError, FetchError) as e:
        # A 404/410 reaches here as a bare HTTPError (fetch_bytes raises those
        # immediately, unretried). Anything else -- a 403 from Cloudflare,
        # most commonly -- gets retried by fetch_bytes and, on exhaustion,
        # wrapped in http.FetchError; the original HTTPError survives as
        # `.cause`. Missing that second shape meant this fallback silently
        # never ran: id=17403 (TD Securities) 403s on the CxS API on every
        # attempt, always arrives as FetchError, and sat stuck at
        # "unreachable" forever with the fallback below unreachable code.
        cause = e if isinstance(e, urllib.error.HTTPError) else e.cause
        code = cause.code if isinstance(cause, urllib.error.HTTPError) else None
        if job_path and code is not None:
            # The CxS API itself is blocked -- fall back to the posting page
            # the row's own url already points at. See _posting_available_from_page.
            available = _posting_available_from_page(url)
            if available is False:
                return VerificationResult(
                    "workday", url, "closed",
                    f"CxS job-detail HTTP {code}; posting page's own "
                    "postingAvailable flag reads false", [])
            if available is True:
                return VerificationResult(
                    "workday", url, "verified-open",
                    f"CxS job-detail HTTP {code}; posting page's own "
                    "postingAvailable flag reads true", [])
        if code is not None:
            return VerificationResult("workday", url, "unreachable", f"HTTP {code} from Workday CxS", [])
        return VerificationResult("workday", url, "unreachable", str(e)[:200], [])
    except Exception as e:  # noqa: BLE001
        return VerificationResult("workday", url, "unreachable", str(e)[:200], [])
