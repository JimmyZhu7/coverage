"""normalize_workday_locations — re-run the Workday connector's own location
normalizer over rows that were ingested before it existed.

    python manage.py normalize_workday_locations            # report only (default)
    python manage.py normalize_workday_locations --apply     # write the repaired locations

WHY THIS EXISTS
---------------
`coverage_connectors.workday.normalize_locations_text` punctuates Workday's
`locationsText` run on its own empty-slot boundary ("Hong Kong  Hong Kong" ->
"Hong Kong, Hong Kong") and drops a leading street address when a place name
follows it ("890 Herron Road, Montreal, Quebec" -> "Montreal, Quebec"). That
fix lands at ingest, so it only reaches rows fetched from here on. 13,332
Workday rows were already in the database when it shipped, and a row is only
re-ingested when its board is scraped again and the posting is still live —
so the rows a student can see today keep the unrepaired run until this runs.

Measured on the live database (2026-08-14): 49 rows carry an unpunctuated
multi-space run, 40 carry a leading street address.

No network, nothing guessed: this reads `location` off rows already in the
database and runs the same pure function the connector runs, so a row can
never end up with a value the connector would not have produced. Rows whose
normalized form equals what they already hold are skipped silently — the
report lists only real changes.

Scope is deliberately narrow, same posture as `backfill_detail_locations`:
`source="workday"` only, because the empty-slot rule is a fact about
Workday's own field and means nothing on a board that writes its locations
some other way.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from coverage_connectors.workday import normalize_locations_text
from directory.models import Opportunity


class Command(BaseCommand):
    help = ("Re-run the Workday location normalizer over already-ingested "
            "workday rows. Reports by default.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Write the repaired locations. Default is report-only.",
        )
        parser.add_argument(
            "--open-only", action="store_true", default=False,
            help="Only rows with status='open' (default: every workday row).",
        )
        parser.add_argument(
            "--ids", default="",
            help="Comma-separated Opportunity ids to restrict the run to.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after N rows (0 = all).",
        )

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        tag = "" if apply_ else "[dry-run] "

        qs = Opportunity.objects.filter(source="workday").select_related("firm")
        if opts["open_only"]:
            qs = qs.filter(status="open")
        if opts["ids"].strip():
            ids = [int(i) for i in opts["ids"].split(",") if i.strip()]
            qs = qs.filter(id__in=ids)

        changes = []
        for o in qs.order_by("firm__name", "id"):
            current = o.location or ""
            fixed = normalize_locations_text(current)[:255]
            if fixed and fixed != current:
                changes.append((o, fixed))

        if opts["limit"]:
            changes = changes[: opts["limit"]]

        if not changes:
            self.stdout.write("Nothing to normalize.")
            return

        for o, fixed in changes:
            self.stdout.write(
                f"{tag}{o.firm.name} #{o.id} — {o.title[:52]}: "
                f"{o.location!r} -> {fixed!r}")
            if apply_:
                o.location = fixed
                o.save(update_fields=["location"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{len(changes)} row(s) "
            f"{'updated' if apply_ else 'would be updated'}."))
