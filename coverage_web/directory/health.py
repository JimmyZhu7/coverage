"""Scrape-health checks: the failures that were happening in silence.

Two blind spots, both observed on the live system before this existed:

1. REPEAT FAILURES. Evercore's board timed out in 5 of 14 daily runs and the
   only trace was a line inside `ScrapeRun.stats["errors"]` — a JSON blob
   nothing read. A board that fails quietly presents stale data as fresh:
   the rows keep their old `last_verified`, the feed keeps rendering them,
   and nobody knows the firm stopped being checked.

2. CONFIGURED-BUT-NEVER-YIELDS. Jefferies has a board entry in the catalog
   and zero rows ever. That is indistinguishable, from the outside, from
   "Jefferies has no openings" — but it is actually "the board URL is wrong
   or lists nothing", which is a configuration bug wearing an empty feed as
   a disguise.

Pure functions over already-recorded data (ScrapeRun rows + the board
catalog); the `refresh` command prints their findings at the end of every
run, so the place a failure would hide is the place it now gets announced.
"""

from __future__ import annotations

from coverage_connectors.http import BOT_BLOCK_PREFIX
from django.db.models import Count

from .boards import BOARDS
from .models import Firm, Opportunity, ScrapeRun

# A firm failing in this many consecutive scrape runs is a pattern, not a
# blip. The transient retry (coverage_connectors.fetch_with_retry) already
# absorbed one network hiccup per run before the failure was recorded.
CONSECUTIVE_FAILURES = 3

# How many runs back to walk when dating a bot wall's streak. Two runs a day
# means this covers a month — plenty to say "since when" without an unbounded
# scan.
WALL_STREAK_SCAN_RUNS = 60


# Not every entry in `stats["errors"]` is a failure. Two of them are the
# ingest's own safety guards REPORTING THEMSELVES: the truncation guard
# ("board reported more results than one fetch returns") and the suspicious-
# wipe guard ("fetch ok but 0 rows while postings are open"). In both cases
# the fetch SUCCEEDED — `boards_ok` was incremented — and the guard simply
# declined to auto-close on a list it knew was incomplete, which is the
# correct and desired behaviour.
#
# Measured 2026-08-18: J.P. Morgan and Lazard (both oracle, both genuinely
# healthy — 146 and 73 open rows, none stale) had been tripping the
# "failing in each of the last 3 scrape runs / stale data being presented as
# fresh" alarm every single run, because `repeat_failures` read every
# `errors` entry as a failure. That is the exact trap this module already
# documents for bot walls: an alarm that fires daily on a working system
# teaches the reader to ignore the line that exists to catch real breakage.
#
# Matched on the message text rather than a new flag on purpose —
# `walled_boards` walks 60 historical runs, and a flag written from today
# forward would misread every `ScrapeRun` already in the table.
_GUARD_NOTICE_MARKER = "skipped auto-close"


def _is_walled(err: dict) -> bool:
    return (err.get("error") or "").startswith(BOT_BLOCK_PREFIX)


def _is_guard_notice(err: dict) -> bool:
    """A guard declining to act on incomplete data — not a fetch failure."""
    return _GUARD_NOTICE_MARKER in (err.get("error") or "")


def _is_real_failure(err: dict) -> bool:
    return not _is_walled(err) and not _is_guard_notice(err)


def repeat_failures(limit_runs: int = CONSECUTIVE_FAILURES) -> list[str]:
    """Firm names that appear in the errors of EVERY one of the last N scrape
    runs. Consecutive, not cumulative: a firm that failed once last week and
    once today is having bad luck; one failing every run is down.

    Bot-walled boards are excluded: a wall the operator put up deliberately
    fails every run by definition, and letting it trip this alarm daily
    teaches the reader to ignore the line that exists to catch fixable
    breakage. Walls get their own report (`walled_boards`).

    Only FULL scrapes (connector="all") count. The old exclude-reverify
    filter let two things poison the window: `enrich`/`extract` rows carry
    no errors at all, so with them interleaved (one each per refresh) the
    three-run intersection was permanently empty and this alarm could never
    fire; and a `firm:x`-scoped run says nothing about the other ninety
    boards yet would have reset every other firm's streak."""
    runs = list(
        ScrapeRun.objects.filter(connector="all")
        .order_by("-started")[:limit_runs]
    )
    if len(runs) < limit_runs:
        return []
    per_run = [
        {e.get("firm", "") for e in (r.stats or {}).get("errors", [])
         if _is_real_failure(e)}
        for r in runs
    ]
    always = set.intersection(*per_run) - {""}
    return sorted(always)


