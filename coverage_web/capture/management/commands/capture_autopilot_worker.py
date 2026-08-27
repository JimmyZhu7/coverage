"""capture_autopilot_worker — run the decide passes students have queued.

    python manage.py capture_autopilot_worker
    python manage.py capture_autopilot_worker --email you@example.com
    python manage.py capture_autopilot_worker --once   # one run, then stop

THE STEP THIS IS. `capture.autopilot.start_run` (behind the "Run Autopilot"
button on Today) writes a QUEUED `AutopilotRun` and returns immediately —
52 sequential model calls is minutes of work and no POST may wait on it,
the same reasoning `capture.views.gmail_rescan` gives for the "Scan Now"
queue. This command is the other half: it claims queued runs and decides
them. Run it on a short tick; most ticks find nothing.

    */5 * * * *  manage.py capture_autopilot_worker

WHAT IT NEVER DOES: apply anything. Deciding writes verdicts only, and the
user's own tap on Today ("Add all N") is still the single thing that
touches the CRM — see `capture/autopilot.py`'s module docstring for why
that separation is the Limited Use posture rather than a UX choice.

RE-ENTRANCY. Two ticks overlapping (a slow run, a five-minute schedule)
cannot both take the same row: `claim_run` is a compare-and-set UPDATE on
`status='queued'` and exactly one racer wins. Two runs for the same USER
cannot exist at all — `uniq_autopilot_active` refuses the second at the
database, before the button's response is even written.

STALE RECLAIM. A worker killed mid-decide (OOM, redeploy, SIGKILL) leaves
its row at `running`, which would otherwise both block that student's next
run forever and render on Today as a run still thinking. Every tick reaps
rows older than `autopilot.STALE_RUN_AFTER` into `failed`, with the reason
on the row — the same three-way pending/failed/stale-running selection
`gmail_backfill` uses, for the same reason.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from capture import autopilot
from capture.models import AutopilotRun
from ops.tracking import track_job_run


class Command(BaseCommand):
    help = "Decide any queued Autopilot runs (the Today button's worker)."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Only this user's queued runs.")
        parser.add_argument(
            "--once", action="store_true",
            help="Take at most one run this tick.",
        )

    def handle(self, *args, **opts):
        # Named for /ops/health/cron/ the way every other scheduled command
        # is (ops/tracking.py). Wraps the whole tick including the "nothing
        # queued" no-op: the health check asks whether this cron is ticking
        # at all, not whether it found work.
        with track_job_run("autopilot"):
            self._tick(opts)

    def _tick(self, opts) -> None:
        reaped = autopilot.reap_stale_runs()
        if reaped:
            self.stderr.write(
                f"Reclaimed {reaped} abandoned run(s) — marked failed."
            )

        queued = AutopilotRun.all_objects.filter(
            status=AutopilotRun.STATUS_QUEUED
        ).select_related("user").order_by("created")
        if opts.get("email"):
            User = get_user_model()
            if not User.objects.filter(email=opts["email"]).exists():
                raise CommandError(f"No user with email {opts['email']!r}.")
            queued = queued.filter(user__email=opts["email"])
        runs = list(queued[:1] if opts.get("once") else queued)

        if not runs:
            self.stdout.write("Nothing queued.")
            return

        for run in runs:
            if not autopilot.claim_run(run):
                # Another worker took it between the SELECT and here. Its
                # run, not ours — say so and move on.
                self.stdout.write(f"Run #{run.pk}: claimed by another worker.")
                continue
            report = autopilot.execute_run(run)
            run.refresh_from_db()
            if run.status == AutopilotRun.STATUS_FAILED:
                self.stderr.write(
                    f"Run #{run.pk} ({run.user.email}): FAILED — "
                    f"{run.failure_reason}"
                )
                continue
            self.stdout.write(
                f"Run #{run.pk} ({run.user.email}): {run.accepts} ready to "
                f"add, {run.escalations} left for the student, "
                f"{run.skips} skipped, {run.deferred} deferred — "
                f"{report.llm_calls} model call(s), "
                f"{run.credits_spent} credit(s)."
            )
            if run.evidence_note:
                self.stdout.write(f"  evidence: {run.evidence_note}")
