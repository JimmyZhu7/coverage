"""enrich_postings — fetch each open posting's own page and read what the
list endpoint never carried.

    python manage.py enrich_postings --dry-run --limit 20
    python manage.py enrich_postings

WHY THIS EXISTS
---------------
The product's public claim is "open roles ranked by deadline", and 24 of 894
open campus roles carried one — 2%. Not an extraction bug: sampling the stored
`raw` payloads found ZERO closing language in 12 of 12, because ATS list
endpoints return titles and metadata, ~250-500 characters, and the deadline
lives in the posting's own page. `reclassify` re-reads what is stored, so it
was already maxed out; the missing step was ever fetching the detail page.

WHAT IT DOES, PER ROW
---------------------
One polite GET of the posting URL, HTML stripped to text, then the SAME
extractors ingest uses (`extract_deadline_from_text`, `extract_sponsorship`)
— fill-only, confidence 0.6 ("reported": the posting said it, through our
regex), exactly the contract `_apply_opportunity` documents. The text is also
stored into `raw["detail_text"]` (bounded) so future re-extraction is offline
and each URL is fetched roughly once, ever.

WHAT IT REFUSES
---------------
- Overwriting a deadline that exists. Provider fields and earlier reads win;
  this only answers where nothing has.
- Hammering anyone: per-host spacing, one worker per host, a real UA, and a
  bounded read. This is 800+ requests across ~50 hosts, not 800 at one.
- Treating a fetch failure as "no deadline". A row it could not read keeps
  `detail_text` unset and will be retried next run; only a fetched page that
  genuinely states nothing is recorded as answered.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime
from collections import defaultdict, deque
from urllib.parse import urlsplit

import requests
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from directory.boards import BOARDS
from directory.classify import TARGET_BUCKETS, extract_deadline_from_text, extract_sponsorship
from directory.ingest import _parse_deadline
from directory.models import Opportunity, ScrapeRun

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
TIMEOUT = 15
PER_HOST_DELAY = 0.8          # seconds between hits to the same host
MAX_TEXT = 20_000             # chars of page text kept in raw["detail_text"]

_TAGS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_MARKUP = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# Workday's list API hands over only an aggregate count ("2 Locations") once
# a posting carries more than one location entry — never the city names —
# and `coverage_connectors.workday._normalize()` stores that count verbatim
# as `Opportunity.location`. This command already fetches and computes the
# real per-posting location (`fetch_posting`'s `location` return) and has
# stored it into `raw["detail_location"]` since that field was added, but
# nothing ever copied it back into the DISPLAYED `location` column — proof
# live in the DB: TD Securities opportunity 19411 already carries
# raw["detail_location"] = "Markham, Ontario, Canada; Scarborough, Ontario,
# Canada" while `location` still reads "2 Locations" verbatim. 1,325 open
# rows show this placeholder today.
_N_LOCATIONS_PLACEHOLDER = re.compile(r"^\d+\s+Locations?$", re.IGNORECASE)


# Workday detail pages are JS shells — a plain GET returns 0 characters of
# text (verified across raymondjames/invesco/db tenants). But the same posting
# is served as JSON from the public `wday/cxs` API the board connector already
# reads, with the full description under jobPostingInfo.jobDescription.
_WORKDAY_URL = re.compile(
    r"https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)/job/[^?#]*?([^/?#]+?)(?:/apply)?/?$"
)


def workday_api_url(url: str) -> str | None:
    m = _WORKDAY_URL.match(url or "")
    if not m:
        return None
    tenant, wd, site, job_path = m.groups()
    return (f"https://{tenant}.{wd}.myworkdayjobs.com"
            f"/wday/cxs/{tenant}/{site}/job/x/{job_path}")


# Goldman Sachs — higher.gs.com is an SPA shell too, and a generic GET picks
# up nothing but the client-rendered page's own <title> ("Careers | Goldman
# Sachs"), which is why every one of the 146 goldmansachs rows this command
# has ever touched carries that exact placeholder as `detail_text`, 132 of
# them currently open. higher.gs.com's own unauthenticated `GetRoleById`
# GraphQL operation serves the real content the list-search `GetCampusRoles`
# query (coverage_connectors/goldmansachs.py) never carries — confirmed live
# 2026-08-14 with a plain POST, no JS engine: `descriptionHtml` came back as
# a real multi-paragraph posting body.
#
# `externalSourceId` needs no new field added to the connector's existing
# `GetCampusRoles` selection: it is already sitting in the `roleId` the list
# query fetches today, as the digits before `_GS_CAMPUS`
# ("180086_GS_CAMPUS" -> "180086") — confirmed live to match
# `externalSource.sourceId` exactly, so it can be sliced straight out of the
# URL this command already has.
_GS_ROLE_URL = re.compile(r"higher\.gs\.com/roles/(\d+)_GS_CAMPUS", re.IGNORECASE)
_GS_ENDPOINT = "https://api-higher.gs.com/gateway/api/v1/graphql"
_GS_ROLE_QUERY = (
    "query GetRoleById($externalSourceId: String!, $externalSourceFetch: Boolean) { "
    "role(externalSourceId: $externalSourceId, externalSourceFetch: $externalSourceFetch) { "
    "roleId descriptionHtml locations { city state country primary } } }"
)


# iCIMS detail pages are ALSO JS shells — a plain GET of careers-sig.icims.com
# returns 351 characters of page chrome and none of the posting (which is how
# 70 SIG rows were recorded as "answered" by a page that answered nothing).
# The same URL with `in_iframe=1` is the content frame the shell itself loads:
# the posting body plus a schema.org JobPosting block whose `jobLocation` is
# the board's own structured filing of where the role sits.
_ICIMS_URL = re.compile(r"https://[\w.-]+\.icims\.com/jobs/", re.IGNORECASE)

# A careers-site URL that embeds a Greenhouse posting by id
# (jumptrading.com/hr/job?gh_jid=…, kkr.com/careers/…?gh_jid=…). The page is a
# JS shell, but Greenhouse's public board API serves the same posting —
# content plus the board's own office/location filing — keyed by the board
# token the catalog already knows.
_GH_JID = re.compile(r"[?&]gh_jid=(\d+)")

# Oracle Recruiting Cloud detail pages are ALSO JS shells — a plain GET of a
# jpmc.fa.oraclecloud.com job URL returns only the page's <title>, "JPMC
# Candidate Experience page" (43 open rows carry that literal string as
# detail_text today; another 79 have never been fetched at all). The same
# public, unauthenticated `recruitingCEJobRequisitions` search endpoint
# coverage_connectors/oracle.py already calls for board fetch/verify answers
# a re-query by requisition id (keyword=<id>) with ShortDescriptionStr /
# ExternalResponsibilitiesStr / ExternalQualificationsStr — real posting
# text, though for JPM specifically confirmed live to run only 97-163 chars
# (ShortDescriptionStr, the page's own og:description teaser) with the two
# Responsibilities/Qualifications fields empty. Real coverage, not a
# Goldman-style full-body fix — but strictly better than the bare shell
# title this command stores today.
_ORACLE_URL = re.compile(
    r"([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/en/sites/([^/?#]+)/job/(\d+)",
    re.IGNORECASE,
)
_ORACLE_SEARCH_URL = (
    "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    "?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber={site},limit=25,keyword={rid}"
)

_LD_JSON = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)


def jobposting_jsonld(page_html: str) -> tuple[str | None, str]:
    """(description_html, "city, region, country; …") from a page's
    schema.org JobPosting block, or (None, "") when the page carries none.

    Only what the posting's own structured data states — placeholder values
    ("UNAVAILABLE") are dropped, multiple jobLocation entries are kept
    separately so a multi-market posting can honestly fail the region
    agreement gate downstream instead of first-match-winning."""
    for m in _LD_JSON.finditer(page_html or ""):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict) or node.get("@type") != "JobPosting":
                continue
            locs = node.get("jobLocation") or []
            if isinstance(locs, dict):
                locs = [locs]
            parts: list[str] = []
            for place in locs:
                addr = (place or {}).get("address") or {}
                keep = [str(v) for v in (addr.get("addressLocality"),
                                         addr.get("addressRegion"),
                                         addr.get("addressCountry"))
                        if v and str(v).strip().lower() != "unavailable"]
                if keep:
                    parts.append(", ".join(keep))
            return (node.get("description") or None,
                    "; ".join(dict.fromkeys(parts)))
    return None, ""


# HSBC's careers site (a SuccessFactors Career Site Builder shell, registered
# as a `sitemap` board — see coverage_connectors/sitemap.py) states its
# JobPosting fields as inline schema.org MICRODATA (`itemprop="jobLocation"`
# with nested `<meta itemprop="addressLocality" ...>` tags), not the
# `<script type="application/ld+json">` block `jobposting_jsonld` reads —
# confirmed live: 0 of these pages carry an ld+json JobPosting block. A
# fallback so this source's own location statement isn't left unread just
# because it filed it in the other schema.org serialization.
_MICRODATA_ADDR_PART = re.compile(
    r'itemprop="address(Locality|Region|Country)"[^>]*content="([^"]*)"',
    re.IGNORECASE)


def microdata_jobposting_location(page_html: str) -> str:
    parts = [v for _, v in _MICRODATA_ADDR_PART.findall(page_html or "")
             if v and v.strip().lower() != "unavailable"]
    return ", ".join(dict.fromkeys(parts))


# ...but HSBC's microdata is itself truncated. Confirmed live 2026-08-14 on
# the same 8 rows the microdata reader was added for: the page's own
# `itemprop="addressRegion"` states content="Hong" — the identical mid-word
# cut the URL slug makes — so reading the microdata swapped a truncated
# title for a truncated LOCATION, "Central, Hong, HK", which is what those
# rows have carried since.
#
# The same page states the location correctly and in full in its VISIBLE
# text, behind a "Location:" label: "Central, Hong Kong Island, HK". That
# text is already fetched and stored verbatim in raw["detail_text"] on every
# one of these rows — every other fact this command extracts is read out of
# it — so the fuller answer was sitting unread next to the truncated one.
#
# This reads the label out of the RAW HTML rather than out of page_text()'s
# stripped output on purpose: in stripped text the value's end is
# unmarked (it runs straight into the next label, "…HK Programme Type:
# Internship") and would have to be guessed, while in the HTML the value is
# still fenced by the block element that holds it. Bounded by the first
# block close (or <br>) after the label so a stray "Location:" in prose
# cannot run away with the rest of the page.
_PLAIN_LOCATION_LABEL = re.compile(
    r"Location:(?:&nbsp;|&#160;|&#xa0;|\s)*"
    r"(?P<value>.{0,400}?)"
    r"(?:</(?:p|div|li|td|tr|section|h[1-6]|dd|dl|ul|ol)>|<br\b)",
    re.IGNORECASE | re.DOTALL)
# A location line is a handful of place names. Anything longer than this came
# from a "Location:" that introduced a sentence, not a place.
MAX_LABEL_LOCATION = 120


def plain_text_jobposting_location(page_html: str) -> str:
    """The place named by the page's own visible "Location:" label, or "" when
    it carries none (or when what follows the label reads as prose)."""
    m = _PLAIN_LOCATION_LABEL.search(page_html or "")
    if not m:
        return ""
    value = page_text(m.group("value")).strip(" ,;-")
    return value if 0 < len(value) <= MAX_LABEL_LOCATION else ""


def stated_page_location(page_html: str) -> str:
    """The location a plain page states about itself, reading BOTH its visible
    "Location:" label and its schema.org microdata and keeping the fuller of
    the two.

    Not "always prefer the label": the microdata is the only location some
    boards state at all, and a label match is only trusted over it when it
    says strictly more — every part the microdata named still appears, and
    there is more of it (HSBC's "Central, Hong, HK" vs "Central, Hong Kong
    Island, HK"). A label that instead CONTRADICTS the microdata is the one
    the page is least sure about, so the structured field keeps the row.
    """
    micro = microdata_jobposting_location(page_html)
    plain = plain_text_jobposting_location(page_html)
    if not plain:
        return micro
    if not micro:
        return plain
    parts = [p.strip() for p in micro.split(",") if p.strip()]
    fuller = (len(plain) > len(micro)
              and all(p.lower() in plain.lower() for p in parts))
    return plain if fuller else micro


# A sitemap board (see coverage_connectors/sitemap.py) has no title field of
# its own — the ingest-time title is reconstructed from the posting URL's
# slug, and HSBC's own slugs truncate long titles mid-word ("...-Hong/"
# instead of "...-Hong-Kong/", confirmed on 8 live rows: ids 1615, 1621,
# 1626-1630, 1632). The posting's own page states its real, untruncated
# title in `og:title` — HSBC's is short and clean ("Investment Banking -
# Internship", confirmed live), never the slug's city-prefixed, truncated
# form — so a fetched page can correct what the slug guessed wrong.
_OG_TITLE = re.compile(
    r'<meta[^>]+property="og:title"[^>]*content="([^"]*)"', re.IGNORECASE)


def page_title(page_html: str) -> str:
    m = _OG_TITLE.search(page_html or "")
    return html_mod.unescape(m.group(1)).strip() if m else ""


def fetch_posting(url: str, *, greenhouse_token: str | None = None
                  ) -> tuple[str | None, str, str]:
    """(text, stated_location, stated_title) — the posting's own words plus
    whatever location and title its page's own structured data states, or
    (None, "", "") when the page could not be read.

    None vs "" on the text matters: an unreachable page is retried next run;
    a reachable page that says nothing is recorded as answered. The location
    is never derived — it is the provider's own field (Workday's location +
    country, iCIMS/JSON-LD jobLocation, Greenhouse's location and offices, or
    for a plain page GET whichever of its microdata and its own visible
    "Location:" label states more — see `stated_page_location`) or it is
    empty. The title is likewise never derived — every board branch
    below except the last already has a real title from its ingest-time API
    payload, so only the final, API-less branch (a plain page GET —
    HSBC's sitemap board among them) recovers one, from the page's own
    `og:title`.
    """
    api = workday_api_url(url)
    if api:
        try:
            resp = requests.get(api, headers={**UA, "Accept": "application/json"},
                                timeout=TIMEOUT)
            if resp.status_code == 200:
                info = (resp.json() or {}).get("jobPostingInfo") or {}
                body = info.get("jobDescription") or ""
                country = info.get("country")
                country = (country.get("descriptor") or ""
                           if isinstance(country, dict) else "")
                locs = [", ".join(p for p in (info.get("location"), country) if p)]
                locs += [", ".join(p for p in (extra, country) if p)
                         for extra in info.get("additionalLocations") or ()
                         if isinstance(extra, str)]
                location = "; ".join(loc for loc in dict.fromkeys(locs) if loc)
                if body:
                    return page_text(body), location, ""
        except (requests.RequestException, ValueError):
            return None, "", ""
        return None, "", ""

    gs = _GS_ROLE_URL.search(url or "")
    if gs:
        try:
            resp = requests.post(
                _GS_ENDPOINT,
                data=json.dumps({
                    "operationName": "GetRoleById",
                    "query": _GS_ROLE_QUERY,
                    "variables": {"externalSourceId": gs.group(1),
                                  "externalSourceFetch": True},
                }).encode(),
                headers={**UA, "Content-Type": "application/json",
                         "Accept": "application/json"},
                timeout=TIMEOUT)
            if resp.status_code == 200:
                role = ((resp.json() or {}).get("data") or {}).get("role") or {}
                body = role.get("descriptionHtml") or ""
                locs = []
                for loc in role.get("locations") or []:
                    piece = ", ".join(
                        str(p) for p in (loc.get("city"), loc.get("state"),
                                          loc.get("country")) if p)
                    if piece:
                        locs.append(piece)
                location = "; ".join(dict.fromkeys(locs))
                if body:
                    return page_text(body), location, ""
        except (requests.RequestException, ValueError):
            return None, "", ""
        return None, "", ""

    jid = _GH_JID.search(url or "")
    if jid and greenhouse_token:
        gh = (f"https://boards-api.greenhouse.io/v1/boards/"
              f"{greenhouse_token}/jobs/{jid.group(1)}")
        try:
            resp = requests.get(gh, headers={**UA, "Accept": "application/json"},
                                timeout=TIMEOUT)
            if resp.status_code == 200:
                job = resp.json() or {}
                body = html_mod.unescape(job.get("content") or "")
                locs = [(job.get("location") or {}).get("name") or ""]
                locs += [o.get("location") or "" for o in job.get("offices") or ()
                         if isinstance(o, dict)]
                location = "; ".join(loc for loc in dict.fromkeys(locs) if loc)
                if body:
                    return page_text(body), location, ""
        except (requests.RequestException, ValueError):
            return None, "", ""
        return None, "", ""

    oracle_m = _ORACLE_URL.search(url or "")
    if oracle_m:
        host, site, rid = oracle_m.groups()
        search_url = _ORACLE_SEARCH_URL.format(host=host, site=site, rid=rid)
        try:
            resp = requests.get(search_url, headers={**UA, "Accept": "application/json"},
                                timeout=TIMEOUT)
            if resp.status_code == 200:
                items = (resp.json() or {}).get("items") or []
                reqs = items[0].get("requisitionList", []) if items else []
                match = next((r for r in reqs if str(r.get("Id")) == rid), None)
                if match:
                    body = "\n\n".join(
                        str(match[k]) for k in
                        ("ShortDescriptionStr", "ExternalResponsibilitiesStr",
                         "ExternalQualificationsStr")
                        if match.get(k))
                    location = match.get("PrimaryLocation") or ""
                    if body:
                        return page_text(body), location, ""
        except (requests.RequestException, ValueError):
            return None, "", ""
        return None, "", ""

    if _ICIMS_URL.match(url or ""):
        url = f"{url}{'&' if '?' in url else '?'}in_iframe=1"
    try:
        resp = requests.get(url, headers=UA, timeout=TIMEOUT)
    except requests.RequestException:
        return None, "", ""
    if resp.status_code != 200 or not resp.text:
        return None, "", ""
    description, location = jobposting_jsonld(resp.text)
    if not location:
        location = stated_page_location(resp.text)
    title = page_title(resp.text)
    if description:
        return page_text(description), location, title
    return page_text(resp.text), location, title


def has_live_api(url: str, *, greenhouse_token: str | None) -> bool:
    """True when `fetch_posting` can reach this URL through a real, live API
    call (Workday's wday/cxs JSON, Goldman's GraphQL, Greenhouse's board
    API) rather than a plain GET of the posting page.

    Why this matters: `payload_text()` below is asked first BECAUSE some
    boards' detail pages cannot be fetched at all (McKinsey resets the
    connection on any non-browser client) — for those, the board's own list
    payload is the only copy available, stale or not. But BMO's Phenom
    board pairs a list payload that carries Phenom's own frozen ML-parsed
    preview snippet (dated to the posting's original `dateCreated`) with a
    detail URL that resolves into a live Workday tenant — one this command
    can already read straight from Workday's own `wday/cxs` API, same as
    every other Workday-backed board. Asking the payload first there means
    the live page is never consulted a second time: verified live 2026-08-14
    on opportunity id 19224, whose stored deadline (2026-04-19, sourced from
    Phenom's cached snippet near its 2026-03-23 `dateCreated`) is four
    months stale next to Workday's own API, fetched fresh, stating
    08/30/2026 — and this was not a one-off: `payload_text()` returns
    non-None for 35/35 open Phenom-source rows carrying a deadline, so NONE
    of them were ever actually checked against their live Workday page.
    Reversing precedence for these reliable-API URLs fixes it at the root:
    a live, unblockable API is asked before a payload that COULD be stale,
    while a page that genuinely resets the connection (no live API,
    `has_live_api` returns False) keeps consulting its payload first, same
    as always.
    """
    if workday_api_url(url):
        return True
    if _GS_ROLE_URL.search(url or ""):
        return True
    jid = _GH_JID.search(url or "")
    if jid and greenhouse_token:
        return True
    if _ORACLE_URL.search(url or ""):
        return True
    return False


def page_text(html: str) -> str:
    """Visible-ish text from a posting page, whitespace collapsed."""
    html = _TAGS.sub(" ", html)
    text = _MARKUP.sub(" ", html)
    return _WS.sub(" ", text).strip()[:MAX_TEXT]


# How long a reading of a posting stays good. A page is not a snapshot: firms
# extend deadlines, swap the eligibility line, and add an assessment step
# without ever touching the list payload the scraper sees — so the content
# hash never moves and nothing else would ever re-read the page.
#
# Before this, the queue was "rows with no detail_text", which meant each page
# was fetched exactly ONCE, ever. A deadline we read in August would still be
# on the board in December after the firm had moved it, stated with a
# countdown and a fuse. A confidently wrong date is worse than no date, and it
# is the one failure this product cannot afford.
STALE_DAYS = 21
# Tighter for anything closing soon. The closer a deadline, the more a stale
# reading costs and the more often it is worth spending a request to confirm.
URGENT_STALE_DAYS = 7
URGENT_WINDOW_DAYS = 30
# Tighter still for a deadline that already reads as PAST on a row still
# open — this is not "closing soon", it is the mirror image and a strictly
# stronger signal: the board is showing an expired date right now, today,
# to a student deciding whether to apply. The 7-day URGENT_STALE_DAYS
# threshold was reused for this case too until round 4, which tightened it
# to one calendar DAY (measured via `(now - at).days`, so effectively a
# 24h+ wait). That was still not tight enough: render.yaml's `coverage-
# scrape` cron runs this command every 6 hours ("0 */6 * * *" — "Four passes
# a day keeps deadlines honest across US and Asia hours", by that file's own
# comment), but a whole-day gate meant an already-wrong reading could sit
# through TWO of those scheduled passes before requalifying. Confirmed live
# 2026-08-14 on the exact row this constant exists for: HSBC Sheffield WIT
# id 1618 was fetched at 01:07 UTC (reading "Aug 09", correct at the time)
# and was still sitting unrefreshed at 12:58 UTC — after the 06:00 refresh
# pass had already run — because `age.days` was still 0. HSBC's own page had
# already moved to "Aug 16" by then. Measuring in hours against the cron's
# own 6-hour cadence closes the gap to "the next scheduled pass", which is
# the tightest any periodic-refresh pipeline can do without polling more
# often than the infrastructure already runs — it cannot make this
# morning's fetch see this afternoon's site edit, but it can stop a known-
# wrong "deadline passed" reading from surviving a whole extra refresh
# cycle.
PAST_DEADLINE_STALE_HOURS = 6


def _fetched_at(o):
    raw = (o.raw or {}).get("detail_fetched")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _queue(rows, *, refetch: bool, stale_days: int, today) -> tuple[list, int]:
    """What to fetch, in the order it earns a request.

    Never-read pages first: they are the only rows that can add a deadline the
    board does not have. Then re-reads, urgent ones ahead of the rest, because
    a run is capped and the cap should fall on the least valuable work.
    """
    now = timezone.now()
    fresh, urgent_stale, stale = [], [], []
    for o in rows:
        if not o.url:
            continue
        if refetch:
            stale.append(o)
            continue
        if "detail_text" not in (o.raw or {}):
            fresh.append(o)
            continue
        if not stale_days:
            continue
        at = _fetched_at(o)
        if at is None:
            stale.append(o)          # read before we recorded when
            continue
        age = (now - at).days
        # Urgent covers two shapes, not one: a deadline approaching (the
        # original case) AND a deadline that already reads as past on a row
        # still open. The second is not "closing soon", it is closing-soon's
        # mirror image and just as urgent to confirm — it is the exact
        # signature of a stale reading actively lying to a student (HSBC
        # Sheffield WIT, id 1617/1618: read 2026-08-09 while the firm had
        # already pushed it to 2026-08-16, and the row sat outside this
        # window because `(deadline - today).days` was negative rather than
        # "soon"). A deadline already in the past is never LESS urgent to
        # verify than one 30 days out.
        days_to_deadline = (o.deadline - today).days if o.deadline is not None else None
        already_past = days_to_deadline is not None and days_to_deadline < 0
        closing_soon = days_to_deadline is not None and 0 <= days_to_deadline <= URGENT_WINDOW_DAYS
        if already_past:
            # Hours, not days: this is the one branch a whole-day gate left
            # exposed through an entire scheduled refresh pass (see
            # PAST_DEADLINE_STALE_HOURS). `stale_days` is still the operator's
            # override knob (e.g. `--stale-days 0` disables re-reads entirely,
            # already handled above), so it still bounds this branch — just
            # converted to hours rather than compared against whole days.
            age_hours = (now - at).total_seconds() / 3600
            if age_hours >= min(PAST_DEADLINE_STALE_HOURS, stale_days * 24):
                urgent_stale.append(o)
        elif closing_soon and age >= min(URGENT_STALE_DAYS, stale_days):
            urgent_stale.append(o)
        elif age >= stale_days:
            stale.append(o)
    todo = fresh + urgent_stale + stale
    return todo, len(urgent_stale) + len(stale)


# Some boards hand over the whole description in the LIST payload and never
# needed a second request at all. McKinsey is the clearest case: 38 open
# campus roles whose detail pages reset the connection on any non-browser
# client (their bot protection, which is theirs to run and not ours to defeat)
# while `whatYouWillDo`, `yourQualifications` and their siblings sit in the
# JSON the board already served us.
#
# So the payload is asked first, every time. It costs nothing, it cannot be
# blocked, and where it answers we spend no request at all.
_PROSE_MIN = 120        # a field shorter than this is a label, not prose
_PAYLOAD_MIN = 300      # below this the payload has not really described the job
# Keys that hold long strings which are never a description.
_NOT_PROSE = ("url", "link", "id", "code", "date", "image", "logo", "slug")
# This command's OWN bookkeeping keys, written into raw at the bottom of
# handle() (detail_text/detail_fetched/detail_location/detail_source). They
# must never be read back as "the board's own payload": once a
# has_live_api()==False row is fetched and detail_text is cached, every
# later run would otherwise find its own prior detail_text sitting in raw,
# mistake it for a fresh board payload, and short-circuit fetch_posting()
# forever -- freezing location/deadline/sponsorship extraction while
# detail_fetched keeps advancing (round 7: 0 of 75 open icims rows, 0/5
# lever, 0/9 talentgateway, 0/10 beisen ever recovered a detail_location
# despite already carrying detail_text).
_SELF_WRITTEN_KEYS = frozenset(
    {"detail_text", "detail_fetched", "detail_location", "detail_source"})


def payload_text(raw: dict | None) -> str | None:
    """The description a board already gave us, or None.

    Prose-like values only, unlike `classify.posting_text` which flattens
    every string for the regex extractors. This one's output is READ BY A
    PERSON in the drawer, so a wall of job IDs and city codes would be worse
    than an empty panel.
    """
    found: list[str] = []

    def walk(node, key="", depth=0):
        if depth > 6 or sum(len(f) for f in found) > MAX_TEXT:
            return
        if isinstance(node, str):
            k = key.lower()
            if k in _SELF_WRITTEN_KEYS:
                return
            if any(bad in k for bad in _NOT_PROSE):
                return
            looks_prose = ("<" in node and ">" in node) or (
                len(node) >= _PROSE_MIN and node.count(" ") >= 15)
            if looks_prose:
                found.append(node)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, k, depth + 1)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, key, depth + 1)

    walk(raw or {})
    if not found:
        return None
    text = page_text("\n\n".join(found))
    return text[:MAX_TEXT] if len(text) >= _PAYLOAD_MIN else None


class Command(BaseCommand):
    help = "Fetch posting detail pages; fill deadline + sponsorship from prose."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0,
                            help="Stop after N fetches (0 = all).")
        parser.add_argument("--refetch", action="store_true",
                            help="Fetch even rows that already carry detail_text.")
        parser.add_argument("--stale-days", type=int, default=STALE_DAYS,
                            help=f"Re-read a page this many days after the last "
                                 f"read (default {STALE_DAYS}; 0 disables).")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        tag = "[dry-run] " if dry else ""

        rows = list(
            Opportunity.objects.filter(status="open")
            .filter(
                Q(bucket__in=TARGET_BUCKETS)
                # A non-campus row can still carry a deadline WE reported
                # (confidence < 1.0, e.g. read out of BMO's Phenom-listed
                # postings at ingest) — and outside TARGET_BUCKETS it was
                # never in this command's queryset at all, so that reading
                # could never be revisited once wrong. BMO ids 19224/9485/
                # 9433 are bucket="other" and had gone unrefreshed since
                # ingest (4+ months) for exactly this reason: not "checked
                # and still stale", never checked a second time, ever. Scope
                # is narrow on purpose — only rows already showing a
                # reported date, not every non-campus posting — so this
                # doesn't turn into an unbounded enrichment run over
                # experienced-hire boards this command was never meant to
                # cover.
                | Q(deadline__isnull=False, confidence__lt=1.0)
            )
            .select_related("firm")
            .order_by("firm__name", "id")
        )
        today = timezone.localdate()
        todo, refreshed = _queue(rows, refetch=opts["refetch"],
                                 stale_days=opts["stale_days"], today=today)
        if opts["limit"]:
            todo = todo[: opts["limit"]]
            refreshed = sum(1 for o in todo if (o.raw or {}).get("detail_text"))

        run = None
        if not dry:
            run = ScrapeRun.objects.create(
                connector="enrich", started=timezone.now(), status="running")

        # Board tokens for the Greenhouse API path: a careers-site shell URL
        # (?gh_jid=…) only carries the job id; the catalog knows whose board
        # it is.
        gh_tokens = {slug: board.token for slug, board in BOARDS
                     if board.__class__.__name__ == "GreenhouseBoard"
                     and getattr(board, "token", "")}

        # Round-robin across hosts so the per-host delay overlaps instead of
        # serializing: ~50 hosts at 0.8s spacing is minutes, not hours.
        by_host: dict[str, deque] = defaultdict(deque)
        for o in todo:
            by_host[urlsplit(o.url).netloc].append(o)
        last_hit: dict[str, float] = {}

        fetched = failed = dated = sponsored = answered_blank = 0
        # Split, because they mean different things to a reader of the log: a
        # new deadline is coverage, a corrected one is a date that would have
        # been WRONG on the board tomorrow.
        dated_new = corrected = from_payload = 0
        hosts = deque(sorted(by_host))
        while hosts:
            host = hosts.popleft()
            queue = by_host[host]
            if not queue:
                continue
            wait = PER_HOST_DELAY - (time.monotonic() - last_hit.get(host, 0.0))
            if wait > 0 and len(hosts) == 0:
                time.sleep(wait)
            elif wait > 0:
                hosts.append(host)      # come back after other hosts
                continue

            o = queue.popleft()
            if queue:
                hosts.append(host)
            last_hit[host] = time.monotonic()

            # The board's own payload first, UNLESS this URL has a live,
            # unblockable API of its own (Workday/Goldman/Greenhouse) --
            # then that live source is asked first, since the payload can
            # be a frozen preview snippet the board cached at ingest time
            # (see `has_live_api`'s docstring: BMO/Phenom postings resolve
            # into a live Workday tenant, but Phenom's own list payload
            # answers first and the live page is never reached). Boards
            # with no live API (McKinsey resets the connection on a plain
            # GET) still ask the payload first, since it may be the only
            # copy reachable at all.
            location = ""
            title = ""
            token = gh_tokens.get(o.firm.slug)
            used_payload = False
            if has_live_api(o.url, greenhouse_token=token):
                text, location, title = fetch_posting(o.url, greenhouse_token=token)
                if text is None:
                    text = payload_text(o.raw)
                    used_payload = bool(text)
                    if text:
                        from_payload += 1
                    else:
                        failed += 1
                        continue
            else:
                text = payload_text(o.raw)
                used_payload = bool(text)
                if text:
                    from_payload += 1
                else:
                    text, location, title = fetch_posting(o.url, greenhouse_token=token)
                    if text is None:
                        failed += 1
                        continue
            fetched += 1

            deadline, ok = _parse_deadline(extract_deadline_from_text(text))
            sponsorship = extract_sponsorship(text)

            changes = []
            if ok and deadline and o.deadline is None:
                changes.append(f"deadline {deadline}")
                dated += 1
            elif ok and deadline and deadline != o.deadline and (o.confidence or 0.0) < 1.0:
                changes.append(f"deadline {o.deadline} -> {deadline}")
                dated += 1
            if sponsorship != "unknown" and sponsorship != (o.sponsorship or "unknown"):
                was = (o.sponsorship or "unknown")
                changes.append(f"sponsorship {sponsorship}"
                               if was == "unknown" else
                               f"sponsorship {was} -> {sponsorship}")
                sponsored += 1
            # The board only ever gave us "N Locations" — this fetch's own
            # structured location, when it recovered one, is real city text
            # and always an improvement over an opaque count. Fill-or-
            # refresh, same posture as detail_location below: a placeholder
            # today may cover a genuinely different set of cities next time
            # the same row is re-read, so this isn't gated to "only if never
            # set before".
            # A blank location is the same kind of "board never gave us one"
            # gap as the "N Locations" placeholder — a `sitemap` board (HSBC)
            # has no location field at ingest at all, so it starts out blank
            # rather than carrying that placeholder string. Recovering into
            # either is a strict improvement over what was there.
            recovers_location = bool(
                location and (not o.location
                              or _N_LOCATIONS_PLACEHOLDER.match(o.location)))
            if recovers_location:
                changes.append(f"location {o.location!r} -> {location!r}")
            # A `sitemap` board's title is reconstructed from the posting
            # URL's slug at ingest — the only title source it has — and
            # HSBC's own slugs truncate long titles mid-word ("...-Hong"
            # instead of "...-Hong-Kong"). Scoped to that one board type: every
            # other connector already carries a real title from its own API
            # at ingest, so there is nothing here for this to correct.
            title = title[:255].strip()
            recovers_title = bool(
                title and o.source == "sitemap" and title != o.title)
            if recovers_title:
                changes.append(f"title {o.title!r} -> {title!r}")
            if not changes:
                answered_blank += 1

            if changes:
                self.stdout.write(
                    f"{tag}+ {o.firm.name} — {o.title[:52]}: {', '.join(changes)}")
            if dry:
                continue

            raw = dict(o.raw or {})
            raw["detail_text"] = text
            raw["detail_fetched"] = timezone.now().isoformat()
            # The location the page's own structured data states, for
            # `reclassify`'s region_from_fields. Fill-or-refresh, never
            # erase: a page that stopped stating one is a page that stopped
            # saying, not a posting that moved nowhere.
            if location:
                raw["detail_location"] = location
            # Where it came from, because the two age differently: a payload
            # copy is refreshed by every scrape, a fetched one only here.
            raw["detail_source"] = "payload" if used_payload else "fetch"
            o.raw = raw
            update = ["raw"]

            if recovers_location:
                o.location = location[:255]
                update.append("location")

            if recovers_title:
                o.title = title
                update.append("title")

            # FILL, and on a re-read also CORRECT. Fill-only was right while a
            # page was read once and never again; with a staleness queue it
            # would make the re-read pointless — the firm moves the date, we
            # fetch the page that says so, and then decline to write it down.
            #
            # The bound: a re-read may only overwrite a date WE read out of
            # prose (confidence < 1.0). A provider's own field stays the
            # provider's. And silence still never erases: a page that has
            # stopped stating a deadline is a page that stopped saying,
            # which is not the same as a firm withdrawing one.
            if ok and deadline:
                ours = (o.confidence or 0.0) < 1.0
                if o.deadline is None or (ours and deadline != o.deadline):
                    if o.deadline is not None and deadline != o.deadline:
                        corrected += 1
                    else:
                        dated_new += 1
                    o.deadline = deadline
                    o.deadline_precision = "day"
                    # "reported": the posting said it, via our regex — the same
                    # 0.6 band ingest gives prose finds, never provider-level 1.0.
                    o.confidence = max(o.confidence or 0.0, 0.6) if not ours else 0.6
                    update += ["deadline", "deadline_precision", "confidence"]

            # The detail page outranks the list payload on sponsorship: it is
            # where the sentence actually lives. So a re-read adopts a changed
            # answer, and "unknown" (the page not saying) still never wins.
            if sponsorship != "unknown" and sponsorship != (o.sponsorship or "unknown"):
                o.sponsorship = sponsorship
                update.append("sponsorship")
            o.save(update_fields=update)

        campus = Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
        with_dl = campus.exclude(deadline=None).count()
        total = campus.count()

        if run is not None:
            # Recorded like every other stage of the pipeline. Without this a
            # week of failing enrichment looked exactly like a week of
            # nothing-new-to-read: same silence, same green board. `health.py`
            # reads these rows, and the feed's "checked N ago" line can only
            # ever be as honest as the runs it can see.
            run.finished = timezone.now()
            run.status = "ok" if fetched or not todo else "partial"
            run.stats = {
                "queued": len(todo), "fetched": fetched, "unreachable": failed,
                "refreshed": refreshed, "from_payload": from_payload,
                "deadlines": dated_new,
                "corrected": corrected,
                "sponsorship": sponsored, "silent": answered_blank,
                "coverage": f"{with_dl}/{total}",
            }
            run.save(update_fields=["finished", "status", "stats"])
        self.stdout.write(self.style.SUCCESS(
            f"{tag}{fetched} pages read, {failed} unreachable · "
            f"({from_payload} from board payloads) · "
            f"+{dated_new} deadlines, {corrected} corrected, "
            f"+{sponsored} sponsorship answers, "
            f"{answered_blank} pages state neither · "
            f"deadline coverage now {with_dl}/{total} "
            f"({100 * with_dl // max(1, total)}%)"))
