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


def _response(blocks, stop_reason, msg_id="msg_1", usage=None):
    return SimpleNamespace(id=msg_id, content=blocks, stop_reason=stop_reason, usage=usage)


def _usage(input_tokens=100, output_tokens=50):
    return SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )


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


class FakeStreamContext:
    """What `client.messages.stream(...)` returns, shaped for `with ... as
    stream:`. `text_stream` yields the scripted chunks; `get_final_message()`
    returns the same kind of object `.create()` would have — stream_turn
    reads both, same as the real SDK's stream manager."""

    def __init__(self, chunks, final):
        self.chunks = chunks
        self.final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    @property
    def text_stream(self):
        return iter(self.chunks)

    def get_final_message(self):
        return self.final


class FakeStreamingMessages:
    def __init__(self, script, title_script=None):
        # Each scripted round is (chunks: list[str], final: response) or an
        # Exception to raise instead of streaming anything.
        self.script = list(script)
        # `.create()` is only ever called for one thing on this fake:
        # agent._ai_title's post-turn retitle. None means "nothing
        # scripted" — every real test that doesn't care about titling
        # leaves this unset, and the resulting AssertionError is exactly
        # what _ai_title's own try/except is built to swallow.
        self.title_script = list(title_script) if title_script is not None else None
        self.requests = []
        self.title_requests = []

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        if not self.script:
            raise AssertionError("the loop asked for more rounds than were scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        chunks, final = item
        return FakeStreamContext(chunks, final)

    def create(self, **kwargs):
        self.title_requests.append(kwargs)
        if not self.title_script:
            raise AssertionError("no title response was scripted")
        item = self.title_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeStreamingClient:
    def __init__(self, script, title_script=None):
        self.messages = FakeStreamingMessages(script, title_script)

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
    reply built on what was already fetched.

    Burned 5 tool calls per round (5 rounds), not one-per-round: the cap is
    now rolling over the SAME window replay uses (agent._replayable /
    REPLAY_TURNS), so a one-call-per-round burn would need 50 rows for 25
    calls — mostly outside a 30-row window, and the cap would never fire at
    all. Bundling several tool calls into one round is also just what a
    real multi-part question looks like, not a contrived shape."""
    ChatMessage(
        user=user, conversation=conversation, role=ChatMessage.ROLE_USER,
        content=[{"type": "text", "text": "earlier question"}],
    ).save()
    for round_i in range(5):
        ChatMessage(
            user=user,
            conversation=conversation,
            role=ChatMessage.ROLE_ASSISTANT,
            content=[
                {"type": "tool_use", "id": f"toolu_{round_i}_{j}", "name": "get_today_queue", "input": {}}
                for j in range(5)
            ],
        ).save()
        ChatMessage(
            user=user,
            conversation=conversation,
            role=ChatMessage.ROLE_USER,
            content=[
                {"type": "tool_result", "tool_use_id": f"toolu_{round_i}_{j}", "content": "{}"}
                for j in range(5)
            ],
        ).save()

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


def test_the_tool_call_cap_is_rolling_not_lifetime(user, conversation, contact):
    """The whole point of the change: a folder is a standing invitation to
    keep one conversation alive for weeks, and a LIFETIME cap made that a
    trap — once MAX_TOOL_CALLS accumulated, ever, the advisor permanently
    lost lookup ability, with "start a new chat" (which defeats the
    folder) as the only way out. Old tool calls that have aged out of the
    replay window must not count against a NEW question."""
    ChatMessage(
        user=user, conversation=conversation, role=ChatMessage.ROLE_USER,
        content=[{"type": "text", "text": "an old question, weeks ago"}],
    ).save()
    for round_i in range(5):
        ChatMessage(
            user=user,
            conversation=conversation,
            role=ChatMessage.ROLE_ASSISTANT,
            content=[
                {"type": "tool_use", "id": f"toolu_old_{round_i}_{j}", "name": "get_today_queue", "input": {}}
                for j in range(5)
            ],
        ).save()
        ChatMessage(
            user=user,
            conversation=conversation,
            role=ChatMessage.ROLE_USER,
            content=[
                {"type": "tool_result", "tool_use_id": f"toolu_old_{round_i}_{j}", "content": "{}"}
                for j in range(5)
            ],
        ).save()
    # 30 plain-text filler turns (REPLAY_TURNS) push every one of the rows
    # above outside the window _tool_calls_used and _api_messages both use.
    for i in range(agent.REPLAY_TURNS // 2):
        ChatMessage(
            user=user, conversation=conversation, role=ChatMessage.ROLE_USER,
            content=[{"type": "text", "text": f"filler question {i}"}],
        ).save()
        ChatMessage(
            user=user, conversation=conversation, role=ChatMessage.ROLE_ASSISTANT,
            content=[{"type": "text", "text": f"filler answer {i}"}],
        ).save()

    client = FakeClient(
        [
            _response([_tool_use("search_contacts", {"query": "jane"})], "tool_use"),
            _response([_text("Found her.")], "end_turn"),
        ]
    )

    result = agent.run_turn(user, conversation, "a new question, much later", client=client)

    assert result.ok
    assert result.tool_calls == ["search_contacts"]  # actually executed — the old burn did not count


_TWO_A_DAY = {
    "free": {"model": "test-free-model", "daily_cap": 2},
    "pro": {"model": "test-pro-model", "daily_cap": 5},
}

# Credit-system equivalent of _TWO_A_DAY (docs/credit-system-plan.md): a
# 1-credit message cost and a generous daily burst means the MONTHLY grant
# is what actually caps these tests at "2" / "5" messages, exactly mirroring
# the shape the old flat daily cap tested.
_TWO_CREDITS = {
    "free": {"monthly_grant": 2, "message_cost": 1, "daily_burst": 10},
    "pro": {"monthly_grant": 5, "message_cost": 1, "daily_burst": 10},
}


@override_settings(ASSISTANT_PLANS=_TWO_A_DAY, CREDIT_PLANS=_TWO_CREDITS)
def test_zero_credits_is_a_plain_notice_not_an_error(user, conversation):
    # 3, not 2: the first of the two real turns also triggers one retitle
    # call (see the "AI-generated titles" section below) — same client,
    # same flat script, one more item to get through it.
    script = [_response([_text("ok")], "end_turn") for _ in range(3)]
    client = FakeClient(script)

    assert agent.run_turn(user, conversation, "one", client=client).ok
    assert agent.run_turn(user, conversation, "two", client=client).ok

    third = agent.run_turn(user, conversation, "three", client=FakeClient([]))

    assert not third.ok
    assert third.reason == "capped"
    assert third.reply.notice == ChatMessage.NOTICE_CAPPED
    assert "last of this month's credits" in third.reply.text
    # A Free student is told what Pro would change; the notice names the plan.
    assert "Free plan" in third.reply.text
    assert "three times the credits" in third.reply.text
    # The student's third question is still in the thread — they can see what
    # they asked when the cap resets.
    assert _turns(user, conversation)[-2].text == "three"


@override_settings(ASSISTANT_PLANS=_TWO_A_DAY, CREDIT_PLANS=_TWO_CREDITS)
def test_the_daily_burst_guard_blocks_with_a_different_notice_than_zero_credits(user, conversation):
    """A burst-guard trip is NOT the same situation as a genuinely empty
    monthly pool — the student still has credits, just not today's worth —
    so it must not say "last of this month's credits" (that would be a lie
    the moment the calendar rolls over mid-conversation)."""
    burst_settings = {
        "free": {"monthly_grant": 100, "message_cost": 1, "daily_burst": 1},
        "pro": {"monthly_grant": 100, "message_cost": 1, "daily_burst": 1},
    }
    with override_settings(CREDIT_PLANS=burst_settings):
        script = [_response([_text("ok")], "end_turn") for _ in range(2)]
        client = FakeClient(script)
        assert agent.run_turn(user, conversation, "one", client=client).ok

        second = agent.run_turn(user, conversation, "two", client=FakeClient([]))

        assert not second.ok
        assert second.reason == "capped"
        assert "safety net" in second.reply.text
        assert "last of this month's credits" not in second.reply.text


# ---------------------------------------------------------------------------
# Plans: Free and Pro differ in model and cap, and nothing else
# ---------------------------------------------------------------------------
@override_settings(ASSISTANT_PLANS=_TWO_A_DAY)
def test_a_free_student_is_answered_by_the_free_tier_model(user, conversation):
    client = FakeClient([_response([_text("ok")], "end_turn")])

    agent.run_turn(user, conversation, "hi", client=client)

    assert client.requests[0]["model"] == "test-free-model"


@override_settings(ASSISTANT_PLANS=_TWO_A_DAY)
def test_a_pro_student_is_answered_by_the_pro_tier_model(user, conversation):
    user.plan = User.PLAN_PRO
    user.save(update_fields=["plan"])
    client = FakeClient([_response([_text("ok")], "end_turn")])

    agent.run_turn(user, conversation, "hi", client=client)

    assert client.requests[0]["model"] == "test-pro-model"


@override_settings(ASSISTANT_PLANS=_TWO_A_DAY, CREDIT_PLANS=_TWO_CREDITS)
def test_a_pro_student_gets_the_pro_grant_and_no_upsell_line(user, conversation):
    user.plan = User.PLAN_PRO
    user.save(update_fields=["plan"])
    # 6, not 5: q0 is the first message and also triggers one retitle call.
    client = FakeClient([_response([_text("ok")], "end_turn") for _ in range(6)])
    for i in range(5):
        assert agent.run_turn(user, conversation, f"q{i}", client=client).ok

    sixth = agent.run_turn(user, conversation, "q5", client=FakeClient([]))

    assert sixth.reason == "capped"
    assert "last of this month's credits" in sixth.reply.text
    assert "Pro plan" in sixth.reply.text
    assert "three times the credits" not in sixth.reply.text


@override_settings(ASSISTANT_PLANS=_TWO_A_DAY)
def test_an_unknown_plan_value_degrades_to_free_rather_than_erroring(user, conversation):
    """The field is written by admin now and a billing webhook later; a typo
    in either must cost the student quality, not their advisor."""
    user.plan = "platinum"
    user.save(update_fields=["plan"])
    client = FakeClient([_response([_text("ok")], "end_turn")])

    result = agent.run_turn(user, conversation, "hi", client=client)

    assert result.ok
    assert client.requests[0]["model"] == "test-free-model"


def test_the_shipped_defaults_are_haiku_for_free_and_sonnet_for_pro():
    """Pin the actual product decision, independent of the fake plans above."""
    from assistant import plans

    free = plans.limits_for(SimpleNamespace(plan="free"))
    pro = plans.limits_for(SimpleNamespace(plan="pro"))
    assert free.model.startswith("claude-haiku")
    assert pro.model.startswith("claude-sonnet")
    assert free.daily_cap < pro.daily_cap
    # docs/credit-system-plan.md §1: a Sonnet (Pro) message is charged the
    # honest 3x price ratio over a Haiku (Free) one.
    assert free.message_cost == 1
    assert pro.message_cost == 3


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
    assert system[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
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


def test_remembered_facts_ride_the_preamble_regardless_of_which_conversation(user):
    """The whole point of AdvisorMemory: a fact saved in one conversation
    must be visible in a completely different, brand-new one — the one
    thing per-conversation threads can't do on their own."""
    from assistant.models import AdvisorMemory, ChatConversation

    AdvisorMemory(user=user, text="Ruled out PE roles.").save()
    AdvisorMemory(user=user, text="Needs sponsorship in the US.").save()
    other_conversation = ChatConversation(user=user)
    other_conversation.save()

    client = FakeClient([_response([_text("ok")], "end_turn")])
    agent.run_turn(user, other_conversation, "hi, first time in this chat", client=client)

    preamble = client.requests[0]["messages"][0]["content"][0]["text"]
    assert "Ruled out PE roles." in preamble
    assert "Needs sponsorship in the US." in preamble


def test_with_no_remembered_facts_the_preamble_says_nothing_about_memory(user, conversation):
    client = FakeClient([_response([_text("ok")], "end_turn")])

    agent.run_turn(user, conversation, "hi", client=client)

    preamble = client.requests[0]["messages"][0]["content"][0]["text"]
    assert "Remembered" not in preamble


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


# ---------------------------------------------------------------------------
# Streaming: the same contract as run_turn, as events instead of one result
# ---------------------------------------------------------------------------
def test_a_plain_streamed_answer_yields_its_chunks_then_done(user, conversation):
    client = FakeStreamingClient(
        [(["Chase ", "Morgan ", "Stanley."], _response([_text("Chase Morgan Stanley.")], "end_turn"))]
    )

    events = list(agent.stream_turn(user, conversation, "Where should I spend this week?", client=client))

    assert [e["type"] for e in events] == ["delta", "delta", "delta", "done"]
    assert "".join(e["text"] for e in events[:-1]) == "Chase Morgan Stanley."
    assert events[-1]["tools"] == []
    # Persisted exactly like the non-streaming loop: one user turn, one
    # assistant turn — a replayed conversation cannot tell how it was sent.
    turns = _turns(user, conversation)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[-1].text == "Chase Morgan Stanley."


def test_a_streamed_tool_round_announces_the_lookup_before_running_it(user, conversation, contact):
    client = FakeStreamingClient(
        [
            ([], _response([_tool_use("search_contacts", {"query": "jane"})], "tool_use")),
            (["Jane is cold."], _response([_text("Jane is cold.")], "end_turn")),
        ]
    )

    events = list(agent.stream_turn(user, conversation, "What's going on with Jane?", client=client))

    assert events[0] == {"type": "tool", "label": "your contacts"}
    turns = _turns(user, conversation)
    # `message_id` is the stored reply's own pk — the view hangs draft-card
    # metadata off this frame with it (assistant.views._draft_segments).
    assert events[-1] == {"type": "done", "tools": ["your contacts"], "message_id": turns[-1].id}
    assert [t.role for t in turns] == ["user", "assistant", "user", "assistant"]


def test_a_streamed_write_is_never_announced_as_a_reading_hint(user, conversation):
    firm = Firm.objects.create(slug="stream-w", name="Stream Bank")
    from directory.models import Opportunity

    opp = Opportunity.objects.create(
        firm=firm, title="Analyst", bucket="internship", status="open",
        url="https://stream-w.example/1",
    )
    client = FakeStreamingClient(
        [
            (
                [],
                _response(
                    [_tool_use("track_opportunity", {"opportunity_id": opp.id, "status": "saved"})],
                    "tool_use",
                ),
            ),
            (["Saved."], _response([_text("Saved.")], "end_turn")),
        ]
    )

    events = list(agent.stream_turn(user, conversation, "save that one", client=client))

    assert not any(e["type"] == "tool" for e in events)
    assert events[-1]["tools"] == ["saved a role"]


@override_settings(ANTHROPIC_API_KEY="")
def test_streaming_with_no_api_key_yields_one_unconfigured_notice(user, conversation):
    events = list(agent.stream_turn(user, conversation, "hi"))

    assert events == [
        {
            "type": "notice",
            "kind": "unconfigured",
            "text": "Talk to Coverage isn't switched on yet — it needs an Anthropic API "
            "key set on the server. Everything else in Coverage works as normal.",
        }
    ]
    assert _turns(user, conversation)[-1].notice == ChatMessage.NOTICE_UNCONFIGURED


@override_settings(CREDIT_PLANS={
    "free": {"monthly_grant": 1, "message_cost": 1, "daily_burst": 10},
    "pro": {"monthly_grant": 5, "message_cost": 1, "daily_burst": 10},
})
def test_streaming_zero_credits_is_one_terminal_notice_event(user, conversation):
    first = list(agent.stream_turn(user, conversation, "one", client=FakeStreamingClient([([], _response([_text("ok")], "end_turn"))])))
    assert first[-1]["type"] == "done"

    second = list(agent.stream_turn(user, conversation, "two", client=FakeStreamingClient([])))

    assert len(second) == 1
    assert second[0]["type"] == "notice"
    assert second[0]["kind"] == "capped"
    assert "last of this month's credits" in second[0]["text"]


def test_a_streaming_api_failure_yields_one_failed_notice_never_raises(user, conversation):
    client = FakeStreamingClient([RuntimeError("connection reset")])

    events = list(agent.stream_turn(user, conversation, "hello", client=client))

    assert events == [
        {
            "type": "notice",
            "kind": "failed",
            "text": "I couldn't reach the model just then. Try that again in a moment.",
        }
    ]
    assert _turns(user, conversation)[0].text == "hello"


def test_streaming_the_round_cap_still_stops_a_model_that_never_stops_calling_tools(user, conversation):
    script = [([], _response([_tool_use("get_today_queue", {})], "tool_use", msg_id=f"m{i}")) for i in range(agent.MAX_ROUNDS)]
    client = FakeStreamingClient(script)

    events = list(agent.stream_turn(user, conversation, "keep going", client=client))

    assert events[-1]["type"] == "notice"
    assert events[-1]["kind"] == "failed"
    assert "went round in circles" in events[-1]["text"]


def test_streaming_an_empty_message_yields_nothing(user, conversation):
    events = list(agent.stream_turn(user, conversation, "   ", client=FakeStreamingClient([])))

    assert events == []
    assert _turns(user, conversation) == []


def test_a_streamed_conversation_replays_correctly_for_a_later_non_streamed_turn(user, conversation, contact):
    """The whole point of persisting identically: a conversation doesn't
    have to pick one transport for its whole life."""
    stream_client = FakeStreamingClient(
        [([], _response([_tool_use("search_contacts", {"query": "jane"})], "tool_use")),
         (["Cold."], _response([_text("Cold.")], "end_turn"))]
    )
    list(agent.stream_turn(user, conversation, "status on Jane?", client=stream_client))

    plain_client = FakeClient([_response([_text("Two calls to make.")], "end_turn")])
    agent.run_turn(user, conversation, "and today?", client=plain_client)

    replayed = plain_client.requests[0]["messages"]
    roles = [m["role"] for m in replayed]
    assert roles == ["user", "assistant", "user", "assistant", "user"]


# ---------------------------------------------------------------------------
# AI-generated titles — the first-message-only retitle, both transports
# ---------------------------------------------------------------------------
def test_a_successful_first_turn_is_retitled_by_the_model(user, conversation):
    client = FakeClient(
        [
            _response([_text("Chase Morgan Stanley this week.")], "end_turn"),
            _response([_text("Where to spend the week")], "end_turn"),  # the title call
        ]
    )

    agent.run_turn(user, conversation, "Where should I spend this week?", client=client)

    conversation.refresh_from_db()
    assert conversation.title == "Where to spend the week"


def test_a_title_wrapped_in_markdown_bold_is_unwrapped(user, conversation):
    """Measured live: Haiku wraps the whole title in **bold** often enough
    that the sidebar showed "**Identifying Coverage Gaps**" literally."""
    client = FakeClient(
        [
            _response([_text("Some answer.")], "end_turn"),
            _response([_text("**Identifying Coverage Gaps**")], "end_turn"),
        ]
    )

    agent.run_turn(user, conversation, "Where am I thinnest?", client=client)

    conversation.refresh_from_db()
    assert conversation.title == "Identifying Coverage Gaps"


def test_the_title_prompt_forbids_naming_the_speakers(user, conversation):
    """Measured live: with the excerpt labelled "Student:"/"Advisor:", Haiku
    described the labelled exchange rather than naming its subject — two
    sidebar rows read "Student asks about ...". The prompt has to ban that
    outright AND stop handing the model speaker nouns to copy."""
    client = FakeClient(
        [
            _response([_text("Some answer.")], "end_turn"),
            _response([_text("Sponsorship rules for interns")], "end_turn"),
        ]
    )

    agent.run_turn(user, conversation, "What do you see in this?", client=client)

    prompt = client.requests[1]["messages"][0]["content"]
    assert "Never mention or refer to the people talking" in prompt
    assert "'Student asks about X'" in prompt  # spelled out as the thing NOT to do
    # The transcript markers are neutral: no speaker label for a title to echo.
    assert "Student:" not in prompt
    assert "Advisor:" not in prompt
    assert "First message: What do you see in this?" in prompt
    assert "Reply: Some answer." in prompt
    # A contentless opener ("What do you see in this?" — an image with no
    # question) got echoed verbatim as a title; the reply is where the
    # substance lives in that case.
    assert "take the title from what the reply is actually about" in prompt
    # And the constraints that were already earning their keep.
    assert "4-6 word" in prompt
    assert "no trailing punctuation" in prompt
    assert "not a generic label" in prompt

    conversation.refresh_from_db()
    assert conversation.title == "Sponsorship rules for interns"


def test_a_second_turn_is_never_retitled(user, conversation):
    conversation.title = "Already named"
    conversation.save()
    client = FakeClient(
        [
            _response([_text("Sure.")], "end_turn"),
            _response([_text("Should never be read")], "end_turn"),
        ]
    )

    agent.run_turn(user, conversation, "another question", client=client)

    conversation.refresh_from_db()
    assert conversation.title == "Already named"
    # Only ONE request went out — the answer. The extra scripted response
    # for a title call was never touched.
    assert len(client.requests) == 1


def test_a_failed_title_call_leaves_the_truncated_fallback_in_place(user, conversation):
    client = FakeClient(
        [
            _response([_text("Sure.")], "end_turn"),
            RuntimeError("network hiccup"),
        ]
    )

    agent.run_turn(user, conversation, "Where should I spend this week?", client=client)

    conversation.refresh_from_db()
    assert conversation.title == "Where should I spend this week?"


def test_titling_never_happens_after_an_unconfigured_or_capped_or_failed_turn(user, conversation):
    """The retitle call sits right at the ok=True return — a turn that
    never gets that far (capped, unconfigured, a mid-loop API failure)
    must never spend a second call trying to name a conversation whose
    real answer never arrived."""
    client = FakeClient([RuntimeError("down")])

    agent.run_turn(user, conversation, "hello", client=client)

    conversation.refresh_from_db()
    assert conversation.title == "hello"  # the plain truncated fallback, nothing fancier
    assert len(client.requests) == 1  # just the one failed attempt at an answer


def test_a_successful_streamed_first_turn_is_also_retitled(user, conversation):
    client = FakeStreamingClient(
        [(["Chase Morgan Stanley."], _response([_text("Chase Morgan Stanley.")], "end_turn"))],
        title_script=[_response([_text("Where to spend the week")], "end_turn")],
    )

    list(agent.stream_turn(user, conversation, "Where should I spend this week?", client=client))

    conversation.refresh_from_db()
    assert conversation.title == "Where to spend the week"


def test_a_streamed_second_turn_is_never_retitled(user, conversation):
    conversation.title = "Already named"
    conversation.save()
    client = FakeStreamingClient(
        [(["ok"], _response([_text("ok")], "end_turn"))],
        title_script=[_response([_text("Should never be read")], "end_turn")],
    )

    list(agent.stream_turn(user, conversation, "another question", client=client))

    conversation.refresh_from_db()
    assert conversation.title == "Already named"
    assert client.messages.title_requests == []


# ---------------------------------------------------------------------------
# Token-usage logging
# ---------------------------------------------------------------------------
def test_a_successful_turn_logs_its_token_usage(user, conversation):
    client = FakeClient([_response([_text("ok")], "end_turn", usage=_usage(input_tokens=321, output_tokens=45))])

    agent.run_turn(user, conversation, "hi", client=client)

    events = list(ProductEvent.objects.for_user(user).filter(event="assistant_usage"))
    assert len(events) == 1
    assert events[0].props["input_tokens"] == 321
    assert events[0].props["output_tokens"] == 45
    assert "model" in events[0].props


def test_a_response_with_no_usage_attribute_logs_nothing_and_does_not_error(user, conversation):
    """The FakeClient in these tests, and real SDK responses shaped slightly
    differently than expected, must never make logging itself the reason a
    turn fails."""
    client = FakeClient([_response([_text("ok")], "end_turn")])  # usage=None by default

    result = agent.run_turn(user, conversation, "hi", client=client)

    assert result.ok
    assert ProductEvent.objects.for_user(user).filter(event="assistant_usage").count() == 0


def test_a_streamed_turn_also_logs_usage(user, conversation):
    client = FakeStreamingClient(
        [(["ok"], _response([_text("ok")], "end_turn", usage=_usage(input_tokens=200, output_tokens=30)))]
    )

    list(agent.stream_turn(user, conversation, "hi", client=client))

    events = list(ProductEvent.objects.for_user(user).filter(event="assistant_usage"))
    assert len(events) == 1
    assert events[0].props["output_tokens"] == 30


# ---------------------------------------------------------------------------
# A failed turn must not spend the day's quota
# ---------------------------------------------------------------------------
def test_a_turn_that_never_reaches_the_model_does_not_count_against_the_daily_cap(user, conversation):
    """Before this fix, assistant_message_sent fired BEFORE the API call —
    a network hiccup on the very first round still burned one of the
    day's messages. At Free's low cap that reads as being charged for an
    error."""
    client = FakeClient([RuntimeError("connection reset")])

    result = agent.run_turn(user, conversation, "hello", client=client)

    assert not result.ok
    assert result.reason == "failed"
    assert agent.messages_sent_today(user) == 0


def test_a_successful_turn_does_count_against_the_daily_cap(user, conversation):
    client = FakeClient([_response([_text("ok")], "end_turn")])

    agent.run_turn(user, conversation, "hello", client=client)

    assert agent.messages_sent_today(user) == 1


def test_a_streamed_turn_that_never_reaches_the_model_does_not_count_against_the_daily_cap(user, conversation):
    client = FakeStreamingClient([RuntimeError("connection reset")])

    list(agent.stream_turn(user, conversation, "hello", client=client))

    assert agent.messages_sent_today(user) == 0


# ---------------------------------------------------------------------------
# The same fairness rule, on the credit ledger (docs/credit-system-plan.md
# §5: "the debit goes exactly where `record_event("assistant_message_sent")`
# already fires — after round 0 returns successfully")
# ---------------------------------------------------------------------------
def _balance(user):
    from billing import credits as billing_credits

    return billing_credits.balance(user)


def test_a_failed_turn_never_debits_credits(user, conversation):
    before = _balance(user)
    client = FakeClient([RuntimeError("connection reset")])

    result = agent.run_turn(user, conversation, "hello", client=client)

    assert not result.ok
    assert _balance(user) == before


def test_a_successful_turn_debits_exactly_the_plans_message_cost(user, conversation):
    before = _balance(user)
    client = FakeClient([_response([_text("ok")], "end_turn")])

    agent.run_turn(user, conversation, "hello", client=client)

    from assistant import plans

    cost = plans.limits_for(user).message_cost
    assert _balance(user) == before - cost


def test_a_streamed_failed_turn_never_debits_credits(user, conversation):
    before = _balance(user)
    client = FakeStreamingClient([RuntimeError("connection reset")])

    list(agent.stream_turn(user, conversation, "hello", client=client))

    assert _balance(user) == before


def test_a_turn_that_fails_on_a_later_round_is_refunded(user, conversation, contact):
    """Round 0 succeeds (a tool_use response) and is charged there, exactly
    as designed — but the SAME network hiccup that round 0 could have hit
    is just as possible on round 1, after the charge already landed. The
    student never sees an answer (the turn ends in "I couldn't reach the
    model" same as any other failed turn), so they must not be left having
    paid for it — the fairness rule this whole gate exists for is "never
    charged for a request the student didn't get an answer to," and that
    has to hold for round 1 exactly as much as round 0."""
    before = _balance(user)
    client = FakeClient(
        [
            _response([_tool_use("get_today_queue", {})], "tool_use"),
            RuntimeError("connection reset"),
        ]
    )

    result = agent.run_turn(user, conversation, "What's on today?", client=client)

    assert not result.ok
    assert result.reason == "failed"
    assert _balance(user) == before


def test_a_turn_that_exhausts_the_round_cap_is_refunded(user, conversation, contact):
    """The other way a charged turn can end without ever landing an answer:
    the model keeps calling tools until MAX_ROUNDS runs out. Round 0 was
    charged; the student sees only "I went round in circles" — a failure,
    not an answer — so that charge must come back."""
    before = _balance(user)
    script = [_response([_tool_use("get_today_queue", {}, block_id=f"t{i}")], "tool_use") for i in range(agent.MAX_ROUNDS)]
    client = FakeClient(script)

    result = agent.run_turn(user, conversation, "What's on today?", client=client)

    assert not result.ok
    assert result.reason == "failed"
    assert _balance(user) == before


def test_a_streamed_turn_that_fails_on_a_later_round_is_refunded(user, conversation, contact):
    """The streaming sibling of test_a_turn_that_fails_on_a_later_round_is_refunded
    — same fairness rule, the other transport."""
    before = _balance(user)
    script = [
        ([], _response([_tool_use("get_today_queue", {})], "tool_use", msg_id="m0")),
        RuntimeError("connection reset"),
    ]
    client = FakeStreamingClient(script)

    list(agent.stream_turn(user, conversation, "What's on today?", client=client))

    assert _balance(user) == before


def test_a_streamed_turn_that_exhausts_the_round_cap_is_refunded(user, conversation, contact):
    before = _balance(user)
    script = [([], _response([_tool_use("get_today_queue", {})], "tool_use", msg_id=f"m{i}")) for i in range(agent.MAX_ROUNDS)]
    client = FakeStreamingClient(script)

    list(agent.stream_turn(user, conversation, "keep going", client=client))

    assert _balance(user) == before


@override_settings(CREDIT_PLANS={
    "free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15},
    "pro": {"monthly_grant": 180, "message_cost": 3, "daily_burst": 45},
})
def test_a_sonnet_pro_message_costs_three_times_a_haiku_free_one(user, conversation):
    """docs/credit-system-plan.md §1's core ratio, pinned end-to-end through
    a real turn rather than just at plans.limits_for."""
    before_free = _balance(user)
    agent.run_turn(user, conversation, "hello", client=FakeClient([_response([_text("ok")], "end_turn")]))
    assert before_free - _balance(user) == 1

    pro = User.objects.create_user(email="pro-student@example.com", password="x", plan=User.PLAN_PRO)
    pro_conversation = ChatConversation(user=pro)
    pro_conversation.save()
    before_pro = _balance(pro)
    agent.run_turn(pro, pro_conversation, "hello", client=FakeClient([_response([_text("ok")], "end_turn")]))
    assert before_pro - _balance(pro) == 3


# ---------------------------------------------------------------------------
# Attachments: replay stub for anything but the most recent one
# ---------------------------------------------------------------------------
def test_only_the_most_recent_attachment_is_replayed_in_full(user, conversation):
    """A PDF/image attached early in a long-lived conversation must not be
    re-sent (and re-billed per page, for a PDF) on every later turn — see
    assistant.attachments.stub_old_blocks. Only the latest attachment-
    bearing turn keeps its real bytes; older ones become a filename stub."""
    old_image_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        "_filename": "old.png",
    }
    ChatMessage(
        user=user, conversation=conversation, role=ChatMessage.ROLE_USER,
        content=[old_image_block, {"type": "text", "text": "what's this?"}],
    ).save()
    ChatMessage(
        user=user, conversation=conversation, role=ChatMessage.ROLE_ASSISTANT,
        content=[{"type": "text", "text": "An old screenshot."}],
    ).save()

    client = FakeClient([_response([_text("Sure.")], "end_turn")])
    agent.run_turn(user, conversation, "and this one?", client=client)

    replayed = client.requests[0]["messages"]
    old_turn_content = replayed[0]["content"]
    # The preamble text block rides in front of it (see _api_messages) —
    # the stub is whichever block is NOT the preamble and NOT the question.
    stub_blocks = [b for b in old_turn_content if b.get("type") == "text" and "old.png" in b.get("text", "")]
    assert len(stub_blocks) == 1
    assert "was attached earlier" in stub_blocks[0]["text"]
    # And the real payload is gone — no "source"/base64 data anywhere in
    # what got sent to the model for that turn.
    assert not any("source" in b for b in old_turn_content)


