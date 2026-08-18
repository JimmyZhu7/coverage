"""The security tests for the advisor: one student's data, and only theirs.

TWO GUARANTEES, TESTED TWO WAYS

1. STRUCTURAL — `test_the_package_never_reaches_for_all_objects`. Every model
   the assistant touches is a `PrivateModel`, whose default manager RAISES on
   an unscoped query (`coverage_web/tenancy.py`). The one way around that is
   `Model.all_objects`, which tenancy.py deliberately makes a visible,
   greppable admission that a query is NOT tenant-scoped. This package has no
   legitimate reason to make that admission, so the name is banned outright
   and the grep is the enforcement. It is a cheap, blunt check on purpose: a
   reviewer can verify it in one command, and it cannot be satisfied by a
   subtly-wrong scope the way a behavioural test can.

2. BEHAVIOURAL — every tool, parametrized, called by user B with user A's row
   id. The expected answer is "not found", never A's data. This is the test
   that would actually catch a `.filter(pk=...)` written before the
   `.for_user()` instead of after it.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from analytics.models import UserOpportunity
from assistant import tools
from assistant.models import AdvisorMemory, ChatConversation, ChatMessage
from crm.models import CalendarEvent, Contact, Touch, UserFirm
from directory.models import Firm, Opportunity, OpportunityChange

User = get_user_model()

APP_DIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. The greppable invariant
# ---------------------------------------------------------------------------
# Django writes `'default_manager_name': 'all_objects'` and
# `managers=[('all_objects', ...)]` into the generated initial migration,
# because that pin is part of `PrivateModel.Meta` and migrations serialize a
# model's Meta verbatim. Those are DECLARATIONS of the manager, not queries
# through it, and they are machine-written — so migrations are checked with
# the stricter, narrower rule below (no attribute access) rather than
# exempted.
_ANY_MENTION = re.compile(r"all_objects")
_ATTRIBUTE_ACCESS = re.compile(r"all_objects\s*\.")


# This file is the one exemption, and it has to be: the check needs the
# forbidden name as a literal to search for and as a fixture to prove the
# search works. Exempting the file that DEFINES a rule is not a loophole in
# the rule — every other file in the package, tests included, is checked.
_SELF = Path(__file__).name


def _source_files():
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == _SELF:
            continue
        yield path


def test_the_package_never_reaches_for_all_objects():
    """No file in `coverage_web/assistant/` — code or tests — may mention the
    unscoped manager. If this fails, the fix is a `.for_user(user)` query, not
    an exemption."""
    offenders = []
    for path in _source_files():
        is_migration = "migrations" in path.parts
        pattern = _ATTRIBUTE_ACCESS if is_migration else _ANY_MENTION
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(APP_DIR)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "assistant/ must never query through the unscoped manager:\n" + "\n".join(offenders)
    )


def test_the_grep_would_actually_catch_something():
    """A guard on the guard: an empty file list or a broken regex would make
    the check above pass vacuously forever."""
    files = list(_source_files())
    assert len(files) >= 8
    assert _ANY_MENTION.search("Contact.all_objects.create(user=u)")
    assert _ATTRIBUTE_ACCESS.search("Contact.all_objects.create(user=u)")


def test_no_tool_body_takes_a_user_from_its_arguments():
    """`execute()` binds `user` from the view's request; the schemas have no
    field for one. Restated here as a source-level check so a future tool
    added with a `user_id` argument fails loudly."""
    source = (APP_DIR / "tools.py").read_text()
    assert "def execute(user" in source
    for schema in tools.TOOL_SCHEMAS:
        assert "user_id" not in json.dumps(schema["input_schema"]), schema["name"]


# Anthropic's grammar compiler refuses a request outright once the OPTIONAL
# parameters across every tool schema in one request pass a fixed ceiling —
# measured live 2026-08-18: 26 offered, every single turn 400'd with
# "Schemas contains too many optional parameters (26)... limit: 24", taking
# the whole advisor down, not just the tool that pushed the count over. This
# is not a per-tool defect a normal test would catch — each tool's own
# schema is perfectly valid on its own, and it only breaks in combination
# with every OTHER tool that already exists. A margin, not the bare limit:
# 20 leaves room for one more small tool before this fires again as a real
# warning instead of a live production outage.
_MAX_TOTAL_OPTIONAL_PARAMS = 20


def test_the_combined_tool_schemas_stay_under_the_apis_optional_param_ceiling():
    total = 0
    for schema in tools.TOOL_SCHEMAS:
        props = schema["input_schema"].get("properties", {})
        required = set(schema["input_schema"].get("required", []))
        total += sum(1 for p in props if p not in required)
    assert total <= _MAX_TOTAL_OPTIONAL_PARAMS, (
        f"{total} optional parameters across all tool schemas combined — "
        f"Anthropic's actual hard limit is 24, at which point EVERY turn "
        f"fails, not just the tool that pushed it over. Trim an existing "
        f"tool's optional fields before adding more (add_contact was cut "
        f"from 9 to 3 for exactly this reason — see its own docstring)."
    )


# ---------------------------------------------------------------------------
# 2. Cross-tenant reads and writes
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.django_db


@pytest.fixture
def alice():
    return User.objects.create_user(email="alice@example.com", password="x")


@pytest.fixture
def bob():
    return User.objects.create_user(email="bob@example.com", password="x")


@pytest.fixture
def firm():
    return Firm.objects.create(slug="north-bank", name="North Bank")


@pytest.fixture
def alices_world(alice, firm):
    """Everything Alice owns: a contact with history, a target firm, a saved
    role, a calendar event."""
    contact = Contact(user=alice, firm=firm, name="Alice's Banker", role="MD")
    # The AI-written relationship note (crm.ai_summary) rides out through
    # `get_contact`'s payload like every other column on this row, so it needs
    # the same cross-tenant proof they get.
    contact.ai_summary = "Alice's AI-written relationship note."
    contact.ai_summary_generated_at = timezone.now()
    contact.save()
    touch = Touch(
        user=alice, contact=contact, ts=timezone.now(), kind="chat",
        channel="coffee_chat", note="Alice's private note",
    )
    touch.save()
    UserFirm(user=alice, firm=firm, tier=1).save()
    opportunity = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        deadline=timezone.localdate() + timedelta(days=20),
        url="https://north.example/jobs/1",
    )
    tracked = UserOpportunity(user=alice, opportunity=opportunity)
    tracked.save()
    event = CalendarEvent(
        user=alice, title="Alice's chat", starts_at=timezone.now() + timedelta(days=1),
        kind="chat", contact=contact,
    )
    event.save()
    return {"contact": contact, "opportunity": opportunity, "firm": firm}


def _call(user, name, args, message_id="msg_test"):
    payload, is_error = tools.execute(user, name, args, message_id)
    return json.loads(payload), is_error


# Every tool that takes ONE tenant-scoped row id, with the id it takes.
#
# The three writes added after this list are covered by their own tests below
# rather than here, because none of them fits this shape: `set_contact_status`
# takes a LIST of ids (a batch fails differently from a single call — it has
# to report the skipped ones, not error out), `add_contact` takes no
# tenant-scoped id at all (only a SHARED-zone `firm_id`), and
# `update_settings` takes no id of any kind. Each still gets a cross-tenant
# test; see "Bob cannot..." / "lands in his own network" / "never reaches
# Alice's account" further down.
_ID_TOOLS = [
    ("get_contact", "contact_id", "contact"),
    ("log_touch", "contact_id", "contact"),
    ("track_opportunity", "opportunity_id", "opportunity"),
    ("add_calendar_event", "contact_id", "contact"),
]


@pytest.mark.parametrize("tool_name,field,owned", _ID_TOOLS)
def test_user_b_cannot_reach_user_a_s_row_by_id(bob, alices_world, tool_name, field, owned):
    args = {field: alices_world[owned].id}
    if tool_name == "log_touch":
        args |= {"kind": "outreach", "channel": "email"}
    if tool_name == "track_opportunity":
        # An Opportunity is SHARED-zone (every student sees the board), so the
        # thing that must not leak is Alice's tracking row, not the posting.
        args |= {"status": "saved"}
    if tool_name == "add_calendar_event":
        args |= {"title": "Coffee", "date": "2026-08-20"}

    result, is_error = _call(bob, tool_name, args)

    if tool_name == "track_opportunity":
        assert not is_error
        # Bob saving a public role creates BOB's row and never touches Alice's.
        assert UserOpportunity.objects.for_user(bob).count() == 1
        assert UserOpportunity.objects.for_user(alices_world["contact"].user).count() == 1
    else:
        assert is_error, result
        assert "no contact with that id" in result["error"].lower()


def test_alices_ai_summary_never_reaches_bob_through_any_tool(bob, alices_world):
    """`get_contact` gained an `advisor_summary` key (crm.ai_summary's
    AI-written relationship note). It is one more column on Alice's contact
    row, so the guarantee is the same one every other column gets: Bob asking
    for it by id, by search, or by queue gets nothing back with her words in
    it. Asserted on the RAW payload rather than a parsed field, so a future
    key rename cannot quietly drop the check."""
    summary = alices_world["contact"].ai_summary
    assert summary  # the fixture really did seed one

    for name, args in (
        ("get_contact", {"contact_id": alices_world["contact"].id}),
        ("search_contacts", {"query": "banker"}),
        ("get_today_queue", {}),
        ("get_my_pipeline", {}),
    ):
        raw, _ = tools.execute(bob, name, args, "msg_test")
        assert summary not in raw, name


def test_user_b_gets_an_empty_network_not_user_a_s(bob, alices_world):
    contacts, is_error = _call(bob, "search_contacts", {"query": "banker"})
    assert not is_error
    assert contacts["total_matches"] == 0
    assert contacts["contacts"] == []


def test_user_b_s_today_queue_is_empty(bob, alices_world):
    queue, is_error = _call(bob, "get_today_queue", {})
    assert not is_error
    assert queue["queue"] == []


def test_user_b_s_calendar_is_empty(bob, alices_world):
    calendar, is_error = _call(bob, "get_calendar", {"days_ahead": 30})
    assert not is_error
    assert calendar["events"] == []


def test_user_b_s_target_firms_are_empty_not_alices(bob, alices_world):
    """Alice tiered North Bank in the fixture; Bob has tiered nothing.
    get_my_firms takes no id at all — the only thing scoping its result to
    the right student is the `user` closure, so this is exactly the shape
    of bug (a forgotten `.for_user()`) the rest of this file exists to
    catch."""
    result, is_error = _call(bob, "get_my_firms", {})
    assert not is_error
    assert result["firms"] == []


def test_user_b_s_pipeline_is_empty(bob, alices_world):
    pipeline, is_error = _call(bob, "get_my_pipeline", {})
    assert not is_error
    assert pipeline["by_status"] == {}


def test_get_firm_shows_the_shared_firm_but_none_of_user_a_s_private_layer(bob, alices_world):
    """A firm is shared-zone data — Bob is entitled to see North Bank exists
    and how many roles it has open. What must never cross is the private
    layer bolted onto it: Alice's tier and Alice's people there."""
    result, is_error = _call(bob, "get_firm", {"name_or_slug": "north-bank"})

    assert not is_error
    assert result["firm"] == "North Bank"
    assert result["open_roles"] == 1
    assert result["my_tier"] is None
    assert result["is_target_firm"] is False
    assert result["my_contacts"] == []


def test_user_b_s_write_attempt_leaves_user_a_s_contact_untouched(bob, alices_world):
    contact = alices_world["contact"]
    before = Touch.objects.for_user(contact.user).count()

    _call(
        bob,
        "log_touch",
        {"contact_id": contact.id, "kind": "reply_received", "channel": "email"},
    )

    assert Touch.objects.for_user(contact.user).count() == before
    contact.refresh_from_db()
    # Still the default: the fixture's touch row was inserted straight through
    # the ORM (no ratchet), and Bob's call never reached the pipeline at all.
    assert contact.warmth == "cold"


def test_search_opportunities_never_leaks_another_student_s_dismissals(bob, alice, firm):
    """Dismissals are private. Alice hiding a role must not hide it from Bob."""
    opportunity = Opportunity.objects.create(
        firm=firm, title="Shared Posting", bucket="internship", status="open",
        url="https://north.example/jobs/shared",
    )
    hidden = UserOpportunity(user=alice, opportunity=opportunity, dismissed=True)
    hidden.save()

    result, _ = _call(bob, "search_opportunities", {})

    assert [r["opportunity_id"] for r in result["roles"]] == [opportunity.id]


def test_bob_cannot_move_alices_contacts_with_a_list_of_her_ids(bob, alices_world):
    """`set_contact_status` takes a LIST, so the parametrized sweep above
    cannot reach it — one bad id in a batch is a different shape of mistake
    from one bad id on its own. Every id Bob borrows must come back in
    `not_found` and change nothing: reported, never silently skipped, and
    never allowed to look like a success."""
    alices = alices_world["contact"]
    before = (alices.warmth, alices.thread_state)
    touches_before = Touch.objects.for_user(alices.user).count()

    result, is_error = _call(
        bob,
        "set_contact_status",
        {"contact_ids": [alices.id, 999999], "thread_state": "parked", "warmth": "advocate"},
    )

    assert not is_error
    assert result["changed_count"] == 0
    assert result["changed"] == []
    assert sorted(result["not_found"]) == sorted([alices.id, 999999])
    assert "do not claim the rest" in result["instruction"]
    alices.refresh_from_db()
    assert (alices.warmth, alices.thread_state) == before
    # No audit touch either: the override was never reached, so her history
    # has nothing in it from Bob's call.
    assert Touch.objects.for_user(alices.user).count() == touches_before


def test_a_contact_bob_adds_lands_in_his_own_network_and_nowhere_else(bob, alices_world):
    """`add_contact` takes no tenant-scoped id at all — the ONLY thing putting
    the new row under the right student is the `user` execute() closes over,
    which is the same shape of bug (a write that forgets whose it is) the
    rest of this file exists to catch on reads."""
    result, is_error = _call(bob, "add_contact", {"name": "Bob's New Person", "firm_text": "Some Bank"})

    assert not is_error
    assert Contact.objects.for_user(bob).filter(name="Bob's New Person").exists()
    # Alice keeps exactly the one contact her fixture gave her — and the
    # shared firm both students can see did not carry anything across.
    assert [c.name for c in Contact.objects.for_user(alices_world["contact"].user)] == ["Alice's Banker"]


def test_bob_changing_a_setting_never_reaches_alices_account(bob, alice):
    """Settings are written straight onto the `User` row, which is not a
    `PrivateModel` with a `.for_user()` scope to forget — the tool writes to
    the `user` object execute() was handed and nothing else. Pinned here
    because a future refactor that looked the user up by anything other than
    that closure would be silent."""
    alice.weekly_touch_goal, alice.school = 25, "Alice's School"
    alice.save(update_fields=["weekly_touch_goal", "school"])

    result, is_error = _call(bob, "update_settings", {"field": "weekly_touch_goal", "value": "3"})

    assert not is_error
    bob.refresh_from_db()
    alice.refresh_from_db()
    assert bob.weekly_touch_goal == 3
    assert alice.weekly_touch_goal == 25
    assert alice.school == "Alice's School"


def test_bobs_situation_never_shows_alices_tracked_deadline_change(bob, alices_world):
    """Alice tracks a role (the `alices_world` fixture) and its deadline
    moves inside the window. `get_situation` (assistant.situation.
    build_situation) joins through `UserOpportunity.objects.for_user` to
    decide which opportunities are even in play — Bob has tracked nothing,
    so his result must come back empty, not Alice's change."""
    OpportunityChange.objects.create(
        opportunity=alices_world["opportunity"], field="deadline",
        old_value="2026-08-01", new_value="2026-08-20",
        stage="reverify", observed_at=timezone.now(),
    )

    result, is_error = _call(bob, "get_situation", {})

    assert not is_error
    assert result == {"total": 0, "events": []}


