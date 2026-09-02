"""How long a posting has been open — the ONE duration fact this data can
carry, and the argument for why the obvious other one cannot be shipped.

WHAT WAS ASKED FOR, AND WHY IT IS NOT HERE
-------------------------------------------
The ask was "show how long a programme stays open" — a typical open-to-close
DURATION per firm, the way `FirmCycleObservation` shows typical open and
close WINDOWS. A per-posting duration turns out to be computable in the
mechanical sense: `Opportunity.first_seen` is that row's own open, and the
same row's `OpportunityChange` close (filtered through `cycle_trust`, exactly
as `build_cycle_observations` already filters it) is that row's own close, so
the two pair per posting rather than per aggregate window. Measured on live
data 2026-08-31: 340 postings across 72 (firm, region) groups have BOTH ends
observed, and 32 of those groups clear three pairs.

It still must not be shipped, because those 340 are not a sample of postings
— they are a sample of the FAST postings, and nothing else could have landed
in it. Coverage's earliest `first_seen` anywhere is 2026-07-23. In a 39-day
observation window the only postings that can contribute a duration are the
ones that finished inside it; every programme that runs longer than the
window is structurally invisible, and the longest duration observed anywhere
(33 days) is the window's own length, not a fact about recruiting. The bias
this produces is not a rounding error, it is the whole number:

  overall           340 closed pairs vs 1686 still-open post-onboarding rows
                    (83% censored). Naive median 9 days — while 77% of the
                    still-open rows have ALREADY been open longer than that.
  Goldman Sachs us  8 closed pairs, median 11 days. 79 postings still open,
                    73 of them already past 11 days, the oldest at 31.
  PwC eu            22 closed pairs, median 18.5 days. 236 still open, 221
                    already past 18.5.
  RBC other         3 closed pairs, median 4 days. 68 still open, 54 already
                    past 4.

Printing "Goldman's roles typically stay open 11 days" on a page whose own
feed is showing 73 Goldman roles that have been open longer than 11 days is
not a hedge-able imprecision; it is a number contradicted by the same
screen. Survival analysis (Kaplan-Meier) is the correct treatment of censored
duration and would use the still-open rows as censored observations rather
than discarding them — but it would still be an ESTIMATE presented as a fact
in a two-word rail slot, and it cannot manufacture evidence past 33 days that
no one has watched yet. `FirmCycleObservation`'s docstring already refuses
"a fabricated value" for a firm with no evidence; a modelled median for a
duration nobody has observed the end of is the same refusal, one step later.
Revisit when the observation window is comfortably longer than the cycles
being measured — a full recruiting cycle, not a month.

WHAT IS HERE INSTEAD
--------------------
Elapsed openness: how long a posting that is open RIGHT NOW has been open.
`today - first_seen`. Both ends are observed, neither end is predicted, and
censoring cannot touch it because it is a measurement of the past making no
claim about the future. It is also the fact a student actually acts on: "this
has been open 22 days" is a reason to apply this week, and it says so without
implying anyone knows when it will close.

The one thing it does need is `FirmCycleObservation`'s onboarding rule, for
that rule's own reason. On the first day Coverage ever scraped a firm's
board, every posting on it gets the same `first_seen`, and that date means
"this is when we started watching," not "this is when this opened." Those
rows get NO fact rather than a wrong one: 880 of the 2581 live campus rows
(34%) are onboarding-batch and stay silent, and the remaining 1701 (66%)
carry a duration that is true. The silence is the same silence
`_cycle_observed` keeps for a below-threshold window.

Deliberately NOT a `FirmCycleObservation` column. Every field on that table
is rebuilt by `build_cycle_observations`, which is run by hand; elapsed
openness changes every day on its own, so a stored copy would be wrong by
exactly however long it has been since someone last ran the command. It is
computed live at both call sites instead, off two indexed reads.
"""

from __future__ import annotations

import collections
import hashlib
from datetime import date

from django.core.cache import cache
from django.db.models import Min
from django.db.models.functions import TruncDate

# Cross-app read, same posture as `directory.views`' import of the same name:
# `local_date` is `crm.utils`' single conversion point for "this stored UTC
# timestamp, on the account's own clock" — see its docstring. `open_run_days`
# needs it for the same reason `directory.views._urgency_item`'s `seen_days`
# does: `first_seen` is UTC, `today` below is the account's local date, and
# comparing a raw `.date()` to a local date is comparing two different clocks.
from crm.utils import local_date

from .classify import TARGET_BUCKETS
from .models import Opportunity

