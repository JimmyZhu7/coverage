"""Google Calendar, mirrored one way: Google to Coverage, never back.

WHY THIS EXISTS. `CalendarEvent` already holds two kinds of row — a chat
Coverage read off an invite in the mailbox, and something the student typed
in — and its own docstring says the table renders "one timeline". The third
thing on a student's timeline is their actual calendar, and Coverage could
not see it. So a chat booked through a Calendly link, an interview an
advisor put on their diary, a superday: all invisible here, while the
Today page confidently reported an afternoon that was missing half of it.

READ-ONLY IS THE WHOLE POSTURE, NOT A SETTING.
----------------------------------------------
The grant is `calendar.readonly` (`settings.GCAL_LIVE_SCOPES`). There is no
function in this module — or anywhere in this project — that calls
`events().insert`, `.update`, `.move` or `.delete`. The Settings card can
say "view only" flatly because Google enforces it: a write attempt under
this scope is refused at the API, not at our own discretion. If that ever
needs to change, it changes in Cloud Console with a new consent screen and
a student re-consenting, which is exactly the friction it should have.

A SEPARATE GRANT FROM GMAIL, ON PURPOSE.
-----------------------------------------
Same OAuth client, same Fernet key, different consent and a different row.
`gmail_live` is untouched by this file: its scope list does not widen, its
connection record is not shared, and disconnecting one grant leaves the
other running. A student who wants mail sync and no calendar (or the
reverse) gets exactly that, because the two questions are asked separately.
`include_granted_scopes="false"` in `capture.google_oauth.auth_url` is what
holds that apart at Google's end — without it, incremental authorisation
would quietly fold the mail scope into the calendar token.

STATED TIMES OUTRANK READ ONES, AND THAT IS ALREADY THE RULE.
--------------------------------------------------------------
`CalendarEvent.time_confidence` exists for exactly this: 1.0 is "a
structured field said so, or the user typed it", 0.6 is "`capture.chattime`
read it out of a sentence somebody wrote". A Google event's start is a
field on the event — the most stated a time gets — so every row this module
writes sits at 1.0. Two consequences, both free, because the guard was
already there:

* `capture.gmail._upsert_scheduled_chat` refuses to let a prose reading
  overwrite a row at 1.0. So a mirrored event can never be dragged to
  "6pm tomorrow works great for me". Nothing was added for this; the write
  guard that protects an `.ics`-set time protects a synced one identically.
* Nothing here downgrades a row. A mirrored event that adopts a row an
  invite already created leaves it at 1.0 and simply keeps it current.

AND IT DOES NOT DUPLICATE THE INVITE PATH EITHER.
--------------------------------------------------
This is the failure worth naming, because it is what a naive sync does. A
banker sends a coffee-chat invite. `gmail_live` reads the `.ics` out of the
mail and writes a `CalendarEvent` with that invite's `UID`. The student
accepts, so the SAME meeting is now also on their Google Calendar — and a
sync keyed only on Google's event id happily writes a SECOND row for it.
The student opens Coverage and sees the same chat twice, which is precisely
the bug `_upsert_scheduled_chat` was rewritten to kill for counter-proposal
threads, arriving again through a different door.

Google returns `iCalUID` on every event, and it is the same UID the `.ics`
in the mailbox carried (RFC 5545 holds it constant across REQUEST / REPLY /
COUNTER / CANCEL). So the lookup goes: Google's event id first, the
meeting's iCalUID second, create only if neither matched. The second lookup
is the whole anti-duplication mechanism, and an adopted row keeps its
contact link — the person the chat is with is what makes it useful, and
Google's attendee list is not where Coverage learned that.

WHAT IT DELIBERATELY DOES NOT DO.
----------------------------------
* **No contact matching from attendees.** Tempting, and wrong here. This
  module's job is "put what the calendar says on the timeline"; deciding
  that an attendee is a `Contact` — or worth proposing as one — is
  `capture.discovery`'s judgment chain, which has its own evidence rules
  and its own dismissal ledger. A synced event that adopts a captured row
  inherits that row's contact and nothing invents one.
* **No push.** Google Calendar has a watch/channel API that needs a public
  HTTPS endpoint, which means "deploy Coverage" becomes a precondition.
  `gcal_sync` polls with a sync token instead, which is the same trade
  `gmail_poll` already made and for the same reason.
* **No writes, no RSVP.** See the top of this docstring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from capture import google_oauth
from capture.gmail_live import decrypt_token, encrypt_token
from capture.models import GoogleCalendarConnection
from crm.models import CalendarEvent

logger = logging.getLogger(__name__)

# Google's own page cap for `events.list`. Asking for more is silently
# clamped, so name it rather than let the sync's page count depend on a
# default that could move.
PAGE_SIZE = 250

# A title for an event whose `summary` Google withheld or that genuinely has
# none. "(No title)" rather than a blank chip: a nameless row on the grid
# reads as a rendering bug, and `CalendarEvent.title` is non-null anyway.
UNTITLED = "(No title)"


class GcalError(Exception):
    """Raised for conditions the caller must react to — a missing config, a
    grant Google has revoked. Deliberately this module's own type and not
    `GmailLiveError`: `capture.views.gmail_callback` catches that one, and
    a shared exception would have a calendar failure surfacing as a Gmail
    message on a Gmail card."""


def is_configured() -> bool:
    """Whether this deployment can connect a calendar at all.

    Three of the four checks are Gmail Live's credentials, because Calendar
    reuses that OAuth client and that Fernet key (see
    `settings.GCAL_LIVE_ENABLED`'s comment on why a second client would buy
    nothing). The fourth is the flag, and it is the one that matters: it
    stays False until the consent screen actually lists the calendar scope,
    so a student is never sent to a Google page that refuses the request and
    reads the refusal as Coverage being broken.
    """
    return bool(
        settings.GCAL_LIVE_ENABLED
        and settings.GMAIL_LIVE_CLIENT_ID
        and settings.GMAIL_LIVE_CLIENT_SECRET
        and settings.GMAIL_LIVE_TOKEN_KEY
    )


# ---------------------------------------------------------------------------
# OAuth connect / disconnect
# ---------------------------------------------------------------------------

def build_auth_url(redirect_uri: str, state: str) -> str:
    """The URL for the calendar consent screen. `GCAL_LIVE_SCOPES` only —
    this asks for read access to the calendar and nothing else, whatever the
    same client may already hold for mail."""
    return google_oauth.auth_url(
        client_id=settings.GMAIL_LIVE_CLIENT_ID,
        client_secret=settings.GMAIL_LIVE_CLIENT_SECRET,
        scopes=settings.GCAL_LIVE_SCOPES,
        redirect_uri=redirect_uri,
        state=state,
    )


def _flow(redirect_uri: str):
    """A fresh `Flow` per request, PKCE off — see `capture.google_oauth`.
    Split out as its own function for the same reason `gmail_live._flow` is:
    it is the seam the connect tests replace."""
    return google_oauth.flow(
        client_id=settings.GMAIL_LIVE_CLIENT_ID,
        client_secret=settings.GMAIL_LIVE_CLIENT_SECRET,
        scopes=settings.GCAL_LIVE_SCOPES,
        redirect_uri=redirect_uri,
    )


def connect_calendar(user, code: str, redirect_uri: str) -> GoogleCalendarConnection:
    """Exchange the consent code and store (or update) this user's calendar
    connection.

    Raises `GcalError` — and only `GcalError` — for anything that leaves the
    user without a working connection, because `capture.views.gcal_callback`
    catches exactly that and renders the message. Every `except` below is
    there so a fixable Cloud Console setting reaches the person who can fix
    it instead of the generic 500 page: that is the lesson `connect_gmail`
    learned the expensive way, applied here before it has to be.
    """
    flow = _flow(redirect_uri)
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 - surfaced as GcalError below
        raise GcalError(f"Google rejected the consent code: {exc}") from exc

    creds = flow.credentials
    if not creds.refresh_token:
        raise GcalError(
            "Google did not return a refresh token — revoke Coverage's "
            "access at https://myaccount.google.com/permissions and "
            "reconnect."
        )

    # The first real Calendar API call on this grant, and the one that fails
    # loudly and specifically when a setup step was missed: a 403 "Google
    # Calendar API has not been used in project N before or it is disabled"
    # is an `HttpError`, not a `GcalError`, and would otherwise escape to
    # the 500 page for a problem the user can fix in a minute.
    try:
        service = build("calendar", "v3", credentials=creds)
        primary = service.calendars().get(calendarId="primary").execute()
    except Exception as exc:  # noqa: BLE001 - surfaced as GcalError below
        raise GcalError(
            f"Google refused to read the calendar: {exc}. Check that the "
            "Google Calendar API is enabled on this OAuth client's Cloud "
            "project and that your account is on the app's test-user list."
        ) from exc

    connection, _ = GoogleCalendarConnection.all_objects.update_or_create(
        user=user,
        defaults={
            # `id` on a calendars.get("primary") response is the calendar's
            # real address, which is the account's own — the name to show on
            # the Settings card. `summary` is the calendar's display name and
            # is often just "user@example.com" too, but not always.
            "google_email": primary.get("id", "") or "",
            "refresh_token_encrypted": encrypt_token(creds.refresh_token),
            "calendar_id": "primary",
            # A RECONNECT STARTS CLEAN. The old cursor was issued against a
            # grant that is being replaced, and Google rejects a stale one
            # with a 410 anyway — clearing it here means the first sync after
            # a reconnect does its windowed read deliberately rather than
            # discovering the same thing through an error path.
            "sync_token": "",
            "status": "active",
            "connected_at": timezone.now(),
        },
    )
    return connection


def disconnect(user) -> int:
    """Hand the calendar grant back to Google, then delete the stored row.

    THROUGH THE SAME DOOR AS GMAIL, on purpose:
    `capture.google_revoke.revoke_connection` is best-effort and never
    raises, and it reads `refresh_token_encrypted` off whatever object it is
    given — the field is named the same on both connection models precisely
    so one revoke implementation covers both. A button labelled "Disconnect"
    that leaves a live grant at Google is telling the student something that
    is not so; that argument was settled for mail and it is the same
    argument here.

    Returns how many rows were removed, so a caller can tell "disconnected"
    from "there was nothing connected".
    """
    from capture import google_revoke

    rows = list(GoogleCalendarConnection.all_objects.filter(user=user))
    for connection in rows:
        google_revoke.revoke_connection(connection)
    GoogleCalendarConnection.all_objects.filter(user=user).delete()
    return len(rows)


# ---------------------------------------------------------------------------
# Client + time helpers
# ---------------------------------------------------------------------------

def _credentials(connection: GoogleCalendarConnection):
    return google_oauth.credentials(
        client_id=settings.GMAIL_LIVE_CLIENT_ID,
        client_secret=settings.GMAIL_LIVE_CLIENT_SECRET,
        refresh_token=decrypt_token(connection.refresh_token_encrypted),
        scopes=settings.GCAL_LIVE_SCOPES,
    )


def _calendar_client(connection: GoogleCalendarConnection):
    """The seam every test replaces. Same shape as
    `gmail_live._gmail_client`, and named to match so the two are recognisably
    the same kind of thing."""
    return build("calendar", "v3", credentials=_credentials(connection))


def _zone_of(user):
    """The user's own zone, with `capture.gmail._zone_of`'s exact fallback
    discipline — and this is a copy of that rule for the same reason it is
    stated there: `timezone.localtime()` converts to whatever zone is
    currently ACTIVE, which inside a management command is the server's UTC
    and never the account owner's.

    It is load-bearing twice in this module. An all-day event is a DATE with
    no clock ("Superday, the 14th"), stored as a datetime at local midnight
    — computed in UTC, that midnight lands on the 13th or the 15th for
    anybody west or east of it. And a Google event start can arrive without
    an offset, in which case the clock time it states means that clock time
    where the student is.
    """
    tzname = (getattr(user, "timezone", "") or "").strip()
    try:
        return ZoneInfo(tzname) if tzname else timezone.get_current_timezone()
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.get_current_timezone()


def _event_zone(user, raw_zone: str):
    """The zone an event's own `timeZone` field names, falling back to the
    user's. Google states a per-event zone for timed events and it is the
    right one to read an offsetless `dateTime` against — a 9am interview
    stated on `Europe/London` is 9am in London whatever the student's own
    setting says."""
    name = (raw_zone or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            # A zone name Google sent and this machine's tzdata does not
            # have. Falling back beats raising: the event still belongs on
            # the timeline, an hour of doubt is better than a missing row,
            # and the alternative is one unfamiliar zone name killing a
            # whole sync.
            logger.warning("Calendar sync: unknown event timeZone %r, using the user's.", name)
    return _zone_of(user)


def _parse_start(user, node: dict) -> tuple[datetime | None, bool]:
    """One Google `start`/`end` node as `(aware datetime, all_day)`.

    Google gives exactly one of two shapes and they mean different things:

    * `{"date": "2026-09-14"}` — an ALL-DAY event. No clock exists, so the
      row is stored at local midnight with `all_day` set, which is the
      convention `CalendarEvent.all_day` already documents ("stored as a
      datetime at local midnight with this flag set, so ordering stays one
      comparison rather than a union over two columns").
    * `{"dateTime": "2026-09-14T09:00:00-07:00", "timeZone": "..."}` — a
      timed event. Usually carries its own offset and is therefore already
      aware. When it does NOT, the clock time is anchored to the event's
      stated `timeZone`, or the user's, and never to the process default —
      the house rule for every naive timestamp in this codebase
      (`capture.gmail._user_aware`).
    """
    raw_date = (node or {}).get("date")
    if raw_date:
        day = parse_date(raw_date)
        if day is None:
            return None, True
        return timezone.make_aware(datetime.combine(day, time.min), _zone_of(user)), True

    raw_dt = (node or {}).get("dateTime")
    if not raw_dt:
        return None, False
    parsed = parse_datetime(raw_dt)
    if parsed is None:
        return None, False
    if timezone.is_aware(parsed):
        return parsed, False
    return timezone.make_aware(parsed, _event_zone(user, (node or {}).get("timeZone", ""))), False


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

@dataclass
class GcalSyncResult:
    """Per-run counters, the same reporting posture as
    `capture.gmail.SyncResult`: a run says what it did in numbers a human
    reads, and `details` carries the lines worth naming individually."""

    created: int = 0
    updated: int = 0
    cancelled: int = 0
    adopted: int = 0
    unchanged: int = 0
    skipped: int = 0
    # True when Google rejected the stored cursor (410) and the run fell
    # back to a full windowed read. Reported rather than hidden: "nothing
    # changed" and "the cursor expired and we re-read everything" look
    # identical from the outside and mean completely different things.
    resynced: bool = False
    details: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.created + self.updated + self.cancelled + self.adopted

    def as_stats(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "cancelled": self.cancelled,
            "adopted": self.adopted,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "resynced": self.resynced,
        }

    def summary(self) -> str:
        """The one line a command prints. Always the same shape whether or
        not anything happened, so a log of runs can be skimmed."""
        parts = [
            f"{self.created} created",
            f"{self.updated} updated",
            f"{self.cancelled} cancelled",
            f"{self.adopted} adopted",
            f"{self.unchanged} unchanged",
            f"{self.skipped} skipped",
        ]
        if self.resynced:
            parts.append("cursor expired, full re-read")
        return ", ".join(parts)


def _list_events(service, connection, *, now) -> tuple[list[dict], str | None, bool]:
    """Every event waiting for this calendar, plus the next cursor.

    Two modes, and the cursor decides which. With a stored `sync_token`
    Google returns only what CHANGED since it was issued — including
    deletions, as `status: "cancelled"` tombstones, which is the only way
    this sync ever learns a meeting was called off. Without one it reads a
    window (`GCAL_SYNC_PAST_DAYS` back, `GCAL_SYNC_FUTURE_DAYS` forward) and
    stores the token that read hands back.

    `singleEvents=True` expands a recurring series into its individual
    occurrences. That is what makes each row a real datetime a timeline can
    hold — an unexpanded RRULE is a rule, not a meeting, and `CalendarEvent`
    has nowhere to put one.

    A 410 means the stored token aged out (Google's own retention, not
    ours). The correct answer is a full windowed re-read, which is what the
    caller does with the `expired` flag — the same shape as
    `gmail_live.sync_connection`'s 404 re-anchor.
    """
    params = {
        "calendarId": connection.calendar_id or "primary",
        "singleEvents": True,
        "showDeleted": True,
        "maxResults": PAGE_SIZE,
    }
    if connection.sync_token:
        params["syncToken"] = connection.sync_token
    else:
        params["timeMin"] = (now - timedelta(days=settings.GCAL_SYNC_PAST_DAYS)).isoformat()
        params["timeMax"] = (now + timedelta(days=settings.GCAL_SYNC_FUTURE_DAYS)).isoformat()
        # Only legal without a syncToken — Google rejects an ordered
        # incremental read outright.
        params["orderBy"] = "startTime"

    events: list[dict] = []
    page_token = None
    next_sync_token = None
    while True:
        call_params = dict(params)
        if page_token:
            call_params["pageToken"] = page_token
        try:
            page = service.events().list(**call_params).execute()
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 410 and connection.sync_token:
                return [], None, True
            raise
        events.extend(page.get("items") or [])
        page_token = page.get("nextPageToken")
        if not page_token:
            # Google only issues the next cursor on the LAST page. Reading
            # it earlier and stopping would store a token that skips
            # everything after it.
            next_sync_token = page.get("nextSyncToken")
            break
    return events, next_sync_token, False


def _upsert_event(user, connection, event: dict, result: GcalSyncResult, *, dry_run: bool) -> None:
    """One Google event onto the timeline. Idempotent by construction.

    THE LOOKUP ORDER IS THE ANTI-DUPLICATION MECHANISM, and it runs widest
    key last:

    1. `external_id` — Google's event id. A row this sync already wrote.
    2. `ics_uid` — the meeting's identity across everything that touched it.
       This is the one that finds the row `capture.gmail` created from the
       `.ics` in the mailbox for the SAME meeting, and adopting it is what
       stops the student seeing one coffee chat twice.
    3. Create.

    An adopted row KEEPS ITS CONTACT and its kind. Coverage learned who the
    chat is with from the mail, not from Google's attendee list, and
    overwriting that with `None` would strip the one fact that makes the row
    useful on the Today page.
    """
    google_id = (event.get("id") or "").strip()[:255]
    if not google_id:
        result.skipped += 1
        return

    # A RECURRING SERIES' INSTANCES SHARE ONE iCalUID, AND USING IT WOULD
    # COLLAPSE THEM. `singleEvents=True` expands "every Tuesday" into one
    # event per Tuesday, each with its own `id` but all carrying the series'
    # UID. Keyed on that UID, the second Tuesday would find the first
    # Tuesday's row and MOVE it, so a weekly standing call would render as a
    # single row that jumped forward a week on every sync — and `ics_uid` is
    # unique per user, so the alternative outcome is an IntegrityError that
    # kills the whole pass.
    #
    # `recurringEventId` is Google's own marker for "this is an instance of
    # a series", so an instance keys on its `id` alone. It loses the mailbox
    # join for recurring events, which is the right trade: an `.ics` invite
    # in the mail is a single meeting, and matching it to an arbitrary
    # occurrence of a series would be a guess.
    ical_uid = "" if event.get("recurringEventId") else (event.get("iCalUID") or "").strip()[:255]

    existing = CalendarEvent.all_objects.filter(user=user, external_id=google_id).first()
    adopting = False
    if existing is None and ical_uid:
        existing = CalendarEvent.all_objects.filter(user=user, ics_uid=ical_uid).first()
        adopting = existing is not None

    # A TOMBSTONE, NOT A DELETE. `status: "cancelled"` is how an incremental
    # read reports a removed or declined event, and the row survives it for
    # exactly the reasons `CalendarEvent.cancelled_at` documents: it is a
    # record of a meeting that really was on the books, and a row that
    # silently vanishes teaches a student to distrust the whole calendar.
    # A cancellation for an event we never had is not an error — it is the
    # ordinary case for anything deleted outside our window.
    if (event.get("status") or "").lower() == "cancelled":
        if existing is None:
            result.skipped += 1
            return
        if existing.cancelled_at is not None:
            result.unchanged += 1
            return
        if not dry_run:
            existing.cancelled_at = timezone.now()
            existing.external_id = existing.external_id or google_id
            existing.save(update_fields=["cancelled_at", "external_id"])
        result.cancelled += 1
        result.details.append(f"cancelled: {existing.title}")
        return

    starts_at, all_day = _parse_start(user, event.get("start") or {})
    if starts_at is None:
        # No usable start. Nothing a timeline can do with it, and inventing
        # one would put a meeting on a day nobody agreed to.
        result.skipped += 1
        return
    ends_at, _ = _parse_start(user, event.get("end") or {})

    title = (event.get("summary") or "").strip()[:255] or UNTITLED
    location = (event.get("location") or "").strip()[:255]
    description = (event.get("description") or "").strip()

    if existing is None:
        if not dry_run:
            CalendarEvent.all_objects.create(
                user=user,
                external_id=google_id,
                ics_uid=ical_uid,
                title=title,
                description=description,
                location=location,
                starts_at=starts_at,
                ends_at=ends_at,
                all_day=all_day,
                kind=CalendarEvent.KIND_EVENT,
                source=CalendarEvent.SOURCE_GCAL,
                # A FIELD ON THE EVENT SAID SO. See the module docstring:
                # this is the same 1.0 an `.ics` DTSTART earns, and it is
                # what makes `capture.gmail._upsert_scheduled_chat` refuse
                # to let a prose reading move this row.
                time_confidence=1.0,
                time_evidence="",
            )
        result.created += 1
        result.details.append(f"created: {title}")
        return

    before = (
        existing.title, existing.description, existing.location, existing.starts_at,
        existing.ends_at, existing.all_day, existing.external_id, existing.ics_uid,
        existing.cancelled_at, existing.time_confidence, existing.time_evidence,
    )

    existing.external_id = google_id
    existing.ics_uid = existing.ics_uid or ical_uid
    existing.starts_at = starts_at
    existing.ends_at = ends_at
    existing.all_day = all_day
    existing.location = location or existing.location
    # A CANCELLATION GOOGLE HAS TAKEN BACK. The event is live again in the
    # calendar we are mirroring, so the row must stop reading as retired —
    # the same revival `_upsert_scheduled_chat` performs for a re-invite,
    # and safe here for a reason it is not there: this is not a rolling
    # re-read of old mail that could resurrect a chat from a stale message,
    # it is the current state of the calendar as Google holds it now.
    existing.cancelled_at = None
    # A STATED TIME IS A STATED TIME. A row adopted from the mailbox may
    # have been sitting at 0.6 because `chattime` read it out of a sentence;
    # a field on a real calendar event settles it, so the provenance is
    # promoted and the quoted sentence goes with it — it is no longer what
    # the row is claiming.
    existing.time_confidence = 1.0
    existing.time_evidence = ""
    if adopting:
        # The TITLE of an adopted row stays whatever Coverage wrote ("Chat
        # with Jane Banker"), because that names the person and Google's
        # summary usually does not. Everything else about the meeting comes
        # from the calendar, which is the more current statement of it.
        pass
    else:
        existing.title = title
        if description:
            existing.description = description

    after = (
        existing.title, existing.description, existing.location, existing.starts_at,
        existing.ends_at, existing.all_day, existing.external_id, existing.ics_uid,
        existing.cancelled_at, existing.time_confidence, existing.time_evidence,
    )
    if before == after:
        # THE IDEMPOTENCY GUARD, and it is the reason a re-run reports
        # honestly. A sync token can hand back the same event again (a
        # change to a field this module does not mirror, a token replayed
        # after a failure), and counting that as "updated" would have a run
        # log read as work that did not happen — the same correction
        # `_upsert_scheduled_chat` needed for `chats_scheduled`.
        result.unchanged += 1
        return

    if not dry_run:
        existing.save(update_fields=[
            "title", "description", "location", "starts_at", "ends_at", "all_day",
            "external_id", "ics_uid", "cancelled_at", "time_confidence", "time_evidence",
        ])
    if adopting:
        result.adopted += 1
        result.details.append(f"adopted the mailbox's row for: {existing.title}")
    else:
        result.updated += 1
        result.details.append(f"updated: {existing.title}")


def sync_connection(connection: GoogleCalendarConnection, *, dry_run: bool = False) -> GcalSyncResult:
    """Bring one connected calendar onto the timeline. Returns the counters.

    `dry_run` reads Google and writes NOTHING here: no rows, no cursor, no
    `last_synced_at`. It still makes the API calls, because a dry run that
    skipped the network could not tell a working connection from a revoked
    one — which is the single most useful thing it reports. That is
    `gmail_live.preview_sync`'s reasoning, held to here.

    THE CURSOR IS ONLY STORED ON A REAL RUN, and only after every page has
    been processed. Storing it earlier would have a crash mid-run advance
    the cursor past events that never landed, and those events are then
    invisible forever — the incremental read will not offer them again.
    """
    # A REVOKED GRANT IS A FACT ABOUT THE CONNECTION, NOT A CRASH. Google
    # answers a refresh on a withdrawn grant with `RefreshError`, and every
    # future run would raise the same thing forever while the Settings card
    # still read "connected". Mark the row and raise a sentence the caller
    # can show, exactly as `gmail_live.register_watch` does for the same
    # condition. The status flip is skipped on a dry run for the same reason
    # every other write is — a dry run reports, it does not decide.
    try:
        service = _calendar_client(connection)
    except RefreshError as exc:
        if not dry_run and connection.status != "revoked":
            connection.status = "revoked"
            connection.save(update_fields=["status"])
        raise GcalError(
            "Google says this calendar grant is no longer valid — reconnect "
            "Google Calendar in Settings."
        ) from exc
    now = timezone.now()

    events, next_token, expired = _list_events(service, connection, now=now)
    result = GcalSyncResult()
    if expired:
        # Google retired the stored cursor. Re-read the window from scratch
        # on the SAME pass rather than making the user wait for the next
        # one: the run has already established the grant works, and a sync
        # that returns "nothing" after a 410 would be lying about coverage.
        result.resynced = True
        if not dry_run:
            connection.sync_token = ""
            connection.save(update_fields=["sync_token"])
        else:
            # A dry run must not persist the clearing, but the re-read below
            # still has to behave as though the cursor is gone.
            connection.sync_token = ""
        events, next_token, _ = _list_events(service, connection, now=now)

    for event in events:
        _upsert_event(connection.user, connection, event, result, dry_run=dry_run)

    if not dry_run:
        connection.sync_token = next_token or connection.sync_token
        connection.last_synced_at = now
        connection.last_sync_stats = result.as_stats()
        connection.save(update_fields=["sync_token", "last_synced_at", "last_sync_stats"])
    for line in result.details:
        logger.info("Calendar sync %s: %s", connection.google_email, line)
    return result