def test_remembering_a_fact_never_reaches_another_students_cap_or_list(alice, bob):
    """Alice's own memories must not count against Bob's MAX_MEMORIES cap,
    and Bob's remember() must never write to Alice's list — the only thing
    scoping AdvisorMemory to the right student is the `user` argument
    execute() closes over, exactly the shape of bug this file exists to
    catch everywhere else."""
    for i in range(tools.MAX_MEMORIES):
        AdvisorMemory(user=alice, text=f"alice fact {i}").save()

    result, is_error = _call(bob, "remember", {"fact": "bob's own fact"})

    assert not is_error
    assert AdvisorMemory.objects.for_user(bob).count() == 1
    assert AdvisorMemory.objects.for_user(alice).count() == tools.MAX_MEMORIES


# ---------------------------------------------------------------------------
# 3. The draft card's one-click write
#
# `log_draft_touch` is the only write on this page that does NOT go through
# `tools.execute()`, so the parametrized sweep above cannot reach it: the ids
# arrive in a POST body straight from a browser instead of from a model's tool
# arguments. Same guarantee, checked at the door it actually has.
# ---------------------------------------------------------------------------
def _alices_draft(alice, alices_world):
    conversation = ChatConversation(user=alice, title="Alice's follow-ups")
    conversation.save()
    message = ChatMessage(
        user=alice,
        conversation=conversation,
        role=ChatMessage.ROLE_ASSISTANT,
        content=[
            {
                "type": "text",
                "text": (
                    f"```draft contact={alices_world['contact'].id} channel=email kind=follow_up\n"
                    "Subject: Alice's draft\n\nHi.\n```"
                ),
            }
        ],
    )
    message.save()
    return message


