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

from assistant.models import ChatConversation, ChatFolder, ChatMessage

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


def test_opening_a_specific_conversation_by_id_shows_its_own_thread(signed_in, user):
    older = ChatConversation(user=user, title="Older chat")
    older.save()
    newer = ChatConversation(user=user, title="Newer chat")
    newer.save()
    ChatMessage(
        user=user, conversation=older, role="user",
        content=[{"type": "text", "text": "only in the older chat"}],
    ).save()

    body = signed_in.get(reverse("assistant:chat_conversation", args=[older.id])).content.decode()

    assert "only in the older chat" in body
    # The bare URL still resolves to the newest one by default.
    assert signed_in.get(reverse("assistant:chat")).content.decode().find("Newer chat") != -1


def test_opening_another_students_conversation_by_id_404s(client, user):
    other = User.objects.create_user(email="other2@example.com", password="pw12345!")
    theirs = ChatConversation(user=other)
    theirs.save()

    client.force_login(user)
    response = client.get(reverse("assistant:chat_conversation", args=[theirs.id]))

    assert response.status_code == 404


@override_settings(ANTHROPIC_API_KEY="")
def test_sending_replies_to_the_conversation_named_in_the_composer_not_the_newest_one(signed_in, user):
    """Once the history panel can open an older chat, a send from inside it
    must land there — not silently on whatever conversation is newest.

    No API key, like the other send() tests in this file: the point is
    routing, not the model, and a real call would be slow, billed, and
    non-deterministic in how many messages it produces."""
    old = ChatConversation(user=user)
    old.save()
    newer = ChatConversation(user=user)  # becomes "newest" just by existing
    newer.save()

    signed_in.post(reverse("assistant:send"), {"message": "hello", "conversation": old.id})

    assert ChatMessage.objects.for_user(user).filter(conversation=old, role="user").count() == 1
    assert ChatMessage.objects.for_user(user).filter(conversation=newer).count() == 0


def test_renaming_a_conversation_updates_its_title(signed_in, user):
    conversation = ChatConversation(user=user, title="Old title")
    conversation.save()

    response = signed_in.post(
        reverse("assistant:rename"), {"conversation": conversation.id, "title": "Follow-up plan"}
    )

    assert response.status_code == 200
    conversation.refresh_from_db()
    assert conversation.title == "Follow-up plan"
    assert "Follow-up plan" in response.content.decode()


def test_renaming_another_students_conversation_404s(client, user):
    other = User.objects.create_user(email="other3@example.com", password="pw12345!")
    theirs = ChatConversation(user=other, title="Not yours")
    theirs.save()

    client.force_login(user)
    response = client.post(reverse("assistant:rename"), {"conversation": theirs.id, "title": "Hijacked"})

    assert response.status_code == 404
    theirs.refresh_from_db()
    assert theirs.title == "Not yours"


def test_deleting_the_conversation_being_viewed_lands_on_the_next_one(signed_in, user):
    kept = ChatConversation(user=user, title="Keep me")
    kept.save()
    doomed = ChatConversation(user=user, title="Delete me")
    doomed.save()

    response = signed_in.post(
        reverse("assistant:delete"), {"conversation": doomed.id, "current": doomed.id}
    )

    assert response.status_code == 302
    assert not ChatConversation.objects.for_user(user).filter(pk=doomed.id).exists()
    assert ChatConversation.objects.for_user(user).filter(pk=kept.id).exists()


