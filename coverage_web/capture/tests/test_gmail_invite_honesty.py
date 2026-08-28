"""Two ways a calendar invite could put a time on the page it has not earned.

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

`transaction=True` for the reason `test_gmail.py` documents: applying a
finding calls `crm.services.log_touch`, which opens its own connection.
"""

from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from capture import gmail_live
from capture.gmail import apply_findings
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
        "'chat set up, no time yet' fallback, which `thread_state` would "
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
