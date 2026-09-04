"""`capture.chattime` — reading a chat time out of prose.

No Django, no database, no network: every test here calls one pure function
with a string, a timestamp and a zone name. That is the whole point of the
module being separate — the hard part is the reading, and the reading can be
argued with in isolation.

THE TWO ACCEPTANCE THREADS are real mail from the founder's own account, read
read-only on 2026-09-03, and they are the reason this module exists. His
timezone is America/Los_Angeles (`accounts.User.timezone`); every expected
answer below is on that clock.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest

from capture import chattime

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
TZ = "America/Los_Angeles"


def _utc(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=dt_timezone.utc)


def _read(text: str, sent: datetime, tz: str = TZ):
    return chattime.extract_chat_time(text, sent_at=sent, tz=tz)


def _local(found: chattime.ChatTime) -> datetime:
    """The reading on the account owner's own clock. A naive answer is
    already on it (see `ChatTime.when`); an aware one is converted."""
    if found.when.tzinfo is None:
        return found.when
    return found.when.astimezone(PT)


# --------------------------------------------------------------------------- #
# Thread A — Lily Liu, Barclays. A negotiation, and the last time wins.
# --------------------------------------------------------------------------- #

class TestThreadALily:
    """Five real messages over two days. The chat ends up at 17:30 on 25
    August, and it gets there by being MOVED — which is the case the whole
    booking/proposal split exists for."""

    def test_her_opening_offer_is_a_proposal_not_a_booking(self):
        """"maybe 6pm tomorrow?" is a question. It names a real time and it
        has agreed nothing, so it may never place a chat on its own."""
        found = _read(
            "Always happy to chat – you can call me later today 615-989-0329 "
            "maybe 6pm tomorrow?",
            _utc(2026, 8, 24, 15, 13),
        )
        assert found is not None
        assert found.kind == chattime.KIND_PROPOSAL
        assert found.books is False
        assert _local(found) == datetime(2026, 8, 25, 18, 0)

    def test_the_phone_number_in_her_offer_is_not_read_as_a_time(self):
        """615-989-0329 sits four words from the time she is proposing. A
        reader that took digits for a clock would answer 3:29 or 9:03."""
        found = _read(
            "Always happy to chat – you can call me later today 615-989-0329 "
            "maybe 6pm tomorrow?",
            _utc(2026, 8, 24, 15, 13),
        )
        assert _local(found).hour == 18
        assert _local(found).minute == 0

    def test_the_nearer_day_wins_when_one_sentence_names_two(self):
        """"later today" and "tomorrow" are both in her sentence. The one
        touching "6pm" is the one that belongs to it."""
        found = _read(
            "Always happy to chat – you can call me later today 615-989-0329 "
            "maybe 6pm tomorrow?",
            _utc(2026, 8, 24, 15, 13),
        )
        assert _local(found).date() == datetime(2026, 8, 25).date()

    def test_his_confirmation_books_it(self):
        """His own sent mail is the strongest evidence in the corpus, and
        this is the sentence that carries it."""
        found = _read(
            "Thank you so much for making the time. 6pm tomorrow works great "
            "for me. I'll give you a call at 615-989-0329 then. Shooting over "
            "an invite as well.",
            _utc(2026, 8, 24, 15, 36),
        )
        assert found is not None
        assert found.kind == chattime.KIND_BOOKING
        assert found.books is True
        assert _local(found) == datetime(2026, 8, 25, 18, 0)
        assert found.evidence == "6pm tomorrow works great for me."
        assert found.confidence == chattime.PROSE_CONFIDENCE

    def test_tomorrow_resolves_against_the_message_not_against_now(self):
        """The same sentence read a week later must still mean 25 August.
        The first-connect backfill applies findings months after they
        happened; anchoring to now() would book every "tomorrow" on the day
        the sync ran."""
        early = _read("6pm tomorrow works great for me.", _utc(2026, 8, 24, 15, 36))
        later = _read("6pm tomorrow works great for me.", _utc(2026, 8, 30, 15, 36))
        assert _local(early).date() == datetime(2026, 8, 25).date()
        assert _local(later).date() == datetime(2026, 8, 31).date()

    def test_her_counter_offer_states_a_clock_and_no_day(self):
        """"Can we do 5:30?" is the renegotiation. It is a PROPOSAL and it is
        UNDATED, which together are the two facts that let it move the chat
        already on the thread and forbid it from making one."""
        found = _read("Can we do 5:30?", _utc(2026, 8, 25, 12, 57))
        assert found is not None
        assert found.kind == chattime.KIND_PROPOSAL
        assert found.dated is False
        assert found.books is False
        assert (_local(found).hour, _local(found).minute) == (17, 30)

    def test_his_bare_acceptance_reads_nothing(self):
        """"Of course! I'll move the invite." agrees to a time it does not
        state. There is nothing here to read and the honest answer is None —
        the move is `capture.gmail`'s job, from her message, against the row
        that already exists."""
        assert _read("Of course! I'll move the invite.", _utc(2026, 8, 25, 15, 10)) is None

    def test_the_quoted_counter_offer_is_not_attributed_to_him(self):
        """His reply carries her sentence underneath it. Read as one text it
        becomes HIM booking 5:30 — the right answer for the wrong reason, and
        the same machinery would resurrect a time from a stale thread."""
        found = _read(
            "Of course! I'll move the invite.\n\n"
            "On Tue, Aug 25, 2026 at 5:57 AM Lily Liu <lily.liu@barclays.com> "
            "wrote:\n> Can we do 5:30?\n",
            _utc(2026, 8, 25, 15, 10),
        )
        assert found is None

    def test_the_thank_you_afterwards_reads_nothing(self):
        """"speaking with me this afternoon" is a chat that already happened
        and it names no clock. Past tense is not this module's business."""
        found = _read(
            "Thank you so much for speaking with me this afternoon.",
            _utc(2026, 8, 25, 23, 7),
        )
        assert found is None