# A single close (or open) is not a window: `open_window_first ==
# open_window_last` by construction when `opened_count == 1`, so the "window"
# is really just the one date something happened to be seen on, no spread at
# all. Two is barely better — a min/max over two postings is still easily
# just which two postings this firm happened to run, not a shape. Three is
# the smallest sample where the spread starts to describe something rather
# than restate an anecdote — the same magnitude `cycle_trust
# .MASS_CLOSE_MIN_OPEN` uses for the identical reason (a count "not yet
# distinguishable from ordinary churn"). Below this, the honesty rule this
# feature exists to uphold is served by rendering nothing, not by hedging.
#
# Lives here rather than in `directory.views` (which re-exports it, and where
# `_cycle_observed` still reads it under its original name) so that the firm
# page's window sentences and the rail's open-run line are gated by ONE
# number. `firm_open_runs` applies it for a related but not identical reason,
# recorded there: a census of one posting is exact, but one posting is not a
# programme.
CYCLE_OBSERVATION_MIN_SAMPLE = 3


# A ceiling, not the invalidation mechanism. The run id in the key is what
# actually expires an entry; this only bounds how long a stale answer can
# survive on a deploy whose scraper has stopped, which is a state
# `directory/health.py` reports on separately.
_CUTOFF_CACHE_SECONDS = 60 * 60 * 6


def _cutoff_generation() -> str:
    """A fingerprint that changes whenever a cached cutoff could be wrong.

    TWO PARTS, and the second one is not belt-and-braces:

      * the newest `ScrapeRun` id — a scrape is what normally brings in the
        postings that move a firm's minimum; and
      * the newest `Opportunity` id — because a posting can be inserted
        without a scrape (a fixture, a management command, a test), and a key
        that ignored that would serve a cutoff computed before the row
        existed. It is also what keeps this cache honest inside a test run:
        the process-local cache outlives a rolled-back transaction, so two
        tests asking about the same firm ids on a database with no ScrapeRun
        would otherwise share one entry. That happened, and the failing test
        (`directory/tests/test_open_runs.py::
        test_scoping_the_cutoff_to_some_firms_matches_the_unscoped_answer`)
        was right to fail.

    Two indexed max lookups against a 24,078-row `TruncDate`/`Min` group-by.
    """
    from directory.models import ScrapeRun

    run = ScrapeRun.objects.order_by("-id").values_list("id", flat=True).first()
    opp = Opportunity.objects.order_by("-id").values_list("id", flat=True).first()
    return f"{run or 0}.{opp or 0}"


def _cutoff_cache_key(firm_ids) -> str:
    """Keyed on the scrape run AND on exactly which firms were asked about.

    The firm set has to be in the key: `onboarding_cutoffs([1, 2])` and
    `onboarding_cutoffs()` return different dictionaries, and a key that
    ignored the argument would serve a two-firm answer to a caller asking
    about thirty. Sorted so two callers naming the same firms in different
    orders share one entry, and hashed because a feed render names up to a
    hundred firm ids and memcached-style backends cap a key's length.
    """
    if firm_ids is None:
        scope = "all"
    else:
        scope = hashlib.sha1(
            ",".join(str(i) for i in sorted(firm_ids)).encode()
        ).hexdigest()[:16]
    return f"onboarding-cutoffs:{_cutoff_generation()}:{scope}"


def onboarding_cutoffs(firm_ids=None, *, fresh: bool = False) -> dict[int, date]:
    """`{firm_id: the calendar date of that firm's first-ever scraped
    posting}` — the day whose `first_seen` values mean "we started watching"
    rather than "this opened."

    Across ALL of a firm's postings, not just campus-bucket ones, because
    onboarding is a fetch-level event: `build_cycle_observations`' module
    docstring makes that call and this is the same function it used to
    define privately, moved here so the command and the two live surfaces
    cannot drift into three different definitions of the same cutoff.

    `firm_ids` narrows the aggregate to the firms a caller actually needs.
    Unscoped it groups all 25.8k rows in ~5 ms warm, which is affordable but
    pointless when a feed page names 30 firms and the Today rail names four.

    CACHED PER SCRAPE RUN. `audit-perf-tests.md §1` defect 10 measured this as
    a sequential scan of 24,078 rows and 4,180 buffers, 5 to 21 ms, on EVERY
    feed render and every Today render — and the answer it computes changes
    only when new postings arrive, which is to say only when a scrape runs.
    The cache key carries the latest `ScrapeRun` id, so a new scrape
    invalidates every entry at once without anything having to remember to
    clear it.

    The key also carries the newest `Opportunity` id, so a posting inserted
    outside a scrape invalidates too — see `_cutoff_generation`.

    WHAT THE KEY DOES NOT COVER, stated rather than left to be found: a
    `first_seen` edited in place on an existing row. That moves a firm's
    minimum without moving either id, and the stale answer survives until the
    next insert or scrape. It is the right trade — this value is the day
    Coverage started watching a firm, it moves once per firm ever, and the
    alternative is the sequential scan on every page load — and a caller who
    needs the uncached truth passes `fresh=True`.
    """
    if firm_ids is not None:
        firm_ids = list(firm_ids)
        if not firm_ids:
            return {}

    if not fresh:
        key = _cutoff_cache_key(firm_ids)
        cached = cache.get(key)
        if cached is not None:
            return cached

    qs = Opportunity.objects.all()
    if firm_ids is not None:
        qs = qs.filter(firm_id__in=firm_ids)
    rows = (qs.annotate(seen_date=TruncDate("first_seen"))
              .values("firm_id")
              .annotate(cutoff=Min("seen_date")))
    out = {r["firm_id"]: r["cutoff"] for r in rows}

    if not fresh:
        cache.set(key, out, _CUTOFF_CACHE_SECONDS)
    return out


