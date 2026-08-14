"""send_deadline_push_alerts — Web Push nudges for tracked roles closing
soon: "a role you're tracking closes soon", sent once a role's deadline
lands exactly N days out (T-7 and T-2 by default, the product's stated
plan).

    python manage.py send_deadline_push_alerts                # T-7 and T-2, real send
    python manage.py send_deadline_push_alerts --days 7        # one window only
    python manage.py send_deadline_push_alerts --dry-run       # render, send nothing
    python manage.py send_deadline_push_alerts --user a@b.com  # one account, real send

WHY THIS SHIPS COMPLETE WITH ZERO ONGOING COST. Web Push needs no paid
service: `accounts.push` signs each request with a VAPID keypair this app
generated for itself (`generate_vapid_keys`), and the push relay itself
(Chrome/Firefox/Safari's own infrastructure) is free and built into the
browser. `accounts.push.is_configured()` is False until real keys land
(VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY blank by default, settings/base.py),
so this command safely no-ops in every environment that hasn't set them up
— nothing crashes, nothing costs money, and turning it on later is a
two-variable env change with no code change.

WHY "EXACTLY N DAYS OUT", NOT THE APP'S "CLOSING SOON" WINDOW.
`directory.deadlines.CLOSING_SOON_DAYS` (10 days) answers "what's worth
showing on a page right now" and is intentionally wide — My Applications and
the weekly digest both reuse it because a page or an email is read once and
should show everything currently relevant. A push alert is different: it
fires once, at a moment, and reusing the 10-day window here would re-alert
the same student on the same role every single day it stayed inside it. So
this command asks a narrower, exact question — `deadline_days_out(uo) in
{7, 2}` — answered by re-deriving from the same source of truth
(`directory.views._tracked_rows` + `_lens_item`'s `days_left`), never a
second copy of the app's deadline arithmetic. See crm/digest.py's module
docstring for the three times this codebase has already shipped the
"second competing formula" bug on data this exact command touches.

TIMEZONE. Same posture as `send_weekly_digest`: every other "today" boundary
in this app activates the user's own `User.timezone` per request
(`accounts.middleware.TimezoneMiddleware`), and a management command has no
request for that middleware to run against. `_local_today` below replicates
its exact activate/fallback/deactivate contract for each user in turn — a
deliberate, small duplication (not a shared helper) because the two
commands' loops are independently owned and a shared import would couple
them for no behavioral gain.

STALE SUBSCRIPTIONS. A push service returning 404/410 means the browser
unsubscribed, was uninstalled, or the endpoint rotated past recovery — this
is documented Web Push behavior, not a transient error, so the subscription
row is deleted on the spot rather than retried on the next run.

NO PER-ALERT DEDUPE, ON PURPOSE (documented gap, not an oversight). This
command doesn't record "already alerted this user about this role at T-7" —
running it twice on the same day for the same `--days` value re-sends. In
production it is meant to run once a day (see render.yaml's cron entry),
where a role's day-count only ever equals a given N on one calendar day, so
in normal operation this never double-fires. A real send-log is a natural
follow-up if the cron ever needs to run more than once daily; not built
here.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts.models import PushSubscription
from accounts.push import SubscriptionExpired, is_configured, send_notification
from directory.views import TRACK_CLOSED, _lens_item, _tracked_rows

#: T-7 and T-2, per the product plan (see this module's docstring).
DEFAULT_DAYS = (7, 2)

#: Where a tap on the notification lands — the same page My Applications'
#: Closing Soon lens renders, so the alert and the page it opens agree.
CLICK_URL = "/opportunities/mine/"


def _local_today(user) -> date:
    """This user's `timezone.localdate()`, activated exactly the way
    `accounts.middleware.TimezoneMiddleware` would for a request — see this
    module's docstring for why a management command replicates rather than
    reuses that middleware. Caller is responsible for `timezone.deactivate()`
    afterward (this loop runs many users on one thread/process)."""
    name = (getattr(user, "timezone", "") or "").strip()
    if name:
        try:
            timezone.activate(ZoneInfo(name))
        except (ZoneInfoNotFoundError, ValueError):
            timezone.deactivate()
    else:
        timezone.deactivate()
    return timezone.localdate()


def _due_rows(user, *, today: date, days: tuple[int, ...]) -> list[dict]:
    """Tracked, still-live roles whose deadline is exactly N days out for
    some N in `days`. See this module's docstring for why "exactly N", not
    the app's wider Closing Soon window.

    Reuses `directory.views._tracked_rows` (the exact fold/dedup My
    Applications and the weekly digest both read) and `_lens_item` for the
    per-row shape (`days_left`, `firm_name`, `title`) rather than re-deriving
    either."""
    rows = _tracked_rows(user)
    live = [uo for uo in rows if (uo.applied_status or "saved") != TRACK_CLOSED]
    items = [_lens_item(uo, today=today) for uo in live]
    return [i for i in items if i["days_left"] in days]


def _notification_text(item: dict) -> tuple[str, str]:
    """(title, body) for one due role. No em dash (house copy style)."""
    n = item["days_left"]
    when = "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"
    title = f"{item['firm_name']} closes {when}"
    body = f"{item['title']} closes {when}. Tap to review your applications."
    return title, body


class Command(BaseCommand):
    help = "Send a Web Push alert for tracked roles closing in N days (T-7 and T-2 by default)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, action="append", default=None,
            help="Days-out to alert on. Repeatable (--days 7 --days 2). "
                 "Defaults to 7 and 2.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be sent. Sends nothing.")
        parser.add_argument(
            "--user", metavar="EMAIL", default="",
            help="Restrict to one account by email, bypassing the "
                 "has-a-subscription gate — for previewing or testing.")

    def handle(self, *args, **opts):
        if not is_configured():
            self.stdout.write(self.style.WARNING(
                "VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY are unset — push alerts "
                "are disabled. Run `manage.py generate_vapid_keys` and set "
                "them to activate this command."))
            return

        days = tuple(opts["days"]) if opts["days"] else DEFAULT_DAYS
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
                User.objects.filter(deleted_at__isnull=True, pushsubscription__isnull=False)
                .distinct().order_by("email")
            )

        if not users:
            self.stdout.write("No users with an active push subscription.")
            return

        sent = skipped = expired = 0
        for user in users:
            try:
                today = _local_today(user)
                due = _due_rows(user, today=today, days=days)
            finally:
                timezone.deactivate()

            if not due:
                skipped += 1
                self.stdout.write(f"{tag}- {user.email}: nothing due, skipped")
                continue

            subs = list(PushSubscription.objects.for_user(user))
            dead_ids: set[int] = set()
            for item in due:
                title, body = _notification_text(item)
                live_subs = [s for s in subs if s.id not in dead_ids]
                self.stdout.write(
                    f"{tag}+ {user.email}: {item['firm_name']} / {item['title']} "
                    f"(T-{item['days_left']}) -> {len(live_subs)} subscription(s)")
                sent += 1
                if dry:
                    continue
                for sub in live_subs:
                    try:
                        send_notification(sub, title=title, body=body, url=CLICK_URL)
                    except SubscriptionExpired:
                        expired += 1
                        dead_ids.add(sub.id)
                        sub.delete()
                    except Exception as exc:  # noqa: BLE001 — one bad send must not stop the run
                        self.stderr.write(f"  ! {user.email} push failed: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{sent} alert(s) {'rendered' if dry else 'sent'} "
            f"· {skipped} skipped (nothing due) "
            f"· {expired} expired subscription(s) removed"))
