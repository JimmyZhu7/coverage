"""The page and its one POST.

The load-bearing case is the LAST one: with no API key set — which is the
state of every environment today and of every test run — sending a message
must produce a plain, readable sentence in the thread, never a 500 and never
a traceback. Same posture as `crm.views.contact_ai_brief`.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from assistant.models import AdvisorMemory, ChatConversation, ChatFolder, ChatMessage
from crm.models import Contact, Touch
from directory.models import Firm

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


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_tool_call_made_mid_stream_sees_the_students_own_timezone(client, monkeypatch):
    """The bug, reproduced: `StreamingHttpResponse` is lazy, so `stream()`'s
    generator body — including every tool call the model makes — actually
    runs AFTER TimezoneMiddleware's own `finally: timezone.deactivate()` has
    already fired, not during the request/response cycle the middleware
    thinks it is protecting. Measured live: a "6-8pm today" calendar entry
    for an Asia/Shanghai student landed at 2am the following day — 18:00
    stored as a UTC wall-clock reading instead of converted from Shanghai
    time. This test scripts the model into calling add_calendar_event with a
    bare "18:00" and asserts the stored instant is 18:00 *Shanghai*, not
    18:00 UTC — it must fail on the bug and pass on the fix."""
    from assistant import agent
    from assistant.tests.test_agent import FakeStreamingClient, _response, _text, _tool_use
    from crm.models import CalendarEvent

    user = User.objects.create_user(
        email="shanghai@example.com", password="pw12345!", timezone="Asia/Shanghai",
    )
    client.force_login(user)

    fake = FakeStreamingClient([
        (
            [],
            _response(
                [_tool_use("add_calendar_event", {"title": "Gym", "date": "2026-08-18", "start_time": "18:00"})],
                "tool_use",
            ),
        ),
        (["Done."], _response([_text("Done.")], "end_turn")),
    ])
    monkeypatch.setattr(agent, "get_client", lambda: fake)

    response = client.post(reverse("assistant:stream"), {"message": "add gym at 6pm today"})
    b"".join(response.streaming_content)  # the generator only runs once consumed

    event = CalendarEvent.objects.for_user(user).get()
    shanghai_wall_clock = timezone.localtime(event.starts_at, ZoneInfo("Asia/Shanghai"))
    assert shanghai_wall_clock.strftime("%H:%M") == "18:00"
    # The bug's own symptom, named directly: with the timezone lost, this
    # would be 18:00 UTC, which reads back as 02:00 the NEXT day in Shanghai.
    assert shanghai_wall_clock.date().isoformat() == "2026-08-18"


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_the_confirm_twice_protocol_survives_a_real_streamed_turn(client, monkeypatch):
    """The two-call handshake, driven end to end through the actual endpoint
    rather than against `tools.execute` directly — because the thing being
    checked is that the REFUSAL is what reaches the model mid-stream, and
    that the turn then finishes normally instead of dying on it. Two POSTs,
    the way a student would do it: the first asks, the second confirms."""
    from assistant import agent
    from assistant.tests.test_agent import FakeStreamingClient, _response, _text, _tool_use

    user = User.objects.create_user(email="mover@example.com", password="pw12345!")
    client.force_login(user)

    fake = FakeStreamingClient([
        # Turn one: the model tries it, gets told no, explains and asks.
        ([], _response([_tool_use("update_settings", {"field": "timezone", "value": "Europe/London"})], "tool_use")),
        (
            ["That changes what day"],
            _response([_text("That changes what day your queue and deadlines think it is. Go ahead?")], "end_turn"),
        ),
        # Turn two: the student said yes, so now it carries confirmed.
        (
            [],
            _response(
                [_tool_use("update_settings", {"field": "timezone", "value": "Europe/London", "confirmed": True})],
                "tool_use",
            ),
        ),
        (["Moved"], _response([_text("Moved you to London time.")], "end_turn")),
    ])
    monkeypatch.setattr(agent, "get_client", lambda: fake)

    first = client.post(reverse("assistant:stream"), {"message": "I've moved to London"})
    b"".join(first.streaming_content)  # the generator only runs once consumed

    user.refresh_from_db()
    assert user.timezone == ""  # asked, not done

    second = client.post(reverse("assistant:stream"), {"message": "yes please"})
    body = b"".join(second.streaming_content).decode()

    user.refresh_from_db()
    assert user.timezone == "Europe/London"
    assert user.timezone_auto is False
    assert "changed a setting" in body  # the evidence line the student sees


@pytest.mark.django_db(transaction=True)
@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_bulk_status_change_made_mid_stream_moves_only_this_students_people(client, monkeypatch):
    """`services.set_contact_state` commits on its own psycopg connection, so
    this is the transactional end-to-end check that the loop over it really
    lands — three ids in, two of them this student's, one somebody else's."""
    from assistant import agent
    from assistant.tests.test_agent import FakeStreamingClient, _response, _text, _tool_use

    user = User.objects.create_user(email="parker@example.com", password="pw12345!")
    stranger = User.objects.create_user(email="stranger@example.com", password="pw12345!")
    client.force_login(user)

    quiet_one = Contact(user=user, name="Quiet One")
    quiet_one.save()
    quiet_two = Contact(user=user, name="Quiet Two")
    quiet_two.save()
    not_theirs = Contact(user=stranger, name="Someone Else's")
    not_theirs.save()

    fake = FakeStreamingClient([
        (
            [],
            _response(
                [_tool_use("set_contact_status", {
                    "contact_ids": [quiet_one.id, not_theirs.id, quiet_two.id],
                    "thread_state": "parked",
                    "note": "No reply after three notes",
                })],
                "tool_use",
            ),
        ),
        (["Parked two"], _response([_text("Parked two of them.")], "end_turn")),
    ])
    monkeypatch.setattr(agent, "get_client", lambda: fake)

    response = client.post(reverse("assistant:stream"), {"message": "park the quiet ones"})
    b"".join(response.streaming_content)

    for contact in (quiet_one, quiet_two):
        contact.refresh_from_db()
        assert contact.thread_state == "parked"
    not_theirs.refresh_from_db()
    assert not_theirs.thread_state == "no_reply"
    assert Touch.objects.for_user(user).count() == 2
    assert Touch.objects.for_user(stranger).count() == 0


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


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
def test_the_page_shows_remembered_facts(signed_in, user):
    AdvisorMemory(user=user, text="Ruled out PE roles.").save()

    body = signed_in.get(reverse("assistant:chat")).content.decode()

    assert "Ruled out PE roles." in body


def test_forgetting_a_fact_deletes_it_and_returns_the_updated_list(signed_in, user):
    memory = AdvisorMemory(user=user, text="Ruled out PE roles.")
    memory.save()
    keep = AdvisorMemory(user=user, text="Needs sponsorship in the US.")
    keep.save()

    response = signed_in.post(reverse("assistant:forget_memory"), {"memory": memory.id})

    assert response.status_code == 200
    body = response.content.decode()
    assert "Ruled out PE roles." not in body
    assert "Needs sponsorship in the US." in body
    assert not AdvisorMemory.objects.for_user(user).filter(pk=memory.id).exists()
    assert AdvisorMemory.objects.for_user(user).filter(pk=keep.id).exists()


def test_forgetting_another_students_memory_404s(client, user):
    other = User.objects.create_user(email="memory-other@example.com", password="pw12345!")
    theirs = AdvisorMemory(user=other, text="Not yours to forget.")
    theirs.save()

    client.force_login(user)
    response = client.post(reverse("assistant:forget_memory"), {"memory": theirs.id})

    assert response.status_code == 404
    assert AdvisorMemory.objects.for_user(other).filter(pk=theirs.id).exists()


def test_forget_memory_rejects_a_get(signed_in):
    assert signed_in.get(reverse("assistant:forget_memory")).status_code == 405


# ---------------------------------------------------------------------------
# The draft card's one-click "Log touch"
#
# The write itself is `crm.services.log_touch` — the same single audited path
# the model's own log_touch tool and the CRM's own button already go through,
# with the same `source="assistant"` and the same `[assistant:<id>]` note
# marker. What is new, and what these cover, is everything AROUND that write:
# who is allowed to ask for it, what happens when they ask twice, and what the
# page shows afterwards.
# ---------------------------------------------------------------------------
@pytest.fixture
def draft_setup(user):
    """A contact and an assistant reply that actually holds a draft for them
    — the endpoint refuses to log against a message that doesn't."""
    firm = Firm.objects.create(slug="draft-bank", name="Draft Bank")
    contact = Contact(user=user, firm=firm, name="Yumna Rahman", role="Associate")
    contact.save()
    conversation = ChatConversation(user=user, title="Follow-ups")
    conversation.save()
    message = ChatMessage(
        user=user,
        conversation=conversation,
        role=ChatMessage.ROLE_ASSISTANT,
        content=[
            {
                "type": "text",
                "text": (
                    f"```draft contact={contact.id} channel=email kind=follow_up\n"
                    "Subject: Catching up\n\nHi Yumna,\n\nBest,\nJimmy\n```"
                ),
            }
        ],
    )
    message.save()
    return {"contact": contact, "conversation": conversation, "message": message}


