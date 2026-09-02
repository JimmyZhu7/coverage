"""build_cycle_observations — rebuild `FirmCycleObservation` from
`OpportunityChange` + `ScrapeRun`.

    python manage.py build_cycle_observations
    python manage.py build_cycle_observations --dry-run

Fully recomputable and idempotent: every row is derived fresh from
`Opportunity`/`OpportunityChange`/`ScrapeRun` on every run, so running this
twice in a row (or after a schema change to how a window is computed)
produces byte-identical output, and nothing here is ever meant to be
hand-edited afterward. See `FirmCycleObservation`'s docstring for what the
table is and why it exists separately from `FirmDate`.

TWO KINDS OF EVIDENCE, TWO DIFFERENT GUARDS
--------------------------------------------
Opens need no trust filter: `Opportunity.first_seen` is stamped once, by the
row's own creation, and a broken or dark board can only make a posting go
MISSING from a fetch, never spuriously CREATE one. The one thing opens do
need guarding against is a firm's own onboarding: the very first day
Coverage ever scraped a firm's board, EVERY posting on it gets the same
`first_seen`, and that date says "this is when we started watching," not
"this is when these roles opened." `_onboarding_cutoff` finds that day per
firm (from ALL of the firm's postings, not just the campus-bucket ones,
because onboarding is a fetch-level event) and excludes it from
`opened_count`/`open_window_*`. This is a firm-level approximation — a board
added to an ALREADY-onboarded firm later in the window would still read as a
real open, which is correct (it is one), but a second provider board bolted
onto an existing firm on its own first day would read the same way too. Not
solved here: distinguishing "a new board" from "a new posting" needs board
history this table doesn't have, and guessing would violate the same
never-invent-a-fact rule the rest of this feature exists to uphold.

Closes DO need a trust filter, because a close can be produced by evidence
that never happened (a board fetch failure, a board gone dark) — see
`directory.cycle_trust`. Only TRUSTED closes count toward `closed_count`/
`close_window_*`; SUSPECT ones are counted only in `excluded_suspect_closes`,
never averaged in.

CAMPUS SCOPE
------------
Every count below is over `classify.TARGET_BUCKETS` (insight/internship/
entry_level) only. A firm's non-campus volume can dwarf its campus postings
— TD Securities alone runs a continuous stream of retail-branch reqs
("Personal Banking Associate Trainee") that open and close constantly and
have nothing to do with a recruiting cycle. Confirmed live: those retail
reqs are independent parallel requisitions with their own requisition
numbers, not one posting relisted under a new id (see `directory.dupes`'s
Class B — "several postings, one job" is exactly the shape TD's, State
Street's and Deutsche Bank's high-volume programmes take, and that module's
own docstring already says merging them "would destroy real data"). Nothing
here attempts to fold them; they are simply out of scope by bucket.

REPOSTING RISK — investigated, not defended against
-----------------------------------------------------
A firm relisting a pulled posting under a new URL would otherwise read as a
false close paired with a false open. `directory.dupes.provider_identity`
already catches this AT INGEST for the three providers where a posting keeps
a stable id across a relist (tal.net, iCIMS, Workday-with-a-requisition-id):
`ingest._match_by_identity` matches the new URL back to the existing row
before `OpportunityChange` is ever written, so a real repost on those
providers surfaces as a REOPEN of the same row, not a close-then-create
pair — there is nothing left for this command to catch after the fact.
Checked live for the remaining providers (Greenhouse/Lever, which mint a
genuinely new id on every requisition with no shared identifier to match):
close-then-similar-open pairs within 14 days do exist in the campus-bucket
data, but every example checked (TD Securities "Personal Banking Associate
Trainee", State Street's Bangalore "Apprentice" programme, Deutsche Bank's
apprentice intake) is the Class B high-volume-parallel-requisition shape
above — independent postings with independent requisition numbers, not the
same posting twice. Folding them by title/location similarity, the way
`dupes.duplicate_key` does for on-screen dedup, would merge exactly the
postings that module's own docstring says have "independent lifecycles" and
"can close on different days." No additional repost defense is built here.
"""

from __future__ import annotations

import collections
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from directory.classify import TARGET_BUCKETS
from directory.cycle_trust import SUSPECT, TRUSTED, classify_closes
from directory.models import Firm, FirmCycleObservation, Opportunity, OpportunityChange
# `_onboarding_cutoff` used to be defined right here as a private helper.
# `directory.open_runs` now shows a live posting's elapsed openness on the
# Opportunities feed and the Today rail, and that fact needs the exact same
# "this date means we started watching, not that this opened" cutoff — so the
# definition moved there and this command imports it, rather than three
# surfaces each carrying their own copy of a rule they have to agree on.
from directory.open_runs import onboarding_cutoffs as _onboarding_cutoff

