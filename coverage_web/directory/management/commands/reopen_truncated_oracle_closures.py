"""reopen_truncated_oracle_closures — find closed Oracle rows a truncated
fetch closed by mistake, and reopen them (report-only by default).

    python manage.py reopen_truncated_oracle_closures            # report only (default)
    python manage.py reopen_truncated_oracle_closures --apply     # write the reopens

WHY THIS EXISTS
---------------
Before this same change, `coverage_connectors.oracle.fetch()` never told
ingest when a board's search had been capped short of everything Oracle
itself reports (`TotalJobsCount`) — every other capped connector
(workday/eightfold/avature/icims/goldmansachs) does, and oracle.py's own
closed-detection then ran unguarded on an incomplete "seen" set. Confirmed
live: JPM's own "insight" keyword search reports 1,631 matching
requisitions against this connector's 25-per-keyword cap, and a genuinely
live, freshly-posted role (opportunity 4731, "Analytics Solutions Manager,
Vice President", posted 2026-08-10) simply ranked outside the top 25 and
was closed alongside 3 other JPM oracle rows in the same batch
(2026-08-13 15:26:17).

The fetch-side fix stops NEW false closures. This command is the one-time
catch-up: every closed, oracle-sourced row is re-checked live against
Oracle's own verify endpoint (the same one `reverify` uses), and only rows
that come back "verified-open" are reported as reopen candidates — a
network error or an undecidable result changes nothing, same honesty
contract as `reverify`.

Live network, read-only against the provider; DB writes require --apply.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from coverage_connectors import verify

from directory.models import Opportunity


class Command(BaseCommand):
    help = ("Re-verify closed Oracle rows against the live provider and report "
            "(or, with --apply, reopen) the ones a truncated fetch closed wrongly.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Reopen the confirmed-live rows. Default is report-only.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after N rows (0 = all).",
        )

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        tag = "" if apply_ else "[dry-run] "

        rows = list(
            Opportunity.objects.filter(status="closed", source="oracle")
            .select_related("firm")
            .order_by("firm__name", "id")
        )
        if opts["limit"]:
            rows = rows[: opts["limit"]]
        if not rows:
            self.stdout.write("No closed Oracle rows to check.")
            return

        now = timezone.now()
        reopened = checked = 0
        for o in rows:
            checked += 1
            try:
                result = verify(o.url)
            except Exception as exc:  # noqa: BLE001 — one bad URL must not kill the run
                self.stdout.write(f"  {o.firm.name} #{o.id} — unreachable: {exc}")
                continue
            if result.result != "verified-open":
                continue
            reopened += 1
            self.stdout.write(
                f"{tag}{o.firm.name} #{o.id} — {o.title[:60]}: "
                f"closed_at={o.closed_at} but {result.evidence}")
            if apply_:
                o.status = "open"
                o.closed_at = None
                o.last_verified = now
                o.last_checked = now
                o.save(update_fields=["status", "closed_at", "last_verified", "last_checked"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{checked} checked, {reopened} confirmed still live "
            f"{'and reopened' if apply_ else '(would be reopened)'}."))
