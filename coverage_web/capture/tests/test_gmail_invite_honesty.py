"""Three ways a calendar invite lied about a chat, or lost one.

Both live in `capture.gmail._upsert_scheduled_chat` and its siblings, and both
are the same failure wearing different clothes: a statement with LESS evidence
behind it overwriting one with more.

1. AN UNDATEABLE INVITE WON EVERY ARGUMENT. `_message_occurred_at` returns
   None for an absent or garbled `internalDate`, and the recency guard spelled
   that case `sent_at is None -> take the branch`. So the one invite that can
   say nothing at all about when it was sent outranked an invite that could —
   "if we can't date it, trust it most". It walked in through the guard's own
   null branch and produced exactly the outcome
   `test_the_older_invite_cannot_drag_the_chat_back` was written to stop.

2. A CANCELLED CHAT STAYED ON THE CALENDAR. `_extract_ics_schedule` learned to
   report no time from a `METHOD:CANCEL`, which stopped a cancellation
   re-asserting the meeting. The row the ORIGINAL invite wrote was untouched:
   still on the grid, still riding out to a subscribed .ics feed and onto a
   phone, still pulling a prep card onto Today for a chat nobody was attending.

3. AN ACCEPTANCE PUT NOTHING ON THE CALENDAR AT ALL (2026-09-01). The mirror
   image of 2, and the more expensive one: "Accepted: Jimmy <> Lily Coffee
   Chat" is a counterparty saying yes to the student's own invite, and every
   one of them was classified auto-submitted bulk and reached `apply_findings`
   with `chat_status: "none"`. Five of the six `review` MailFacts on the
   founder's live account (read-only, 2026-09-01) are that message. The
   classifier fix is `capture.gmail_live._ics_rsvp`; this file is where it has
   to actually produce a `CalendarEvent`.

`transaction=True` for the reason `test_gmail.py` documents: applying a
finding calls `crm.services.log_touch`, which opens its own connection.
"""

from __future__ import annotations

import base64
from datetime import timedelta, timezone as dt_timezone

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from capture import gmail_live
from capture.gmail import apply_findings
from capture.models import MailFact
from crm.models import CalendarEvent, Contact

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()

UID = "0abc1def2ghi3jkl@google.com"


@pytest.fixture
def student():
    return User.objects.create_user(email="invite-honesty@example.com", password="x")


@pytest.fixture
def lily(student):
    return Contact.all_objects.create(
        user=student, name="Lily Liu", email="lily.liu@barclays.com")


def _at(days=0, hour=15):
    return timezone.localtime(timezone.now()).replace(
        hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)


def _invite(thread_id, when, *, sent_at=None, uid=UID):
    """One finding in `GmailFindingsProvider`'s shape. `sent_at=None` is the
    undateable case — a real message whose `internalDate` Gmail did not send
    or sent garbled, which `_message_occurred_at` reports as None."""
    return {
        "name": "Lily Liu", "found": True, "email": "lily.liu@barclays.com",
        "thread_id": thread_id, "chat_status": "scheduled",
        "chat_scheduled_at": when.isoformat(), "ics_uid": uid,
        "occurred_at": sent_at.isoformat() if sent_at else None,
        "evidence": "Calendar invite received: Coffee Chat",
    }


# ---------------------------------------------------------------------------
# 1 — the undateable invite
# ---------------------------------------------------------------------------

def test_an_undateable_invite_cannot_overwrite_a_dated_ones_time(student, lily):
    """THE BUG. A dated "New Time Proposed" sets the chat to Thursday 11am.
    An invite that arrives with no readable timestamp then names Tuesday 3pm,
    and cannot say it is the newer of the two — so it must not win. Under the
    old guard `sent_at is None` was the FIRST branch, so it won outright."""
    proposed, stale = _at(days=4, hour=11), _at(days=3, hour=15)
    apply_findings(student, [
        _invite("thread-dated", proposed, sent_at=timezone.now() - timedelta(days=1))])

    apply_findings(student, [_invite("thread-undateable", stale, sent_at=None)])

    ev = CalendarEvent.objects.for_user(student).get()
    assert timezone.localtime(ev.starts_at) == proposed, (
        "an invite with no timestamp carries no evidence of being newer"
    )
    assert ev.thread_id == "thread-dated"


