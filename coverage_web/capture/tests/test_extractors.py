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