def _log_draft(client, setup, **overrides):
    payload = {
        "message": setup["message"].id,
        "contact": setup["contact"].id,
        "channel": "email",
        "kind": "follow_up",
    }
    payload.update(overrides)
    return client.post(reverse("assistant:log_draft_touch"), payload)


@pytest.mark.django_db(transaction=True)
def test_logging_a_drafted_touch_writes_it_to_the_crm_and_swaps_the_chip(signed_in, user, draft_setup):
    """The whole point: the student sends the email in Gmail, comes back, and
    records it in one click with no second trip through the model.

    `transaction=True` for the same reason assistant/tests/test_tools.py's
    write test needs it: `crm.services.log_touch` commits on its own psycopg
    connection, outside Django's per-test transaction."""
    response = _log_draft(signed_in, draft_setup)

    assert response.status_code == 200
    assert "Logged" in response.content.decode()

    touches = list(Touch.objects.for_user(user))
    assert len(touches) == 1
    assert (touches[0].kind, touches[0].channel) == ("follow_up", "email")
    # Same source and same marker as a touch the MODEL logged, so a later
    # audit of "what did the advisor do to my network" reads both identically.
    assert touches[0].source == "assistant"
    assert touches[0].note == f"[assistant:{draft_setup['message'].id}]"


@pytest.mark.django_db(transaction=True)
def test_logging_the_same_draft_twice_does_not_double_log_it(signed_in, user, draft_setup):
    """A double-click and a retried request are both normal. The marker is
    the identity, so the second POST is a no-op that still answers Logged."""
    _log_draft(signed_in, draft_setup)
    response = _log_draft(signed_in, draft_setup)

    assert response.status_code == 200
    assert "Logged" in response.content.decode()
    assert Touch.objects.for_user(user).count() == 1


