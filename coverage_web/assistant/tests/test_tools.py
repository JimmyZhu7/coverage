"""Happy-path behaviour for every tool the advisor can call.

Tenant isolation for the same set lives in `test_isolation.py` — that is the
security test and it is parametrized over every tool; this file is about
whether each one returns the right SHAPE and the right facts.

Nothing here touches the network: no test in this package constructs an
Anthropic client. `tools.execute` is a pure function of (user, name, args).

Rows are created with the plain model constructor plus `.save()` rather than
through the unscoped manager, which is the idiom the rest of the repo's tests
use. That is not stylistic: `test_isolation.py` fails the build if that
manager's name appears anywhere in this package, tests included, and a test
file quietly holding the escape hatch would make the check a formality.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from analytics.models import UserOpportunity
from assistant import tools
from assistant.models import AdvisorMemory
from crm.models import CalendarEvent, Contact, Touch, UserFirm
from directory.models import Firm, FirmDate, Opportunity

User = get_user_model()

pytestmark = pytest.mark.django_db


def _call(user, name, args=None, message_id="msg_test"):
    payload, is_error = tools.execute(user, name, args or {}, message_id)
    return json.loads(payload), is_error


@pytest.fixture
def user():
    return User.objects.create_user(
        email="student@example.com", password="x", name="Sam", school="HKU", class_year=2028
    )


@pytest.fixture
def firm():
    return Firm.objects.create(
        slug="north-bank", name="North Bank", regions=["hk"], tracks=["ibd"]
    )


@pytest.fixture
def contact(user, firm):
    c = Contact(user=user, firm=firm, name="Jane Banker", role="VP, IBD", region="hk")
    c.save()
    return c


@pytest.fixture
def opportunity(firm):
    return Opportunity.objects.create(
        firm=firm,
        title="2028 Summer Analyst, Hong Kong",
        bucket="internship",
        region="hk",
        location="Hong Kong",
        status="open",
        deadline=timezone.localdate() + timedelta(days=10),
        url="https://north.example/jobs/1",
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def test_today_queue_returns_the_cadence_engine_s_own_rows(user, contact):
    """The queue is `crm.today._build_actions` wrapped, not re-derived — a
    brand-new cold contact is a first_outreach in both places."""
    result, is_error = _call(user, "get_today_queue")

    assert not is_error
    assert result["today"] == timezone.localdate().isoformat()
    assert [r["contact_id"] for r in result["queue"]] == [contact.id]
    row = result["queue"][0]
    assert row["action"] == "first_outreach"
    assert row["contact"] == "Jane Banker"
    assert row["firm"] == "North Bank"
    assert row["reason"]


def test_search_contacts_matches_name_firm_and_role_and_always_returns_ids(user, contact, firm):
    other = Contact(user=user, name="Bob Trader", firm_text="South Bank", role="Analyst")
    other.save()

    by_name, _ = _call(user, "search_contacts", {"query": "jane"})
    assert [c["contact_id"] for c in by_name["contacts"]] == [contact.id]

    by_firm, _ = _call(user, "search_contacts", {"query": "north"})
    assert [c["contact_id"] for c in by_firm["contacts"]] == [contact.id]

    by_role, _ = _call(user, "search_contacts", {"query": "analyst"})
    assert [c["contact_id"] for c in by_role["contacts"]] == [other.id]


def test_search_contacts_flags_ambiguity_in_the_result_not_only_the_prompt(user, firm):
    """The disambiguation rule has to reach the model at the moment it is
    about to pick, so it rides in the payload."""
    for name in ("Alice Chen", "Andy Chen"):
        c = Contact(user=user, firm=firm, name=name)
        c.save()

    result, _ = _call(user, "search_contacts", {"query": "chen"})

    assert result["total_matches"] == 2
    assert result["ambiguous"] is True
    assert "ask the student" in result["instruction"].lower()
    assert "do not choose" in result["instruction"].lower()


def test_search_contacts_single_match_is_not_flagged_ambiguous(user, contact):
    result, _ = _call(user, "search_contacts", {"query": "jane"})
    assert "ambiguous" not in result


def test_search_contacts_limit_is_capped_by_code(user, firm):
    for i in range(30):
        c = Contact(user=user, firm=firm, name=f"Person {i:02d}")
        c.save()

    result, _ = _call(user, "search_contacts", {"query": "person", "limit": 500})

    assert result["total_matches"] == 30
    assert result["shown"] == tools.MAX_ROWS


def test_get_contact_carries_history_tier_and_a_stripped_note(user, contact, firm):
    UserFirm(user=user, firm=firm, tier=1).save()
    t = Touch(
        user=user,
        contact=contact,
        ts=timezone.now(),
        kind="chat",
        channel="coffee_chat",
        note="[assistant:msg_1] Talked about the HK team",
    )
    t.save()

    result, is_error = _call(user, "get_contact", {"contact_id": contact.id})

    assert not is_error
    assert result["firm_tier"] == 1
    assert result["firm"] == "North Bank"
    history = result["recent_interactions"]
    assert len(history) == 1
    # The bookkeeping marker is machinery — the model gets the human words.
    assert history[0]["note"] == "Talked about the HK team"
    assert history[0]["kind"] == "Chat happened"


def test_get_contact_truncates_an_over_long_note(user, contact):
    t = Touch(
        user=user,
        contact=contact,
        ts=timezone.now(),
        kind="outreach",
        channel="email",
        note="x" * 5000,
    )
    t.save()

    result, _ = _call(user, "get_contact", {"contact_id": contact.id})

    assert len(result["recent_interactions"][0]["note"]) == tools.MAX_STR


def test_get_contact_caps_history_at_eight_touches(user, contact):
    now = timezone.now()
    for i in range(12):
        t = Touch(
            user=user, contact=contact, ts=now - timedelta(days=i), kind="maintain", channel="email"
        )
        t.save()

    result, _ = _call(user, "get_contact", {"contact_id": contact.id})

    assert len(result["recent_interactions"]) == 8


def test_search_opportunities_filters_region_firm_and_closing_window(user, firm, opportunity):
    us_firm = Firm.objects.create(slug="west-co", name="West Co")
    Opportunity.objects.create(
        firm=us_firm,
        title="2028 Summer Analyst, New York",
        bucket="internship",
        region="us",
        status="open",
        deadline=timezone.localdate() + timedelta(days=120),
        url="https://west.example/jobs/1",
    )

    everything, _ = _call(user, "search_opportunities", {})
    assert everything["total_matches"] == 2

    hk, _ = _call(user, "search_opportunities", {"region": "hk"})
    assert [r["opportunity_id"] for r in hk["roles"]] == [opportunity.id]

    by_firm, _ = _call(user, "search_opportunities", {"firm": "west"})
    assert [r["firm"] for r in by_firm["roles"]] == ["West Co"]

    soon, _ = _call(user, "search_opportunities", {"closing_within_days": 30})
    assert [r["opportunity_id"] for r in soon["roles"]] == [opportunity.id]
    assert soon["roles"][0]["days_left"] == 10


def test_search_opportunities_excludes_closed_and_non_campus_rows(user, firm):
    Opportunity.objects.create(
        firm=firm, title="Closed role", bucket="internship", status="closed",
        url="https://north.example/jobs/closed",
    )
    Opportunity.objects.create(
        firm=firm, title="Experienced hire", bucket="other", status="open",
        url="https://north.example/jobs/exp",
    )

    result, _ = _call(user, "search_opportunities", {})

    assert result["total_matches"] == 0


def test_search_opportunities_hides_roles_this_student_dismissed(user, opportunity):
    uo = UserOpportunity(user=user, opportunity=opportunity, dismissed=True)
    uo.save()

    result, _ = _call(user, "search_opportunities", {})

    assert result["total_matches"] == 0


def test_get_firm_reports_tier_dates_open_roles_and_my_people_by_warmth(user, firm, opportunity):
    UserFirm(user=user, firm=firm, tier=2).save()
    FirmDate.objects.create(
        firm=firm,
        cycle="2028",
        region="hk",
        event_kind="applications_open",
        date=timezone.localdate() + timedelta(days=5),
        confidence=0.8,
    )
    cold = Contact(user=user, firm=firm, name="Cold Person", warmth="cold")
    cold.save()
    warm = Contact(user=user, firm=firm, name="Warm Person", warmth="advocate")
    warm.save()

    result, is_error = _call(user, "get_firm", {"name_or_slug": "north bank"})

    assert not is_error
    assert result["my_tier"] == 2
    assert result["open_roles"] == 1
    assert [d["event"] for d in result["upcoming_dates"]] == ["applications_open"]
    assert [c["name"] for c in result["my_contacts"]] == ["Warm Person", "Cold Person"]


def test_get_firm_by_slug_and_unknown_firm_is_an_error_not_a_crash(user, firm):
    by_slug, is_error = _call(user, "get_firm", {"name_or_slug": "north-bank"})
    assert not is_error
    assert by_slug["slug"] == "north-bank"

    missing, is_error = _call(user, "get_firm", {"name_or_slug": "nowhere plc"})
    assert is_error
    assert "error" in missing


def test_get_my_firms_reports_every_tiered_firm_with_coverage_at_each(user, firm):
    other_firm = Firm.objects.create(slug="south-bank", name="South Bank")
    untiered_firm = Firm.objects.create(slug="untiered", name="Untiered Co")
    UserFirm(user=user, firm=firm, tier=1).save()
    UserFirm(user=user, firm=other_firm, tier=2).save()
    UserFirm(user=user, firm=untiered_firm, tier=None).save()  # not a target — no tier set
    Contact(user=user, firm=firm, name="Cold One", warmth="cold").save()
    Contact(user=user, firm=firm, name="Warm One", warmth="advocate").save()
    Opportunity.objects.create(
        firm=firm, title="SA", bucket="internship", status="open", url="https://x.example/1"
    )
    Opportunity.objects.create(
        firm=firm, title="Closed role", bucket="internship", status="closed", url="https://x.example/2"
    )

    result, is_error = _call(user, "get_my_firms")

    assert not is_error
    firms = {f["firm"]: f for f in result["firms"]}
    assert set(firms) == {"North Bank", "South Bank"}  # the untiered firm is not a target
    assert firms["North Bank"]["tier"] == 1
    assert firms["North Bank"]["contact_count"] == 2
    assert firms["North Bank"]["warmest_contact"] == "advocate"
    assert firms["North Bank"]["open_roles"] == 1  # the closed role does not count
    assert firms["South Bank"]["contact_count"] == 0
    assert firms["South Bank"]["warmest_contact"] is None
    # Tier order, not alphabetical — the student's own priority.
    assert [f["firm"] for f in result["firms"]] == ["North Bank", "South Bank"]


def test_get_my_firms_with_no_targets_is_a_plain_empty_result(user):
    result, is_error = _call(user, "get_my_firms")

    assert not is_error
    assert result["firms"] == []
    assert "note" in result


def test_get_calendar_returns_upcoming_events_only(user, contact):
    now = timezone.now()
    soon = CalendarEvent(
        user=user, title="Chat with Jane", starts_at=now + timedelta(days=2),
        kind="chat", contact=contact,
    )
    soon.save()
    far = CalendarEvent(user=user, title="Superday", starts_at=now + timedelta(days=90))
    far.save()
    past = CalendarEvent(user=user, title="Last week", starts_at=now - timedelta(days=7))
    past.save()

    result, is_error = _call(user, "get_calendar", {"days_ahead": 14})

    assert not is_error
    assert [e["title"] for e in result["events"]] == ["Chat with Jane"]
    assert result["events"][0]["with_contact"] == "Jane Banker"


def test_get_my_pipeline_groups_by_status(user, firm, opportunity):
    second = Opportunity.objects.create(
        firm=firm, title="Spring Week", bucket="insight", status="open",
        url="https://north.example/jobs/2",
    )
    saved = UserOpportunity(user=user, opportunity=opportunity)
    saved.save()
    applied = UserOpportunity(user=user, opportunity=second, applied_status="submitted")
    applied.save()

    result, is_error = _call(user, "get_my_pipeline")

    assert not is_error
    assert set(result["by_status"]) == {"saved", "submitted"}
    assert result["by_status"]["saved"][0]["opportunity_id"] == opportunity.id
    assert result["by_status"]["saved"][0]["days_left"] == 10


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_log_touch_writes_source_assistant_and_the_marker():
    """`crm.services.log_touch` commits on its own psycopg connection outside
    Django's transaction, so this one needs `transaction=True` (see
    crm/tests/test_services.py's docstring for the full reasoning)."""
    from crm.views import _display_note

    u = User.objects.create_user(email="tx@example.com", password="x")
    f = Firm.objects.create(slug="tx-bank", name="TX Bank")
    c = Contact(user=u, firm=f, name="Reply Person")
    c.save()

    result, is_error = tools.execute(
        u,
        "log_touch",
        {"contact_id": c.id, "kind": "reply_received", "channel": "email", "note": "She replied"},
        "msg_abc123",
    )
    payload = json.loads(result)

    assert not is_error
    assert payload["logged"] is True
    assert payload["warmth_before"] == "cold"
    assert payload["warmth_after"] == "replied"

    touch = Touch.objects.for_user(u).get(contact=c)
    assert touch.source == "assistant"
    assert touch.note.startswith("[assistant:msg_abc123] ")
    # ...and the marker never reaches the student's own history view.
    assert _display_note(touch.note) == "She replied"


