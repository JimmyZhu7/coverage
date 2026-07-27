"""Unit tests for the deterministic extractors — no DB, no Django, no LLM."""

from __future__ import annotations

from capture import extractors
from capture.providers import InboundEmailProvider


USER = "student@example.com"


def _parse(payload):
    return InboundEmailProvider().parse(payload)


def test_direction_outbound_when_user_is_sender(make_payload, capture_addr):
    payload = make_payload(
        from_email=USER, to=[("jane@bank.example", "Jane")], capture_addr=capture_addr
    )
    parsed = _parse(payload)
    assert extractors.detect_direction(parsed, USER) == "outbound"


def test_direction_inbound_when_counterparty_is_sender(make_payload, capture_addr):
    payload = make_payload(
        from_email="jane@bank.example", from_name="Jane", capture_addr=capture_addr,
        to=[(USER, "Student")],
    )
    parsed = _parse(payload)
    assert extractors.detect_direction(parsed, USER) == "inbound"


def test_bounce_detected_from_mailer_daemon(make_payload, capture_addr):
    payload = make_payload(
        from_email="mailer-daemon@bank.example", capture_addr=capture_addr,
        subject="Delivery Status Notification (Failure)",
        text="Your message wasn't delivered to jane@bank.example.",
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.signals.get("bounced") is True
    assert cls.touch_kind is None
    assert cls.needs_review is False


def test_autoreply_routed_to_needs_review(make_payload, capture_addr):
    payload = make_payload(
        from_email="jane@bank.example", capture_addr=capture_addr, to=[(USER, "")],
        subject="Automatic reply: Out of office",
        headers=[("Auto-Submitted", "auto-replied")],
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.needs_review is True
    assert cls.review_reason == "auto_reply"
    assert cls.touch_kind is None


def test_inbound_plain_reply_is_reply_received(make_payload, capture_addr):
    payload = make_payload(
        from_email="jane@bank.example", capture_addr=capture_addr, to=[(USER, "")],
        subject="Re: coffee", text="Thanks for reaching out, happy to help.",
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.touch_kind == extractors.KIND_REPLY
    assert cls.signals.get("replied") is True


def test_inbound_ics_future_is_chat_scheduled(make_payload, capture_addr, ics_builder, future_dt):
    payload = make_payload(
        from_email="jane@bank.example", capture_addr=capture_addr, to=[(USER, "")],
        subject="Invite", ics_text=ics_builder(future_dt),
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.touch_kind == extractors.KIND_CHAT_SCHEDULED
    assert cls.signals.get("chat_scheduled") is True
    assert "chat_scheduled_at" in cls.signals


def test_inbound_ics_past_is_chat_completed(make_payload, capture_addr, ics_builder, past_dt):
    payload = make_payload(
        from_email="jane@bank.example", capture_addr=capture_addr, to=[(USER, "")],
        subject="Invite", ics_text=ics_builder(past_dt),
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.touch_kind == extractors.KIND_CHAT
    assert cls.signals.get("chat_completed") is True


def test_outbound_text_scheduling_stays_outreach(make_payload, capture_addr):
    """The user proposing a time in their own outbound mail must NOT be read as
    a scheduled chat — we never infer a stage from the user talking."""
    payload = make_payload(
        from_email=USER, to=[("jane@bank.example", "Jane")], capture_addr=capture_addr,
        subject="Intro", text="Would love to chat — are you free at 3pm Tuesday?",
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.touch_kind == extractors.KIND_OUTREACH
    assert cls.signals.get("outreach_sent") is True


def test_unparseable_sender_needs_review(make_payload, capture_addr):
    payload = make_payload(from_email="", capture_addr=capture_addr, to=[(USER, "")])
    # blank From
    payload["FromFull"] = {"Email": "", "Name": ""}
    payload["From"] = ""
    cls = extractors.classify(_parse(payload), USER)
    assert cls.needs_review is True
    assert cls.review_reason == "unparseable_sender"


def test_extraction_version_stamped(make_payload, capture_addr):
    payload = make_payload(from_email="jane@bank.example", capture_addr=capture_addr, to=[(USER, "")])
    cls = extractors.classify(_parse(payload), USER)
    assert cls.signals["extraction_version"] == extractors.EXTRACTION_VERSION


# --------------------------------------------------------------------------- #
# Bounce: WHICH address bounced, not just that one did.
# --------------------------------------------------------------------------- #

def test_bounced_recipient_from_x_failed_recipients_header(make_payload, capture_addr):
    """The sender of a bounce is mailer-daemon -- never the address that
    actually failed. X-Failed-Recipients is the most authoritative source
    for that address when present."""
    payload = make_payload(
        from_email="mailer-daemon@bank.example", capture_addr=capture_addr,
        subject="Delivery Status Notification (Failure)",
        text="550 5.1.1 user unknown",
        headers=[("X-Failed-Recipients", "jane@bank.example")],
    )
    parsed = _parse(payload)
    assert extractors.detect_bounce(parsed)
    assert extractors.bounced_recipient(parsed) == "jane@bank.example"


def test_bounced_recipient_absent_returns_empty_not_a_guess(make_payload, capture_addr):
    """No X-Failed-Recipients, no DSN attachment, no attached original -- this
    function must admit it doesn't know rather than fall back to the
    mailer-daemon sender."""
    payload = make_payload(
        from_email="mailer-daemon@bank.example", capture_addr=capture_addr,
        subject="Delivery Status Notification (Failure)",
        text="550 5.1.1 user unknown",
    )
    parsed = _parse(payload)
    assert extractors.detect_bounce(parsed)
    assert extractors.bounced_recipient(parsed) == ""


def test_classify_bounce_records_failed_recipient_signal(make_payload, capture_addr):
    payload = make_payload(
        from_email="mailer-daemon@bank.example", capture_addr=capture_addr,
        subject="Delivery Status Notification (Failure)",
        text="550 5.1.1 user unknown",
        headers=[("X-Failed-Recipients", "jane@bank.example")],
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.signals.get("bounced") is True
    assert cls.signals.get("failed_recipient") == "jane@bank.example"


# --------------------------------------------------------------------------- #
# Bounce: soft body language may CORROBORATE, never convict.
#
# The bounce branch is first in `classify` and returns touch_kind=None with
# needs_review=False, which the pipeline records as `applied` and forgets. It
# is the only verdict in the module that discards a message, so it is the only
# one that must never be reachable from a phrase a human can write on purpose.
# --------------------------------------------------------------------------- #

# Every soft phrase, each shown inside a sentence a real banker might write.
_HUMAN_SENTENCES_WITH_BOUNCE_WORDS = [
    ("does not exist",
     "Our sophomore programme does not exist this year, but happy to chat."),
    ("was not delivered",
     "Your note was not delivered to me until Monday — sorry for the delay."),
    ("user unknown",
     "The portal kept saying user unknown when I tried to refer you."),
    ("no such user",
     "IT told me there is no such user on the old system, use my new address."),
    ("550 5.1.1",
     "The bounce I got read 550 5.1.1 — try my personal address instead."),
    ("recipient address rejected",
     "Compliance says any recipient address rejected by the filter needs a "
     "ticket. Anyway — free Thursday?"),
    ("message could not be delivered",
     "I heard your message could not be delivered to Tom; I'll pass it on."),
    ("wasn't delivered to",
     "Your CV wasn't delivered to the team until after the cut, resending now."),
]


def test_a_human_reply_containing_bounce_words_is_never_dropped(
    make_payload, capture_addr
):
    """Each soft phrase, in a genuine reply. None of them may produce a bounce
    verdict, because a bounce verdict is a silent deletion: no touch, no
    contact, no trace."""
    for token, sentence in _HUMAN_SENTENCES_WITH_BOUNCE_WORDS:
        payload = make_payload(
            from_email="jane@bank.example", from_name="Jane Banker",
            capture_addr=capture_addr, to=[(USER, "")],
            subject="Re: coffee chat", text=sentence,
        )
        parsed = _parse(payload)
        assert not extractors.detect_bounce(parsed), token
        cls = extractors.classify(parsed, USER)
        assert cls.signals.get("bounced") is not True, token
        # Not dropped: it lands in the human queue, with the reason named.
        assert cls.needs_review is True, token
        assert cls.review_reason == "unconfirmed_bounce", token
        assert cls.signals.get("bounce_language") == token
        assert token in cls.signals.get("evidence_quote", "")


def test_a_real_dsn_still_classifies_as_a_bounce(make_payload, capture_addr):
    """The other half of the ruling: nothing about a genuine delivery-status
    report changed. Structural signal present, so the body token corroborates
    it instead of being ignored."""
    payload = make_payload(
        from_email="mailer-daemon@bank.example", capture_addr=capture_addr,
        subject="Delivery Status Notification (Failure)",
        text="550 5.1.1 user unknown — the recipient does not exist.",
        headers=[("X-Failed-Recipients", "jane@bank.example")],
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.signals.get("bounced") is True
    assert cls.touch_kind is None
    assert cls.needs_review is False


def test_each_structural_signal_alone_still_convicts(make_payload, capture_addr):
    """A bounce needs no body language at all — the four structural signals
    are each sufficient on their own, so tightening the body scan did not
    narrow real bounce detection."""
    import base64

    bodyless = "Your message could not be handed to the remote host."
    # 1. mailer-daemon sender
    p = make_payload(from_email="MAILER-DAEMON@bank.example",
                     capture_addr=capture_addr, subject="hello", text=bodyless)
    assert extractors.detect_bounce(_parse(p))
    # 2. X-Failed-Recipients
    p = make_payload(from_email="postmaster-ish@bank.example",
                     capture_addr=capture_addr, subject="hello", text=bodyless,
                     headers=[("X-Failed-Recipients", "jane@bank.example")])
    assert extractors.detect_bounce(_parse(p))
    # 3. report-type=delivery-status content type
    p = make_payload(from_email="noreply@bank.example", capture_addr=capture_addr,
                     subject="hello", text=bodyless,
                     headers=[("Content-Type",
                               'multipart/report; report-type=delivery-status')])
    assert extractors.detect_bounce(_parse(p))
    # 4. an attached message/delivery-status part
    p = make_payload(from_email="noreply@bank.example", capture_addr=capture_addr,
                     subject="hello", text=bodyless)
    p["Attachments"] = [{
        "Name": "status.txt", "ContentType": "message/delivery-status",
        "Content": base64.b64encode(
            b"Final-Recipient: rfc822; jane@bank.example"
        ).decode("ascii"),
        "ContentLength": 42,
    }]
    assert extractors.detect_bounce(_parse(p))


def test_bounce_evidence_names_the_matched_signal(make_payload, capture_addr):
    """`_bounce_evidence` used to re-check only subject and sender and fall
    through to a generic "delivery-failure pattern" — which is what the two
    MOST authoritative routes (X-Failed-Recipients, an attached DSN) both
    recorded."""
    payload = make_payload(
        from_email="noreply@bank.example", capture_addr=capture_addr,
        subject="hello", text="nothing failure-shaped here",
        headers=[("X-Failed-Recipients", "jane@bank.example")],
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.signals["evidence_quote"] == "header: X-Failed-Recipients"
    assert "delivery-failure pattern" not in cls.signals["evidence_quote"]


def test_bounce_evidence_records_both_signal_and_corroboration(
    make_payload, capture_addr
):
    payload = make_payload(
        from_email="mailer-daemon@bank.example", capture_addr=capture_addr,
        subject="hello", text="550 5.1.1 mailbox unavailable",
    )
    evidence = extractors._bounce_evidence(_parse(payload))
    assert evidence == "sender: mailer-daemon + body: 550 5.1.1"


# --------------------------------------------------------------------------- #
# Scheduling: the quoted thread does not get a vote, and a bare clock time is
# not a meeting.
# --------------------------------------------------------------------------- #

# What every threaded reply on earth carries below the new text.
_GMAIL_QUOTE = (
    "On Mon, Jul 20, 2026 at 9:12 AM Student <student@example.com> wrote:\n"
    "> Hi Jane — would love 15 minutes if you have any.\n"
    "> Best, Student\n"
)


def test_quoted_thread_time_does_not_schedule_a_chat(make_payload, capture_addr):
    """THE regression. `text_body` carries the whole thread, and the quote
    header always contains a clock time, so the old bare-time fallback
    classified essentially EVERY reply as chat_scheduled — suppressing the
    correct "they replied, propose a chat" nudge and later asking "did the
    chat happen?" about a chat nobody scheduled."""
    payload = make_payload(
        from_email="jane@bank.example", capture_addr=capture_addr, to=[(USER, "")],
        subject="Re: coffee chat",
        text="Thanks for reaching out — happy to help.\n\n" + _GMAIL_QUOTE,
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.touch_kind == extractors.KIND_REPLY
    assert cls.signals.get("replied") is True
    assert "chat_scheduled" not in cls.signals


def test_a_bare_time_in_live_text_is_not_a_meeting(make_payload, capture_addr):
    """The fallback is gone outright, not merely quote-guarded: "3pm" in a
    live sentence is as often "I'm in a meeting until 3pm" as it is a
    proposal, and over-calling fabricates a meeting the product then thanks
    the user for."""
    payload = make_payload(
        from_email="jane@bank.example", capture_addr=capture_addr, to=[(USER, "")],
        subject="Re: coffee chat",
        text="I'm on the desk until 3pm most days, so mornings are easier.",
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.touch_kind == extractors.KIND_REPLY


def test_outlook_quote_marker_is_stripped_too(make_payload, capture_addr):
    payload = make_payload(
        from_email="jane@bank.example", capture_addr=capture_addr, to=[(USER, "")],
        subject="RE: coffee chat",
        text=(
            "Happy to help.\n\n"
            "-----Original Message-----\n"
            "Sent: Monday, July 20, 2026 9:12 AM\n"
            "Shall we set up a call at 4:00?\n"
        ),
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.touch_kind == extractors.KIND_REPLY, (
        "the user's OWN earlier proposal, quoted back, must not schedule"
    )


def test_an_explicit_phrase_still_schedules(make_payload, capture_addr):
    """The surviving text rung. An explicit phrase in the LIVE part of the
    message is still enough."""
    payload = make_payload(
        from_email="jane@bank.example", capture_addr=capture_addr, to=[(USER, "")],
        subject="Re: coffee chat",
        text="Sure — let's schedule a call. Here's my zoom link.\n\n" + _GMAIL_QUOTE,
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.touch_kind == extractors.KIND_CHAT_SCHEDULED
    assert cls.signals.get("chat_scheduled") is True


def test_an_ics_still_schedules(make_payload, capture_addr, ics_builder, future_dt):
    """The .ics rung is untouched — it carries a real, deterministic tense."""
    payload = make_payload(
        from_email="jane@bank.example", capture_addr=capture_addr, to=[(USER, "")],
        subject="Invite", text="See attached.\n\n" + _GMAIL_QUOTE,
        ics_text=ics_builder(future_dt),
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.touch_kind == extractors.KIND_CHAT_SCHEDULED
    assert cls.signals.get("chat_scheduled_at")


def test_strip_quoted_keeps_the_new_text_only():
    body = "Live sentence.\n\n" + _GMAIL_QUOTE
    stripped = extractors.strip_quoted(body)
    assert "Live sentence." in stripped
    assert "9:12 AM" not in stripped
    assert "15 minutes" not in stripped


# --------------------------------------------------------------------------- #
# Forwards.
# --------------------------------------------------------------------------- #

def test_forwarded_subject_routes_to_needs_review(make_payload, capture_addr):
    """A forward's envelope describes the STUDENT, not the exchange inside it:
    From is the student, so direction reads outbound and the counterparty
    search finds nobody. Acting on that recorded the student as having SENT
    mail they in fact RECEIVED."""
    payload = make_payload(
        from_email=USER, capture_addr=capture_addr, to=[],
        subject="Fwd: Re: coffee chat",
        text="Sharing this one.\n\n"
             "---------- Forwarded message ----------\n"
             "From: Jane Banker <jane@bank.example>\n"
             "Happy to help.\n",
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.needs_review is True
    assert cls.review_reason == "forward_unparsed"
    assert cls.touch_kind is None
    assert cls.signals.get("forwarded") is True


def test_forward_detected_from_body_marker_alone(make_payload, capture_addr):
    """Subject prefixes get edited off; the separator survives."""
    payload = make_payload(
        from_email=USER, capture_addr=capture_addr, to=[],
        subject="this one's interesting",
        text="---------- Forwarded message ----------\nFrom: Jane\n",
    )
    cls = extractors.classify(_parse(payload), USER)
    assert cls.review_reason == "forward_unparsed"


def test_forward_detected_from_an_rfc822_attachment(make_payload, capture_addr):
    """"Forward as attachment" leaves no body marker at all."""
    import base64

    payload = make_payload(
        from_email=USER, capture_addr=capture_addr, to=[], subject="see attached",
        text="",
    )
    payload["Attachments"] = [{
        "Name": "forwarded.eml", "ContentType": "message/rfc822",
        "Content": base64.b64encode(b"From: jane@bank.example").decode("ascii"),
        "ContentLength": 24,
    }]
    cls = extractors.classify(_parse(payload), USER)
    assert cls.review_reason == "forward_unparsed"


def test_a_re_chain_carrying_fw_still_counts_as_a_forward(make_payload, capture_addr):
    payload = make_payload(
        from_email=USER, capture_addr=capture_addr, to=[],
        subject="Re: FW: coffee chat", text="thoughts?",
    )
    assert extractors.detect_forward(_parse(payload))


def test_an_ordinary_reply_is_not_mistaken_for_a_forward(
    make_payload, capture_addr
):
    """The false-positive guard: "fw" must be matched as a prefix token, not
    as a substring of ordinary words."""
    payload = make_payload(
        from_email="jane@bank.example", capture_addr=capture_addr, to=[(USER, "")],
        subject="Re: your note about the FWIW deck",
        text="Happy to help. Forward-looking statements aside, let's talk.",
    )
    assert extractors.detect_forward(_parse(payload)) == ""
    cls = extractors.classify(_parse(payload), USER)
    assert cls.touch_kind == extractors.KIND_REPLY
