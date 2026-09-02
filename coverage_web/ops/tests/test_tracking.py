"""track_job_run and JobHeartbeat: the two shapes a job reports itself in.
The first is one row per RUN, which every render.yaml cron wraps its work
in; the second is one row per WORKER PROCESS, bumped per tick, for
`gmail_poll --interval`. Nothing here exercises a real management command
end to end — that's each command's own test module's job (e.g.
capture/tests/test_gmail_backfill.py, capture/tests/test_gmail_poll.py) —
this only proves the wrappers themselves record what actually happened.
"""

from __future__ import annotations

import time

import pytest
import requests
from django.utils import timezone

from ops.models import JobRun
from ops.tracking import JobHeartbeat, track_job_run

pytestmark = pytest.mark.django_db


def test_a_clean_run_records_start_finish_and_success():
    with track_job_run("scrape") as run:
        pass

    run.refresh_from_db()
    assert run.name == "scrape"
    assert run.status == JobRun.STATUS_SUCCESS
    assert run.started_at is not None
    assert run.finished_at is not None
    assert run.finished_at >= run.started_at


def test_a_run_is_visible_as_running_while_the_body_executes():
    """A JobRun exists from the moment the command starts, not only once it
    finishes — the health view (and a human staring at admin/) should be
    able to tell a hung job from one that never started."""
    with track_job_run("scrape"):
        mid_run = JobRun.objects.get(name="scrape")
        assert mid_run.status == JobRun.STATUS_RUNNING
        assert mid_run.finished_at is None


def test_a_raised_exception_is_recorded_as_failed_and_still_propagates():
    with pytest.raises(ValueError, match="boom"):
        with track_job_run("gmail-backfill"):
            raise ValueError("boom")

    run = JobRun.objects.get(name="gmail-backfill")
    assert run.status == JobRun.STATUS_FAILED
    assert run.finished_at is not None


def test_each_call_is_its_own_row_not_a_shared_singleton():
    """Render's cron re-invokes the command fresh every schedule tick —
    each tick must get its own JobRun, so a string of successes and one
    failure is legible in the history rather than overwriting itself."""
    with track_job_run("pro-trial-expire"):
        pass
    with pytest.raises(RuntimeError):
        with track_job_run("pro-trial-expire"):
            raise RuntimeError
    with track_job_run("pro-trial-expire"):
        pass

    runs = list(JobRun.objects.filter(name="pro-trial-expire").order_by("started_at"))
    assert [r.status for r in runs] == [
        JobRun.STATUS_SUCCESS, JobRun.STATUS_FAILED, JobRun.STATUS_SUCCESS,
    ]


# ---------------------------------------------------------------------------
# healthchecks.io pings — fired from the same wrapper (see the module
# docstring). `settings.HEALTHCHECK_URLS` is blank for every job by default
# (settings/base.py), which every test above already relies on implicitly:
# none of them mock `requests.get`, so if a ping were firing unconfigured it
# would either raise (a real HTTP call in a test) or hang — the fact they
# pass is itself proof this feature is off by default.
# ---------------------------------------------------------------------------
def test_a_successful_run_pings_its_configured_healthcheck_url(settings, monkeypatch):
    settings.HEALTHCHECK_URLS = {"scrape": "https://hc-ping.com/fake-scrape-id"}
    calls = []
    monkeypatch.setattr(
        "ops.tracking.requests.get",
        lambda url, timeout: calls.append((url, timeout)),
    )

    with track_job_run("scrape"):
        pass

    assert calls == [("https://hc-ping.com/fake-scrape-id", 5)]


def test_a_job_with_no_configured_url_pings_nothing(settings, monkeypatch):
    settings.HEALTHCHECK_URLS = {"scrape": ""}
    calls = []
    monkeypatch.setattr("ops.tracking.requests.get", lambda *a, **kw: calls.append((a, kw)))

    with track_job_run("scrape"):
        pass

    assert calls == []


def test_a_failed_run_does_not_ping_the_healthcheck(settings, monkeypatch):
    """Success-only, deliberately — see _ping_healthcheck's docstring. A
    ping here is only meaningful if it means the job actually succeeded."""
    settings.HEALTHCHECK_URLS = {"scrape": "https://hc-ping.com/fake-scrape-id"}
    calls = []
    monkeypatch.setattr("ops.tracking.requests.get", lambda *a, **kw: calls.append((a, kw)))

    with pytest.raises(ValueError):
        with track_job_run("scrape"):
            raise ValueError("boom")

    assert calls == []