def test_log_touch_rejects_an_unknown_kind_or_channel(user, contact):
    bad_kind, is_error = _call(
        user, "log_touch", {"contact_id": contact.id, "kind": "vibes", "channel": "email"}
    )
    assert is_error and "vibes" in bad_kind["error"]

    bad_channel, is_error = _call(
        user, "log_touch", {"contact_id": contact.id, "kind": "outreach", "channel": "telepathy"}
    )
    assert is_error and "telepathy" in bad_channel["error"]

    assert not Touch.objects.for_user(user).exists()


def test_track_opportunity_saves_then_clears(user, opportunity):
    saved, is_error = _call(
        user, "track_opportunity", {"opportunity_id": opportunity.id, "status": "saved"}
    )
    assert not is_error
    assert saved["saved"] is True
    row = UserOpportunity.objects.for_user(user).get(opportunity=opportunity)
    assert row.applied_status == ""
    assert row.dismissed is False

    cleared, is_error = _call(
        user, "track_opportunity", {"opportunity_id": opportunity.id, "status": "clear"}
    )
    assert not is_error
    assert cleared["cleared"] is True
    assert not UserOpportunity.objects.for_user(user).filter(opportunity=opportunity).exists()


def test_track_opportunity_save_is_idempotent(user, opportunity):
    _call(user, "track_opportunity", {"opportunity_id": opportunity.id, "status": "saved"})
    _call(user, "track_opportunity", {"opportunity_id": opportunity.id, "status": "saved"})

    assert UserOpportunity.objects.for_user(user).filter(opportunity=opportunity).count() == 1


