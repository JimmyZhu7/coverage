"""Regression: a NAIVE `occurred_at` on a finding must not crash the sync.

Django 5 removed the `django.utils.timezone.utc` alias, and three capture
helpers still spelled the UTC anchor that way on their naive-timestamp
branch. The live Gmail path never hit it (Gmail's `internalDate` converts
to an aware ISO string), which is why the bug survived — but any finding
supplied with a naive `occurred_at` (an agent-run findings file, a hand
built batch) raised AttributeError mid-`apply_findings`. On the
`gmail_poll --interval` loop that is the worst shape a crash can take: the
exception fires before the history cursor is saved, so the same messages
are re-fetched and re-crashed every interval forever.

These tests pin the contract: a naive timestamp is anchored to UTC and
comes back aware, on all three parsers, and a whole `apply_findings` batch
carrying one completes.
"""

from __future__ import annotations

from datetime import timezone as dt_timezone

import pytest
from django.contrib.auth import get_user_model

from capture.appmail import _occurred_at as appmail_occurred_at
from capture.discovery import _parse_occurred_at as discovery_occurred_at
from capture.gmail import _finding_occurred_at as gmail_occurred_at, apply_findings
from crm.models import Contact, Touch

User = get_user_model()

NAIVE = "2026-08-01T10:00:00"  # no offset on purpose — the crashing shape


@pytest.mark.parametrize(
    "parser",
    [gmail_occurred_at, discovery_occurred_at, appmail_occurred_at],
    ids=["gmail", "discovery", "appmail"],
)
def test_naive_occurred_at_is_anchored_to_utc_not_a_crash(parser):
    when = parser({"occurred_at": NAIVE})
    assert when is not None
    assert when.tzinfo is not None
    assert when.utcoffset() == dt_timezone.utc.utcoffset(None)
    assert (when.year, when.month, when.day, when.hour) == (2026, 8, 1, 10)


@pytest.mark.django_db(transaction=True)
def test_apply_findings_survives_a_naive_timestamp():
    """End to end: one reply finding with a naive `occurred_at` logs its
    touch instead of killing the batch."""
    user = User.objects.create_user(email="naive-ts@example.com", password="x")
    contact = Contact.all_objects.create(
        user=user, name="Jane Banker", email="jane@bank.example", source="manual"
    )
    result = apply_findings(user, [{
        "name": "Jane Banker",
        "email": "jane@bank.example",
        "found": True,
        "bounced": False,
        "outreach_sent": False,
        "replied": True,
        "chat_status": "none",
        "evidence": "replied to your note",
        "thread_id": "t-naive",
        "occurred_at": NAIVE,
    }])
    assert result.touches_logged == 1
    touch = Touch.objects.for_user(user).get(contact=contact, kind="reply_received")
    assert touch.ts.tzinfo is not None
