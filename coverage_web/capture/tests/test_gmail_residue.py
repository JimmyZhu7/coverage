"""Phase 3: the capped Haiku residue classifier (`capture/gmail_residue.py`).

Modeled on `directory/tests/test_ai_extract.py`'s own approach: most of
these tests exist to prove an ungrounded/malformed model answer is
rejected, not to prove a well-formed one is accepted, plus the one thing
unique to this module — the exact 100-thread cap.

Mocks `_post_json` directly (never a real network call — the suite-wide
`_no_live_anthropic_calls` autouse fixture in the root conftest also blanks
`ANTHROPIC_API_KEY` for every test unless a test explicitly overrides it,
same discipline as `directory.ai_extract`'s own tests).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from capture import gmail_residue
from capture.gmail_residue import MAX_RESIDUE_THREADS, is_configured, run_residue_stage
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def student(db):
    return User.objects.create_user(email="residue-student@example.com", password="x")


class FakeConnection:
    """A stand-in for `GmailConnection` — `run_residue_stage` only reads
    `.user` and `.gmail_address` off its `connection` argument."""

    def __init__(self, user, gmail_address="me@example.com"):
        self.user = user
        self.gmail_address = gmail_address


def _api_text_response(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _residue_message(*, thread_id: str, from_addr: str, subject: str, snippet: str):
    return {
        "message": {
            "threadId": thread_id,
            "snippet": snippet,
            "payload": {
                "headers": [
                    {"name": "From", "value": from_addr},
                    {"name": "Subject", "value": subject},
                ],
            },
        },
        "thread_id": thread_id,
    }


# ---------------------------------------------------------------------------
# is_configured() gate
# ---------------------------------------------------------------------------
@override_settings(ANTHROPIC_API_KEY="")
def test_is_configured_false_by_default():
    assert is_configured() is False


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_is_configured_true_when_key_set():
    assert is_configured() is True


@override_settings(ANTHROPIC_API_KEY="")
def test_run_residue_stage_makes_no_api_call_when_unconfigured(student):
    residue = [
        _residue_message(
            thread_id="t1", from_addr="a@x.com", subject="hi", snippet="hello there"
        )
    ]
    called = []
    with patch.object(gmail_residue, "_post_json", side_effect=lambda *a, **kw: called.append(1)):
        stats = run_residue_stage(FakeConnection(student), residue)
    assert called == []
    assert stats["residue_threads_processed"] == 0
    assert stats["residue_threads_seen"] == 1


# ---------------------------------------------------------------------------
# THE grounding rule: a fabricated/non-matching quote must never be trusted
# ---------------------------------------------------------------------------
@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_a_genuine_reply_with_an_ungrounded_quote_is_downgraded_to_ambiguous(student):
    """Core defense: the model claims `genuine_reply` and sounds confident,
    but its `quote` does not actually appear anywhere in the message text
    it was given. That claim must not be trusted -- no touch is logged."""
    Contact.all_objects.create(
        user=student, name="Jane Banker", email="jane@bank.example", source="manual"
    )
    residue = [
        _residue_message(
            thread_id="t-ungrounded",
            from_addr="Jane Banker <jane@bank.example>",
            subject="Re: chat",
            snippet="Sure, works for me.",
        )
    ]
    reply = _api_text_response({
        "outcome": "genuine_reply",
        "quote": "I would absolutely love to grab coffee next Tuesday!",  # fabricated
    })
    with patch.object(gmail_residue, "_post_json", return_value=reply):
        stats = run_residue_stage(FakeConnection(student), residue)

    assert stats["residue_threads_processed"] == 1
    assert stats["genuine_reply"] == 0
    assert stats["ambiguous"] == 1
    assert stats["touches_logged"] == 0
    assert not Touch.objects.for_user(student).exists()


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_a_genuine_reply_with_a_grounded_quote_is_trusted_and_logs_a_touch(student):
    Contact.all_objects.create(
        user=student, name="Jane Banker", email="jane@bank.example", source="manual"
    )
    residue = [
        _residue_message(
            thread_id="t-grounded",
            from_addr="Jane Banker <jane@bank.example>",
            subject="Re: chat",
            snippet="Sure, works for me next week!",
        )
    ]
    reply = _api_text_response({
        "outcome": "genuine_reply",
        "quote": "Sure, works for me next week!",  # a real substring of the snippet
    })
    with patch.object(gmail_residue, "_post_json", return_value=reply):
        stats = run_residue_stage(FakeConnection(student), residue)

    assert stats["genuine_reply"] == 1
    assert stats["touches_logged"] == 1
    assert Touch.objects.for_user(student).filter(kind="reply_received").exists()


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_an_auto_reply_outcome_never_logs_a_touch_even_when_grounded(student):
    Contact.all_objects.create(
        user=student, name="Jane Banker", email="jane@bank.example", source="manual"
    )
    residue = [
        _residue_message(
            thread_id="t-ooo",
            from_addr="Jane Banker <jane@bank.example>",
            subject="Automatic reply: Out of office",
            snippet="I am currently out of office and will respond when I return.",
        )
    ]
    reply = _api_text_response({
        "outcome": "auto_reply",
        "quote": "I am currently out of office and will respond when I return.",
    })
    with patch.object(gmail_residue, "_post_json", return_value=reply):
        stats = run_residue_stage(FakeConnection(student), residue)

    assert stats["auto_reply"] == 1
    assert stats["touches_logged"] == 0
    assert not Touch.objects.for_user(student).exists()


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_malformed_json_is_treated_as_ambiguous_not_a_crash(student):
    residue = [_residue_message(thread_id="t-bad", from_addr="a@x.com", subject="hi", snippet="hello")]
    reply = {"content": [{"type": "text", "text": "not json at all"}]}
    with patch.object(gmail_residue, "_post_json", return_value=reply):
        stats = run_residue_stage(FakeConnection(student), residue)
    assert stats["ambiguous"] == 1


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_an_outcome_outside_the_closed_set_is_rejected(student):
    """The model must answer one of exactly three fixed outcomes -- an
    open-ended or invented label (even a plausible-sounding one) is
    rejected rather than trusted verbatim."""
    residue = [_residue_message(thread_id="t-weird", from_addr="a@x.com", subject="hi", snippet="hello there")]
    reply = _api_text_response({"outcome": "definitely_interested", "quote": "hello there"})
    with patch.object(gmail_residue, "_post_json", return_value=reply):
        stats = run_residue_stage(FakeConnection(student), residue)
    assert stats["ambiguous"] == 1
    assert stats["genuine_reply"] == 0


# ---------------------------------------------------------------------------
# THE 100-thread cap
# ---------------------------------------------------------------------------
@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_exactly_100_residue_threads_are_processed_and_the_101st_is_not(student):
    assert MAX_RESIDUE_THREADS == 100

    residue = [
        _residue_message(
            thread_id=f"thread-{i}", from_addr=f"person{i}@x.com",
            subject="hi", snippet=f"message body number {i}",
        )
        for i in range(101)
    ]

    seen_texts: list[str] = []

    def _fake_post_json(payload, *, timeout, retries):
        prompt = payload["messages"][0]["content"]
        seen_texts.append(prompt)
        return _api_text_response({"outcome": "ambiguous", "quote": None})

    with patch.object(gmail_residue, "_post_json", side_effect=_fake_post_json):
        stats = run_residue_stage(FakeConnection(student), residue)

    assert stats["residue_threads_seen"] == 101
    assert stats["residue_threads_processed"] == 100
    assert len(seen_texts) == 100
    # The 101st candidate's own message text must never have reached the model.
    assert not any("message body number 100" in text for text in seen_texts)
    # But every one of the first 100 did.
    assert all(
        any(f"message body number {i}" in text for text in seen_texts) for i in range(100)
    )


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_the_cap_applies_per_distinct_thread_not_per_raw_message(student):
    """Two residue messages sharing one thread_id must count and dedupe as
    ONE thread -- the cap's own unit, per the module docstring."""
    residue = [
        _residue_message(thread_id="dup-thread", from_addr="a@x.com", subject="hi", snippet="first"),
        _residue_message(thread_id="dup-thread", from_addr="a@x.com", subject="hi", snippet="second"),
    ]
    reply = _api_text_response({"outcome": "ambiguous", "quote": None})
    with patch.object(gmail_residue, "_post_json", return_value=reply) as mocked:
        stats = run_residue_stage(FakeConnection(student), residue)
    assert stats["residue_threads_seen"] == 1
    assert stats["residue_threads_processed"] == 1
    assert mocked.call_count == 1


