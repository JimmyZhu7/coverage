"""ops/admin.py — `JobRunAdmin`'s own docstring claims "Read-only, same
posture as billing.ProcessedStripeEventAdmin... A row rewritten after the
fact would defeat the point of /ops/health/cron/ trusting it." But
`ProcessedStripeEventAdmin` blocks change, delete AND add; `JobRunAdmin`
only overrode `has_change_permission` and `has_add_permission`, leaving
`ModelAdmin`'s own default `has_delete_permission` (True) in place.

Deleting a row is not less of a rewrite than editing one — it is the same
health check's evidence disappearing rather than being falsified in place.
Deleting the most recent `success` row for a job flips `/ops/health/cron/`
from "ok" to "never_run" for a job that is actually running fine; deleting a
`failed` row erases the one thing that view exists to surface. Either way
the docstring's claim ("read-only") stops being true the moment delete is
left open.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from ops.admin import JobRunAdmin
from ops.models import JobRun

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def jobrun_admin():
    from django.contrib import admin

    return JobRunAdmin(JobRun, admin.site)


@pytest.fixture
def staff_request():
    staff = User.objects.create_superuser(email="founder@coverage.local", password="x")
    request = RequestFactory().get("/admin/ops/jobrun/")
    request.user = staff
    return request


def test_a_jobrun_row_cannot_be_deleted_through_admin(jobrun_admin, staff_request):
    """The bug with teeth: deleting the most recent success row for a job
    makes /ops/health/cron/ report "never_run" for a job that is actually
    healthy, and deleting a failed row hides the one thing that view exists
    to surface."""
    assert jobrun_admin.has_delete_permission(staff_request) is False


def test_a_jobrun_row_still_cannot_be_edited_or_added(jobrun_admin, staff_request):
    """Unchanged behaviour — pinned alongside the delete guard above."""
    assert jobrun_admin.has_change_permission(staff_request) is False
    assert jobrun_admin.has_add_permission(staff_request) is False
