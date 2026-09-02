"""accounts/trials.py — the Pro trial mechanics (settings.PRO_TRIAL_DAYS /
PRO_TRIAL_TRIGGER) and the `pro_trial_expire` management command that
reverts an expired one.

Only the MECHANICS are tested here: plan flip, timestamps, the
never-a-second-trial rule, expiry, expiry NOT disconnecting Gmail, and the
three things expiry now does that it used to skip — the Settings banner, the
email behind the EMAIL_URL gate, and unlocking the Free "Scan Now". The call
site that actually triggers a trial (`capture.gmail_live.connect_gmail`) has
its own narrow pin in capture/tests/test_gmail_plan_gate.py; this file does
not re-test that wiring, only the module's own contract.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts import trials as pro_trials
from capture.models import GmailConnection

pytestmark = pytest.mark.django_db

User = get_user_model()

#: An SMTP-shaped backend that still collects into `mail.outbox`. The gate
#: `accounts.trials.email_is_configured()` reads is "is this backend one that
#: only prints" — console and dummy — so locmem counts as configured, which
#: is what makes the sent path testable at all. Under a plain pytest run the
#: backend is locmem, so a test that wants the UNconfigured path has to say
#: so explicitly; that is the `_console_email` fixture below.
_CONSOLE_BACKEND = "django.core.mail.backends.console.EmailBackend"


@pytest.fixture
def free_student():
    return User.objects.create_user(email="trial-student@example.com", password="x")


@pytest.fixture
def console_email(settings):
    """A deploy with no EMAIL_URL — production.py's own default."""
    settings.EMAIL_BACKEND = _CONSOLE_BACKEND
    return settings


def _expire(user):
    """Put this account's trial in the past, the way a day passing would."""
    user.pro_trial_ends_at = timezone.now() - timedelta(hours=1)
    user.save(update_fields=["pro_trial_ends_at"])


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


# ---------------------------------------------------------------------------
# The end of a trial, said out loud. Two docstrings (accounts/models.py's
# note on `pro_trial_ends_at`, and pro_trial_expire's) promised Settings
# could say "your trial ended"; no template said it and no mail was sent.
# ---------------------------------------------------------------------------
class TestTrialEndedNotice:
    def _lapsed(self, user, settings):
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_trials.start_trial_if_eligible(user, trigger="gmail_connect")
        _expire(user)
        call_command("pro_trial_expire")
        user.refresh_from_db()
        return user

    def test_a_lapsed_trial_gets_a_notice_naming_the_end_date(self, free_student, settings):
        user = self._lapsed(free_student, settings)

        notice = pro_trials.trial_ended_notice(user)

        assert notice is not None
        assert notice["ended_at"] == user.pro_trial_ends_at

    def test_an_active_trial_gets_no_notice(self, free_student, settings):
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")

        assert pro_trials.trial_ended_notice(free_student) is None

    def test_an_account_that_never_had_a_trial_gets_no_notice(self, free_student):
        assert pro_trials.trial_ended_notice(free_student) is None

    def test_an_admin_granted_pro_account_gets_no_notice(self):
        """Same distinction `pro_trial_expire`'s selection query relies on:
        a hand-flipped Pro account has `pro_trial_ends_at` null forever."""
        admin_granted = User.objects.create_user(
            email="admin-pro-notice@example.com", password="x", plan=User.PLAN_PRO
        )
        assert pro_trials.trial_ended_notice(admin_granted) is None

    def test_dismissing_it_makes_it_stay_gone(self, free_student, settings):
        user = self._lapsed(free_student, settings)
        assert pro_trials.trial_ended_notice(user) is not None

        assert pro_trials.dismiss_trial_ended_notice(user) is True

        user.refresh_from_db()
        assert pro_trials.trial_ended_notice(user) is None

    def test_dismissing_twice_does_not_move_the_timestamp(self, free_student, settings):
        """A double-submit from two open tabs must be a no-op, not a second
        write."""
        user = self._lapsed(free_student, settings)
        pro_trials.dismiss_trial_ended_notice(user)
        first = user.pro_trial_notice_dismissed_at

        assert pro_trials.dismiss_trial_ended_notice(user) is False
        user.refresh_from_db()
        assert user.pro_trial_notice_dismissed_at == first

    def test_the_settings_page_renders_the_banner_and_the_post_clears_it(
        self, free_student, settings
    ):
        """End to end through the page a student actually looks at."""
        user = self._lapsed(free_student, settings)
        client = Client()
        client.force_login(user)

        body = client.get(reverse("accounts:settings")).content.decode()
        assert "Pro trial ended" in body
        # The three facts the banner exists to carry.
        assert "Real-time Gmail sync is paused" in body
        assert "Scan Now is unlocked" in body
        assert "credits, contacts and touches stayed" in body

        resp = client.post(reverse("accounts:dismiss_trial_notice"))
        assert resp.status_code == 302

        assert "Pro trial ended" not in client.get(reverse("accounts:settings")).content.decode()

    def test_the_dismiss_route_refuses_a_get(self, free_student, settings):
        """Acknowledging a notice must never be something a link, a prefetch
        or a crawler can do on the student's behalf."""
        user = self._lapsed(free_student, settings)
        client = Client()
        client.force_login(user)

        assert client.get(reverse("accounts:dismiss_trial_notice")).status_code == 405
        user.refresh_from_db()
        assert user.pro_trial_notice_dismissed_at is None

    def test_a_settings_render_for_an_ordinary_free_account_has_no_banner(self, free_student):
        client = Client()
        client.force_login(free_student)

        assert "Pro trial ended" not in client.get(reverse("accounts:settings")).content.decode()


