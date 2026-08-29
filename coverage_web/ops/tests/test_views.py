"""/ops/health/cron/ — staff-only JSON reader over JobRun rows.

Each test writes JobRun rows directly (not through track_job_run — that
wrapper is tested on its own in test_tracking.py) so a run's age can be
pinned to an exact offset from "now" instead of depending on real wall-clock
time between the write and the request.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from ops.models import JobRun
from ops.tracking import EXPECTED_INTERVALS

pytestmark = pytest.mark.django_db

URL = "/ops/health/cron/"

User = get_user_model()


def _staff_client(client):
    user = User.objects.create_user(email="staff@example.com", password="x" * 14, is_staff=True)
    client.force_login(user)
    return client


def test_requires_staff_login(client):
    """An anonymous request must not learn anything about deploy health."""
    resp = client.get(URL)
    assert resp.status_code in (302, 403)


def test_a_non_staff_user_is_refused(client):
    user = User.objects.create_user(email="student@example.com", password="x" * 14)
    client.force_login(user)
    resp = client.get(URL)
    assert resp.status_code in (302, 403)


def test_a_job_with_no_jobrun_ever_is_flagged_never_run(client):
    _staff_client(client)
    resp = client.get(URL)

    body = resp.json()
    assert body["healthy"] is False
    by_name = {j["name"]: j for j in body["jobs"]}
    for name in EXPECTED_INTERVALS:
        assert by_name[name]["status"] == "never_run"
        assert by_name[name]["last_success"] is None


def test_a_job_that_ran_well_within_its_interval_reads_ok(client):
    _staff_client(client)
    now = timezone.now()
    # gmail-backfill's expected interval is 5 minutes; a run 2 minutes ago
    # is well inside it.
    JobRun.objects.create(
        name="gmail-backfill", started_at=now - timedelta(minutes=3),
        finished_at=now - timedelta(minutes=2), status=JobRun.STATUS_SUCCESS,
    )

    resp = client.get(URL)
    by_name = {j["name"]: j for j in resp.json()["jobs"]}
    assert by_name["gmail-backfill"]["status"] == "ok"
    assert by_name["gmail-backfill"]["last_success"] is not None


def test_a_job_older_than_its_expected_interval_is_flagged_overdue(client):
    _staff_client(client)
    now = timezone.now()
    # scrape is expected every 6 hours; a last success 7 hours ago is overdue.
    JobRun.objects.create(
        name="scrape", started_at=now - timedelta(hours=7, minutes=5),
        finished_at=now - timedelta(hours=7), status=JobRun.STATUS_SUCCESS,
    )

    resp = client.get(URL)
    body = resp.json()
    assert body["healthy"] is False
    by_name = {j["name"]: j for j in body["jobs"]}
    assert by_name["scrape"]["status"] == "overdue"


def test_a_failed_run_does_not_count_as_the_last_success(client):
    """A job that has been failing every tick must not read as healthy just
    because it keeps *starting* on schedule."""
    _staff_client(client)
    now = timezone.now()
    JobRun.objects.create(
        name="pro-trial-expire", started_at=now - timedelta(minutes=5),
        finished_at=now - timedelta(minutes=4), status=JobRun.STATUS_FAILED,
    )

    resp = client.get(URL)
    by_name = {j["name"]: j for j in resp.json()["jobs"]}
    assert by_name["pro-trial-expire"]["status"] == "never_run"


def test_a_currently_running_job_does_not_mask_the_last_real_success(client):
    """A `running` row (job mid-execution right now) must not itself count
    as evidence of health, but a prior real success should still show."""
    _staff_client(client)
    now = timezone.now()
    JobRun.objects.create(
        name="weekly-digest", started_at=now - timedelta(days=1),
        finished_at=now - timedelta(days=1) + timedelta(minutes=1),
        status=JobRun.STATUS_SUCCESS,
    )
    JobRun.objects.create(
        name="weekly-digest", started_at=now - timedelta(seconds=5),
        finished_at=None, status=JobRun.STATUS_RUNNING,
    )

    resp = client.get(URL)
    by_name = {j["name"]: j for j in resp.json()["jobs"]}
    assert by_name["weekly-digest"]["status"] == "ok"


def test_reverses_by_name(client):
    _staff_client(client)
    assert reverse("ops:health-cron") == URL
