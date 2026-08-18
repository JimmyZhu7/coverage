"""merge_duplicate_firms — report, and optionally merge, Firm rows that
share the same name.

    python manage.py merge_duplicate_firms              # report only (default)
    python manage.py merge_duplicate_firms --apply      # merge for real

Unlike `dedupe_opportunities`'s LOOKALIKE section, a Firm name collision is
not a guess: see `directory.firm_merge`'s module docstring for why a
proper-noun employer name is treated as an identifier here, not descriptive
text a human still has to judge. `--apply` is fully implemented and tested
(directory/tests/test_firm_merge.py) — the reason it is still gated behind
an explicit flag, and the reason THIS round's dedup builder ran it in
report-only mode against the live/dev database rather than with --apply, is
process, not code: live data is read-only to a builder session per this
round's standing rules, and a merge that deletes 1+ Firm rows and reparents
thousands of Opportunity/tracking rows is exactly the kind of write that
needs the founder or the main session to actually pull the trigger, not an
agent's own initiative.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from directory.firm_merge import find_duplicate_firm_groups, merge_firms
from directory.models import Opportunity


class Command(BaseCommand):
    help = "Report (and, with --apply, merge) Firm rows that share the same name."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Merge for real. Without this flag, report only and write nothing.",
        )

    def handle(self, *args, **options):
        groups = find_duplicate_firm_groups()
        if not groups:
            self.stdout.write("No duplicate firm names found.")
            return

        for group in groups:
            canonical, *dupes = group
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"{canonical.name!r} — {len(group)} rows share this name"))
            for firm in group:
                tag = " (canonical — lowest id)" if firm.id == canonical.id else ""
                self.stdout.write(
                    f"  id={firm.id:<6} slug={firm.slug:<20} "
                    f"opportunities={firm.opportunities.count():<6}{tag}"
                )
            for dup in dupes:
                shared_open = Opportunity.objects.filter(
                    firm=dup, status="open",
                    url__in=Opportunity.objects.filter(firm=canonical).values("url"),
                ).count()
                shared_total = Opportunity.objects.filter(
                    firm=dup,
                    url__in=Opportunity.objects.filter(firm=canonical).values("url"),
                ).count()
                self.stdout.write(
                    f"    id={dup.id} -> id={canonical.id}: {shared_total} URLs already exist "
                    f"under the canonical firm ({shared_open} open on both sides right now)"
                )

            if options["apply"]:
                with transaction.atomic():
                    for dup in dupes:
                        stats = merge_firms(canonical, dup)
                        self.stdout.write(f"    merged: {stats}")
            else:
                self.stdout.write("    (dry run — pass --apply to merge for real)")

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write("No rows were changed. Pass --apply to merge for real.")