def test_logging_a_drafted_touch_rejects_a_get(signed_in):
    assert signed_in.get(reverse("assistant:log_draft_touch")).status_code == 405


@pytest.mark.parametrize(
    "bad",
    [{"channel": "carrier_pigeon"}, {"kind": "vibes"}, {"channel": ""}, {"kind": ""}],
)
def test_a_channel_or_kind_the_client_invented_is_a_400_not_a_write(signed_in, user, draft_setup, bad):
    """The chip posts these, so they are client input, so they are checked
    against the same enums crm.views.log_touch checks against."""
    response = _log_draft(signed_in, draft_setup, **bad)

    assert response.status_code == 400
    assert Touch.objects.for_user(user).count() == 0


def test_a_message_holding_no_draft_for_that_contact_is_a_400(signed_in, user, draft_setup):
    """This endpoint is a shortcut for a write the page already offered on
    screen, not a general-purpose "log anything" that happens to be POST."""
    other = Contact(user=user, firm=draft_setup["contact"].firm, name="Someone Else")
    other.save()

    response = _log_draft(signed_in, draft_setup, contact=other.id)

    assert response.status_code == 400
    assert Touch.objects.for_user(user).count() == 0


def test_logging_against_another_students_contact_404s(signed_in, user, draft_setup):
    other_student = User.objects.create_user(email="draft-other@example.com", password="pw12345!")
    theirs = Contact(user=other_student, firm=draft_setup["contact"].firm, name="Their Banker")
    theirs.save()

    response = _log_draft(signed_in, draft_setup, contact=theirs.id)

    assert response.status_code == 404
    assert Touch.objects.for_user(other_student).count() == 0


