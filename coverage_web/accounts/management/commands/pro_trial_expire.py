"""pro_trial_expire — reverts an expired Pro trial's plan back to Free.

    python manage.py pro_trial_expire

Run daily (render.yaml's coverage-pro-trial-expire cron). Selects every
account whose `pro_trial_ends_at` has passed and is still on `plan="pro"`,
flips `plan` back to "free", and deliberately leaves everything else alone:

- `pro_trial_started_at` / `pro_trial_ends_at` are NOT cleared — see
  accounts/models.py's own comment on why they stay set (so Settings can
  keep saying "your trial ended" honestly, and so a lapsed trial can never
  be restarted by `accounts.trials.start_trial_if_eligible`).
- Gmail is NOT disconnected, and nothing here talks to Google at all. The
  trial's only real-time gate is `capture.gmail_live`'s own plan check
  (`connect_gmail` / `renew_watches`) — the moment `plan` flips back to
  "free" here, the next `gmail_watch_renew` tick simply stops re-registering
  that connection's watch, and Google's own 7-day expiry finishes the job.
  The connection, its history, and Scan Now all keep working exactly as
  they do for any other Free account.
- Credits are NOT touched here. `billing.credits` is the one writer of
  `CreditLedger` rows, and it lazily grants from whatever `plan_config(user)`
  says for the account's CURRENT plan on the next balance check — flipping
  `plan` is the entire signal it needs; duplicating grant logic in this app
  would be exactly the "second app owns credits" drift that module's own
  docstring warns against.

`pro_trial_ends_at__isnull=False` is what keeps this command from ever
touching an admin-granted Pro account (the founder's own, a beta tester's):
those accounts never went through `start_trial_if_eligible`, so that field
stays null forever, and this command's own selection query leaves them
alone by construction — not a special-cased email check.

A per-row loop, not a bulk `.update()`: at this app's scale ("a handful of
users", per capture/gmail_live.py's own comments) the cost is nothing, and
it is what lets each reversion also fire a `pro_trial_expired` product
event — a bulk update would silently drop that funnel signal.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from analytics.events import record_event
from ops.tracking import track_job_run


class Command(BaseCommand):
    help = "Revert expired Pro trials back to the Free plan."

    def handle(self, *args, **opts):
        # "pro-trial-expire" matches render.yaml's coverage-pro-trial-expire
        # cron — see ops/tracking.py.
        with track_job_run("pro-trial-expire"):
            User = get_user_model()
            expired = User.objects.filter(
                plan=User.PLAN_PRO,
                pro_trial_ends_at__isnull=False,
                pro_trial_ends_at__lte=timezone.now(),
            )
            count = 0
            for user in expired:
                user.plan = User.PLAN_FREE
                user.save(update_fields=["plan"])
                record_event("pro_trial_expired", user=user)
                count += 1
            self.stdout.write(f"reverted: {count}")
