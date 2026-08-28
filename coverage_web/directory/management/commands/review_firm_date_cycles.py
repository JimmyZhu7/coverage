"""review_firm_date_cycles — which recruiting dates nobody has filed to a cycle.

    python manage.py review_firm_date_cycles

Read-only and idempotent. Prints nothing but a report.

WHY IT EXISTS
-------------
Migration 0014 closed the `firm_dates.cycle` vocabulary and refused to guess a
cycle for the rows it could not map — a `cycle` it cannot read becomes ""
("not stated") with the original string kept in `history`, rather than being
rounded into whichever bucket most of its neighbours are in. That is the right
call at write time and a dead end at read time: a blank cycle is invisible, and
"three rows need a human" is not a fact that should live only in a migration
docstring nobody re-reads.

This command is that list, recomputed from the live table every time. A row
leaves it the moment someone gives it a cycle.

WHAT A BLANK CYCLE COSTS
------------------------
A dated event with no cycle still renders on the firm page — it is a real
deadline and hiding it would be worse. What it cannot do is match a student:
the timeline marks the rows belonging to the cycle a student SAID they are
recruiting for, and a row with no cycle can never be one of them. Two of the
three live blanks are confirmed_official closes at J.P. Morgan and Goldman
Sachs, which is exactly the kind of date a student most needs matched.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from directory.models import FirmDate
from directory.timeline import CYCLE_TRACKS


class Command(BaseCommand):
    help = "List firm_dates rows with no cycle on file, and why that matters."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true",
            help="Also print the rows that DO have a cycle, grouped by cycle.",
        )

    def handle(self, *args, **opts):
        blanks = (FirmDate.objects.filter(cycle="")
                  .select_related("firm").order_by("firm__name", "event_kind"))
        total = FirmDate.objects.count()

        if not blanks:
            self.stdout.write(self.style.SUCCESS(
                f"Every one of the {total} firm_dates rows names a cycle."))
        else:
            self.stdout.write(self.style.WARNING(
                f"{blanks.count()} of {total} firm_dates rows have no cycle on file. "
                f"They render on the firm page but can never match a student's "
                f"stated cycle."))
            for row in blanks:
                was = _original_cycle(row)
                origin = f"  was {was!r}" if was else ""
                self.stdout.write(
                    f"  #{row.id:<4} {row.firm.name:<24} {row.event_kind:<18} "
                    f"{row.date or '(no date)'}  conf {row.confidence}{origin}")
            self.stdout.write(
                "\n  To file one: set `cycle` to a season+year slug "
                f"(sa2028, ft2027) and optionally `track` to one of "
                f"{', '.join(CYCLE_TRACKS)}.")

        if not opts["all"]:
            return

        self.stdout.write("\nRows by cycle:")
        rows = FirmDate.objects.exclude(cycle="").select_related("firm")
        by_cycle: dict[tuple[str, str], list] = {}
        for row in rows:
            by_cycle.setdefault((row.cycle, row.track), []).append(row)
        for (cycle, track), group in sorted(by_cycle.items()):
            scope = f"{cycle}/{track}" if track else cycle
            firms = sorted({r.firm.name for r in group})
            self.stdout.write(
                f"  {scope:<14} {len(group):>3} rows across {len(firms)} firms")


def _original_cycle(row) -> str:
    """What `cycle` said before 0014 blanked it, if it said anything.

    Read out of `history` rather than stored in a second column: history is
    already this model's append-only provenance record, and a column that only
    ever holds a value for three rows is not a column.
    """
    for entry in reversed(list(row.history or [])):
        if isinstance(entry, dict) and entry.get("was_cycle"):
            return str(entry["was_cycle"])
    return ""
