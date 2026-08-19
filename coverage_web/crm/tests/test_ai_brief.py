"""Tests for the coffee-chat AI brief (crm/ai_brief.py and the
contact_ai_brief view). The safety posture here is different from
directory.ai_extract's single-fact grounding: this is synthesis, not
extraction, so what's tested is that the prompt is built ENTIRELY from data
already scoped to the requesting user, and that every unconfigured/failed
path renders a plain "not available" state rather than an error.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from crm import ai_brief
from crm.models import Contact, Touch
from directory.models import Firm, Opportunity

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(email="student@example.com", password="x")


@pytest.fixture
def firm():
    return Firm.objects.create(slug="north-bank", name="North Bank")


@pytest.fixture
def contact(user, firm):
    return Contact.all_objects.create(
        user=user, name="Jordan Lee", firm=firm, role="Analyst",
        angle="Met at a case comp, very responsive over email.",
    )


# ---------------------------------------------------------------------------
# build_prompt: everything in the prompt must come from THIS contact's own
# scoped data, nothing invented, nothing leaked from another tenant.
# ---------------------------------------------------------------------------

def test_build_prompt_includes_the_contacts_own_facts(contact):
    prompt = ai_brief.build_prompt(contact)
    assert "Jordan Lee" in prompt
    assert "North Bank" in prompt
    assert "Analyst" in prompt
    assert "Met at a case comp" in prompt


def test_build_prompt_falls_back_to_firm_text_when_no_directory_firm(user):
    c = Contact.all_objects.create(user=user, name="Sam", firm_text="Some Boutique Shop")
    prompt = ai_brief.build_prompt(c)
    assert "Some Boutique Shop" in prompt


def test_build_prompt_lists_recent_touches_most_recent_first(contact, user):
    older = timezone.now() - timedelta(days=10)
    newer = timezone.now() - timedelta(days=1)
    Touch.all_objects.create(user=user, contact=contact, ts=older, kind="chat", note="First call.")
    Touch.all_objects.create(user=user, contact=contact, ts=newer, kind="reply_received", note="Replied to follow-up.")

    prompt = ai_brief.build_prompt(contact)
    first_idx = prompt.index("Replied to follow-up.")
    second_idx = prompt.index("First call.")
    assert first_idx < second_idx


def test_build_prompt_says_none_logged_with_no_history(contact):
    prompt = ai_brief.build_prompt(contact)
    assert "(none logged)" in prompt


def test_build_prompt_mentions_open_roles_only_when_the_firm_has_campus_roles(contact, firm):
    prompt_before = ai_brief.build_prompt(contact)
    assert "open campus role" not in prompt_before

    Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        url="https://north-bank.example/apply/1",
    )
    prompt_after = ai_brief.build_prompt(contact)
    assert "1 open campus role" in prompt_after


def test_build_prompt_truncates_a_very_long_angle_note(user, firm):
    c = Contact.all_objects.create(user=user, name="Sam", firm=firm, angle="x" * 5000)
    prompt = ai_brief.build_prompt(c)
    # The raw 5000-char note must not ride into the prompt whole.
    assert "x" * 5000 not in prompt


# ---------------------------------------------------------------------------
# generate_coffee_chat_brief: the is_configured() gate
# ---------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY="")
def test_generate_returns_none_when_not_configured(contact, monkeypatch):
    called = []
    monkeypatch.setattr(ai_brief, "complete_text", lambda *a, **kw: called.append(1))
    assert ai_brief.generate_coffee_chat_brief(contact) is None
    assert called == []


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_generate_returns_the_model_text(contact, monkeypatch):
    monkeypatch.setattr(ai_brief, "complete_text", lambda *a, **kw: "BACKGROUND\n...")
    assert ai_brief.generate_coffee_chat_brief(contact) == "BACKGROUND\n..."


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_generate_returns_none_when_the_api_call_fails(contact, monkeypatch):
    monkeypatch.setattr(ai_brief, "complete_text", lambda *a, **kw: None)
    assert ai_brief.generate_coffee_chat_brief(contact) is None


# ---------------------------------------------------------------------------
# The view: tenant isolation, method, and both rendered states.
# ---------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_view_renders_the_drafted_brief(client, user, contact, monkeypatch):
    monkeypatch.setattr("crm.views.ai_brief.generate_coffee_chat_brief", lambda c: "BACKGROUND\nJordan is warm.")
    client.force_login(user)
    resp = client.post(reverse("crm:contact_ai_brief", args=[contact.pk]))
    assert resp.status_code == 200
    assert b"Jordan is warm" in resp.content
    assert b"AI-drafted" in resp.content


@override_settings(ANTHROPIC_API_KEY="")
def test_view_renders_unavailable_when_not_configured(client, user, contact):
    client.force_login(user)
    resp = client.post(reverse("crm:contact_ai_brief", args=[contact.pk]))
    assert resp.status_code == 200
    assert b"unavailable" in resp.content


def test_view_requires_post(client, user, contact):
    client.force_login(user)
    resp = client.get(reverse("crm:contact_ai_brief", args=[contact.pk]))
    assert resp.status_code == 405


def test_view_404s_for_another_tenants_contact(client, user, contact):
    intruder = User.objects.create_user(email="intruder@example.com", password="x")
    client.force_login(intruder)
    resp = client.post(reverse("crm:contact_ai_brief", args=[contact.pk]))
    assert resp.status_code == 404


def test_view_requires_login(client, contact):
    resp = client.post(reverse("crm:contact_ai_brief", args=[contact.pk]))
    assert resp.status_code in (302, 403)


# ---------------------------------------------------------------------------
# Credit metering (docs/founder-decisions-2026-08-20.md §2b): the brief is
# a user-triggered model call behind a POST button, same shape as an
# advisor chat message, so it is metered through billing.credits the same
# way assistant/agent.py::run_turn meters a chat turn. There is no cache
# to hit here -- unlike crm/ai_summary.py, a brief is never persisted on
# the Contact, so every click is a genuine live call; "not charged twice"
# below is about one click never writing two ledger rows, and "not charged
# on failure" is the closest real analogue this feature has to "not
# charged on a cache hit."
# ---------------------------------------------------------------------------

from billing import credits as billing_credits
from billing.models import CreditLedger

_ONE_CREDIT_PLANS = {
    "free": {"monthly_grant": 1, "message_cost": 1, "daily_burst": 10},
    "pro": {"monthly_grant": 1, "message_cost": 1, "daily_burst": 10},
}


@override_settings(ANTHROPIC_API_KEY="sk-test", CREDIT_PLANS=_ONE_CREDIT_PLANS)
def test_a_successful_generation_charges_exactly_one_credit(client, user, contact, monkeypatch):
    monkeypatch.setattr(
        "crm.views.ai_brief.generate_coffee_chat_brief",
        lambda c: "BACKGROUND\nJordan is warm.",
    )
    client.force_login(user)
    starting_balance = billing_credits.balance(user)

    resp = client.post(reverse("crm:contact_ai_brief", args=[contact.pk]))

    assert resp.status_code == 200
    assert billing_credits.balance(user) == starting_balance - 1
    rows = CreditLedger.all_objects.filter(user=user, kind=CreditLedger.KIND_SPEND_BRIEF)
    assert rows.count() == 1, "one click, one ledger row -- never charged twice for the same generation"
    assert rows.first().delta == -1


@override_settings(ANTHROPIC_API_KEY="", CREDIT_PLANS=_ONE_CREDIT_PLANS)
def test_an_unconfigured_or_failed_generation_is_not_charged(client, user, contact):
    """The closest thing this feature has to a "cache hit": a call that
    returns None (unconfigured here; a failed API call is the same path in
    generate_coffee_chat_brief) never wrote anything metered, so it must
    never debit a credit."""
    client.force_login(user)
    starting_balance = billing_credits.balance(user)

    resp = client.post(reverse("crm:contact_ai_brief", args=[contact.pk]))

    assert resp.status_code == 200
    assert billing_credits.balance(user) == starting_balance
    assert not CreditLedger.all_objects.filter(user=user, kind=CreditLedger.KIND_SPEND_BRIEF).exists()


@override_settings(ANTHROPIC_API_KEY="sk-test", CREDIT_PLANS=_ONE_CREDIT_PLANS)
def test_blocked_at_zero_credits_renders_the_same_honest_notice_the_chat_uses(client, user, contact, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "crm.views.ai_brief.generate_coffee_chat_brief",
        lambda c: calls.append(1) or "should never be reached",
    )
    client.force_login(user)
    # Spend the one monthly credit this plan grants, then try again.
    billing_credits.spend(user, 1, CreditLedger.KIND_SPEND_CHAT, model="test")

    resp = client.post(reverse("crm:contact_ai_brief", args=[contact.pk]))

    assert resp.status_code == 200, "a credit block is a plain notice, never a 500"
    assert calls == [], "the model must never be called once credits are exhausted"
    assert "last of this month&#x27;s credits" in resp.content.decode()
    assert not CreditLedger.all_objects.filter(user=user, kind=CreditLedger.KIND_SPEND_BRIEF).exists()


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_the_daily_burst_guard_counts_a_brief_spend(client, user, contact, monkeypatch):
    """spend_brief must land inside billing.credits._SPEND_KINDS, or a
    student could draft unlimited briefs past the daily abuse guard."""
    burst_settings = {
        "free": {"monthly_grant": 100, "message_cost": 1, "daily_burst": 1},
        "pro": {"monthly_grant": 100, "message_cost": 1, "daily_burst": 1},
    }
    monkeypatch.setattr(
        "crm.views.ai_brief.generate_coffee_chat_brief",
        lambda c: "BACKGROUND\n...",
    )
    client.force_login(user)
    with override_settings(CREDIT_PLANS=burst_settings):
        first = client.post(reverse("crm:contact_ai_brief", args=[contact.pk]))
        assert b"AI-drafted" in first.content

        assert billing_credits.daily_spent(user) == 1

        second = client.post(reverse("crm:contact_ai_brief", args=[contact.pk]))
        # The burst guard trips with balance still positive (100 granted,
        # 1 spent) -- so this is the "safety net" notice, not "last of this
        # month's credits".
        assert b"safety net" in second.content
        assert b"last of this month" not in second.content
