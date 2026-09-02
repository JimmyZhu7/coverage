"""/ops/health/cron/ and /ops/health/gmail/ — staff-only JSON readers.

Each test writes JobRun/GmailConnection rows directly (not through
track_job_run or connect_gmail, which are tested on their own) so a row's
age can be pinned to an exact offset from "now" instead of depending on
real wall-clock time between the write and the request.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from capture.models import GmailConnection
from ops.models import JobRun
from ops.tracking import EXPECTED_INTERVALS
from ops.views import GMAIL_STALE_WARNING_AFTER

pytestmark = pytest.mark.django_db

URL = "/ops/health/cron/"
GMAIL_URL = "/ops/health/gmail/"

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


def _make_gmail_connection(*, status, connected_at, email="student@example.com"):
    """`connected_at` is auto_now_add=True, so create() can't set it
    directly — the value passed at construction is silently overridden on
    first save. Backdate it with a raw .update() afterward instead, the
    same way any test touching an auto_now_add field has to."""
    user = User.objects.create_user(email=email, password="x" * 14)
    conn = GmailConnection.all_objects.create(
        user=user,
        gmail_address=email,
        refresh_token_encrypted="ciphertext",
        status=status,
    )
    GmailConnection.all_objects.filter(pk=conn.pk).update(connected_at=connected_at)
    return conn


def test_gmail_health_requires_staff_login(client):
    resp = client.get(GMAIL_URL)
    assert resp.status_code in (302, 403)


def test_gmail_health_reverses_by_name(client):
    _staff_client(client)
    assert reverse("ops:health-gmail") == GMAIL_URL


def test_a_revoked_connection_is_surfaced(client):
    _staff_client(client)
    now = timezone.now()
    _make_gmail_connection(
        status="revoked", connected_at=now - timedelta(days=10),
        email="revoked-student@example.com",
    )

    resp = client.get(GMAIL_URL)
    body = resp.json()
    assert body["healthy"] is False
    assert len(body["revoked"]) == 1
    entry = body["revoked"][0]
    assert entry["user_email"] == "revoked-student@example.com"
    assert entry["gmail_address"] == "revoked-student@example.com"
    assert "connected_at" in entry
    # The caveat must travel with the data, not live only in a comment a
    # reader of the raw JSON would never see. It USED to read "does not update
    # on reconnect", which was true and is not any more: `connect_gmail` now
    # writes `connected_at` on every successful connect (WS-OPS-20), so the
    # field is the token issue date. Rewritten rather than deleted, because a
    # row last connected before 2026-09-02 still carries the old meaning and a
    # staff reader has no way to tell which kind they are looking at.
    assert "token issue date" in entry["connected_at_note"]
    assert "2026-09-02" in entry["connected_at_note"]


def test_an_active_connection_does_not_appear_in_revoked(client):
    _staff_client(client)
    now = timezone.now()
    _make_gmail_connection(
        status="active", connected_at=now - timedelta(days=10),
        email="active-student@example.com",
    )

    resp = client.get(GMAIL_URL)
    body = resp.json()
    assert body["healthy"] is True
    assert body["revoked"] == []


def test_an_active_connection_older_than_the_warning_window_is_stale(client):
    _staff_client(client)
    now = timezone.now()
    _make_gmail_connection(
        status="active",
        connected_at=now - GMAIL_STALE_WARNING_AFTER - timedelta(hours=1),
        email="stale-student@example.com",
    )

    resp = client.get(GMAIL_URL)
    body = resp.json()
    # A warning-window entry is a heads-up, not a failure — it must not
    # flip `healthy` the way an actual `revoked` row does.
    assert body["healthy"] is True
    assert len(body["stale_active"]) == 1
    entry = body["stale_active"][0]
    assert entry["user_email"] == "stale-student@example.com"
    assert "inaccurate for any reconnected mailbox" in entry["note"]


def test_an_active_connection_within_the_warning_window_is_not_stale(client):
    _staff_client(client)
    now = timezone.now()
    _make_gmail_connection(
        status="active",
        connected_at=now - GMAIL_STALE_WARNING_AFTER + timedelta(hours=1),
        email="fresh-student@example.com",
    )

    resp = client.get(GMAIL_URL)
    assert resp.json()["stale_active"] == []


def test_a_revoked_connection_never_appears_in_stale_active(client):
    """stale_active is specifically an early-warning list for connections
    that have not revoked yet — a connection that already flipped to
    revoked belongs only in the `revoked` list, not double-counted here."""
    _staff_client(client)
    now = timezone.now()
    _make_gmail_connection(
        status="revoked",
        connected_at=now - GMAIL_STALE_WARNING_AFTER - timedelta(days=5),
        email="long-revoked@example.com",
    )

    resp = client.get(GMAIL_URL)
    assert resp.json()["stale_active"] == []