def test_run_residue_stage_is_a_noop_with_empty_residue(student):
    stats = run_residue_stage(FakeConnection(student), [])
    assert stats["residue_threads_seen"] == 0
    assert stats["residue_threads_processed"] == 0


# ---------------------------------------------------------------------------
# max_threads — the credit-metering clamp
# (docs/credit-system-plan.md's enforcement point 2, capture/gmail_live.py)
# ---------------------------------------------------------------------------
@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_max_threads_clamps_below_the_hard_cap_when_it_is_the_smaller_limit(student):
    """A student whose credit balance can only afford 7 threads must not
    have the classifier reach for the 8th, even though the hard
    MAX_RESIDUE_THREADS cap would allow up to 100."""
    residue = [
        _residue_message(thread_id=f"thread-{i}", from_addr=f"p{i}@x.com", subject="hi", snippet=f"body {i}")
        for i in range(10)
    ]
    reply = _api_text_response({"outcome": "ambiguous", "quote": None})
    with patch.object(gmail_residue, "_post_json", return_value=reply) as mocked:
        stats = run_residue_stage(FakeConnection(student), residue, max_threads=7)

    assert stats["residue_threads_seen"] == 10
    assert stats["residue_threads_processed"] == 7
    assert mocked.call_count == 7


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_max_threads_larger_than_the_hard_cap_never_exceeds_max_residue_threads(student):
    """The two caps are independent and the SMALLER one wins — a generous
    credit balance must never let a rescan exceed the 100-thread ceiling."""
    residue = [
        _residue_message(thread_id=f"thread-{i}", from_addr=f"p{i}@x.com", subject="hi", snippet=f"body {i}")
        for i in range(101)
    ]
    reply = _api_text_response({"outcome": "ambiguous", "quote": None})
    with patch.object(gmail_residue, "_post_json", return_value=reply):
        stats = run_residue_stage(FakeConnection(student), residue, max_threads=1000)

    assert stats["residue_threads_processed"] == MAX_RESIDUE_THREADS


@override_settings(ANTHROPIC_API_KEY="sk-test-key")
def test_max_threads_of_zero_makes_no_api_call_at_all(student):
    """A student with no affordable credits: the residue stage runs (it is
    not skipped entirely — `residue_threads_seen` still reports what was
    THERE), it just classifies nothing."""
    residue = [_residue_message(thread_id="t1", from_addr="a@x.com", subject="hi", snippet="body")]

    with patch.object(gmail_residue, "_post_json") as mocked:
        stats = run_residue_stage(FakeConnection(student), residue, max_threads=0)

    assert stats["residue_threads_seen"] == 1
    assert stats["residue_threads_processed"] == 0
    mocked.assert_not_called()
