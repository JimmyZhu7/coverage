"""`capture.gcal_live` — the read-only Google Calendar mirror.

OFFLINE, ALWAYS. Every test here injects a fake `events()` client through
`gcal_live._calendar_client`, the same seam `test_gmail_backfill.py` uses
for `gmail_live._gmail_client`. Nothing in this file reaches Google, and
nothing needs a real grant: the credential half is exercised separately in
test_gcal_connect.py with the flow mocked.

`transaction=True` for the same reason the Gmail integration tests need it —
these assert against rows written through managers that open their own
connections.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from googleapiclient.errors import HttpError

from capture import gcal_live
from capture.models import GoogleCalendarConnection
from crm.models import CalendarEvent, Contact

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def student(db):
    # America/Los_Angeles is this account's real zone, and it is load-bearing
    # in the all-day and naive-time tests below rather than decoration: it is
    # 7-8 hours off UTC, so a midnight computed in the wrong zone lands on the
    # wrong DAY and the assertion catches it.
    return User.objects.create_user(
        email="cal-student@example.com", password="x", timezone="America/Los_Angeles"
    )


@pytest.fixture
def connection(student):
    return GoogleCalendarConnection.all_objects.create(
        user=student,
        google_email="cal-student@example.com",
        refresh_token_encrypted="unused-in-these-tests",
    )


def _event(
    *, event_id: str, summary: str = "Coffee chat", start: dict | None = None,
    end: dict | None = None, ical_uid: str = "", status: str = "confirmed",
    location: str = "", description: str = "",
) -> dict:
    event = {
        "id": event_id,
        "status": status,
        "summary": summary,
        "start": start if start is not None else {"dateTime": "2026-09-10T09:00:00-07:00"},
        "end": end if end is not None else {"dateTime": "2026-09-10T09:30:00-07:00"},
    }
    if ical_uid:
        event["iCalUID"] = ical_uid
    if location:
        event["location"] = location
    if description:
        event["description"] = description
    return event


def _fake_client(pages: list[dict]):
    """A MagicMock standing in for `build("calendar", "v3", ...)`.

    `pages` are returned in order from successive `events().list().execute()`
    calls, so a test can drive paging, a fresh window read followed by an
    incremental one, or a 410 followed by a re-read.
    """
    client = MagicMock()
    listing = client.events.return_value.list
    listing.return_value.execute.side_effect = list(pages)
    return client


def _page(items: list[dict], *, next_sync_token: str | None = "tok-1",
          next_page_token: str | None = None) -> dict:
    page = {"items": items}
    if next_page_token:
        page["nextPageToken"] = next_page_token
    elif next_sync_token:
        page["nextSyncToken"] = next_sync_token
    return page


class _FakeResp:
    def __init__(self, status: int):
        self.status = status
        self.reason = "error"

    def get(self, key, default=None):
        return {"status": self.status, "content-type": "application/json"}.get(key, default)


def _http_error(status: int) -> HttpError:
    body = ('{"error": {"code": %d, "message": "gone"}}' % status).encode()
    return HttpError(_FakeResp(status), body, uri="https://www.googleapis.com/calendar/v3/")


def _sync(connection, client, **kwargs):
    with patch.object(gcal_live, "_calendar_client", return_value=client):
        return gcal_live.sync_connection(connection, **kwargs)


# ---------------------------------------------------------------------------
class TestCreatesAndUpdates:
    def test_a_google_event_lands_on_the_timeline(self, student, connection):
        client = _fake_client([_page([_event(
            event_id="g1", summary="Superday prep", location="Zoom",
        )])])

        result = _sync(connection, client)

        assert result.created == 1
        event = CalendarEvent.all_objects.get(user=student, external_id="g1")
        assert event.title == "Superday prep"
        assert event.location == "Zoom"
        assert event.source == CalendarEvent.SOURCE_GCAL
        # A FIELD ON THE EVENT SAID SO. This is the whole reason a synced
        # event cannot be dragged around by a prose reading later.
        assert event.time_confidence == 1.0
        assert event.time_evidence == ""

    def test_the_cursor_is_stored_so_the_next_run_is_incremental(self, connection):
        client = _fake_client([_page([_event(event_id="g1")], next_sync_token="tok-42")])

        _sync(connection, client)

        connection.refresh_from_db()
        assert connection.sync_token == "tok-42"
        assert connection.last_synced_at is not None
        assert connection.last_sync_stats["created"] == 1

    def test_the_cursor_is_only_read_off_the_last_page(self, connection):
        """Google issues `nextSyncToken` on the final page only. Reading it
        early and stopping would store a cursor that skips every event after
        it — the gap would be permanent, because an incremental read never
        offers those events again."""
        client = _fake_client([
            _page([_event(event_id="g1")], next_sync_token=None, next_page_token="p2"),
            _page([_event(event_id="g2", summary="Second page")], next_sync_token="tok-final"),
        ])

        result = _sync(connection, client)

        assert result.created == 2
        connection.refresh_from_db()
        assert connection.sync_token == "tok-final"

    def test_re_reading_the_same_event_changes_nothing_and_says_so(self, connection):
        client = _fake_client([_page([_event(event_id="g1")])])
        _sync(connection, client)

        again = _fake_client([_page([_event(event_id="g1")])])
        result = _sync(connection, again)

        assert result.created == 0
        assert result.updated == 0
        assert result.unchanged == 1
        assert CalendarEvent.all_objects.filter(external_id="g1").count() == 1

    def test_a_moved_event_moves_the_row_rather_than_adding_one(self, student, connection):
        client = _fake_client([_page([_event(event_id="g1")])])
        _sync(connection, client)

        moved = _fake_client([_page([_event(
            event_id="g1", start={"dateTime": "2026-09-11T14:00:00-07:00"},
            end={"dateTime": "2026-09-11T14:30:00-07:00"},
        )])])
        result = _sync(connection, moved)

        assert result.updated == 1
        assert CalendarEvent.all_objects.filter(user=student).count() == 1
        event = CalendarEvent.all_objects.get(external_id="g1")
        assert event.starts_at.astimezone(dt_timezone.utc).day == 11

    def test_an_event_with_no_usable_start_is_skipped_not_invented(self, connection):
        client = _fake_client([_page([_event(event_id="g1", start={})])])

        result = _sync(connection, client)

        assert result.skipped == 1
        assert not CalendarEvent.all_objects.filter(external_id="g1").exists()

    def test_a_nameless_event_gets_a_title_rather_than_a_blank_chip(self, connection):
        client = _fake_client([_page([_event(event_id="g1", summary="")])])

        _sync(connection, client)

        assert CalendarEvent.all_objects.get(external_id="g1").title == gcal_live.UNTITLED


class TestRecurringSeries:
    """`singleEvents=True` expands "every Tuesday" into one event per
    Tuesday, each with its own id but ALL carrying the series' one iCalUID.
    Keyed on that UID they would collapse onto a single row that jumped
    forward a week on every sync — or, since `ics_uid` is unique per user,
    blow the whole pass up with an IntegrityError."""

    def _instance(self, *, event_id, day, uid="series-uid@google.com"):
        return {
            "id": event_id,
            "status": "confirmed",
            "summary": "Weekly standing call",
            "recurringEventId": "series-1",
            "iCalUID": uid,
            "start": {"dateTime": f"2026-09-{day:02d}T09:00:00-07:00"},
            "end": {"dateTime": f"2026-09-{day:02d}T09:30:00-07:00"},
        }

    def test_each_occurrence_gets_its_own_row(self, student, connection):
        client = _fake_client([_page([
            self._instance(event_id="series-1_20260908", day=8),
            self._instance(event_id="series-1_20260915", day=15),
            self._instance(event_id="series-1_20260922", day=22),
        ])])

        result = _sync(connection, client)

        assert result.created == 3
        assert CalendarEvent.all_objects.filter(user=student).count() == 3

    def test_an_instance_stores_no_ics_uid_to_collide_on(self, connection):
        client = _fake_client([_page([self._instance(event_id="series-1_20260908", day=8)])])

        _sync(connection, client)

        assert CalendarEvent.all_objects.get(external_id="series-1_20260908").ics_uid == ""

    def test_a_one_off_event_still_keeps_its_uid_for_the_mailbox_join(self, connection):
        client = _fake_client([_page([_event(event_id="g1", ical_uid="one-off@google.com")])])

        _sync(connection, client)

        assert CalendarEvent.all_objects.get(external_id="g1").ics_uid == "one-off@google.com"


# ---------------------------------------------------------------------------
class TestTimezones:
    def test_an_all_day_event_lands_at_local_midnight_on_the_stated_day(
        self, student, connection
    ):
        """`{"date": "2026-09-14"}` is a DAY, not a moment. Anchored in UTC
        it becomes 2026-09-14T00:00Z, which is the 13th at 5pm in Los
        Angeles — the event would render on the wrong day of the grid."""
        client = _fake_client([_page([_event(
            event_id="g1", summary="Superday",
            start={"date": "2026-09-14"}, end={"date": "2026-09-15"},
        )])])

        _sync(connection, client)

        event = CalendarEvent.all_objects.get(external_id="g1")
        assert event.all_day is True
        local = event.starts_at.astimezone(gcal_live._zone_of(student))
        assert (local.year, local.month, local.day) == (2026, 9, 14)
        assert (local.hour, local.minute) == (0, 0)

    def test_a_dateTime_with_an_offset_is_taken_at_its_word(self, connection):
        client = _fake_client([_page([_event(
            event_id="g1", start={"dateTime": "2026-09-10T09:00:00-07:00"},
        )])])

        _sync(connection, client)

        starts = CalendarEvent.all_objects.get(external_id="g1").starts_at
        assert starts == datetime(2026, 9, 10, 16, 0, tzinfo=dt_timezone.utc)

    def test_a_naive_dateTime_anchors_to_the_events_own_zone(self, connection):
        """Google states a per-event `timeZone`. A 9am interview stated on
        Europe/London is 9am in London, whatever the student's own setting
        says — and it is certainly not 9am on the server's UTC."""
        client = _fake_client([_page([_event(
            event_id="g1",
            start={"dateTime": "2026-09-10T09:00:00", "timeZone": "Europe/London"},
            end={"dateTime": "2026-09-10T10:00:00", "timeZone": "Europe/London"},
        )])])

        _sync(connection, client)

        starts = CalendarEvent.all_objects.get(external_id="g1").starts_at
        # BST in September: UTC+1.
        assert starts == datetime(2026, 9, 10, 8, 0, tzinfo=dt_timezone.utc)

    def test_a_naive_dateTime_with_no_zone_falls_back_to_the_users(self, connection):
        client = _fake_client([_page([_event(
            event_id="g1", start={"dateTime": "2026-09-10T09:00:00"},
            end={"dateTime": "2026-09-10T10:00:00"},
        )])])

        _sync(connection, client)

        starts = CalendarEvent.all_objects.get(external_id="g1").starts_at
        # PDT in September: UTC-7.
        assert starts == datetime(2026, 9, 10, 16, 0, tzinfo=dt_timezone.utc)

    def test_an_unknown_zone_name_does_not_kill_the_sync(self, connection):
        client = _fake_client([_page([_event(
            event_id="g1",
            start={"dateTime": "2026-09-10T09:00:00", "timeZone": "Mars/Olympus"},
        )])])

        result = _sync(connection, client)

        assert result.created == 1