def walled_boards() -> list[dict]:
    """Firms whose latest scrape hit a bot-protection interstitial, each with
    the streak's start and how many open rows are frozen behind the wall.

    A wall is a standing condition, not an outage: the operator decided the
    board needs a human check, so it will "fail" every run until they change
    their mind, and no config edit on this side fixes it. The report keeps it
    visible (frozen rows age silently otherwise) without letting it wear the
    alarm styling that belongs to fixable failures.

    `is_new` is True only while the latest run is the streak's FIRST — the
    one moment a wall deserves to be loud. Every run after that it is known
    history.

    Full scrapes only, same reasoning as `repeat_failures`: a `firm:hps`
    run that fetched cleanly must not read as "the wall came down"."""
    runs = list(
        ScrapeRun.objects.filter(connector="all")
        .order_by("-started")[:WALL_STREAK_SCAN_RUNS]
    )
    if not runs:
        return []
    latest_walled = {
        e.get("firm", "") for e in (runs[0].stats or {}).get("errors", [])
        if _is_walled(e)
    } - {""}
    if not latest_walled:
        return []

    open_rows = dict(
        Opportunity.objects.exclude(status="closed")
        .filter(firm__name__in=latest_walled)
        .values_list("firm__name")
        .annotate(n=Count("id"))
        .values_list("firm__name", "n")
    )

    out = []
    for firm in sorted(latest_walled):
        since = runs[0].started
        streak = 1
        for r in runs[1:]:
            if firm in {e.get("firm", "") for e in (r.stats or {}).get("errors", [])
                        if _is_walled(e)}:
                since = r.started
                streak += 1
            else:
                break
        out.append({
            "firm": firm,
            "since": since,
            "is_new": streak == 1,
            "open_rows": open_rows.get(firm, 0),
        })
    return out


def duplicate_firms() -> list[dict]:
    """Firm rows sharing a name, with the postings stranded behind each
    non-canonical copy.

    THE INCIDENT. Ids 199 (slug `td`) and 207 (slug `td-closed`) both carry
    the name "TD Securities" — one seeded, one minted by a test fixture run
    directly against the dev database on 2026-08-15. Measured 2026-08-18:
    1,475 URLs exist under both firms and 1,258 of them are open on BOTH
    sides right now, which is 1,258 duplicate cards in a feed whose whole
    pitch is that it is more trustworthy than the spreadsheet it replaces.

    WHY IT IS WORSE THAN COSMETIC. The board catalog keys on slug, and
    `ingest._FirmResolver` resolves a board's firm NAME to the lowest-id
    match. So every scrape lands on 199, and nothing ever resolves to 207:
    its rows are unreachable by any board, so they are never re-fetched,
    never re-verified, and never closed. They sit open forever at whatever
    they said the day the split happened. Staleness elsewhere in this
    pipeline self-heals on the next pass; this does not, because there is
    no next pass for those rows.

    WHY THIS CHECK EXISTS. `_FirmResolver`'s `.order_by("id")` fix stopped a
    collision from WIDENING, and `manage.py merge_duplicate_firms` can clean
    one up — but between them nothing ever said a collision was there. The
    live one went unnoticed for three days across roughly a dozen scheduled
    scrapes. That is exactly this module's subject: a failure whose only
    symptom is data quietly going wrong while every count stays green.

    Cheap in the healthy case, which is the one that runs nightly: one
    aggregate query returning nothing. The per-group work only happens when
    a collision actually exists.
    """
    from .firm_merge import find_duplicate_firm_groups

    groups = find_duplicate_firm_groups()
    if not groups:
        return []

    # Only the non-canonical rows strand postings — the canonical (lowest-id)
    # row is the one every board already resolves to.
    stranded_ids = [f.id for group in groups for f in group[1:]]
    frozen = dict(
        Opportunity.objects.exclude(status="closed")
        .filter(firm_id__in=stranded_ids)
        .values_list("firm_id")
        .annotate(n=Count("id"))
        .values_list("firm_id", "n")
    )
    return [
        {
            "name": group[0].name,
            "canonical": group[0].slug,
            "duplicates": [f.slug for f in group[1:]],
            "stranded_rows": sum(frozen.get(f.id, 0) for f in group[1:]),
        }
        for group in groups
    ]