class TestTrialEndedEmail:
    def test_the_expiry_run_mails_the_student_when_email_is_configured(
        self, free_student, settings
    ):
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        settings.PRO_TRIAL_DAYS = 14
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        _expire(free_student)
        mail.outbox.clear()

        call_command("pro_trial_expire")

        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == [free_student.email]
        assert "trial" in message.subject.lower()
        # The one fact that makes the reversion safe, in the body itself.
        assert "clawed back" in message.body
        assert "14-day" in message.body

    def test_nothing_is_mailed_on_a_deploy_with_no_relay(
        self, free_student, settings, console_email
    ):
        """The console backend prints to the service logs. A notice printed
        into a log has not reached anyone, so this sends nothing and lets the
        Settings banner carry the message alone — the deferred-paid-setup
        posture, not a swallowed failure."""
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        _expire(free_student)
        mail.outbox.clear()

        call_command("pro_trial_expire")

        assert mail.outbox == []
        # The plan still flipped, and the banner is still there.
        free_student.refresh_from_db()
        assert free_student.plan == User.PLAN_FREE
        assert pro_trials.trial_ended_notice(free_student) is not None

    def test_a_send_failure_never_undoes_the_plan_flip(self, free_student, settings):
        """The plan reversion is the real work; the mail is the receipt. An
        SMTP relay having a bad minute must not leave an account stuck on
        Pro, nor fail the cron."""
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        _expire(free_student)

        with patch(
            "accounts.trials.EmailMultiAlternatives.send", side_effect=OSError("relay down")
        ):
            call_command("pro_trial_expire")  # must not raise

        free_student.refresh_from_db()
        assert free_student.plan == User.PLAN_FREE

    def test_email_is_configured_reads_the_backend(self, settings):
        settings.EMAIL_BACKEND = _CONSOLE_BACKEND
        assert pro_trials.email_is_configured() is False

        settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
        assert pro_trials.email_is_configured() is False

        settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
        assert pro_trials.email_is_configured() is True


