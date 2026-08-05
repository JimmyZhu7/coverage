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

import re
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

from directory.classify import TARGET_BUCKETS, extract_deadline_from_text, extract_sponsorship
from directory.ingest import _parse_deadline
from directory.models import Opportunity

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


def fetch_posting_text(url: str) -> str | None:
    """The posting's own words, or None when the page could not be read.

    None vs "" matters: an unreachable page is retried next run; a reachable
    page that says nothing is recorded as answered.
    """
    api = workday_api_url(url)
    if api:
        try:
            resp = requests.get(api, headers={**UA, "Accept": "application/json"},
                                timeout=TIMEOUT)
            if resp.status_code == 200:
                info = (resp.json() or {}).get("jobPostingInfo") or {}
                body = info.get("jobDescription") or ""
                if body:
                    return page_text(body)
        except (requests.RequestException, ValueError):
            return None
        return None
    try:
        resp = requests.get(url, headers=UA, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.text:
        return None
    return page_text(resp.text)


def page_text(html: str) -> str:
    """Visible-ish text from a posting page, whitespace collapsed."""
    html = _TAGS.sub(" ", html)
    text = _MARKUP.sub(" ", html)
    return _WS.sub(" ", text).strip()[:MAX_TEXT]


class Command(BaseCommand):
    help = "Fetch posting detail pages; fill deadline + sponsorship from prose."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0,
                            help="Stop after N fetches (0 = all).")
        parser.add_argument("--refetch", action="store_true",
                            help="Fetch even rows that already carry detail_text.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        tag = "[dry-run] " if dry else ""

        rows = list(
            Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
            .select_related("firm")
            .order_by("firm__name", "id")
        )
        todo = [
            o for o in rows
            if o.url
            and (opts["refetch"] or "detail_text" not in (o.raw or {}))
        ]
        if opts["limit"]:
            todo = todo[: opts["limit"]]

        # Round-robin across hosts so the per-host delay overlaps instead of
        # serializing: ~50 hosts at 0.8s spacing is minutes, not hours.
        by_host: dict[str, deque] = defaultdict(deque)
        for o in todo:
            by_host[urlsplit(o.url).netloc].append(o)
        last_hit: dict[str, float] = {}

        fetched = failed = dated = sponsored = answered_blank = 0
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

            text = fetch_posting_text(o.url)
            if text is None:
                failed += 1
                continue
            fetched += 1

            deadline, ok = _parse_deadline(extract_deadline_from_text(text))
            sponsorship = extract_sponsorship(text)

            changes = []
            if o.deadline is None and ok and deadline:
                changes.append(f"deadline {deadline}")
                dated += 1
            if (o.sponsorship or "unknown") == "unknown" and sponsorship != "unknown":
                changes.append(f"sponsorship {sponsorship}")
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
            o.raw = raw
            update = ["raw"]
            if o.deadline is None and ok and deadline:
                o.deadline = deadline
                o.deadline_precision = "day"
                # "reported": the posting said it, via our regex — the same
                # 0.6 band ingest gives prose finds, never provider-level 1.0.
                o.confidence = max(o.confidence or 0.0, 0.6)
                update += ["deadline", "deadline_precision", "confidence"]
            if (o.sponsorship or "unknown") == "unknown" and sponsorship != "unknown":
                o.sponsorship = sponsorship
                update.append("sponsorship")
            o.save(update_fields=update)

        campus = Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
        with_dl = campus.exclude(deadline=None).count()
        total = campus.count()
        self.stdout.write(self.style.SUCCESS(
            f"{tag}{fetched} pages read, {failed} unreachable · "
            f"+{dated} deadlines, +{sponsored} sponsorship answers, "
            f"{answered_blank} pages state neither · "
            f"deadline coverage now {with_dl}/{total} "
            f"({100 * with_dl // max(1, total)}%)"))