def test_an_undateable_invite_still_creates_a_chat_where_none_exists(student, lily):
    """The rule is about OVERWRITING, not about admission. With no row to
    protect there is nothing to weigh the invite against, and some evidence
    beats none — refusing here would silently drop real chats from every
    sender whose message Gmail dates badly."""
    when = _at(days=3, hour=15)
    apply_findings(student, [_invite("thread-a", when, sent_at=None)])

    ev = CalendarEvent.objects.for_user(student).get()
    assert timezone.localtime(ev.starts_at) == when


def test_an_undateable_invite_still_moves_a_row_with_no_recorded_invite_time(
        student, lily):
    """`invite_sent_at` is null on every row written before the column
    existed — all six on the founder's live board, checked read-only
    2026-08-28. Neither side has evidence here, so the incoming invite is the
    only statement available and it speaks. Refusing would freeze exactly
    those rows at whatever an old sync happened to write."""
    first, moved = _at(days=3, hour=15), _at(days=5, hour=9)
    apply_findings(student, [_invite("thread-a", first, sent_at=None)])
    assert CalendarEvent.objects.for_user(student).get().invite_sent_at is None

    apply_findings(student, [_invite("thread-a", moved, sent_at=None)])
    assert timezone.localtime(
        CalendarEvent.objects.for_user(student).get().starts_at) == moved


def test_a_dated_invite_still_moves_a_row_with_no_recorded_invite_time(student, lily):
    """The other half of the same case, and the branch that carries the live
    table: a dated invite over a row that has no provenance to defend."""
    first, moved = _at(days=3, hour=15), _at(days=5, hour=9)
    CalendarEvent.all_objects.create(
        user=student, contact=lily, thread_id="thread-a", ics_uid=UID,
        title="Chat with Lily Liu", starts_at=first,
        kind=CalendarEvent.KIND_CHAT, source=CalendarEvent.SOURCE_CAPTURE)

    apply_findings(student, [_invite("thread-a", moved, sent_at=timezone.now())])
    assert timezone.localtime(
        CalendarEvent.objects.for_user(student).get().starts_at) == moved


# ---------------------------------------------------------------------------
# 2 — the cancellation
# ---------------------------------------------------------------------------

def _cancel_message(student, thread_id="thread-z", uid=UID, prop="METHOD:CANCEL"):
    """A real cancellation shape: the whole event again — same UID, same
    SUMMARY, same DTSTART — plus the statement that it is off, and on a
    BRAND-NEW Gmail thread, which is why the UID is the only usable key."""
    return {
        "threadId": thread_id,
        "internalDate": str(int(timezone.now().timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "From", "value": "Lily Liu <lily.liu@barclays.com>"},
                {"name": "To", "value": student.email},
                {"name": "Subject", "value": "Cancelled: Coffee Chat"},
            ],
            "parts": [{
                "mimeType": "text/calendar",
                "body": {"data": base64.urlsafe_b64encode(
                    (f"BEGIN:VCALENDAR\n{prop}\nBEGIN:VEVENT\n"
                     f"UID:{uid}\nDTSTART:20260901T140000Z\n"
                     "SUMMARY:Coffee Chat\nEND:VEVENT\nEND:VCALENDAR\n"
                     ).encode()).decode()},
            }],
        },
    }


def test_a_cancellation_retires_the_row_the_invite_created(student, lily):
    """THE BUG. The chat is on the calendar, the organiser calls it off, and
    the row sat there reading as a live 3pm meeting."""
    when = _at(days=3, hour=15)
    apply_findings(student, [
        _invite("thread-a", when, sent_at=timezone.now() - timedelta(days=1))])

    apply_findings(student, [
        gmail_live._classify_message(student.email, _cancel_message(student))])

    ev = CalendarEvent.all_objects.get(user=student)
    assert ev.cancelled_at is not None
    assert ev.title.startswith("Cancelled: "), ev.title
    assert timezone.localtime(ev.starts_at) == when, (
        "the date survives — this is a record of something that was booked"
    )


