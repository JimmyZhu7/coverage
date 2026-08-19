"""Phase 1 of the CSV-import enrichment feature: `accounts.services.
import_contacts` triggering a scoped, zero-AI `gmail_live.backfill_new_contacts`
pass right after a CSV import creates contacts. See `capture/gmail_live.py`'s
`backfill_connection`/`backfill_new_contacts` docstrings and
`accounts/services.py::import_contacts` for the wiring.

``transaction=True`` for the same reason `test_gmail_backfill.py` needs it:
applying a finding calls `crm.services.log_touch`, which opens its own
psycopg connection and cannot see rows written inside pytest's wrapping
transaction.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model

from accounts import services
from capture import gmail_live
from capture.models import GmailConnection
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def student(db):
    return User.objects.create_user(email="import-student@example.com", password="x")


def _fake_gmail_client(message_ids: list[str], messages_by_id: dict[str, dict]):
    """A MagicMock standing in for `build("gmail", "v1", ...)` — identical
    shape to `test_gmail_backfill.py`'s helper of the same name."""
    client = MagicMock()
    client.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": mid} for mid in message_ids],
    }

    def _get(*, userId, id, format):  # noqa: A002 - matches the real API's kwarg name
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


CSV_ONE_CONTACT = "name,email\nJane Banker,jane@bank.example\n"


class TestImportTriggersScopedBackfill:
    def test_import_scans_gmail_and_a_replying_contact_gets_a_touch_and_warmer(self, student):
        """The headline case: a student imports a contact they've already
        emailed, and that history really is in Gmail. The import must not
        leave them looking like a fresh, cold, un-contacted row."""
        GmailConnection.all_objects.create(
            user=student,
            gmail_address="import-student@example.com",
            refresh_token_encrypted="unused-in-these-tests",
            status="active",
        )
        message = _message(
            from_addr="Jane Banker <jane@bank.example>",
            to_addr="import-student@example.com",
            subject="Re: coffee chat",
            snippet="Would love to chat next week!",
            internal_date_ms=1_700_000_000_000,
        )
        client = _fake_gmail_client(["m1"], {"m1": message})

        with patch.object(gmail_live, "is_configured", return_value=True), \
             patch.object(gmail_live, "_gmail_client", return_value=client):
            result = services.import_contacts(
                student, file_bytes=CSV_ONE_CONTACT.encode("utf-8"), filename="c.csv"
            )

        assert result.created == 1
        contact = Contact.objects.for_user(student).get(email="jane@bank.example")
        assert Touch.objects.for_user(student).filter(contact=contact, kind="reply_received").exists()
        assert contact.warmth != "cold"  # default model warmth, per crm/models.py

    def test_import_never_touches_the_originals_backfill_status(self, student):
        """A scoped, import-triggered scan is a different action from the
        ONE-TIME original post-connect backfill — it must never flip
        `backfill_status`, which is sticky and means something else
        entirely (see `backfill_connection`'s docstring)."""
        connection = GmailConnection.all_objects.create(
            user=student,
            gmail_address="import-student@example.com",
            refresh_token_encrypted="unused-in-these-tests",
            status="active",
            backfill_status="pending",
        )
        message = _message(
            from_addr="Jane Banker <jane@bank.example>",
            to_addr="import-student@example.com",
            subject="Re: coffee chat",
            snippet="Sure, let's chat!",
            internal_date_ms=1_700_000_000_000,
        )
        client = _fake_gmail_client(["m1"], {"m1": message})

        with patch.object(gmail_live, "is_configured", return_value=True), \
             patch.object(gmail_live, "_gmail_client", return_value=client):
            services.import_contacts(
                student, file_bytes=CSV_ONE_CONTACT.encode("utf-8"), filename="c.csv"
            )

        connection.refresh_from_db()
        assert connection.backfill_status == "pending"  # unchanged

    def test_import_gracefully_no_ops_when_gmail_live_is_not_connected(self, student):
        """The other half of the contract: no connection at all must not
        raise, and must leave the import's own success untouched."""
        result = services.import_contacts(
            student, file_bytes=CSV_ONE_CONTACT.encode("utf-8"), filename="c.csv"
        )
        assert result.created == 1
        contact = Contact.objects.for_user(student).get(email="jane@bank.example")
        assert not Touch.objects.for_user(student).filter(contact=contact).exists()
        assert contact.warmth == "cold"

    def test_import_gracefully_no_ops_when_the_scoped_scan_itself_errors(self, student, monkeypatch):
        """An import's own success/failure must be independent of whether
        Gmail scanning worked — a live API error scanning history must
        never fail (or even flag) the CSV import itself."""
        GmailConnection.all_objects.create(
            user=student,
            gmail_address="import-student@example.com",
            refresh_token_encrypted="unused-in-these-tests",
            status="active",
        )

        def _boom(connection):
            raise RuntimeError("Gmail API is down")

        monkeypatch.setattr(gmail_live, "is_configured", lambda: True)
        monkeypatch.setattr(gmail_live, "_gmail_client", _boom)

        result = services.import_contacts(
            student, file_bytes=CSV_ONE_CONTACT.encode("utf-8"), filename="c.csv"
        )
        assert result.created == 1
        assert Contact.objects.for_user(student).filter(email="jane@bank.example").exists()

    def test_backfill_new_contacts_is_a_noop_with_no_contacts(self, student):
        assert gmail_live.backfill_new_contacts(student, []) is None
        assert gmail_live.backfill_new_contacts(student, None) is None
