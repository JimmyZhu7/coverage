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
from django.utils import timezone

from analytics.models import UserOpportunity
from assistant import tools
from assistant.models import AdvisorMemory
from crm.models import CalendarEvent, Contact, Touch, UserFirm
from directory.models import Firm, Opportunity

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


# Every tool that takes a row id, with the id it takes.
_ID_TOOLS = [
    ("get_contact", "contact_id", "contact"),
    ("log_touch", "contact_id", "contact"),
    ("track_opportunity", "opportunity_id", "opportunity"),
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

    result, is_error = _call(bob, tool_name, args)

    if tool_name == "track_opportunity":
        assert not is_error
        # Bob saving a public role creates BOB's row and never touches Alice's.
        assert UserOpportunity.objects.for_user(bob).count() == 1
        assert UserOpportunity.objects.for_user(alices_world["contact"].user).count() == 1
    else:
        assert is_error, result
        assert "no contact with that id" in result["error"].lower()


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
