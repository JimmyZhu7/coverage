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

import math

from django.conf import settings
from django.utils import timezone

from analytics.events import record_event


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