def test_a_ping_failure_does_not_fail_the_job_or_raise(settings, monkeypatch):
    """healthchecks.io being unreachable is not this job's problem — the
    JobRun must still read `success`, and the exception from `requests`
    must not escape `track_job_run` at all."""
    settings.HEALTHCHECK_URLS = {"scrape": "https://hc-ping.com/fake-scrape-id"}

    def _raise(*a, **kw):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr("ops.tracking.requests.get", _raise)

    with track_job_run("scrape") as run:
        pass

    run.refresh_from_db()
    assert run.status == JobRun.STATUS_SUCCESS


# ---------------------------------------------------------------------------
# JobHeartbeat — the long-running-worker shape. `track_job_run` writes one
# row per RUN, which for `gmail_poll --interval 120` means one row for the
# life of the deploy: a `running` row that never becomes a success, so
# /ops/health/cron/ reports the worker dead forever. Wrapping each tick
# instead would be 720 rows a day. Hence: one row, bumped.
# ---------------------------------------------------------------------------

def test_the_first_beat_writes_one_successful_row():
    beat = JobHeartbeat("gmail-poll").beat()

    beat.refresh_from_db()
    assert beat.name == "gmail-poll"
    assert beat.status == JobRun.STATUS_SUCCESS
    assert beat.finished_at is not None


def test_repeated_beats_reuse_the_same_row_and_move_finished_at():
    heartbeat = JobHeartbeat("gmail-poll")
    first = heartbeat.beat()
    first_finished = first.finished_at

    time.sleep(0.01)
    second = heartbeat.beat()

    assert second.pk == first.pk
    assert JobRun.objects.filter(name="gmail-poll").count() == 1
    second.refresh_from_db()
    assert second.finished_at > first_finished


def test_the_health_view_query_reads_a_bumped_row_as_fresh():
    """The row is only useful if the query /ops/health/cron/ actually runs
    finds it: "most recent SUCCESS for this name, by finished_at"."""
    heartbeat = JobHeartbeat("gmail-poll")
    heartbeat.beat()
    time.sleep(0.01)
    heartbeat.beat()

    latest = (
        JobRun.objects.filter(name="gmail-poll", status=JobRun.STATUS_SUCCESS)
        .order_by("-finished_at").first()
    )
    assert latest is not None
    assert (timezone.now() - latest.finished_at).total_seconds() < 5


def test_started_at_stays_pinned_to_when_the_worker_booted():
    """The row answers two questions: "did it tick recently" (finished_at)
    and "how long has this process been up" (started_at). Bumping both would
    lose the second."""
    heartbeat = JobHeartbeat("gmail-poll")
    first = heartbeat.beat()
    booted = first.started_at

    time.sleep(0.01)
    heartbeat.beat()

    first.refresh_from_db()
    assert first.started_at == booted


def test_a_failed_tick_gets_its_own_row_and_does_not_touch_the_heartbeat():
    """Flipping the heartbeat to `failed` would take the job's only success
    row out of the health view's reach, reporting a live worker as
    `never_run` over one bad database second."""
    heartbeat = JobHeartbeat("gmail-poll")
    alive = heartbeat.beat()

    failure = heartbeat.failed()

    assert failure.pk != alive.pk
    assert failure.status == JobRun.STATUS_FAILED
    alive.refresh_from_db()
    assert alive.status == JobRun.STATUS_SUCCESS


def test_a_beat_whose_row_was_deleted_underneath_it_starts_a_new_one():
    """A worker outlives any row-pruning a human does in admin. The bump is a
    queryset UPDATE precisely so the row count is visible, rather than a
    `save(update_fields=...)` that raises on a row that is gone."""
    heartbeat = JobHeartbeat("gmail-poll")
    first = heartbeat.beat()
    JobRun.objects.filter(pk=first.pk).delete()

    second = heartbeat.beat()

    assert second.pk != first.pk
    assert second.status == JobRun.STATUS_SUCCESS


def test_a_beat_pings_the_jobs_healthcheck(settings, monkeypatch):
    """Same wrapper posture as `track_job_run`: one place records the run and
    pings the check, so a job cannot do one without the other."""
    settings.HEALTHCHECK_URLS = {"gmail-poll": "https://hc-ping.com/fake-poll-id"}
    calls = []
    monkeypatch.setattr("ops.tracking.requests.get", lambda url, **kw: calls.append(url))

    JobHeartbeat("gmail-poll").beat()

    assert calls == ["https://hc-ping.com/fake-poll-id"]
