"""The board must agree with itself about who you have contacted.

Guards the class of bug behind "why do a lot of cards say send the first
note?" — contacts the mailbox scan created sitting at zero touches, so the
cadence engine reported them as never-contacted while their own notes said
outreach had been sent. The specific cause is fixed; these pin the invariant
so the next writer to create contacts cannot reintroduce the shape quietly.
"""

from __future__ import annotations

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from crm import health
from crm.models import Contact, Touch

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        email="board@example.com", password="x", capture_slug="boardslug1"
    )


def _touch(user, contact, kind="outreach"):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel="email", ts=timezone.now()
    )


# ---------------------------------------------------------------------------
# The contradiction
# ---------------------------------------------------------------------------
def test_a_discovered_contact_with_no_touch_is_a_contradiction(user):
    """You cannot discover a stranger. The scan only creates people it found
    in your mail, so zero touches means evidence was dropped on the way to
    the ledger — not that the relationship is cold."""
    Contact.all_objects.create(user=user, name="Jason Law", source="capture",
                               notes="Follow-up outreach sent, no reply yet")
    assert [c.name for c in health.discovered_but_untouched(user)] == ["Jason Law"]
    assert "never-contacted" in "\n".join(health.health_report(user))


def test_recording_the_outreach_clears_it(user):
    """The fix, from the checker's side."""
    c = Contact.all_objects.create(user=user, name="Jason Law", source="capture")
    _touch(user, c, "outreach")
    assert health.discovered_but_untouched(user) == []


def test_a_hand_added_contact_with_no_touches_is_not_flagged(user):
    """The common, honest case: you added someone you have not written to.
    "Send the first note" is exactly right for them, and flagging it would
    make the checker noise."""
    Contact.all_objects.create(user=user, name="Someone New", source="manual")
    assert health.discovered_but_untouched(user) == []
    assert not any(l.startswith("⚠") for l in health.health_report(user))


def test_archived_people_are_left_out(user):
    """Archiving is a deliberate exit. Their history stops being a live claim
    the product makes."""
    Contact.all_objects.create(user=user, name="Gone", source="capture", archived=True)
    assert health.discovered_but_untouched(user) == []


def test_one_users_contradiction_is_not_reported_to_another(user, django_user_model):
    other = django_user_model.objects.create_user(
        email="other@example.com", password="x", capture_slug="otherslug1")
    Contact.all_objects.create(user=other, name="Theirs", source="capture")
    assert health.discovered_but_untouched(user) == []
    assert [c.name for c in health.discovered_but_untouched(other)] == ["Theirs"]


# ---------------------------------------------------------------------------
# The review list — surfaced, never called a fault
# ---------------------------------------------------------------------------
def test_an_address_with_no_touches_is_reviewed_not_blamed(user):
    Contact.all_objects.create(user=user, name="Not Written Yet",
                               email="x@y.com", source="manual")
    lines = health.health_report(user)
    assert len(lines) == 1
    assert lines[0].startswith("·"), "a note, not a warning"
    assert "Not Written Yet" in lines[0]


def test_a_contact_with_no_address_is_not_on_the_review_list(user):
    """Nothing to search a mailbox for, so nothing the sync could have
    missed."""
    Contact.all_objects.create(user=user, name="No Address", source="manual")
    assert health.untouched_with_an_address(user) == []


# ---------------------------------------------------------------------------
# The command fails loudly, and only on the real thing
# ---------------------------------------------------------------------------
def test_the_command_exits_non_zero_on_a_contradiction(user):
    Contact.all_objects.create(user=user, name="Jason Law", source="capture")
    with pytest.raises(CommandError, match="contradiction"):
        call_command("crm_health", email=user.email)


def test_the_review_list_alone_does_not_fail_the_command(user):
    """A perfectly ordinary board must not make a scheduled job scream."""
    Contact.all_objects.create(user=user, name="Not Written Yet",
                               email="x@y.com", source="manual")
    call_command("crm_health", email=user.email)


def test_a_clean_board_passes(user):
    c = Contact.all_objects.create(user=user, name="Ada", source="capture",
                                   email="ada@gs.com")
    _touch(user, c)
    call_command("crm_health", email=user.email)
