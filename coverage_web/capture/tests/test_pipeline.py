"""Pipeline integration tests against the REAL migrated schema + the ported
warmth ratchet.

``transaction=True`` for the same reason crm's own service tests need it: the
apply step calls ``crm.services.log_touch``, which opens a separate physical
psycopg connection (see crm/services.py) that can only see committed rows.
"""

from __future__ import annotations

import pytest

from analytics.models import ProductEvent
from capture import services
from crm.models import CaptureEvent, Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

USER_EMAIL = "student@example.com"


# --------------------------------------------------------------------------- #
# Headline: an outbound BCC to a known contact
# --------------------------------------------------------------------------- #

def test_outbound_bcc_to_known_contact_logs_touch(user, known_contact, capture_addr, make_payload):
    """CaptureEvent created, existing contact resolved (not duplicated), a
    touch logged. A first outbound outreach does NOT ratchet warmth by the
    ported domain rule (TOUCH_TRANSITIONS['outreach'] == (None, None)); warmth
    movement is proven separately on the reply path below."""
    payload = make_payload(
        from_email=USER_EMAIL,
        to=[(known_contact.email, "Jane Banker")],
        bcc=[(capture_addr, "")],
        capture_addr=capture_addr,
    )

    result = services.ingest_inbound_email(payload)

    assert result.status == "applied"
    events = CaptureEvent.objects.for_user(user)
    assert events.count() == 1
    ev = events.first()
    assert ev.provider == "bcc"
    assert ev.direction == "outbound"

    # Resolved to the existing contact — no pending duplicate created.
    assert Contact.objects.for_user(user).count() == 1
    touch = Touch.objects.for_user(user).get(contact=known_contact)
    assert touch.kind == "outreach"

    known_contact.refresh_from_db()
    assert known_contact.warmth == "cold"  # outreach is not a ratcheting kind


def test_inbound_reply_moves_warmth_cold_to_replied(user, known_contact, capture_addr, make_payload):
    """The 'warmth moved' proof: a captured inbound reply from a known contact
    ratchets warmth cold -> replied through the ported machine."""
    payload = make_payload(
        from_email=known_contact.email,
        from_name="Jane Banker",
        to=[(USER_EMAIL, "Student")],
        capture_addr=capture_addr,
        subject="Re: intro",
        text="Great to hear from you — happy to chat.",
    )

    result = services.ingest_inbound_email(payload)

    assert result.status == "applied"
    known_contact.refresh_from_db()
    assert known_contact.warmth == "replied"
    assert known_contact.thread_state == "replied"

    ev = CaptureEvent.objects.for_user(user).first()
    assert ev.provider == "forward"
    assert ev.status == "applied"

    # touch_logged instrumentation recorded with source=capture (§8 mix).
    logged = ProductEvent.all_objects.filter(user=user, event="touch_logged")
    assert logged.count() == 1
    assert logged.first().props.get("source") == "capture"


# --------------------------------------------------------------------------- #
# Idempotency — the load-bearing guarantee
# --------------------------------------------------------------------------- #

def test_duplicate_delivery_is_a_no_op(user, known_contact, capture_addr, make_payload):
    """Same payload (same Message-ID) twice -> exactly one CaptureEvent and one
    Touch. At-least-once webhook delivery must be safe."""
    payload = make_payload(
        from_email=known_contact.email,
        to=[(USER_EMAIL, "")],
        capture_addr=capture_addr,
        message_id="<dedup-me@mail.example>",
        text="Thanks, will do.",
    )

    first = services.ingest_inbound_email(payload)
    second = services.ingest_inbound_email(payload)

    assert first.status == "applied"
    assert second.status == "duplicate"
    assert CaptureEvent.objects.for_user(user).count() == 1
    assert Touch.objects.for_user(user).filter(contact=known_contact).count() == 1


# --------------------------------------------------------------------------- #
# Unknown address — rejected/counted, never crashes, never inserts
# --------------------------------------------------------------------------- #

def test_unknown_capture_address_is_rejected_and_counted(user, make_payload):
    payload = make_payload(
        from_email="jane@bank.example",
        to=[("u-doesnotexist@in.coverage.app", "")],
        capture_addr="u-doesnotexist@in.coverage.app",
    )

    result = services.ingest_inbound_email(payload)

    assert result.status == "ignored"
    assert result.http_status == 202
    assert CaptureEvent.all_objects.count() == 0
    rejected = ProductEvent.all_objects.filter(event="capture_email_received", props__routed=False)
    assert rejected.count() == 1


# --------------------------------------------------------------------------- #
# Ambiguous -> needs_review, no touch (deliberately NOT sent to an LLM)
# --------------------------------------------------------------------------- #

