"""detect_campaigns — group a user's outbound touches into bulk sends.

    python manage.py detect_campaigns --user you@example.com
    python manage.py detect_campaigns --all
    python manage.py detect_campaigns --user you@example.com --dry-run
    python manage.py detect_campaigns --user you@example.com --retire --dry-run
    python manage.py detect_campaigns --user you@example.com --retire

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
removes anyone, and even then only from the daily queue and the Network board
(which states the count and links to the list) — never from the contact book,
search, history or an export.

--dry-run prints what would be recorded and writes nothing, which is the right
first call against a real account.

--retire IS THE CLEAN-UP FOR A CAMPAIGN THAT SHOULD NEVER HAVE EXISTED.
Detection is append-only on purpose (see `crm.campaigns.detect`), so a row
recorded from a grouping key that has since stopped qualifying stays on the
Settings card forever with its question attached. Campaign 3 on the founder's
account was exactly that: 41 unrelated bankers grouped on `outreach sent no
reply yet`, which is Coverage's own boilerplate rather than anybody's subject
line, and one answer of "not my recruiting" would have silenced all 41.

What --retire will and will not do:

  - it retires ONLY campaigns whose signature no outbound touch of that user
    produces any more. Not "the count changed" — vanished entirely.
  - it NEVER touches a campaign the user has answered by hand. Those are
    listed under "held back" and left exactly as they are.
  - it deletes nothing. It stamps `Campaign.retired_at`, which removes the
    card from Settings and leaves the row, its label, its window and all of
    its memberships in place for the export.
  - it is reversible: `retired_at = None` restores the card, and a re-run of
    detection clears the stamp by itself if the signature ever qualifies
    again.

Pair it with --dry-run first. That prints the same two lists and writes
nothing.
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
        parser.add_argument(
            "--retire", action="store_true",
            help="Also retire campaigns whose signature no longer qualifies. "
                 "Never touches one the user has classified by hand.",
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

        retire = bool(options.get("retire"))
        for user in users:
            if options.get("dry_run"):
                self._report(user, retire=retire)
                continue
            campaigns = campaigns_svc.detect(user)
            self._print(user, campaigns)
            if retire:
                self._retire(user, dry_run=False)

    def _report(self, user, *, retire: bool = False) -> None:
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
                if retire:
                    # Inside the same rolled-back transaction, and AFTER
                    # detection: detection is what un-retires a signature that
                    # qualifies again, so asking "what has no live signature"
                    # before it has run would answer about a stale picture.
                    self._retire(user, dry_run=False)
                raise _Rollback()
        except _Rollback:
            pass

    def _retire(self, user, *, dry_run: bool) -> None:
        retired, held_back = campaigns_svc.retire_stale(user, dry_run=dry_run)
        if not retired and not held_back:
            self.stdout.write("  nothing to retire")
            return
        for c in retired:
            self.stdout.write(self.style.WARNING(
                f"  RETIRED  {c.recipient_count:4d} recipients  "
                f"{(c.label or c.signature)[:60]}"
            ))
        for c in held_back:
            self.stdout.write(
                f"  held back (answered {c.kind!r}, left alone)  "
                f"{c.recipient_count:4d} recipients  "
                f"{(c.label or c.signature)[:60]}"
            )

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
