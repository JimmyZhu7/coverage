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


def test_captured_touch_has_source_capture_and_a_linked_capture_event(
    user, known_contact, capture_addr, make_payload
):
    """Touch.SOURCE_CHOICES's 'capture' value and the Touch.capture_event FK
    both existed but nothing ever wrote them -- apply_touch hardcoded
    'manual' into every INSERT regardless of caller, which made "captured
    vs typed touches" (the product's own risk metric) unanswerable from the
    DB."""
    payload = make_payload(
        from_email=known_contact.email,
        to=[(USER_EMAIL, "")],
        capture_addr=capture_addr,
        subject="Re: intro",
        text="Great to hear from you — happy to chat.",
    )

    services.ingest_inbound_email(payload)

    ev = CaptureEvent.objects.for_user(user).first()
    touch = Touch.objects.for_user(user).get(contact=known_contact)
    assert touch.source == "capture"
    assert touch.capture_event_id == ev.id


def test_captured_touch_is_timestamped_when_the_email_happened_not_ingested(
    user, known_contact, capture_addr, make_payload
):
    """CaptureEvent.occurred_at is parsed from the Date header and stored,
    but nothing read it back -- every touch was stamped at ingest time. A
    student forwarding last week's reply must ratchet with THAT date, or the
    cadence's business-day math and the fit score's recency axis both read
    the wrong age for it."""
    from datetime import timedelta
    from email.utils import format_datetime

    from django.utils import timezone

    older = timezone.now() - timedelta(days=9)
    payload = make_payload(
        from_email=known_contact.email,
        to=[(USER_EMAIL, "")],
        capture_addr=capture_addr,
        subject="Re: intro",
        text="Sorry for the late reply, happy to help.",
        date=format_datetime(older),
    )

    services.ingest_inbound_email(payload)

    touch = Touch.objects.for_user(user).get(contact=known_contact)
    assert abs((touch.ts - older).total_seconds()) < 2


def test_a_forged_future_date_header_is_clamped_to_now(
    user, known_contact, capture_addr, make_payload
):
    """A forged/garbled Date header must never produce a touch stamped in
    the future -- that would sort ahead of "now" everywhere Touch.ts is
    read (the activity feed, the cadence engine's "days since" math)."""
    from datetime import timedelta
    from email.utils import format_datetime

    from django.utils import timezone

    before = timezone.now()
    future = before + timedelta(days=30)
    payload = make_payload(
        from_email=known_contact.email,
        to=[(USER_EMAIL, "")],
        capture_addr=capture_addr,
        subject="Re: intro",
        text="Happy to help.",
        date=format_datetime(future),
    )

    services.ingest_inbound_email(payload)

    touch = Touch.objects.for_user(user).get(contact=known_contact)
    assert touch.ts <= timezone.now()
    assert touch.ts < future


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


def test_bounce_records_which_address_actually_failed(user, capture_addr, make_payload):
    """Before this fix, a bounce's counterparty was read off the sender —
    mailer-daemon — and the actually-failed address (available via
    X-Failed-Recipients) was thrown away entirely. The CaptureEvent row must
    now carry it, even though the touch/contact side is unchanged (still no
    touch, still no contact created for it)."""
    payload = make_payload(
        from_email="mailer-daemon@bank.example",
        to=[(USER_EMAIL, "")],
        capture_addr=capture_addr,
        subject="Delivery Status Notification (Failure)",
        text="550 5.1.1 user unknown: jane@bank.example",
        headers=[("X-Failed-Recipients", "jane@bank.example")],
    )

    result = services.ingest_inbound_email(payload)

    assert result.status == "applied"
    ev = CaptureEvent.objects.for_user(user).first()
    assert ev.signals.get("bounced") is True
    assert ev.signals.get("failed_recipient") == "jane@bank.example"
    assert ev.counterparty_email == "jane@bank.example"
    assert Touch.objects.for_user(user).count() == 0, "still no touch -- that needs a decision"


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


def test_ambiguous_name_match_routes_to_needs_review_not_a_touch(
    user, capture_addr, make_payload
):
    """Two contacts share a normalized name (e.g. two "Michael Chen"s at
    different firms) and the inbound address matches neither of their
    emails, so resolution falls back to name matching. Before this fix,
    resolve_contact silently returned whichever row the queryset happened
    to yield first -- the touch landed on a coin flip. Now it must refuse
    and park the event for a human instead."""
    chen_a = Contact.all_objects.create(
        user=user, name="Michael Chen", email="mchen@firm-a.example", source="manual",
    )
    chen_b = Contact.all_objects.create(
        user=user, name="Michael Chen", email="mchen@firm-b.example", source="manual",
    )
    payload = make_payload(
        from_email="michael.chen@personal.example",
        from_name="Michael Chen",
        to=[(USER_EMAIL, "")],
        capture_addr=capture_addr,
        text="Great to connect!",
    )

    result = services.ingest_inbound_email(payload)

    assert result.status == "needs_review"
    ev = CaptureEvent.objects.for_user(user).first()
    assert ev.status == "needs_review"
    assert Touch.objects.for_user(user).count() == 0
    # Neither homonym was touched, and no third (pending) contact was
    # invented for the ambiguous match.
    chen_a.refresh_from_db()
    chen_b.refresh_from_db()
    assert chen_a.warmth == "cold" and chen_b.warmth == "cold"
    assert Contact.objects.for_user(user).count() == 2


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