def test_status_cancelled_retires_it_too(student, lily):
    apply_findings(student, [
        _invite("thread-a", _at(days=3), sent_at=timezone.now() - timedelta(days=1))])
    apply_findings(student, [gmail_live._classify_message(
        student.email, _cancel_message(student, prop="STATUS:CANCELLED"))])
    assert CalendarEvent.all_objects.get(user=student).cancelled_at is not None


def test_a_retired_chat_leaves_todays_queue(student, lily):
    """The loudest surface a cancelled chat reaches. `_chat_prep` renders the
    CONTACT and the CLOCK and never prints the event title at all, so a
    "Cancelled: " prefix is invisible there — the row has to actually leave."""
    from crm.today import _cockpit_context

    apply_findings(student, [
        _invite("thread-a", _at(days=0, hour=23), sent_at=timezone.now() - timedelta(days=1))])
    assert _cockpit_context(student)["chat_prep"], "precondition: prep card is there"

    apply_findings(student, [
        gmail_live._classify_message(student.email, _cancel_message(student))])

    ctx = _cockpit_context(student)
    assert ctx["chat_prep"] == [], "no prep for a meeting nobody is attending"
    assert all("Lily" not in r["title"] for r in ctx["schedule"]), (
        "and it is not coming up either — including via the untimed "
        "'chat agreed, no time yet' fallback, which `thread_state` would "
        "otherwise hand it straight back to"
    )


def test_a_retired_chat_stays_on_the_calendar_struck_through(student, logged_in=None):
    """It is NOT deleted. This grid is a record of the student's month, and a
    row that silently vanishes teaches them to distrust the page — the same
    argument layer 4 already settled for a posting the firm pulled."""
    from crm.calendar_views import _events_by_day

    lily = Contact.all_objects.create(
        user=student, name="Lily Liu", email="lily.liu@barclays.com")
    when = _at(days=3, hour=15)
    apply_findings(student, [
        _invite("thread-a", when, sent_at=timezone.now() - timedelta(days=1))])
    apply_findings(student, [
        gmail_live._classify_message(student.email, _cancel_message(student))])

    day = timezone.localtime(when).date()
    rows = _events_by_day(student, day, day)[day]
    assert len(rows) == 1
    assert rows[0]["cancelled"] is True
    assert rows[0]["title"].startswith("Cancelled: ")


def test_the_feed_carries_the_cancellation_to_the_phone(client, student, lily):
    """The .ics SUMMARY is the whole of what a lock-screen notification shows,
    which is why the marker is stored on the title rather than added by one
    template. The VEVENT stays: this feed is SUBSCRIBED, and dropping it
    deletes the entry out of the student's own calendar app with no
    explanation."""
    apply_findings(student, [
        _invite("thread-a", _at(days=3), sent_at=timezone.now() - timedelta(days=1))])
    apply_findings(student, [
        gmail_live._classify_message(student.email, _cancel_message(student))])

    student.refresh_from_db()
    body = client.get(f"/app/calendar/feed/{student.calendar_token}.ics").content.decode()
    assert "SUMMARY:Cancelled: Chat with Lily Liu" in body, body


def test_processing_the_same_cancellation_twice_is_the_same_as_once(student, lily):
    """IDEMPOTENCE. The sync re-reads a rolling window, so it will see this
    cancellation again tomorrow and the day after. An already-retired row is
    left exactly alone rather than re-stamped — a `now()` rewrite would walk
    the timestamp forward on every run, and a second prefix would stack
    ("Cancelled: Cancelled: Chat with ...")."""
    apply_findings(student, [
        _invite("thread-a", _at(days=3), sent_at=timezone.now() - timedelta(days=1))])
    cancel = gmail_live._classify_message(student.email, _cancel_message(student))

    apply_findings(student, [cancel])
    first = CalendarEvent.all_objects.get(user=student)
    stamp, title = first.cancelled_at, first.title

    for _ in range(3):
        apply_findings(student, [cancel])

    again = CalendarEvent.all_objects.get(user=student)
    assert again.cancelled_at == stamp
    assert again.title == title == "Cancelled: Chat with Lily Liu"
    assert CalendarEvent.all_objects.filter(user=student).count() == 1