def test_the_latest_attachment_keeps_its_real_bytes(user, conversation):
    """The image on THIS turn — not an earlier one — is exactly the case
    stubbing must never touch: the student is asking about it right now."""
    new_image_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "BBBB"},
        "_filename": "new.png",
    }
    client = FakeClient([_response([_text("ok")], "end_turn")])
    agent.run_turn(user, conversation, "what's this?", client=client, attachment_blocks=[new_image_block])

    replayed = client.requests[0]["messages"]
    last_turn_content = replayed[-1]["content"]
    image_blocks = [b for b in last_turn_content if b.get("type") == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["source"]["data"] == "BBBB"
    assert "_filename" not in image_blocks[0]  # stripped, but the real bytes stayed


def test_the_prompt_teaches_the_draft_fence_and_when_not_to_use_it():
    """A card can only appear if the model writes the fence, so the prompt is
    half the feature. Pinned as content assertions on the string itself: the
    syntax, the three keys, the kind distinction (the model's instinct is to
    call every draft "outreach"), and — the part that decides whether the card
    stays trustworthy — that half an idea never goes in one."""
    prompt = agent.SYSTEM_PROMPT

    assert "```draft contact=482 channel=email kind=follow_up" in prompt
    assert "Subject:" in prompt
    # The id has to be one it actually looked up, not one it invented.
    assert "search_contacts or get_contact result in THIS conversation" in prompt
    assert "leave the whole key out if you haven't looked the person up" in prompt
    # Every channel and every kind the card can log, named.
    for channel in ("email", "linkedin", "coffee_chat", "call", "event", "other"):
        assert channel in prompt
    assert "outreach for a first approach" in prompt
    assert "follow_up for a check-in" in prompt
    assert "thank_you after a chat" in prompt
    assert "Do not default everything to outreach." in prompt
    # Only a FINISHED draft, and the framing that was already there survives.
    assert "Only a finished draft goes in the block." in prompt
    assert "Drafting is not sending." in prompt
    assert "It still isn't sending" in prompt
    # The chat question the chip replaces.
    assert "don't end a draft by asking whether to log it" in prompt


def test_the_prompt_teaches_one_draft_block_per_person_on_a_batch_request():
    """Without this, a "draft a re-ping for everyone who's gone cold" request
    reads like any other single-draft ask, and the model's default instinct
    is one draft plus an offer to do the rest on request — three extra round
    trips for something that should be one reply. Pinned as content
    assertions: the trigger phrasing, the look-everyone-up-first rule (no
    guessed ids), and that this is still drafting, not a new write."""
    prompt = agent.SYSTEM_PROMPT

    assert "one draft block per person in that same reply" in prompt
    assert "not one now and an offer to do the rest" in prompt
    assert "Look each person up first" in prompt
    assert "skip anyone you can't find rather than guessing" in prompt
    assert "nothing sends and nothing logs until they act on each card themselves" in prompt


def test_the_draft_fence_in_the_prompt_parses_with_the_real_parser():
    """The example the model copies has to be one the page can actually read.
    A drifted example is the one prompt bug no content assertion catches."""
    from assistant import drafts

    segments = drafts.split(agent.SYSTEM_PROMPT)
    drafted = [s for s in segments if s["type"] == "draft"]

    assert len(drafted) == 1
    assert drafted[0]["contact_id"] == 482
    assert drafted[0]["channel"] == "email"
    assert drafted[0]["kind"] == "follow_up"
    assert drafted[0]["subject"] == "Catching up on the summer analyst process"


# ---------------------------------------------------------------------------
# The two endings that used to read as an ordinary success.
# ---------------------------------------------------------------------------
def test_a_truncated_answer_says_so_instead_of_passing_as_a_whole_one(user, conversation):
    """`stop_reason == "max_tokens"` means MAX_TOKENS cut the model off
    mid-sentence. Every other cap in this module exists so the student is
    told plainly what happened rather than handed a truncated answer that
    looks like the real one; this was the one cap that did exactly that.

    The reachable case is not exotic: SYSTEM_PROMPT asks for one draft block
    per person on a batch request, and five send-ready emails do not fit in
    2048 tokens — the last would render as a card with a Copy button under
    half an email."""
    client = FakeClient([_response([_text("Hi Yumna,\n\nI wanted to fol")], "max_tokens")])

    result = agent.run_turn(user, conversation, "Draft a re-ping for everyone cold", client=client)

    # The partial answer is kept — the student can see exactly where it cut
    # off, and it is genuinely the start of what they asked for.
    assert result.ok
    assert result.reply.text.startswith("Hi Yumna,")
    notice = _turns(user, conversation)[-1]
    assert notice.notice == ChatMessage.NOTICE_TRUNCATED
    assert "length limit" in notice.text
    assert "—" not in notice.text  # product copy rule: no em dashes


def test_an_ordinary_answer_gets_no_truncation_notice(user, conversation):
    client = FakeClient([_response([_text("Chase Jane today.")], "end_turn")])

    agent.run_turn(user, conversation, "who first", client=client)

    assert [m.notice for m in _turns(user, conversation)] == ["", ""]


def test_a_reply_with_nothing_in_it_is_a_notice_not_silence(user, conversation):
    """`views._thread_rows` skips an assistant row with no text, so an empty
    response rendered as nothing whatsoever: the student's own question, no
    answer, no error, and a credit gone."""
    client = FakeClient([_response([], "end_turn")])

    result = agent.run_turn(user, conversation, "who first", client=client)

    assert not result.ok and result.reason == "failed"
    assert result.reply.notice == ChatMessage.NOTICE_FAILED
    assert result.reply.text.strip()


def test_a_reply_with_nothing_in_it_is_refunded(user, conversation):
    """Nothing to read is not an answer — the same fairness rule every other
    failed turn already gets."""
    from billing import credits as billing_credits

    before = billing_credits.balance(user)
    agent.run_turn(user, conversation, "who first", client=FakeClient([_response([], "end_turn")]))

    assert billing_credits.balance(user) == before


def test_a_truncated_answer_is_not_refunded(user, conversation):
    """They did get an answer and the tokens were genuinely spent on it."""
    from billing import credits as billing_credits
    from assistant import plans

    before = billing_credits.balance(user)
    client = FakeClient([_response([_text("Half an ans")], "max_tokens")])
    agent.run_turn(user, conversation, "who first", client=client)

    assert billing_credits.balance(user) == before - plans.limits_for(user).message_cost


def test_a_tool_call_the_model_never_finished_is_never_persisted(user, conversation):
    """A turn only executes tools on `stop_reason == "tool_use"`, so a
    `tool_use` block in a turn that stopped for any other reason gets no
    `tool_result` — and the Messages API rejects the very next request over
    an assistant turn holding one nothing answered. One truncated tool call
    must not 400 every later turn in the conversation."""
    cut_off = _response(
        [_text("Let me look her up."), _tool_use("get_contact", {"contact_id": 1})],
        "max_tokens",
    )
    client = FakeClient([cut_off])

    agent.run_turn(user, conversation, "tell me about Jane", client=client)

    stored = [b for m in _turns(user, conversation) for b in m.blocks()]
    assert not any(b.get("type") == "tool_use" for b in stored)
    assert any(b.get("type") == "tool_result" for b in stored) is False

    # ...and the next turn's replay is a shape the API will accept.
    second = FakeClient([_response([_text("She's warm.")], "end_turn")])
    result = agent.run_turn(user, conversation, "and her firm?", client=second)
    assert result.ok
    replayed = [b for m in second.requests[0]["messages"] for b in m["content"]]
    assert not any(b.get("type") == "tool_use" for b in replayed)


def test_a_streamed_truncated_answer_yields_a_notice_before_done(user, conversation):
    """Before "done", not after: the notice belongs under the partial answer
    in the log, not after the composer has already re-enabled."""
    client = FakeStreamingClient(
        [(["Hi Yumna,", "\n\nI wanted to fol"], _response([_text("Hi Yumna,\n\nI wanted to fol")], "max_tokens"))]
    )

    events = list(agent.stream_turn(user, conversation, "draft them all", client=client))

    kinds = [e["type"] for e in events]
    assert kinds[-2:] == ["notice", "done"]
    assert events[-2]["kind"] == ChatMessage.NOTICE_TRUNCATED
    assert _turns(user, conversation)[-1].notice == ChatMessage.NOTICE_TRUNCATED


def test_a_streamed_empty_reply_is_one_terminal_failed_notice(user, conversation):
    client = FakeStreamingClient([([], _response([], "end_turn"))])

    events = list(agent.stream_turn(user, conversation, "who first", client=client))

    assert [e["type"] for e in events] == ["notice"]
    assert events[0]["kind"] == "failed"
    # No "done" frame at all — there is nothing whole to finalise.
    assert not any(e["type"] == "done" for e in events)


# ---------------------------------------------------------------------------
# The per-user preamble tells the advisor what the student STATED.
#
# Measured on the founder's account 2026-09-01 with only name / school /
# class / regions / tracks in it: 405 board roles were visa-blocked for him
# and the advisor could not know he needed sponsorship; all four digest picks
# were the wrong intake and it could not know which cycle he was in; 44
# follow-ups were due and it could not say that was his own 7-business-day
# window. Every clause below reads a stated column, and the cached system
# prefix stays byte-stable because none of it goes there.
# ---------------------------------------------------------------------------
def _founder_like(**overrides):
    fields = dict(
        email="founder@example.com", password="x", name="Jimmy", school="USC",
        class_year=2029, regions=["hk", "us"], tracks=["ib", "st"],
        target_cycles=["2028 Summer Internship"], timezone="Asia/Hong_Kong",
        work_authorization={
            "us": "sponsorship", "hk": "sponsorship", "eu": "sponsorship",
            "sg": "sponsorship", "jp": "sponsorship", "cn": "citizen",
        },
        cadence_params={
            "followup_after_business_days": 7,
            "advocate_touch_min_weeks": 3,
            "chatted_touch_min_weeks": 6,
        },
        weekly_touch_goal=14,
    )
    fields.update(overrides)
    return User.objects.create_user(**fields)


def test_the_preamble_names_markets_and_tracks_in_words_not_codes():
    preamble = agent.build_preamble(_founder_like())

    assert "Recruiting in: Hong Kong, United States" in preamble
    assert "Tracks of interest: Investment Banking, Sales & Trading" in preamble
    assert "hk, us" not in preamble
    assert "ib, st" not in preamble


def test_the_preamble_states_work_authorization_per_market_in_words():
    preamble = agent.build_preamble(_founder_like())

    assert (
        "Work authorization: needs visa sponsorship in Hong Kong, United States, "
        "Singapore, Europe, Japan; no sponsorship needed in Mainland China."
    ) in preamble


def test_the_preamble_names_the_markets_work_authorization_was_not_answered_for():
    preamble = agent.build_preamble(
        _founder_like(work_authorization={"us": "sponsorship", "hk": "citizen"})
    )

    assert "needs visa sponsorship in United States" in preamble
    assert "no sponsorship needed in Hong Kong" in preamble
    assert "not stated for Singapore, Europe, Mainland China, Japan" in preamble


def test_a_student_with_no_work_authorization_answers_gets_no_clause():
    preamble = agent.build_preamble(_founder_like(work_authorization={}))

    assert "Work authorization" not in preamble


def test_the_preamble_states_the_target_cycle_and_the_timezone_name():
    preamble = agent.build_preamble(_founder_like())

    assert "Recruiting for: 2028 Summer Internship" in preamble
    assert "Timezone: Asia/Hong_Kong" in preamble


def test_an_unset_timezone_is_named_as_utc_not_guessed_from_regions():
    preamble = agent.build_preamble(_founder_like(timezone=""))

    assert "Timezone: not set, so Coverage uses UTC" in preamble


def test_the_preamble_carries_the_students_own_cadence_settings_and_marks_overrides():
    preamble = agent.build_preamble(_founder_like())

    assert "first follow-up: 7 business days (their own setting; the default is 6)" in preamble
    assert "advocate check-in: 3 weeks (their own setting; the default is 4)" in preamble
    assert "keep-warm check-in: 6 weeks (their own setting; the default is 3)" in preamble
    assert "weekly touch goal: 14 touches (their own setting; the default is 10)" in preamble
    assert "advocate target: 2 per firm" in preamble


def test_default_cadence_reads_as_the_default_not_as_a_choice():
    preamble = agent.build_preamble(_founder_like(cadence_params={}, weekly_touch_goal=None))

    assert "first follow-up: 6 business days" in preamble
    assert "their own setting" not in preamble
    assert "weekly touch goal: 10 touches (the default)" in preamble


def test_the_preamble_reads_fields_that_may_not_exist_yet_only_when_present():
    """`languages`, `study_level`, `affiliations` are being added to User
    alongside this. Absent (or empty) they print nothing; present they print
    as stated — read with getattr, never assumed."""
    user = _founder_like()
    before = agent.build_preamble(user)
    assert "Languages:" not in before
    assert "Study level:" not in before
    assert "Affiliations:" not in before

    user.languages = ["English", "Mandarin"]
    user.study_level = "undergrad"
    user.affiliations = ["USC Investment Club", "Trojan Finance Society"]
    after = agent.build_preamble(user)

    assert "Languages: English, Mandarin" in after
    assert "Study level: undergrad" in after
    assert "Affiliations: USC Investment Club, Trojan Finance Society" in after


def test_an_empty_profile_still_gets_the_cadence_and_timezone_facts():
    """The old "they have not filled in their profile" sentence survives for
    a blank account, but the settings that exist regardless of the profile
    (cadence defaults, timezone) are still stated."""
    blank = User.objects.create_user(email="blank@example.com", password="x")

    preamble = agent.build_preamble(blank)

    assert "have not filled in their profile yet" in preamble
    assert "Timezone: not set, so Coverage uses UTC" in preamble
    assert "Cadence settings" in preamble


def test_none_of_the_new_per_user_facts_reach_the_cached_system_prefix():
    """The byte-stability rule: everything above rides the first user turn,
    never the system blocks. Checked on the wire, not on the function."""
    user = _founder_like()
    conversation = ChatConversation(user=user)
    conversation.save()
    client = FakeClient([_response([_text("ok")], "end_turn")])

    agent.run_turn(user, conversation, "hi", client=client)

    request = client.requests[0]
    system_blob = json.dumps(request["system"])
    preamble = request["messages"][0]["content"][0]["text"]
    for fact in (
        "needs visa sponsorship in Hong Kong", "Asia/Hong_Kong",
        "2028 Summer Internship", "7 business days",
    ):
        assert fact not in system_blob
        assert fact in preamble


# ---------------------------------------------------------------------------
# The drafting rules: one specific hook, short, no placeholders, no tells,
# and honesty on a batch.
# ---------------------------------------------------------------------------
def test_the_prompt_replaces_the_one_sentence_template_rule_with_a_rule_block():
    prompt = agent.SYSTEM_PROMPT

    assert "not a generic template" not in prompt
    assert "DRAFTING RULES" in prompt
    # One specific thing from a tool result, named in prose before the block.
    assert "Opens on ONE specific thing that came back from a tool result" in prompt
    assert "say in prose which one you used" in prompt
    # Not resending what already went.
    assert "my_opener" in prompt
    assert "recent_subjects" in prompt
    # The caps.
    assert "under 120 words" in prompt
    assert "under 80" in prompt
    # No placeholders.
    assert "[Firm]" in prompt
    assert "{name}" in prompt
    # The banned phrases.
    for phrase in ("pick your brain", "learn more about your journey",
                   "would love to connect", "cut my teeth"):
        assert phrase in prompt
    # And the page's own enforcement is disclosed to the model.
    assert "shown as plain text with no card" in prompt


def test_the_prompt_caps_a_batch_at_five_and_asks_for_honesty_past_it():
    prompt = agent.SYSTEM_PROMPT

    assert "up to five people" in prompt
    assert "opens on a different specific observation" in prompt
    assert "these three have nothing in their history to hook on" in prompt
    assert "name who is left for the next message" in prompt


def test_the_prompts_own_example_draft_passes_the_pages_template_guard():
    """The example the model copies must itself clear `drafts.flag_reason`,
    or the page would demote the very thing the prompt teaches."""
    from assistant import drafts

    drafted = [s for s in drafts.split(agent.SYSTEM_PROMPT) if s["type"] == "draft"]

    assert len(drafted) == 1
    assert drafts.flag_reason(drafted[0]["subject"], drafted[0]["body"], drafted[0]["kind"]) is None
