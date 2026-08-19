"""Phase 2: the user-triggered "Scan Now" rescan — `capture.views.gmail_rescan`
(queues it), `gmail_live.run_rescan` (the deterministic pass + Phase 3 residue
stage composed together), and the `gmail_backfill` command's rescan
selection (separate from its original backfill selection).

``transaction=True`` for the same reason `test_gmail_backfill.py` needs it:
applying a finding calls `crm.services.log_touch`, which opens its own
psycopg connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from capture import gmail_live, gmail_residue
from capture.models import GmailConnection
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def student(db):
    return User.objects.create_user(email="rescan-student@example.com", password="x")


@pytest.fixture
def connection(student):
    return GmailConnection.all_objects.create(
        user=student,
        gmail_address="rescan-student@example.com",
        refresh_token_encrypted="unused-in-these-tests",
        status="active",
        backfill_status="done",
    )


def _fake_gmail_client(message_ids: list[str], messages_by_id: dict[str, dict]):
    client = MagicMock()
    client.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": mid} for mid in message_ids],
    }

    def _get(*, userId, id, format):  # noqa: A002
        mock = MagicMock()
        mock.execute.return_value = messages_by_id[id]
        return mock

    client.users.return_value.messages.return_value.get.side_effect = _get
    return client


def _message(*, from_addr: str, to_addr: str, subject: str, snippet: str, internal_date_ms: int):
    return {
        "threadId": f"thread-{internal_date_ms}",
        "snippet": snippet,
        "internalDate": str(internal_date_ms),
        "payload": {
            "headers": [
                {"name": "From", "value": from_addr},
                {"name": "To", "value": to_addr},
                {"name": "Subject", "value": subject},
            ],
        },
    }


# ---------------------------------------------------------------------------
# The "Scan Now" POST endpoint
# ---------------------------------------------------------------------------
class TestGmailRescanView:
    def test_queues_a_rescan_for_a_connected_user(self, client, student, connection):
        client.force_login(student)
        resp = client.post(reverse("capture:gmail_rescan"))
        assert resp.status_code == 302
        connection.refresh_from_db()
        assert connection.rescan_status == "pending"
        assert connection.rescan_requested_at is not None

    def test_refuses_a_second_rescan_while_one_is_in_flight(self, client, student, connection):
        connection.rescan_status = "running"
        connection.save(update_fields=["rescan_status"])
        client.force_login(student)
        client.post(reverse("capture:gmail_rescan"))
        connection.refresh_from_db()
        assert connection.rescan_status == "running"  # unchanged, not re-queued

    def test_requires_a_connection_to_exist(self, client, student):
        client.force_login(student)
        resp = client.post(reverse("capture:gmail_rescan"))
        assert resp.status_code == 302
        assert not GmailConnection.all_objects.filter(user=student).exists()

    def test_requires_login(self, client):
        resp = client.post(reverse("capture:gmail_rescan"))
        assert resp.status_code in (302, 401, 403)

    def test_get_is_not_allowed(self, client, student, connection):
        client.force_login(student)
        resp = client.get(reverse("capture:gmail_rescan"))
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# gmail_live.run_rescan — the deterministic + residue composition
# ---------------------------------------------------------------------------
class TestRunRescan:
    def test_run_rescan_scans_all_contacts_and_never_touches_backfill_status(self, student, connection):
        Contact.all_objects.create(
            user=student, name="Jane Banker", email="jane@bank.example", source="manual"
        )
        message = _message(
            from_addr="Jane Banker <jane@bank.example>",
            to_addr=connection.gmail_address,
            subject="Re: chat",
            snippet="Happy to chat!",
            internal_date_ms=1_700_000_000_000,
        )
        client = _fake_gmail_client(["m1"], {"m1": message})

        with patch.object(gmail_live, "_gmail_client", return_value=client):
            stats = gmail_live.run_rescan(connection)

        assert stats["touches_logged"] == 1
        assert "residue" in stats
        connection.refresh_from_db()
        assert connection.backfill_status == "done"  # untouched by run_rescan itself

    def test_dry_run_makes_no_ai_call_even_if_configured(self, student, connection, settings):
        settings.ANTHROPIC_API_KEY = "sk-test-key"
        Contact.all_objects.create(
            user=student, name="Jane Banker", email="jane@bank.example", source="manual"
        )
        # A message from an unresolvable stranger becomes residue (no from_addr
        # match issue here — use a bounce-shaped message with no findable
        # recipient, which `_classify_message` returns None for).
        message = {
            "threadId": "thread-residue-1",
            "snippet": "delivery failed, no address found in this text",
            "internalDate": "1700000000000",
            "payload": {
                "headers": [
                    {"name": "From", "value": "mailer-daemon@mail.example"},
                    {"name": "To", "value": connection.gmail_address},
                    {"name": "Subject", "value": "Delivery Status Notification"},
                ],
            },
        }
        client = _fake_gmail_client(["m1"], {"m1": message})

        called = []
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch.object(gmail_residue, "_post_json", side_effect=lambda *a, **kw: called.append(1)):
            stats = gmail_live.run_rescan(connection, dry_run=True)

        assert called == []  # no AI call made under dry-run
        assert stats["residue"]["residue_threads_processed"] == 0


# ---------------------------------------------------------------------------
# The gmail_backfill command's SEPARATE rescan selection
# ---------------------------------------------------------------------------
class TestGmailBackfillCommandRescanSelection:
    def test_picks_up_pending_rescans_independently_of_backfill_status(self, student, connection):
        connection.backfill_status = "done"  # already done — must stay done
        connection.rescan_status = "pending"
        connection.save(update_fields=["backfill_status", "rescan_status"])

        client = _fake_gmail_client([], {})
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live.is_configured", return_value=True):
            call_command("gmail_backfill")

        connection.refresh_from_db()
        assert connection.rescan_status == "done"
        assert connection.rescan_completed_at is not None
        assert connection.backfill_status == "done"  # untouched, still sticky

    def test_a_connection_only_needing_backfill_does_not_get_a_rescan_status_change(self, student, connection):
        connection.backfill_status = "pending"
        connection.rescan_status = "none"
        connection.save(update_fields=["backfill_status", "rescan_status"])

        client = _fake_gmail_client([], {})
        with patch.object(gmail_live, "_gmail_client", return_value=client), \
             patch("capture.management.commands.gmail_backfill.gmail_live.is_configured", return_value=True):
            call_command("gmail_backfill")

        connection.refresh_from_db()
        assert connection.backfill_status == "done"
        assert connection.rescan_status == "none"  # never touched — wasn't queued