def test_rereading_the_same_scheduled_chat_does_not_recount_it(student, lily):
    """IDEMPOTENCE, on the counter rather than the row. The rolling window
    means the SAME "scheduled" finding for an already-current chat comes
    back on every sync pass until it ages out of the search window —
    `_upsert_scheduled_chat` used to return True unconditionally once it
    reached the update path, so `SyncResult.chats_scheduled` (the number a
    sync digest reports) went up once per pass even when nothing on the row
    had changed. "3 chats scheduled" could mean 3 new chats or 0 new and 3
    re-touched, with no way to tell which from the digest alone."""
    invite = _invite("thread-a", _at(days=3, hour=15), sent_at=timezone.now())
    first = apply_findings(student, [invite])
    assert first.chats_scheduled == 1, "the actual creation counts once"

    for _ in range(3):
        again = apply_findings(student, [invite])
        assert again.chats_scheduled == 0, (
            "re-reading the identical finding must not recount a chat that "
            "is already scheduled"
        )

    assert CalendarEvent.all_objects.filter(user=student).count() == 1


def test_a_genuinely_later_invite_still_counts_as_scheduled(student, lily):
    """The counter is not just latched off after the first hit — a real
    change to the row (the organiser moving the time) must still register,
    the same way `test_a_dated_invite_still_moves_a_row_...` shows the row
    itself still moves."""
    first_time = _at(days=3, hour=15)
    moved_time = _at(days=5, hour=9)
    apply_findings(student, [_invite("thread-a", first_time, sent_at=timezone.now())])

    result = apply_findings(student, [
        _invite("thread-a", moved_time, sent_at=timezone.now() + timedelta(minutes=1))])

    assert result.chats_scheduled == 1, "a genuine reschedule still counts"
    assert timezone.localtime(
        CalendarEvent.objects.for_user(student).get().starts_at) == moved_time


def test_the_original_invite_resurfacing_does_not_revive_the_chat(student, lily):
    """THE REASON THE ROW IS RETIRED RATHER THAN DELETED. The sync reads a
    rolling window of the mailbox, so the original REQUEST is still sitting in
    it after the CANCEL lands. Deleting the row would let the next run mint it
    again — a chat resurrecting itself twice a day, each time looking like a
    fresh booking. Nor may the update path quietly undo it: the re-read
    original's own send time predates the cancellation, so it cannot speak
    over it."""
    sent = timezone.now() - timedelta(days=1)
    original = _invite("thread-a", _at(days=3), sent_at=sent)
    apply_findings(student, [original])
    apply_findings(student, [
        gmail_live._classify_message(student.email, _cancel_message(student))])

    apply_findings(student, [original])
    apply_findings(student, [original])

    ev = CalendarEvent.all_objects.get(user=student)
    assert ev.cancelled_at is not None, "still retired"
    assert ev.title == "Cancelled: Chat with Lily Liu", "and not re-titled live"


def test_a_genuine_re_invite_after_the_cancellation_revives_the_chat(student, lily):
    """The other side of that guard. An invite that CAN prove it postdates the
    cancellation is the organiser booking again, and the row comes back —
    which is the whole reason it was kept rather than destroyed."""
    apply_findings(student, [
        _invite("thread-a", _at(days=3), sent_at=timezone.now() - timedelta(days=2))])
    apply_findings(student, [
        gmail_live._classify_message(student.email, _cancel_message(student))])

    rebooked = _at(days=6, hour=9)
    apply_findings(student, [
        _invite("thread-new", rebooked, sent_at=timezone.now() + timedelta(minutes=5))])

    ev = CalendarEvent.all_objects.get(user=student)
    assert ev.cancelled_at is None
    assert ev.title == "Chat with Lily Liu"
    assert timezone.localtime(ev.starts_at) == rebooked


