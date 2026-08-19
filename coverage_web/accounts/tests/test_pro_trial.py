"""accounts/trials.py — the Pro trial mechanics (settings.PRO_TRIAL_DAYS /
PRO_TRIAL_TRIGGER) and the `pro_trial_expire` management command that
reverts an expired one.

Only the MECHANICS are tested here: plan flip, timestamps, the
never-a-second-trial rule, expiry, and expiry NOT disconnecting Gmail. The
call site that actually triggers a trial (`capture.gmail_live.connect_gmail`)
has its own narrow pin in capture/tests/test_gmail_plan_gate.py; this file
does not re-test that wiring, only the module's own contract.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from accounts import trials as pro_trials
from capture.models import GmailConnection

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def free_student():
    return User.objects.create_user(email="trial-student@example.com", password="x")


class TestStartTrialIfEligible:
    def test_a_free_user_on_the_configured_trigger_starts_a_trial(self, free_student, settings):
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        settings.PRO_TRIAL_DAYS = 14

        with patch("accounts.trials.record_event") as mock_record:
            started = pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")

        assert started is True
        free_student.refresh_from_db()
        assert free_student.plan == User.PLAN_PRO
        assert free_student.pro_trial_started_at is not None
        assert free_student.pro_trial_ends_at is not None
        delta = free_student.pro_trial_ends_at - free_student.pro_trial_started_at
        assert delta == timedelta(days=14)
        mock_record.assert_called_once()
        assert mock_record.call_args.args[0] == "pro_trial_started"

    def test_the_default_trial_length_is_fourteen_days(self):
        """PRO_TRIAL_DAYS default — the founder's own call, superseding an
        earlier 7-day default."""
        from django.conf import settings as real_settings
        assert real_settings.PRO_TRIAL_DAYS == 14

    def test_a_mismatched_trigger_does_nothing(self, free_student, settings):
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        started = pro_trials.start_trial_if_eligible(free_student, trigger="csv_import")

        assert started is False
        free_student.refresh_from_db()
        assert free_student.plan == User.PLAN_FREE
        assert free_student.pro_trial_started_at is None

    def test_an_already_pro_user_does_not_get_a_trial(self, settings):
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_user = User.objects.create_user(
            email="already-pro@example.com", password="x", plan=User.PLAN_PRO
        )
        started = pro_trials.start_trial_if_eligible(pro_user, trigger="gmail_connect")

        assert started is False
        pro_user.refresh_from_db()
        assert pro_user.pro_trial_started_at is None

    def test_never_starts_a_second_trial(self, free_student, settings):
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        assert pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect") is True

        # Simulate the trial having already ended (plan reverted to free —
        # exactly what pro_trial_expire does) and try again.
        free_student.plan = User.PLAN_FREE
        free_student.save(update_fields=["plan"])

        started_again = pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")

        assert started_again is False
        free_student.refresh_from_db()
        assert free_student.plan == User.PLAN_FREE


class TestTrialDaysLeft:
    def test_none_when_no_trial_ever_started(self, free_student):
        assert pro_trials.trial_days_left(free_student) is None

    def test_an_active_trial_reports_days_left_rounded_up(self, free_student):
        free_student.pro_trial_ends_at = timezone.now() + timedelta(days=6, hours=23)
        assert pro_trials.trial_days_left(free_student) == 7

    def test_an_expired_trial_reports_none(self, free_student):
        free_student.pro_trial_ends_at = timezone.now() - timedelta(hours=1)
        assert pro_trials.trial_days_left(free_student) is None


class TestProTrialExpireCommand:
    def test_an_expired_trial_reverts_to_free(self, free_student, settings):
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        free_student.pro_trial_ends_at = timezone.now() - timedelta(hours=1)
        free_student.save(update_fields=["pro_trial_ends_at"])

        with patch("accounts.management.commands.pro_trial_expire.record_event") as mock_record:
            call_command("pro_trial_expire")

        free_student.refresh_from_db()
        assert free_student.plan == User.PLAN_FREE
        # The timestamps stay put — see accounts/models.py's own comment on
        # why: Settings can still say "your trial ended" honestly, and a
        # lapsed trial can never restart.
        assert free_student.pro_trial_started_at is not None
        assert free_student.pro_trial_ends_at is not None
        mock_record.assert_called_once()
        assert mock_record.call_args.args[0] == "pro_trial_expired"

    def test_an_active_trial_is_left_alone(self, free_student, settings):
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")

        call_command("pro_trial_expire")

        free_student.refresh_from_db()
        assert free_student.plan == User.PLAN_PRO

    def test_an_admin_granted_pro_account_with_no_trial_is_never_touched(self, settings):
        """`pro_trial_ends_at__isnull=False` is the entire guard — an
        account that never went through `start_trial_if_eligible` (the
        founder's own, a beta tester's manual grant) has that field null
        forever, so this command's selection query excludes it by
        construction."""
        admin_granted = User.objects.create_user(
            email="admin-granted-pro@example.com", password="x", plan=User.PLAN_PRO
        )

        call_command("pro_trial_expire")

        admin_granted.refresh_from_db()
        assert admin_granted.plan == User.PLAN_PRO

    def test_expiry_does_not_disconnect_gmail(self, free_student, settings):
        """The trial ending must not disconnect Gmail — it only stops the
        real-time watch per the plan gate (capture.gmail_live.
        renew_watches' `user__plan="pro"` filter); the connection itself,
        its history, and Scan Now all keep working."""
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        connection = GmailConnection.all_objects.create(
            user=free_student,
            gmail_address="trial-student@example.com",
            refresh_token_encrypted="unused",
            status="active",
            backfill_status="done",
            watch_expiration=timezone.now() + timedelta(days=7),
        )
        free_student.pro_trial_ends_at = timezone.now() - timedelta(hours=1)
        free_student.save(update_fields=["pro_trial_ends_at"])

        call_command("pro_trial_expire")

        assert GmailConnection.all_objects.filter(pk=connection.pk).exists()
        connection.refresh_from_db()
        assert connection.status == "active"
        # The watch registration itself is untouched by this command — it
        # simply stops renewing on the next gmail_watch_renew tick (a
        # separate, already-tested gate in
        # capture/tests/test_gmail_plan_gate.py).
        assert connection.watch_expiration is not None

    def test_a_second_run_with_nothing_expired_reverts_no_one(self, free_student, settings):
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        free_student.pro_trial_ends_at = timezone.now() - timedelta(hours=1)
        free_student.save(update_fields=["pro_trial_ends_at"])
        call_command("pro_trial_expire")

        # Second run: the account is already back to Free, so nothing
        # matches `plan="pro"` any more.
        call_command("pro_trial_expire")

        free_student.refresh_from_db()
        assert free_student.plan == User.PLAN_FREE