@pytest.mark.parametrize("borrowed", ["message", "contact", "both"])
def test_bob_cannot_log_a_touch_from_alices_draft(client, alice, bob, alices_world, borrowed):
    """Every combination of Alice's ids Bob could paste into the POST: her
    message, her contact, or both. All 404, and none of them leave a touch on
    her network."""
    message = _alices_draft(alice, alices_world)
    bobs_conversation = ChatConversation(user=bob)
    bobs_conversation.save()
    bobs_message = ChatMessage(
        user=bob, conversation=bobs_conversation, role=ChatMessage.ROLE_ASSISTANT,
        content=[{"type": "text", "text": "nothing drafted here"}],
    )
    bobs_message.save()
    bobs_contact = Contact(user=bob, firm=alices_world["firm"], name="Bob's Banker")
    bobs_contact.save()

    client.force_login(bob)
    response = client.post(
        reverse("assistant:log_draft_touch"),
        {
            "message": message.id if borrowed in ("message", "both") else bobs_message.id,
            "contact": alices_world["contact"].id if borrowed in ("contact", "both") else bobs_contact.id,
            "channel": "email",
            "kind": "follow_up",
        },
    )

    assert response.status_code == 404
    # Alice's contact keeps the one touch her own fixture gave her.
    assert Touch.objects.for_user(alice).count() == 1
    assert Touch.objects.for_user(bob).count() == 0


