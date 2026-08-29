"""Adversarial invariant suite for the advisor's CAPABILITY SURFACE — what
the model can call, what each call is allowed to do, and what a malformed
call must never do.

Companion to the two files that already exist and deliberately NOT a third
copy of either. `test_tools.py` asks "does each tool return the right facts";
`test_isolation.py` asks "can user B reach user A's row" and answers it
per-tool, parametrized. Neither asks the question this file asks, which is
the one the capability surface itself poses: is the SET closed and
self-describing, and does the whole set survive being called wrongly?

Same discipline as `coverage_domain/tests/test_stress_invariants.py` and
`crm/tests/test_stress_crm.py`: NO `hypothesis`. The input space here is a
small enumerated cross-product — 17 tools x a dozen junk argument shapes —
which is small enough to walk EXHAUSTIVELY, and exhaustive beats sampled.

THE FOUR INVARIANTS, and why each is worth a test rather than a comment:

  1. THE SET IS CLOSED. Every schema has a handler, every handler has a
     schema, every tool has a student-facing label. A tool present in one
     of the three and absent from another is not a typo — it is a tool the
     model can call that the page cannot name, or a tool advertised to the
     model that errors on every call.

  2. `WRITE_TOOLS` IS THE COMPLETE, CORRECT SET OF WRITES. This is the one
     with teeth. `WRITE_TOOLS` is not documentation: `agent.stream_turn`
     reads it to decide whether to announce a call mid-stream with a casual
     "reading..." label, and both loops read it to record the
     `assistant_write` product event. A write tool missing from the set
     would be announced to the student as a harmless lookup and would leave
     no write in the audit trail. So the set is checked against what the
     tools ACTUALLY DO — by counting rows before and after a real call —
     not against a second hand-written list.

  3. NO READ TOOL WRITES. The house rule ("a GET must not create, update,
     or charge") stated as a property of the tool layer and checked by
     running every read tool against a populated account and asserting the
     database is byte-identical afterwards.

  4. `execute` IS TOTAL. Its docstring promises it "never raises". Every
     tool x every junk argument shape, asserting a JSON string comes back
     — and, for the write tools, that the junk wrote nothing.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from analytics.models import ProductEvent, UserOpportunity
from assistant import agent, tools
from assistant.models import AdvisorMemory
from crm.models import CalendarEvent, Contact, Touch, UserFirm
from directory.models import Firm, Opportunity

User = get_user_model()

pytestmark = pytest.mark.django_db


SCHEMA_NAMES = [t["name"] for t in tools.TOOL_SCHEMAS]

# Every tool that is not declared a write. Derived, never restated: a tool
# added to `WRITE_TOOLS` leaves this list on the same commit.
READ_NAMES = [n for n in SCHEMA_NAMES if n not in tools.WRITE_TOOLS]

# The argument shapes a model can produce that the schema's own
# grammar-constrained sampling is NOT guaranteed to stop — a tool_use block
# with a null input, a field of the wrong type, a list where a scalar
# belongs. `strict: true` makes these unlikely; `execute`'s "never raises"
# promise makes them harmless, and that is what is being checked.
JUNK_ARGS: list[object] = [
    None,
    {},
    {"contact_id": None},
    {"contact_id": "not-an-int"},
    {"contact_id": -1},
    {"contact_id": 10**12},
    {"contact_ids": "not-a-list"},
    {"contact_ids": []},
    {"contact_ids": [None, "x", 1.5]},
    {"query": None},
    {"query": ""},
    {"limit": "many"},
    {"field": "password", "value": "hunter2"},
    {"field": "timezone", "value": "Mars/Olympus", "confirmed": True},
    {"date": "2026-02-30", "title": "Bad day"},
    {"status": "submitted", "opportunity_id": 1},
    {"kind": "../../etc/passwd", "channel": "email", "contact_id": 1},
    {"fact": ""},
    {"unexpected_key": ["deeply", {"nested": None}]},
]


def _snapshot(user):
    """Every private row this package can reach, as a comparable tuple. If a
    tool wrote anything at all, one of these numbers moves."""
    return {
        "contacts": list(
            Contact.objects.for_user(user)
            .order_by("pk")
            .values_list("pk", "name", "warmth", "thread_state", "email",
                         "role", "firm_text", "notes", "ai_summary")
        ),
        "touches": list(
            Touch.objects.for_user(user).order_by("pk").values_list("pk", "kind", "note")
        ),
        "events": list(
            CalendarEvent.objects.for_user(user).order_by("pk").values_list("pk", "title")
        ),
        "memories": list(
            AdvisorMemory.objects.for_user(user).order_by("pk").values_list("pk", "text")
        ),
        "tracked": list(
            UserOpportunity.objects.for_user(user)
            .order_by("pk")
            .values_list("pk", "applied_status", "dismissed")
        ),
        "firms": list(
            UserFirm.objects.for_user(user).order_by("pk").values_list("pk", "tier")
        ),
        "profile": (
            user.__class__.objects.filter(pk=user.pk)
            .values_list("name", "school", "class_year", "timezone", "timezone_auto",
                         "regions", "tracks", "target_cycles", "weekly_touch_goal",
                         "weekly_digest_opt_out", "cadence_params", "assets",
                         "work_authorization")
            .first()
        ),
    }


@pytest.fixture
def firm():
    return Firm.objects.create(
        slug="north-bank", name="North Bank", regions=["hk"], tracks=["ibd"]
    )


@pytest.fixture
def world(firm):
    """One student with something on every surface a tool can read, so a read
    tool that writes has something to write over."""
    user = User.objects.create_user(
        email="stress@example.com", password="x", name="Sam", school="HKU",
        class_year=2028, regions=["hk"], tracks=["ib"],
    )
    contact = Contact(user=user, firm=firm, name="Jane Banker", role="VP, IBD",
                      region="hk", email="jane@north.example")
    contact.save()
    Touch(user=user, contact=contact, kind="outreach", channel="email",
          note="[gmail:abc123] first note", ts=timezone.now() - timedelta(days=9)).save()
    Touch(user=user, contact=contact, kind="reply_received", channel="email",
          note="she wrote back", ts=timezone.now() - timedelta(days=7)).save()
    UserFirm(user=user, firm=firm, tier=1).save()
    CalendarEvent(user=user, title="Coffee with Jane",
                  starts_at=timezone.now() + timedelta(days=2)).save()
    AdvisorMemory(user=user, text="Ruled out PE.").save()
    opp = Opportunity.objects.create(
        firm=firm, title="2028 Summer Analyst, Hong Kong", bucket="internship",
        region="hk", location="Hong Kong", status="open",
        deadline=timezone.localdate() + timedelta(days=10),
        url="https://north.example/jobs/1",
    )
    UserOpportunity(user=user, opportunity=opp).save()
    return {"user": user, "contact": contact, "firm": firm, "opportunity": opp}


# ===========================================================================
# INVARIANT 1 — the capability set is closed and self-describing.
#
# Three lists have to agree about what the advisor can do: the schemas the
# API is handed, the handlers `execute` dispatches to, and the labels the
# page shows the student. A name in one and not another is a tool the model
# can call and the page cannot name, or one advertised and unimplemented.
# ===========================================================================
def test_every_advertised_tool_has_exactly_one_handler():
    dispatchable = set(tools._HANDLERS) | set(tools._MESSAGE_ID_HANDLERS)
    assert dispatchable == set(SCHEMA_NAMES)
    # And no name is in both dispatch tables, which would make which one runs
    # depend on the order of two `if`s in `execute`.
    assert not (set(tools._HANDLERS) & set(tools._MESSAGE_ID_HANDLERS))


def test_every_tool_has_a_student_facing_label():
    """`agent.TOOL_LABELS` is what the thread and the mid-stream hint print.
    A missing entry falls back to the raw internal name, which the system
    prompt explicitly forbids showing ("never `get_my_firms`")."""
    assert set(agent.TOOL_LABELS) == set(SCHEMA_NAMES)
    for name, label in agent.TOOL_LABELS.items():
        assert label and label != name
        assert "_" not in label, f"{name}'s label reads like an identifier"


def test_write_tools_are_all_real_tools():
    assert tools.WRITE_TOOLS <= set(SCHEMA_NAMES)


def test_no_schema_offers_a_user_or_tenant_argument():
    """The tenant rule, restated as a property of the SCHEMAS rather than of
    the bodies: `user` is a parameter of `execute`, and there must be no
    field anywhere the model could use to name somebody else."""
    banned = {"user", "user_id", "student", "student_id", "owner", "account",
              "account_id", "tenant", "email_of"}
    for schema in tools.TOOL_SCHEMAS:
        fields = set(schema["input_schema"]["properties"])
        assert not (fields & banned), schema["name"]
        assert schema["input_schema"]["additionalProperties"] is False, schema["name"]


# ===========================================================================
# INVARIANT 2 — `WRITE_TOOLS` is exactly the set of tools that write.
#
# The set with teeth. `agent.stream_turn` announces a call mid-stream ONLY
# when the name is NOT in `WRITE_TOOLS`; both loops record `assistant_write`
# ONLY when it is. A write missing from the set is therefore a change made
# to the student's CRM, announced to them as a lookup, with no write event
# behind it. Checked against behaviour — a real call, rows counted either
# side — never against a second hand-maintained list.
# ===========================================================================
@pytest.mark.parametrize("name", READ_NAMES)
def test_no_tool_outside_write_tools_writes_anything(world, name):
    user = world["user"]
    args = {
        "search_contacts": {"query": "Jane"},
        "get_contact": {"contact_id": world["contact"].id},
        "search_opportunities": {"query": "Analyst"},
        "get_firm": {"name_or_slug": "north-bank"},
        "date_facts": {"query": "thanksgiving"},
    }.get(name, {})
    before = _snapshot(user)
    payload, is_error = tools.execute(user, name, args, message_id="msg_1")
    assert not is_error, payload
    user.refresh_from_db()
    assert _snapshot(user) == before, f"{name} is a read tool and it wrote"


# `transaction=True` wherever `crm.services.log_touch` / `set_contact_state`
# are reached: both commit on their own psycopg connection, OUTSIDE Django's
# test transaction, so a rolled-back test would not see the row they wrote.
# Same reasoning (and the same marker) as `test_tools.py`'s own log_touch
# tests.
@pytest.mark.django_db(transaction=True)
def test_each_write_tool_actually_writes(world):
    """The other direction: a name declared a write must be able to make one.
    A tool listed in `WRITE_TOOLS` that cannot write is a tool the stream
    silently refuses to announce for no reason."""
    user, contact, opp = world["user"], world["contact"], world["opportunity"]
    calls = {
        "log_touch": {"contact_id": contact.id, "kind": "chat", "channel": "coffee_chat"},
        "track_opportunity": {"opportunity_id": opp.id, "status": "clear"},
        "remember": {"fact": "Targeting HK over US."},
        "add_calendar_event": {"title": "Superday", "date": "2026-12-01"},
        "add_contact": {"name": "Marcus Lee", "firm_text": "Evercore", "role": "Associate"},
        "set_contact_status": {"contact_ids": [contact.id], "thread_state": "parked"},
        "update_settings": {"field": "school", "value": "USC"},
    }
    assert set(calls) == tools.WRITE_TOOLS, "a write tool has no coverage here"
    for name, args in calls.items():
        before = _snapshot(user)
        payload, is_error = tools.execute(user, name, args, message_id="msg_w")
        assert not is_error, (name, payload)
        user.refresh_from_db()
        assert _snapshot(user) != before, f"{name} is declared a write and wrote nothing"


# ===========================================================================
# INVARIANT 3 — `execute` is total. Its docstring promises "never raises";
# a tool that 500s costs the student the whole message they paid for.
# ===========================================================================
@pytest.mark.parametrize("name", SCHEMA_NAMES)
@pytest.mark.parametrize("args", JUNK_ARGS)
def test_execute_never_raises_over_the_whole_tool_x_junk_cross_product(world, name, args):
    payload, is_error = tools.execute(world["user"], name, args, message_id="msg_junk")
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    if is_error:
        # An error is a message for the model to read, not a stack trace.
        assert parsed.get("error")
        assert "Traceback" not in parsed["error"]


def test_an_unknown_tool_name_is_an_error_not_a_crash(world):
    for name in ("", "send_email", "log_touch ", "LOG_TOUCH", "__init__", "execute"):
        payload, is_error = tools.execute(world["user"], name, {}, message_id="m")
        assert is_error, name
        assert "error" in json.loads(payload)


@pytest.mark.parametrize("name", sorted(tools.WRITE_TOOLS))
@pytest.mark.parametrize("args", JUNK_ARGS)
def test_a_write_tool_handed_junk_writes_nothing(world, name, args):
    """The failure mode worth naming: a write that half-succeeds. A malformed
    call must leave the account exactly as it found it, not apply the fields
    it managed to parse before hitting the bad one."""
    user = world["user"]
    before = _snapshot(user)
    payload, is_error = tools.execute(user, name, args, message_id="msg_junk")
    user.refresh_from_db()
    if is_error:
        assert _snapshot(user) == before, (name, args, payload)


# ===========================================================================
# INVARIANT 4 — the two settings tiers. An IMPORTANT field must write nothing
# on an unconfirmed call, whatever else is true about the call.
# ===========================================================================
@pytest.mark.parametrize("field", sorted(tools.SETTINGS_IMPORTANT))
@pytest.mark.parametrize("confirmed", [None, False, "", 0, "false"])
def test_an_important_setting_never_applies_without_a_real_confirmation(
    world, field, confirmed
):
    user = world["user"]
    value = {"timezone": "America/New_York", "regions": "us",
             "tracks": "st", "target_cycles": ""}[field]
    args = {"field": field, "value": value}
    if confirmed is not None:
        args["confirmed"] = confirmed
    before = _snapshot(user)
    payload, is_error = tools.execute(user, field and "update_settings", args,
                                      message_id="m")
    user.refresh_from_db()
    if confirmed in (None, False, "", 0):
        # Falsy: the handshake has not happened, so nothing may move.
        assert is_error, (field, confirmed, payload)
        assert _snapshot(user) == before
        assert "NOT CHANGED" in json.loads(payload)["error"]


def test_every_important_field_carries_the_sentence_the_model_must_say(world):
    """The refusal is only useful if it tells the model WHAT to tell the
    student. A field in the important set with no effect copy would raise a
    KeyError out of the refusal path instead of refusing."""
    for field in tools.SETTINGS_IMPORTANT:
        assert tools._IMPORTANT_EFFECTS.get(field), field
    for field in tools.SETTINGS_IMPORTANT:
        payload, is_error = tools.execute(
            world["user"], "update_settings", {"field": field, "value": ""},
            message_id="m",
        )
        assert is_error
        assert tools._IMPORTANT_EFFECTS[field] in json.loads(payload)["error"]


# ===========================================================================
# INVARIANT 5 — the settings allowlist is a real fence. Credentials,
# entitlements and identity are not recruiting preferences, and the tool's
# own module docstring lists them by name as deliberately absent.
# ===========================================================================
@pytest.mark.parametrize("field", [
    "email", "password", "is_staff", "is_superuser", "plan", "google_sub",
    "calendar_token", "avatar", "language", "timezone_auto", "id", "pk",
    "credits", "weekly_digest_opt_out",
])
def test_the_settings_tool_refuses_every_field_outside_its_allowlist(world, field):
    user = world["user"]
    before = _snapshot(user)
    payload, is_error = tools.execute(
        user, "update_settings", {"field": field, "value": "x"}, message_id="m"
    )
    assert is_error, field
    user.refresh_from_db()
    assert _snapshot(user) == before
    assert field not in tools.SETTINGS_FIELDS


def test_the_schema_enum_and_the_body_allowlist_are_the_same_list():
    """Two copies of an allowlist is how one of them drifts. The schema's
    enum is what the model is allowed to ask for; `SETTINGS_FIELDS` is what
    the body will do. They must not be able to disagree."""
    schema = next(t for t in tools.TOOL_SCHEMAS if t["name"] == "update_settings")
    assert schema["input_schema"]["properties"]["field"]["enum"] == list(
        tools.SETTINGS_FIELDS
    )


# ===========================================================================
# INVARIANT 6 — the bulk write's cap and its honesty. `set_contact_status` is
# the only tool that touches several rows on one call, so it is the only one
# that can half-succeed and report "done".
# ===========================================================================
def test_the_bulk_cap_refuses_the_whole_call_rather_than_doing_the_first_n(world):
    user, contact = world["user"], world["contact"]
    ids = [contact.id] * 1 + list(range(9000, 9000 + tools.MAX_BULK_CONTACTS))
    before = _snapshot(user)
    payload, is_error = tools.execute(
        user, "set_contact_status", {"contact_ids": ids, "thread_state": "parked"},
        message_id="m",
    )
    assert is_error
    user.refresh_from_db()
    assert _snapshot(user) == before


@pytest.mark.django_db(transaction=True)
def test_ids_that_are_not_this_students_are_reported_not_silently_dropped(world):
    user, contact = world["user"], world["contact"]
    payload, is_error = tools.execute(
        user, "set_contact_status",
        {"contact_ids": [contact.id, 999_001, 999_002], "warmth": "advocate"},
        message_id="m",
    )
    assert not is_error
    body = json.loads(payload)
    assert body["changed_count"] == 1
    assert sorted(body["not_found"]) == [999_001, 999_002]
    assert body.get("instruction"), "the model needs to be told not to claim the rest"


@pytest.mark.django_db(transaction=True)
def test_a_repeated_id_is_one_change_not_three(world):
    user, contact = world["user"], world["contact"]
    payload, is_error = tools.execute(
        user, "set_contact_status",
        {"contact_ids": [contact.id, contact.id, contact.id], "thread_state": "parked"},
        message_id="m",
    )
    assert not is_error
    assert json.loads(payload)["changed_count"] == 1


# ===========================================================================
# INVARIANT 7 — every write leaves a trail the student can find. The two
# tools that move a relationship stamp the assistant message id into the row
# they leave behind; a write the student cannot attribute is a write they
# cannot argue with.
# ===========================================================================
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("name,args", [
    ("log_touch", {"kind": "chat", "channel": "coffee_chat"}),
    ("set_contact_status", {"thread_state": "parked"}),
])
def test_a_relationship_write_is_attributable_to_the_message_that_made_it(
    world, name, args
):
    user, contact = world["user"], world["contact"]
    call = dict(args)
    if name == "log_touch":
        call["contact_id"] = contact.id
    else:
        call["contact_ids"] = [contact.id]
    _, is_error = tools.execute(user, name, call, message_id="msg_01ATTRIB")
    assert not is_error
    notes = list(
        Touch.objects.for_user(user).filter(contact=contact).values_list("note", flat=True)
    )
    assert any("[assistant:msg_01ATTRIB]" in (n or "") for n in notes)


def test_a_contact_the_advisor_added_is_marked_as_the_advisors(world):
    user = world["user"]
    _, is_error = tools.execute(
        user, "add_contact", {"name": "Marcus Lee", "firm_text": "Evercore"},
        message_id="m",
    )
    assert not is_error
    added = Contact.objects.for_user(user).get(name="Marcus Lee")
    assert added.source == "assistant"
    assert ProductEvent.objects.for_user(user).filter(event="contact_added").exists()


# ===========================================================================
# INVARIANT 8 — untrusted text never reaches the model raw. Notes, roles and
# titles were written by other people; every one is capped, and this app's
# own bookkeeping markers are stripped.
# ===========================================================================
def test_a_notes_bookkeeping_marker_never_reaches_the_model(world):
    payload, is_error = tools.execute(
        world["user"], "get_contact", {"contact_id": world["contact"].id},
        message_id="m",
    )
    assert not is_error
    blob = payload
    for marker in ("[gmail:", "[capture:", "[assistant:", "manual override:"):
        assert marker not in blob, marker


def test_every_untrusted_string_is_capped_before_it_reaches_the_model(world):
    user, contact = world["user"], world["contact"]
    # `role`/`name`/`firm_text` are varchar(255) and cannot exceed the cap
    # in the database at all; `angle` and `notes` are TextFields, so they are
    # the ones where an uncapped write would actually reach the model.
    contact.role = "R" * 255
    contact.angle = "A" * 5000
    contact.notes = "N" * 5000
    contact.save(update_fields=["role", "angle", "notes"])
    payload, _ = tools.execute(user, "get_contact", {"contact_id": contact.id},
                               message_id="m")
    body = json.loads(payload)
    for key in ("role", "student_note_about_them", "name", "firm"):
        assert len(body.get(key) or "") <= tools.MAX_STR
    assert "A" * (tools.MAX_STR + 1) not in payload
    assert "N" * (tools.MAX_STR + 1) not in payload


def test_a_row_cap_is_the_codes_decision_not_the_models(world):
    """`limit` is a hint. Whatever the model asks for, the answer is bounded."""
    for asked in (0, -5, 1, 25, 1000, "many", None, 10**9):
        assert 1 <= tools._limit(asked) <= tools.MAX_ROWS
