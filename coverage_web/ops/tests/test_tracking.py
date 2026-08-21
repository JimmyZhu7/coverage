"""track_job_run: the one call every render.yaml cron command wraps its work
in. Nothing here exercises a real management command end to end — that's
each command's own test module's job (e.g. capture/tests/test_gmail_backfill.py) —
this only proves the wrapper itself records what actually happened.
"""

from __future__ import annotations

import pytest

from ops.models import JobRun
from ops.tracking import track_job_run

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