def guarded_boards() -> list[str]:
    """Firms whose latest full scrape hit a guard that declined to auto-close.

    Not an alarm, and deliberately a quiet ·-line: these boards are working.
    But the operator does lose something real — for as long as a board
    reports a partial list, closed-detection never runs on it, so a posting
    the firm took down stays open until `reverify` reaches that URL on its
    own and closes it from evidence. That is slower than the nightly sweep,
    and it is worth knowing which firms are on the slow path.

    Replaces, rather than merely removes, what these boards used to
    contribute: before `_is_guard_notice` they were being announced every
    run as failing and serving stale data, which was false on both counts.
    """
    latest = (ScrapeRun.objects.filter(connector="all")
              .order_by("-started").first())
    if latest is None:
        return []
    return sorted({
        e.get("firm", "") for e in (latest.stats or {}).get("errors", [])
        if _is_guard_notice(e)
    } - {""})


def boards_that_never_yield() -> dict[str, list[str]]:
    """Catalog entries whose firm has never produced a single row, split by
    what the scrape logs say about WHY.

    The old version lumped them together and called every one "a config bug,
    not a market fact". Checked by hand 2026-08-05, that was wrong for both
    of its live hits: HPS's Greenhouse token resolves and returns
    `{"jobs": [], "total": 0}` (their hiring moved under BlackRock after the
    acquisition), and jefferies.tal.net serves a full board page containing
    zero vacancy links. Both boards are live and genuinely empty — exactly
    the "market fact" the message insisted they weren't.

    The scrape runs already record which firms ERRORED, so the two cases are
    distinguishable from data on hand:

    - "broken": never yielded AND erroring in the most recent run — the URL
      is wrong or the fetch is failing. A real configuration bug.
    - "walled": never yielded and the latest error is a bot-protection
      interstitial. Neither of the other two messages is true for these:
      the config is fine (nothing to fix) and the board is NOT empty (it is
      unreadable — jefferies.tal.net sat in "empty" for weeks reading as
      "Jefferies has no openings" while Oleeo Protect was eating the page).
    - "empty": never yielded and fetching cleanly — the board just has
      nothing on it. Worth a quiet eye (an empty board and a silently
      wrong-but-resolving URL look identical from here), not an alarm.
    """
    producing_firm_ids = set(
        Opportunity.objects.values_list("firm_id", flat=True).distinct()
    )
    producing_slugs = set(
        Firm.objects.filter(id__in=producing_firm_ids).values_list("slug", flat=True)
    )
    configured = {slug for slug, _ in BOARDS}
    silent = configured - producing_slugs

    latest = (ScrapeRun.objects.filter(connector="all")
              .order_by("-started").first())
    latest_errors = (latest.stats or {}).get("errors", []) if latest else []
    erroring_firms = {
        e.get("firm", "").lower() for e in latest_errors if _is_real_failure(e)
    }
    walled_firms = {
        e.get("firm", "").lower() for e in latest_errors if _is_walled(e)
    }
    firm_names = dict(
        Firm.objects.filter(slug__in=silent).values_list("slug", "name")
    )
    walled = {s for s in silent
              if firm_names.get(s, s).lower() in walled_firms}
    broken = {s for s in silent - walled
              if firm_names.get(s, s).lower() in erroring_firms}
    return {"broken": sorted(broken), "walled": sorted(walled),
            "empty": sorted(silent - broken - walled)}


# How long the detail-page pass may be silent before that silence is itself
# the finding. It runs inside `refresh`, so on a healthy cadence there is a
# run every night.
ENRICH_SILENT_DAYS = 3


