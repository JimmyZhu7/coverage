"""Integration tests for crm.services — the Django -> coverage_domain
adapter. These are the tests that matter most: they prove the ported
warmth/thread-state machine (coverage_domain.pipeline, verified by its
own 40-test suite against test-only DDL) also works correctly against
the REAL Django-migrated `contacts`/`touches` schema.

`transaction=True` is required, not optional: crm.services opens a
dedicated, separate physical psycopg connection (see services.py's
module docstring for why it must be separate from Django's own). pytest-
django's default `db` fixture wraps each test in an outer transaction
that's rolled back afterward and never committed — a second, independent
connection could never see that transaction's uncommitted writes.
`django_db(transaction=True)` switches to commit-then-truncate, so rows
created via the Django ORM in this test are actually visible to the
dedicated connection crm.services opens.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from coverage_domain.pipeline import ContactNotFound
from crm import services
from crm.models import Contact, Touch

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def user():
    return User.objects.create_user(email="student@example.com", password="x")


@pytest.fixture
def contact(user):
    return Contact.all_objects.create(user=user, name="Jane Banker")


def test_log_touch_reply_received_moves_warmth_cold_to_replied(user, contact):
    """The headline proof requested in the task brief: create a User and
    a Contact via the Django ORM, call the adapter with
    kind='reply_received', and see warmth ratchet cold -> replied on the
    real, migrated schema."""
    assert contact.warmth == "cold"
    assert contact.thread_state == "no_reply"

    updates = services.log_touch(user.id, contact.id, "reply_received", "email")

    assert updates == {"warmth": "replied", "thread_state": "replied"}

    contact.refresh_from_db()
    assert contact.warmth == "replied"
    assert contact.thread_state == "replied"

    touch = Touch.all_objects.get(user=user, contact=contact)
    assert touch.kind == "reply_received"
    assert touch.channel == "email"
    assert touch.source == "manual"


def test_log_touch_is_scoped_to_the_given_tenant(user, contact):
    """A contact_id that exists but belongs to a different user_id must be
    indistinguishable from one that doesn't exist (pipeline.py's
    ContactNotFound docstring) — proves tenancy is enforced end to end
    through the adapter, not just inside pipeline.py's own tests."""
    other = User.objects.create_user(email="other-student@example.com", password="x")

    with pytest.raises(ContactNotFound):
        services.log_touch(other.id, contact.id, "reply_received", "email")


def test_terminal_advocate_guard_blocks_further_ratchet_via_apply_touch(user, contact):
    """Once thread_state is the terminal 'advocate', a normal touch must
    not move it — only set_contact_state() (the manual override) can."""
    services.set_contact_state(user.id, contact.id, warmth="advocate")
    contact.refresh_from_db()
    assert contact.warmth == "advocate"
    assert contact.thread_state == "advocate"

    updates = services.log_touch(user.id, contact.id, "chat_scheduled", "email")

    contact.refresh_from_db()
    assert contact.thread_state == "advocate"
    assert updates == {}


def test_manual_override_writes_an_audit_touch(user, contact):
    """set_contact_state() is the one path that can move warmth down or
    thread_state out of 'advocate' — and, per the one intentional addition
    in this port, must also leave an audit touch so the log has no gap."""
    updates = services.set_contact_state(
        user.id,
        contact.id,
        warmth="chatted",
        thread_state="chat_done",
        note="caught up over coffee",
    )

    assert updates == {"warmth": "chatted", "thread_state": "chat_done"}

    contact.refresh_from_db()
    assert contact.warmth == "chatted"
    assert contact.thread_state == "chat_done"

    audit = Touch.all_objects.get(user=user, contact=contact, kind="manual_override")
    assert "warmth=chatted" in audit.note
    assert "thread_state=chat_done" in audit.note
    assert "caught up over coffee" in audit.note
    assert audit.channel is None