# ---------------------------------------------------------------------------
class TestCancellations:
    def test_a_cancelled_event_is_retired_not_deleted(self, student, connection):
        client = _fake_client([_page([_event(event_id="g1")])])
        _sync(connection, client)

        tombstone = _fake_client([_page([{"id": "g1", "status": "cancelled"}])])
        result = _sync(connection, tombstone)

        assert result.cancelled == 1
        event = CalendarEvent.all_objects.get(external_id="g1")
        # The row survives: it is a record of a meeting that really was on
        # the books, and a row that silently vanishes teaches a student to
        # distrust the whole calendar.
        assert event.cancelled_at is not None

    def test_a_second_cancellation_is_not_counted_twice(self, connection):
        _sync(connection, _fake_client([_page([_event(event_id="g1")])]))
        _sync(connection, _fake_client([_page([{"id": "g1", "status": "cancelled"}])]))

        result = _sync(connection, _fake_client([_page([{"id": "g1", "status": "cancelled"}])]))

        assert result.cancelled == 0
        assert result.unchanged == 1

    def test_a_cancellation_for_an_unknown_event_is_not_an_error(self, connection):
        client = _fake_client([_page([{"id": "never-seen", "status": "cancelled"}])])

        result = _sync(connection, client)

        assert result.skipped == 1
        assert result.cancelled == 0

    def test_an_event_google_reinstates_stops_reading_as_cancelled(self, connection):
        _sync(connection, _fake_client([_page([_event(event_id="g1")])]))
        _sync(connection, _fake_client([_page([{"id": "g1", "status": "cancelled"}])]))

        result = _sync(connection, _fake_client([_page([_event(event_id="g1")])]))

        assert result.updated == 1
        assert CalendarEvent.all_objects.get(external_id="g1").cancelled_at is None