def test_track_opportunity_save_never_downgrades_a_funnel_row(user, opportunity):
    """"Save it" on a role the student is already interviewing for must not
    blank the stage back out. The Opportunities feed guards this structurally
    — a funnel row renders a read-only chip, not a Save button — and the tool
    has to hold the same line, because "yeah save that one" said about a role
    the model didn't check is exactly how the state would be lost."""
    row = UserOpportunity(user=user, opportunity=opportunity, applied_status="interview")
    row.save()

    result, is_error = _call(
        user, "track_opportunity", {"opportunity_id": opportunity.id, "status": "saved"}
    )

    row.refresh_from_db()
    assert row.applied_status == "interview"
    assert row.dismissed is False
    # And the model is told, rather than being left to report a save that
    # didn't happen.
    assert not is_error
    assert result["saved"] is False
    assert result["already_tracked"] is True
    assert result["current_status"] == "interview"
    assert result["current_status_label"] == "Interviewing"
    assert "further along" in result["instruction"]


def test_track_opportunity_refuses_any_other_status(user, opportunity):
    """`saved`/`clear` only — the funnel states stay the student's to set."""
    result, is_error = _call(
        user, "track_opportunity", {"opportunity_id": opportunity.id, "status": "offer"}
    )

    assert is_error
    assert not UserOpportunity.objects.for_user(user).exists()


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
def test_every_schema_is_strict_and_closed():
    for schema in tools.TOOL_SCHEMAS:
        assert schema["strict"] is True, schema["name"]
        assert schema["input_schema"]["additionalProperties"] is False, schema["name"]
        assert schema["description"], schema["name"]


