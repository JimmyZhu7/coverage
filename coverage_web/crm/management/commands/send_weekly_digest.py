"""send_weekly_digest — Coverage's retention loop: what closes this week, who
to ping, and (when there's a real fit) what's new, mailed once a week to
every onboarded student who has something to act on.

    python manage.py send_weekly_digest --dry-run            # render every recipient, send nothing
    python manage.py send_weekly_digest --user a@b.com       # one account, real send (testing)
    python manage.py send_weekly_digest                      # every onboarded, opted-in-by-default user

WHY THIS SHIPS COMPLETE WITH ZERO PAID SETUP. Outbound email already has one
point of configuration, in settings/production.py:
`vars().update(env.email_url("EMAIL_URL", default="consolemail://"))`. Unset,
mail is printed to the service logs instead of actually sent — so this
command is safe to point a cron/scheduler at in production today, before
anyone has bought an SMTP relay: nothing breaks, nothing bounces, nothing
costs money, and the rendered digest is inspectable in the logs exactly like
a password-reset email is today. Buying a real provider later is a one-line
env var change (`EMAIL_URL=smtp+tls://...`, see .env.example); it requires no
change to this command, `crm/digest.py`, or the templates.

WHO GETS ONE. Every `User` with `onboarded_at` set (accounts/models.py) and
`deleted_at` unset, AND something to report this week — `crm.digest.
assemble_digest` returns `None` for everyone else, and that skip is logged
by email so a dry run reads as a full roster, not a silent partial one. See
that module's docstring for the exact "nothing to report" rule.

NO OPT-OUT YET, ON PURPOSE. `accounts.models.User` carries no email-
preference column today (checked before writing this command). Rather than
invent a settings-page control this pass doesn't own, every onboarded user
with content gets the digest — a real opt-out is a natural next step, flagged
in this feature's commit message, not built here.

TIMEZONE. Every other "today" boundary in this app activates the user's own
`User.timezone` per request (`accounts.middleware.TimezoneMiddleware`) so a
Hong Kong student's week rolls over on Hong Kong midnight, not UTC's. A
management command has no request for that middleware to run against, so
`_local_today` below replicates its exact activate/fallback/deactivate
contract for each user in turn — see that function's docstring.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string
from django.utils import timezone

from crm.digest import assemble_digest


def _local_today(user) -> date:
    """This user's `timezone.localdate()`, activated exactly the way
    `accounts.middleware.TimezoneMiddleware` would for a request: a blank or
    unrecognized zone falls back to the project default rather than raising,
    the same "validated on write is not trustworthy on read" posture that
    middleware documents. Caller is responsible for `timezone.deactivate()`
    afterward — this loop runs many users on one thread/process, and a stuck
    activation would leak one student's zone into the next student's
    "today", exactly the bug the middleware's own `finally` exists to
    prevent."""
    name = (getattr(user, "timezone", "") or "").strip()
    if name:
        try:
            timezone.activate(ZoneInfo(name))
        except (ZoneInfoNotFoundError, ValueError):
            timezone.deactivate()
    else:
        timezone.deactivate()
    return timezone.localdate()


def _subject(digest: dict) -> str:
    """No em dash (house copy style): a comma-joined clause list instead."""
    n_closing = len(digest["closing"])
    n_actions = len(digest["actions"])
    parts = []
    if n_closing:
        parts.append(f"{n_closing} closing this week")
    if n_actions:
        parts.append(f"{n_actions} to ping")
    # assemble_digest only ever returns a digest when at least one of these
    # is non-empty, so `parts` is never empty here — the fallback exists so a
    # future third section can't silently produce a blank subject.
    return "Coverage: " + ", ".join(parts) if parts else "Coverage: your weekly digest"


class Command(BaseCommand):
    help = "Send the weekly digest email to onboarded users who have something to act on."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Render every recipient's digest and report it. Sends nothing.")
        parser.add_argument(
            "--user", metavar="EMAIL", default="",
            help="Restrict to one account by email, bypassing the onboarded_at "
                 "gate — for previewing or testing a specific user's digest.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        tag = "[dry-run] " if dry else ""
        User = get_user_model()

        if opts["user"]:
            try:
                users = [User.objects.get(email__iexact=opts["user"])]
            except User.DoesNotExist as exc:
                raise CommandError(f"no user with email {opts['user']!r}") from exc
        else:
            users = list(
                User.objects.filter(onboarded_at__isnull=False, deleted_at__isnull=True)
                .order_by("email")
            )

        if not users:
            self.stdout.write("No eligible users (onboarded, not deleted).")
            return

        site_url = getattr(settings, "SITE_URL", "").rstrip("/")
        sent = skipped = 0
        for user in users:
            try:
                today = _local_today(user)
                digest = assemble_digest(user, today=today)
            finally:
                timezone.deactivate()

            if digest is None:
                skipped += 1
                self.stdout.write(f"{tag}- {user.email}: nothing to report, skipped")
                continue

            ctx = {"user": user, "digest": digest, "site_url": site_url}
            subject = _subject(digest)
            text_body = render_to_string("crm/emails/weekly_digest.txt", ctx)
            html_body = render_to_string("crm/emails/weekly_digest.html", ctx)

            self.stdout.write(
                f"{tag}+ {user.email}: {len(digest['closing'])} closing, "
                f"{len(digest['actions'])} to ping, {len(digest['picks'])} new picks")
            sent += 1
            if dry:
                continue

            message = EmailMultiAlternatives(subject=subject, body=text_body, to=[user.email])
            message.attach_alternative(html_body, "text/html")
            message.send()

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{sent} digest(s) {'rendered' if dry else 'sent'} "
            f"· {skipped} skipped (nothing to report)"))
