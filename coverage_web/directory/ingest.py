"""Ingest service — run the shared ATS connectors, normalize, and upsert into
the shared `firms` / `opportunities` tables (build-plan.md §4, "the
shared-cache design, concretely").

Design contract
----------------
- **Shared-cache, never per-user.** One fetch populates the shared table for
  ALL users. There is no `user_id` anywhere in this module and no per-user
  filtering at ingest time — per-user relevance is a read-time query later
  (a `user_firms` join). We ingest broadly.
- **Idempotent.** Keyed on `opportunities` UNIQUE(firm_id, url). Re-running an
  identical fetch updates rows in place and never duplicates. `first_seen` is
  stamped once (Django `auto_now_add`); `last_checked` / `last_verified` /
  `content_hash` are refreshed every run.
- **Closed-detection.** A posting previously seen for a `(firm, source)` that
  is no longer returned by a *successful* fetch of that pair is flipped to
  `status="closed"`. A board that *failed* to fetch never closes anything (a
  network blip must not read as "every posting vanished"). A closed posting
  that reappears is reopened.
- **Faithful mapping.** Connector `Opportunity` fields map straight onto the
  Django columns; where the connector returns null (deadline is null for
  Lever/Workday, and for Greenhouse jobs whose firm set none) we store null,
  never a fabricated value. See `_apply_opportunity` for the field-by-field
  mapping and its documented compromises.

Network seam
------------
`ingest_boards` calls `fetch_many` (imported at module scope) exactly once,
then hands the results to `ingest_results`, which is pure DB work. Tests patch
`directory.ingest.fetch_many` to inject crafted `FetchResult`s and exercise the
upsert / closed / reopen logic with no live network.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Iterable

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from coverage_connectors import BoardConfig, FetchResult, Opportunity as ConnOpportunity, fetch_many

from .classify import board_is_campus, classify_role, clean_title, extract_cohort, normalize_region
from .models import Firm, Opportunity, ScrapeRun

# Connector confidence-band string labels are absent for ATS postings (the
# connectors deliberately never invent a confidence for an API value), so
# opportunities land with the model default 0.0 confidence — see
# _apply_opportunity.


# --------------------------------------------------------------------------- hashing

_HASH_FIELDS = ("title", "location", "deadline", "region", "cohort", "sponsorship", "source")


def content_hash_for(opp: ConnOpportunity) -> str:
    """Stable sha256 over the posting's content-bearing fields. Drives
    change-detection (did this row's substance move since last run?) — it
    deliberately excludes `status`, `posted_at`, and any ingest bookkeeping so
    that re-fetching an unchanged posting produces an identical hash."""
    payload = {
        "title": opp.title or "",
        "location": opp.location or "",
        "deadline": opp.deadline or "",
        "region": opp.region or "",
        "cohort": opp.cohort or "",
        "sponsorship": opp.sponsorship or "",
        "source": opp.source or "",
    }
    blob = json.dumps({k: payload[k] for k in _HASH_FIELDS if k in payload}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse_deadline(value: str | None) -> date | None:
    """`YYYY-MM-DD` -> date, anything else -> None. The connectors already
    slice provider deadlines to `[:10]`, so this only ever sees ISO dates or
    None; a malformed value degrades to null rather than raising."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- firm resolve

class _FirmResolver:
    """Resolve a connector board's firm NAME to a `Firm` row, caching within a
    run. Match order: exact-name (case-insensitive) -> slug(name) -> create.

    Auto-create is intentional: build-plan §4 says ingest broadly, so a live
    board whose firm isn't in the seed set (e.g. Palantir on Lever) still lands
    its postings under a freshly-created shared `Firm` rather than being
    dropped. The `scrape` command sets `board.firm` to the seeded `Firm.name`
    exactly, so seeded firms always hit the name match and never fork."""

    def __init__(self) -> None:
        self._cache: dict[str, Firm] = {}
        self.created_firms: list[str] = []

    def resolve(self, name: str) -> Firm:
        key = (name or "").strip().lower()
        if key in self._cache:
            return self._cache[key]
        firm = Firm.objects.filter(name__iexact=name).first()
        if firm is None:
            slug = slugify(name) or "firm"
            firm = Firm.objects.filter(slug=slug).first()
        if firm is None:
            slug = self._unique_slug(slugify(name) or "firm")
            firm = Firm.objects.create(slug=slug, name=name, status="active")
            self.created_firms.append(firm.slug)
        self._cache[key] = firm
        return firm

    @staticmethod
    def _unique_slug(base: str) -> str:
        slug, i = base, 2
        while Firm.objects.filter(slug=slug).exists():
            slug = f"{base}-{i}"
            i += 1
        return slug


# --------------------------------------------------------------------------- upsert

def _apply_opportunity(firm: Firm, opp: ConnOpportunity, now, stats: dict, *, campus_hint: bool = False) -> None:
    """Create or update the one `opportunities` row keyed by (firm, url).

    Field mapping (connector `Opportunity` -> Django `Opportunity` column):
      firm            -> firm FK (already resolved)
      title           -> title
      location        -> location
      url             -> url                     (the dedup key; blank url skipped upstream)
      source          -> source                  ("greenhouse"|"lever"|"workday")
      status          -> status = "open"         (present in a live fetch right now)
      region          -> region (or "")          connector never populates region -> ""
      deadline        -> deadline (date|None)     Greenhouse's real application_deadline
                                                  when set; null for Lever/Workday and for
                                                  Greenhouse jobs with none -> stored null,
                                                  NOT fabricated
      deadline (set)  -> deadline_precision="day" a real API date is day-precise; "" when null
      cohort          -> cohort (or "")           connector: always None -> ""
      sponsorship     -> sponsorship (or default) connector: always None -> "unknown"
      (computed)      -> content_hash, first_seen, last_verified, last_checked

    Dropped on purpose (no destination column in the Django model, which omits
    them by design): `posted_at` (evidence-only "posted/updated" date) and
    `raw` (the provider's raw JSON). `confidence` has no connector source and
    keeps its model default (0.0). See project report.

    `bucket` and (when the connector gives none) `cohort` are DERIVED here via
    directory.classify — the one deliberate exception to "store only what the
    API said". The connectors refuse to invent taxonomy, but the calendar's
    whole promise is the insight/internship/entry_level buckets, so the
    classification happens at ingest where it is testable and re-runnable
    (`manage.py reclassify`). Derived values stay OUT of `content_hash`, which
    keeps hashing purely connector-based: a classifier upgrade changes buckets
    on the next run without spuriously reporting every posting as "updated".
    `campus_hint` says the source board is itself campus-scoped (see
    `classify.board_is_campus`), which lets neutral Analyst titles on a
    students board classify as entry-level.
    """
    deadline = _parse_deadline(opp.deadline)
    h = content_hash_for(opp)
    bucket = classify_role(opp.title or "", campus_hint=campus_hint)
    cohort = opp.cohort or extract_cohort(opp.title or "")
    # Clip to the storage columns' width (CharField(255)). A few boards carry
    # multi-city locations or very long titles that overrun the column and
    # would 500 the whole scrape (EQT posts roles listing 3+ office cities).
    # The full value still feeds `content_hash`, so change-detection is
    # unaffected. `[:255]` is safe on "" (opp.title/location are never None).
    title = clean_title(opp.title)[:255]
    location = (opp.location or "")[:255]
    # Canonical market (hk/us/sg/eu or "") derived from the location — a
    # connector never sets region, so the location text is the only signal.
    region = (opp.region or "").strip() or normalize_region(location)

    existing = Opportunity.objects.filter(firm=firm, url=opp.url).first()
    if existing is None:
        Opportunity.objects.create(
            firm=firm,
            title=title,
            bucket=bucket,
            location=location,
            url=opp.url,
            source=opp.source or "",
            status="open",
            region=region,
            deadline=deadline,
            deadline_precision="day" if deadline else "",
            cohort=cohort,
            sponsorship=opp.sponsorship or "unknown",
            content_hash=h,
            last_verified=now,
            last_checked=now,
        )
        stats["created"] += 1
        return

    was_closed = existing.status == "closed"
    changed = existing.content_hash != h

    existing.title = title
    existing.bucket = bucket
    existing.location = location
    existing.source = opp.source or ""
    existing.region = region
    existing.deadline = deadline
    existing.deadline_precision = "day" if deadline else ""
    existing.cohort = cohort
    existing.sponsorship = opp.sponsorship or "unknown"
    existing.content_hash = h
    existing.status = "open"
    existing.last_verified = now
    existing.last_checked = now
    existing.save()

    if was_closed:
        stats["reopened"] += 1
    elif changed:
        stats["updated"] += 1
    else:
        stats["unchanged"] += 1


# --------------------------------------------------------------------------- results -> DB

@transaction.atomic
def ingest_results(results: Iterable[FetchResult], *, mark_closed: bool = True) -> dict:
    """Upsert every opportunity from `results`, then (optionally) close the
    postings that a *successful* fetch of a `(firm, source)` pair no longer
    returns. Pure DB work — no network. Runs in one transaction so a mid-ingest
    failure leaves the shared table untouched. Returns a stats dict.
    """
    now = timezone.now()
    resolver = _FirmResolver()
    stats = {
        "boards_total": 0, "boards_ok": 0, "boards_failed": 0,
        "fetched": 0, "skipped_no_url": 0,
        "created": 0, "updated": 0, "unchanged": 0, "reopened": 0, "closed": 0,
        "providers": set(), "firms_touched": set(), "created_firms": [],
        "errors": [],
    }

    seen_by_pair: dict[tuple[int, str], set[str]] = {}
    pair_all_ok: dict[tuple[int, str], bool] = {}

    for result in results:
        stats["boards_total"] += 1
        board: BoardConfig = result.board
        source = board.provider
        stats["providers"].add(source)
        firm = resolver.resolve(board.firm)
        stats["firms_touched"].add(firm.slug)
        pair = (firm.id, source)
        pair_all_ok.setdefault(pair, True)
        seen_by_pair.setdefault(pair, set())

        if not result.ok:
            stats["boards_failed"] += 1
            pair_all_ok[pair] = False
            stats["errors"].append({"firm": board.firm, "provider": source, "error": result.error or "unknown"})
            continue

        stats["boards_ok"] += 1
        campus = board_is_campus(board)
        for opp in result.opportunities:
            stats["fetched"] += 1
            if not opp.url:
                stats["skipped_no_url"] += 1
                continue
            seen_by_pair[pair].add(opp.url)
            _apply_opportunity(firm, opp, now, stats, campus_hint=campus)

    if mark_closed:
        for pair, all_ok in pair_all_ok.items():
            if not all_ok:
                continue  # a board for this (firm, source) failed -> never close on partial data
            firm_id, source = pair
            seen = seen_by_pair.get(pair, set())
            closed = (
                Opportunity.objects.filter(firm_id=firm_id, source=source)
                .exclude(status="closed")
                .exclude(url__in=seen)
                .update(status="closed", last_checked=now)
            )
            stats["closed"] += closed

    stats["created_firms"] = resolver.created_firms
    # Make the stats JSON-serializable for the ScrapeRun.stats jsonb column.
    stats["providers"] = sorted(stats["providers"])
    stats["firms_touched"] = sorted(stats["firms_touched"])
    return stats


# --------------------------------------------------------------------------- public entry

def _derive_label(boards: list[BoardConfig]) -> str:
    providers = sorted({b.provider for b in boards})
    if not providers:
        return "none"
    return providers[0] if len(providers) == 1 else "mixed:" + ",".join(providers)


def ingest_boards(
    boards: list[BoardConfig], *, label: str | None = None, mark_closed: bool = True
) -> ScrapeRun:
    """Run one scrape: fetch every board once via `fetch_many`, upsert into the
    shared tables, and record the run in `scrape_runs`.

    Returns the finalized `ScrapeRun`. `ScrapeRun.stats` holds the full counter
    dict from `ingest_results`; `status` is "ok" (all boards fetched),
    "partial" (some failed), or "error" (all failed / nothing fetched).
    """
    started = timezone.now()
    run = ScrapeRun.objects.create(
        connector=label or _derive_label(boards), started=started, status="running"
    )
    try:
        results = fetch_many(boards) if boards else []
        stats = ingest_results(results, mark_closed=mark_closed)
    except Exception as exc:  # noqa: BLE001 — a hard failure still gets an honest run record
        run.finished = timezone.now()
        run.status = "error"
        run.error = str(exc)[:2000]
        run.save()
        raise

    run.finished = timezone.now()
    run.stats = stats
    if stats["boards_total"] == 0 or stats["boards_ok"] == 0:
        run.status = "error"
    elif stats["boards_failed"]:
        run.status = "partial"
    else:
        run.status = "ok"
    if stats["errors"]:
        run.error = "\n".join(f"{e['firm']} ({e['provider']}): {e['error']}" for e in stats["errors"])[:2000]
    run.save()
    return run