# ---------------------------------------------------------------------------
class TestItDoesNotFightTheMailboxPath:
    """The failure this whole join exists to prevent: a coffee chat that the
    mailbox captured off an `.ics` AND the student then accepted onto their
    Google Calendar, showing up twice in Coverage — once from each pipeline,
    at whichever times each happened to read."""

    @pytest.fixture
    def contact(self, student):
        return Contact.all_objects.create(
            user=student, name="Jane Banker", email="jane@bank.example", source="manual"
        )

    def test_a_synced_event_adopts_the_row_the_invite_already_made(
        self, student, connection, contact
    ):
        captured = CalendarEvent.all_objects.create(
            user=student,
            title="Chat with Jane Banker",
            ics_uid="uid-abc@google.com",
            thread_id="thread-1",
            starts_at=timezone.now() + timedelta(days=3),
            kind=CalendarEvent.KIND_CHAT,
            source=CalendarEvent.SOURCE_CAPTURE,
            contact=contact,
        )

        client = _fake_client([_page([_event(
            event_id="g1", summary="Jimmy <> Jane", ical_uid="uid-abc@google.com",
        )])])
        result = _sync(connection, client)

        assert result.adopted == 1
        assert result.created == 0
        assert CalendarEvent.all_objects.filter(user=student).count() == 1

        captured.refresh_from_db()
        assert captured.external_id == "g1"
        # THE CONTACT SURVIVES. Coverage learned who this chat is with from
        # the mail, not from Google's attendee list, and that link is what
        # makes the row useful on the Today page.
        assert captured.contact_id == contact.id
        assert captured.kind == CalendarEvent.KIND_CHAT
        # And so does the title that names the person — Google's summary
        # usually does not.
        assert captured.title == "Chat with Jane Banker"

    def test_adopting_promotes_a_prose_guess_to_a_stated_time(
        self, student, connection, contact
    ):
        """A row `capture.chattime` read out of a sentence sits at 0.6 and
        quotes that sentence. A field on a real calendar event settles the
        question, so the provenance is promoted and the quote goes with it —
        it is no longer what the row is claiming."""
        guessed = CalendarEvent.all_objects.create(
            user=student,
            title="Chat with Jane Banker",
            ics_uid="uid-abc@google.com",
            starts_at=timezone.now() + timedelta(days=3),
            kind=CalendarEvent.KIND_CHAT,
            source=CalendarEvent.SOURCE_CAPTURE,
            contact=contact,
            time_confidence=0.6,
            time_evidence="6pm tomorrow works great for me",
        )

        client = _fake_client([_page([_event(
            event_id="g1", ical_uid="uid-abc@google.com",
        )])])
        _sync(connection, client)

        guessed.refresh_from_db()
        assert guessed.time_confidence == 1.0
        assert guessed.time_evidence == ""
        assert guessed.time_reported is False

    def test_a_prose_reading_cannot_move_a_synced_event(self, student, connection, contact):
        """The other direction, and it needed no new code: a synced row is
        written at `time_confidence` 1.0, and
        `capture.gmail._upsert_scheduled_chat` already refuses to let a prose
        reading overwrite a stated time. This test pins that the guard covers
        calendar rows too, not just `.ics` ones."""
        from capture.gmail import _upsert_scheduled_chat

        _sync(connection, _fake_client([_page([_event(
            event_id="g1", ical_uid="uid-abc@google.com",
        )])]))
        event = CalendarEvent.all_objects.get(external_id="g1")
        stated_at = event.starts_at

        moved = _upsert_scheduled_chat(student, contact, {
            "ics_uid": "uid-abc@google.com",
            "thread_id": "thread-9",
            "chat_scheduled_at": "2026-09-20T17:30:00",
            "occurred_at": "2026-09-19T12:00:00",
            "prose_time": {
                "kind": "booking", "dated": True, "confidence": 0.6,
                "evidence": "5:30 on the 20th works",
            },
        })

        assert moved is False
        event.refresh_from_db()
        assert event.starts_at == stated_at
        assert event.time_confidence == 1.0