def open_run_days(opp, today: date, cutoffs: dict[int, date]) -> int | None:
    """Days this posting has been open, or `None` when that is not a fact we
    hold.

    `None` on three counts, each of which is a different missing fact rather
    than a zero:

    * the row is not open, so "has been open N days" is not what it is doing;
    * `first_seen` is missing entirely;
    * `first_seen` falls on or before the firm's onboarding day, where it
      records when Coverage arrived, not when the posting did (see the module
      docstring). "At least N days" would be the true statement for those
      rows, and it is a different claim with a different shape; the honest
      move is to say nothing rather than to quietly print the wrong one.

    A same-day open returns 0, not `None` — a posting first seen this morning
    genuinely has been open zero days, and callers render that as "today"
    rather than "0d".
    """
    if opp.status != "open" or opp.first_seen is None:
        return None
    # `local_date`, not a raw `.date()`. `first_seen` is stored UTC and
    # `today` (the caller's `timezone.localdate()`) is the account's local
    # date; comparing UTC's `.date()` against a local date is comparing two
    # different clocks. An account whose zone sits any number of hours off
    # UTC — an Asia/Hong_Kong account is 8 hours wide, for instance — can
    # have that skew move a row across a calendar-date boundary. Measured on
    # the live board 2026-08-31: of the 2,339 undated open campus rows with
    # a `seen_days`, this line and `directory.views._urgency_item`'s (also
    # fixed to route through the same `crm.utils.local_date`/
    # `_calendar_days_ago`) disagreed on 2,202 of them (94%) before this fix.
    seen = local_date(opp.first_seen).date()
    cutoff = cutoffs.get(opp.firm_id)
    if cutoff is not None and seen <= cutoff:
        return None
    days = (today - seen).days
    # Negative would mean a row first seen in the future. Not reachable from
    # `auto_now_add` under a shared clock, but `seen` and `today` are each
    # resolved at a slightly different instant within the same request
    # (`today` upstream, `seen` here), so a posting whose `first_seen` lands
    # in the handful of milliseconds between the two — or ordinary clock
    # skew between app server and DB — could still print -0 days without
    # this floor. Floored rather than dropped: the posting is open and we
    # did watch it open, which is the whole claim.
    return max(0, days)


def firm_open_runs(firm_ids, today: date) -> dict[int, dict]:
    """Per firm: how many campus postings are open right now whose opening we
    actually watched, and how long the longest of them has been open.

    Only firms clearing `CYCLE_OBSERVATION_MIN_SAMPLE` appear at all. The
    count itself is a census, not a sample — it is exact, and a gate is not
    needed to make it honest. The gate is here for the other half of the
    sentence: one posting open for 22 days is a posting, not a programme, and
    this line runs underneath a firm-level deadline where it will be read as
    a statement about the firm's cycle. Three is the same floor, for the same
    "not yet distinguishable from ordinary churn" reason, that the firm
    page's window sentences use.

    LONGEST, not mean or median. The longest run is one real posting's real
    elapsed time — a fact with a row behind it — whereas an average over the
    live set would be a summary statistic of a set that is still changing
    underneath it, and would drift downward every time the firm posts
    something new. This line answers "how long has this been open", and only
    the maximum answers it without averaging.

    Campus buckets only, matching `build_cycle_observations`: a firm's
    non-campus volume can dwarf its campus postings (TD Securities' retail
    branch reqs), and counting those under a recruiting deadline would make
    the number describe the wrong thing entirely.
    """
    firm_ids = list(firm_ids)
    if not firm_ids:
        return {}
    cutoffs = onboarding_cutoffs(firm_ids)
    runs: dict[int, list[int]] = collections.defaultdict(list)
    for opp in (Opportunity.objects
                .filter(firm_id__in=firm_ids, status="open",
                        bucket__in=TARGET_BUCKETS)
                .only("id", "firm_id", "status", "first_seen")):
        days = open_run_days(opp, today, cutoffs)
        if days is not None:
            runs[opp.firm_id].append(days)
    out = {}
    for firm_id, days_list in runs.items():
        if len(days_list) < CYCLE_OBSERVATION_MIN_SAMPLE:
            continue
        out[firm_id] = {
            "count": len(days_list),
            "longest_days": max(days_list),
        }
    return out
