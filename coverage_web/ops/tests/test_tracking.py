"""track_job_run: the one call every render.yaml cron command wraps its work
in. Nothing here exercises a real management command end to end — that's
each command's own test module's job (e.g. capture/tests/test_gmail_backfill.py) —
this only proves the wrapper itself records what actually happened.
"""

from __future__ import annotations

import pytest
import requests

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
