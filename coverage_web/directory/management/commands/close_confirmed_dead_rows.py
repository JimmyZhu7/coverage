"""close_confirmed_dead_rows — close specific open rows a gauntlet audit has
already re-verified live as gone (report-only by default).

    python manage.py close_confirmed_dead_rows            # report only (default)
    python manage.py close_confirmed_dead_rows --apply     # write the closes

WHY THIS EXISTS
---------------
The routine staleness sweep (`reverify`, default `--max-age-days 7`) only
picks up a row once its `last_checked` is a week old, oldest-first, capped at
--limit per run. A row re-verified as dead by a one-off audit in between
those windows sits open and visible on the board until the routine sweep
happens to reach it — sometimes days later. This command is the targeted,
same-day catch-up for rows a specific audit has ALREADY confirmed live
against the provider, rather than a fresh full-table scan (which would be
both slow and impolite to re-run against every provider for one row).

Confirmed live 2026-08-14: Opportunity id=8885 ("Marshall Wace",
"Recruitment Assistant", stored status='open',
url=https://job-boards.greenhouse.io/marshallwace/jobs/8501520002) —
`coverage_connectors.greenhouse.verify()` on that exact URL returns
`result='closed', evidence='boards-api 404 for job 8501520002 — no longer
listed'`. A direct `curl` against `boards-api.greenhouse.io` (bypassing the
connector) reproduces the same 404, and the firm's own board listing
(`.../v1/boards/marshallwace/jobs`) currently returns `{"jobs":[],
"meta":{"total":0}}` — a clean empty list, not an error, ruling out a
board-wide outage.

The DEFAULT_IDS list below is exactly the rows this round's audit confirmed;
--ids overrides it for any future one-off audit finding, so this command
doesn't need to be re-written each time. Live network, read-only against the
provider unless --apply is given; the live database itself is never written
to without it, per this repo's read-only-DB-by-default rule.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from coverage_connectors import verify

from directory.models import Opportunity

# Confirmed dead by this round's read-only audit — see module docstring.
DEFAULT_IDS = [8885]


class Command(BaseCommand):
    help = ("Re-verify specific open rows against the live provider and report "
            "(or, with --apply, close) the ones confirmed gone.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--ids", type=int, nargs="+", default=None,
            help="Opportunity ids to re-check. Defaults to this round's confirmed list.",
        )
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Close the confirmed-dead rows. Default is report-only.",
        )

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        tag = "" if apply_ else "[dry-run] "
        ids = opts["ids"] or DEFAULT_IDS

        rows = list(
            Opportunity.objects.filter(id__in=ids, status="open").select_related("firm")
        )
        missing = set(ids) - {o.id for o in rows}
        for m in missing:
            self.stdout.write(f"  #{m} — not found, or not currently status='open'; skipped")
        if not rows:
            self.stdout.write("No matching open rows to check.")
            return

        now = timezone.now()
        closed = checked = 0
        for o in rows:
            checked += 1
            try:
                result = verify(o.url)
            except Exception as exc:  # noqa: BLE001 — one bad URL must not kill the run
                self.stdout.write(f"  {o.firm.name} #{o.id} — unreachable: {exc}")
                continue
            if result.result != "closed":
                self.stdout.write(
                    f"  {o.firm.name} #{o.id} — still {result.result}, left alone: {result.evidence}")
                continue
            closed += 1
            self.stdout.write(
                f"{tag}{o.firm.name} #{o.id} — {o.title[:60]}: {result.evidence}")
            if apply_:
                o.status = "closed"
                o.closed_at = now
                o.last_checked = now
                o.save(update_fields=["status", "closed_at", "last_checked"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{checked} checked, {closed} confirmed gone "
            f"{'and closed' if apply_ else '(would be closed)'}."))
