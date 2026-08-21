"""`track_job_run`: the one call site every render.yaml cron command wraps
its actual work in, so /ops/health/cron/ has something real to read.

Also the one call site that pings healthchecks.io on a successful run (see
`_ping_healthcheck` below) — deliberately the SAME wrapper rather than a
second decorator each command would need to add separately, so a command
that records a JobRun always also pings its check, and there is exactly one
place to look for either mechanism.

Otherwise kept deliberately dumb — a context manager writing two rows'
worth of timestamps plus one best-effort HTTP GET, nothing else — because
the six commands it wraps (gmail_backfill, gmail_watch_renew, refresh,
send_deadline_push_alerts, send_weekly_digest, pro_trial_expire) already
have their own retry/failure handling for the WORK itself; this only
answers "did the job run, and when."
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

from .models import JobRun

logger = logging.getLogger(__name__)

# render.yaml's cron `schedule:` for each job, minus the "coverage-" service
# name prefix. The health view flags a job whose last SUCCESS is older than
# this — see /ops/health/cron/ in views.py. Hardcoded rather than read from
# render.yaml itself: this app has no dependency on that file's format, and
# a schedule change is already a deploy-config edit, so updating this dict
# alongside it is one more line in the same review, not a separate source of
# truth silently drifting from a file this code doesn't parse.
EXPECTED_INTERVALS: dict[str, timedelta] = {
    "gmail-backfill": timedelta(minutes=15),
    "gmail-watch-renew": timedelta(days=1),
    "scrape": timedelta(hours=6),
    "push-alerts": timedelta(days=1),
    "weekly-digest": timedelta(days=7),
    "pro-trial-expire": timedelta(days=1),
}


def _ping_healthcheck(name: str) -> None:
    """Best-effort GET of `settings.HEALTHCHECK_URLS[name]` on a successful
    run. Never raises: a healthchecks.io outage, DNS blip, or bad URL must
    not turn a real, successful cron run into a `failed` JobRun over a
    third-party ping that was never the job. Silently a no-op when the URL
    isn't configured for this job name — see settings/base.py's
    HEALTHCHECK_URL_* comment for why that's the default.

    Success only, deliberately: healthchecks.io also supports an explicit
    `<url>/fail` ping, but wiring that up is a separate decision (does a
    `failed` JobRun always mean "page someone", or does that need its own
    tuning per job?) left for a follow-up rather than assumed here.
    """
    url = getattr(settings, "HEALTHCHECK_URLS", {}).get(name, "")
    if not url:
        return
    try:
        requests.get(url, timeout=5)
    except requests.RequestException:
        logger.exception("healthcheck ping failed for job %r", name)


@contextmanager
def track_job_run(name: str):
    """Wrap a management command's `handle()` body in this:

        def handle(self, *args, **opts):
            with track_job_run("gmail-backfill"):
                ...the command's actual work...

    Creates a `JobRun` row up front (status=running), then flips it to
    `success` on a clean exit or `failed` on any exception — re-raised
    either way, unchanged, so a command's own exit code (and Render's
    cron-failure notification, per render.yaml's coverage-scrape comment)
    still reflects the real outcome. This never swallows or masks a
    failure; it only records one. On success, also pings the job's
    healthchecks.io check (`_ping_healthcheck`) — see the module docstring
    for why that lives here rather than as a second wrapper.
    """
    run = JobRun.objects.create(name=name, started_at=timezone.now(), status=JobRun.STATUS_RUNNING)
    try:
        yield run
    except BaseException:
        run.status = JobRun.STATUS_FAILED
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
        raise
    else:
        run.status = JobRun.STATUS_SUCCESS
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
        _ping_healthcheck(name)
