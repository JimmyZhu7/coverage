"""The Gmail Live Pro-only gate (docs/pricing-rebalance-plan.md §7).

Real-time sync (`users.watch()`) is Pro-only; the on-demand connection,
backfill, and Scan Now stay free on every plan. Nothing checked `user.plan`
anywhere in `capture/` before this — these tests pin the three gate points:

1. `connect_gmail` only calls `register_watch` for a Pro user. A Free
   connect still succeeds in full (connection stored, backfill queued).
2. `renew_watches` only re-registers watches for Pro connections.
3. `process_notification` drops a live notification for a non-Pro
   connection defensively (a trial that just expired, a manual downgrade).

Google, `Flow`, and the Gmail client are all mocked — same posture as
test_gmail_connect.py, which this file borrows its fixtures' shape from.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model

from capture import gmail_live
from capture.models import GmailConnection

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def free_student(db):
    return User.objects.create_user(
        email="free-student@example.com", password="x", plan=User.PLAN_FREE
    )


@pytest.fixture
def pro_student(db):
    return User.objects.create_user(
        email="pro-student@example.com", password="x", plan=User.PLAN_PRO
    )


def _fake_flow(refresh_token: str = "1//fake-refresh-token") -> MagicMock:
    flow = MagicMock()
    flow.credentials = MagicMock(refresh_token=refresh_token)
    return flow


def _fake_gmail_client(address: str) -> MagicMock:
    client = MagicMock()
    client.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": address,
        "historyId": "1000",
    }
    client.users.return_value.watch.return_value.execute.return_value = {
        "historyId": "1001",
        "expiration": "9999999999999",
    }
    return client


class TestConnectGmailPlanGate:
    def test_free_user_connects_without_a_watch_but_backfill_still_queues(
        self, free_student, settings
    ):
        """Isolates the PLAN gate from the Pro trial (accounts.trials),
        which also lives on this same call site and is ON by default
        (PRO_TRIAL_TRIGGER="gmail_connect") — see
        TestConnectGmailStartsAProTrial below for that behavior. Turning the
        trigger off here proves the underlying gate holds independently: a
        Free user who is NOT trial-eligible (or trials are configured off
        entirely) still gets no watch."""
        settings.PRO_TRIAL_TRIGGER = "off"
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        fake_gmail = _fake_gmail_client("free-student@example.com")
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail), \
             patch.object(gmail_live, "register_watch") as mock_register:
            connection = gmail_live.connect_gmail(
                free_student, "auth-code", "https://x/callback"
            )

        mock_register.assert_not_called()
        assert connection.status == "active"
        assert connection.watch_expiration is None
        # The connection and its backfill are unaffected by the gate — only
        # the watch registration is skipped.
        assert connection.backfill_status == "pending"
        assert GmailConnection.all_objects.filter(user=free_student).count() == 1

    def test_pro_user_connects_and_a_watch_is_registered(self, pro_student, settings):
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        # A real (unmocked) `register_watch` now holds `is_push_configured()`
        # — a Pub/Sub topic — so a watch that's actually meant to succeed
        # needs one set, same as production (the connect flow's own
        # `is_configured()` gate already guarantees CLIENT_ID/SECRET by the
        # time register_watch runs; only TOPIC is unique to push).
        settings.GMAIL_LIVE_CLIENT_ID = "cid"
        settings.GMAIL_LIVE_CLIENT_SECRET = "csecret"
        settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
        fake_gmail = _fake_gmail_client("pro-student@example.com")
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail), \
             patch.object(gmail_live, "_gmail_client", return_value=fake_gmail):
            connection = gmail_live.connect_gmail(
                pro_student, "auth-code", "https://x/callback"
            )

        assert connection.status == "active"
        assert connection.watch_expiration is not None
        assert connection.backfill_status == "pending"

    def test_an_existing_pro_connection_is_not_broken_by_a_reconnect(
        self, pro_student, settings
    ):
        """The founder's own account is already connected on production —
        the gate must not regress an existing Pro connection's real-time
        sync on a routine reconnect."""
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        settings.GMAIL_LIVE_CLIENT_ID = "cid"
        settings.GMAIL_LIVE_CLIENT_SECRET = "csecret"
        settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
        GmailConnection.all_objects.create(
            user=pro_student,
            gmail_address="old@example.com",
            refresh_token_encrypted="stale",
            backfill_status="done",
        )
        fake_gmail = _fake_gmail_client("pro-student@example.com")
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail), \
             patch.object(gmail_live, "_gmail_client", return_value=fake_gmail):
            connection = gmail_live.connect_gmail(
                pro_student, "auth-code", "https://x/callback"
            )

        assert connection.watch_expiration is not None


class TestConnectGmailStartsAProTrial:
    """The Pro trial (accounts.trials) shares this exact call site — a Free
    account's first Gmail connect flips `plan` to "pro" BEFORE the gate
    above runs, so its real-time sync turns on the same request. Full
    trial-lifecycle coverage (no-second-trial, expiry, expiry-doesn't-
    disconnect) lives in accounts/tests/test_pro_trial.py; this class only
    pins the one thing that's specific to THIS call site: the trial flip
    actually reaches the watch-registration gate right below it."""

    def test_a_free_users_first_connect_starts_a_trial_and_registers_a_watch(
        self, free_student, settings
    ):
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        settings.GMAIL_LIVE_CLIENT_ID = "cid"
        settings.GMAIL_LIVE_CLIENT_SECRET = "csecret"
        settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
        fake_gmail = _fake_gmail_client("free-student@example.com")
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail), \
             patch.object(gmail_live, "_gmail_client", return_value=fake_gmail):
            connection = gmail_live.connect_gmail(
                free_student, "auth-code", "https://x/callback"
            )

        free_student.refresh_from_db()
        assert free_student.plan == "pro"
        assert free_student.pro_trial_ends_at is not None
        assert connection.watch_expiration is not None

    def test_the_trigger_can_be_turned_off_entirely(self, free_student, settings):
        settings.PRO_TRIAL_TRIGGER = "off"
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        fake_gmail = _fake_gmail_client("free-student@example.com")
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail), \
             patch.object(gmail_live, "register_watch") as mock_register:
            gmail_live.connect_gmail(free_student, "auth-code", "https://x/callback")

        free_student.refresh_from_db()
        assert free_student.plan == "free"
        mock_register.assert_not_called()


class TestRenewWatchesPlanGate:
    def test_a_free_connections_watch_is_never_renewed(self, free_student):
        connection = GmailConnection.all_objects.create(
            user=free_student,
            gmail_address="free-student@example.com",
            refresh_token_encrypted="unused",
            status="active",
            backfill_status="done",
        )
        with patch.object(gmail_live, "register_watch") as mock_register:
            renewed, revoked = gmail_live.renew_watches()

        mock_register.assert_not_called()
        assert (renewed, revoked) == (0, 0)
        connection.refresh_from_db()
        assert connection.watch_expiration is None

    def test_a_pro_connections_watch_is_renewed(self, pro_student):
        GmailConnection.all_objects.create(
            user=pro_student,
            gmail_address="pro-student@example.com",
            refresh_token_encrypted="unused",
            status="active",
            backfill_status="done",
        )

        def _fake_register(connection):
            from django.utils import timezone
            from datetime import timedelta
            connection.watch_expiration = timezone.now() + timedelta(days=7)
            connection.save(update_fields=["watch_expiration"])

        with patch.object(gmail_live, "register_watch", side_effect=_fake_register) as mock_register:
            renewed, revoked = gmail_live.renew_watches()

        mock_register.assert_called_once()
        assert (renewed, revoked) == (1, 0)

    def test_a_downgraded_trial_connection_stops_renewing_without_being_deleted(
        self, free_student
    ):
        """A Pro trial that just expired flips `user.plan` back to Free
        (accounts.trials / the pro_trial_expire command) — this is the exact
        state renew_watches must degrade honestly rather than delete."""
        connection = GmailConnection.all_objects.create(
            user=free_student,
            gmail_address="free-student@example.com",
            refresh_token_encrypted="unused",
            status="active",
            backfill_status="done",
        )
        with patch.object(gmail_live, "register_watch") as mock_register:
            gmail_live.renew_watches()

        mock_register.assert_not_called()
        assert GmailConnection.all_objects.filter(pk=connection.pk).exists()


class TestProcessNotificationPlanGate:
    def test_a_notification_for_a_non_pro_connection_is_dropped(self, free_student):
        GmailConnection.all_objects.create(
            user=free_student,
            gmail_address="free-student@example.com",
            refresh_token_encrypted="unused",
            status="active",
        )
        with patch.object(gmail_live, "sync_connection") as mock_sync:
            gmail_live.process_notification("free-student@example.com", "2000")

        mock_sync.assert_not_called()

    def test_a_notification_for_a_pro_connection_still_syncs(self, pro_student):
        GmailConnection.all_objects.create(
            user=pro_student,
            gmail_address="pro-student@example.com",
            refresh_token_encrypted="unused",
            status="active",
        )
        with patch.object(gmail_live, "sync_connection") as mock_sync:
            gmail_live.process_notification("pro-student@example.com", "2000")

        mock_sync.assert_called_once()
