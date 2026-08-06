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

from .boards import BOARDS
from .models import Firm, Opportunity, ScrapeRun

# A firm failing in this many consecutive scrape runs is a pattern, not a
# blip. The transient retry (coverage_connectors.fetch_with_retry) already
# absorbed one network hiccup per run before the failure was recorded.
CONSECUTIVE_FAILURES = 3


def repeat_failures(limit_runs: int = CONSECUTIVE_FAILURES) -> list[str]:
    """Firm names that appear in the errors of EVERY one of the last N scrape
    runs. Consecutive, not cumulative: a firm that failed once last week and
    once today is having bad luck; one failing every run is down."""
    runs = list(
        ScrapeRun.objects.exclude(connector="reverify")
        .order_by("-started")[:limit_runs]
    )
    if len(runs) < limit_runs:
        return []
    per_run = [
        {e.get("firm", "") for e in (r.stats or {}).get("errors", [])}
        for r in runs
    ]
    always = set.intersection(*per_run) - {""}
    return sorted(always)


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

    latest = (ScrapeRun.objects.exclude(connector="reverify")
              .order_by("-started").first())
    erroring_firms = {
        e.get("firm", "").lower()
        for e in ((latest.stats or {}).get("errors", []) if latest else [])
    }
    firm_names = dict(
        Firm.objects.filter(slug__in=silent).values_list("slug", "name")
    )
    broken = {s for s in silent
              if firm_names.get(s, s).lower() in erroring_firms}
    return {"broken": sorted(broken), "empty": sorted(silent - broken)}


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
    lines += enrichment_health()
    failing = repeat_failures()
    if failing:
        lines.append(
            f"⚠ failing in each of the last {CONSECUTIVE_FAILURES} scrape runs "
            f"(stale data being presented as fresh): {', '.join(failing)}"
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
