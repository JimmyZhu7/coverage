"""Tests for the post-chat debrief (crm/debrief.py, crm.models.ChatDebrief,
and the three debrief views).

`transaction=True` throughout: `debrief.promote` goes through
`crm.services.set_contact_state`, which opens its own psycopg connection
and can only see committed rows (see test_services.py's module docstring for
the full reasoning). The prompt/expiry tests don't strictly need it, but
keeping one mode for the whole module avoids the trap of a later edit adding
a services call to a non-transactional test.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm import debrief as debrief_svc
from crm.models import ChatDebrief, Contact, Task, Touch
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def user():
    return User.objects.create_user(email="student@example.com", password="x")


@pytest.fixture
def firm():
    return Firm.objects.create(slug="north-bank", name="North Bank")


@pytest.fixture
def contact(user, firm):
    return Contact.all_objects.create(
        user=user, name="Dana Chat", firm=firm, warmth="chatted",
        thread_state="chat_done", region="us",
    )


def _chat(user, contact, *, days_ago=1):
    return Touch.all_objects.create(
        user=user,
        contact=contact,
        ts=timezone.now() - timedelta(days=days_ago),
        kind="chat",
        channel="coffee_chat",
    )


# ---------------------------------------------------------------------------
# 1. The prompt: which chats ask for a debrief.
# ---------------------------------------------------------------------------
def test_recent_chat_without_a_debrief_is_pending(user, contact):
    touch = _chat(user, contact, days_ago=2)
    pending = debrief_svc.pending(user)
    assert [p["touch"].id for p in pending] == [touch.id]
    assert pending[0]["days_ago"] == 2


def test_old_chat_never_asks_for_a_debrief(user, contact):
    """The stale-thank-you lesson, applied: replaying months of history must
    not produce a wall of debrief prompts on day one."""
    _chat(user, contact, days_ago=debrief_svc.DEBRIEF_EXPIRES_AFTER_DAYS + 1)
    assert debrief_svc.pending(user) == []


def test_boundary_chat_is_still_inside_the_window(user, contact):
    _chat(user, contact, days_ago=debrief_svc.DEBRIEF_EXPIRES_AFTER_DAYS - 1)
    assert len(debrief_svc.pending(user)) == 1


def test_written_or_dismissed_debriefs_stop_asking(user, contact):
    touch = _chat(user, contact)
    debrief_svc.dismiss(user, touch)
    assert debrief_svc.pending(user) == []


def test_only_one_prompt_per_person(user, contact):
    """Two chats in one week is one card, not two faces of the same person
    stacked in the queue."""
    _chat(user, contact, days_ago=1)
    _chat(user, contact, days_ago=4)
    assert len(debrief_svc.pending(user)) == 1


def test_today_page_surfaces_a_debrief_card(client, user, contact):
    _chat(user, contact, days_ago=1)
    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Debrief" in body
    assert "Dana Chat" in body


def test_today_dismiss_removes_the_card(client, user, contact):
    touch = _chat(user, contact, days_ago=1)
    client.force_login(user)
    resp = client.post(reverse("crm:debrief_dismiss", args=[touch.id]))
    assert resp.status_code == 200
    assert "Write it down" not in resp.content.decode()
    assert ChatDebrief.objects.for_user(user).get(touch=touch).dismissed is True


# ---------------------------------------------------------------------------
# 2. The record: side effects, exactly once.
# ---------------------------------------------------------------------------
def test_record_creates_note_referral_contact_and_tasks(user, contact, firm):
    touch = _chat(user, contact)
    today = timezone.localdate()
    row, made = debrief_svc.record(
        user,
        touch,
        learned="Runs the HK coverage team. Wants a follow-up in August.",
        intro_name="Sam Referral",
        intro_email="sam@northbank.com",
        tracked_date=today + timedelta(days=20),
        date_note="Their desk starts reading apps",
        advocate_answer="yes",
    )

    contact.refresh_from_db()
    assert "Chat debrief" in contact.notes
    assert "Runs the HK coverage team" in contact.notes

    referral = made["intro_contact"]
    assert referral.name == "Sam Referral"
    # Same firm by default, provenance recorded, region inherited.
    assert referral.firm_id == firm.id
    assert referral.source == "referral"
    assert referral.region == "us"
    assert "Intro from Dana Chat" in referral.notes

    tasks = list(Task.objects.for_user(user).order_by("kind"))
    assert [t.kind for t in tasks] == ["debrief_date", "intro_follow_up"]
    assert tasks[0].due == today + timedelta(days=20)
    assert tasks[0].title == "Their desk starts reading apps"
    assert tasks[1].firm_id == firm.id

    # Recorded, not acted on: the promotion is only an offer.
    assert row.advocate_answer == "yes"
    assert row.promoted is False
    contact.refresh_from_db()
    assert contact.warmth == "chatted"


def test_record_is_idempotent(user, contact):
    """The brief's hard requirement: re-submitting must not create duplicate
    contacts or tasks, and must not append the note twice."""
    touch = _chat(user, contact)
    payload = dict(
        learned="They mentor two analysts.",
        intro_name="Sam Referral",
        tracked_date=timezone.localdate() + timedelta(days=10),
        advocate_answer="unsure",
    )
    first, _ = debrief_svc.record(user, touch, **payload)
    second, made = debrief_svc.record(user, touch, **payload)

    assert first.pk == second.pk
    assert ChatDebrief.objects.for_user(user).count() == 1
    assert Contact.objects.for_user(user).filter(name="Sam Referral").count() == 1
    assert Task.objects.for_user(user).count() == 2
    assert made == {}  # the second call made nothing
    contact.refresh_from_db()
    assert contact.notes.count("They mentor two analysts.") == 1


def test_a_second_chat_gets_its_own_debrief(user, contact):
    """Idempotence is per chat touch, not per contact — a later chat is a
    new conversation with new things to remember."""
    first_chat = _chat(user, contact, days_ago=5)
    second_chat = _chat(user, contact, days_ago=1)
    debrief_svc.record(user, first_chat, learned="First.")
    debrief_svc.record(user, second_chat, learned="Second.")
    assert ChatDebrief.objects.for_user(user).count() == 2


def test_promotion_goes_through_the_audited_override(user, contact):
    touch = _chat(user, contact)
    row, _ = debrief_svc.record(user, touch, advocate_answer="yes")
    debrief_svc.promote(row)

    contact.refresh_from_db()
    assert contact.warmth == "advocate"
    assert row.promoted is True
    # set_contact_state writes its own audit touch — the state change is
    # never silent.
    audit = Touch.objects.for_user(user).exclude(id=touch.id)
    assert audit.count() == 1
    assert "advocate" in (audit.first().note or "")

    # Second call is a no-op, not a second audit row.
    debrief_svc.promote(row)
    assert Touch.objects.for_user(user).exclude(id=touch.id).count() == 1


# ---------------------------------------------------------------------------
# 3. The views, end to end.
# ---------------------------------------------------------------------------
def test_debrief_post_saves_and_offers_the_promotion(client, user, contact):
    touch = _chat(user, contact)
    client.force_login(user)
    resp = client.post(
        reverse("crm:debrief", args=[touch.id]),
        {
            "learned": "Covers financial sponsors.",
            "intro_name": "Sam Referral",
            "intro_email": "sam@northbank.com",
            "advocate_answer": "yes",
        },
        follow=True,
    )
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Promote to advocate" in body
    assert Contact.objects.for_user(user).filter(name="Sam Referral").exists()
    # Still not promoted — the page offers, the user decides.
    contact.refresh_from_db()
    assert contact.warmth == "chatted"

    client.post(reverse("crm:debrief_promote", args=[touch.id]))
    contact.refresh_from_db()
    assert contact.warmth == "advocate"


def test_advocate_radios_offer_exactly_three_answers(user, contact):
    """The model field is blank=True, so Django would prepend an empty
    choice — which RadioSelect renders as a pre-CHECKED fourth radio,
    silently answering the question for the user. Pinned here because it
    only shows up in the rendered widget, not in cleaned_data."""
    from crm.forms import ChatDebriefForm

    form = ChatDebriefForm()
    assert [v for v, _ in form.fields["advocate_answer"].choices] == [
        "yes", "no", "unsure",
    ]
    assert 'value=""' not in str(form["advocate_answer"])


def test_a_label_without_a_date_is_an_error_not_a_silent_drop(client, user, contact):
    touch = _chat(user, contact)
    client.force_login(user)
    resp = client.post(
        reverse("crm:debrief", args=[touch.id]),
        {"date_note": "Their apps open", "tracked_date": ""},
    )
    assert resp.status_code == 200  # re-rendered with the error, not saved
    assert "Pick the date this refers to." in resp.content.decode()
    assert not ChatDebrief.objects.for_user(user).exists()


def test_debrief_rejects_a_non_chat_touch(client, user, contact):
    """The debrief is defined against a chat; an outreach touch has nothing
    to debrief."""
    outreach = Touch.all_objects.create(
        user=user, contact=contact, ts=timezone.now(), kind="outreach", channel="email"
    )
    client.force_login(user)
    assert client.get(reverse("crm:debrief", args=[outreach.id])).status_code == 404


# ---------------------------------------------------------------------------
# 4. Tenancy — a debrief belongs to exactly one user.
# ---------------------------------------------------------------------------
def test_another_users_debrief_is_invisible(client, user, contact):
    touch = _chat(user, contact)
    debrief_svc.record(user, touch, learned="Private to the owner.")

    intruder = User.objects.create_user(email="intruder@example.com", password="x")
    client.force_login(intruder)
    # The touch itself 404s for the intruder, indistinguishably from a
    # missing id — so does dismissing and promoting it.
    assert client.get(reverse("crm:debrief", args=[touch.id])).status_code == 404
    assert client.post(reverse("crm:debrief_dismiss", args=[touch.id])).status_code == 404
    assert client.post(reverse("crm:debrief_promote", args=[touch.id])).status_code == 404
    # And the intruder's own scoped reads see nothing.
    assert ChatDebrief.objects.for_user(intruder).count() == 0
    assert debrief_svc.pending(intruder) == []
    assert ChatDebrief.objects.for_user(user).count() == 1
