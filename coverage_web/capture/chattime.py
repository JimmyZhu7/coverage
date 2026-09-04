"""Reading a chat time out of prose — the one thing `gmail_live` refuses to
guess at, done deterministically enough to be allowed to.

WHY THIS EXISTS
---------------
`capture.gmail_live` puts a chat on the calendar only when a counterparty
returns an `.ics`. That rule is right about the failure it prevents — a
programme blast's webinar invite must never become "Chat with <recruiter>" —
and wrong about almost everything else, because almost nothing real produces
an `.ics`. Measured on the founder's live account (read-only, 2026-09-03):
the last `CalendarEvent` was 2026-08-05 while 43 `chat`/`chat_scheduled`
touches had landed since. His chats are arranged in sentences and held on the
phone. The calendar was empty of the thing it exists for.

So this module reads sentences. It is the same posture
`directory.classify.extract_deadline_from_text` already takes for a deadline
in a job posting, held to the same two rules:

* **Refusing beats guessing** (`capture.discovery`). Every ambiguity below
  resolves to None. Two readings that disagree, a time with no day and no
  chat to attach it to, a window instead of a booking, a sentence about a
  deadline — all of them make no event, and that is the correct outcome, not
  a miss.
* **Provenance travels with the reading.** Nothing here returns a time
  without also returning the sentence it came from and a confidence BELOW the
  one a structured field earns (`PROSE_CONFIDENCE` vs `STATED_CONFIDENCE`,
  the deadline pipeline's 0.6 vs 1.0). A caller that stores the datetime and
  drops the provenance has broken this module's contract.

WHAT IT IS NOT
--------------
Not a natural-language date library, and deliberately not one. It reads the
shapes a coffee chat is actually arranged in — "6pm tomorrow works great for
me", "sending an invite over for tomorrow 7:30 PM ET", "can we do 5:30?" —
and answers None to everything else. It has no model, no LLM, no network and
no Django import: it is a pure function of (text, when the message was sent,
which clock the reader keeps), which is what makes the whole of it testable
without a database.

THE THREE ANSWERS
-----------------
A message either books a time, proposes one, or says neither.

* `KIND_BOOKING` — somebody committed. "6pm tomorrow works great for me",
  "sending an invite over for tomorrow 7:30 PM ET", "see you at 3 on
  Thursday". This is the only kind that may PLACE a chat on a calendar.
* `KIND_PROPOSAL` — somebody asked. "maybe 6pm tomorrow?", "can we do 5:30?",
  "does Thursday at 3 work?". A proposal may MOVE a chat that already exists
  on the same thread — which is what a renegotiation is, and the prose twin
  of an iTIP COUNTER — and may never create one. An unanswered proposal is a
  question, and a question on a calendar is a lie.
* None — everything else, including the case this module is most careful
  about: an AVAILABILITY WINDOW. "Any time after 7PM ET works for me" carries
  a commitment phrase ("works for me"), a clock time and a zone, and books
  nothing at all. It is the counterparty describing when they COULD talk. The
  window vocabulary is checked before the commitment vocabulary for exactly
  this reason, and it wins.

WHAT THE CALLER STILL OWES
--------------------------
Everything about WHO. This module reads text; it does not know whether the
message was bulk, whether the sender is a person, whether the thread is a 1:1
with somebody already in Coverage, or which direction it travelled. Those are
`capture.gmail_live`'s guards and they are not optional — without them this
module is precisely the "programme blast on the calendar" bug the no-prose
doctrine was written to prevent, arriving through a different door.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The two confidence bands, and they are the deadline pipeline's own
# (`directory.ingest`: an API deadline is 1.0, a deadline read out of the
# posting's prose is 0.6, and the UI says "reported" for the second). A chat
# time is the same kind of claim in a different table, so it uses the same
# numbers rather than inventing a parallel vocabulary — `crm.calendar_views`
# can then ask one question, "is this below 1.0", of a deadline and a chat
# alike.
PROSE_CONFIDENCE = 0.6
STATED_CONFIDENCE = 1.0

KIND_BOOKING = "booking"
KIND_PROPOSAL = "proposal"

# How far ahead a prose booking may reach, and how far behind. A chat arranged
# more than half a year out is not a chat, it is a misread year; a time that
# lands BEFORE the message stating it is a misread day. The backward tolerance
# is not zero because a zone we could not name can put an evening chat a few
# hours the wrong side of the send time.
MAX_HORIZON_DAYS = 180
BACKSTOP_HOURS = 12

# How near a day has to sit to a clock time before it is read as THAT clock
# time's day. Lily's "you can call me later today 615-989-0329 maybe 6pm
# tomorrow?" names two days in one sentence; "tomorrow" is one character from
# "6pm" and "today" is twenty away, and the near one is the one she meant.
_DATE_REACH = 60

# How far past a clock time a zone abbreviation may sit and still belong to
# it. Wide enough for "4:30 PM Pacific Time (US and Canada)", narrow enough
# that a stray "ET" in a signature cannot re-zone a time three lines up.
_ZONE_REACH = 30


@dataclass(frozen=True)
class ChatTime:
    """One reading of one message.

    `when` is AWARE only when the message named a zone ("7:30 PM ET"). With no
    zone stated it is NAIVE, which in this codebase means "this clock time on
    the account owner's own clock" — the same contract
    `gmail_live._extract_ics_schedule` returns a floating DTSTART under, and
    `capture.gmail._user_aware` is the one place that anchors it. Anchoring
    here would duplicate that decision in a second place.

    `dated` is False when the message stated a clock and no day: "can we do
    5:30?". `when` then carries the message's OWN local date purely as a
    carrier for the clock, and a caller may use nothing from it but the time
    of day. A dateless reading can only ever move a chat that already has a
    day; on its own it means nothing, because "5:30" with no answer to "which
    5:30" is not a fact.
    """

    when: datetime
    dated: bool
    kind: str
    confidence: float
    evidence: str

    @property
    def books(self) -> bool:
        """True when this reading may PLACE a chat, not merely move one. A
        proposal never may, and neither does a booking with no day."""
        return self.kind == KIND_BOOKING and self.dated


# --------------------------------------------------------------------------- #
# Zones
# --------------------------------------------------------------------------- #
#
# Only zones a student's mail actually names, and only ones whose abbreviation
# is unambiguous. "IST" is deliberately absent: it is India, Ireland and
# Israel at once, and a wrong guess among three is worse than no guess. Same
# rule as everywhere else here.
_ZONE_NAMES = {
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
    "eastern": "America/New_York",
    "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
    "central": "America/Chicago",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
    "mountain": "America/Denver",
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles", "pacific": "America/Los_Angeles",
    "utc": "UTC", "gmt": "UTC", "bst": "Europe/London",
    "cet": "Europe/Paris", "cest": "Europe/Paris",
    "hkt": "Asia/Hong_Kong", "sgt": "Asia/Singapore", "jst": "Asia/Tokyo",
}
_ZONE_RE = re.compile(
    r"\b(" + "|".join(sorted(_ZONE_NAMES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2, "thursday": 3, "thu": 3,
    "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sunday": 6,
}

# --------------------------------------------------------------------------- #
# What a clock time looks like, and what one does NOT
# --------------------------------------------------------------------------- #
#
# Two shapes only: a number with am/pm ("6pm", "6:30 pm"), or a number with a
# COLON and two digits ("5:30", "17:30"). A bare number is never a time — "can
# we do 6?" is real English and so is "6 of us", and no reading of a lone
# digit is safe enough to put on a calendar.
#
# The lookarounds are the phone-number guard, and it is not theoretical: "you
# can call me later today 615-989-0329 maybe 6pm tomorrow?" is a real sentence
# from the founder's mailbox with a phone number and a chat time in it.
#
# They differ between the two shapes on purpose. The am/pm shape needs the
# strict one — a digit, dot, slash or HYPHEN touching it means the digits
# belong to something longer, which is exactly how "...-0312 am" would
# otherwise be read as midday. The colon shape needs the loose one: a phone
# number has no colon in it, so the hyphen was buying nothing there and
# costing "5:30-6:30", where it refused BOTH ends of a perfectly ordinary
# range and returned nothing at all.
_CLOCK_RE = re.compile(
    r"(?<![\d:.\-/])(\d{1,2})(?::(\d{2}))?\s*([ap])m\b"
    r"|(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])",
    re.IGNORECASE,
)

# A RANGE IS ITS START. "let's do 5:30-6:30" states one meeting, not two times
# that disagree, and refusing it as ambiguous would be pedantry rather than
# honesty. Only joiners that genuinely mean "through" count.
_RANGE_JOIN_RE = re.compile(r"^\s*(?:-|–|—|to|until|till|thru|through)\s*$", re.I)

# --------------------------------------------------------------------------- #
# The vocabularies
# --------------------------------------------------------------------------- #
#
# AVAILABILITY IS CHECKED FIRST AND WINS OUTRIGHT. Freddy Guerrero's "any time
# after 7PM ET works for me" is the case this ordering exists for: it carries
# a commitment phrase, a clock and a zone, and it books nothing. He is
# describing a window. Read for its commitment phrase alone it would have put
# a 7pm chat on the calendar that nobody agreed to.
_WINDOW_RE = re.compile(
    r"\bany\s?time\b|\bwhenever\b|\bsome\s?time\b|\bafter\b|\bbefore\b"
    r"|\bbetween\b|\bonwards?\b|\bor later\b|\bor earlier\b|\bor after\b"
    r"|\ball day\b|\bany day\b|\beither day\b|\bmost (?:mornings|afternoons|"
    r"evenings|days|weekdays)\b|\bavailability\b|\bflexible\b"
    r"|\bwork(?:s|ing)? around\b|\bopen (?:from|between)\b",
    re.IGNORECASE,
)

# A DEADLINE IS NOT AN APPOINTMENT. "Applications close Oct 30" and "the
# deadline is 30 October at 5pm" are dates about a posting, and
# `directory.ingest` already owns them. A chat pipeline reading them would put
# a firm's cut-off on the student's calendar as a meeting with a person.
_DEADLINE_RE = re.compile(
    r"\bdeadline\b|\bapplications? (?:close|are due|open)\b|\bcloses? on\b"
    r"|\bdue (?:by|on|date)\b|\bsubmit by\b|\bapply by\b|\bcut.?off\b"
    r"|\bexpires?\b|\brsvp by\b|\bregistration closes\b",
    re.IGNORECASE,
)

# A MAILBOX ANSWERING FOR ITS OWNER STATES NO APPOINTMENTS. An out-of-office
# names dates and times constantly ("back on 8 September", "I check email at
# 9am"), all of them about absence. `capture.inbound` already flags most of
# these as `auto_submitted` and `gmail_live` refuses them on that alone; this
# is the same refusal made available to a caller with no headers to read, so
# the pure function cannot be talked into an answer the pipeline would reject.
_OOO_RE = re.compile(
    r"\bout of (?:the )?office\b|\bautomatic reply\b|\bauto.?reply\b"
    r"|\bon (?:annual |parental |maternity |paternity )?leave\b"
    r"|\baway from (?:my|the) (?:desk|office|email)\b"
    r"|\blimited access to (?:my )?e-?mail\b"
    r"|\bcurrently (?:traveling|travelling)\b"
    r"|\bwill be back (?:in|on)\b|\breturning to the office\b"
    r"|\bi am (?:currently )?unavailable\b",
    re.IGNORECASE,
)

# SOMEBODY COMMITTED. These are the sentences a chat is actually agreed in,
# collected from the founder's own threads rather than imagined.
_BOOKING_RE = re.compile(
    r"\bworks? (?:great |well |perfectly |fine |better )?for (?:me|us|you)\b"
    r"|\bthat works\b|\bworks? (?:great|well|fine|better|best)\b"
    r"|\bthat'?s perfect\b"
    r"|\bsounds (?:good|great|perfect)\b"
    r"|\blet'?s (?:do|say|make it|go with|meet|talk|speak|chat|connect)\b"
    r"|\bsee you (?:at|then|on|tomorrow)\b"
    r"|\b(?:talk|speak|chat) (?:to |with )?(?:you )?(?:at|then|on)\b"
    r"|\b(?:i'?ll|i will|we'?ll) (?:give you a )?call\b"
    r"|\bcalling you\b|\bi'?ll ring\b"
    r"|\b(?:send|sending|shoot|shooting|fire|firing|put|putting)"
    r"(?: over| out| across)? (?:an?|the|you an?) (?:calendar |zoom |teams )?invite\b"
    r"|\binvite (?:for|at)\b|\bmov(?:e|ing|ed) the invite\b"
    r"|\bconfirm(?:ed|ing)?\b|\bbooked\b|\bpencil(?:ed)? (?:you )?in\b"
    r"|\blocked in\b|\bschedul(?:ed|ing) for\b|\bset for\b"
    r"|\bour (?:call|chat|conversation|meeting) (?:at|on|is)\b"
    r"|\blooking forward to (?:speaking|chatting|talking|our|meeting)\b"
    r"|\bhere'?s (?:a|the|my) (?:zoom|google meet|meet|teams|calendar|dial.?in)\b"
    r"|(?:^|[\n:])\s*time\s*:",
    re.IGNORECASE | re.MULTILINE,
)

# SOMEBODY ASKED. A proposal may move a chat that exists and may never make
# one, so the cost of reading a booking as a proposal is low and the cost of
# the reverse is a meeting nobody agreed to. When both vocabularies fire, the
# proposal wins.
_PROPOSAL_RE = re.compile(
    r"\b(?:can|could|shall|should|would) (?:we|you|i)\b|\bhow about\b"
    r"|\bwhat about\b|\bdoes .{0,24}\bwork\b|\bwould .{0,24}\bwork\b"
    r"|\bare you (?:free|available|around)\b|\bwould you be (?:free|available)\b"
    r"|\bmaybe\b|\bperhaps\b|\b(?:i|we) could do\b|\bif that works\b"
    r"|\blet me know\b|\bdoes that work\b|\bopen to\b|\bhappy to do\b",
    re.IGNORECASE,
)

_REL_DAY_RE = re.compile(
    r"\b(the day after tomorrow|tomorrow|tmrw|tmr|tonight|today"
    r"|this (?:afternoon|evening|morning))\b",
    re.IGNORECASE,
)
_WEEKDAY_RE = re.compile(
    r"\b(?:next|this|coming|on)?\s*\b("
    + "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)
# THE MONTH HAS TO END WHERE THE MONTH ENDS. A three-letter prefix followed
# by "any letters" reads "maybe 6pm tomorrow?" as May the 6th — a real
# sentence from the founder's mailbox, turned into a date eight months out by
# a lazy suffix. Each month is spelled out to its own word boundary instead.
_MONTH_WORD = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_MONTH_DAY_RE = re.compile(
    r"\b(" + _MONTH_WORD + r")\b\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?",
    re.IGNORECASE,
)
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH_WORD + r")\b"
    r"(?:,?\s+(\d{4}))?",
    re.IGNORECASE,
)
_SLASH_DATE_RE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])")


# --------------------------------------------------------------------------- #
# Quoted text
# --------------------------------------------------------------------------- #

_QUOTE_CUTS = (
    # Gmail's attribution line, which wraps across lines in the wild.
    re.compile(r"\n[ \t]*On\s(?:.|\n){0,300}?\bwrote:", re.IGNORECASE),
    re.compile(r"\n[ \t]*-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"\n[ \t]*_{10,}"),
    re.compile(
        r"\n[ \t]*From:.{0,200}?\n[ \t]*(?:Sent|Date):", re.IGNORECASE | re.DOTALL
    ),
    re.compile(r"\n[ \t]*>"),
    re.compile(r"\n[ \t]*Begin forwarded message:", re.IGNORECASE),
)


def visible_body(text: str | None) -> str:
    """`text` with the quoted reply trailer removed.

    THIS IS A CORRECTNESS RULE, NOT TIDYING. A reply carries the previous
    message underneath it, so reading a whole reply body attributes the OTHER
    person's sentences to this sender and this timestamp. Jimmy's "Of course!
    I'll move the invite." quotes Lily's "Can we do 5:30?" directly beneath
    it; read as one text it becomes him booking 5:30 — the right answer
    reached by fabricating who said it, and the same machinery would just as
    happily resurrect a time out of a thread three weeks stale.
    """
    body = (text or "").replace("\r\n", "\n")
    if not body:
        return ""
    cut = len(body)
    for pattern in _QUOTE_CUTS:
        match = pattern.search(body)
        if match is not None:
            cut = min(cut, match.start())
    return body[:cut].strip()


def _strip_tags(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    for entity, char in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&rsquo;", "'"), ("&ndash;", "-"),
    ):
        text = text.replace(entity, char)
    return text


def _normalise(text: str) -> str:
    """Length-preserving tidy-ups, so every offset below still indexes the
    same characters the evidence string is cut from.

    "p.m." becomes "pm" plus padding rather than "pm": the sentence splitter
    breaks on full stops, and "6 p.m. tomorrow" split on its own abbreviation
    is three fragments with the time in one and the day in another.
    """

    def _pad(match: re.Match) -> str:
        head = match.group(1) + "m"
        return head + " " * (len(match.group(0)) - len(head))

    text = re.sub(r"\b([ap])\.\s?m\.?", _pad, text, flags=re.IGNORECASE)
    # A month abbreviation's full stop, for the same splitter: "Sept. 3".
    text = re.sub(
        r"\b(jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec)\.",
        lambda m: m.group(1) + " ",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _sentences(text: str) -> list[tuple[int, str]]:
    """(offset, sentence) for each sentence, offsets into `text`."""
    out: list[tuple[int, str]] = []
    start = 0
    for match in re.finditer(r"[.!?\n;]+", text):
        chunk = text[start:match.end()]
        if chunk.strip():
            out.append((start, chunk))
        start = match.end()
    tail = text[start:]
    if tail.strip():
        out.append((start, tail))
    return out


def _zone_for(text: str, at: int) -> ZoneInfo | None:
    """The zone named just after the clock match ending at `at`, or None."""
    match = _ZONE_RE.search(text, at, at + _ZONE_REACH)
    if match is None:
        return None
    try:
        return ZoneInfo(_ZONE_NAMES[match.group(1).lower()])
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - tzdata gap
        return None


def _clock_of(match: re.Match) -> tuple[int, int] | None:
    """(hour, minute) from a clock match, or None when the digits cannot be a
    time at all ("13pm", "25:00").

    THE BARE-TIME RULE, stated out loud because it IS a convention and not a
    fact. "5:30" names no half of the day. Hours 1-6 are read as afternoon,
    7-11 as morning, 12 as noon — the same reading a calendar's quick-add
    makes, and the reason Lily's "can we do 5:30?" becomes half past five in
    the afternoon rather than an alarm clock. Anything already 13 or above is
    a 24-hour clock and is taken as written.
    """
    if match.group(3):  # am/pm stated
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if hour < 1 or hour > 12 or minute > 59:
            return None
        if match.group(3).lower() == "a":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        return hour, minute
    hour, minute = int(match.group(4)), int(match.group(5))
    if hour > 23 or minute > 59:
        return None
    if 1 <= hour <= 6:
        hour += 12
    return hour, minute


def _resolve_year(month: int, day: int, anchor: date, year: int | None):
    if year is not None:
        try:
            return date(year, month, day)
        except ValueError:
            return None
    for candidate in (anchor.year, anchor.year + 1):
        try:
            found = date(candidate, month, day)
        except ValueError:
            continue
        if found >= anchor - timedelta(days=1):
            return found
    try:
        return date(anchor.year, month, day)
    except ValueError:
        return None


def _date_candidates(text: str, anchor: date):
    """Every day this text names, as (start, end, date) — and, separately,
    every span that IS a day and could not be read.

    `anchor` is the day the message was sent ON THE READER'S OWN CLOCK. Every
    relative date resolves against that and never against now(), because
    "tomorrow" in an email sent on 24 August is 25 August forever — including
    when the first-connect backfill reads that message in December.

    THE SECOND LIST IS THE DIFFERENCE BETWEEN "no day" AND "a day we could not
    read", and they must not end up meaning the same thing. "Let's do 3pm on
    9/3" states a date; day-first and month-first are both real conventions,
    so there is no answer. Dropped silently, what is left is a bare 3pm — a
    dateless reading, which this module lets move an existing chat's clock.
    That would move a chat to 3pm on the strength of a sentence that was
    talking about a different day entirely.
    """
    found: list[tuple[int, int, date]] = []
    unreadable: list[tuple[int, int]] = []

    for match in _REL_DAY_RE.finditer(text):
        word = match.group(1).lower()
        if word == "the day after tomorrow":
            offset = 2
        elif word in ("tomorrow", "tmrw", "tmr"):
            offset = 1
        else:
            offset = 0
        found.append((match.start(), match.end(), anchor + timedelta(days=offset)))

    for match in _WEEKDAY_RE.finditer(text):
        target = _WEEKDAYS[match.group(1).lower()]
        ahead = (target - anchor.weekday()) % 7 or 7
        found.append((match.start(1), match.end(1), anchor + timedelta(days=ahead)))

    for match in _MONTH_DAY_RE.finditer(text):
        month = _MONTHS[match.group(1).lower()[:3]]
        year = int(match.group(3)) if match.group(3) else None
        when = _resolve_year(month, int(match.group(2)), anchor, year)
        if when is not None:
            found.append((match.start(), match.end(), when))

    for match in _DAY_MONTH_RE.finditer(text):
        month = _MONTHS[match.group(2).lower()[:3]]
        year = int(match.group(3)) if match.group(3) else None
        when = _resolve_year(month, int(match.group(1)), anchor, year)
        if when is not None:
            found.append((match.start(), match.end(), when))

    for match in _SLASH_DATE_RE.finditer(text):
        first, second = int(match.group(1)), int(match.group(2))
        raw_year = match.group(3)
        year = None
        if raw_year:
            year = int(raw_year)
            if year < 100:
                year += 2000
        # `directory.classify.extract_deadline_from_text`'s own rule, reused
        # rather than restated differently: day-first and month-first are both
        # real conventions, and when both readings are possible there is no
        # answer, only a coin toss.
        if first > 12 and second <= 12:
            month, day = second, first
        elif second > 12 and first <= 12:
            month, day = first, second
        else:
            unreadable.append((match.start(), match.end()))
            continue
        when = _resolve_year(month, day, anchor, year)
        if when is not None:
            found.append((match.start(), match.end(), when))
        else:
            unreadable.append((match.start(), match.end()))

    return found, unreadable


def _gap(span: tuple[int, int], start: int, end: int) -> int:
    if span[1] <= start:
        return start - span[1]
    if span[0] >= end:
        return span[0] - end
    return 0


def _nearest_date(dates, start: int, end: int):
    best = None
    best_gap = _DATE_REACH + 1
    for d_start, d_end, value in dates:
        gap = _gap((d_start, d_end), start, end)
        if gap < best_gap:
            best, best_gap = value, gap
    return best


def _as_utc(when: datetime, zone: ZoneInfo) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=zone).astimezone(dt_timezone.utc)
    return when.astimezone(dt_timezone.utc)


def _user_zone(tz: str | None) -> ZoneInfo:
    name = (tz or "").strip()
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def extract_chat_time(
    text: str | None,
    *,
    sent_at: datetime | None,
    tz: str | None = None,
) -> ChatTime | None:
    """The chat time this message states, or None.

    `sent_at` is when the message was SENT — the message's own timestamp, not
    now(). Every relative date resolves against it (see `_date_candidates`),
    and a caller with no timestamp gets None rather than a date computed off
    the clock on the wall during the sync: a backfill reading August's mail in
    December would otherwise book every "tomorrow" in December.

    `tz` is the account owner's IANA zone (`accounts.User.timezone`). It does
    two jobs and only two: it decides which calendar day the message was sent
    ON, and it anchors the comparison when a message states two times in
    different zones. It is NOT stamped onto the answer — a time with no zone
    in the text comes back NAIVE, which this codebase already reads as "the
    account owner's own clock" (`capture.gmail._user_aware`).
    """
    if sent_at is None:
        return None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=dt_timezone.utc)

    body = _normalise(visible_body(_strip_tags(text or "")))
    if not body or _OOO_RE.search(body):
        return None

    zone = _user_zone(tz)
    anchor = sent_at.astimezone(zone).date()
    dates, unreadable_dates = _date_candidates(body, anchor)
    floor = sent_at - timedelta(hours=BACKSTOP_HOURS)
    ceiling = sent_at + timedelta(days=MAX_HORIZON_DAYS)

    candidates: list[ChatTime] = []
    for offset, sentence in _sentences(body):
        if _WINDOW_RE.search(sentence) or _DEADLINE_RE.search(sentence):
            continue
        booking = _BOOKING_RE.search(sentence) is not None
        proposal = _PROPOSAL_RE.search(sentence) is not None or "?" in sentence
        if not booking and not proposal:
            # A time nobody committed to and nobody asked about — "the 3pm
            # slot filled up", "I saw your 9am email". Mentioning a clock is
            # not arranging one.
            continue
        kind = KIND_PROPOSAL if proposal else KIND_BOOKING

        matches = list(_CLOCK_RE.finditer(sentence))
        for index, match in enumerate(matches):
            if index and _RANGE_JOIN_RE.match(
                sentence[matches[index - 1].end():match.start()]
            ):
                # The far end of "5:30-6:30". Its start is the appointment.
                continue
            clock = _clock_of(match)
            if clock is None:
                continue
            start, end = offset + match.start(), offset + match.end()
            named_zone = _zone_for(body, end)
            day = _nearest_date(dates, start, end)
            if day is None and any(
                _gap(span, start, end) <= _DATE_REACH for span in unreadable_dates
            ):
                # A day is sitting right there and we could not read it. See
                # `_date_candidates` — degrading this to a dateless clock
                # would let a sentence about another day move a chat.
                continue
            dated = day is not None
            when = datetime.combine(day or anchor, datetime.min.time()).replace(
                hour=clock[0], minute=clock[1]
            )
            if named_zone is not None:
                when = when.replace(tzinfo=named_zone)
            if dated and not (floor <= _as_utc(when, zone) <= ceiling):
                # A day outside the horizon is a misread year or a misread
                # day, never a chat. See MAX_HORIZON_DAYS.
                continue
            candidates.append(
                ChatTime(
                    when=when,
                    dated=dated,
                    kind=kind,
                    confidence=PROSE_CONFIDENCE,
                    evidence=" ".join(sentence.split())[:255],
                )
            )

    if not candidates:
        return None

    # TWO READINGS THAT DISAGREE ARE NO READING. Jimmy's Zoom note states the
    # same appointment twice, "tomorrow 7:30 PM ET" and "September 3, 2026 at
    # 4:30 PM PT", and those are one instant in two zones — so it answers. A
    # message naming two DIFFERENT instants has not said which chat it means,
    # and taking the first is a coin toss wearing a rule.
    keys = {
        (c.dated, _as_utc(c.when, zone) if c.dated else (c.when.hour, c.when.minute))
        for c in candidates
    }
    if len(keys) > 1:
        return None

    candidates.sort(key=lambda c: (c.kind == KIND_PROPOSAL, not c.dated))
    return candidates[0]
