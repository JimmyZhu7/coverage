"""`gmail_live.connect_gmail` — the OAuth callback's own write.

Regression coverage for a real bug found 2026-08-19: `connect_gmail` wrote
the new/updated `GmailConnection` through `GmailConnection.objects.
update_or_create(...)` — the tenant-SCOPED manager (`coverage_web.tenancy.
TenantManager`). That manager refuses to build a queryset at all unless the
call goes through `.for_user(user)` first; every other queryset method,
`update_or_create` included, reaches `get_queryset()` and raises
`TenantScopeError` unconditionally. Nothing in the existing test suite
called `connect_gmail` all the way through its DB write (test_gmail_live.py
is explicitly pure-function/no-database, per its own module docstring), so
this broke every real Gmail connect attempt without a single test noticing.

Google, `Flow` and the Gmail client are all mocked — this is only testing
what happens once the OAuth code exchange has already succeeded and
`connect_gmail` starts writing to the database.
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
def student(db):
    return User.objects.create_user(email="connect-student@example.com", password="x")


def _fake_flow(refresh_token: str = "1//fake-refresh-token") -> MagicMock:
    flow = MagicMock()
    flow.credentials = MagicMock(refresh_token=refresh_token)
    return flow


def _fake_gmail_client() -> MagicMock:
    client = MagicMock()
    client.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "connect-student@example.com",
        "historyId": "1000",
    }
    client.users.return_value.watch.return_value.execute.return_value = {
        "historyId": "1001",
        "expiration": "9999999999999",
    }
    return client


class TestConnectGmail:
    def test_first_connect_creates_a_connection_without_a_tenant_scope_error(self, student, settings):
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        fake_gmail = _fake_gmail_client()
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail), \
             patch.object(gmail_live, "_gmail_client", return_value=fake_gmail):
            connection = gmail_live.connect_gmail(student, "auth-code", "https://x/callback")

        assert connection.user_id == student.id
        assert connection.gmail_address == "connect-student@example.com"
        assert connection.status == "active"
        assert GmailConnection.all_objects.filter(user=student).count() == 1

    def test_reconnect_updates_the_existing_connection_in_place(self, student, settings):
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        GmailConnection.all_objects.create(
            user=student,
            gmail_address="old-address@example.com",
            refresh_token_encrypted="stale",
            backfill_status="failed",
        )
        fake_gmail = _fake_gmail_client()
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail), \
             patch.object(gmail_live, "_gmail_client", return_value=fake_gmail):
            connection = gmail_live.connect_gmail(student, "auth-code", "https://x/callback")

        assert GmailConnection.all_objects.filter(user=student).count() == 1
        assert connection.gmail_address == "connect-student@example.com"
        assert connection.backfill_status == "pending"