def test_autoreply_lands_in_needs_review_with_no_touch(user, known_contact, capture_addr, make_payload):
    payload = make_payload(
        from_email=known_contact.email,
        to=[(USER_EMAIL, "")],
        capture_addr=capture_addr,
        subject="Automatic reply: Out of office",
        headers=[("Auto-Submitted", "auto-replied")],
    )

    result = services.ingest_inbound_email(payload)

    assert result.status == "needs_review"
    ev = CaptureEvent.objects.for_user(user).first()
    assert ev.status == "needs_review"
    assert Touch.objects.for_user(user).count() == 0
    known_contact.refresh_from_db()
    assert known_contact.warmth == "cold"  # untouched


def test_bounce_is_applied_with_no_touch(user, known_contact, capture_addr, make_payload):
    payload = make_payload(
        from_email="mailer-daemon@bank.example",
        to=[(USER_EMAIL, "")],
        capture_addr=capture_addr,
        subject="Delivery Status Notification (Failure)",
        text="550 5.1.1 user unknown: jane@bank.example",
    )

    result = services.ingest_inbound_email(payload)

    assert result.status == "applied"
    assert result.detail.get("bounced") is True
    assert Touch.objects.for_user(user).count() == 0
    ev = CaptureEvent.objects.for_user(user).first()
    assert ev.signals.get("bounced") is True


# --------------------------------------------------------------------------- #
# Stage ratchet: reply -> scheduled -> chat advances correctly
# --------------------------------------------------------------------------- #

def test_reply_then_scheduled_then_chat_advances_stage(
    user, known_contact, capture_addr, make_payload, ics_builder, future_dt, past_dt
):
    """The existing-system lesson (existing-system.md §1): a thread legitimately
    progresses reply -> scheduled -> chat; each captured email advances the
    ratchet without a flat dedup swallowing a real chat."""
    reply = make_payload(
        from_email=known_contact.email, to=[(USER_EMAIL, "")], capture_addr=capture_addr,
        message_id="<seq-1@mail.example>", subject="Re: intro", text="Happy to help!",
    )
    scheduled = make_payload(
        from_email=known_contact.email, to=[(USER_EMAIL, "")], capture_addr=capture_addr,
        message_id="<seq-2@mail.example>", subject="Invite", ics_text=ics_builder(future_dt),
    )
    chatted = make_payload(
        from_email=known_contact.email, to=[(USER_EMAIL, "")], capture_addr=capture_addr,
        message_id="<seq-3@mail.example>", subject="Recap", ics_text=ics_builder(past_dt),
    )

    services.ingest_inbound_email(reply)
    known_contact.refresh_from_db()
    assert (known_contact.warmth, known_contact.thread_state) == ("replied", "replied")

    services.ingest_inbound_email(scheduled)
    known_contact.refresh_from_db()
    assert known_contact.thread_state == "chat_scheduled"

    services.ingest_inbound_email(chatted)
    known_contact.refresh_from_db()
    assert (known_contact.warmth, known_contact.thread_state) == ("chatted", "chat_done")

    # Three distinct captured events, three touches.
    assert CaptureEvent.objects.for_user(user).count() == 3
    assert Touch.objects.for_user(user).filter(contact=known_contact).count() == 3


# --------------------------------------------------------------------------- #
# Resolver: unknown counterparty -> pending contact
# --------------------------------------------------------------------------- #

def test_unknown_counterparty_creates_pending_contact(user, capture_addr, make_payload):
    payload = make_payload(
        from_email="newperson@firm.example",
        from_name="New Person",
        to=[(USER_EMAIL, "")],
        capture_addr=capture_addr,
        text="Hi, thanks for connecting.",
    )

    services.ingest_inbound_email(payload)

    contact = Contact.objects.for_user(user).get(email="newperson@firm.example")
    assert contact.source == services.PENDING_SOURCE
    assert contact.name == "New Person"


# --------------------------------------------------------------------------- #
# needs_review confirmation flow
# --------------------------------------------------------------------------- #

def test_confirm_event_applies_touch(user, known_contact, capture_addr, make_payload):
    payload = make_payload(
        from_email=known_contact.email, to=[(USER_EMAIL, "")], capture_addr=capture_addr,
        subject="Automatic reply", headers=[("Auto-Submitted", "auto-replied")],
    )
    services.ingest_inbound_email(payload)
    ev = services.needs_review_events(user).first()

    services.confirm_event(user, ev.id, "reply_received")

    ev.refresh_from_db()
    assert ev.status == "applied"
    known_contact.refresh_from_db()
    assert known_contact.warmth == "replied"