def test_a_cancellation_never_touches_a_hand_added_event(student, lily):
    """The limit that keeps propose-then-confirm intact. Coverage may retract
    what Coverage itself wrote off the invite stream; a typed event is the
    student's OWN record and an inbound .ics has no standing over it, whatever
    UID it happens to carry."""
    mine = CalendarEvent.all_objects.create(
        user=student, contact=lily, ics_uid=UID, title="Coffee with Lily",
        starts_at=_at(days=3), kind=CalendarEvent.KIND_CHAT,
        source=CalendarEvent.SOURCE_MANUAL)

    apply_findings(student, [
        gmail_live._classify_message(student.email, _cancel_message(student))])

    mine.refresh_from_db()
    assert mine.cancelled_at is None
    assert mine.title == "Coffee with Lily"


def test_a_cancellation_for_an_unknown_event_changes_nothing(student, lily):
    """No UID match and no thread match is not an error — it is the ordinary
    case for a chat captured before the UID column existed. It must not fall
    back to retiring some other chat."""
    when = _at(days=3)
    apply_findings(student, [
        _invite("thread-a", when, sent_at=timezone.now() - timedelta(days=1))])

    apply_findings(student, [gmail_live._classify_message(
        student.email, _cancel_message(student, uid="someone-else@google.com"))])

    ev = CalendarEvent.all_objects.get(user=student)
    assert ev.cancelled_at is None
    assert ev.title == "Chat with Lily Liu"


def test_a_cancellation_stays_inside_one_users_calendar(student, lily):
    """`CalendarEvent` is a `PrivateModel` and the lookup runs on
    `all_objects`, so tenancy here is the explicit `user=` predicate and
    nothing else. Two students can hold the same invite UID — they were both
    on it."""
    other = User.objects.create_user(email="other@example.com", password="x")
    Contact.all_objects.create(
        user=other, name="Lily Liu", email="lily.liu@barclays.com")
    for u in (student, other):
        apply_findings(u, [
            _invite("thread-a", _at(days=3), sent_at=timezone.now() - timedelta(days=1))])

    apply_findings(student, [
        gmail_live._classify_message(student.email, _cancel_message(student))])

    assert CalendarEvent.all_objects.get(user=student).cancelled_at is not None
    theirs = CalendarEvent.all_objects.get(user=other)
    assert theirs.cancelled_at is None, "another student's calendar is untouched"
    assert theirs.title == "Chat with Lily Liu"


# ---------------------------------------------------------------------------
# 3 — the acceptance
# ---------------------------------------------------------------------------

def _rsvp_message(student, *, when, partstat="ACCEPTED", uid=UID, sent_at=None,
                  sender="Lily Liu <lily.liu@barclays.com>"):
    """The real Google Calendar shape: `Auto-Submitted: auto-replied`, a
    `text/calendar; method=REPLY` part, the STUDENT as ORGANIZER, and one
    ATTENDEE line carrying the answer."""
    stamp = sent_at or timezone.now()
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "PRODID:-//Google Inc//Google Calendar 70.9054//EN\r\n"
        "VERSION:2.0\r\n"
        "METHOD:REPLY\r\n"
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{when.astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')}\r\n"
        f"ORGANIZER;CN={student.email}:mailto:{student.email}\r\n"
        f"UID:{uid}\r\n"
        "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
        f"PARTSTAT={partstat};CN=Lily Liu;X-NUM-GUESTS=0:"
        "mailto:lily.liu@barclays.com\r\n"
        "STATUS:CONFIRMED\r\n"
        "SUMMARY:Jimmy <> Lily Coffee Chat\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    verb = "Accepted" if partstat == "ACCEPTED" else "Declined"
    return {
        "threadId": f"thread-rsvp-{partstat.lower()}",
        "internalDate": str(int(stamp.timestamp() * 1000)),
        "snippet": f"{verb}: Jimmy <> Lily Coffee Chat",
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": student.email},
                {"name": "Subject",
                 "value": f"{verb}: Jimmy <> Lily Coffee Chat @ Thu Sep 3, 2026"},
                {"name": "Auto-Submitted", "value": "auto-replied"},
            ],
            "parts": [{
                "mimeType": "text/calendar",
                "headers": [{"name": "Content-Type",
                             "value": "text/calendar; charset=UTF-8; method=REPLY"}],
                "body": {"data": base64.urlsafe_b64encode(ics.encode()).decode()},
            }],
        },
    }