def test_logging_against_another_students_message_404s(signed_in, user, draft_setup):
    other_student = User.objects.create_user(email="draft-msg-other@example.com", password="pw12345!")
    their_conversation = ChatConversation(user=other_student)
    their_conversation.save()
    their_message = ChatMessage(
        user=other_student,
        conversation=their_conversation,
        role=ChatMessage.ROLE_ASSISTANT,
        content=[{"type": "text", "text": "not yours"}],
    )
    their_message.save()

    response = _log_draft(signed_in, draft_setup, message=their_message.id)

    assert response.status_code == 404
    assert Touch.objects.for_user(user).count() == 0


def test_an_anonymous_visitor_cannot_log_a_drafted_touch(client):
    response = client.post(reverse("assistant:log_draft_touch"))

    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


# ---------------------------------------------------------------------------
# Rewind: edit one of your own past messages and carry on from there.
#
# Destructive by design — everything after the edited message is deleted, not
# branched — so these tests are mostly about what is GONE afterwards, and
# about the one thing that must never be gone: another student's thread.
#
# ANTHROPIC_API_KEY="" throughout except where a scripted client is the
# point. With no key the fresh turn writes the "not switched on" notice
# instead of calling anything, which is all these cases need: a new assistant
# row at the end proves the turn ran, without a slow, billed, non-deterministic
# model call.
# ---------------------------------------------------------------------------
def _turn(user, conversation, role, text):
    message = ChatMessage(
        user=user, conversation=conversation, role=role, content=[{"type": "text", "text": text}]
    )
    message.save()
    return message


def _ids(user, conversation):
    return [m.id for m in ChatMessage.objects.for_user(user).filter(conversation=conversation)]


def _rendered_thread(html: str) -> str:
    """Just the turns on screen — the page also carries every one of these
    class names in its <style> block and in the JS that mirrors this markup
    for a streamed reply, and neither of those is a control anyone can
    click."""
    return html[html.index('id="as-log"') : html.index('class="as-composer"')]


@override_settings(ANTHROPIC_API_KEY="")
def test_editing_a_message_drops_every_turn_after_it_and_answers_again(signed_in, user):
    conversation = ChatConversation(user=user, title="Goldman plan")
    conversation.save()
    first = _turn(user, conversation, "user", "who should I chase?")
    answer = _turn(user, conversation, "assistant", "Chase Morgan Stanley.")
    second = _turn(user, conversation, "user", "and after that?")
    later = _turn(user, conversation, "assistant", "Then Goldman.")

    response = signed_in.post(
        reverse("assistant:edit_message"), {"message": first.id, "text": "who should I chase at Citi?"}
    )

    assert response.status_code == 302
    surviving = _ids(user, conversation)
    assert answer.id not in surviving
    assert second.id not in surviving
    assert later.id not in surviving
    first.refresh_from_db()
    # Replaced, not appended to: the old question must not still be in there
    # for the model to answer instead of the new one.
    assert first.text == "who should I chase at Citi?"
    assert "who should I chase?" not in first.text
    # The turn really ran: a fresh assistant row sits after the edited one.
    assert [m.role for m in ChatMessage.objects.for_user(user).filter(conversation=conversation)] == [
        "user",
        "assistant",
    ]


