"""backfill_detail_locations — copy an already-fetched detail_location into
the displayed `location` field, for rows still showing Workday's aggregate
"N Locations" placeholder.

    python manage.py backfill_detail_locations           # report only (default)
    python manage.py backfill_detail_locations --apply    # write the recovered locations

WHY THIS EXISTS
---------------
`enrich_postings` fetches and stores each re-read posting's real per-posting
location as `raw["detail_location"]`, but until this same change, nothing
ever copied that value back into the DISPLAYED `location` column — it was
wired only into `reclassify`'s region classification. Proof this already
happened silently in the live data: TD Securities opportunity 19411 already
carries `raw["detail_location"] = "Markham, Ontario, Canada; Scarborough,
Ontario, Canada"` while `location` still reads the opaque "2 Locations"
Workday's list API handed over.

`enrich_postings`'s own fix closes this going forward for every row it
re-reads from here on. This command is the one-time catch-up for rows that
were ALREADY re-read under the old code and are sitting on a correct,
unused `detail_location` that the staleness queue may not revisit again for
weeks (or ever, once a row has answered "neither" on deadline/sponsorship
and so stops looking urgent) — reading `raw` off rows already in the
database, no network, nothing guessed.

Scope is deliberately narrow: only OPEN rows whose `location` matches
Workday's own `^\\d+ Locations?$` shape today, and only where
`detail_location` is already stored and non-blank. A row whose location
reads something else entirely is left alone — this command corrects the one
known-bad shape, not location text in general.
"""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from directory.models import Opportunity

_N_LOCATIONS_PLACEHOLDER = re.compile(r"^\d+\s+Locations?$", re.IGNORECASE)


class Command(BaseCommand):
    help = ("Copy raw['detail_location'] into `location` for open rows still "
            "showing Workday's 'N Locations' placeholder. Reports by default.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Write the recovered locations. Default is report-only.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after N rows (0 = all).",
        )

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        tag = "" if apply_ else "[dry-run] "

        candidates = []
        for o in (Opportunity.objects.filter(status="open")
                  .filter(raw__has_key="detail_location")
                  .select_related("firm")
                  .order_by("firm__name", "id")):
            detail_location = (o.raw or {}).get("detail_location") or ""
            if detail_location and _N_LOCATIONS_PLACEHOLDER.match(o.location or ""):
                candidates.append((o, detail_location[:255]))

        if opts["limit"]:
            candidates = candidates[: opts["limit"]]

        if not candidates:
            self.stdout.write("Nothing to backfill.")
            return

        for o, new_location in candidates:
            self.stdout.write(
                f"{tag}{o.firm.name} #{o.id} — {o.title[:52]}: "
                f"{o.location!r} -> {new_location!r}")
            if apply_:
                o.location = new_location
                o.save(update_fields=["location"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{len(candidates)} row(s) "
            f"{'updated' if apply_ else 'would be updated'}."))
