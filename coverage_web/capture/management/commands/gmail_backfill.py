"""gmail_backfill — the one-time historical pass for a newly-connected
Gmail Live mailbox, AND the user-triggered "Scan Now" rescan (Settings >
Gmail Live). One command, one tick, two independent selections — see the
build plan's Phase 2 note on why this reuses the existing scheduling
mechanism rather than inventing a second one.

`connect_gmail()` (capture/gmail_live.py) marks a fresh connection
`backfill_status="pending"` right after the live watch is registered — so
live coverage starts immediately, and this command is what fills in the
past. `capture.views.gmail_rescan` marks a connection `rescan_status=
"pending"` when a user presses "Scan Now" in Settings. Run this command on
a short tick (10-15 min is plenty; most runs find nothing pending in
either queue). Never run either pass inline in a request: a year of
per-contact Gmail searches is a multi-minute job, and both the connect
flow's redirect and the Settings POST have to stay instant.

    python manage.py gmail_backfill
    python manage.py gmail_backfill --email you@example.com
    python manage.py gmail_backfill --dry-run

BACKFILL selection: `backfill_status` `pending` (a first run), `failed` (an
earlier attempt died partway — Google API hiccups happen over a
multi-minute job, and this retries automatically on the next tick rather
than leaving a mailbox stuck with no history forever), OR `running` for
longer than `STALE_RUNNING_AFTER` — a process killed mid-run (SIGKILL, OOM,
a redeploy) leaves the row parked at `running` with nothing left to finish
it; without this a stuck row would never be picked up again by ANY future
tick, since `running` isn't one of the two states above. `done` is sticky
and never re-selected — see `connect_gmail`'s comment on why a reconnect
must not silently re-run a completed backfill.

RESCAN selection: same three-way `pending` / `failed` / stale-`running`
selection, off `rescan_status` and `rescan_started_at` instead (NOT
`rescan_requested_at` — that's only when the button was pressed, which can
be a while before a tick actually picks a `pending` row up; anchoring
staleness there reclaimed rescans that were still genuinely running, just
slow to start, causing a duplicate `spend_rescan` charge and duplicate
touch writes on the same mailbox). Deliberately
a SEPARATE field/query from `backfill_status` — a rescan is a different,
repeatable action, not the one-time original backfill; see
`GmailConnection.rescan_status`'s own comment. Runs `gmail_live.run_rescan`,
which is the deterministic pass over ALL contacts followed by the capped
Haiku residue stage (`capture.gmail_residue`) — never run as part of the
BACKFILL selection above, only here.

`--dry-run` runs every search, classification, and apply decision for
BOTH selections and writes nothing — including no status transition and
no `Import` row, and (per `run_rescan`) no AI call for the residue stage
either — same discipline `capture_gmail --dry-run` already uses, for the
same reason: a mis-scoped first run is the one mistake here that's tedious
to unpick after the fact.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from analytics.models import Import
from capture import gmail_live, locks
from capture.models import GmailConnection
from ops.tracking import track_job_run

# How long a `running` row is trusted to actually still be running before
# this command treats it as abandoned (a crashed process) and picks it back
# up. Both jobs are documented as multi-minute at their largest even on a
# full mailbox; two hours is generous enough that a real long run is never
# falsely reclaimed, while a genuinely dead one is not stuck forever. A
# cron tick shorter than this (the module docstring recommends 10-15 min)
# means a row is checked many times before it's ever reclaimed, not
# reclaimed on the very next tick after it dies.
STALE_RUNNING_AFTER = timedelta(hours=2)


class Command(BaseCommand):
    help = (
        "Run the one-time historical backfill for pending Gmail Live "
        "connections, and any queued 'Scan Now' rescans."
    )

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Run backfill/rescan for just this user.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would happen; write nothing (no status change, no Import row).",
        )

    def handle(self, *args, **opts):
        # Named "gmail-backfill" for /ops/health/cron/ to match render.yaml's
        # coverage-gmail-backfill cron — see ops/tracking.py. Wraps the WHOLE
        # tick, including the "nothing pending" no-op: a run that legitimately
        # found nothing to do is still a run, and the health check is asking
        # "is this cron ticking at all", not "did it find work".
        with track_job_run("gmail-backfill"):
            if not gmail_live.is_configured():
                self.stdout.write("Gmail Live is not configured — nothing to backfill.")
                return

            base = GmailConnection.all_objects.filter(status="active")
            if opts["email"]:
                base = base.filter(user__email=opts["email"])

            stale_cutoff = timezone.now() - STALE_RUNNING_AFTER
            backfill_connections = list(
                base.filter(
                    Q(backfill_status="pending")
                    | Q(backfill_status="failed")
                    | Q(backfill_status="running", backfill_started_at__lt=stale_cutoff)
                    # A "running" row with no timestamp at all predates this
                    # field (or something else cleared it) — treat it the same
                    # as stale rather than leaving it stuck for a different
                    # reason.
                    | Q(backfill_status="running", backfill_started_at__isnull=True)
                )
            )
            rescan_connections = list(
                base.filter(
                    Q(rescan_status="pending")
                    | Q(rescan_status="failed")
                    | Q(rescan_status="running", rescan_started_at__lt=stale_cutoff)
                    | Q(rescan_status="running", rescan_started_at__isnull=True)
                )
            )

            if not backfill_connections and not rescan_connections:
                self.stdout.write("Nothing pending.")
                return

            for connection in backfill_connections:
                self._run_backfill(connection, dry_run=opts["dry_run"])

            for connection in rescan_connections:
                self._run_rescan(connection, dry_run=opts["dry_run"])

    def _run_backfill(self, connection, *, dry_run: bool) -> None:
        prefix = "[dry-run] " if dry_run else ""
        # The shared per-mailbox lock (capture.locks): a backfill walks the
        # same apply_findings machinery the 2-minute poll loop does, and
        # used to be free to interleave with a live pass on the same
        # mailbox. Taken BEFORE the status flips to "running", so a skipped
        # run leaves the row `pending`/`failed` and the next cron tick
        # simply tries again — by which time the seconds-long poll pass
        # holding it is long gone. A dry run writes nothing and takes no
        # lock, same as the poll command's rule 4.
        if not dry_run and not locks.try_mailbox_lock(connection.pk):
            self.stdout.write(
                f"{connection.gmail_address}: mailbox is being synced by "
                "another run — backfill deferred to the next tick"
            )
            return
        try:
            self._run_backfill_locked(connection, dry_run=dry_run, prefix=prefix)
        finally:
            if not dry_run:
                locks.unlock_mailbox(connection.pk)

    def _run_backfill_locked(self, connection, *, dry_run: bool, prefix: str) -> None:
        if not dry_run:
            connection.backfill_status = "running"
            connection.backfill_started_at = timezone.now()
            connection.save(update_fields=["backfill_status", "backfill_started_at"])

        try:
            result = gmail_live.backfill_connection(connection, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            if not dry_run:
                connection.backfill_status = "failed"
                connection.save(update_fields=["backfill_status"])
            self.stderr.write(
                f"{prefix}{connection.gmail_address}: backfill failed, will "
                f"retry next run: {exc}"
            )
            return

        if not dry_run:
            Import.all_objects.create(
                user=connection.user,
                kind="gmail_backfill",
                filename=connection.gmail_address,
                row_stats=result.as_stats(),
            )

        self.stdout.write(
            f"{prefix}{connection.gmail_address}: {result.findings} findings, "
            f"{result.touches_logged} touches, {result.outreach_logged} outreach, "
            f"{result.bounced_cleared} bounces cleared"
        )
        for line in result.details:
            self.stdout.write(f"  {prefix}{line}")

    def _run_rescan(self, connection, *, dry_run: bool) -> None:
        prefix = "[dry-run] " if dry_run else ""
        # Same shared lock, same reasoning and same skip semantics as
        # `_run_backfill` above — the "Scan Now" rescan is the other
        # long-running writer that could interleave with a live poll pass.
        if not dry_run and not locks.try_mailbox_lock(connection.pk):
            self.stdout.write(
                f"{connection.gmail_address}: mailbox is being synced by "
                "another run — rescan deferred to the next tick"
            )
            return
        try:
            self._run_rescan_locked(connection, dry_run=dry_run, prefix=prefix)
        finally:
            if not dry_run:
                locks.unlock_mailbox(connection.pk)

    def _run_rescan_locked(self, connection, *, dry_run: bool, prefix: str) -> None:
        if not dry_run:
            connection.rescan_status = "running"
            connection.rescan_started_at = timezone.now()
            connection.save(update_fields=["rescan_status", "rescan_started_at"])

        try:
            stats = gmail_live.run_rescan(connection, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            if not dry_run:
                connection.rescan_status = "failed"
                connection.save(update_fields=["rescan_status"])
            self.stderr.write(
                f"{prefix}{connection.gmail_address}: rescan failed, will "
                f"retry next run: {exc}"
            )
            return

        if not dry_run:
            connection.rescan_status = "done"
            connection.rescan_completed_at = timezone.now()
            connection.rescan_stats = stats
            connection.save(
                update_fields=["rescan_status", "rescan_completed_at", "rescan_stats"]
            )
            Import.all_objects.create(
                user=connection.user,
                kind="gmail_rescan",
                filename=connection.gmail_address,
                row_stats=stats,
            )

        residue = stats.get("residue", {})
        self.stdout.write(
            f"{prefix}{connection.gmail_address}: rescan — {stats.get('touches_logged', 0)} "
            f"touches, residue {residue.get('residue_threads_processed', 0)}/"
            f"{residue.get('residue_threads_seen', 0)} threads processed "
            f"({residue.get('genuine_reply', 0)} genuine, {residue.get('auto_reply', 0)} auto-reply)"
        )
