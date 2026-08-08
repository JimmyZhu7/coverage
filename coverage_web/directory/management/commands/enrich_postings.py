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


def fetch_posting(url: str, *, greenhouse_token: str | None = None
                  ) -> tuple[str | None, str]:
    """(text, stated_location) — the posting's own words plus whatever
    location its page's structured data states, or (None, "") when the page
    could not be read.

    None vs "" on the text matters: an unreachable page is retried next run;
    a reachable page that says nothing is recorded as answered. The location
    is never derived — it is the provider's own field (Workday's location +
    country, iCIMS/JSON-LD jobLocation, Greenhouse's location and offices) or
    it is empty.
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
                    return page_text(body), location
        except (requests.RequestException, ValueError):
            return None, ""
        return None, ""

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
                    return page_text(body), location
        except (requests.RequestException, ValueError):
            return None, ""
        return None, ""

    if _ICIMS_URL.match(url or ""):
        url = f"{url}{'&' if '?' in url else '?'}in_iframe=1"
    try:
        resp = requests.get(url, headers=UA, timeout=TIMEOUT)
    except requests.RequestException:
        return None, ""
    if resp.status_code != 200 or not resp.text:
        return None, ""
    description, location = jobposting_jsonld(resp.text)
    if description:
        return page_text(description), location
    return page_text(resp.text), location


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
        closing_soon = (o.deadline is not None
                        and 0 <= (o.deadline - today).days <= URGENT_WINDOW_DAYS)
        if closing_soon and age >= min(URGENT_STALE_DAYS, stale_days):
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
            Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
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

            # The board's own payload first: free, unblockable, and on some
            # boards the only copy we are allowed to have.
            location = ""
            text = payload_text(o.raw)
            if text:
                from_payload += 1
            else:
                text, location = fetch_posting(
                    o.url, greenhouse_token=gh_tokens.get(o.firm.slug))
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
            raw["detail_source"] = "payload" if payload_text(o.raw) else "fetch"
            o.raw = raw
            update = ["raw"]

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
