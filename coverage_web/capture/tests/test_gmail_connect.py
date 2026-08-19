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
from django.urls import reverse
from googleapiclient.errors import HttpError

from capture import gmail_live
from capture.models import GmailConnection

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def student(db):
    return User.objects.create_user(email="connect-student@example.com", password="x")


class _FakeResp:
    """The `resp` half of a googleapiclient HttpError — `register_watch`
    branches on `exc.resp.status`, so that is the only attribute that has to
    be real."""

    def __init__(self, status: int):
        self.status = status
        self.reason = "error"

    def get(self, key, default=None):
        return {"status": self.status, "content-type": "application/json"}.get(key, default)


def _http_error(status: int, message: str) -> HttpError:
    body = ('{"error": {"code": %d, "message": "%s"}}' % (status, message)).encode()
    return HttpError(_FakeResp(status), body, uri="https://gmail.googleapis.com/")


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


class TestConnectGmailConfigFailures:
    """Every remaining way a real, live connect can fail is a CONFIG problem
    on the Google side — and `capture.views.gmail_callback` only catches
    `GmailLiveError`. Anything else escaping `connect_gmail` renders the
    generic 500 page ("Something broke on our side") with no explanation of
    which of the five setup steps in docs/gmail-live-setup.md was missed.
    `connect_gmail`'s own docstring already promises `GmailLiveError` "on
    anything that leaves the user without a working connection"; these are
    the paths that did not honour it.
    """

    def test_gmail_api_disabled_on_the_project_is_a_legible_error(self, student, settings):
        """getProfile 403s with "Gmail API has not been used in project N
        before or it is disabled" until the API is enabled in Cloud Console —
        the single most common first-connect failure."""
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        fake_gmail = MagicMock()
        fake_gmail.users.return_value.getProfile.return_value.execute.side_effect = (
            _http_error(403, "Gmail API has not been used in project 1234 before")
        )
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail):
            with pytest.raises(gmail_live.GmailLiveError) as exc_info:
                gmail_live.connect_gmail(student, "auth-code", "https://x/callback")

        assert "Gmail API" in str(exc_info.value)
        assert GmailConnection.all_objects.filter(user=student).count() == 0

    def test_a_malformed_token_key_is_a_legible_error_not_a_valueerror(self, student, settings):
        """`is_configured()` only checks GMAIL_LIVE_TOKEN_KEY is non-empty.
        A key that isn't 32 url-safe-base64 bytes makes `Fernet(...)` raise a
        bare ValueError from inside `encrypt_token`."""
        settings.GMAIL_LIVE_TOKEN_KEY = "not-a-real-fernet-key"
        fake_gmail = _fake_gmail_client()
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail), \
             patch.object(gmail_live, "_gmail_client", return_value=fake_gmail):
            with pytest.raises(gmail_live.GmailLiveError) as exc_info:
                gmail_live.connect_gmail(student, "auth-code", "https://x/callback")

        assert "GMAIL_LIVE_TOKEN_KEY" in str(exc_info.value)

    def test_a_broken_pubsub_topic_does_not_throw_away_a_working_connection(
        self, student, settings
    ):
        """`users.watch()` 400s when the Pub/Sub topic doesn't exist or
        hasn't granted publish rights to gmail-api-push@system.gserviceaccount
        .com. `register_watch` only special-cases 401/403 and re-raises
        everything else — which used to escape `connect_gmail` AFTER the
        connection row had already been committed (no ATOMIC_REQUESTS), so
        the user saw a 500 for a mailbox that was, in fact, connected.

        The watch is the one part of a connection that retries on its own:
        `renew_watches()` re-picks-up every active row with a null
        `watch_expiration`, daily. Losing it must not lose the connect.
        """
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        fake_gmail = _fake_gmail_client()
        fake_gmail.users.return_value.watch.return_value.execute.side_effect = (
            _http_error(400, "Error sending test message to Cloud PubSub")
        )
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail), \
             patch.object(gmail_live, "_gmail_client", return_value=fake_gmail):
            connection = gmail_live.connect_gmail(student, "auth-code", "https://x/callback")

        assert connection.status == "active"
        assert connection.gmail_address == "connect-student@example.com"
        assert connection.backfill_status == "pending"
        # Null expiration is exactly what renew_watches()'s
        # `watch_expiration__isnull=True` arm re-picks-up on the next daily tick.
        assert connection.watch_expiration is None
        assert GmailConnection.all_objects.filter(user=student).count() == 1

    def test_a_dead_refresh_at_watch_time_does_not_throw_away_the_connection(
        self, student, settings
    ):
        """`register_watch` -> `_credentials` -> `creds.refresh()` raises
        `google.auth.exceptions.RefreshError`, which is not an HttpError at
        all, so even the 401/403 arm never sees it."""
        from google.auth.exceptions import RefreshError

        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        fake_gmail = _fake_gmail_client()
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail), \
             patch.object(gmail_live, "_gmail_client", side_effect=RefreshError("bad grant")):
            connection = gmail_live.connect_gmail(student, "auth-code", "https://x/callback")

        assert connection.status == "active"
        assert connection.watch_expiration is None


class TestGmailCallbackNeverRenders500:
    """The view-level half of the same guarantee: whatever Google is
    misconfigured, the callback redirects to Settings with a message. It
    never falls through to Django's 500 handler."""

    def _consented(self, client, student):
        client.force_login(student)
        session = client.session
        session["gmail_live_oauth_state"] = "state-123"
        session.save()
        return f"{reverse('capture:gmail_callback')}?code=abc&state=state-123"

    def test_callback_survives_a_broken_pubsub_topic(self, client, student, settings):
        settings.GMAIL_LIVE_CLIENT_ID = "cid"
        settings.GMAIL_LIVE_CLIENT_SECRET = "csecret"
        settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        url = self._consented(client, student)

        fake_gmail = _fake_gmail_client()
        fake_gmail.users.return_value.watch.return_value.execute.side_effect = (
            _http_error(400, "Error sending test message to Cloud PubSub")
        )
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail), \
             patch.object(gmail_live, "_gmail_client", return_value=fake_gmail):
            response = client.get(url)

        assert response.status_code == 302
        assert GmailConnection.all_objects.filter(user=student).count() == 1

    def test_callback_survives_the_gmail_api_being_disabled(self, client, student, settings):
        settings.GMAIL_LIVE_CLIENT_ID = "cid"
        settings.GMAIL_LIVE_CLIENT_SECRET = "csecret"
        settings.GMAIL_LIVE_PUBSUB_TOPIC = "projects/p/topics/t"
        settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
        url = self._consented(client, student)

        fake_gmail = MagicMock()
        fake_gmail.users.return_value.getProfile.return_value.execute.side_effect = (
            _http_error(403, "Gmail API has not been used in project 1234 before")
        )
        with patch.object(gmail_live, "_flow", return_value=_fake_flow()), \
             patch.object(gmail_live, "build", return_value=fake_gmail):
            response = client.get(url)

        assert response.status_code == 302
        assert GmailConnection.all_objects.filter(user=student).count() == 0