def test_deleting_a_different_conversation_leaves_the_current_one_open(signed_in, user):
    viewing = ChatConversation(user=user, title="Currently open")
    viewing.save()
    other = ChatConversation(user=user, title="Delete from the sidebar")
    other.save()

    response = signed_in.post(
        reverse("assistant:delete"), {"conversation": other.id, "current": viewing.id}
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("assistant:chat_conversation", args=[viewing.id])
    assert not ChatConversation.objects.for_user(user).filter(pk=other.id).exists()


def test_deleting_the_only_conversation_creates_a_fresh_one_on_return(signed_in, user):
    only = ChatConversation(user=user)
    only.save()

    signed_in.post(reverse("assistant:delete"), {"conversation": only.id, "current": only.id})

    assert ChatConversation.objects.for_user(user).count() == 0
    # The bare chat view's own fallback creates a new one rather than 500ing.
    response = signed_in.get(reverse("assistant:chat"))
    assert response.status_code == 200
    assert ChatConversation.objects.for_user(user).count() == 1


def test_deleting_another_students_conversation_404s_and_changes_nothing(client, user):
    other = User.objects.create_user(email="other4@example.com", password="pw12345!")
    theirs = ChatConversation(user=other, title="Not yours")
    theirs.save()

    client.force_login(user)
    response = client.post(reverse("assistant:delete"), {"conversation": theirs.id, "current": theirs.id})

    assert response.status_code == 404
    assert ChatConversation.objects.for_user(other).filter(pk=theirs.id).exists()


def test_deleting_a_conversation_also_deletes_its_messages(signed_in, user):
    conversation = ChatConversation(user=user)
    conversation.save()
    ChatMessage(
        user=user, conversation=conversation, role="user",
        content=[{"type": "text", "text": "gone with the conversation"}],
    ).save()

    signed_in.post(reverse("assistant:delete"), {"conversation": conversation.id, "current": conversation.id})

    assert ChatMessage.objects.for_user(user).count() == 0


def test_delete_rejects_a_get(signed_in):
    assert signed_in.get(reverse("assistant:delete")).status_code == 405


@override_settings(ANTHROPIC_API_KEY="")
def test_the_stream_endpoint_is_server_sent_events_and_still_persists_the_turn(signed_in, user):
    response = signed_in.post(reverse("assistant:stream"), {"message": "who should I chase?"})

    assert response.status_code == 200
    assert response["Content-Type"] == "text/event-stream"
    body = b"".join(response.streaming_content).decode()
    assert body.startswith("data: ")
    assert '"type": "notice"' in body
    assert '"kind": "unconfigured"' in body

    turns = list(ChatMessage.objects.for_user(user))
    assert [t.role for t in turns] == ["user", "assistant"]


def test_stream_rejects_a_get(signed_in):
    assert signed_in.get(reverse("assistant:stream")).status_code == 405


def test_history_fragment_lists_conversations_and_marks_the_current_one(signed_in, user):
    older = ChatConversation(user=user, title="Older")
    older.save()
    newer = ChatConversation(user=user, title="Newer")
    newer.save()

    body = signed_in.get(
        reverse("assistant:history"), {"conversation": older.id}
    ).content.decode()

    assert "Older" in body
    assert "Newer" in body
    assert 'id="as-history"' in body


def test_history_fragment_never_shows_another_students_conversations(client, user):
    other = User.objects.create_user(email="hist-other@example.com", password="pw12345!")
    ChatConversation(user=other, title="Not yours").save()

    client.force_login(user)
    body = client.get(reverse("assistant:history")).content.decode()

    assert "Not yours" not in body


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------
def test_creating_a_folder_shows_it_empty_in_the_sidebar(signed_in, user):
    response = signed_in.post(reverse("assistant:folder_new"), {"name": "BofA prep"})

    assert response.status_code == 200
    assert "BofA prep" in response.content.decode()
    assert ChatFolder.objects.for_user(user).filter(name="BofA prep").exists()


def test_a_blank_folder_name_creates_nothing(signed_in, user):
    signed_in.post(reverse("assistant:folder_new"), {"name": "   "})

    assert ChatFolder.objects.for_user(user).count() == 0


def test_moving_a_conversation_into_a_folder_groups_it_there(signed_in, user):
    folder = ChatFolder(user=user, name="BofA prep")
    folder.save()
    conversation = ChatConversation(user=user, title="Chase them")
    conversation.save()

    response = signed_in.post(
        reverse("assistant:move"), {"conversation": conversation.id, "folder": folder.id}
    )

    assert response.status_code == 200
    conversation.refresh_from_db()
    assert conversation.folder_id == folder.id


def test_moving_a_conversation_back_to_no_folder(signed_in, user):
    folder = ChatFolder(user=user, name="BofA prep")
    folder.save()
    conversation = ChatConversation(user=user, folder=folder)
    conversation.save()

    signed_in.post(reverse("assistant:move"), {"conversation": conversation.id, "folder": ""})

    conversation.refresh_from_db()
    assert conversation.folder_id is None


def test_moving_into_another_students_folder_404s(client, user):
    other = User.objects.create_user(email="folder-other@example.com", password="pw12345!")
    theirs = ChatFolder(user=other, name="Not yours")
    theirs.save()
    conversation = ChatConversation(user=user)
    conversation.save()

    client.force_login(user)
    response = client.post(
        reverse("assistant:move"), {"conversation": conversation.id, "folder": theirs.id}
    )

    assert response.status_code == 404
    conversation.refresh_from_db()
    assert conversation.folder_id is None


def test_renaming_a_folder(signed_in, user):
    folder = ChatFolder(user=user, name="Old name")
    folder.save()

    response = signed_in.post(reverse("assistant:folder_rename"), {"folder": folder.id, "name": "New name"})

    assert response.status_code == 200
    folder.refresh_from_db()
    assert folder.name == "New name"
    assert "New name" in response.content.decode()


def test_deleting_a_folder_unfiles_its_conversations_without_deleting_them(signed_in, user):
    folder = ChatFolder(user=user, name="Temp")
    folder.save()
    conversation = ChatConversation(user=user, folder=folder, title="Still here")
    conversation.save()

    response = signed_in.post(reverse("assistant:folder_delete"), {"folder": folder.id})

    assert response.status_code == 200
    assert not ChatFolder.objects.for_user(user).filter(pk=folder.id).exists()
    conversation.refresh_from_db()
    assert conversation.folder_id is None
    assert ChatConversation.objects.for_user(user).filter(pk=conversation.id).exists()


def test_deleting_another_students_folder_404s(client, user):
    other = User.objects.create_user(email="folder-other2@example.com", password="pw12345!")
    theirs = ChatFolder(user=other, name="Not yours")
    theirs.save()

    client.force_login(user)
    response = client.post(reverse("assistant:folder_delete"), {"folder": theirs.id})

    assert response.status_code == 404
    assert ChatFolder.objects.for_user(other).filter(pk=theirs.id).exists()


def test_the_history_fragment_groups_conversations_by_folder(signed_in, user):
    folder = ChatFolder(user=user, name="BofA prep")
    folder.save()
    ChatConversation(user=user, folder=folder, title="In the folder").save()
    ChatConversation(user=user, title="Not in any folder").save()

    body = signed_in.get(reverse("assistant:history")).content.decode()

    assert "BofA prep" in body
    assert "In the folder" in body
    assert "Not in any folder" in body


def test_folder_actions_reject_a_get(signed_in):
    for name in ("assistant:folder_new", "assistant:folder_rename", "assistant:folder_delete", "assistant:move"):
        assert signed_in.get(reverse(name)).status_code == 405


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
@override_settings(ANTHROPIC_API_KEY="")
def test_an_unsupported_attachment_is_a_notice_not_a_model_call(signed_in, user):
    """No API key AND an invalid file — if the attachment check didn't
    short-circuit before the agent, this would show the unconfigured
    notice instead of the attachment one. It has to show the attachment
    reason, which only happens if send() never even tried to call the
    agent for a request this bad."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    bad = SimpleUploadedFile("resume.docx", b"not a real docx", content_type="application/msword")

    response = signed_in.post(
        reverse("assistant:send"), {"message": "here's my resume", "file": bad}
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "resume.docx" in body
    # Not "isn't supported" verbatim — chat_format HTML-escapes the notice
    # text like any other model-adjacent text, which turns the apostrophe
    # into an entity. "images, PDFs" is the same sentence without one.
    assert "images, PDFs" in body
    # The student's own words survive the rejection, same as every other
    # reason a turn stops before the API.
    assert "here" in body and "resume" in body


def test_an_unsupported_attachment_never_reaches_stream_turn(signed_in, user):
    """Same guarantee as above, on the streaming transport — the SSE body
    carries one notice frame, not a model round-trip."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    bad = SimpleUploadedFile("resume.docx", b"not a real docx", content_type="application/msword")

    response = signed_in.post(reverse("assistant:stream"), {"message": "hi", "file": bad})

    assert response.status_code == 200
    body = b"".join(response.streaming_content).decode()
    assert '"type": "notice"' in body
    assert "resume.docx" in body

    turns = list(ChatMessage.objects.for_user(user))
    # The user's text, then the notice — never a tool_use/tool_result pair,
    # which is what a real model round-trip would have produced.
    assert [t.role for t in turns] == ["user", "assistant"]


@override_settings(ANTHROPIC_API_KEY="")
def test_a_message_with_only_an_oversized_attachment_and_no_text_is_still_a_clean_notice(signed_in, user):
    from django.core.files.uploadedfile import SimpleUploadedFile

    huge = SimpleUploadedFile("huge.png", b"x" * (6 * 1024 * 1024), content_type="image/png")

    response = signed_in.post(reverse("assistant:send"), {"message": "", "file": huge})

    assert response.status_code == 200
    body = response.content.decode()
    assert "huge.png" in body
    # No text was sent, so there is no user turn to preserve — just the
    # notice explaining why nothing went out.
    turns = list(ChatMessage.objects.for_user(user))
    assert [t.role for t in turns] == ["assistant"]
