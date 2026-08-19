"""Free plan's once-per-GMAIL_FREE_RESCAN_INTERVAL_DAYS "Scan Now" throttle.

Real-time sync (capture/tests/test_gmail_plan_gate.py) is only worth a paid
axis if Free can't reproduce it for free by mashing the same button the
gmail_backfill cron already polls every 15 minutes — see
settings.py's GMAIL_FREE_RESCAN_INTERVAL_DAYS comment. Pro (including an
active Pro trial, which is just `user.plan == "pro"`) is never throttled.

`gmail_live.free_rescan_unlocks_at` is the single function both
`capture.views.gmail_rescan` (enforcement) and `accounts.views.
_gmail_live_context` (the Settings button's disabled state) call — these
tests exercise it through the view, the one place a block is user-visible.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from capture import gmail_live
from capture.models import GmailConnection

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def free_student(db):
    return User.objects.create_user(
        email="throttle-free@example.com", password="x", plan=User.PLAN_FREE
    )


@pytest.fixture
def pro_student(db):
    return User.objects.create_user(
        email="throttle-pro@example.com", password="x", plan=User.PLAN_PRO
    )


def _connection(user, **extra):
    return GmailConnection.all_objects.create(
        user=user,
        gmail_address=user.email,
        refresh_token_encrypted="unused-in-these-tests",
        status="active",
        backfill_status="done",
        **extra,
    )


class TestFreeRescanUnlocksAt:
    def test_pro_is_never_throttled_even_with_a_scan_moments_ago(self, pro_student):
        connection = _connection(pro_student, rescan_completed_at=timezone.now())
        assert gmail_live.free_rescan_unlocks_at(connection) is None

    def test_free_with_no_prior_scan_is_not_throttled(self, free_student):
        connection = _connection(free_student)
        assert gmail_live.free_rescan_unlocks_at(connection) is None

    def test_free_inside_the_window_is_throttled(self, free_student, settings):
        settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7
        connection = _connection(
            free_student, rescan_completed_at=timezone.now() - timedelta(days=1)
        )
        unlocks_at = gmail_live.free_rescan_unlocks_at(connection)
        assert unlocks_at is not None
        assert unlocks_at > timezone.now()

    def test_free_after_the_window_is_not_throttled(self, free_student, settings):
        settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7
        connection = _connection(
            free_student, rescan_completed_at=timezone.now() - timedelta(days=8)
        )
        assert gmail_live.free_rescan_unlocks_at(connection) is None

    def test_falls_back_to_requested_at_when_nothing_has_completed_yet(
        self, free_student, settings
    ):
        """A rescan that was queued but never finished (still pending/
        running, or crashed) must still count against the throttle — a
        Free user can't bypass it by repeatedly queuing without waiting for
        completion."""
        settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7
        connection = _connection(
            free_student,
            rescan_status="pending",
            rescan_requested_at=timezone.now() - timedelta(hours=1),
        )
        assert gmail_live.free_rescan_unlocks_at(connection) is not None


class TestGmailRescanViewThrottle:
    def test_a_free_user_inside_the_window_is_refused_with_a_clear_message(
        self, client, free_student, settings
    ):
        settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7
        connection = _connection(
            free_student, rescan_completed_at=timezone.now() - timedelta(days=1)
        )
        client.force_login(free_student)

        resp = client.post(reverse("capture:gmail_rescan"), follow=True)

        connection.refresh_from_db()
        assert connection.rescan_status != "pending"  # not queued
        messages = [str(m) for m in resp.context["messages"]]
        assert any("Free plan" in m for m in messages)

    def test_a_free_user_after_the_window_is_allowed(self, client, free_student, settings):
        settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7
        connection = _connection(
            free_student, rescan_completed_at=timezone.now() - timedelta(days=8)
        )
        client.force_login(free_student)

        client.post(reverse("capture:gmail_rescan"))

        connection.refresh_from_db()
        assert connection.rescan_status == "pending"

    def test_a_pro_user_is_never_blocked_by_the_throttle(self, client, pro_student, settings):
        settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7
        connection = _connection(pro_student, rescan_completed_at=timezone.now())
        client.force_login(pro_student)

        client.post(reverse("capture:gmail_rescan"))

        connection.refresh_from_db()
        assert connection.rescan_status == "pending"

    def test_a_trial_user_is_never_blocked_by_the_throttle(self, client, free_student, settings):
        """An active Pro trial is just `user.plan == "pro"`
        (accounts.trials never introduces a third plan value) — the
        throttle's own plan check covers it with no special case."""
        settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7
        free_student.plan = User.PLAN_PRO
        free_student.pro_trial_started_at = timezone.now()
        free_student.pro_trial_ends_at = timezone.now() + timedelta(days=13)
        free_student.save(
            update_fields=["plan", "pro_trial_started_at", "pro_trial_ends_at"]
        )
        connection = _connection(free_student, rescan_completed_at=timezone.now())
        client.force_login(free_student)

        client.post(reverse("capture:gmail_rescan"))

        connection.refresh_from_db()
        assert connection.rescan_status == "pending"

    def test_the_in_flight_guard_still_wins_over_the_throttle_message(
        self, client, free_student, settings
    ):
        """Both guards could theoretically fire together (a Free user's
        earlier scan is still running AND inside the throttle window) — the
        in-flight message ("already in progress") is the more specific and
        more urgent one, so it must be checked first."""
        settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7
        connection = _connection(
            free_student,
            rescan_status="running",
            rescan_requested_at=timezone.now() - timedelta(hours=1),
        )
        client.force_login(free_student)

        resp = client.post(reverse("capture:gmail_rescan"), follow=True)

        connection.refresh_from_db()
        assert connection.rescan_status == "running"  # untouched
        messages = [str(m) for m in resp.context["messages"]]
        assert any("already in progress" in m for m in messages)