def test_no_schema_exposes_a_user_or_tenant_argument():
    """The isolation guarantee starts here: there is no field for the model to
    put a user in, so it cannot ask about anyone but the signed-in student."""
    for schema in tools.TOOL_SCHEMAS:
        for field in schema["input_schema"]["properties"]:
            assert "user" not in field.lower(), (schema["name"], field)
            assert "tenant" not in field.lower(), (schema["name"], field)
            assert "student" not in field.lower(), (schema["name"], field)


def test_every_schema_has_a_handler_and_every_handler_a_schema():
    named = {s["name"] for s in tools.TOOL_SCHEMAS}
    assert named == set(tools._HANDLERS) | {"log_touch"}
    assert tools.WRITE_TOOLS <= named


def test_an_unknown_tool_is_an_error_result_never_an_exception(user):
    result, is_error = _call(user, "delete_everything", {})
    assert is_error
    assert "error" in result


# ---------------------------------------------------------------------------
# remember
# ---------------------------------------------------------------------------
def test_remember_saves_a_fact(user):
    result, is_error = _call(user, "remember", {"fact": "Ruled out PE roles."})

    assert not is_error
    assert result["remembered"] == "Ruled out PE roles."
    assert list(AdvisorMemory.objects.for_user(user).values_list("text", flat=True)) == ["Ruled out PE roles."]


def test_remember_with_no_fact_is_an_error(user):
    result, is_error = _call(user, "remember", {})
    assert is_error
    assert AdvisorMemory.objects.for_user(user).count() == 0


def test_remember_refuses_past_the_cap_without_silently_dropping_anything(user):
    for i in range(tools.MAX_MEMORIES):
        AdvisorMemory(user=user, text=f"fact {i}").save()

    result, is_error = _call(user, "remember", {"fact": "one too many"})

    assert is_error
    assert AdvisorMemory.objects.for_user(user).count() == tools.MAX_MEMORIES  # nothing evicted
    assert "one too many" not in AdvisorMemory.objects.for_user(user).values_list("text", flat=True)