def enrichment_health() -> list[str]:
    """Warnings about the pass that reads postings' own pages.

    It earns its own checks because its failure is invisible in every other
    signal on the board: the scrape still succeeds, the counts still look
    right, and the roles still list. What quietly stops is the deadlines. The
    first version of this pipeline lost a full enrichment run to an overnight
    scrape and nothing anywhere went red.
    """
    from django.utils import timezone

    from .models import ScrapeRun

    lines: list[str] = []
    latest = ScrapeRun.objects.filter(connector="enrich").order_by("-started").first()
    if latest is None:
        return ["· the detail-page pass has never run — deadlines come only "
                "from list endpoints, which mostly do not carry one"]

    age = (timezone.now() - latest.started).days
    if age >= ENRICH_SILENT_DAYS:
        lines.append(
            f"⚠ the detail-page pass has not run in {age} days — deadlines and "
            "sponsorship answers are going stale silently")

    stats = latest.stats or {}
    queued, fetched = stats.get("queued", 0), stats.get("fetched", 0)
    unreachable = stats.get("unreachable", 0)
    # A run that reached nothing it queued is a broken run wearing a green
    # status: it exits 0, writes no rows, and reports "0 pages read".
    if queued and not fetched:
        lines.append(
            f"⚠ the last detail-page pass queued {queued} pages and read none "
            "(host blocking, or every URL shape unrecognised)")
    elif fetched and unreachable > fetched:
        lines.append(
            f"⚠ the last detail-page pass failed on more pages than it read "
            f"({unreachable} unreachable vs {fetched} read)")
    return lines


def health_report() -> list[str]:
    """Human-readable warning lines; empty when everything is healthy."""
    lines: list[str] = []
    # First, and always a ⚠ rather than a standing ·-line: unlike a bot wall,
    # this one is fixable from here by a single command, and the rows behind
    # it never self-heal. It should keep nagging until somebody runs it.
    for dupe in duplicate_firms():
        stranded = (f"{dupe['stranded_rows']} postings stranded"
                    if dupe["stranded_rows"] else "no postings stranded")
        lines.append(
            f"⚠ duplicate firm rows for {dupe['name']!r} "
            f"({', '.join(dupe['duplicates'])} alongside {dupe['canonical']}; "
            f"{stranded} — no board resolves to them, so they never refresh "
            f"or close, and they render as duplicate cards): fix with "
            f"`manage.py merge_duplicate_firms --apply`"
        )
    lines += enrichment_health()
    failing = repeat_failures()
    if failing:
        lines.append(
            f"⚠ failing in each of the last {CONSECUTIVE_FAILURES} scrape runs "
            f"(stale data being presented as fresh): {', '.join(failing)}"
        )
    walled = walled_boards()
    if walled:
        # ⚠ exactly once — the run that first hits the wall — then the
        # standing ·-line. The daily pipeline notifies on ⚠ lines, so a new
        # wall reaches the owner without every later run re-ringing the bell.
        parts = []
        for w in walled:
            frozen = (f"{w['open_rows']} open rows frozen" if w["open_rows"]
                      else "no rows held")
            parts.append(f"{w['firm']} ({frozen}, since {w['since']:%Y-%m-%d})")
        marker = "⚠" if any(w["is_new"] for w in walled) else "·"
        lines.append(
            f"{marker} bot-walled (operator requires a human check; not a "
            f"config bug and not an empty board — check by hand): "
            f"{', '.join(parts)}"
        )
    guarded = guarded_boards()
    if guarded:
        lines.append(
            "· auto-close skipped on a partial list (board is healthy; these "
            "firms' dead postings close via reverify instead of the nightly "
            f"sweep, so they clear more slowly): {', '.join(guarded)}"
        )
    silent = boards_that_never_yield()
    if silent["broken"]:
        lines.append(
            "⚠ never produced a row AND erroring in the latest run (bad board "
            f"URL or failing fetch — fix the config): {', '.join(silent['broken'])}"
        )
    if silent["empty"]:
        lines.append(
            "· never produced a row but fetches cleanly (board is live and "
            "empty — plausible market fact, worth a manual look now and then): "
            f"{', '.join(silent['empty'])}"
        )
    return lines
