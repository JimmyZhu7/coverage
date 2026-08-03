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


def boards_that_never_yield() -> list[str]:
    """Catalog entries whose firm has never produced a single row, from any
    provider, ever. Not "no open roles" — literally nothing has ever landed,
    which means the board URL is wrong, the board is empty, or the firm name
    doesn't resolve. Each is a configuration problem, not a market fact."""
    producing_firm_ids = set(
        Opportunity.objects.values_list("firm_id", flat=True).distinct()
    )
    producing_slugs = set(
        Firm.objects.filter(id__in=producing_firm_ids).values_list("slug", flat=True)
    )
    configured = {slug for slug, _ in BOARDS}
    return sorted(configured - producing_slugs)


def health_report() -> list[str]:
    """Human-readable warning lines; empty when everything is healthy."""
    lines: list[str] = []
    failing = repeat_failures()
    if failing:
        lines.append(
            f"⚠ failing in each of the last {CONSECUTIVE_FAILURES} scrape runs "
            f"(stale data being presented as fresh): {', '.join(failing)}"
        )
    silent = boards_that_never_yield()
    if silent:
        lines.append(
            "⚠ configured but has NEVER produced a row (bad board URL or empty "
            f"board — a config bug, not a market fact): {', '.join(silent)}"
        )
    return lines
