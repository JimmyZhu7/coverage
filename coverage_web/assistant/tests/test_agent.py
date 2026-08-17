"""The agent loop, driven by a fake client. Nothing here goes near the network.

`FakeClient` returns a scripted list of responses in the shape the SDK
returns them (`.content` blocks, `.stop_reason`, `.id`) and records every
request it was handed, which is what lets these tests assert on the things
that matter and are otherwise invisible: that the caps are the CODE's, that
tool_use/tool_result pairing survives persistence and replay, that the cached
system prefix stays byte-stable, and that a failure is a message rather than
a 500.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from analytics.models import ProductEvent
from assistant import agent
from assistant.models import ChatConversation, ChatMessage
from crm.models import Contact
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------
class Block:
    """A response content block shaped like the SDK's: attribute access plus
    `model_dump(exclude_none=True)`, which is the exact surface
    `agent._as_dict` reads. Mimicking pydantic here rather than handing the
    loop plain dicts means these tests exercise the conversion that actually
    runs in production."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self, exclude_none=False):
        return {
            k: v for k, v in self.__dict__.items() if not (exclude_none and v is None)
        }


def _text(text):
    return Block(type="text", text=text)


def _tool_use(name, tool_input, block_id="toolu_1"):
    return Block(type="tool_use", id=block_id, name=name, input=tool_input)


