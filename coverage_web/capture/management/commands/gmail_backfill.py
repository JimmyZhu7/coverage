"""gmail_backfill — the one-time historical pass for a newly-connected
Gmail Live mailbox.

`connect_gmail()` (capture/gmail_live.py) marks a fresh connection
`backfill_status="pending"` right after the live watch is registered — so
live coverage starts immediately, and this command is what fills in the
past. Run it on a short tick (10-15 min is plenty; most runs find nothing
pending). Never run inline in the OAuth callback: a year of per-contact
Gmail searches is a multi-minute job, and the connect flow's redirect has
to stay instant.

    python manage.py gmail_backfill
    python manage.py gmail_backfill --email you@example.com
    python manage.py gmail_backfill --dry-run

Picks up connections whose `backfill_status` is `pending` (a first run) or
`failed` (an earlier attempt died partway — Google API hiccups happen over
a multi-minute job, and this retries automatically on the next tick rather
than leaving a mailbox stuck with no history forever). `done` is sticky and
never re-selected — see `connect_gmail`'s comment on why a reconnect must
not silently re-run a completed backfill.

`--dry-run` runs every search, classification, and apply decision and
writes nothing — including no `backfill_status` transition and no `Import`
row — same discipline `capture_gmail --dry-run` already uses, for the same
reason: a mis-scoped first run is the one mistake here that's tedious to
unpick after the fact.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from analytics.models import Import
from capture import gmail_live
from capture.models import GmailConnection


class Command(BaseCommand):
    help = "Run the one-time historical backfill for pending Gmail Live connections."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Run backfill for just this user.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would happen; write nothing (no status change, no Import row).",
        )

    def handle(self, *args, **opts):
        if not gmail_live.is_configured():
            self.stdout.write("Gmail Live is not configured — nothing to backfill.")
            return

        connections = GmailConnection.all_objects.filter(status="active").filter(
            Q(backfill_status="pending") | Q(backfill_status="failed")
        )
        if opts["email"]:
            connections = connections.filter(user__email=opts["email"])

        connections = list(connections)
        if not connections:
            self.stdout.write("Nothing pending.")
            return

        for connection in connections:
            prefix = "[dry-run] " if opts["dry_run"] else ""
            if not opts["dry_run"]:
                connection.backfill_status = "running"
                connection.save(update_fields=["backfill_status"])

            try:
                result = gmail_live.backfill_connection(
                    connection, dry_run=opts["dry_run"]
                )
            except Exception as exc:  # noqa: BLE001
                if not opts["dry_run"]:
                    connection.backfill_status = "failed"
                    connection.save(update_fields=["backfill_status"])
                self.stderr.write(
                    f"{prefix}{connection.gmail_address}: backfill failed, will "
                    f"retry next run: {exc}"
                )
                continue

            if not opts["dry_run"]:
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
