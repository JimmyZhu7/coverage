"""dedupe_opportunities — report, and optionally merge, duplicate rows.

    python manage.py dedupe_opportunities              # report only (default)
    python manage.py dedupe_opportunities --apply      # merge IDENTITY groups

Output is grouped by mechanism, and the two are never treated alike:

  IDENTITY   Same posting, two addresses — the provider's own id says so.
             `--apply` merges these.
  LOOKALIKE  Same firm, title and location, no shared provider id. These may
             be one job filed twice or several real openings, and only the
             employer knows which. A merge here would be a guess, so `--apply`
             never touches them, at any volume, with any flag.

WHY --apply EXISTS NOW. It did not, originally, on the reasoning that "merging
rows is a decision, not a chore" — correct for LOOKALIKE and wrong for
IDENTITY, where the provider has already made the decision and the residue is
not inert. `provider_identity` (in ingest) stopped NEW duplicates on
2026-08-14, and `fold_duplicates` hides stored copies from the feed, but
neither cleans up what was already stored and neither helps a caller that
REASONS over rows instead of rendering them. On 2026-08-15
`capture_applications` refused a Bank of America confirmation as "2 roles match
that title about equally well" when both rows were tal.net opp 14594 under
candidate pools `pl/1` and `pl/2` — one posting, a choice that did not exist,
and a submitted application the board went on showing as untracked. The matcher
now collapses identity copies itself (`dupes.collapse_by_identity`), so that
particular caller is safe whatever the table holds; this command exists to stop
the table holding it.

WHAT A MERGE PRESERVES. The survivor is chosen by `dupes._survivor_rank`, the
same order the feed already uses to decide which copy a student sees, with
every row anyone tracks treated as sticky. Onto it are folded: the earliest
`first_seen` (the row has been on the board since its oldest copy), the most
recent verification, a stated deadline or location over a missing one, and
`open` over `closed` — a posting one url still serves is live, and the closed
copy is the artifact of the duplicate, not evidence of a death. Tracking rows
move across; where a user tracks both copies the two are combined, keeping the
furthest-along `applied_status`, the earliest `applied_at`, and the union of
`interview_dates`, so a merge can never walk a pipeline backwards. Losing rows
are then deleted.
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