# WHICH CLOCK A DAY IS COUNTED ON. This command used to bucket both windows on
# `.date()` of a stored UTC instant while `open_runs.open_run_days` — reading
# the SAME `Opportunity.first_seen` for the Today rail and the Opportunities
# feed — bucketed on `crm.utils.local_date`. A posting first seen at 01:00 Hong
# Kong time therefore landed on the previous day in the observation window on a
# firm page and on the right day in the rail two clicks away, from one column,
# on one screen.
#
# They agree now because they call one function. The clock it reads is the
# ACTIVE Django timezone, which for a management command with nothing
# activated is `settings.TIME_ZONE` — the deployment's own clock, the same one
# `open_runs.onboarding_cutoffs`' `TruncDate` already truncates on, so the
# cutoff and the dates it is compared against are finally measured the same
# way. The command prints the zone it used, because a date with no clock named
# is a date two readers can disagree about in good faith.
#
# THE ALTERNATIVE, AND WHY NOT YET. The better answer for a table keyed on
# (firm, region) is the MARKET's clock: a Hong Kong posting's "opened Aug 3"
# should mean 3 August in Hong Kong. That is a bigger change than it looks —
# `onboarding_cutoffs` is shared with two live per-user surfaces and would have
# to move too, and the region-to-zone map would be a new vocabulary. One clock,
# named, is the honest step; two clocks silently disagreeing was the defect.
from crm.utils import local_date


class Command(BaseCommand):
    help = "Rebuild FirmCycleObservation from OpportunityChange + ScrapeRun (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Compute and report, write nothing.")

    def handle(self, *args, **opts):
        # Name the clock. Every window below is a calendar DAY, and a day is
        # only a fact once you say whose. See the note above the `local_date`
        # import.
        self.stdout.write(
            f"bucketing days on {timezone.get_current_timezone_name()}")
        campus = Opportunity.objects.filter(bucket__in=TARGET_BUCKETS)
        cutoff_by_firm = _onboarding_cutoff()

        # ---- opens: every campus posting first seen after its firm's
        # onboarding day, grouped by (firm, region).
        opens_by_group: dict[tuple[int, str], list] = collections.defaultdict(list)
        for opp in campus.only("id", "firm_id", "region", "first_seen"):
            cutoff = cutoff_by_firm.get(opp.firm_id)
            seen_date = local_date(opp.first_seen).date()
            if cutoff is not None and seen_date <= cutoff:
                continue  # onboarding backlog, not an observed open
            opens_by_group[(opp.firm_id, opp.region)].append(seen_date)

        # ---- closes: TRUSTED only, campus-scoped, grouped the same way.
        campus_closes = OpportunityChange.objects.filter(
            field="status", new_value="closed",
            stage=OpportunityChange.STAGE_SCRAPE_CLOSE,
            opportunity__bucket__in=TARGET_BUCKETS,
        )
        verdicts = classify_closes(campus_closes)
        opp_meta = {
            o.id: (o.firm_id, o.region)
            for o in Opportunity.objects.filter(
                id__in=[v.opportunity_id for v in verdicts]
            ).only("id", "firm_id", "region")
        }
        # observed_at per change id, for the close window dates.
        observed_at_by_change = dict(
            campus_closes.values_list("id", "observed_at")
        )

        closes_by_group: dict[tuple[int, str], list] = collections.defaultdict(list)
        suspect_count_by_group: collections.Counter = collections.Counter()
        for v in verdicts:
            key = opp_meta.get(v.opportunity_id)
            if key is None:
                continue
            if v.verdict == TRUSTED:
                closes_by_group[key].append(
                    local_date(observed_at_by_change[v.change_id]).date())
            elif v.verdict == SUSPECT:
                suspect_count_by_group[key] += 1

        # ---- live snapshot, for currently_open_count.
        open_now = collections.Counter(
            campus.filter(status="open").values_list("firm_id", "region")
        )

        groups = set(opens_by_group) | set(closes_by_group) | set(suspect_count_by_group) | set(open_now)
        firms = {f.id: f for f in Firm.objects.filter(id__in={g[0] for g in groups})}

        rows = []
        for firm_id, region in groups:
            opens = opens_by_group.get((firm_id, region), [])
            closes = closes_by_group.get((firm_id, region), [])
            rows.append(FirmCycleObservation(
                firm_id=firm_id,
                region=region,
                opened_count=len(opens),
                open_window_first=min(opens) if opens else None,
                open_window_last=max(opens) if opens else None,
                closed_count=len(closes),
                close_window_first=min(closes) if closes else None,
                close_window_last=max(closes) if closes else None,
                excluded_suspect_closes=suspect_count_by_group.get((firm_id, region), 0),
                currently_open_count=open_now.get((firm_id, region), 0),
                onboarded_at=cutoff_by_firm.get(firm_id),
            ))
        rows.sort(key=lambda r: (firms[r.firm_id].name, r.region))

        if opts["dry_run"]:
            for r in rows:
                self.stdout.write(
                    f"{firms[r.firm_id].name:35s} {r.region or '(unstated)':10s} "
                    f"opened={r.opened_count:3d} [{r.open_window_first}..{r.open_window_last}] "
                    f"closed={r.closed_count:3d} [{r.close_window_first}..{r.close_window_last}] "
                    f"suspect={r.excluded_suspect_closes:3d} open_now={r.currently_open_count:4d}"
                )
            self.stdout.write(self.style.WARNING(f"[dry-run] {len(rows)} row(s), nothing written."))
            return

        with transaction.atomic():
            # Delete-then-recreate rather than update_or_create: this table
            # is defined as fully recomputable (never hand-edited), so there
            # is no state worth diffing against, and a group that no longer
            # qualifies (e.g. its only postings were reclassified out of
            # TARGET_BUCKETS) must not leave a stale row behind.
            FirmCycleObservation.objects.all().delete()
            FirmCycleObservation.objects.bulk_create(rows, batch_size=500)

        self.stdout.write(self.style.SUCCESS(
            f"rebuilt {len(rows)} firm cycle observation(s)."
        ))
