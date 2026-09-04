"""gcal_sync — pull connected Google Calendars onto the Coverage timeline.

DRY BY DEFAULT, AND THAT IS THE HOUSE RULE, NOT A PREFERENCE. Anything in
this project that writes says what it would do first and needs `--apply` to
do it. `gmail_poll` is the exception rather than the model: it is driven by
a launchd job that must write on every tick, so its default is the write
and `--dry-run` is the flag. This command has no scheduler behind it yet —
a human runs it, and a human running a sync for the first time against a
real calendar should see the list before the rows exist.

WHAT A DRY RUN STILL DOES. It talks to Google: an OAuth refresh and one or
more `events.list` pages. That is deliberate and is `gmail_live.
preview_sync`'s reasoning — a dry run that skipped the network could not
tell a working connection from a revoked one, which is the single most
useful thing it reports. What it does not do is write: no rows, no cursor
advance, no `last_synced_at`, no status flip.

THE CURSOR IS THE REASON THIS IS CHEAP TO RE-RUN. After the first pass the
connection holds a Google sync token, and every later run asks only "what
changed since this". A calendar nobody touched costs one HTTP call and
prints zeroes.

    manage.py gcal_sync                      # every connected calendar, dry
    manage.py gcal_sync --user a@b.com       # just that student's, dry
    manage.py gcal_sync --user a@b.com --apply
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from capture import gcal_live
from capture.models import GoogleCalendarConnection

User = get_user_model()


class Command(BaseCommand):
    help = "Sync connected Google Calendars onto the timeline (dry by default)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user", metavar="EMAIL",
            help="Sync just this user's calendar. Omit for every connected one.",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write. Without it this reports and changes nothing.",
        )

    def handle(self, *args, **opts):
        if not gcal_live.is_configured():
            # Not an error: an unconfigured deploy is the NORMAL state until
            # the consent screen carries the calendar scope, and a cron
            # wrapper should not start failing the day it is added.
            self.stdout.write(
                "Google Calendar is not configured (GCAL_LIVE_ENABLED, plus "
                "the Gmail Live client id, secret and token key this shares) "
                "— nothing to sync."
            )
            return

        apply = opts["apply"]
        connections = GoogleCalendarConnection.all_objects.select_related("user").filter(
            status="active"
        )
        email = (opts.get("user") or "").strip()
        if email:
            user = User.objects.filter(email__iexact=email).first()
            if user is None:
                raise CommandError(f"No user with email {email}.")
            connections = connections.filter(user=user)

        connections = list(connections)
        if not connections:
            self.stdout.write("No active calendar connections.")
            return

        totals = gcal_live.GcalSyncResult()
        failures = 0
        for connection in connections:
            try:
                result = gcal_live.sync_connection(connection, dry_run=not apply)
            except gcal_live.GcalError as exc:
                # One student's revoked grant must not stop the pass. The
                # row is already marked `revoked` by `sync_connection`, so
                # the Settings card tells them; this line tells whoever ran
                # the command.
                failures += 1
                self.stderr.write(f"{connection.google_email}: {exc}")
                continue
            totals.created += result.created
            totals.updated += result.updated
            totals.cancelled += result.cancelled
            totals.adopted += result.adopted
            totals.unchanged += result.unchanged
            totals.skipped += result.skipped
            totals.resynced = totals.resynced or result.resynced
            for line in result.details:
                self.stdout.write(f"  {connection.google_email}: {line}")

        # THE SUMMARY LINE, and it always renders — a run that found nothing
        # says so in the same shape as a run that found plenty, so a log of
        # them can be skimmed rather than read.
        mode = "applied" if apply else "dry run, nothing written"
        line = (
            f"{len(connections)} calendar{'' if len(connections) == 1 else 's'}: "
            f"{totals.summary()} ({mode})"
        )
        if failures:
            line += f" · {failures} failed"
        self.stdout.write(line)
        if not apply and totals.changed:
            self.stdout.write("Re-run with --apply to write these.")
