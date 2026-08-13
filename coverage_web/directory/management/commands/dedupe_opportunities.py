"""dedupe_opportunities — report the duplicate rows already in the table.

    python manage.py dedupe_opportunities              # report only (default)
    python manage.py dedupe_opportunities --apply      # refuses; see below

REPORT ONLY, ON PURPOSE. `--dry-run` is the default and `--apply` is not
implemented: this command exists so the size and shape of the existing mess can
be read off the live data before anyone decides what to do about it, and
merging rows is not a decision code should make on its own.

The two fixes already shipped alongside this command handle the problem going
forward and for the reader, without touching a single stored row:

  * `directory.dupes.provider_identity`, used by ingest, stops NEW duplicate
    rows being created when a provider re-addresses a posting it already gave
    us (tal.net's second candidate pool, an edited iCIMS slug, a Workday
    posting whose multi-city url picked a different city this run).
  * `directory.dupes.fold_duplicates`, used by the Opportunities feed, hides
    the copies that are already stored, with a count and an undo link.

So nothing here is urgent. What this reports is historical residue, and the
open question it answers is whether that residue is worth a one-off merge at
all — a merge would have to decide which requisition's `closed_at`, `first_seen`
and tracking rows survive, and for the Class B cases (two genuinely separate
requisitions with the same title) the honest answer is that neither should.

Output is grouped by mechanism so the two are never confused:

  IDENTITY   Same posting, two addresses. A merge here is defensible: one of
             the two urls is simply an older way of naming the same thing.
  LOOKALIKE  Same firm, title and location, no shared provider id. These may
             be one job filed twice or several real openings, and only the
             employer knows which. A merge here would be a guess.
"""

from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from directory.dupes import duplicate_key, fold_duplicates, provider_identity
from directory.models import Opportunity


class Command(BaseCommand):
    help = "Report duplicate opportunity rows. Reports only; never writes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", default=True,
            help="Report only. The default, and currently the only mode.",
        )
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Not implemented — merging rows is a decision, not a chore.",
        )
        parser.add_argument(
            "--open-only", action="store_true", default=False,
            help="Only consider rows a student can currently see (status=open).",
        )
        parser.add_argument(
            "--limit", type=int, default=40,
            help="How many groups to print per section (default 40).",
        )

    def handle(self, *args, **options):
        if options["apply"]:
            raise CommandError(
                "--apply is deliberately not implemented. This command reports; "
                "it does not merge or delete rows. Read the report, decide what "
                "should happen to each group, and write that migration knowingly."
            )

        qs = Opportunity.objects.select_related("firm")
        if options["open_only"]:
            qs = qs.filter(status="open")
        rows = list(qs)
        self.stdout.write(f"Scanning {len(rows)} rows "
                          f"({'open only' if options['open_only'] else 'all statuses'})\n")

        # ---- IDENTITY: the provider itself says these are the same posting.
        by_identity: dict[tuple, list] = defaultdict(list)
        for row in rows:
            identity = provider_identity(row.url)
            if identity is not None:
                by_identity[(row.firm_id, identity)].append(row)
        identity_groups = [v for v in by_identity.values() if len(v) > 1]

        # ---- LOOKALIKE: same words, no shared id. Everything the identity
        # pass already claimed is excluded, so the two sections never
        # double-count the same row.
        claimed = {id(r) for g in identity_groups for r in g}
        by_words: dict[tuple, list] = defaultdict(list)
        for row in rows:
            if id(row) not in claimed:
                by_words[duplicate_key(row)].append(row)
        lookalike_groups = [v for v in by_words.values() if len(v) > 1]

        self._section("IDENTITY — same posting, two addresses (a merge is defensible)",
                      identity_groups, options["limit"])
        self._section("LOOKALIKE — same words, no shared provider id (a merge would be a guess)",
                      lookalike_groups, options["limit"])

        # What the READER is spared today, which is the number that actually
        # matters until someone decides about a merge.
        open_rows = [r for r in rows if r.status == "open"]
        _, folded = fold_duplicates(open_rows)

        extra_identity = sum(len(g) - 1 for g in identity_groups)
        extra_lookalike = sum(len(g) - 1 for g in lookalike_groups)
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("SUMMARY"))
        self.stdout.write(f"  identity groups   {len(identity_groups):>5}  "
                          f"({extra_identity} redundant rows)")
        self.stdout.write(f"  lookalike groups  {len(lookalike_groups):>5}  "
                          f"({extra_lookalike} redundant rows)")
        self.stdout.write(f"  folded from the open feed by fold_duplicates: {folded}")
        self.stdout.write("")
        self.stdout.write("No rows were changed. This command never writes.")

    def _section(self, heading: str, groups: list[list], limit: int) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(heading))
        if not groups:
            self.stdout.write("  (none)")
            return
        groups = sorted(groups, key=lambda g: (g[0].firm.name, g[0].title))
        for group in groups[:limit]:
            self.stdout.write(f"\n  {group[0].firm.name} — {group[0].title[:70]}")
            for row in sorted(group, key=lambda r: r.id):
                self.stdout.write(
                    f"    id={row.id:<7} {row.status:<7} "
                    f"deadline={str(row.deadline or '-'):<12} "
                    f"first_seen={row.first_seen:%Y-%m-%d}"
                )
                self.stdout.write(f"      {row.url}")
        if len(groups) > limit:
            self.stdout.write(f"\n  … and {len(groups) - limit} more groups "
                              f"(raise --limit to see them)")