@override_settings(ANTHROPIC_API_KEY="")
def test_a_rewind_drops_a_later_row_that_shares_the_edited_messages_timestamp(signed_in, user):
    """The compound-ordering case, forced.

    `ChatMessage.Meta.ordering` is ["created", "id"], and one turn writes
    several rows inside a single request — on any clock the database rounds,
    two of them can land on an identical `created`. So "after" has to be that
    same compound comparison. With `created__gt` alone the row below the
    edited one would survive on screen, and a `tool_result` could be left
    with no `tool_use` to answer, which the Messages API rejects outright.
    """
    conversation = ChatConversation(user=user)
    conversation.save()
    earlier = _turn(user, conversation, "user", "the question before")
    target = _turn(user, conversation, "user", "the one being edited")
    later = _turn(user, conversation, "assistant", "the answer after")
    instant = timezone.now()
    for message in (earlier, target, later):
        ChatMessage.objects.for_user(user).filter(pk=message.pk).update(created=instant)

    signed_in.post(
        reverse("assistant:edit_message"), {"message": target.id, "text": "the one being edited, reworded"}
    )

    surviving = _ids(user, conversation)
    # Same instant, lower id: before it, and untouched.
    assert earlier.id in surviving
    # Same instant, higher id: after it, and gone.
    assert later.id not in surviving


@override_settings(ANTHROPIC_API_KEY="")
def test_editing_the_last_message_in_a_chat_deletes_nothing_else(signed_in, user):
    conversation = ChatConversation(user=user)
    conversation.save()
    kept_question = _turn(user, conversation, "user", "first question")
    kept_answer = _turn(user, conversation, "assistant", "first answer")
    last = _turn(user, conversation, "user", "second question")

    signed_in.post(
        reverse("assistant:edit_message"), {"message": last.id, "text": "second question, reworded"}
    )

    surviving = _ids(user, conversation)
    assert kept_question.id in surviving
    assert kept_answer.id in surviving
    assert last.id in surviving
    last.refresh_from_db()
    assert last.text == "second question, reworded"
    # Still answered again, even though nothing needed deleting.
    assert ChatMessage.objects.for_user(user).filter(conversation=conversation).count() == 4


@override_settings(ANTHROPIC_API_KEY="")
def test_the_advisors_own_words_cannot_be_edited(signed_in, user):
    conversation = ChatConversation(user=user)
    conversation.save()
    question = _turn(user, conversation, "user", "who should I chase?")
    answer = _turn(user, conversation, "assistant", "Chase Morgan Stanley.")

    response = signed_in.post(
        reverse("assistant:edit_message"), {"message": answer.id, "text": "Chase nobody."}
    )

    assert response.status_code == 400
    answer.refresh_from_db()
    assert answer.text == "Chase Morgan Stanley."
    assert _ids(user, conversation) == [question.id, answer.id]


@override_settings(ANTHROPIC_API_KEY="")
def test_a_tool_result_row_cannot_be_edited(signed_in, user):
    """Machinery, not a message. It is user-ROLE, which is exactly why the
    check is on `is_tool_result` too and not on the role alone."""
    conversation = ChatConversation(user=user)
    conversation.save()
    _turn(user, conversation, "user", "who today?")
    machinery = ChatMessage(
        user=user, conversation=conversation, role="user",
        content=[{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}],
    )
    machinery.save()

    response = signed_in.post(
        reverse("assistant:edit_message"), {"message": machinery.id, "text": "something else"}
    )

    assert response.status_code == 400
    assert machinery.id in _ids(user, conversation)