# --------------------------------------------------------------------------- #
# Thread B — Freddy Guerrero, Bank of America. A window, then a booking.
# --------------------------------------------------------------------------- #

class TestThreadBFreddy:
    def test_an_availability_window_books_nothing(self):
        """THE CASE THIS MODULE IS MOST CAREFUL ABOUT. "any time after 7PM ET
        works for me" carries a commitment phrase, a clock AND a zone, and it
        agrees to nothing — he is saying when he COULD talk. Read for its
        "works for me" alone it puts a 7pm chat on a calendar nobody
        arranged."""
        found = _read(
            "Hi Jimmy, any time after 7PM ET works for me.",
            _utc(2026, 9, 2, 17, 0),
        )
        assert found is None

    def test_his_reply_books_the_time_it_states_twice(self):
        """He states the same appointment in two zones. They agree, so the
        message answers — and the answer is half past four in the afternoon
        on his own clock."""
        found = _read(
            "That's perfect, sending an invite over for tomorrow 7:30 PM ET. "
            "Here's a Zoom invite:\n"
            "Time: September 3, 2026 at 4:30 PM PT\n"
            "Join Zoom Meeting https://zoom.us/j/81234567890\n"
            "Meeting ID: 812 3456 7890\n"
            "Passcode: 449281\n"
            "One tap mobile +16699006833,,81234567890#",
            _utc(2026, 9, 2, 18, 36),
        )
        assert found is not None
        assert found.kind == chattime.KIND_BOOKING
        assert found.books is True
        assert _local(found) == datetime(2026, 9, 3, 16, 30, tzinfo=PT)
        # The same instant, stated the other way round.
        assert found.when.astimezone(dt_timezone.utc) == _utc(2026, 9, 3, 23, 30)

    def test_a_stated_zone_beats_the_account_owners_own(self):
        """"7:30 PM ET" is half past seven in New York whatever clock the
        reader keeps. Anchoring it to his own would put the chat three hours
        out."""
        found = _read("Sending an invite for tomorrow 7:30 PM ET.", _utc(2026, 9, 2, 18, 36))
        assert found.when.utcoffset() == ET.utcoffset(datetime(2026, 9, 3, 19, 30))
        assert found.when.astimezone(dt_timezone.utc) == _utc(2026, 9, 3, 23, 30)

    def test_a_time_with_no_zone_comes_back_naive(self):
        """The codebase's own convention: a floating time means "the account
        owner's clock", and `capture.gmail._user_aware` is the one place that
        anchors it. Stamping a zone here would put that decision in two."""
        found = _read("6pm tomorrow works great for me.", _utc(2026, 8, 24, 15, 36))
        assert found.when.tzinfo is None

    def test_the_zoom_blocks_own_digits_are_not_times(self):
        """A meeting id, a passcode and a dial-in number are all digits in a
        message whose subject is a time. None of them may become one."""
        found = _read(
            "Here's a Zoom invite:\n"
            "Time: September 3, 2026 at 4:30 PM PT\n"
            "Meeting ID: 812 3456 7890\nPasscode: 449281\n"
            "One tap mobile +16699006833,,81234567890#",
            _utc(2026, 9, 2, 18, 36),
        )
        assert found is not None
        assert _local(found) == datetime(2026, 9, 3, 16, 30, tzinfo=PT)


# --------------------------------------------------------------------------- #
# The refusals
# --------------------------------------------------------------------------- #

