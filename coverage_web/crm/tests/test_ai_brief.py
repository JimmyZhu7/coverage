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
