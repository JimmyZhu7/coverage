from __future__ import annotations

import pytest
from django.utils import timezone

from ops.models import JobRun

pytestmark = pytest.mark.django_db


def test_a_fresh_jobrun_records_correctly():
    now = timezone.now()
    run = JobRun.objects.create(name="scrape", started_at=now, status=JobRun.STATUS_RUNNING)

    run.refresh_from_db()
    assert run.name == "scrape"
    assert run.started_at == now
    assert run.finished_at is None
    assert run.status == JobRun.STATUS_RUNNING


def test_str_names_the_job_and_status():
    run = JobRun.objects.create(
        name="scrape", started_at=timezone.now(), status=JobRun.STATUS_SUCCESS
    )
    assert "scrape" in str(run)
    assert "success" in str(run)
