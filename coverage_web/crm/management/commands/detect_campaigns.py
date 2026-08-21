"""detect_campaigns — group a user's outbound touches into bulk sends.

    python manage.py detect_campaigns --user you@example.com
    python manage.py detect_campaigns --all
    python manage.py detect_campaigns --user you@example.com --dry-run

WHAT IT DOES. Runs `crm.campaigns.detect`, which finds groups of outbound
touches sharing one normalized subject inside a 24-hour window with at least
`BULK_MIN_RECIPIENTS` distinct recipients, and records each as a `Campaign` the
user can then answer one question about in Settings. See `crm/campaigns.py` for
the signals, the threshold, and why the threshold is where it is.

SAFE TO RE-RUN, AND MEANT TO BE. Detection is idempotent: it updates a
campaign's label, window and recipient count, never its classification. A `kind`
the user has answered is locked by `classified_at` and no run touches it.

WHAT IT DOES NOT DO. It never hides anybody by itself. A freshly detected
campaign is `unclassified`, which behaves exactly as before it existed —
everyone stays in the daily queue. Only the user's own answer in Settings
removes anyone, and even then only from the daily queue, never from the contact
book, the Network board, search, history or an export.

--dry-run prints what would be recorded and writes nothing, which is the right
first call against a real account.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm import campaigns as campaigns_svc
from crm.models import Campaign, Touch


class Command(BaseCommand):
    help = "Group a user's outbound touches into bulk-send campaigns."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Email of the user to scan.")
        parser.add_argument(
            "--all", action="store_true",
            help="Scan every user. Mutually exclusive with --user.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be recorded; write nothing.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        if bool(options.get("user")) == bool(options.get("all")):
            raise CommandError("Pass exactly one of --user <email> or --all.")

        if options.get("all"):
            users = list(User.objects.all())
        else:
            users = list(User.objects.filter(email__iexact=options["user"]))
            if not users:
                raise CommandError(f"No user with email {options['user']!r}.")

        for user in users:
            if options.get("dry_run"):
                self._report(user)
                continue
            campaigns = campaigns_svc.detect(user)
            self._print(user, campaigns)

    def _report(self, user) -> None:
        """Dry run. Deliberately re-runs the real detector inside a
        transaction that is then rolled back, rather than reimplementing the
        grouping here — a dry run that used different code from the real one
        would be reporting on a program nobody is going to execute."""
        touches = Touch.objects.for_user(user).filter(
            kind__in=campaigns_svc.OUTBOUND_KINDS
        ).count()
        try:
            with transaction.atomic():
                campaigns = campaigns_svc.detect(user)
                self.stdout.write(
                    f"{user.email}: {touches} outbound touches -> "
                    f"{len(campaigns)} campaign(s) [DRY RUN, rolled back]"
                )
                self._rows(campaigns)
                raise _Rollback()
        except _Rollback:
            pass

    def _print(self, user, campaigns) -> None:
        self.stdout.write(f"{user.email}: {len(campaigns)} campaign(s)")
        self._rows(campaigns)

    def _rows(self, campaigns) -> None:
        for c in campaigns:
            flag = "" if c.kind == Campaign.KIND_UNCLASSIFIED else f" [{c.kind}]"
            self.stdout.write(
                f"  {c.recipient_count:4d} recipients  "
                f"{c.first_sent:%Y-%m-%d}  {(c.label or c.signature)[:70]}{flag}"
            )


class _Rollback(Exception):
    """Internal: unwinds the dry run's transaction. Never escapes handle()."""