# ---------------------------------------------------------------------------
class TestCursorExpiry:
    def test_a_410_falls_back_to_a_full_window_read_on_the_same_pass(self, connection):
        """Google retires a sync token after its own retention window. A
        sync that returned "nothing changed" after a 410 would be lying
        about coverage — the correct answer is to re-read the window."""
        connection.sync_token = "stale"
        connection.save(update_fields=["sync_token"])

        client = MagicMock()
        client.events.return_value.list.return_value.execute.side_effect = [
            _http_error(410),
            _page([_event(event_id="g1")], next_sync_token="tok-new"),
        ]

        result = _sync(connection, client)

        assert result.resynced is True
        assert result.created == 1
        connection.refresh_from_db()
        assert connection.sync_token == "tok-new"

    def test_any_other_http_error_is_not_swallowed(self, connection):
        client = MagicMock()
        client.events.return_value.list.return_value.execute.side_effect = _http_error(500)

        with pytest.raises(HttpError):
            _sync(connection, client)


# ---------------------------------------------------------------------------
class TestRevokedGrant:
    def test_a_revoked_grant_marks_the_row_and_raises_a_readable_error(self, connection):
        from google.auth.exceptions import RefreshError

        with patch.object(gcal_live, "_calendar_client", side_effect=RefreshError("gone")):
            with pytest.raises(gcal_live.GcalError) as exc:
                gcal_live.sync_connection(connection)

        assert "reconnect" in str(exc.value).lower()
        connection.refresh_from_db()
        assert connection.status == "revoked"

    def test_a_dry_run_does_not_flip_the_status(self, connection):
        from google.auth.exceptions import RefreshError

        with patch.object(gcal_live, "_calendar_client", side_effect=RefreshError("gone")):
            with pytest.raises(gcal_live.GcalError):
                gcal_live.sync_connection(connection, dry_run=True)

        connection.refresh_from_db()
        assert connection.status == "active"


# ---------------------------------------------------------------------------
class TestDryRun:
    def test_a_dry_run_reports_but_writes_nothing(self, student, connection):
        client = _fake_client([_page([_event(event_id="g1")], next_sync_token="tok-1")])

        result = _sync(connection, client, dry_run=True)

        assert result.created == 1
        assert not CalendarEvent.all_objects.filter(user=student).exists()
        connection.refresh_from_db()
        assert connection.sync_token == ""
        assert connection.last_synced_at is None

    def test_a_dry_run_still_talks_to_google(self, connection):
        """Deliberate: a dry run that skipped the network could not tell a
        working connection from a revoked one, which is the single most
        useful thing it reports."""
        client = _fake_client([_page([])])

        _sync(connection, client, dry_run=True)

        assert client.events.return_value.list.called
