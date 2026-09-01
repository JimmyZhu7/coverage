"""The chat-claim audit reports; it never decides.

`chat_done` sets warmth to `chatted`, and `capture_worklist` drops a
`chatted` contact from every later re-check -- so a wrong one is
unrecoverable. Three capture paths could once write that state off a
language judgement; all are gated now, and this command exists for the rows
written before the gate.

The tempting rule ("no CalendarEvent means no chat") is measurably wrong.
On the founder's real board, `[CITED]` holds two claims that are NOT chats
(a reply, and an intro email) while `[UNCITED]` holds one that is (a note
reading "replied twice and confirmed a call"). Evidence tier predicts
nothing on its own, which is the whole argument for a human reading it.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from crm.models import CalendarEvent, Contact, Touch

User = get_user_model()

# `set_contact_state` writes through `coverage_domain.pipeline`, which uses a
# RAW psycopg cursor rather than the ORM connection -- so it cannot see rows
# a wrapped test transaction has not committed. Same marker the other suites
# that exercise that path already use (see test_appmail, test_autopilot_*).
pytestmark = pytest.mark.django_db(transaction=True)


def _user():
    return User.objects.create_user(email="chataudit@example.com", password="pw12345!")


def _chatted(user, name, *, note, with_event=False, kind="chat"):
    c = Contact.all_objects.create(
        user=user, name=name, warmth="chatted", thread_state="chat_done",
    )
    Touch.all_objects.create(
        user=user, contact=c, kind=kind, channel="email", note=note,
        ts=timezone.now(),
    )
    if with_event:
        CalendarEvent.all_objects.create(
            user=user, contact=c, title=f"{name} chat", starts_at=timezone.now(),
        )
    return c


def test_it_grades_by_evidence_and_writes_nothing_by_default():
    user = _user()
    booked = _chatted(user, "Booked", note="[gmail:abc] confirmed 12:30", with_event=True)
    cited = _chatted(user, "Cited", note="[gmail:def] Wrote to you from a firm address")
    bare = _chatted(user, "Bare", note="Discovered by mailbox scan")

    out = StringIO()
    call_command("audit_chat_claims", email=user.email, stdout=out)
    body = out.getvalue()

    assert f"[CALENDAR ] #{booked.id}" in body
    assert f"[CITED    ] #{cited.id}" in body
    assert f"[UNCITED  ] #{bare.id}" in body
    # Report-only: every contact still stands exactly where it did.
    for c in (booked, cited, bare):
        c.refresh_from_db()
        assert (c.warmth, c.thread_state) == ("chatted", "chat_done")


def test_a_revert_needs_commit_and_then_writes_an_audit_touch():
    user = _user()
    bare = _chatted(user, "Bare", note="Discovered by mailbox scan")

    dry = StringIO()
    call_command("audit_chat_claims", email=user.email, revert=bare.id, stdout=dry)
    assert "Nothing written" in dry.getvalue()
    bare.refresh_from_db()
    assert bare.warmth == "chatted"

    live = StringIO()
    call_command(
        "audit_chat_claims", email=user.email, revert=bare.id, commit=True, stdout=live
    )
    bare.refresh_from_db()
    assert (bare.warmth, bare.thread_state) == ("replied", "replied")
    # Through `set_contact_state`, so the History says why they cooled.
    assert Touch.all_objects.filter(contact=bare, kind="manual_override").exists()


def test_reverting_a_calendar_backed_claim_warns_before_it_writes():
    """The one case the tiering exists to protect. A booked chat is a real
    fact; the command still allows it, because the human may know the
    invite was never honoured, but it does not let it pass silently."""
    user = _user()
    booked = _chatted(user, "Booked", note="[gmail:abc] confirmed", with_event=True)

    out = StringIO()
    call_command("audit_chat_claims", email=user.email, revert=booked.id, stdout=out)
    assert "calendar event" in out.getvalue()
    assert "probably wrong" in out.getvalue()


def test_it_refuses_a_contact_that_is_not_at_a_chat_state():
    user = _user()
    cold = Contact.all_objects.create(user=user, name="Cold", warmth="cold")
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("audit_chat_claims", email=user.email, revert=cold.id, commit=True)
