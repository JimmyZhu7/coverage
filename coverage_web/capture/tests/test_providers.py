"""Provider-level tests for `InboundEmailProvider.build_event` that don't fit
`test_extractors.py` (which is about the deterministic classifier, not the
event assembly built on top of it): the synthetic dedup key and the bounce
counterparty rewiring.

These construct `ParsedInbound` directly rather than going through
`make_payload` + `.parse()` — the shared fixture always stamps a Postmark
envelope `MessageID`, so `parsed.message_id` is never actually empty via that
path and the synthetic-ref branch would never be exercised."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from capture.providers import InboundEmailProvider, ParsedInbound

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="student@example.com", password="x", capture_slug="synthslug1"
    )


def _parsed(**over) -> ParsedInbound:
    base: dict = dict(
        from_email="student@example.com",
        from_name="Student",
        recipients=[],
        subject="Cold outreach",
        message_id="",  # forces the synthetic-ref path (no Message-ID)
        text_body="Hi, following up.",
        occurred_at=None,
        headers={},
        attachments=[],
    )
    base.update(over)
    return ParsedInbound(**base)


def test_synthetic_ref_differs_by_recipient(user):
    """A batch of templated cold emails sent in the same second (same
    sender, same subject, same date, no Message-ID) used to collapse to ONE
    dedup key because the recipient wasn't part of the hash basis — every
    email after the first came back "duplicate" with no contact resolved
    and no touch logged."""
    provider = InboundEmailProvider()
    to_alex = _parsed(recipients=[("Alex", "alex@firm.example")])
    to_bo = _parsed(recipients=[("Bo", "bo@firm.example")])

    ref_alex = provider.build_event(to_alex, user).provider_ref
    ref_bo = provider.build_event(to_bo, user).provider_ref

    assert ref_alex.startswith("synth-") and ref_bo.startswith("synth-")
    assert ref_alex != ref_bo


def test_synthetic_ref_is_stable_for_a_genuine_redelivery(user):
    """The fix must not break the dedup property it's protecting: the exact
    same header-less message delivered twice still hashes to the same key."""
    provider = InboundEmailProvider()
    parsed = _parsed(recipients=[("Alex", "alex@firm.example")])

    first = provider.build_event(parsed, user).provider_ref
    second = provider.build_event(parsed, user).provider_ref

    assert first == second


def test_bounce_event_counterparty_is_the_failed_address_not_mailer_daemon(user):
    """build_event's bounce branch must read the failed recipient out of the
    classifier's signals rather than falling through to `_counterparty`,
    which would otherwise happily return mailer-daemon (it isn't the user
    or the capture address, so the ordinary counterparty rule accepts it)."""
    provider = InboundEmailProvider()
    parsed = _parsed(
        from_email="mailer-daemon@bank.example",
        from_name="Mail Delivery Subsystem",
        subject="Delivery Status Notification (Failure)",
        text_body="550 5.1.1 user unknown",
        headers={"x-failed-recipients": "jane@bank.example"},
    )

    event = provider.build_event(parsed, user)

    assert event.signals.get("bounced") is True
    assert event.counterparty_email == "jane@bank.example"
    assert event.counterparty_email != "mailer-daemon@bank.example"
