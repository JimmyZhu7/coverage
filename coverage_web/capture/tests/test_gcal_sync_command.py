"""`manage.py gcal_sync` — the command's ergonomics, not the sync itself.

What is worth pinning here is the SHAPE: dry by default, `--apply` to
write, `--user` to narrow, and a summary line that always renders. The
mapping of a Google event onto a `CalendarEvent` is test_gcal_sync.py's
job; this file only checks that the command reaches it with the right
arguments and reports honestly.

Offline, like everything else in this directory: the Calendar client is
replaced at `gcal_live._calendar_client`.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from capture import gcal_live
from capture.models import GoogleCalendarConnection
from crm.models import CalendarEvent

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def configured(settings):
    settings.GCAL_LIVE_ENABLED = True
    settings.GMAIL_LIVE_CLIENT_ID = "client-id"
    settings.GMAIL_LIVE_CLIENT_SECRET = "client-secret"
    settings.GMAIL_LIVE_TOKEN_KEY = "key"
    return settings


@pytest.fixture
def student(db):
    return User.objects.create_user(email="cmd-student@example.com", password="x")


@pytest.fixture
def connection(student):
    return GoogleCalendarConnection.all_objects.create(
        user=student, google_email="cmd-student@example.com",
        refresh_token_encrypted="unused",
    )


def _client_with(items):
    client = MagicMock()
    client.events.return_value.list.return_value.execute.return_value = {
        "items": items, "nextSyncToken": "tok-1",
    }
    return client


def _event(event_id="g1", summary="Interview"):
    return {
        "id": event_id, "status": "confirmed", "summary": summary,
        "start": {"dateTime": "2026-09-10T09:00:00-07:00"},
        "end": {"dateTime": "2026-09-10T10:00:00-07:00"},
    }


def _run(*args, client=None, **opts) -> str:
    out = StringIO()
    ctx = (
        patch.object(gcal_live, "_calendar_client", return_value=client)
        if client is not None else patch.object(gcal_live, "_calendar_client")
    )
    with ctx:
        call_command("gcal_sync", *args, stdout=out, stderr=StringIO(), **opts)
    return out.getvalue()


class TestDryByDefault:
    def test_no_flag_means_no_writes(self, configured, student, connection):
        out = _run(client=_client_with([_event()]))

        assert not CalendarEvent.all_objects.filter(user=student).exists()
        assert "dry run, nothing written" in out
        assert "Re-run with --apply" in out

    def test_apply_writes(self, configured, student, connection):
        out = _run("--apply", client=_client_with([_event()]))

        assert CalendarEvent.all_objects.filter(user=student).count() == 1
        assert "applied" in out
        assert "Re-run with --apply" not in out


class TestTheSummaryLine:
    def test_it_always_renders_even_when_nothing_happened(self, configured, connection):
        out = _run(client=_client_with([]))

        assert "1 calendar:" in out
        assert "0 created" in out
        assert "0 updated" in out

    def test_it_counts_what_it_did(self, configured, connection):
        out = _run("--apply", client=_client_with([_event("g1"), _event("g2", "Superday")]))

        assert "2 created" in out


class TestUserSelection:
    def test_user_narrows_to_one_students_calendar(self, configured, connection, db):
        other = User.objects.create_user(email="other@example.com", password="x")
        GoogleCalendarConnection.all_objects.create(
            user=other, google_email="other@example.com", refresh_token_encrypted="unused",
        )

        out = _run("--user", "cmd-student@example.com", client=_client_with([]))

        assert "1 calendar:" in out

    def test_every_connection_by_default(self, configured, connection, db):
        other = User.objects.create_user(email="other@example.com", password="x")
        GoogleCalendarConnection.all_objects.create(
            user=other, google_email="other@example.com", refresh_token_encrypted="unused",
        )

        out = _run(client=_client_with([]))

        assert "2 calendars:" in out

    def test_an_unknown_email_is_an_error_not_a_silent_no_op(self, configured, connection):
        with pytest.raises(CommandError):
            _run("--user", "nobody@example.com", client=_client_with([]))


class TestItDoesNotFailWhenThereIsNothingToDo:
    def test_an_unconfigured_deploy_says_so_and_exits_cleanly(self, settings, connection):
        settings.GCAL_LIVE_ENABLED = False
        out = _run()

        assert "not configured" in out

    def test_no_connections_is_not_an_error(self, configured):
        out = _run()

        assert "No active calendar connections." in out

    def test_a_revoked_connection_is_skipped(self, configured, connection):
        connection.status = "revoked"
        connection.save(update_fields=["status"])

        out = _run(client=_client_with([]))

        assert "No active calendar connections." in out


class TestFaultIsolation:
    def test_one_broken_grant_does_not_stop_the_pass(self, configured, connection, db):
        """A student whose grant went away must not cost every other student
        their sync."""
        other = User.objects.create_user(email="other@example.com", password="x")
        GoogleCalendarConnection.all_objects.create(
            user=other, google_email="other@example.com", refresh_token_encrypted="unused",
        )

        calls = {"n": 0}

        def _sync(conn, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise gcal_live.GcalError("grant is gone")
            return gcal_live.GcalSyncResult(created=1)

        out = StringIO()
        err = StringIO()
        with patch.object(gcal_live, "sync_connection", side_effect=_sync):
            call_command("gcal_sync", stdout=out, stderr=err)

        assert calls["n"] == 2
        assert "1 failed" in out.getvalue()
        assert "grant is gone" in err.getvalue()
