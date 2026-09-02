"""Pro trial mechanics — settings.PRO_TRIAL_DAYS / PRO_TRIAL_TRIGGER.

The founder's own read on the single highest-leverage conversion fix: "let
them watch three replies log themselves, then charge." This module is the
MECHANICS of that, deliberately narrow:

- `start_trial_if_eligible` flips a Free account to Pro plus a trial-end
  timestamp, the moment its one call site (today: `capture.gmail_live.
  connect_gmail`'s success path) fires for the configured trigger.
- `trial_days_left` reads that state back for Settings' Credits and Gmail
  Live cards ("Pro trial · N days left").
- The `pro_trial_expire` management command (accounts/management/commands/
  pro_trial_expire.py) reverts an expired trial's `plan` back to "free".
- `trial_ended_notice` / `send_trial_ended_email` /
  `reset_free_rescan_throttle` are the other end of that: TELLING the
  student the trial ended, and unlocking the one button they will press
  first afterwards.

THE END OF A TRIAL USED TO BE SILENT. `accounts/models.py`'s own comment on
`pro_trial_ends_at` and `pro_trial_expire`'s docstring both said the
timestamps stay set "so Settings can keep saying 'your trial ended'
honestly" — and no template said it. The plan pill flipped from Pro to
Free, real-time sync stopped, and nothing anywhere named the day or the
change. `trial_ended_notice` below is that missing sentence, and
`send_trial_ended_email` is the same sentence by mail on a deploy that has
a relay configured (it stays silent on one that does not, rather than
printing a student's trial notice into a log nobody reads and calling it
sent).

WHAT THIS MODULE DELIBERATELY DOES NOT DO: grant, adjust, or even look at
credits. `billing.credits` owns the ledger and the plan-driven grant math
(CreditLedger, ensure_monthly_grant, plan_config) — this module only ever
writes three columns on `User` (`plan`, `pro_trial_started_at`,
`pro_trial_ends_at`). The moment `start_trial_if_eligible` commits
`plan="pro"`, the next credits touch (a chat message, a Settings render)
reads Pro's grant honestly through `billing.credits.plan_config(user)`,
which already keys off `user.plan` — no separate reconcile call needed here,
and none added, on purpose: duplicating that logic in two apps is exactly
the kind of drift `billing.credits`'s own docstring warns against ("Neither
app owns credits — this app does").
"""

from __future__ import annotations

import logging
import math

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from analytics.events import record_event

logger = logging.getLogger(__name__)

#: Email backends that do not put mail in front of a human. `production.py`
#: defaults `EMAIL_URL` to `consolemail://`, which prints the message to the
#: service logs — the right default for a deploy with no relay bought yet
#: (a password-reset link is still recoverable from the logs by the founder),
#: and the wrong thing to count as "the student was told". A trial-end notice
#: printed to Render's log stream has not reached anyone, so
#: `email_is_configured()` reads False there and the Settings banner carries
#: the message alone. See the deferred-paid-setup note in docs/deploy.md.
_UNCONFIGURED_EMAIL_BACKENDS = (
    "django.core.mail.backends.console.EmailBackend",
    "django.core.mail.backends.dummy.EmailBackend",
)


def email_is_configured() -> bool:
    """Whether outbound mail actually leaves this deploy. Named and shaped
    like every other integration's off-switch in this codebase
    (`gmail_live.is_configured`, `stripe_gateway.is_configured`,
    `push.is_configured`) so the "no paid setup, no crash, no lie" posture
    reads the same everywhere."""
    backend = (getattr(settings, "EMAIL_BACKEND", "") or "").strip()
    return bool(backend) and backend not in _UNCONFIGURED_EMAIL_BACKENDS


