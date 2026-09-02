"""board_health — the per-board table behind the health report's one-liners.

    python manage.py board_health              # every board, worst first
    python manage.py board_health --alarming   # only the ones with a finding

Read-only: one query per aggregate, no writes, no network. `refresh` prints
`health_report()`'s summary lines after every run; those lines name the
boards but not the provider's own message, and this is where the message is.

The report `refresh` prints is per FIRM for everything except the lines this
table backs, and that was the gap: the catalog holds 127 boards under 110
slugs, so 13 firms have a board whose verdict was being averaged away behind
a sibling. See `health.board_health` for the states and what each one means.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from directory.health import _ALARMING_BOARD_STATES, board_health, board_health_table


class Command(BaseCommand):
    help = "Print one line per catalog board with what the latest full scrape did to it."

    def add_arguments(self, parser):
        parser.add_argument(
            "--alarming", action="store_true",
            help="Only boards in a state worth acting on (failed / wiped).")

    def handle(self, *args, **opts):
        if not opts["alarming"]:
            self.stdout.write(board_health_table())
            return
        rows = [b for b in board_health() if b["state"] in _ALARMING_BOARD_STATES]
        if not rows:
            self.stdout.write(self.style.SUCCESS("No board is failing or silently wiped."))
            return
        for row in sorted(rows, key=lambda r: (r["state"], r["slug"])):
            self.stdout.write(self.style.WARNING(
                f"{row['state']:<7} {row['slug']}/{row['board']} ({row['provider']}) "
                f"— {row['open_rows']} open rows: {row['error'] or 'fetched clean, zero rows'}"))
