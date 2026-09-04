"""A chat arranged in prose, from the Gmail message to the calendar row.

WHAT WAS BROKEN. `capture.gmail_live` put a chat on the calendar only when a
counterparty returned an `.ics`. On the founder's live account (read-only,
2026-09-03) the last `CalendarEvent` was 5 August while forty-three
`chat`/`chat_scheduled` touches had landed behind it: he arranges chats in
sentences and holds them on the phone, and almost none of them produce an
invite file. The calendar was empty of the one thing it exists for.

WHAT MUST NOT BREAK WHILE FIXING IT. The no-prose doctrine
(`gmail_live`'s module docstring) was written about a real failure — a
programme blast's webinar `.ics` putting "Chat with <recruiter>" on the
calendar. Reading prose reopens that door unless every message is first shown
to be a threaded, one-to-one, non-bulk conversation with somebody already in
Coverage. Half this file is those refusals.

`capture/tests/test_chattime.py` is where the READING is argued with. This
file is about what the pipeline does with it.

`transaction=True` for the reason `test_gmail.py` documents: applying a
finding calls `crm.services.log_touch`, which opens its own connection.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from capture import chattime, gmail_live
from capture.gmail import apply_findings
from crm.models import CalendarEvent, Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()

OWN = "jimmy@example.com"
TZ = "America/Los_Angeles"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def student():
    return User.objects.create_user(
        email=OWN, password="x", timezone=TZ,
    )


@pytest.fixture
def lily(student):
    return Contact.all_objects.create(
        user=student, name="Lily Liu", email="lily.liu@barclays.com")


@pytest.fixture
def freddy(student):
    return Contact.all_objects.create(
        user=student, name="Freddy Guerrero", email="freddy.guerrero@bofa.com")


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _message(*, frm, to, body, at, thread="t-lily", subject="Coffee chat",
             cc="", extra_headers=None, message_id="m-2"):
    """One Gmail message in the API's own shape.

    `message_id != thread` is the structural "this is a reply" fact
    `gmail_live._threaded` reads when no In-Reply-To header is present, and
    every fixture here is a reply, because a first cold email proposing a
    time is refused on purpose (see the guardrail tests below).
    """
    headers = {"From": frm, "To": to, "Subject": subject}
    if cc:
        headers["Cc"] = cc
    headers.update(extra_headers or {})
    return {
        "id": message_id,
        "threadId": thread,
        "internalDate": str(int(at.timestamp() * 1000)),
        "snippet": body[:120],
        "payload": {
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "parts": [{"mimeType": "text/plain", "body": {"data": _b64(body)}}],
        },
    }


def _utc(y, m, d, hour, minute=0):
    return datetime(y, m, d, hour, minute, tzinfo=dt_timezone.utc)


def _findings(message):
    return gmail_live.classify_message_findings(OWN, message, tz=TZ)


def _local(event):
    """The row's time ON THE STUDENT'S OWN CLOCK.

    Deliberately not `timezone.localtime`: nothing here passes through
    TimezoneMiddleware, so the active zone in a test (and in the management
    command this pipeline actually runs inside) is the server's UTC. Asserting
    through it would let a wrong-zone bug read as correct.
    """
    return event.starts_at.astimezone(ZoneInfo(TZ))


# --------------------------------------------------------------------------- #
# Thread A — Lily Liu. The renegotiation.
# --------------------------------------------------------------------------- #

class TestThreadALilyEndToEnd:
    """Five real messages, in order, through the real pipeline. The chat is
    booked at 18:00 and ends the thread at 17:30, because she moved it."""

    def _thread(self):
        return [
            # 1. Her opening offer — a proposal, and there is no chat yet.
            _message(
                frm="Lily Liu <lily.liu@barclays.com>", to=OWN,
                body="Always happy to chat – you can call me later today "
                     "615-989-0329 maybe 6pm tomorrow?",
                at=_utc(2026, 8, 24, 15, 13), message_id="m-1a",
            ),
            # 2. His confirmation — the booking.
            _message(
                frm=f"Jimmy <{OWN}>", to="Lily Liu <lily.liu@barclays.com>",
                body="Thank you so much for making the time. 6pm tomorrow "
                     "works great for me. I'll give you a call at "
                     "615-989-0329 then. Shooting over an invite as well.",
                at=_utc(2026, 8, 24, 15, 36), message_id="m-2a",
            ),
            # 3. Her counter — a proposal, and now there IS a chat to move.
            _message(
                frm="Lily Liu <lily.liu@barclays.com>", to=OWN,
                body="Can we do 5:30?",
                at=_utc(2026, 8, 25, 12, 57), message_id="m-3a",
            ),
            # 4. His bare acceptance — agrees to a time it does not state.
            _message(
                frm=f"Jimmy <{OWN}>", to="Lily Liu <lily.liu@barclays.com>",
                body="Of course! I'll move the invite.",
                at=_utc(2026, 8, 25, 15, 10), message_id="m-4a",
            ),
            # 5. Afterwards — the proof it happened at all.
            _message(
                frm=f"Jimmy <{OWN}>", to="Lily Liu <lily.liu@barclays.com>",
                body="Thank you so much for speaking with me this afternoon.",
                at=_utc(2026, 8, 25, 23, 7), message_id="m-5a",
            ),
        ]

    def test_the_whole_thread_lands_on_the_renegotiated_time(self, student, lily):
        """THE ACCEPTANCE CASE. 25 August, 17:30, on his own clock — the time
        they finished agreeing on, not the one they started with."""
        for message in self._thread():
            apply_findings(student, _findings(message))

        event = CalendarEvent.objects.for_user(student).get()
        assert _local(event) == datetime(2026, 8, 25, 17, 30, tzinfo=ZoneInfo(TZ))
        assert event.contact_id == lily.id

    def test_her_opening_proposal_places_nothing(self, student, lily):
        """"maybe 6pm tomorrow?" with no chat on the thread is a question.
        A question on a calendar is a lie."""
        apply_findings(student, _findings(self._thread()[0]))
        assert not CalendarEvent.objects.for_user(student).exists()

    def test_his_confirmation_places_the_chat(self, student, lily):
        apply_findings(student, _findings(self._thread()[1]))

        event = CalendarEvent.objects.for_user(student).get()
        assert _local(event).hour == 18
        assert _local(event).date() == datetime(2026, 8, 25).date()
        assert event.source == CalendarEvent.SOURCE_CAPTURE
        assert event.kind == CalendarEvent.KIND_CHAT

    def test_her_counter_moves_the_chat_it_did_not_create(self, student, lily):
        thread = self._thread()
        apply_findings(student, _findings(thread[1]))
        apply_findings(student, _findings(thread[2]))

        event = CalendarEvent.objects.for_user(student).get()
        assert (_local(event).hour, _local(event).minute) == (17, 30)
        # The DAY is the one the booking established. Her message stated a
        # clock and no day, so a clock is all it may change.
        assert _local(event).date() == datetime(2026, 8, 25).date()

    def test_the_counter_leaves_exactly_one_row(self, student, lily):
        """A moved chat is one chat. The `.ics` path learned this the hard
        way when a "New Time Proposed" on a fresh Gmail thread produced a
        second row at the time she had just moved away from."""
        for message in self._thread():
            apply_findings(student, _findings(message))
        assert CalendarEvent.objects.for_user(student).count() == 1

    def test_a_proposal_does_not_climb_the_relationship_ladder(self, student, lily):
        """Her counter moves the calendar and logs no `chat_scheduled`
        touch. It is a question; the ladder is for what has happened."""
        [finding] = _findings(self._thread()[2])
        assert finding["chat_status"] == "none"
        assert finding["prose_time"]["kind"] == chattime.KIND_PROPOSAL

    def test_the_booking_does_climb_it(self, student, lily):
        apply_findings(student, _findings(self._thread()[1]))
        assert Touch.objects.for_user(student).filter(
            contact=lily, kind="chat_scheduled").exists()


# --------------------------------------------------------------------------- #
# Thread B — Freddy Guerrero. The window that must not book.
# --------------------------------------------------------------------------- #

class TestThreadBFreddyEndToEnd:
    def test_his_availability_window_places_nothing(self, student, freddy):
        """THE OTHER ACCEPTANCE CASE. "any time after 7PM ET works for me"
        carries a commitment phrase, a clock and a zone, and agrees to
        nothing. It must never reach the calendar on its own."""
        message = _message(
            frm="Freddy Guerrero <freddy.guerrero@bofa.com>", to=OWN,
            body="Hi Jimmy, any time after 7PM ET works for me.",
            at=_utc(2026, 9, 2, 17, 0), thread="t-freddy", message_id="m-1b",
        )
        findings = _findings(message)

        assert findings[0]["prose_time"] is None
        assert findings[0]["chat_status"] == "none"
        apply_findings(student, findings)
        assert not CalendarEvent.objects.for_user(student).exists()

    def test_his_own_reply_books_the_zoom_time(self, student, freddy):
        """Half past four in the afternoon on his clock, which is the same
        instant as the half past seven Eastern he wrote one line above."""
        message = _message(
            frm=f"Jimmy <{OWN}>", to="Freddy Guerrero <freddy.guerrero@bofa.com>",
            body="That's perfect, sending an invite over for tomorrow 7:30 PM "
                 "ET. Here's a Zoom invite:\n"
                 "Time: September 3, 2026 at 4:30 PM PT\n"
                 "Join Zoom Meeting https://zoom.us/j/81234567890\n"
                 "Meeting ID: 812 3456 7890\nPasscode: 449281",
            at=_utc(2026, 9, 2, 18, 36), thread="t-freddy", message_id="m-2b",
        )
        apply_findings(student, _findings(message))

        event = CalendarEvent.objects.for_user(student).get()
        assert event.starts_at == _utc(2026, 9, 3, 23, 30)
        assert (_local(event).hour, _local(event).minute) == (16, 30)
        assert event.contact_id == freddy.id

    def test_the_window_then_the_booking_makes_one_chat_at_the_booked_time(
            self, student, freddy):
        for message in (
            _message(
                frm="Freddy Guerrero <freddy.guerrero@bofa.com>", to=OWN,
                body="Hi Jimmy, any time after 7PM ET works for me.",
                at=_utc(2026, 9, 2, 17, 0), thread="t-freddy", message_id="m-1b",
            ),
            _message(
                frm=f"Jimmy <{OWN}>",
                to="Freddy Guerrero <freddy.guerrero@bofa.com>",
                body="That's perfect, sending an invite over for tomorrow "
                     "7:30 PM ET.",
                at=_utc(2026, 9, 2, 18, 36), thread="t-freddy", message_id="m-2b",
            ),
        ):
            apply_findings(student, _findings(message))

        event = CalendarEvent.objects.for_user(student).get()
        assert event.starts_at == _utc(2026, 9, 3, 23, 30)


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #

class TestProvenance:
    """A prose time is distinguishable from an `.ics` one in the model, on the
    same scale the deadline pipeline already uses (`directory.ingest`: 1.0
    stated, 0.6 reported)."""

    def _booking(self):
        return _message(
            frm=f"Jimmy <{OWN}>", to="Lily Liu <lily.liu@barclays.com>",
            body="6pm tomorrow works great for me.",
            at=_utc(2026, 8, 24, 15, 36),
        )

    def test_a_prose_chat_is_marked_reported(self, student, lily):
        apply_findings(student, _findings(self._booking()))

        event = CalendarEvent.objects.for_user(student).get()
        assert event.time_confidence == chattime.PROSE_CONFIDENCE
        assert event.time_reported is True

    def test_it_quotes_the_sentence_it_was_read_from(self, student, lily):
        apply_findings(student, _findings(self._booking()))

        event = CalendarEvent.objects.for_user(student).get()
        assert event.time_evidence == "6pm tomorrow works great for me."

    def test_an_ics_chat_stays_stated(self, student, lily):
        apply_findings(student, [{
            "name": "Lily Liu", "email": "lily.liu@barclays.com", "found": True,
            "thread_id": "t-ics", "chat_status": "scheduled",
            "chat_scheduled_at": "2026-08-25T18:00:00", "ics_uid": "uid-1",
            "occurred_at": "2026-08-24T15:36:00+00:00",
            "evidence": "Calendar invite received: Coffee Chat",
        }])

        event = CalendarEvent.objects.for_user(student).get()
        assert event.time_confidence == 1.0
        assert event.time_reported is False
        assert event.time_evidence == ""

    def test_a_hand_added_event_is_stated_by_default(self, student):
        """The user typed it. Nothing outranks that, and every row written
        before this column existed reads the same way."""
        event = CalendarEvent.all_objects.create(
            user=student, title="Superday", starts_at=timezone.now(),
        )
        assert event.time_confidence == 1.0
        assert event.time_reported is False

    def test_the_touch_note_says_the_time_was_reported(self, student, lily):
        apply_findings(student, _findings(self._booking()))

        notes = " ".join(
            t.note or "" for t in Touch.objects.for_user(student).filter(contact=lily)
        )
        assert "reported" in notes.lower()
        assert "6pm tomorrow works great for me." in notes


# --------------------------------------------------------------------------- #
# Structured beats prose, always
# --------------------------------------------------------------------------- #

class TestStructuredWins:
    def _ics_finding(self, when, *, sent_at, thread="t-lily"):
        return {
            "name": "Lily Liu", "email": "lily.liu@barclays.com", "found": True,
            "thread_id": thread, "chat_status": "scheduled",
            "chat_scheduled_at": when, "ics_uid": "uid-1",
            "occurred_at": sent_at,
            "evidence": "Calendar invite received: Coffee Chat",
        }

    def test_prose_never_overwrites_an_ics_time(self, student, lily):
        """THE RULE THIS FEATURE IS NOT ALLOWED TO BREAK. The invite said
        14:00; a later sentence says 18:00. The invite is a statement and the
        sentence is a reading, and a reading does not get to argue."""
        apply_findings(student, [self._ics_finding(
            "2026-08-25T14:00:00", sent_at="2026-08-24T09:00:00+00:00")])

        apply_findings(student, _findings(_message(
            frm=f"Jimmy <{OWN}>", to="Lily Liu <lily.liu@barclays.com>",
            body="6pm tomorrow works great for me.",
            at=_utc(2026, 8, 24, 15, 36),
        )))

        event = CalendarEvent.objects.for_user(student).get()
        assert _local(event).hour == 14
        assert event.time_confidence == 1.0

    def test_prose_never_overwrites_a_time_the_user_typed(self, student, lily):
        typed = datetime(2026, 8, 25, 9, 0, tzinfo=ZoneInfo(TZ))
        CalendarEvent.all_objects.create(
            user=student, contact=lily, thread_id="t-lily",
            title="Chat with Lily Liu", starts_at=typed,
            kind=CalendarEvent.KIND_CHAT, source=CalendarEvent.SOURCE_MANUAL,
        )

        apply_findings(student, _findings(_message(
            frm=f"Jimmy <{OWN}>", to="Lily Liu <lily.liu@barclays.com>",
            body="6pm tomorrow works great for me.",
            at=_utc(2026, 8, 24, 15, 36),
        )))

        event = CalendarEvent.objects.for_user(student).get()
        assert _local(event).hour == 9
        assert event.source == CalendarEvent.SOURCE_MANUAL

    def test_an_ics_promotes_a_row_we_had_only_read(self, student, lily):
        """The other direction, and it must hold even when the invite is
        OLDER than the sentence: the recency guard weighs two statements
        against each other, and here only one side is a statement."""
        apply_findings(student, _findings(_message(
            frm=f"Jimmy <{OWN}>", to="Lily Liu <lily.liu@barclays.com>",
            body="6pm tomorrow works great for me.",
            at=_utc(2026, 8, 24, 15, 36),
        )))
        assert CalendarEvent.objects.for_user(student).get().time_reported is True

        apply_findings(student, [self._ics_finding(
            "2026-08-25T14:00:00", sent_at="2026-08-24T09:00:00+00:00")])

        event = CalendarEvent.objects.for_user(student).get()
        assert _local(event).hour == 14
        assert event.time_confidence == 1.0
        assert event.time_evidence == ""

    def test_a_message_carrying_an_invite_is_not_also_read_for_prose(self):
        """An `.ics` has already stated the time structurally. Reading the
        sentence beside it could only ever disagree with the field."""
        ics = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:uid-9\n"
            "DTSTART:20260825T210000Z\nSUMMARY:Coffee Chat\n"
            "END:VEVENT\nEND:VCALENDAR"
        )
        message = _message(
            frm=f"Jimmy <{OWN}>", to="Lily Liu <lily.liu@barclays.com>",
            body="6pm tomorrow works great for me.",
            at=_utc(2026, 8, 24, 15, 36),
        )
        message["payload"]["parts"].append(
            {"mimeType": "text/calendar", "filename": "invite.ics",
             "body": {"data": _b64(ics)}}
        )

        [finding] = _findings(message)
        assert finding["prose_time"] is None
        assert finding["chat_scheduled_at"] == "2026-08-25T21:00:00+00:00"


# --------------------------------------------------------------------------- #
# The guardrails — the no-prose doctrine, kept
# --------------------------------------------------------------------------- #

class TestGuardrails:
    """Every one of these carries a perfectly readable chat time, and none of
    them may produce one. The reading is not the hard part; knowing whose
    chat it is, is."""

    BODY = "Sounds great, 4pm tomorrow works for me."
    AT = _utc(2026, 9, 2, 18, 0)

    def test_a_programme_blast_is_refused(self, student):
        """THE FAILURE THE NO-PROSE DOCTRINE WAS WRITTEN ABOUT, arriving in
        prose instead of in a webinar `.ics`. `List-Unsubscribe` is what
        `capture.inbound` calls bulk, and bulk never reaches the calendar."""
        message = _message(
            frm="Campus Recruiting <campus@bigbank.com>", to=OWN,
            body="Join us! Our info session is at 4pm tomorrow, see you then.",
            at=self.AT, thread="t-blast", message_id="m-blast",
            extra_headers={
                "List-Unsubscribe": "<https://bigbank.com/u>",
                "Precedence": "bulk",
            },
        )
        [finding] = _findings(message)
        assert finding["bulk"] is True
        assert finding["prose_time"] is None
        assert finding["chat_status"] == "none"

    def test_a_no_reply_sender_is_refused(self, student):
        message = _message(
            frm="No Reply <no-reply@bigbank.com>", to=OWN,
            body=self.BODY, at=self.AT, thread="t-nr", message_id="m-nr",
        )
        [finding] = _findings(message)
        assert finding["prose_time"] is None

    def test_a_no_reply_recipient_is_refused(self, student):
        """The outbound mirror: nobody is behind the mailbox, so there is
        nobody to have agreed with."""
        message = _message(
            frm=f"Jimmy <{OWN}>", to="noreply@bigbank.com",
            body=self.BODY, at=self.AT, thread="t-nr2", message_id="m-nr2",
        )
        [finding] = _findings(message)
        assert finding["prose_time"] is None

    def test_a_message_to_several_people_is_refused(self, student):
        """A time in a note to a group is a schedule somebody is announcing,
        not a chat with any one of them."""
        message = _message(
            frm=f"Jimmy <{OWN}>",
            to="lily.liu@barclays.com, freddy.guerrero@bofa.com",
            body=self.BODY, at=self.AT, thread="t-many", message_id="m-many",
        )
        assert all(f["prose_time"] is None for f in _findings(message))

    def test_a_cc_makes_it_not_one_to_one(self, student):
        message = _message(
            frm="Lily Liu <lily.liu@barclays.com>", to=OWN,
            cc="staffer@barclays.com",
            body=self.BODY, at=self.AT, thread="t-cc", message_id="m-cc",
        )
        [finding] = _findings(message)
        assert finding["prose_time"] is None

    def test_a_first_unanswered_email_is_refused(self, student):
        """A cold email proposing a time is a question the recipient has not
        seen yet. Gmail's own structure says so: the first message on a
        thread carries the thread's id."""
        message = _message(
            frm=f"Jimmy <{OWN}>", to="lily.liu@barclays.com",
            body="Would love to connect — are you free 4pm tomorrow?",
            at=self.AT, thread="t-cold", message_id="t-cold",
        )
        [finding] = _findings(message)
        assert finding["prose_time"] is None

    def test_a_reply_pointer_is_enough_on_its_own(self, student):
        """A message whose id Gmail did not return is still threaded when it
        carries In-Reply-To. Both facts are structural; either will do."""
        message = _message(
            frm=f"Jimmy <{OWN}>", to="lily.liu@barclays.com",
            body=self.BODY, at=self.AT, thread="t-x", message_id="t-x",
            extra_headers={"In-Reply-To": "<abc@barclays.com>"},
        )
        [finding] = _findings(message)
        assert finding["prose_time"] is not None

    def test_a_stranger_gets_no_calendar_row(self, student):
        """`apply_findings` only ever logs against contacts already in
        Coverage, and this feature does not change that. A readable time from
        somebody with no card is a no-op."""
        message = _message(
            frm=f"Jimmy <{OWN}>", to="stranger@elsewhere.com",
            body=self.BODY, at=self.AT, thread="t-un", message_id="m-un",
        )
        apply_findings(student, _findings(message))
        assert not CalendarEvent.objects.for_user(student).exists()

    def test_an_undateable_message_reads_nothing(self, student, lily):
        """No `internalDate` means no anchor for "tomorrow", and computing
        one off the sync's own clock would book August's mail in December."""
        message = _message(
            frm=f"Jimmy <{OWN}>", to="lily.liu@barclays.com",
            body=self.BODY, at=self.AT,
        )
        del message["internalDate"]
        [finding] = _findings(message)
        assert finding["prose_time"] is None


# --------------------------------------------------------------------------- #
# Idempotence
# --------------------------------------------------------------------------- #

def test_the_same_prose_finding_re_read_writes_nothing_new(student, lily):
    """The sync re-reads a rolling window, so the same sentence comes back
    every pass until it ages out. It must not count as a new chat each time —
    the same rule `_upsert_scheduled_chat`'s snapshot already enforces for an
    invite."""
    message = _message(
        frm=f"Jimmy <{OWN}>", to="Lily Liu <lily.liu@barclays.com>",
        body="6pm tomorrow works great for me.",
        at=_utc(2026, 8, 24, 15, 36),
    )
    first = apply_findings(student, _findings(message))
    second = apply_findings(student, _findings(message))

    assert first.chats_scheduled == 1
    assert second.chats_scheduled == 0
    assert CalendarEvent.objects.for_user(student).count() == 1