def start_trial_if_eligible(user, *, trigger: str) -> bool:
    """Flip `user` from Free to a Pro trial when `trigger` matches
    `settings.PRO_TRIAL_TRIGGER` and this account has never had a trial.

    Returns True if a trial was started, False otherwise — wrong trigger,
    already Pro (whether from an earlier trial or an admin grant), or
    `pro_trial_started_at` already set (a past trial, active or expired).
    That last check is the entire "never start a second trial" rule; it
    intentionally does not look at `pro_trial_ends_at` at all, so a trial
    that a student let lapse can't be restarted by reconnecting Gmail again.
    """
    if trigger != settings.PRO_TRIAL_TRIGGER:
        return False
    if user.plan == user.PLAN_PRO:
        return False
    if user.pro_trial_started_at is not None:
        return False

    now = timezone.now()
    user.plan = user.PLAN_PRO
    user.pro_trial_started_at = now
    user.pro_trial_ends_at = now + timezone.timedelta(days=settings.PRO_TRIAL_DAYS)
    user.save(update_fields=["plan", "pro_trial_started_at", "pro_trial_ends_at"])
    record_event(
        "pro_trial_started", user=user, trigger=trigger, days=settings.PRO_TRIAL_DAYS
    )
    return True


def trial_days_left(user) -> int | None:
    """Whole days left in an ACTIVE trial, or None when there is no active
    trial to report (never started, or already expired/reverted). Settings'
    Credits and Gmail Live cards both read this for the "Pro trial · N days
    left" line — see accounts/views.py's `_credits_context` and
    `_gmail_live_context`.

    Rounds UP: 6 days and 23 hours left should read "7 days left" on the day
    the trial started, not "6" — flooring would undercount from the first
    hour.
    """
    ends_at = getattr(user, "pro_trial_ends_at", None)
    if not ends_at:
        return None
    remaining = ends_at - timezone.now()
    if remaining.total_seconds() <= 0:
        return None
    return max(1, math.ceil(remaining.total_seconds() / 86400))


# ---------------------------------------------------------------------------
# The end of a trial, said out loud — see the module docstring.
# ---------------------------------------------------------------------------
def trial_ended_notice(user) -> dict | None:
    """The Settings banner's context, or `None` when there is nothing to
    say. Read by `accounts.views._credits_context`.

    Shown when all four hold: the account is back on Free, a trial really
    ran (`pro_trial_started_at` set), its end date has passed, and the
    student has not dismissed the notice. Dismissal is a stored timestamp
    rather than a session flag or `localStorage`, because the thing being
    acknowledged is a change to what the account can do — it should not
    reappear on their laptop after they closed it on their phone.

    Never shown to an admin-granted Pro account: those have
    `pro_trial_ends_at` null forever (accounts/models.py), the same
    distinction `pro_trial_expire`'s selection query relies on.
    """
    if getattr(user, "plan", "") != getattr(user, "PLAN_FREE", "free"):
        return None
    started_at = getattr(user, "pro_trial_started_at", None)
    ends_at = getattr(user, "pro_trial_ends_at", None)
    if not started_at or not ends_at:
        return None
    if ends_at > timezone.now():
        return None
    if getattr(user, "pro_trial_notice_dismissed_at", None):
        return None
    return {"ended_at": ends_at}


def dismiss_trial_ended_notice(user) -> bool:
    """Stamp the notice as acknowledged. Returns whether anything changed,
    so a double-submit (two tabs, a retried POST) is a no-op rather than a
    second write moving the timestamp forward."""
    if getattr(user, "pro_trial_notice_dismissed_at", None):
        return False
    user.pro_trial_notice_dismissed_at = timezone.now()
    user.save(update_fields=["pro_trial_notice_dismissed_at"])
    return True