# ---------------------------------------------------------------------------
# 4. The rewind
#
# The other write on this page that takes an id straight from a POST body
# rather than from a model's tool arguments — and the only one that DELETES.
# A rewind pointed at another student's message would take out their whole
# conversation from that point on, so it gets its own door check.
# ---------------------------------------------------------------------------
def test_bob_cannot_rewind_a_message_in_alices_conversation(client, alice, bob):
    conversation = ChatConversation(user=alice, title="Alice's plan")
    conversation.save()
    question = ChatMessage(
        user=alice, conversation=conversation, role=ChatMessage.ROLE_USER,
        content=[{"type": "text", "text": "where should I spend this week?"}],
    )
    question.save()
    answer = ChatMessage(
        user=alice, conversation=conversation, role=ChatMessage.ROLE_ASSISTANT,
        content=[{"type": "text", "text": "On Morgan Stanley."}],
    )
    answer.save()

    client.force_login(bob)
    response = client.post(
        reverse("assistant:edit_message"), {"message": question.id, "text": "hijacked"}
    )

    assert response.status_code == 404
    # Nothing of hers moved: not the words, not the turns after them, not the
    # title the answer earned.
    assert [m.id for m in ChatMessage.objects.for_user(alice).filter(conversation=conversation)] == [
        question.id,
        answer.id,
    ]
    question.refresh_from_db()
    assert question.text == "where should I spend this week?"
    conversation.refresh_from_db()
    assert conversation.title == "Alice's plan"
    assert ChatMessage.objects.for_user(bob).count() == 0
