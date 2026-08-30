"""Was a `scrape.close` event actually witnessed, or did a board just go dark?

`OpportunityChange` (see its docstring) records *that* a posting closed, and
`ScrapeRun.stats` records *whether the fetch that produced that record could
be trusted* — but nothing joins the two. A pass that fetches 0/2 boards for a
firm (a real, recorded sample: `stats = {"boards_ok": 0, "boards_total": 2,
"boards_failed": 2, "errors": [{"firm": "William Blair", ...}]}`) never marks
anything closed FOR THAT PASS — `ingest.ingest_results`'s own `pair_all_ok`
guard already refuses to close on a failed board, and its "suspicious wipe"
guard already refuses to close a firm whose board suddenly returns zero rows.
Those guards are real and they work (see `test_cycle_trust.py` and the
report `build_cycle_observations` prints), but they answer "should THIS
pass close anything" at write time, with no memory kept of which pass wrote
which close. A firm cycle observation built by reading `scrape.close` rows
back out has no way to ask "was the run that wrote this row healthy" unless
something reconstructs that join after the fact — which is what this module
is for. It is deliberately read-only and lives outside `ingest.py`: Phase 1
of this feature is a report, not a pipeline change, and the task that added
it was explicitly told not to touch the scrape/ingest pipeline.

Two independent signals, both computed from data already on the table:

1. **Board health.** Match a close event's `observed_at` back to the
   `ScrapeRun` whose `[started, finished]` window contains it (see
   `_match_run` for why this is unambiguous in practice), then check whether
   that run's own `stats["errors"]` names this (firm, provider) pair as a
   fetch failure. `ScrapeRun.stats["errors"]` is a single free-text list that
   ingest.py appends to for FIVE different reasons and only one of them is
   "the board fetch failed" (see `_NON_FAILURE_MARKERS`) — there is no
   structured tag distinguishing them, so this module reconstructs the
   distinction the same way `scrape.py`'s own command output does (matching
   a fixed set of note substrings), rather than inventing a second, possibly
   inconsistent, classification of the same strings.

2. **Mass-close shape.** Even a run every one of whose boards reports
   `result.ok=True` can still be lying — a provider can return HTTP 200 with
   a body that no longer resembles a job list (a redesigned page, a CAPTCHA
   wall that isn't recognised as one) and the connector has no way to know.
   The signature this leaves is not a reported failure, it's a whole firm's
   board emptying out in one pass. `ingest.py`'s own wipe guard already
   catches the extreme case (zero rows returned at all — see
   `ingest_results`'s "suspected shape change" branch) but a connector that
   returns SOME rows, just not most of them, sails straight past that guard.
   `classify_closes` re-checks the shape independently of what the run
   itself reported: if a run closed nearly everything a (firm, source) pair
   had open, that is flagged regardless of whether the run called itself
   healthy.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import QuerySet

from .models import Opportunity, OpportunityChange, ScrapeRun

TRUSTED = "trusted"
SUSPECT = "suspect"

# Every non-board-failure `errors[]` template ingest.py writes today (see the
# module docstring). Anything in `stats["errors"]` that matches none of these
# is, by elimination, the one remaining template: `result.error` from a board
# whose fetch itself failed (`ingest.py`'s `if not result.ok:` branch) — the
# only case that actually sets `pair_all_ok[pair] = False`. Enumerated by
# exclusion rather than by a positive "board failed" tag because the schema
# has no such tag; if ingest.py ever grows a sixth error template this list
# has to grow with it or that template gets misread as a board failure.
_NON_FAILURE_MARKERS = (
    "skipped auto-close",     # partial-list guard AND suspected-shape-change guard
    "unparseable deadline",   # a single row's date didn't parse; the board was fine
    "row failed (",           # a single row's upsert raised; the board was fine
    "row skipped",            # oversized url, one row; the board was fine
)

# A run that closed nearly everything a (firm, source) pair had open is the
# signature `ingest.py`'s wipe guard exists for, and it can still happen when
# the guard doesn't fire (see module docstring, signal 2). 0.9 rather than
# 1.0: a board that closes 9 of 10 open roles in one pass and leaves a single
# stray row live (a connector's own pagination edge, say) is exhibiting the
# same "the board went dark" shape as one that closes all 10, and a threshold
# that only catches literal 100% would miss it for no principled reason.
MASS_CLOSE_FRACTION = 0.9
# Below this many pre-run-open postings, "nearly all of them closed" is not a
# distinguishable shape from ordinary single-digit churn — a 2-person firm
# whose only 2 roles both fill in one week is not a dark board, it's a small
# firm. Chosen well under the smallest genuine multi-role firm on the live
# board (see the Phase 1 report: no firm+source pair in 5 weeks of live data
# ever closes above ~40% of its open set, let alone tests this floor).
MASS_CLOSE_MIN_OPEN = 3


@dataclass(frozen=True)
class CloseVerdict:
    change_id: int
    opportunity_id: int
    verdict: str  # TRUSTED | SUSPECT
    reason: str


def _is_fetch_run(run: ScrapeRun) -> bool:
    """Only `ingest.ingest_boards` runs carry `boards_total` — `reverify`,
    `enrich_postings` and `extract_facts` each create their own `ScrapeRun`
    row (see each command's `handle`) with a stats shape of their own, and
    none of them ever write a `scrape.close` change (only `ingest_results`
    does). Filtering on this key rather than on `connector` because the
    fetch label itself varies freely (`"all"`, `"firm:<slug>"`, a bare
    provider name) and there is no closed vocabulary to check it against."""
    return "boards_total" in (run.stats or {})


def _board_failed(run: ScrapeRun, firm_name: str, provider: str) -> bool:
    for e in (run.stats or {}).get("errors", []):
        if e.get("firm") != firm_name or e.get("provider") != provider:
            continue
        text = e.get("error") or ""
        if not any(marker in text for marker in _NON_FAILURE_MARKERS):
            return True
    return False


def _match_run(observed_at, runs: list[ScrapeRun]) -> ScrapeRun | None:
    """The fetch run whose window contains `observed_at`, preferring the
    NARROWEST matching window.

    `observed_at` is stamped once, inside `ingest_results`, strictly between
    that run's own `started` and `finished` — so in the overwhelmingly common
    case exactly one run's window contains it. Two windows CAN both contain
    it only when an ad hoc single-firm/provider run (`--firm`, `--provider`)
    happened to execute while the scheduled `all` run was also in flight; the
    ad hoc run's window is necessarily the tighter one (it fetches far fewer
    boards), so it is the more specific, and more likely correct, match.
    """
    candidates = [r for r in runs if r.started and r.finished
                  and r.started <= observed_at <= r.finished]
    if not candidates:
        return None
    return min(candidates, key=lambda r: r.finished - r.started)


def classify_closes(
    changes: QuerySet | None = None,
) -> list[CloseVerdict]:
    """TRUSTED/SUSPECT for every `scrape.close` row in `changes` (default:
    all of them). Read-only — callers decide what to do with the verdicts.
    """
    if changes is None:
        changes = OpportunityChange.objects.filter(
            field="status", new_value="closed",
            stage=OpportunityChange.STAGE_SCRAPE_CLOSE,
        )
    changes = list(changes.select_related("opportunity", "opportunity__firm"))
    if not changes:
        return []

    fetch_runs = [r for r in ScrapeRun.objects.all() if _is_fetch_run(r)]

    # Pass 1: board-health verdict, and group by the run actually matched
    # (not by raw timestamp — two runs could in principle share a start
    # second, though `_match_run` already resolves that) for the mass-close
    # pass below.
    provisional: dict[int, CloseVerdict] = {}
    by_run_firm_source: dict[tuple, list[OpportunityChange]] = {}
    for c in changes:
        opp = c.opportunity
        run = _match_run(c.observed_at, fetch_runs)
        if run is None:
            provisional[c.id] = CloseVerdict(
                c.id, opp.id, SUSPECT,
                "no scrape run found covering this close's observed_at",
            )
            continue
        if _board_failed(run, opp.firm.name, opp.source):
            provisional[c.id] = CloseVerdict(
                c.id, opp.id, SUSPECT,
                f"run #{run.id}: {opp.firm.name} ({opp.source}) board fetch "
                f"failed this pass",
            )
            continue
        provisional[c.id] = CloseVerdict(c.id, opp.id, TRUSTED, f"run #{run.id}: board fetch ok")
        by_run_firm_source.setdefault((run.id, opp.firm_id, opp.source), []).append(c)

    # Pass 2: mass-close shape, independent of what the run self-reported.
    # Only re-examines groups that passed pass 1 — a group already SUSPECT
    # for a failed board doesn't need a second reason.
    for (run_id, firm_id, source), group in by_run_firm_source.items():
        observed_at = group[0].observed_at
        n_closed = len(group)
        if n_closed < MASS_CLOSE_MIN_OPEN:
            continue
        open_before = Opportunity.objects.filter(
            firm_id=firm_id, source=source, first_seen__lte=observed_at,
        ).exclude(status="closed", closed_at__lt=observed_at).count()
        if open_before < MASS_CLOSE_MIN_OPEN:
            continue
        if n_closed / open_before >= MASS_CLOSE_FRACTION:
            for c in group:
                provisional[c.id] = CloseVerdict(
                    c.id, c.opportunity_id, SUSPECT,
                    f"run #{run_id}: mass-close — {n_closed} of {open_before} "
                    f"open postings for this firm/board closed in one pass",
                )

    return list(provisional.values())
