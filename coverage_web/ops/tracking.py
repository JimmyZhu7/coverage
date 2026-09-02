"""`track_job_run`: the one call site every render.yaml cron command wraps
its actual work in, so /ops/health/cron/ has something real to read.

Also the one call site that pings healthchecks.io on a successful run (see
`_ping_healthcheck` below) — deliberately the SAME wrapper rather than a
second decorator each command would need to add separately, so a command
that records a JobRun always also pings its check, and there is exactly one
place to look for either mechanism.

`JobHeartbeat` is the same idea for a LONG-RUNNING worker, where "one row
per run" would mean one row for the life of the deploy: it keeps a single
row and bumps its `finished_at` on every tick. See its own docstring.

Otherwise kept deliberately dumb — a context manager writing two rows'
worth of timestamps plus one best-effort HTTP GET, nothing else — because
the commands it wraps (gmail_backfill, gmail_watch_renew, refresh,
send_deadline_push_alerts, send_weekly_digest, pro_trial_expire,
capture_autopilot_worker, gmail_poll) already have their own retry/failure
handling for the WORK itself; this only answers "did the job run, and when."
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
    # render.yaml's cron is */5, not the */15 this said until the tick
    # budget (gmail_backfill.py's TICK_BUDGET) made a shorter interval safe.
    "gmail-backfill": timedelta(minutes=5),
    # The Today button's worker (coverage-autopilot, */5). Five minutes,
    # not fifteen, because a student is watching this one — a stalled tick
    # here is a run that never starts, and the strip says "within a few
    # minutes".
    "autopilot": timedelta(minutes=5),
    # gmail_poll is a long-running worker (render.yaml's coverage-gmail-live),
    # not a cron — in loop mode it bumps a single `JobHeartbeat` row's
    # `finished_at` once per 120s tick (DEFAULT_INTERVAL in gmail_poll.py)
    # rather than writing a row per tick; see that class below. Ten minutes
    # is five missed ticks' worth of slack: enough that an ordinary GC pause
    # or a slow Gmail API call never trips this, not so much that a genuinely
    # dead worker sits unflagged for the length of the old --interval-free
    # gap this dict used to leave for it entirely.
    "gmail-poll": timedelta(minutes=10),
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


class JobHeartbeat:
    """The long-running-worker counterpart to `track_job_run` below.

    WHY A SECOND SHAPE AT ALL. `track_job_run` writes one row per run, which
    is exactly right for a cron: a run is a discrete thing with a start, an
    end and an outcome. A worker has none of that — `gmail_poll --interval
    120` is ONE run that lasts until the next deploy, ticking 720 times a
    day. Wrapping each tick in `track_job_run` would write 720 rows a day of
    pure noise (the reason gmail_poll's own docstring refused to), and
    wrapping the whole process in one would record a `running` row that
    never becomes a success, so /ops/health/cron/ would report the poller
    dead forever — which is precisely what it did.

    So: ONE row per worker process, its `finished_at` bumped on every tick.
    The health view's query is "the most recent SUCCESS row for this name,
    ordered by finished_at" (ops/views.py), which reads a moving
    `finished_at` exactly the way it reads a fresh row — no change needed
    there, and `EXPECTED_INTERVALS["gmail-poll"]` keeps meaning what it says.
    `started_at` stays pinned to when the process booted, so the row also
    answers "how long has this worker been up".

    A FAILED TICK GETS ITS OWN ROW, and the heartbeat row is left alone.
    Flipping the heartbeat to `failed` would take the job's only success row
    out of the health view's reach and report a live worker as `never_run`
    over one bad Postgres second. Failures are rare and individually
    interesting, so they are worth a row each — the same reasoning
    gmail_poll.py already applies to its `gmail_poll_error` Import rows.
    """

    def __init__(self, name: str):
        self.name = name
        self.run: JobRun | None = None

    def beat(self) -> JobRun:
        """Record that one tick just finished cleanly. Creates the row on
        the first call, bumps `finished_at` on every call after."""
        now = timezone.now()
        if self.run is not None:
            # A queryset UPDATE rather than `instance.save(update_fields=...)`
            # so the row count is visible: a worker outlives any row-pruning
            # a human does in admin, and `save()` on a row that no longer
            # exists raises rather than telling us to start a new one.
            if JobRun.objects.filter(pk=self.run.pk).update(finished_at=now):
                self.run.finished_at = now
                _ping_healthcheck(self.name)
                return self.run
            self.run = None
        self.run = JobRun.objects.create(
            name=self.name, started_at=now, finished_at=now,
            status=JobRun.STATUS_SUCCESS,
        )
        _ping_healthcheck(self.name)
        return self.run

    def failed(self) -> JobRun:
        """Record one failed tick as its own row — see the class docstring
        on why this never touches the heartbeat row."""
        now = timezone.now()
        return JobRun.objects.create(
            name=self.name, started_at=now, finished_at=now,
            status=JobRun.STATUS_FAILED,
        )


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