@override_settings(ANTHROPIC_API_KEY="")
def test_an_edit_with_nothing_in_it_is_rejected_and_deletes_nothing(signed_in, user):
    conversation = ChatConversation(user=user)
    conversation.save()
    question = _turn(user, conversation, "user", "who should I chase?")
    answer = _turn(user, conversation, "assistant", "Chase Morgan Stanley.")

    response = signed_in.post(reverse("assistant:edit_message"), {"message": question.id, "text": "   "})

    assert response.status_code == 400
    assert _ids(user, conversation) == [question.id, answer.id]


@override_settings(ANTHROPIC_API_KEY="")
def test_a_rewind_keeps_the_files_attached_to_the_message_it_edits(signed_in, user):
    """The words changed; the resume they attached did not."""
    conversation = ChatConversation(user=user)
    conversation.save()
    message = ChatMessage(
        user=user, conversation=conversation, role="user",
        content=[
            {"type": "document", "_filename": "resume.pdf"},
            {"type": "text", "text": "what do you think?"},
        ],
    )
    message.save()

    signed_in.post(
        reverse("assistant:edit_message"), {"message": message.id, "text": "be honest about this"}
    )

    message.refresh_from_db()
    assert message.attachment_names == ["resume.pdf"]
    assert message.text == "be honest about this"


@override_settings(ANTHROPIC_API_KEY="")
def test_rewinding_the_first_message_retitles_the_conversation(signed_in, user):
    """A title derived from words that no longer exist is a stale title."""
    conversation = ChatConversation(user=user, title="Chasing Goldman")
    conversation.save()
    first = _turn(user, conversation, "user", "how do I get into Goldman?")
    _turn(user, conversation, "assistant", "Start with the alumni.")

    signed_in.post(
        reverse("assistant:edit_message"), {"message": first.id, "text": "how do I get into Citi?"}
    )

    conversation.refresh_from_db()
    assert conversation.title == "how do I get into Citi?"


@override_settings(ANTHROPIC_API_KEY="")
def test_rewinding_a_later_message_leaves_the_title_alone(signed_in, user):
    conversation = ChatConversation(user=user, title="Chasing Goldman")
    conversation.save()
    _turn(user, conversation, "user", "how do I get into Goldman?")
    _turn(user, conversation, "assistant", "Start with the alumni.")
    second = _turn(user, conversation, "user", "which alumni?")

    signed_in.post(
        reverse("assistant:edit_message"), {"message": second.id, "text": "which alumni exactly?"}
    )

    conversation.refresh_from_db()
    assert conversation.title == "Chasing Goldman"


@override_settings(ANTHROPIC_API_KEY="")
def test_editing_another_students_message_404s_and_changes_nothing(client, user):
    other = User.objects.create_user(email="rewind-other@example.com", password="pw12345!")
    theirs = ChatConversation(user=other, title="Not yours")
    theirs.save()
    their_question = _turn(other, theirs, "user", "their private plan")
    their_answer = _turn(other, theirs, "assistant", "their private advice")

    client.force_login(user)
    response = client.post(
        reverse("assistant:edit_message"), {"message": their_question.id, "text": "hijacked"}
    )

    assert response.status_code == 404
    assert _ids(other, theirs) == [their_question.id, their_answer.id]
    their_question.refresh_from_db()
    assert their_question.text == "their private plan"


def test_edit_rejects_a_get(signed_in):
    """It deletes, and it spends a turn. Not something a prefetcher or a back
    button gets to fire."""
    assert signed_in.get(reverse("assistant:edit_message")).status_code == 405


def test_an_anonymous_visitor_cannot_rewind_anything(client):
    response = client.post(reverse("assistant:edit_message"))

    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@override_settings(ANTHROPIC_API_KEY="sk-test")
