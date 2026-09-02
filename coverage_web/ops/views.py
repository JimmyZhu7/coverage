"""/ops/health/cron/ — staff-only. Answers "is every render.yaml cron still
actually running", from JobRun rows the 6 wrapped commands write themselves
(ops/tracking.py). JSON, same posture as core/views.py's `healthz`: this is
read by a person checking on deploys as readily as by a script, and a
dashboard template is more code than the question needs.

/ops/health/gmail/ — same posture, different question: "which mailboxes
need a human to reconnect them." Until Coverage's Gmail OAuth client is
Google-verified, every consented token is issued under Google's "Testing"
publishing status, which expires it 7 days after consent regardless of use
(Google's own documented behavior for unverified apps, not a Coverage bug).
The next sync after that gets an invalid_grant-shaped error back, and
gmail_live.py already reacts to it by flipping GmailConnection.status to
"revoked" — see the STATUS_CHOICES docstring on that model: "surfaced rather
than silently retried forever." Nothing was doing that surfacing before this
view; a revoked connection just sat there until a student noticed their
sync had stopped.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.utils import timezone

from capture.models import GmailConnection

from .models import JobRun
from .tracking import EXPECTED_INTERVALS

# Testing-mode grants expire 7 days after consent (Google's fixed limit).
# A connection whose `connected_at` is already past this many days without
# having been revoked yet is worth a heads-up before it flips — see
# `health_gmail`'s "stale_active" list below for the load-bearing caveat on
# why this is approximate, not a countdown.
GMAIL_STALE_WARNING_AFTER = timedelta(days=5)


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


@staff_member_required
def health_gmail(request):
    """Surfaces GmailConnection rows a human needs to act on.

    Deliberately scoped to visibility, not prediction:

    - `revoked`: connections gmail_live.py has already flipped out of
      `active` because Google's own refresh call reported the grant gone.
      This is ground truth, not a guess — these need a reconnect now.
    - `stale_active`: `active` connections whose `connected_at` already
      exceeds GMAIL_STALE_WARNING_AFTER, as an early-warning heads-up before
      they hit `revoked` (Testing-mode expiry is a fixed 7 days, not a
      random event, so this is worth flagging ahead of time).

      `connected_at` is now the TOKEN ISSUE DATE, which is what makes this
      age mean anything. It used to be `auto_now_add` and nothing else, so it
      recorded the first connect and never moved: for any mailbox that had
      ever been reconnected, this list measured staleness from the ORIGINAL
      connection and overstated it, sometimes by months. `connect_gmail`
      writes the field explicitly on every successful connect as of
      2026-09-02 (WS-OPS-20), so a reconnect resets the clock the way a
      reader of this page always assumed it did. Rows last written before
      that date still carry the old meaning, which is why each entry says
      where its timestamp comes from rather than presenting a bare age.
    """
    all_ok = True
    now = timezone.now()

    revoked_qs = (
        GmailConnection.all_objects.filter(status="revoked")
        .select_related("user")
        .order_by("-connected_at")
    )
    revoked = []
    for conn in revoked_qs:
        all_ok = False
        revoked.append({
            "user_email": conn.user.email,
            "gmail_address": conn.gmail_address,
            "connected_at": conn.connected_at.isoformat(),
            "connected_at_note": (
                "connected_at is the token issue date: connect_gmail writes "
                "it on every successful connect (since 2026-09-02). Rows "
                "last connected before that date still read as first-connect "
                "and may overstate the age"
            ),
        })

    stale_cutoff = now - GMAIL_STALE_WARNING_AFTER
    stale_qs = (
        GmailConnection.all_objects.filter(status="active", connected_at__lt=stale_cutoff)
        .select_related("user")
        .order_by("connected_at")
    )
    stale_active = []
    for conn in stale_qs:
        stale_active.append({
            "user_email": conn.user.email,
            "gmail_address": conn.gmail_address,
            "connected_at": conn.connected_at.isoformat(),
            "age_seconds": int((now - conn.connected_at).total_seconds()),
            "note": (
                "approximate, based on original connection date; inaccurate "
                "for any reconnected mailbox — not a precise expiry countdown"
            ),
        })

    return JsonResponse({
        "healthy": all_ok,
        "revoked": revoked,
        "stale_active_warning_after_days": GMAIL_STALE_WARNING_AFTER.days,
        "stale_active": stale_active,
    })
