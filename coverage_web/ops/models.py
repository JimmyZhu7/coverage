"""Cron/management-command health tracking (tooling-plan item 5).

Not a `PrivateModel` (coverage_web/tenancy.py) — a `JobRun` row belongs to
the deployment, not to any one student, so it carries no `user` FK and is
never touched by the multi-tenant `for_user`/`all_objects` contract every
other app's models follow.
"""

from __future__ import annotations

from django.db import models


class JobRun(models.Model):
    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
    ]

    # One of render.yaml's cron `name:` fields, minus the "coverage-" prefix
    # (e.g. "gmail-backfill" for the `coverage-gmail-backfill` cron) — see
    # ops/tracking.py's `EXPECTED_INTERVALS`, which is keyed the same way.
    # Not a `choices=` field: a new job wiring up `track_job_run("...")` for
    # the first time should not also need a migration to name itself.
    name = models.CharField(max_length=64, db_index=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            # The health view's own query: "the most recent SUCCESS row for
            # this job name" — (name, status) narrows to the right rows,
            # -started_at lets it take the first one without a full scan.
            models.Index(fields=["name", "status", "-started_at"], name="ops_jobrun_health_lookup"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.status}) @ {self.started_at:%Y-%m-%d %H:%M}"