def test_a_streamed_rewind_answers_over_the_turns_it_deleted(client, monkeypatch):
    """The transport the page actually uses: `stream=1` gets the same SSE
    frames a send does, so the new reply grows in where the old ones were.

    The model is a scripted stub (the same `FakeStreamingClient` the agent's
    own tests use), not a real call — this is about the endpoint's shape, and
    a real turn would be slow, billed and non-deterministic.
    """
    from assistant import agent
    from assistant.tests.test_agent import FakeStreamingClient, _response, _text

    student = User.objects.create_user(email="rewind-stream@example.com", password="pw12345!")
    client.force_login(student)
    conversation = ChatConversation(user=student, title="Where to spend the week")
    conversation.save()
    question = _turn(student, conversation, "user", "where should I spend this week?")
    stale = _turn(student, conversation, "assistant", "Spend it on Goldman.")

    monkeypatch.setattr(
        agent,
        "get_client",
        lambda: FakeStreamingClient([(["Spend it on ", "Citi."], _response([_text("Spend it on Citi.")], "end_turn"))]),
    )

    response = client.post(
        reverse("assistant:edit_message"),
        {"message": question.id, "text": "where should I spend this week, Citi aside?", "stream": "1"},
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "text/event-stream"
    body = b"".join(response.streaming_content).decode()  # the generator only runs once consumed
    assert '"type": "delta"' in body
    assert '"type": "done"' in body
    # The terminal frame names the question it answered, which is what hangs
    # an edit control on a bubble the browser drew itself.
    assert f'"user_message_id": {question.id}' in body

    turns = list(ChatMessage.objects.for_user(student).filter(conversation=conversation))
    assert stale.id not in [t.id for t in turns]
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[0].id == question.id
    assert turns[0].text == "where should I spend this week, Citi aside?"
    assert turns[1].text == "Spend it on Citi."


@override_settings(ANTHROPIC_API_KEY="")
def test_the_thread_offers_copy_on_every_answer_and_edit_on_every_question(signed_in, user):
    """ChatGPT's own habit: a Copy on every reply, not only the ones that
    happen to hold a drafted email. And its opposite number on the student's
    side — the pencil that rewinds."""
    conversation = ChatConversation(user=user)
    conversation.save()
    question = _turn(user, conversation, "user", "who should I chase?")
    _turn(user, conversation, "assistant", "Chase Morgan Stanley.")

    body = signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id])).content.decode()

    # Scoped to the rendered thread: the same class names also appear in the
    # page's own <style> block and in the JS that mirrors this markup for a
    # streamed turn, and neither of those is a control on screen.
    thread = _rendered_thread(body)
    assert thread.count('class="as-msg-btn as-msg-copy"') == 1
    assert thread.count('class="as-msg-edit-form"') == 1
    # The edit control posts the row's own id back.
    assert f'name="message" value="{question.id}"' in thread
    # And says plainly what it is about to destroy.
    assert "Everything after this message is deleted" in thread


@override_settings(ANTHROPIC_API_KEY="")
def test_a_notice_gets_no_copy_button(signed_in, user):
    """Coverage talking about itself is not an answer worth copying."""
    conversation = ChatConversation(user=user)
    conversation.save()
    _turn(user, conversation, "user", "who should I chase?")
    notice = ChatMessage(
        user=user, conversation=conversation, role="assistant", notice=ChatMessage.NOTICE_FAILED,
        content=[{"type": "text", "text": "I couldn't reach the model just then."}],
    )
    notice.save()

    body = signed_in.get(reverse("assistant:chat_conversation", args=[conversation.id])).content.decode()

    assert "couldn't reach the model" in body
    assert 'class="as-msg-btn as-msg-copy"' not in _rendered_thread(body)


@override_settings(ANTHROPIC_API_KEY="")
def test_a_stream_that_ends_badly_still_names_the_message_to_edit(signed_in, user):
    """A composer send draws its own user bubble, so the id that makes it
    editable can only come from the stream's terminal frame. A turn that
    FAILED is the likeliest moment of all to want to reword and try again,
    so the failure frame carries it too, not just the successful one."""
    response = signed_in.post(reverse("assistant:stream"), {"message": "who should I chase?"})

    body = b"".join(response.streaming_content).decode()
    question = ChatMessage.objects.for_user(user).filter(role="user").first()
    assert '"type": "notice"' in body
    assert f'"user_message_id": {question.id}' in body