class TestFreeRescanUnlockedAtExpiry:
    """An expired trialist's first instinct is Scan Now, and it was locked
    for up to `GMAIL_FREE_RESCAN_INTERVAL_DAYS` from their last PRO-era scan
    — a scan the Free throttle never applied to."""

    def _connection(self, user, **kwargs):
        return GmailConnection.all_objects.create(
            user=user,
            gmail_address=user.email,
            refresh_token_encrypted="unused",
            status="active",
            backfill_status="done",
            **kwargs,
        )

    def test_a_pro_era_scan_no_longer_locks_the_first_free_scan(
        self, free_student, settings
    ):
        """The audit's exact timeline: scan at T while Pro, trial expires at
        T+1d, and the button must work at T+1d+1h rather than at T+7d."""
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        connection = self._connection(
            free_student,
            rescan_status="done",
            rescan_requested_at=timezone.now() - timedelta(days=1, hours=1),
            rescan_completed_at=timezone.now() - timedelta(days=1, hours=1),
        )
        _expire(free_student)

        call_command("pro_trial_expire")

        free_student.refresh_from_db()
        connection.refresh_from_db()
        from capture import gmail_live

        assert gmail_live.free_rescan_unlocks_at(connection) is None

    def test_the_button_is_actually_pressable_afterwards(self, free_student, settings):
        """Through the view, not just the helper — the throttle is enforced
        server-side in `capture.views.gmail_rescan`."""
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        connection = self._connection(
            free_student,
            rescan_status="done",
            rescan_completed_at=timezone.now() - timedelta(days=1),
        )
        _expire(free_student)
        call_command("pro_trial_expire")

        client = Client()
        client.force_login(free_student)
        client.post(reverse("capture:gmail_rescan"))

        connection.refresh_from_db()
        assert connection.rescan_status == "pending"

    def test_a_scan_taken_AFTER_expiry_does_throttle_normally(self, free_student, settings):
        """The reset is a one-time unlock at the moment the plan flips, not a
        removal of the Free throttle. The very next scan starts a real
        seven-day clock."""
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS = 7
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        connection = self._connection(free_student, rescan_status="done",
                                      rescan_completed_at=timezone.now() - timedelta(days=1))
        _expire(free_student)
        call_command("pro_trial_expire")

        free_student.refresh_from_db()
        connection.refresh_from_db()
        connection.rescan_status = "done"
        connection.rescan_completed_at = timezone.now()
        connection.save(update_fields=["rescan_status", "rescan_completed_at"])

        from capture import gmail_live

        unlocks_at = gmail_live.free_rescan_unlocks_at(connection)
        assert unlocks_at is not None
        assert (unlocks_at - timezone.now()).days == 6  # ~7 days out

    def test_an_in_flight_rescan_is_left_completely_alone(self, free_student, settings):
        """Resetting a `pending`/`running` row would strand a run
        `gmail_backfill` has already claimed. A scan queued during the trial
        still finishes."""
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        requested = timezone.now() - timedelta(minutes=5)
        connection = self._connection(
            free_student, rescan_status="pending", rescan_requested_at=requested
        )
        _expire(free_student)

        call_command("pro_trial_expire")

        connection.refresh_from_db()
        assert connection.rescan_status == "pending"
        assert connection.rescan_requested_at == requested

    def test_the_scan_history_a_student_can_still_read_survives(self, free_student, settings):
        """`rescan_stats` is what the data export and admin read for what
        that scan actually found — the reset clears the throttle anchor, not
        the record."""
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        connection = self._connection(
            free_student, rescan_status="done",
            rescan_completed_at=timezone.now() - timedelta(days=1),
            rescan_stats={"touches_written": 14},
        )
        _expire(free_student)

        call_command("pro_trial_expire")

        connection.refresh_from_db()
        assert connection.rescan_stats == {"touches_written": 14}

    def test_an_account_with_no_gmail_connection_is_a_clean_no_op(
        self, free_student, settings
    ):
        settings.PRO_TRIAL_TRIGGER = "gmail_connect"
        pro_trials.start_trial_if_eligible(free_student, trigger="gmail_connect")
        _expire(free_student)

        call_command("pro_trial_expire")  # must not raise

        free_student.refresh_from_db()
        assert free_student.plan == User.PLAN_FREE