def send_trial_ended_email(user) -> bool:
    """One plain "your Pro trial ended" mail. Returns whether it was sent.

    A no-op — returning False, raising nothing — when `email_is_configured()`
    is False or the account has no address, so the `pro_trial_expire` cron
    runs identically on a deploy with no relay bought: the Settings banner
    is the whole notice there, which is the founder's stated posture on
    parked paid setup.

    Failures are logged and swallowed. A student's plan reversion must not
    be undone, nor the cron failed, because an SMTP relay had a bad minute:
    the plan flip is the real work, the mail is the receipt, and this
    module's own `pro_trial_expired` event plus the banner both survive a
    lost send.
    """
    address = (getattr(user, "email", "") or "").strip()
    if not address or not email_is_configured():
        return False

    site_url = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    ctx = {
        "user": user,
        "ended_at": user.pro_trial_ends_at,
        "trial_days": settings.PRO_TRIAL_DAYS,
        "site_url": site_url,
        "settings_url": f"{site_url}/welcome/settings/#credits",
    }
    try:
        message = EmailMultiAlternatives(
            subject="Your Coverage Pro trial has ended",
            body=render_to_string("accounts/emails/trial_ended.txt", ctx),
            to=[address],
        )
        message.attach_alternative(
            render_to_string("accounts/emails/trial_ended.html", ctx), "text/html"
        )
        message.send()
    except Exception:  # noqa: BLE001 — see the docstring above.
        logger.exception("trial-ended email failed for user %s", user.pk)
        return False
    return True


def reset_free_rescan_throttle(user) -> bool:
    """Clear this account's "Scan Now" throttle anchor, so the first scan
    they take as a Free user is available immediately. Returns whether
    anything changed.

    THE BUG THIS FIXES. `capture.gmail_live.free_rescan_unlocks_at` keys the
    Free plan's once-per-`GMAIL_FREE_RESCAN_INTERVAL_DAYS` throttle off the
    connection's last scan, whenever it happened. A trialist who used Scan
    Now on day 13 — while Pro, where the throttle does not apply at all —
    was therefore locked out of it until day 20, and that button is the
    first thing an expired trialist presses. The throttle is meant to stop a
    Free account reproducing real-time sync by mashing a button; a scan
    taken under Pro was never one of those, so it should not count against
    a Free allowance the account did not have at the time.

    Clearing the anchor rather than teaching the throttle to compare against
    `pro_trial_ends_at` keeps the change inside the trial's own code: the
    throttle function stays the single shared definition
    `capture.views.gmail_rescan` and `accounts.views._gmail_live_context`
    both read, with no new "was this scan Pro-era" concept in it.

    `rescan_status` goes back to "none" alongside the timestamps, because
    Settings' Rescan row reads "Last scan {{ rescan_completed_at|timesince1 }}
    ago" under `status == "done"` — leaving the status while clearing the
    date it names would render a sentence with a hole in it. The row falls
    back to its plain "Re-check Gmail against all your contacts", which is
    true. `rescan_stats` is left in place: it is what the data export and
    admin read for what that scan actually found.

    An IN-FLIGHT rescan (`pending`/`running`) is left completely alone —
    resetting it would strand a run `gmail_backfill` has already claimed,
    and a scan queued during the trial should still finish.
    """
    # Imported inside the function, not at module level: `capture.gmail_live`
    # imports THIS module (start_trial_if_eligible's call site), so a
    # top-level `capture.models` import here would close a cycle between the
    # two apps for no gain — this is the only function that needs it.
    from capture.models import GmailConnection

    changed = False
    for connection in GmailConnection.all_objects.filter(user=user).exclude(
        rescan_status__in=("pending", "running")
    ):
        if (
            connection.rescan_requested_at is None
            and connection.rescan_completed_at is None
            and connection.rescan_started_at is None
            and connection.rescan_status == "none"
        ):
            continue
        connection.rescan_status = "none"
        connection.rescan_requested_at = None
        connection.rescan_started_at = None
        connection.rescan_completed_at = None
        connection.save(update_fields=[
            "rescan_status", "rescan_requested_at",
            "rescan_started_at", "rescan_completed_at",
        ])
        changed = True
    return changed