# --------------------------------------------------------------------------- #
# Forwards, and the guard that stops junk contacts
# --------------------------------------------------------------------------- #

def _forward_payload(capture_addr, make_payload, *, subject="Fwd: Re: coffee"):
    """A student forwarding a reply they RECEIVED to their capture address.
    The envelope From is the student and there is no other recipient — the
    exact shape that used to manufacture a placeholder contact."""
    return make_payload(
        from_email=USER_EMAIL, to=[(capture_addr, "")], capture_addr=capture_addr,
        subject=subject,
        text=(
            "Look at this one.\n\n"
            "---------- Forwarded message ----------\n"
            "From: Jane Banker <jane@bank.example>\n"
            "Happy to help — send me your CV.\n"
        ),
    )


def test_a_forward_needs_review_and_creates_no_contact(
    user, capture_addr, make_payload
):
    """The whole failure, end to end: direction read `outbound`, no
    counterparty was found, and the resolver created a contact literally named
    "Unknown contact" plus an `outreach` touch — recording that the student
    SENT mail they had in fact RECEIVED."""
    result = services.ingest_inbound_email(_forward_payload(capture_addr, make_payload))

    assert result.status == "needs_review"
    assert result.detail["reason"] == "forward_unparsed"
    assert Contact.objects.for_user(user).count() == 0
    assert Touch.objects.for_user(user).count() == 0
    assert not Contact.all_objects.filter(name="Unknown contact").exists()

    ev = CaptureEvent.objects.for_user(user).get()
    assert ev.status == "needs_review"


def test_n_forwards_do_not_make_n_junk_contacts(user, capture_addr, make_payload):
    """The compounding version — the reason this showed up as clutter rather
    than as one odd row."""
    for i in range(3):
        payload = _forward_payload(capture_addr, make_payload)
        payload["Headers"][0]["Value"] = f"<fwd-{i}@mail.example>"
        services.ingest_inbound_email(payload)

    assert CaptureEvent.objects.for_user(user).count() == 3
    assert Contact.objects.for_user(user).count() == 0


def test_resolve_contact_refuses_to_invent_a_nameless_contact(user):
    """The guard itself, unit-tested — this is what actually stops the junk.
    Forward DETECTION narrows how often we get here; this makes the outcome
    impossible regardless of which path arrives with an empty pair."""
    with pytest.raises(services.UnidentifiableContactError):
        services.resolve_contact(user, "", "")
    with pytest.raises(services.UnidentifiableContactError):
        services.resolve_contact(user, "   ", "  ")
    assert Contact.all_objects.filter(user=user).count() == 0


def test_resolve_contact_still_creates_from_a_name_or_an_email_alone(user):
    """The guard must not narrow the legitimate auto-create path: either
    identifier on its own is enough to make a pending contact worth
    confirming."""
    by_name, created = services.resolve_contact(user, "", "Jane Banker")
    assert created and by_name.name == "Jane Banker"
    by_email, created = services.resolve_contact(user, "sam@bank.example", "")
    assert created and by_email.email == "sam@bank.example"
    assert by_email.name == "sam@bank.example"


def test_an_unidentifiable_event_parks_instead_of_creating_a_placeholder(
    user, capture_addr, make_payload
):
    """The pipeline's side of the guard: a classified, actionable event whose
    counterparty is blank must land in the review queue, not invent a row.
    Reached here by confirming an event that names nobody."""
    services.ingest_inbound_email(_forward_payload(capture_addr, make_payload))
    ev = services.needs_review_events(user).first()

    with pytest.raises(services.UnidentifiableContactError):
        services.confirm_event(user, ev.id, "reply_received")

    ev.refresh_from_db()
    assert ev.status == "needs_review", "and it stays in the queue"
    assert Contact.objects.for_user(user).count() == 0


def test_a_reply_to_a_forwarded_thread_is_reviewable_not_lost(
    user, known_contact, capture_addr, make_payload
):
    """An INBOUND `Re: Fwd:` reply is applied directly, not sent to review.

    Forward detection is scoped to outbound messages, because that is the only
    direction where the envelope lies. Here the banker is the sender, so From
    IS the counterparty and there is nothing to disambiguate — a "Re: Fwd:"
    chain is just what a reply looks like after someone forwarded an intro.
    Routing these to review taxed the commonest good case in the product to
    guard a bad one that is already covered: a forward the student sends with
    the prefix edited off is still outbound with no non-self recipient, so
    `_counterparty` comes back empty and `resolve_contact`'s name-and-email
    guard refuses to invent a contact for it (see
    `test_forwarded_reply_creates_no_contact`).
    """
    payload = make_payload(
        from_email=known_contact.email, from_name="Jane Banker",
        to=[(USER_EMAIL, "")], capture_addr=capture_addr,
        subject="Re: Fwd: intro to Jane",
        text="Happy to help — how's Thursday?",
    )

    result = services.ingest_inbound_email(payload)
    assert result.status == "applied", "a readable inbound reply needs no click"

    ev = CaptureEvent.objects.for_user(user).get()
    assert ev.counterparty_email == known_contact.email, "the person is known"
    assert ev.status == "applied"

    known_contact.refresh_from_db()
    assert known_contact.warmth == "replied"
    assert Contact.objects.for_user(user).count() == 1, "and no second contact"
