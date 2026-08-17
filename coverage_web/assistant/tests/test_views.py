"""The page and its one POST.

The load-bearing case is the LAST one: with no API key set — which is the
state of every environment today and of every test run — sending a message
must produce a plain, readable sentence in the thread, never a 500 and never
a traceback. Same posture as `crm.views.contact_ai_brief`.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from assistant.models import ChatConversation, ChatMessage

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(email="student@example.com", password="pw12345!")


@pytest.fixture
def signed_in(client, user):
    client.force_login(user)
    return client


def test_the_page_renders_for_a_signed_in_student(signed_in):
    response = signed_in.get(reverse("assistant:chat"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Talk to Coverage" in body
    # The label the whole app puts on model-written text.
    assert "Check it before you rely on it" in body
    assert 'id="as-thread"' in body


def test_an_anonymous_visitor_is_redirected_to_sign_in(client):
    for url in (reverse("assistant:chat"), reverse("assistant:send")):
        response = client.post(url) if url.endswith("send/") else client.get(url)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]


def test_send_rejects_a_get(signed_in):
    """Every send costs money, so the entry point must not be something a
    prefetcher or a back button can fire."""
    assert signed_in.get(reverse("assistant:send")).status_code == 405


def test_opening_the_page_creates_exactly_one_conversation(signed_in, user):
    signed_in.get(reverse("assistant:chat"))
    signed_in.get(reverse("assistant:chat"))

    assert ChatConversation.objects.for_user(user).count() == 1


def test_new_conversation_starts_a_fresh_thread(signed_in, user):
    signed_in.get(reverse("assistant:chat"))

    response = signed_in.post(reverse("assistant:new"))

    assert response.status_code == 302
    assert ChatConversation.objects.for_user(user).count() == 2


@override_settings(ANTHROPIC_API_KEY="")
def test_sending_with_no_api_key_is_a_readable_message_not_a_500(signed_in, user):
    response = signed_in.post(reverse("assistant:send"), {"message": "who should I chase?"})

    assert response.status_code == 200
    body = response.content.decode()
    assert "switched on yet" in body
    # The student's own words survive the failure.
    assert "who should I chase?" in body

    turns = list(ChatMessage.objects.for_user(user))
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[1].notice == ChatMessage.NOTICE_UNCONFIGURED


@override_settings(ANTHROPIC_API_KEY="")
def test_the_composer_is_disabled_while_the_feature_is_dark(signed_in):
    body = signed_in.get(reverse("assistant:chat")).content.decode()

    assert "needs an Anthropic API key" in body
    assert "disabled" in body


def test_an_empty_send_changes_nothing(signed_in, user):
    response = signed_in.post(reverse("assistant:send"), {"message": "   "})

    assert response.status_code == 200
    assert ChatMessage.objects.for_user(user).count() == 0


def test_the_thread_hides_tool_machinery_and_names_the_lookups(signed_in, user):
    """A student reads a conversation, not a transcript: tool_result turns are
    invisible, and the tools behind an answer become one quiet evidence line."""
    conversation = ChatConversation(user=user)
    conversation.save()
    for role, content in [
        ("user", [{"type": "text", "text": "who today?"}]),
        ("assistant", [{"type": "tool_use", "id": "t1", "name": "get_today_queue", "input": {}}]),
        ("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]),
        ("assistant", [{"type": "text", "text": "Two people."}]),
    ]:
        m = ChatMessage(user=user, conversation=conversation, role=role, content=content)
        m.save()

    body = signed_in.get(reverse("assistant:chat")).content.decode()

    assert "Two people." in body
    assert "Read your queue" in body
    assert "tool_result" not in body
    assert "get_today_queue" not in body


def test_one_student_never_sees_another_s_thread(client, user):
    other = User.objects.create_user(email="other@example.com", password="pw12345!")
    conversation = ChatConversation(user=other)
    conversation.save()
    m = ChatMessage(
        user=other,
        conversation=conversation,
        role="user",
        content=[{"type": "text", "text": "other student's private plan"}],
    )
    m.save()

    client.force_login(user)
    body = client.get(reverse("assistant:chat")).content.decode()

    assert "other student's private plan" not in body


def test_the_nav_offers_the_page_on_every_signed_in_screen(signed_in):
    body = signed_in.get(reverse("crm:week")).content.decode()

    assert 'href="/assistant/"' in body
    assert ">Talk<" in body
