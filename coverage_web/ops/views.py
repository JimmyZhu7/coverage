"""/ops/health/cron/ — staff-only. Answers "is every render.yaml cron still
actually running", from JobRun rows the 6 wrapped commands write themselves
(ops/tracking.py). JSON, same posture as core/views.py's `healthz`: this is
read by a person checking on deploys as readily as by a script, and a
dashboard template is more code than the question needs.
"""

from __future__ import annotations

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.utils import timezone

from .models import JobRun
from .tracking import EXPECTED_INTERVALS


@staff_member_required
def health_cron(request):
    now = timezone.now()
    jobs = []
    all_ok = True
    for name, interval in EXPECTED_INTERVALS.items():
        last_success = (
            JobRun.objects.filter(name=name, status=JobRun.STATUS_SUCCESS)
            .order_by("-finished_at")
            .first()
        )
        if last_success is None:
            # Distinct from "overdue": this job has never once recorded a
            # successful run, which is either a brand-new job or a command
            # that has been failing since before the earliest JobRun row.
            all_ok = False
            jobs.append({
                "name": name,
                "status": "never_run",
                "last_success": None,
                "age_seconds": None,
                "expected_interval_seconds": int(interval.total_seconds()),
            })
            continue

        age = now - last_success.finished_at
        overdue = age > interval
        all_ok = all_ok and not overdue
        jobs.append({
            "name": name,
            "status": "overdue" if overdue else "ok",
            "last_success": last_success.finished_at.isoformat(),
            "age_seconds": int(age.total_seconds()),
            "expected_interval_seconds": int(interval.total_seconds()),
        })

    return JsonResponse({"healthy": all_ok, "jobs": jobs})
