"""The shared per-mailbox advisory lock (capture/locks.py).

The defect: the lock lived inside gmail_poll and guarded poll-vs-poll only,
while gmail_backfill's two selections, the Pub/Sub listener, and the
import-triggered scan all write through the same apply_findings machinery
against the same mailbox — free to interleave with a live poll pass. The
ratchet bounds a race to one duplicated same-stage touch, but a 2-minute
loop running permanently makes "eventually" a schedule. Every writer now
takes the same (namespace, connection_id) lock and SKIPS when it is held.

The second-session pattern mirrors test_gmail_poll's: a genuinely separate
Postgres connection stands in for the other running process.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connections
from io import StringIO

from capture import gmail_live, locks
from capture.models import GmailConnection

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()

SETTINGS = dict(
    GMAIL_LIVE_CLIENT_ID="cid",
    GMAIL_LIVE_CLIENT_SECRET="secret",
    GMAIL_LIVE_PUBSUB_TOPIC="projects/x/topics/y",
    GMAIL_LIVE_TOKEN_KEY="ln1vlZQY1lTQ9DK9zS9DsWzzVFhaZFqNiK1S6FMbY24=",
)


def _user(email="lock-user@example.com", plan="pro"):
    user = User.objects.create_user(email=email, password="x")
    user.plan = plan
    user.save(update_fields=["plan"])
    return user


def _connection(user, **over):
    fields = dict(
        user=user, gmail_address=user.email,
        refresh_token_encrypted="x", history_id="1000", status="active",
    )
    fields.update(over)
    return GmailConnection.all_objects.create(**fields)


class _OtherSession:
    """Holds the mailbox lock from a second, real Postgres session."""

    def __init__(self, connection_id: int):
        self.connection_id = connection_id
        self.conn = connections.create_connection("default")

    def __enter__(self):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)",
                [locks.ADVISORY_LOCK_NAMESPACE, self.connection_id],
            )
            assert cursor.fetchone()[0] is True
        return self

    def __exit__(self, *exc):
        self.conn.close()  # closing the session releases its locks
        return False


def test_backfill_skips_a_mailbox_another_run_holds(settings):
    for key, value in SETTINGS.items():
        setattr(settings, key, value)
    connection = _connection(_user(), backfill_status="pending")

    with _OtherSession(connection.pk):
        with patch.object(gmail_live, "backfill_connection") as backfill:
            out = StringIO()
            call_command("gmail_backfill", stdout=out)

        backfill.assert_not_called()
        assert "deferred to the next tick" in out.getvalue()

    connection.refresh_from_db()
    # Still pending: the next cron tick retries — the skip is a deferral,
    # never a silent completion or a failure.
    assert connection.backfill_status == "pending"


def test_rescan_skips_a_mailbox_another_run_holds(settings):
    for key, value in SETTINGS.items():
        setattr(settings, key, value)
    connection = _connection(
        _user("lock-rescan@example.com"), rescan_status="pending"
    )

    with _OtherSession(connection.pk):
        with patch.object(gmail_live, "run_rescan") as rescan:
            out = StringIO()
            call_command("gmail_backfill", stdout=out)

        rescan.assert_not_called()

    connection.refresh_from_db()
    assert connection.rescan_status == "pending"


def test_backfill_releases_the_lock_when_done(settings):
    for key, value in SETTINGS.items():
        setattr(settings, key, value)
    connection = _connection(
        _user("lock-release@example.com"), backfill_status="pending"
    )

    class _Result:
        findings = 0
        touches_logged = 0
        outreach_logged = 0
        bounced_cleared = 0
        details = []

        @staticmethod
        def as_stats():
            return {}

    with patch.object(gmail_live, "backfill_connection", return_value=_Result):
        call_command("gmail_backfill", stdout=StringIO())

    other = connections.create_connection("default")
    try:
        with other.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)",
                [locks.ADVISORY_LOCK_NAMESPACE, connection.pk],
            )
            assert cursor.fetchone()[0] is True, "backfill leaked its lock"
    finally:
        other.close()


def test_pubsub_notification_skips_a_held_mailbox(settings):
    for key, value in SETTINGS.items():
        setattr(settings, key, value)
    connection = _connection(_user("lock-push@example.com"))

    with _OtherSession(connection.pk):
        with patch.object(gmail_live, "sync_connection") as sync:
            gmail_live.process_notification(connection.gmail_address, "2000")
        sync.assert_not_called()

    # And with the lock free, the notification syncs as before.
    with patch.object(gmail_live, "sync_connection") as sync:
        gmail_live.process_notification(connection.gmail_address, "2000")
    sync.assert_called_once()


def test_import_scan_skips_a_held_mailbox(settings):
    for key, value in SETTINGS.items():
        setattr(settings, key, value)
    user = _user("lock-import@example.com")
    connection = _connection(user)
    from crm.models import Contact

    contact = Contact.all_objects.create(
        user=user, name="Jane Banker", email="jane@firm.example",
        source="manual",
    )

    with _OtherSession(connection.pk):
        with patch.object(gmail_live, "backfill_connection") as backfill:
            result = gmail_live.backfill_new_contacts(user, [contact])
        backfill.assert_not_called()
    # Best-effort contract: "didn't run" is the documented None.
    assert result is None