class TestRefusals:
    """Refusing beats guessing (`capture.discovery`). Every case here is a
    correct None, not a miss."""

    SENT = _utc(2026, 9, 2, 18, 0)

    @pytest.mark.parametrize("text", [
        "Let's chat sometime!",
        "I'm free most afternoons.",
        "Happy to connect — let me know what works.",
        "Looking forward to speaking soon.",
        "Would love to grab coffee at some point this fall.",
    ])
    def test_a_vague_offer_names_no_time(self, text):
        assert _read(text, self.SENT) is None

    @pytest.mark.parametrize("text", [
        "Any time after 5pm works.",
        "I'm free between 2pm and 4pm on Thursday.",
        "Anything before 11am tomorrow is good for me.",
        "I'm around all day Wednesday from 9am.",
        "My availability tomorrow is 3pm onwards.",
    ])
    def test_a_window_is_not_a_booking(self, text):
        assert _read(text, self.SENT) is None

    @pytest.mark.parametrize("text", [
        "Applications close Oct 30.",
        "Just a reminder that the deadline is October 30 at 5pm.",
        "Please submit by 11:59pm on 15 October.",
        "RSVP by Friday 5pm if you'd like to attend.",
    ])
    def test_a_deadline_is_not_an_appointment(self, text):
        """`directory.ingest` owns these. A chat pipeline reading them puts a
        firm's cut-off on the calendar as a meeting with a person."""
        assert _read(text, self.SENT) is None

    @pytest.mark.parametrize("text", [
        "Give me a ring on 615-989-0329 when you get a chance.",
        "Let's talk — my direct is +1 (212) 555-1200.",
        "Can we do it on 617.555.0142?",
        "My extension is 4530 if you'd like to call.",
    ])
    def test_a_phone_number_is_not_a_clock(self, text):
        assert _read(text, self.SENT) is None

    def test_an_out_of_office_states_no_appointments(self):
        """An auto-reply names dates and times constantly, all of them about
        absence. `capture.inbound` refuses most of these on headers alone;
        this is the same refusal available to a caller with no headers."""
        found = _read(
            "Automatic reply: I am out of the office until September 8. "
            "I will respond at 9am on my return.",
            self.SENT,
        )
        assert found is None

    def test_two_readings_that_disagree_are_no_reading(self):
        """The message has not said which chat it means, and taking the first
        is a coin toss wearing a rule."""
        found = _read(
            "Let's do 3pm tomorrow. Actually, 4pm tomorrow works better.",
            self.SENT,
        )
        assert found is None

    def test_a_time_nobody_committed_to_or_asked_about_reads_nothing(self):
        """Mentioning a clock is not arranging one."""
        assert _read("The 3pm slot filled up, unfortunately.", self.SENT) is None
        assert _read("I saw your 9am email, thanks.", self.SENT) is None

    def test_a_message_with_no_timestamp_reads_nothing(self):
        """Every relative date resolves against the message's own send time.
        With none, "tomorrow" would have to mean "tomorrow from whenever the
        sync happened to run"."""
        assert chattime.extract_chat_time(
            "6pm tomorrow works great for me.", sent_at=None, tz=TZ
        ) is None

    def test_a_date_beyond_the_horizon_is_a_misreading(self):
        """A chat half a year out is a misread year, not a chat."""
        assert _read("Let's do 3pm on 12 March 2028.", self.SENT) is None

    def test_a_time_in_the_past_of_its_own_message_is_a_misreading(self):
        """A stated year that has already gone. A chat cannot be arranged
        backwards, so this is a typo or a quoted line, never an appointment."""
        assert _read("Let's do 3pm on 4 January 2026.", self.SENT) is None

    def test_a_bare_month_and_day_already_past_rolls_to_next_year(self):
        """The other half of the rule above, and the reason it has to be
        stated separately: "4 January" said in September means the January
        ahead, not the one behind. Only an EXPLICIT past year is a
        misreading."""
        found = _read("Let's do 3pm on 4 January.", self.SENT)
        assert _local(found) == datetime(2027, 1, 4, 15, 0)

    def test_an_ambiguous_numeric_date_has_no_answer(self):
        """`directory.classify.extract_deadline_from_text`'s own rule: 9/3 is
        two real conventions and no answer."""
        assert _read("Let's do 3pm on 9/3.", self.SENT) is None

    def test_an_unambiguous_numeric_date_still_answers(self):
        found = _read("Let's do 3pm on 30/9.", self.SENT)
        assert _local(found) == datetime(2026, 9, 30, 15, 0)


# --------------------------------------------------------------------------- #
# The readings the fixtures do not cover
# --------------------------------------------------------------------------- #