def _response(blocks, stop_reason, msg_id="msg_1"):
    return SimpleNamespace(id=msg_id, content=blocks, stop_reason=stop_reason)


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.script:
            raise AssertionError("the loop asked for more rounds than were scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self.messages = FakeMessages(script)

    @property
    def requests(self):
        return self.messages.requests


@pytest.fixture
def user():
    return User.objects.create_user(
        email="student@example.com", password="x", name="Sam", school="HKU",
        class_year=2028, regions=["hk"], tracks=["ibd"],
    )


@pytest.fixture
def conversation(user):
    c = ChatConversation(user=user)
    c.save()
    return c


@pytest.fixture
def contact(user):
    firm = Firm.objects.create(slug="north-bank", name="North Bank")
    c = Contact(user=user, firm=firm, name="Jane Banker")
    c.save()
    return c


def _turns(user, conversation):
    return list(ChatMessage.objects.for_user(user).filter(conversation=conversation))


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def test_a_plain_answer_is_one_round_and_two_persisted_turns(user, conversation):
    client = FakeClient([_response([_text("Chase Morgan Stanley this week.")], "end_turn")])

    result = agent.run_turn(user, conversation, "Where should I spend this week?", client=client)

    assert result.ok
    assert result.rounds == 1
    assert result.tool_calls == []

    turns = _turns(user, conversation)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[-1].text == "Chase Morgan Stanley this week."
    assert result.reply.pk == turns[-1].pk


def test_a_tool_round_executes_the_tool_and_persists_the_pairing(user, conversation, contact):
    client = FakeClient(
        [
            _response([_tool_use("search_contacts", {"query": "jane"})], "tool_use"),
            _response([_text("Jane is cold — send the first note today.")], "end_turn"),
        ]
    )

    result = agent.run_turn(user, conversation, "What's going on with Jane?", client=client)

    assert result.ok
    assert result.rounds == 2
    assert result.tool_calls == ["search_contacts"]

    turns = _turns(user, conversation)
    assert [t.role for t in turns] == ["user", "assistant", "user", "assistant"]

    tool_use_block = turns[1].blocks()[0]
    tool_result_block = turns[2].blocks()[0]
    assert tool_use_block["type"] == "tool_use"
    assert tool_result_block["type"] == "tool_result"
    # The pairing the Messages API requires, surviving a round-trip through
    # the JSONField — this is why content is stored as blocks, not prose.
    assert tool_result_block["tool_use_id"] == tool_use_block["id"]
    assert tool_result_block["is_error"] is False
    assert contact.name in tool_result_block["content"]


def test_the_second_request_replays_the_whole_pairing_verbatim(user, conversation, contact):
    client = FakeClient(
        [
            _response([_tool_use("get_today_queue", {})], "tool_use"),
            _response([_text("Two calls to make.")], "end_turn"),
        ]
    )

    agent.run_turn(user, conversation, "What's on today?", client=client)

    second = client.requests[1]["messages"]
    roles = [m["role"] for m in second]
    assert roles == ["user", "assistant", "user"]
    assert second[1]["content"][0]["type"] == "tool_use"
    assert second[2]["content"][0]["type"] == "tool_result"


def test_a_failing_tool_comes_back_as_an_error_result_and_the_turn_continues(user, conversation):
    client = FakeClient(
        [
            _response([_tool_use("get_contact", {"contact_id": 999999})], "tool_use"),
            _response([_text("I can't find that person in your network.")], "end_turn"),
        ]
    )

    result = agent.run_turn(user, conversation, "Tell me about contact 999999", client=client)

    assert result.ok
    tool_result = _turns(user, conversation)[2].blocks()[0]
    assert tool_result["is_error"] is True
    assert "error" in json.loads(tool_result["content"])


# ---------------------------------------------------------------------------
# Caps — all four enforced in code, none of them trusted to the model
# ---------------------------------------------------------------------------
def test_the_round_cap_stops_a_model_that_never_stops_calling_tools(user, conversation):
    client = FakeClient(
        [_response([_tool_use("get_today_queue", {})], "tool_use") for _ in range(agent.MAX_ROUNDS)]
    )

    result = agent.run_turn(user, conversation, "go", client=client)

    assert not result.ok
    assert result.reason == "failed"
    assert result.rounds == agent.MAX_ROUNDS
    assert len(client.requests) == agent.MAX_ROUNDS
    assert result.reply.notice == ChatMessage.NOTICE_FAILED
    assert "narrower" in result.reply.text


def test_the_conversation_tool_call_cap_refuses_further_lookups(user, conversation, contact):
    """Past the cap the loop does not execute the tool; it answers the model
    with an error result telling it to wrap up, so the student still gets a
    reply built on what was already fetched."""
    # Burn the budget with previously-persisted assistant turns.
    for i in range(agent.MAX_TOOL_CALLS):
        m = ChatMessage(
            user=user,
            conversation=conversation,
            role=ChatMessage.ROLE_ASSISTANT,
            content=[{"type": "tool_use", "id": f"toolu_{i}", "name": "get_today_queue", "input": {}}],
        )
        m.save()
        r = ChatMessage(
            user=user,
            conversation=conversation,
            role=ChatMessage.ROLE_USER,
            content=[{"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": "{}"}],
        )
        r.save()

    client = FakeClient(
        [
            _response([_tool_use("search_contacts", {"query": "jane"})], "tool_use"),
            _response([_text("Going on what I already have.")], "end_turn"),
        ]
    )

    result = agent.run_turn(user, conversation, "one more thing", client=client)

    assert result.ok
    assert result.tool_calls == []  # nothing executed
    blocked = _turns(user, conversation)[-2].blocks()[0]
    assert blocked["is_error"] is True
    assert "tool-call limit" in blocked["content"]


@override_settings(ASSISTANT_DAILY_MESSAGE_CAP=2)
def test_the_daily_message_cap_is_a_plain_notice_not_an_error(user, conversation):
    script = [_response([_text("ok")], "end_turn") for _ in range(2)]
    client = FakeClient(script)

    assert agent.run_turn(user, conversation, "one", client=client).ok
    assert agent.run_turn(user, conversation, "two", client=client).ok

    third = agent.run_turn(user, conversation, "three", client=FakeClient([]))

    assert not third.ok
    assert third.reason == "capped"
    assert third.reply.notice == ChatMessage.NOTICE_CAPPED
    assert "2 messages today" in third.reply.text
    # The student's third question is still in the thread — they can see what
    # they asked when the cap resets.
    assert _turns(user, conversation)[-2].text == "three"


def test_an_api_failure_is_a_message_in_the_thread_never_a_500(user, conversation):
    client = FakeClient([RuntimeError("connection reset")])

    result = agent.run_turn(user, conversation, "hello", client=client)

    assert not result.ok
    assert result.reason == "failed"
    assert result.reply.notice == ChatMessage.NOTICE_FAILED
    assert _turns(user, conversation)[0].text == "hello"


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------
def test_the_system_prefix_is_cacheable_and_carries_nothing_per_user(user, conversation):
    client = FakeClient([_response([_text("ok")], "end_turn")])

    agent.run_turn(user, conversation, "hi", client=client)

    request = client.requests[0]
    system = request["system"]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    blob = json.dumps(system)
    # Byte-stability is the whole point of the breakpoint: a name or a date in
    # here would invalidate every cached prefix.
    for leak in ("Sam", "HKU", "2028", str(timezone.localdate().year)):
        assert leak not in blob
    assert request["tools"] == __import__("assistant.tools", fromlist=["x"]).TOOL_SCHEMAS


def test_the_preamble_rides_on_the_first_user_turn(user, conversation):
    client = FakeClient([_response([_text("ok")], "end_turn")])

    agent.run_turn(user, conversation, "hi", client=client)

    first_turn = client.requests[0]["messages"][0]
    preamble = first_turn["content"][0]["text"]
    assert "Today is" in preamble
    assert "Sam" in preamble
    assert "HKU" in preamble
    assert "2028" in preamble
    assert first_turn["content"][1]["text"] == "hi"


def test_replay_never_opens_on_an_orphaned_tool_result(user, conversation):
    """Slicing the last N turns can cut between a tool_use and its answer; the
    window has to be trimmed forward or the API rejects the request."""
    m = ChatMessage(
        user=user, conversation=conversation, role=ChatMessage.ROLE_ASSISTANT,
        content=[{"type": "tool_use", "id": "toolu_x", "name": "get_today_queue", "input": {}}],
    )
    m.save()
    r = ChatMessage(
        user=user, conversation=conversation, role=ChatMessage.ROLE_USER,
        content=[{"type": "tool_result", "tool_use_id": "toolu_x", "content": "{}"}],
    )
    r.save()

    client = FakeClient([_response([_text("ok")], "end_turn")])
    agent.run_turn(user, conversation, "and now?", client=client)

    sent = client.requests[0]["messages"]
    assert sent[0]["role"] == "user"
    assert all(b.get("type") != "tool_result" for b in sent[0]["content"])


def test_notices_are_shown_but_never_replayed_to_the_model(user, conversation):
    agent.run_turn(user, conversation, "first", client=FakeClient([RuntimeError("down")]))

    client = FakeClient([_response([_text("back now")], "end_turn")])
    agent.run_turn(user, conversation, "second", client=client)

    replayed = json.dumps(client.requests[0]["messages"])
    assert "couldn't reach the model" not in replayed
    # ...while the thread still holds it for the student.
    assert any(t.notice == ChatMessage.NOTICE_FAILED for t in _turns(user, conversation))


def test_replay_is_windowed(user, conversation):
    for i in range(agent.REPLAY_TURNS + 10):
        m = ChatMessage(
            user=user,
            conversation=conversation,
            role=ChatMessage.ROLE_USER if i % 2 == 0 else ChatMessage.ROLE_ASSISTANT,
            content=[{"type": "text", "text": f"turn {i}"}],
        )
        m.save()

    client = FakeClient([_response([_text("ok")], "end_turn")])
    agent.run_turn(user, conversation, "latest", client=client)

    assert len(client.requests[0]["messages"]) <= agent.REPLAY_TURNS


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------
def test_events_are_recorded_and_never_carry_message_text(user, conversation, contact):
    client = FakeClient(
        [
            _response([_tool_use("search_contacts", {"query": "jane"})], "tool_use"),
            _response(
                [_tool_use("track_opportunity", {"opportunity_id": 1, "status": "saved"}, "toolu_2")],
                "tool_use",
            ),
            _response([_text("done")], "end_turn"),
        ]
    )

    agent.run_turn(user, conversation, "secret strategy question", client=client)

    events = list(ProductEvent.objects.for_user(user))
    names = {e.event for e in events}
    assert "assistant_message_sent" in names
    assert "assistant_tool_call" in names
    for event in events:
        assert "secret strategy" not in json.dumps(event.props)

    calls = [e for e in events if e.event == "assistant_tool_call"]
    assert {e.props["tool"] for e in calls} == {"search_contacts", "track_opportunity"}
    # The failed write (opportunity 1 doesn't exist) records the attempt as
    # not-ok and records no assistant_write.
    assert any(e.props["ok"] is False for e in calls)
    assert "assistant_write" not in names


def test_a_successful_write_records_assistant_write(user, conversation):
    firm = Firm.objects.create(slug="w", name="W Bank")
    from directory.models import Opportunity

    opp = Opportunity.objects.create(
        firm=firm, title="Analyst", bucket="internship", status="open",
        url="https://w.example/1",
    )
    client = FakeClient(
        [
            _response(
                [_tool_use("track_opportunity", {"opportunity_id": opp.id, "status": "saved"})],
                "tool_use",
            ),
            _response([_text("Saved.")], "end_turn"),
        ]
    )

    agent.run_turn(user, conversation, "save that one", client=client)

    writes = ProductEvent.objects.for_user(user).filter(event="assistant_write")
    assert [w.props["tool"] for w in writes] == ["track_opportunity"]


def test_an_empty_message_does_nothing(user, conversation):
    result = agent.run_turn(user, conversation, "   ", client=FakeClient([]))

    assert not result.ok
    assert _turns(user, conversation) == []
