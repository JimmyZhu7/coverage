"""pro_trial_expire — reverts an expired Pro trial's plan back to Free.

    python manage.py pro_trial_expire

Run daily (render.yaml's coverage-pro-trial-expire cron). Selects every
account whose `pro_trial_ends_at` has passed and is still on `plan="pro"`,
flips `plan` back to "free", TELLS THE STUDENT, unlocks their Free "Scan
Now", and deliberately leaves everything else alone.

RUNS BEFORE coverage-gmail-watch-renew, NOT AFTER. These two crons were
30 minutes apart in the wrong order — renew at 05:00, expire at 05:30 — so
a trial that ended overnight got one last 7-day watch renewal half an hour
before the plan flip that was supposed to stop it. Harmless with a pull
subscription (nobody is listening), but it is the wrong shape: the job that
decides who is Pro has to land before the job that acts on who is Pro. The
schedules are now expire 05:00, renew 05:30.

- `pro_trial_started_at` / `pro_trial_ends_at` are NOT cleared — see
  accounts/models.py's own comment on why they stay set (so Settings can
  keep saying "your trial ended" honestly, and so a lapsed trial can never
  be restarted by `accounts.trials.start_trial_if_eligible`).
- THE STUDENT IS TOLD, twice over: `accounts.trials.trial_ended_notice`
  puts a dismissable line on Settings naming the date and what changed
  (real-time Gmail sync paused, credits kept), and
  `accounts.trials.send_trial_ended_email` mails the same thing on a deploy
  that has a real relay configured. Both were promised in comments and
  neither existed; the plan simply flipped and the student found out by
  noticing their mail had stopped logging itself. The email no-ops on a
  deploy with no `EMAIL_URL` rather than printing a notice into a log and
  calling it delivered, so this command behaves identically with zero paid
  setup — the same posture `send_weekly_digest` documents.
- THE FREE "SCAN NOW" IS UNLOCKED (`accounts.trials.
  reset_free_rescan_throttle`). A trialist who scanned on day 13, while Pro
  and therefore unthrottled, was locked out of that button until day 20 —
  and it is the first thing they press once real-time stops. See that
  function's docstring for why the reset lives here rather than in the
  throttle.
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

from accounts import trials as pro_trials
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
            mailed = 0
            unlocked = 0
            for user in expired:
                user.plan = User.PLAN_FREE
                user.save(update_fields=["plan"])
                # Order matters only in one direction: the plan flip is the
                # real work and commits first, so a failure in either of the
                # two lines below leaves an account correctly on Free with
                # its notice still pending, never a Pro account that has
                # already been told it is not one.
                if pro_trials.reset_free_rescan_throttle(user):
                    unlocked += 1
                if pro_trials.send_trial_ended_email(user):
                    mailed += 1
                record_event("pro_trial_expired", user=user)
                count += 1
            self.stdout.write(
                f"reverted: {count} · scan-now unlocked: {unlocked} · emailed: "
                f"{mailed}"
                + ("" if pro_trials.email_is_configured()
                   else " (no EMAIL_URL on this deploy: the Settings banner "
                        "is the whole notice)")
            )