class TestShapes:
    SENT = _utc(2026, 9, 2, 18, 0)   # a Wednesday, 11am in Los Angeles

    def test_the_send_day_is_the_owners_day_not_utcs(self):
        """A note sent at 8pm in Los Angeles is already tomorrow in UTC.
        Anchored to UTC, every relative date in it lands a day late."""
        late = _utc(2026, 9, 3, 3, 30)          # 2 Sep, 20:30 in Los Angeles
        found = _read("Let's do 3pm tomorrow.", late)
        assert _local(found).date() == datetime(2026, 9, 3).date()

    def test_a_weekday_is_the_next_one_ahead(self):
        found = _read("Let's do 4pm on Friday.", self.SENT)
        assert _local(found) == datetime(2026, 9, 4, 16, 0)

    def test_a_weekday_naming_the_send_day_means_the_one_after(self):
        """Said on a Wednesday, "Wednesday at 4" is next week's."""
        found = _read("Let's do 4pm on Wednesday.", self.SENT)
        assert _local(found) == datetime(2026, 9, 9, 16, 0)

    @pytest.mark.parametrize("text,expected", [
        ("Let's do 4pm on September 10.", datetime(2026, 9, 10, 16, 0)),
        ("Let's do 4pm on Sept. 10.", datetime(2026, 9, 10, 16, 0)),
        ("Let's do 4pm on 10 September.", datetime(2026, 9, 10, 16, 0)),
        ("Let's do 4pm on 10th September 2026.", datetime(2026, 9, 10, 16, 0)),
        ("Let's do 4 p.m. on 10 September.", datetime(2026, 9, 10, 16, 0)),
        ("Let's do 16:00 on 10 September.", datetime(2026, 9, 10, 16, 0)),
    ], ids=["month-day", "abbrev", "day-month", "with-year", "dotted-meridiem", "24h"])
    def test_the_shapes_a_date_and_a_clock_are_written_in(self, text, expected):
        assert _local(_read(text, self.SENT)) == expected

    def test_a_bare_clock_takes_the_civil_reading(self):
        """"5:30" names no half of the day. 1-6 is afternoon, 7-11 morning,
        12 noon — the reading a calendar's quick-add makes, and the reason
        Lily's counter-offer is half past five in the afternoon."""
        assert _local(_read("Can we do 5:30 tomorrow?", self.SENT)).hour == 17
        assert _local(_read("Can we do 9:30 tomorrow?", self.SENT)).hour == 9
        assert _local(_read("Can we do 12:15 tomorrow?", self.SENT)).hour == 12

    def test_a_range_is_read_as_its_start(self):
        """"5:30-6:30" states one meeting, not two times that disagree."""
        found = _read("Let's do 5:30-6:30 tomorrow.", self.SENT)
        assert _local(found) == datetime(2026, 9, 3, 17, 30)

    def test_a_lone_number_is_never_a_time(self):
        """"can we do 6?" is real English and so is "6 of us". No reading of
        a bare digit is safe enough for a calendar."""
        assert _read("Can we do 6 tomorrow?", self.SENT) is None

    def test_an_impossible_clock_reads_nothing(self):
        assert _read("Let's do 13pm tomorrow.", self.SENT) is None
        assert _read("Let's do 25:70 tomorrow.", self.SENT) is None

    def test_html_mail_is_read_through_its_tags(self):
        found = _read(
            "<div><p>Sounds good, <b>4pm tomorrow</b> then.</p></div>", self.SENT
        )
        assert _local(found) == datetime(2026, 9, 3, 16, 0)

    def test_an_unknown_timezone_name_falls_back_rather_than_raising(self):
        """Same discipline as `capture.gmail._user_aware` and
        `TimezoneMiddleware`: a blank or unloadable zone falls back, it does
        not take the sync down."""
        found = chattime.extract_chat_time(
            "Let's do 4pm on 10 September.", sent_at=self.SENT, tz="Mars/Olympus"
        )
        assert found is not None
        assert found.when.hour == 16

    def test_the_evidence_is_the_sentence_it_was_read_from(self):
        found = _read(
            "Great to hear from you. Let's do 4pm tomorrow. I'll send a link.",
            self.SENT,
        )
        assert found.evidence == "Let's do 4pm tomorrow."
        assert len(found.evidence) <= 255


class TestVisibleBody:
    @pytest.mark.parametrize("trailer", [
        "\nOn Tue, Aug 25, 2026 at 5:57 AM Lily Liu <l@b.com> wrote:\n> Can we do 5:30?",
        "\n-----Original Message-----\nFrom: Lily\nCan we do 5:30?",
        "\n________________________________\nFrom: Lily\nCan we do 5:30?",
        "\n> Can we do 5:30?",
        "\nBegin forwarded message:\nCan we do 5:30?",
    ])
    def test_every_quoting_style_is_cut(self, trailer):
        assert chattime.visible_body("Of course!" + trailer) == "Of course!"

    def test_a_message_with_no_quote_survives_whole(self):
        assert chattime.visible_body("Of course!") == "Of course!"

    def test_empty_input_is_empty_output(self):
        assert chattime.visible_body(None) == ""
        assert chattime.visible_body("") == ""
