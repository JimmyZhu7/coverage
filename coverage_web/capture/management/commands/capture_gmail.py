"""capture_gmail — apply a batch of Gmail findings to one user's CRM.

    python manage.py capture_gmail --email you@example.com --findings findings.json
    python manage.py capture_gmail --email you@example.com --findings -    # stdin
    python manage.py capture_gmail --email you@example.com --findings f.json --dry-run
    python manage.py capture_gmail --email you@example.com --window        # print + exit

WHERE FINDINGS COME FROM
------------------------
Not from here. This command is the *apply* half of a two-part design the
founder's single-user system has run daily for months: an agent searches the
mailbox with the Gmail API and emits findings; `netdash/gmail_enrich.py`
applies them to `campaign.db`. `capture.gmail` is that same apply layer pointed
at Coverage, and this command is its entry point — so one nightly search can
feed both systems.

Nothing in this path talks to Google. The `gmail.*` restricted-scope decision
(deploy.md §4) stays open; sign-in keeps `openid`/`email`/`profile` only.

--window prints how many days back the *next search* should reach, sized from
the last applied batch rather than hardcoded. Hardcoding "2 days" is correct
only while the job actually runs daily: miss three days and the activity in
that gap is never scanned by anything, because nothing re-reads older history.
So the caller asks for the window, then searches `newer_than:Nd`.

--dry-run runs every match, ratchet, and dedup decision and prints what WOULD
happen without writing. Use it the first time a new findings source is wired
up: a mis-shaped batch that clears the wrong addresses as bounced is the one
failure here that is tedious to unpick.
"""

from __future__ import annotations

import json
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from analytics.models import Import
from capture.gmail import apply_findings

# Mirrors netdash/gmail_enrich.py's window constants so the two systems size
# their searches identically.
DEFAULT_WINDOW_DAYS = 2   # 1 day of work + 1 day of overlap
MAX_WINDOW_DAYS = 30      # don't re-scan all of history after a long outage


def sync_window_days(user) -> int:
    """Days back the next Gmail search should reach: the gap since the last
    applied batch, plus a day of overlap, floored at the normal window and
    capped so a long outage doesn't become an unbounded re-scan.

    Derived from the `Import` ledger rather than a state file — Coverage is
    multi-tenant, so "when did this last run" is per-user and belongs in the
    database, not in one file on one host's disk.
    """
    last = (
        Import.all_objects.filter(user=user, kind="gmail_findings")
        .order_by("-created")
        .first()
    )
    if last is None:
        return DEFAULT_WINDOW_DAYS
    gap = (timezone.now() - last.created).days
    return max(DEFAULT_WINDOW_DAYS, min(gap + 1, MAX_WINDOW_DAYS))


class Command(BaseCommand):
    help = "Apply Gmail findings to a user's contacts (the daily sync's apply half)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument(
            "--findings",
            help="Path to a JSON array of findings, or '-' for stdin. "
                 "Omit only with --window.",
        )
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change; write nothing.")
        parser.add_argument("--window", action="store_true",
                            help="Print the next search window in days and exit.")

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(email=opts["email"])
        except User.DoesNotExist as exc:
            raise CommandError(f"no user {opts['email']}") from exc

        if opts["window"]:
            self.stdout.write(str(sync_window_days(user)))
            return

        if not opts["findings"]:
            raise CommandError("--findings is required (or pass --window)")

        raw = sys.stdin.read() if opts["findings"] == "-" else _read(opts["findings"])
        try:
            findings = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(f"findings is not valid JSON: {exc}") from exc
        if not isinstance(findings, list):
            raise CommandError("findings must be a JSON array")

        if opts["dry_run"]:
            # Delegated to apply_findings' own flag, NOT wrapped in
            # transaction.atomic(): log_touch commits on its own psycopg
            # connection, so an ORM rollback would leave those touches behind.
            # An earlier version of this command did exactly that and wrote
            # during a "dry" run.
            self._report(apply_findings(user, findings, dry_run=True), dry_run=True)
            return

        result = apply_findings(user, findings)
        Import.all_objects.create(
            user=user,
            kind="gmail_findings",
            filename=(opts["findings"] if opts["findings"] != "-" else "stdin")[:255],
            row_stats=result.as_stats(),
        )
        self._report(result, dry_run=False)

    def _report(self, result, *, dry_run: bool) -> None:
        prefix = "[dry-run] " if dry_run else ""
        for line in result.details:
            self.stdout.write(f"  {prefix}{line}")
        self.stdout.write(
            f"{prefix}{result.findings} findings: "
            f"{result.touches_logged} touches, {result.outreach_logged} outreach, "
            f"{result.emails_backfilled} emails backfilled, "
            f"{result.alternate_emails_noted} alternate emails noted, "
            f"{result.bounced_cleared} bounced addresses cleared"
        )
        self.stdout.write(
            f"{prefix}skipped: {result.skipped_already_logged} already logged, "
            f"{result.skipped_not_found} not found, "
            f"{result.skipped_unmatched} unmatched in Coverage"
        )
        # Only when they happen. These three are rare next to the counts above
        # — most runs schedule no chat and bank no pattern evidence — and a
        # permanent "0 chats scheduled" trains the reader to skip the line that
        # matters on the day it isn't zero.
        extras = [
            (result.chats_scheduled, "chats put on the calendar"),
            (result.pattern_delivered, "email patterns confirmed"),
            (result.pattern_bounced, "email patterns disproved"),
        ]
        shown = [f"{count} {label}" for count, label in extras if count]
        if shown:
            self.stdout.write(f"{prefix}{', '.join(shown)}")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise CommandError(f"can't read {path}: {exc}") from exc