def test_an_acceptance_of_the_students_own_invite_books_the_chat(student, lily):
    """THE BUG. She said yes, on a message carrying the time in a machine-
    readable field, and the calendar stayed empty."""
    when = _at(days=3, hour=15)
    apply_findings(student, [gmail_live._classify_message(
        student.email, _rsvp_message(student, when=when))])

    ev = CalendarEvent.objects.for_user(student).get()
    assert timezone.localtime(ev.starts_at) == when
    assert ev.contact_id == lily.id
    assert ev.ics_uid == UID

    lily.refresh_from_db()
    assert lily.thread_state == "chat_scheduled", (
        "an accepted invite is the strongest thread state the ladder has"
    )


def test_an_acceptance_from_googles_robot_still_reaches_the_right_contact(
        student, lily):
    """Two of the founder's six live acceptances came From
    `calendar-notification@google.com`. The `.ics` names the attendee; the
    From: header names a robot, and only one of those is a person he knows."""
    when = _at(days=4, hour=11)
    apply_findings(student, [gmail_live._classify_message(
        student.email,
        _rsvp_message(student, when=when,
                      sender="Google Calendar <calendar-notification@google.com>"))])

    ev = CalendarEvent.objects.for_user(student).get()
    assert ev.contact_id == lily.id
    assert timezone.localtime(ev.starts_at) == when


def test_an_acceptance_is_no_longer_surfaced_as_unreadable(student, lily):
    """What the student actually saw before: "automated reply we could not
    read — surfaced for your look", five times over, for the five clearest
    messages in the mailbox."""
    apply_findings(student, [gmail_live._classify_message(
        student.email, _rsvp_message(student, when=_at(days=3)))])

    assert not MailFact.all_objects.filter(
        user=student, kind=MailFact.KIND_REVIEW).exists()


def test_a_decline_retires_the_chat_instead_of_rebooking_it(student, lily):
    """A DECLINED reply carries the whole event back, DTSTART included. Read
    for its time alone it books the meeting she just refused."""
    booked = timezone.now() - timedelta(minutes=10)
    apply_findings(student, [gmail_live._classify_message(
        student.email, _rsvp_message(student, when=_at(days=3), sent_at=booked))])
    assert CalendarEvent.objects.for_user(student).get().cancelled_at is None

    apply_findings(student, [gmail_live._classify_message(
        student.email,
        _rsvp_message(student, when=_at(days=3), partstat="DECLINED",
                      sent_at=timezone.now()))])

    ev = CalendarEvent.all_objects.get(user=student)
    assert ev.cancelled_at is not None
    assert ev.title.startswith("Cancelled: "), ev.title


def test_an_acceptance_of_somebody_elses_event_books_nothing(student, lily):
    """The organiser test is the gate: a REPLY the student was merely copied
    on answers an invite they never sent."""
    message = _rsvp_message(student, when=_at(days=3))
    body = message["payload"]["parts"][0]["body"]
    ics = base64.urlsafe_b64decode(body["data"]).decode()
    ics = ics.replace(student.email, "events@bigconf.example")
    body["data"] = base64.urlsafe_b64encode(ics.encode()).decode()

    apply_findings(student, [gmail_live._classify_message(student.email, message)])
    assert CalendarEvent.all_objects.filter(user=student).count() == 0
