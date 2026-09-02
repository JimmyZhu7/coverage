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
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

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
def test_today_queue_returns_the_cadence_engine_s_own_rows(user, contact, firm):
    """The queue is `crm.today._build_actions` wrapped, not re-derived — a
    brand-new cold contact is a first_outreach in both places.

    The `UserFirm` is part of that reuse rather than incidental setup: the
    queue gained a relevance gate (`crm.relevance`) and the advisor inherits
    it, deliberately. An assistant that reads out people the student does not
    target is the same failure as a page that does."""
    UserFirm(user=user, firm=firm, tier=1).save()
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


def test_search_contacts_last_touch_field_is_named_and_computed_in_calendar_days(user, firm):
    """Cross-surface consistency audit, finding B: `assistant.tools` handed
    the model two "how long ago" numbers under near-identical names —
    `days_idle` (business days, `crm.today`'s own reasoning) and
    `last_touch_days` (calendar days via a local `_days_since`) — with
    neither name saying which was which. This pins the renamed field
    (`last_touch_calendar_days`, the old name must be gone) and that
    `_days_since` now routes through `crm.utils._calendar_days_ago` instead
    of its own raw UTC floor, so a contact's last touch cannot read a
    different "how long ago" here than it does on the Today page for the
    identical timestamp. Same fixture shape as
    `directory.tests.test_seen_days_timezone` (the sibling fix for the
    feed)."""
    HK = ZoneInfo("Asia/Hong_Kong")
    # 2026-08-27 20:00 UTC is 2026-08-28 04:00 in Hong Kong.
    NOW = datetime(2026, 8, 27, 20, 0, tzinfo=ZoneInfo("UTC"))
    # 2026-08-17 15:00 UTC is 2026-08-17 23:00 in Hong Kong — 5 hours before
    # HK midnight. Raw UTC elapsed time is 10 days 5 hours (floors to 10);
    # the LOCAL calendar-date difference (Aug 28 minus Aug 17) is 11.
    LAST_TOUCH = datetime(2026, 8, 17, 15, 0, tzinfo=ZoneInfo("UTC"))

    c = Contact(user=user, firm=firm, name="Jane Banker", region="hk")
    c.save()
    Touch(user=user, contact=c, kind="email", channel="email", ts=LAST_TOUCH).save()

    timezone.activate(HK)
    try:
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            result, is_error = _call(user, "search_contacts", {"query": "jane"})
    finally:
        timezone.deactivate()

    assert not is_error
    row = result["contacts"][0]
    assert "last_touch_days" not in row
    # 11, the local-calendar answer — not 10, the raw UTC floor the old
    # name and old implementation would have given for this exact instant.
    assert row["last_touch_calendar_days"] == 11


def test_get_today_queue_labels_its_idle_count_as_business_days(user, contact, firm):
    """The sibling half of finding B: `days_idle` (BUSINESS days — the
    cadence engine's own reasoning, `crm.today._build_actions`'
    `last_business_days`) renamed to `business_days_idle` so it cannot be
    mistaken for `last_touch_calendar_days` (CALENDAR days) above. This is a
    deliberately different question from the calendar-day count — see
    `crm.today`'s own business-day cadence math — so only the NAME changes
    here, not the value."""
    UserFirm(user=user, firm=firm, tier=1).save()
    result, is_error = _call(user, "get_today_queue")

    assert not is_error
    assert result["queue"]
    row = result["queue"][0]
    assert "days_idle" not in row
    assert "business_days_idle" in row


def test_get_today_queue_says_which_lane_each_row_is_in(user, firm):
    """Every row says WHERE the Today page puts it, and a paced row names the
    firm holding it back.

    The payload used to carry neither, so the model could not tell a card in
    the day's plan from one the cap or a firm's own daily budget is holding,
    and could not answer "why is X not on today's plan" at all. Six contacts
    at one bank is `FIRM_DAILY_CONTACT_CAP`'s own fixture shape: two get a
    plan slot, the other four pace out under "Up next" and say whose budget
    is spent.

    `lane` is read off `crm.today.plan_split`, not derived here — the tool
    and the page cannot disagree about a card the student can see.
    """
    from crm.today import plan_split  # noqa: F401 — named so the seam is greppable

    UserFirm(user=user, firm=firm, tier=1).save()
    for i in range(6):
        Contact(user=user, firm=firm, name=f"Banker {i}", region="hk").save()

    result, is_error = _call(user, "get_today_queue")

    assert not is_error
    rows = result["queue"]
    assert len(rows) == 6
    assert all(r["lane"] in {"today", "up_next", "still_open", "park"} for r in rows)
    # Nobody here is stale-critical or parkable, so the split is exactly the
    # two lanes the plan and the cap produce.
    assert {r["lane"] for r in rows} == {"today", "up_next"}

    paced = [r for r in rows if r["paced_by"]]
    assert len(paced) == 4
    assert all(r["paced_by"] == "North Bank (HK)" for r in paced)
    assert all(r["lane"] == "up_next" for r in paced)
    # A row the pace is not holding says so with a null, never a blank string.
    assert all(r["paced_by"] is None for r in rows if r["lane"] == "today")


def test_get_today_queue_tool_description_names_the_lane_and_pacing_fields():
    """A field the model is never told about is a field it will not use. Both
    names appear in the description the API actually receives."""
    schema = next(s for s in tools.TOOL_SCHEMAS if s["name"] == "get_today_queue")

    assert "lane" in schema["description"]
    assert "paced_by" in schema["description"]
    for value in ("today", "up_next", "still_open"):
        assert value in schema["description"]


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


def test_get_contact_keeps_the_ai_summary_in_its_own_key(user, contact):
    """The AI-written relationship note (crm.ai_summary) and the student's own
    note about the same person are two different authorships, so they are two
    different keys. Merging them would leave nothing in the payload able to
    say which sentence came from whom."""
    contact.angle = "STUDENT WROTE THIS"
    contact.ai_summary = "THE MODEL WROTE THIS"
    contact.ai_summary_generated_at = timezone.now()
    contact.save()

    result, is_error = _call(user, "get_contact", {"contact_id": contact.id})

    assert not is_error
    assert result["student_note_about_them"] == "STUDENT WROTE THIS"
    assert result["advisor_summary"] == "THE MODEL WROTE THIS"
    assert result["advisor_summary_written"] is not None
    # Neither field absorbed the other, in either direction.
    assert "THE MODEL WROTE THIS" not in result["student_note_about_them"]
    assert "STUDENT WROTE THIS" not in result["advisor_summary"]


def test_get_contact_reports_an_absent_ai_summary_as_blank_and_undated(user, contact):
    result, _ = _call(user, "get_contact", {"contact_id": contact.id})
    assert result["advisor_summary"] == ""
    assert result["advisor_summary_written"] is None


def test_get_contact_never_writes_an_ai_summary(user, contact):
    """Reading a contact through the advisor is a read. Generation is the
    student's own POST on the contact page (crm.views.contact_ai_summary)."""
    for i in range(4):
        Touch(
            user=user, contact=contact, ts=timezone.now() - timedelta(days=i),
            kind="outreach", channel="email", note=f"note {i}",
        ).save()

    _call(user, "get_contact", {"contact_id": contact.id})

    contact.refresh_from_db()
    assert contact.ai_summary == ""
    assert contact.ai_summary_generated_at is None


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
        cycle="sa2028",
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


def test_get_firm_dates_carry_their_precision_alongside_confidence(user, firm):
    """`confidence` alone is not the whole claim about a firm date — a
    `precision="estimated"` row is a month-level GUESS extrapolated from
    past cycles, not a day the firm stated, however high its `confidence`
    reads (see `crm.utils.firm_date_confidence`'s identical two-part bar).
    Before this fix the advisor tool exposed `confidence` alone, which could
    lead it to state a specific day off a row the firm page itself only
    ever prints as "~ Sep 2027"."""
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_open",
        date=timezone.localdate() + timedelta(days=5),
        confidence=0.6, precision="estimated",
    )
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        date=timezone.localdate() + timedelta(days=20),
        confidence=1.0, precision="",
    )

    result, is_error = _call(user, "get_firm", {"name_or_slug": "north bank"})

    assert not is_error
    by_event = {d["event"]: d for d in result["upcoming_dates"]}
    assert by_event["app_open"]["precision"] == "estimated"
    assert by_event["app_close"]["precision"] == "day"


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


def test_get_my_firms_counts_parked_contacts_beside_coverage_not_inside_it(user, firm):
    """A parked contact is a thread the student has stopped working, and
    `contact_count` counted them as coverage. Measured on the founder's
    account, 2026-09-01: 219 contacts at his tiered firms, 89 of them not
    parked; Nomura read 13 and had 1 live thread; Macquarie, Standard
    Chartered, Amazon, BCG and Blackstone were 100% parked and read as
    covered. This tool's whole job is "across my targets, where am I
    thinnest".

    `contact_count` is UNCHANGED on purpose — silently redefining a number
    the model has been reading is its own kind of lie — so the honest one is
    added beside it, and `warmest_active_contact` with it: a firm whose only
    `chatted` contact is parked is not a firm with a warm relationship."""
    UserFirm(user=user, firm=firm, tier=1).save()
    Contact(user=user, firm=firm, name="Live One", warmth="cold").save()
    Contact(user=user, firm=firm, name="Parked Warm", warmth="chatted",
            thread_state="parked").save()
    Contact(user=user, firm=firm, name="Quiet One", warmth="replied",
            thread_state="quiet").save()

    result, is_error = _call(user, "get_my_firms")

    assert not is_error
    row = {f["firm"]: f for f in result["firms"]}["North Bank"]
    assert row["contact_count"] == 3
    assert row["parked_count"] == 2
    assert row["active_count"] == 1
    # The warmest person on file is parked; the warmest LIVE thread is cold.
    assert row["warmest_contact"] == "chatted"
    assert row["warmest_active_contact"] == "cold"


def test_get_my_firms_says_which_number_means_coverage(user):
    """The counts are useless if the model cannot tell which one answers the
    question. A schema-text test, deliberately: the description is the only
    thing the model reads before it picks a number."""
    schema = {t["name"]: t for t in tools.TOOL_SCHEMAS}["get_my_firms"]
    assert "active_count" in schema["description"]
    assert "parked" in schema["description"]


def test_get_firm_counts_parked_contacts_off_the_database_not_the_capped_list(user, firm):
    """`my_contacts` is capped at MAX_ROWS, so a count taken from it would be
    wrong at exactly the firms where coverage matters most — the founder has
    26 contacts at Morgan Stanley and the slice shows 25. The three counts
    come from the database."""
    for i in range(tools.MAX_ROWS + 3):
        Contact(user=user, firm=firm, name=f"Parked {i:02d}", warmth="cold",
                thread_state="parked").save()
    Contact(user=user, firm=firm, name="Live One", warmth="replied").save()

    result, is_error = _call(user, "get_firm", {"name_or_slug": "north bank"})

    assert not is_error
    assert len(result["my_contacts"]) == tools.MAX_ROWS  # the list is capped
    assert result["my_contacts_total"] == tools.MAX_ROWS + 4  # the count is not
    assert result["my_contacts_parked"] == tools.MAX_ROWS + 3
    assert result["my_contacts_active"] == 1


def test_search_contacts_last_touch_ignores_the_kinds_the_queue_ignores(user, firm):
    """`last_touch_calendar_days` took the max over EVERY touch kind, so a
    park (which writes a `manual_override` audit row) or an inbound bulk send
    read back to the advisor as contact. Measured on the founder's account,
    2026-09-01: 165 of 265 live contacts had a clock-silent kind as their
    most recent row, 44 of them parked that evening and therefore reading
    "last touch 0 days" while the Today page correctly called them idle.

    The definition is imported from `coverage_domain.cadence`, not restated
    here — this test pins that the tool agrees with the engine, and
    `coverage_domain/tests/test_stress_invariants.py` pins the set itself."""
    now = timezone.now()
    c = Contact(user=user, firm=firm, name="Jane Banker")
    c.save()
    Touch(user=user, contact=c, kind="outreach", channel="email",
          ts=now - timedelta(days=20)).save()
    # The student parked them today. An audit row, not an interaction.
    Touch(user=user, contact=c, kind="manual_override", channel="other", ts=now).save()
    # And a newsletter landed an hour ago.
    Touch(user=user, contact=c, kind="bulk_received", channel="email", ts=now).save()

    result, is_error = _call(user, "search_contacts", {"query": "jane"})

    assert not is_error
    assert result["contacts"][0]["last_touch_calendar_days"] == 20


def test_search_contacts_reports_no_real_touch_as_null_rather_than_zero(user, firm):
    """A contact whose ONLY rows are clock-silent has never actually been
    contacted, which is a different fact from "contacted just now" and from
    "contacted a long time ago". Null, per P1, rather than a number."""
    c = Contact(user=user, firm=firm, name="Jane Banker")
    c.save()
    Touch(user=user, contact=c, kind="bulk_received", channel="email",
          ts=timezone.now()).save()

    result, _ = _call(user, "search_contacts", {"query": "jane"})

    assert result["contacts"][0]["last_touch_calendar_days"] is None


def test_closing_within_days_says_it_returns_reported_deadlines_too(user):
    """The schema promised "Only roles with a stated deadline" and the filter
    has always been any deadline at all: measured 2026-09-01, a 14-day window
    returned 22 `reported` and 3 `stated`.

    REWORDED, NOT NARROWED. Filtering to `stated` would cut the window to a
    handful of rows and hide the only closing signal that exists for most
    postings (P9 — the board cannot see what the ATS does not publish). The
    honest alternative to hiding a date Coverage read itself is labelling it,
    which every row already does through `deadline_source`; the description
    now points at that field instead of promising something else."""
    schema = {t["name"]: t for t in tools.TOOL_SCHEMAS}["search_opportunities"]
    text = schema["input_schema"]["properties"]["closing_within_days"]["description"]
    assert "stated" in text.lower() and "reported" in text.lower()
    assert "deadline_source" in text
    assert "Only roles with a stated deadline" not in text


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


def test_get_calendar_leaves_out_a_cancelled_chat_unless_asked(user, contact):
    """A chat that was called off is not on your afternoon.

    `CalendarEvent.cancelled_at` is set when a METHOD:CANCEL invite retires a
    captured chat, and the row deliberately survives (the model's own comment
    argues why: deleting it makes a ghost the next mailbox sync re-creates).
    Every surface that says what is HAPPENING drops it — `crm.today._schedule`
    and so the day bar and the chat-prep card — and this tool did not. It
    returned the row with `kind: "chat"` and a "Cancelled: " prefix inside the
    title as the only tell, which is a formatting convention and not a field.
    """
    now = timezone.now()
    live = CalendarEvent(
        user=user, title="Chat with Jane", starts_at=now + timedelta(days=2),
        kind="chat", contact=contact,
    )
    live.save()
    dead = CalendarEvent(
        user=user, title="Cancelled: Chat with Bob",
        starts_at=now + timedelta(days=3), kind="chat", cancelled_at=now,
    )
    dead.save()

    default, is_error = _call(user, "get_calendar", {})
    assert not is_error
    assert [e["title"] for e in default["events"]] == ["Chat with Jane"]
    assert default["events"][0]["cancelled"] is False
    assert default["includes_cancelled"] is False

    asked, is_error = _call(user, "get_calendar", {"include_cancelled": True})
    assert not is_error
    assert asked["includes_cancelled"] is True
    by_title = {e["title"]: e for e in asked["events"]}
    assert by_title["Cancelled: Chat with Bob"]["cancelled"] is True
    assert by_title["Chat with Jane"]["cancelled"] is False


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


def test_get_situation_reports_a_moved_deadline_on_a_tracked_role(user, opportunity):
    from directory.models import OpportunityChange

    tracked = UserOpportunity(user=user, opportunity=opportunity)
    tracked.save()
    OpportunityChange.objects.create(
        opportunity=opportunity, field="deadline", old_value="2026-08-01",
        new_value="2026-08-20", stage="reverify", observed_at=timezone.now(),
    )

    result, is_error = _call(user, "get_situation")

    assert not is_error
    assert result["total"] == 1
    event = result["events"][0]
    assert event["kind"] == "deadline_moved"
    assert event["opportunity_id"] == opportunity.id
    assert event["old_deadline"] == "2026-08-01"
    assert event["new_deadline"] == "2026-08-20"


def test_get_situation_is_empty_for_a_student_with_no_changes(user):
    result, is_error = _call(user, "get_situation")

    assert not is_error
    assert result == {"total": 0, "events": []}


# ---------------------------------------------------------------------------
# date_facts — the fix for the advisor stating a wrong calendar date from
# memory ("Labor Day is 1 September 2026" — it's the 7th). Every assertion
# below is checked against the RULE, not a hardcoded date, so these stay
# correct after this year the same way the tool itself does.
# ---------------------------------------------------------------------------
def test_date_facts_with_a_date_reports_weekday_and_days_until(user):
    from datetime import date as _date

    today = timezone.localdate()
    target = today + timedelta(days=15)
    result, is_error = _call(user, "date_facts", {"query": target.isoformat()})

    assert not is_error
    assert result["today"] == today.isoformat()
    assert result["date"] == target.isoformat()
    assert result["days_until"] == 15
    assert result["weekday"] == target.strftime("%A")
    assert _date.fromisoformat(result["date"]) == target


def test_date_facts_days_until_a_past_date_is_negative(user):
    today = timezone.localdate()
    target = today - timedelta(days=3)
    result, is_error = _call(user, "date_facts", {"query": target.isoformat()})

    assert not is_error
    assert result["days_until"] == -3


def test_date_facts_with_no_query_is_an_error(user):
    result, is_error = _call(user, "date_facts", {})

    assert is_error
    assert "error" in result


def test_date_facts_rejects_a_holiday_it_does_not_know(user):
    result, is_error = _call(user, "date_facts", {"query": "arbor_day"})

    assert is_error
    assert "error" in result


def test_date_facts_labor_day_is_the_first_monday_of_september_not_hardcoded(user):
    """The bug this tool exists to fix: the advisor once said Labor Day 2026
    was 1 September (a Tuesday) — it's the 7th, the first Monday. Asserted
    against the RULE (weekday == Monday, month == September, day <= 7) so
    this keeps passing in 2027, 2028, ... without editing a stored date."""
    from datetime import date as _date

    result, is_error = _call(user, "date_facts", {"query": "labor_day"})

    assert not is_error
    labor_day = _date.fromisoformat(result["date"])
    assert labor_day.weekday() == 0  # Monday
    assert labor_day.month == 9
    assert labor_day.day <= 7  # the FIRST Monday, not just any Monday
    assert result["weekday"] == "Monday"
    assert result["days_until"] >= 0  # always the upcoming occurrence


@pytest.mark.parametrize(
    "key,expected_weekday,expected_month",
    [
        ("labor_day", 0, 9),      # 1st Monday of September
        ("mlk_day", 0, 1),        # 3rd Monday of January
        ("memorial_day", 0, 5),   # last Monday of May
        ("thanksgiving", 3, 11),  # 4th Thursday of November
    ],
)
def test_date_facts_us_floating_holidays_land_on_the_right_weekday_and_month(
    user, key, expected_weekday, expected_month
):
    from datetime import date as _date

    result, is_error = _call(user, "date_facts", {"query": key})

    assert not is_error
    d = _date.fromisoformat(result["date"])
    assert d.weekday() == expected_weekday
    assert d.month == expected_month
    assert result["days_until"] >= 0


@pytest.mark.parametrize(
    "key,expected_month,expected_day",
    [("independence_day", 7, 4), ("christmas", 12, 25), ("new_year", 1, 1)],
)
def test_date_facts_us_fixed_date_holidays(user, key, expected_month, expected_day):
    from datetime import date as _date

    result, is_error = _call(user, "date_facts", {"query": key})

    assert not is_error
    d = _date.fromisoformat(result["date"])
    assert (d.month, d.day) == (expected_month, expected_day)
    assert result["days_until"] >= 0


def test_date_facts_lunar_new_year_is_within_the_known_table(user):
    result, is_error = _call(user, "date_facts", {"query": "lunar_new_year"})

    assert not is_error
    assert result["holiday"] == "lunar_new_year"
    assert result["days_until"] >= 0


def test_date_facts_lunar_new_year_refuses_a_year_outside_its_table():
    """No lunisolar formula backs this one — past the table, the tool must
    say so rather than guess (see the module docstring on `_lunar_new_year`)."""
    with pytest.raises(tools.ToolError):
        tools._lunar_new_year(2099)


def test_date_facts_query_is_case_and_punctuation_insensitive(user):
    result, is_error = _call(user, "date_facts", {"query": "Labor Day"})

    assert not is_error
    assert result["holiday"] == "labor_day"


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
    assert named == set(tools._HANDLERS) | set(tools._MESSAGE_ID_HANDLERS)
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


# ---------------------------------------------------------------------------
# add_calendar_event
# ---------------------------------------------------------------------------
def test_add_calendar_event_with_a_time_range(user):
    result, is_error = _call(user, "add_calendar_event", {
        "title": "Going to Gym", "date": "2026-08-18",
        "start_time": "18:00", "end_time": "20:00",
    })

    assert not is_error
    event = CalendarEvent.objects.for_user(user).get()
    assert event.title == "Going to Gym"
    assert event.all_day is False
    assert event.kind == CalendarEvent.KIND_EVENT
    assert event.source == CalendarEvent.SOURCE_MANUAL
    assert timezone.localtime(event.starts_at).strftime("%H:%M") == "18:00"
    assert timezone.localtime(event.ends_at).strftime("%H:%M") == "20:00"
    assert result["all_day"] is False


def test_add_calendar_event_with_no_time_is_all_day(user):
    result, is_error = _call(user, "add_calendar_event", {
        "title": "Superday", "date": "2026-09-01",
    })

    assert not is_error
    event = CalendarEvent.objects.for_user(user).get()
    assert event.all_day is True
    # Local midnight, not an invented clock time.
    assert timezone.localtime(event.starts_at).strftime("%H:%M") == "00:00"
    assert event.ends_at is None


def test_add_calendar_event_links_a_real_contact(user, contact):
    result, is_error = _call(user, "add_calendar_event", {
        "title": "Coffee with Jane", "date": "2026-08-20",
        "start_time": "09:00", "kind": "chat", "contact_id": contact.id,
    })

    assert not is_error
    event = CalendarEvent.objects.for_user(user).get()
    assert event.contact_id == contact.id
    assert event.kind == "chat"


def test_add_calendar_event_rejects_another_students_contact(user, firm):
    other = User.objects.create_user(email="other@example.com", password="x")
    theirs = Contact(user=other, firm=firm, name="Not Yours")
    theirs.save()

    result, is_error = _call(user, "add_calendar_event", {
        "title": "Coffee", "date": "2026-08-20", "contact_id": theirs.id,
    })

    assert is_error
    assert CalendarEvent.objects.for_user(user).count() == 0


def test_add_calendar_event_with_no_title_is_an_error(user):
    result, is_error = _call(user, "add_calendar_event", {"date": "2026-08-20"})
    assert is_error
    assert CalendarEvent.objects.for_user(user).count() == 0


def test_add_calendar_event_with_a_bad_date_is_an_error(user):
    result, is_error = _call(user, "add_calendar_event", {"title": "X", "date": "not a date"})
    assert is_error
    assert CalendarEvent.objects.for_user(user).count() == 0


def test_add_calendar_event_end_time_without_start_time_is_an_error(user):
    result, is_error = _call(user, "add_calendar_event", {
        "title": "X", "date": "2026-08-20", "end_time": "20:00",
    })
    assert is_error
    assert CalendarEvent.objects.for_user(user).count() == 0


def test_add_calendar_event_end_before_start_is_an_error(user):
    result, is_error = _call(user, "add_calendar_event", {
        "title": "X", "date": "2026-08-20", "start_time": "20:00", "end_time": "18:00",
    })
    assert is_error
    assert CalendarEvent.objects.for_user(user).count() == 0


def test_add_calendar_event_rejects_an_unknown_kind(user):
    result, is_error = _call(user, "add_calendar_event", {
        "title": "X", "date": "2026-08-20", "kind": "birthday",
    })
    assert is_error
    assert CalendarEvent.objects.for_user(user).count() == 0


# ---------------------------------------------------------------------------
# add_contact
# ---------------------------------------------------------------------------
def test_add_contact_writes_the_same_row_the_hand_form_would(user):
    result, is_error = _call(user, "add_contact", {
        "name": "Priya Nair", "firm_text": "South Bank", "role": "Associate",
        "email": "priya@south.example",
    })

    assert not is_error
    assert result["added"] is True
    contact = Contact.objects.for_user(user).get(name="Priya Nair")
    assert result["contact_id"] == contact.id
    assert contact.firm_text == "South Bank"
    assert contact.role == "Associate"
    assert contact.email == "priya@south.example"
    # Warmth and thread state are the ratchet's to set, never the form's or
    # this tool's — a brand-new contact starts exactly where the CRM's own
    # add path leaves them.
    assert (contact.warmth, contact.thread_state) == ("cold", "no_reply")
    # Provenance is honest about which door the row came in by.
    assert contact.source == "assistant"


def test_add_contact_records_the_same_funnel_event_the_crm_page_does(user):
    """`crm.views.contact_new` records `contact_added`, and the activation
    health check counts it. A contact added through chat is the same
    activation, so it has to land in the same place or the funnel
    under-counts the students who used the advisor to do it."""
    from analytics.models import ProductEvent

    _call(user, "add_contact", {"name": "Funnel Person"})

    event = ProductEvent.objects.for_user(user).get(event="contact_added")
    assert event.props["source"] == "assistant"


def test_add_contact_offers_only_the_quick_add_fields(user):
    """`add_contact`'s schema is deliberately name/firm_text/role/email only
    — not ContactForm's full eleven fields. Two reasons: it mirrors the
    CRM's own `?quick=1` fast path (role/school/email/linkedin/region/angle
    all hidden behind that page's `{% if not quick %}`), and TOOL_SCHEMAS
    combined has a hard ceiling of 24 optional parameters before Anthropic's
    API refuses every request outright — measured live at 26 offered, which
    404'd the entire advisor, not just this tool. firm_id and region are
    gone entirely rather than merely untested; a value for either is simply
    not read."""
    result, is_error = _call(user, "add_contact", {
        "name": "Ken Lau", "firm_id": 999999, "region": "eu",
    })

    assert not is_error
    contact = Contact.objects.for_user(user).get(name="Ken Lau")
    assert contact.firm_id is None
    assert contact.region == ""


def test_add_contact_rejects_a_value_the_contact_form_would_reject(user):
    """Validation is the model's own `full_clean`, so the email rule here is
    the same one `crm.forms.ContactForm` enforces — and the message the model
    reads back is the one the student would have seen on the form."""
    result, is_error = _call(user, "add_contact", {"name": "Bad Email", "email": "not-an-email"})

    assert is_error
    assert "valid email" in result["error"].lower()
    assert not Contact.objects.for_user(user).exists()


def test_add_contact_with_no_name_is_an_error(user):
    result, is_error = _call(user, "add_contact", {"firm_text": "South Bank"})

    assert is_error
    assert not Contact.objects.for_user(user).exists()


def test_add_contact_refuses_to_make_a_second_copy_of_someone(user, contact):
    """"Add Jane Banker" said about someone already tracked is the common
    case, and a duplicate row forks a history in two — the same split
    `crm.views`' archive comment describes, arriving through a different
    door. The refusal names the id they already have so the model can talk
    about the real relationship instead."""
    result, is_error = _call(user, "add_contact", {"name": "jane banker", "firm_text": "North Bank"})

    assert not is_error
    assert result["added"] is False
    assert result["already_exists"] is True
    assert result["contact_id"] == contact.id
    assert "already in the student's network" in result["instruction"]
    assert Contact.objects.for_user(user).count() == 1


def test_add_contact_resolves_the_firm_so_the_row_lands_on_the_board(user):
    """Every row this tool wrote used to land OFF the coverage board.

    `firm_text` was stored verbatim and `firm_id` left null, so the contact
    never appeared on their firm's page, never counted toward that firm's
    coverage, and — because `Contact.resolve_region` reads the FIRM's markets
    — carried a blank region no matter how plainly the student had named the
    bank. The match rule is `accounts.services.match_firm_text`, the CSV
    importer's own, which forgives "&"/"and" and a trailing legal suffix.
    The region is not set here at all: it falls out of `Contact.save()`
    because the firm now answers.
    """
    Firm.objects.create(slug="gs", name="Goldman Sachs", regions=["us"])

    result, is_error = _call(user, "add_contact", {
        "name": "Marcus Lee", "firm_text": "Goldman Sachs", "role": "Associate",
    })

    assert not is_error
    assert result["firm_on_board"] is True
    assert result["firm"] == "Goldman Sachs"
    assert result["region"] == "us"
    contact = Contact.objects.for_user(user).get(name="Marcus Lee")
    assert contact.firm.slug == "gs"
    assert contact.firm_text == "Goldman Sachs"
    assert (contact.region, contact.region_source) == ("us", "firm")


def test_add_contact_leaves_an_unknown_employer_as_typed(user):
    """A firm the directory does not carry is a real answer, not a failure.
    The text is stored exactly as before and nothing is invented to make it
    match (P1)."""
    result, is_error = _call(user, "add_contact", {
        "name": "Dana Wu", "firm_text": "A Very Small Advisory LLP",
    })

    assert not is_error
    assert result["firm_on_board"] is False
    contact = Contact.objects.for_user(user).get(name="Dana Wu")
    assert contact.firm_id is None
    assert contact.firm_text == "A Very Small Advisory LLP"
    assert contact.region == ""


def test_add_contact_reports_an_archived_match_instead_of_duplicating_it(user, firm):
    """REWRITTEN 2026-09-02. This used to pin `name__iexact` over
    `archived=False`, which meant a contact the student had archived came
    back as a SECOND card with the whole history stranded on the first — a
    false split with no clean undo. The matcher is now `capture.discovery
    ._match_existing`, the single one `consider_finding`, `accept`, `restore`
    and mailfacts already share: it reads archived rows too, recognises the
    corporate `Last, First` spelling and a dropped middle initial, and
    abstains rather than picking when two rows answer to one name.
    """
    gone = Contact(user=user, firm=firm, name="Wei Zhang", archived=True)
    gone.save()

    result, is_error = _call(user, "add_contact", {
        "name": "Zhang, Wei", "firm_text": "North Bank",
    })

    assert not is_error
    assert result["added"] is False
    assert result["already_exists"] is True
    assert result["archived"] is True
    assert result["contact_id"] == gone.id
    assert "archived" in result["instruction"]
    assert Contact.objects.for_user(user).count() == 1


def test_add_contact_matches_on_the_email_even_under_a_different_spelling(user, firm):
    """The strong key is the address. Somebody already tracked under a
    nickname is not a new person because the student said their full name."""
    existing = Contact(user=user, firm=firm, name="Kit Fung",
                       email="kit.fung@north.example")
    existing.save()

    result, _ = _call(user, "add_contact", {
        "name": "Kit Wing Fung", "email": "kit.fung@north.example",
    })

    assert result["added"] is False
    assert result["contact_id"] == existing.id
    assert Contact.objects.for_user(user).count() == 1


# ---------------------------------------------------------------------------
# set_contact_status
#
# `crm.services.set_contact_state` commits on its own psycopg connection
# outside Django's transaction, so every test that reaches it needs
# `transaction=True` — same reasoning as the log_touch test above.
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_set_contact_status_moves_one_contact_and_leaves_an_audit_touch():
    from crm.views import _display_note

    u = User.objects.create_user(email="status@example.com", password="x")
    c = Contact(user=u, name="Never Replies")
    c.save()

    payload, is_error = tools.execute(
        u, "set_contact_status",
        {"contact_ids": [c.id], "thread_state": "parked", "note": "Three notes, nothing back"},
        "msg_status1",
    )
    result = json.loads(payload)

    assert not is_error
    assert result["changed_count"] == 1
    assert result["not_found"] == []
    assert result["changed"][0]["thread_state_before"] == "no_reply"
    assert result["changed"][0]["thread_state_after"] == "parked"
    c.refresh_from_db()
    assert c.thread_state == "parked"
    # The override writes its own touch row, so the history has no gap — and
    # the assistant marker rides on it exactly as it does for log_touch. It
    # sits INSIDE set_state's own "manual override: ..." prefix (that string
    # is written by the domain package, not here), and `_display_note` strips
    # both, so the student reads only their own words and the before-state.
    touch = Touch.objects.for_user(u).get(contact=c)
    assert "[assistant:msg_status1]" in touch.note
    assert _display_note(touch.note) == (
        "Three notes, nothing back (was warmth=cold, thread=no_reply)"
    )


@pytest.mark.django_db(transaction=True)
def test_set_contact_status_moves_a_whole_batch_in_one_call():
    u = User.objects.create_user(email="bulk@example.com", password="x")
    contacts = []
    for i in range(3):
        c = Contact(user=u, name=f"Quiet {i}")
        c.save()
        contacts.append(c)

    payload, is_error = tools.execute(
        u, "set_contact_status",
        {"contact_ids": [c.id for c in contacts], "thread_state": "parked", "warmth": "cold"},
        "msg_bulk",
    )
    result = json.loads(payload)

    assert not is_error
    assert result["changed_count"] == 3
    for c in contacts:
        c.refresh_from_db()
        assert c.thread_state == "parked"


@pytest.mark.django_db(transaction=True)
def test_set_contact_status_reports_the_ids_it_could_not_find():
    """A batch with one bad id must not fail the other two, and must not
    quietly pretend it moved three. The model needs the skipped ids in the
    result to answer honestly."""
    u = User.objects.create_user(email="partial@example.com", password="x")
    other = User.objects.create_user(email="notmine@example.com", password="x")
    mine = Contact(user=u, name="Mine")
    mine.save()
    also_mine = Contact(user=u, name="Also Mine")
    also_mine.save()
    theirs = Contact(user=other, name="Theirs")
    theirs.save()

    payload, is_error = tools.execute(
        u, "set_contact_status",
        {"contact_ids": [mine.id, theirs.id, 999999, also_mine.id], "thread_state": "quiet"},
        "msg_partial",
    )
    result = json.loads(payload)

    assert not is_error
    assert result["changed_count"] == 2
    assert sorted(result["not_found"]) == sorted([theirs.id, 999999])
    assert "do not claim the rest" in result["instruction"]
    theirs.refresh_from_db()
    assert theirs.thread_state == "no_reply"


@pytest.mark.django_db(transaction=True)
def test_set_contact_status_counts_a_repeated_id_once():
    u = User.objects.create_user(email="dupes@example.com", password="x")
    c = Contact(user=u, name="Listed Twice")
    c.save()

    payload, _ = tools.execute(
        u, "set_contact_status", {"contact_ids": [c.id, c.id], "warmth": "replied"}, "msg_dupe"
    )

    assert json.loads(payload)["changed_count"] == 1
    assert Touch.objects.for_user(u).count() == 1


def test_set_contact_status_rejects_an_unknown_thread_state_or_warmth(user, contact):
    bad_state, is_error = _call(
        user, "set_contact_status", {"contact_ids": [contact.id], "thread_state": "ghosted"}
    )
    assert is_error and "ghosted" in bad_state["error"]

    bad_warmth, is_error = _call(
        user, "set_contact_status", {"contact_ids": [contact.id], "warmth": "toasty"}
    )
    assert is_error and "toasty" in bad_warmth["error"]

    contact.refresh_from_db()
    assert (contact.warmth, contact.thread_state) == ("cold", "no_reply")
    assert not Touch.objects.for_user(user).exists()


def test_set_contact_status_with_nothing_to_set_is_an_error(user, contact):
    result, is_error = _call(user, "set_contact_status", {"contact_ids": [contact.id]})

    assert is_error
    assert not Touch.objects.for_user(user).exists()


def test_set_contact_status_with_an_empty_list_is_an_error(user):
    result, is_error = _call(user, "set_contact_status", {"contact_ids": [], "warmth": "cold"})
    assert is_error


def test_set_contact_status_refuses_a_batch_over_the_cap_rather_than_doing_part_of_it(user, firm):
    """Over the cap the whole call is refused. Silently doing the first 25 of
    40 is the worst of both: the student is told it worked and a quarter of
    their network is in a state nobody chose."""
    ids = []
    for i in range(tools.MAX_BULK_CONTACTS + 1):
        c = Contact(user=user, firm=firm, name=f"Person {i:02d}")
        c.save()
        ids.append(c.id)

    result, is_error = _call(user, "set_contact_status", {"contact_ids": ids, "thread_state": "parked"})

    assert is_error
    assert str(tools.MAX_BULK_CONTACTS) in result["error"]
    assert not Touch.objects.for_user(user).exists()
    assert not Contact.objects.for_user(user).filter(thread_state="parked").exists()


# ---------------------------------------------------------------------------
# update_settings
# ---------------------------------------------------------------------------
def test_update_settings_changes_an_ordinary_field_on_the_first_call(user):
    result, is_error = _call(user, "update_settings", {"field": "weekly_touch_goal", "value": "15"})

    assert not is_error
    assert result["updated"] is True
    assert result["value_after"] == "15"
    user.refresh_from_db()
    assert user.weekly_touch_goal == 15


def test_update_settings_writes_each_family_of_ordinary_field(user):
    """One case per storage shape the five settings forms actually use: a
    plain column, an int column, a JSON dict keyed by region, a JSON dict of
    cadence overrides, the assets dict, and the boolean whose form field is
    named the opposite of its column."""
    _call(user, "update_settings", {"field": "school", "value": "HKUST"})
    _call(user, "update_settings", {"field": "class_year", "value": str(min(tools._CLASS_YEARS))})
    _call(user, "update_settings", {"field": "work_auth_us", "value": "sponsorship"})
    _call(user, "update_settings", {"field": "followup_after_business_days", "value": "5"})
    _call(user, "update_settings", {"field": "advocate_target", "value": "3"})
    _call(user, "update_settings", {"field": "weekly_digest_enabled", "value": "false"})

    user.refresh_from_db()
    assert user.school == "HKUST"
    assert user.class_year == min(tools._CLASS_YEARS)
    assert user.work_authorization["us"] == "sponsorship"
    assert user.cadence_params["followup_after_business_days"] == 5
    assert user.assets["advocate_target"] == 3
    # The checkbox reads "Weekly Email Digest" and the column reads opt-OUT;
    # false in, opted out stored.
    assert user.weekly_digest_opt_out is True


def test_update_settings_clears_an_override_rather_than_storing_a_zero(user):
    """`CadenceForm`'s own contract: a blank input REMOVES the override so the
    product default applies. Storing a 0 would leave a number the engine
    silently ignores, which is the defect that contract exists to avoid."""
    _call(user, "update_settings", {"field": "park_after_business_days", "value": "30"})
    _call(user, "update_settings", {"field": "park_after_business_days", "value": ""})

    user.refresh_from_db()
    assert "park_after_business_days" not in user.cadence_params


def test_update_settings_keeps_the_keys_it_does_not_own(user):
    """Copy-then-set, never a fresh dict — the same rule the forms follow. A
    key an admin pinned by hand, or another region's work-auth answer, must
    survive a change to a neighbouring one."""
    user.cadence_params = {"some_admin_key": 99}
    user.work_authorization = {"hk": "citizen"}
    user.save(update_fields=["cadence_params", "work_authorization"])

    _call(user, "update_settings", {"field": "max_cold_touches", "value": "2"})
    _call(user, "update_settings", {"field": "work_auth_us", "value": "citizen"})

    user.refresh_from_db()
    assert user.cadence_params["some_admin_key"] == 99
    assert user.work_authorization == {"hk": "citizen", "us": "citizen"}


def test_update_settings_rejects_a_value_the_settings_page_would_reject(user):
    """The ranges and vocabularies are imported from the forms, not restated,
    so anything the real page refuses is refused here too."""
    out_of_range, is_error = _call(
        user, "update_settings", {"field": "max_cold_touches", "value": "7"}
    )
    assert is_error and "between 1 and 2" in out_of_range["error"]

    not_a_number, is_error = _call(
        user, "update_settings", {"field": "weekly_touch_goal", "value": "loads"}
    )
    assert is_error

    bad_auth, is_error = _call(
        user, "update_settings", {"field": "work_auth_us", "value": "green-card"}
    )
    assert is_error

    user.refresh_from_db()
    assert user.cadence_params == {}
    assert user.weekly_touch_goal is None
    assert user.work_authorization == {}


def test_update_settings_refuses_a_field_outside_the_allowlist(user):
    """The login identity is not a recruiting preference. It is not in the
    allowlist at all, so this is a refusal with the page named, not a write."""
    result, is_error = _call(user, "update_settings", {"field": "email", "value": "new@example.com"})

    assert is_error
    assert "Settings page" in result["error"]
    user.refresh_from_db()
    assert user.email == "student@example.com"


def test_the_settings_allowlist_never_offers_identity_auth_or_upload_fields():
    """Structural, not behavioural: these must not be reachable even by name.
    A field added to this tool later that belongs to the account rather than
    the recruiting campaign fails here before anyone has to reason about it."""
    banned = {
        "email", "password", "avatar", "remove_avatar", "google_sub",
        "calendar_token", "plan", "is_staff", "is_superuser",
        "timezone_auto", "language",
    }
    assert banned.isdisjoint(tools.SETTINGS_FIELDS)
    schema = next(s for s in tools.TOOL_SCHEMAS if s["name"] == "update_settings")
    assert banned.isdisjoint(schema["input_schema"]["properties"]["field"]["enum"])


def test_an_important_setting_changes_nothing_on_a_call_without_confirmation(user):
    """One call, no `confirmed`: the write does not happen, and the error is
    the protocol — describe the effect, then ask. A timezone silently moving
    a student's week boundary is exactly the bug class Settings exists to
    avoid, and it is no better arriving through chat."""
    result, is_error = _call(user, "update_settings", {"field": "timezone", "value": "Europe/London"})

    assert is_error
    assert "NOT CHANGED" in result["error"]
    assert "what day your queue and deadlines think it is" in result["error"]
    assert "confirmed=true" in result["error"]
    user.refresh_from_db()
    assert user.timezone == ""


def test_an_important_setting_applies_only_on_the_second_confirmed_call(user):
    first, is_error = _call(user, "update_settings", {"field": "timezone", "value": "Europe/London"})
    assert is_error
    user.refresh_from_db()
    assert user.timezone == ""

    second, is_error = _call(
        user, "update_settings", {"field": "timezone", "value": "Europe/London", "confirmed": True}
    )

    assert not is_error
    assert second["updated"] is True
    user.refresh_from_db()
    assert user.timezone == "Europe/London"
    # An explicit pick turns following OFF, exactly as ProfileForm.apply_to
    # does — otherwise the next page load would overrule the choice.
    assert user.timezone_auto is False


def test_confirming_a_timezone_of_auto_turns_following_back_on(user):
    user.timezone, user.timezone_auto = "Europe/London", False
    user.save(update_fields=["timezone", "timezone_auto"])

    result, is_error = _call(
        user, "update_settings", {"field": "timezone", "value": "auto", "confirmed": True}
    )

    assert not is_error
    user.refresh_from_db()
    assert user.timezone_auto is True
    # The stored zone is left alone: it stays correct until the browser next
    # reports one, and clearing it would hand them a UTC day for no reason.
    assert user.timezone == "Europe/London"


def test_an_unknown_timezone_is_refused_even_when_confirmed(user):
    """Confirmation is about blast radius, not about validation. A zone
    `zoneinfo` does not know would make the middleware's read fail later,
    somewhere with no student in front of it."""
    result, is_error = _call(
        user, "update_settings", {"field": "timezone", "value": "Mars/Olympus", "confirmed": True}
    )

    assert is_error
    user.refresh_from_db()
    assert user.timezone == ""


def test_regions_replace_the_whole_list_which_is_why_they_need_confirming(user):
    user.regions = ["us", "hk"]
    user.save(update_fields=["regions"])

    unconfirmed, is_error = _call(user, "update_settings", {"field": "regions", "value": "hk"})
    assert is_error
    assert "REPLACES" in unconfirmed["error"]
    user.refresh_from_db()
    assert user.regions == ["us", "hk"]  # nothing dropped on the first call

    confirmed, is_error = _call(
        user, "update_settings", {"field": "regions", "value": "hk, us", "confirmed": True}
    )

    assert not is_error
    user.refresh_from_db()
    assert user.regions == ["hk", "us"]


def test_an_unknown_region_token_is_refused_even_when_confirmed(user):
    result, is_error = _call(
        user, "update_settings", {"field": "regions", "value": "hk,atlantis", "confirmed": True}
    )

    assert is_error
    assert "atlantis" in result["error"]
    user.refresh_from_db()
    assert user.regions == []


def test_confirmed_is_ignored_on_an_ordinary_field(user):
    """`confirmed=true` on a field that never needed it is not an error and
    not a second meaning — an ordinary write is an ordinary write."""
    result, is_error = _call(
        user, "update_settings", {"field": "name", "value": "Sam Chan", "confirmed": True}
    )

    assert not is_error
    user.refresh_from_db()
    assert user.name == "Sam Chan"


# ---------------------------------------------------------------------------
# Honesty about a posting's own liveness.
#
# `Opportunity.status` is a bare CharField defaulting to `""` — a posting the
# reverify pass has never re-checked carries neither "open" nor "closed", and
# `directory.deadlines.is_posting_closed` exists precisely so no surface reads
# that third state as a confirmed close. These pin that the advisor's tools
# use it rather than an is-open test of their own.
# ---------------------------------------------------------------------------
def test_a_pipeline_role_the_scraper_never_rechecked_is_not_reported_as_closed(user, firm):
    """Regression: `still_open: o.status == "open"` reported every unverified
    posting as not open, which the model can only read as "that role is
    closed". Nothing established that — the scraper has simply never looked
    at it again since it was ingested."""
    never_rechecked = Opportunity.objects.create(
        firm=firm, title="2028 Summer Analyst", bucket="internship", region="hk",
        status="", deadline=timezone.localdate() + timedelta(days=20),
        url="https://north.example/jobs/unverified",
    )
    UserOpportunity(user=user, opportunity=never_rechecked).save()

    result, is_error = _call(user, "get_my_pipeline")

    assert not is_error
    row = result["by_status"]["saved"][0]
    assert row["posting_closed"] is False


def test_a_pipeline_role_the_scraper_confirmed_closed_is_reported_as_closed(user, firm):
    dead = Opportunity.objects.create(
        firm=firm, title="2028 Summer Analyst", bucket="internship", region="hk",
        status="closed", deadline=timezone.localdate() + timedelta(days=20),
        url="https://north.example/jobs/dead",
    )
    UserOpportunity(user=user, opportunity=dead).save()

    result, is_error = _call(user, "get_my_pipeline")

    assert not is_error
    assert result["by_status"]["saved"][0]["posting_closed"] is True


def test_saving_a_closed_posting_says_so_rather_than_offering_its_deadline(user, firm):
    """Ids reach `track_opportunity` from `get_situation` too, and a
    `role_closed` event carries the id of a posting the scraper watched the
    firm take down. The save still happens — it is the student's own
    application record — but the result must not read as a live window."""
    dead = Opportunity.objects.create(
        firm=firm, title="2028 Summer Analyst", bucket="internship", region="hk",
        status="closed", deadline=timezone.localdate() + timedelta(days=20),
        url="https://north.example/jobs/dead",
    )

    result, is_error = _call(
        user, "track_opportunity", {"opportunity_id": dead.id, "status": "saved"}
    )

    assert not is_error
    assert result["saved"] is True
    assert result["posting_closed"] is True
    assert "closed" in result["instruction"]
    assert UserOpportunity.objects.for_user(user).filter(opportunity=dead).exists()


def test_saving_a_live_posting_carries_no_closed_flag_at_all(user, opportunity):
    """The flag is only ever present on a confirmed close, so its absence is
    never something the model has to interpret."""
    result, is_error = _call(
        user, "track_opportunity", {"opportunity_id": opportunity.id, "status": "saved"}
    )

    assert not is_error
    assert "posting_closed" not in result
    assert "instruction" not in result


# ---------------------------------------------------------------------------
# The bulk override: what makes it recoverable.
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_a_bulk_status_change_records_what_each_contact_was_before():
    """`set_state` writes only the state AFTER. This is the one write the
    model can make against many relationships at once off a single reading
    of a chat, and "the student can set any of these back" is only a real
    instruction if their own history says what each one was."""
    from crm.views import _display_note

    u = User.objects.create_user(email="bulkundo@example.com", password="x")
    warm = Contact(user=u, name="Warm Person", warmth="chatted", thread_state="replied")
    warm.save()
    cold = Contact(user=u, name="Cold Person")
    cold.save()

    payload, is_error = tools.execute(
        u, "set_contact_status",
        {"contact_ids": [warm.id, cold.id], "thread_state": "parked"},
        "msg_bulk1",
    )
    result = json.loads(payload)

    assert not is_error
    assert result["changed_count"] == 2

    warm_note = _display_note(Touch.objects.for_user(u).get(contact=warm).note)
    cold_note = _display_note(Touch.objects.for_user(u).get(contact=cold).note)
    # Per contact, not one note for the batch: the whole point is that a
    # student reading ONE contact's page learns what THAT contact was.
    assert warm_note == "(was warmth=chatted, thread=replied)"
    assert cold_note == "(was warmth=cold, thread=no_reply)"


@pytest.mark.django_db(transaction=True)
def test_the_students_own_words_still_lead_the_audit_note():
    """The before-state is a suffix, never a replacement — the reason the
    student gave is what they will actually be reading."""
    from crm.views import _display_note

    u = User.objects.create_user(email="bulkwords@example.com", password="x")
    c = Contact(user=u, name="Never Replies")
    c.save()

    tools.execute(
        u, "set_contact_status",
        {"contact_ids": [c.id], "warmth": "cold", "note": "Three notes, nothing back"},
        "msg_bulk2",
    )

    note = _display_note(Touch.objects.for_user(u).get(contact=c).note)
    assert note.startswith("Three notes, nothing back")
    assert "(was warmth=cold, thread=no_reply)" in note


# ---------------------------------------------------------------------------
# log_touch: idempotent within one model response, the same guard
# `assistant.views.log_draft_touch` already applies to one click.
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_the_same_touch_logged_twice_in_one_response_is_written_once():
    u = User.objects.create_user(email="dupe@example.com", password="x")
    c = Contact(user=u, name="Reply Person")
    c.save()

    first = json.loads(tools.execute(
        u, "log_touch",
        {"contact_id": c.id, "kind": "reply_received", "channel": "email"},
        "msg_same",
    )[0])
    second_payload, is_error = tools.execute(
        u, "log_touch",
        {"contact_id": c.id, "kind": "reply_received", "channel": "email"},
        "msg_same",
    )
    second = json.loads(second_payload)

    assert first["logged"] is True
    # Not an error: the model has nothing to correct, it just must not say
    # it logged the thing twice.
    assert not is_error
    assert second["logged"] is False
    assert second["already_logged"] is True
    assert Touch.objects.for_user(u).filter(contact=c).count() == 1


@pytest.mark.django_db(transaction=True)
def test_two_different_interactions_in_one_response_are_still_two_touches():
    """"I emailed her Monday and she replied Wednesday" is two real events in
    one reply. The dedupe key carries kind and channel so it can never
    swallow the second one."""
    u = User.objects.create_user(email="twokinds@example.com", password="x")
    c = Contact(user=u, name="Reply Person")
    c.save()

    tools.execute(u, "log_touch",
                  {"contact_id": c.id, "kind": "outreach", "channel": "email"}, "msg_two")
    tools.execute(u, "log_touch",
                  {"contact_id": c.id, "kind": "reply_received", "channel": "email"}, "msg_two")

    assert Touch.objects.for_user(u).filter(contact=c).count() == 2


@pytest.mark.django_db(transaction=True)
def test_the_same_touch_in_a_later_response_is_a_new_interaction():
    """A different message id is a different turn, which is a genuinely new
    interaction — the guard must not reach across turns and silently refuse
    to log a second coffee chat with the same person."""
    u = User.objects.create_user(email="laterturn@example.com", password="x")
    c = Contact(user=u, name="Reply Person")
    c.save()

    tools.execute(u, "log_touch",
                  {"contact_id": c.id, "kind": "outreach", "channel": "email"}, "msg_one")
    tools.execute(u, "log_touch",
                  {"contact_id": c.id, "kind": "outreach", "channel": "email"}, "msg_two")

    assert Touch.objects.for_user(u).filter(contact=c).count() == 2


@pytest.mark.django_db(transaction=True)
def test_a_blank_message_id_never_deduplicates_anything():
    """`[assistant:]` is not an identity. A caller with no message id (the
    `execute` default) must write every touch it is asked for rather than
    matching them all against each other."""
    u = User.objects.create_user(email="nomsgid@example.com", password="x")
    c = Contact(user=u, name="Reply Person")
    c.save()

    tools.execute(u, "log_touch", {"contact_id": c.id, "kind": "outreach", "channel": "email"}, "")
    tools.execute(u, "log_touch", {"contact_id": c.id, "kind": "outreach", "channel": "email"}, "")

    assert Touch.objects.for_user(u).filter(contact=c).count() == 2


# ---------------------------------------------------------------------------
# Deadline provenance has to survive into the advisor.
#
# Measured on the live board 2026-09-01: of 341 dated open campus roles, 14
# came from a provider's structured field and 327 were our own regex reading
# the posting's prose — 96%. Every visual surface carries that distinction as
# a dotted underline the reader can hover. A sentence in a chat reply has no
# underline, so the tool payload is the only place it can travel, and the
# tool description used to assert the opposite outright: "Deadlines here are
# what the firm published."
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_deadline_source_names_a_prose_read_date_as_reported(firm):
    o = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        deadline=timezone.localdate() + timedelta(days=10),
        confidence=0.6, url="https://north.example/jobs/reported")
    assert tools._deadline_source(o) == "reported"


@pytest.mark.django_db
def test_deadline_source_names_a_board_published_date_as_stated(firm):
    o = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        deadline=timezone.localdate() + timedelta(days=10),
        confidence=1.0, url="https://north.example/jobs/stated")
    assert tools._deadline_source(o) == "stated"


@pytest.mark.django_db
def test_deadline_source_is_none_when_there_is_no_date_to_qualify(firm):
    o = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        deadline=None, url="https://north.example/jobs/undated")
    assert tools._deadline_source(o) is None


@pytest.mark.django_db
def test_search_results_carry_provenance_beside_every_deadline(user, firm):
    """A bare date in the payload is not neutral. With 96% of the board
    prose-read, a payload that says nothing hands the model the rare case as
    though it were the normal one."""
    Opportunity.objects.create(
        firm=firm, title="2028 Summer Analyst", bucket="internship",
        region="hk", status="open",
        deadline=timezone.localdate() + timedelta(days=10),
        confidence=0.6, url="https://north.example/jobs/searchable")
    out, _ = _call(user, "search_opportunities", {})
    dated = [r for r in out["roles"] if r.get("deadline")]
    assert dated, "fixture should have produced at least one dated row"
    for row in dated:
        assert row["deadline_source"] in ("reported", "stated")


def test_the_tool_description_no_longer_claims_the_firm_published_these_dates():
    """The specific false sentence, pinned so it cannot come back. It was
    false for 327 of 341 dated rows."""
    spec = next(t for t in tools.TOOL_SCHEMAS
                if t["name"] == "search_opportunities")
    text = spec["description"]
    assert "what the firm published" not in text
    assert "deadline_source" in text
    assert "reported" in text


# ---------------------------------------------------------------------------
# get_situation forwards a moved deadline's provenance (2026-09-01).
# ---------------------------------------------------------------------------
def _move_deadline(user, opportunity, *, confidence):
    from directory.models import OpportunityChange

    Opportunity.objects.filter(pk=opportunity.pk).update(confidence=confidence)
    if not UserOpportunity.objects.for_user(user).filter(opportunity=opportunity).exists():
        UserOpportunity(user=user, opportunity=opportunity).save()
    OpportunityChange.objects.create(
        opportunity=opportunity, field="deadline", old_value="2026-08-01",
        new_value="2026-08-20", stage="reverify", observed_at=timezone.now(),
    )


def test_get_situation_forwards_the_provenance_of_a_moved_deadline(user, opportunity):
    """354 of the 394 moved-deadline rows in the last 30 days were on dates
    our own regex read; the payload said nothing, so the advisor reported
    every one as the firm moving its deadline."""
    _move_deadline(user, opportunity, confidence=0.6)

    result, is_error = _call(user, "get_situation")

    assert not is_error
    event = result["events"][0]
    assert event["kind"] == "deadline_moved"
    assert event["deadline_source"] == "reported"


def test_get_situation_marks_a_board_published_moved_deadline_stated(user, opportunity):
    _move_deadline(user, opportunity, confidence=1.0)

    result, _ = _call(user, "get_situation")

    assert result["events"][0]["deadline_source"] == "stated"


def test_the_situation_tool_description_carries_the_provenance_rule():
    spec = next(t for t in tools.TOOL_SCHEMAS if t["name"] == "get_situation")
    assert "deadline_source" in spec["description"]
    assert "reported" in spec["description"]


# ---------------------------------------------------------------------------
# search_opportunities can ask about every market a student can target.
#
# The enum was ("us", "hk") while Settings offered six and the board held 910
# eu / 250 sg / 48 cn / 40 jp open campus rows — a student who set Europe as
# a target could not ask the advisor about it.
# ---------------------------------------------------------------------------
def test_search_opportunities_region_enum_is_every_market_a_student_can_target():
    from directory.classify import TRACKED_REGIONS

    spec = next(t for t in tools.TOOL_SCHEMAS if t["name"] == "search_opportunities")
    enum = spec["input_schema"]["properties"]["region"]["enum"]

    assert enum == list(TRACKED_REGIONS)
    for code in ("eu", "sg", "cn", "jp"):
        assert code in enum
    # Places a role can BE, never a market a student targets.
    assert "other" not in enum
    assert "global" not in enum


def test_search_opportunities_filters_a_market_beyond_us_and_hk(user, firm):
    london = Opportunity.objects.create(
        firm=firm, title="2028 Summer Analyst, London", bucket="internship",
        region="eu", location="London", status="open",
        url="https://north.example/jobs/london")
    Opportunity.objects.create(
        firm=firm, title="2028 Summer Analyst, Hong Kong", bucket="internship",
        region="hk", location="Hong Kong", status="open",
        url="https://north.example/jobs/hk")

    result, is_error = _call(user, "search_opportunities", {"region": "eu"})

    assert not is_error
    assert [r["opportunity_id"] for r in result["roles"]] == [london.id]


# ---------------------------------------------------------------------------
# get_firm survives the slug the model guesses.
#
# The schema's own example was 'goldman-sachs'; the real slug is `gs`, so the
# tool refused the very string it had suggested.
# ---------------------------------------------------------------------------
def test_get_firm_finds_a_firm_by_its_hyphenated_name_when_the_slug_differs(user):
    Firm.objects.create(slug="gs", name="Goldman Sachs")

    result, is_error = _call(user, "get_firm", {"name_or_slug": "goldman-sachs"})

    assert not is_error
    assert result["slug"] == "gs"
    assert result["firm"] == "Goldman Sachs"


def test_get_firm_still_refuses_a_hyphenated_name_nothing_matches(user):
    Firm.objects.create(slug="gs", name="Goldman Sachs")

    result, is_error = _call(user, "get_firm", {"name_or_slug": "nowhere-plc"})

    assert is_error
    assert "error" in result


def test_the_get_firm_example_no_longer_suggests_a_slug_that_does_not_exist():
    spec = next(t for t in tools.TOOL_SCHEMAS if t["name"] == "get_firm")
    description = spec["input_schema"]["properties"]["name_or_slug"]["description"]
    assert "goldman-sachs" not in description
    assert "Goldman Sachs" in description


# ---------------------------------------------------------------------------
# get_contact carries what the student already sent this person, so a draft
# never resends it.
# ---------------------------------------------------------------------------
def test_get_contact_carries_the_students_own_opener_and_recent_subjects(user, contact):
    contact.opener = "Hi Jane, I'm a second-year at HKU and saw you moved to the HK desk in June."
    contact.save(update_fields=["opener"])
    now = timezone.now()
    for days_ago, subject in ((40, "Intro from an HKU student"), (30, ""),
                              (20, "Following up"), (10, "Quick question on the SA process"),
                              (5, "Thank you for Tuesday")):
        Touch(
            user=user, contact=contact, ts=now - timedelta(days=days_ago),
            kind="outreach", channel="email", subject=subject,
        ).save()

    result, is_error = _call(user, "get_contact", {"contact_id": contact.id})

    assert not is_error
    assert result["my_opener"] == contact.opener
    # Newest first, blanks skipped, capped at three.
    assert result["recent_subjects"] == [
        "Thank you for Tuesday",
        "Quick question on the SA process",
        "Following up",
    ]


def test_get_contact_with_nothing_sent_yet_says_so_plainly(user, contact):
    result, _ = _call(user, "get_contact", {"contact_id": contact.id})

    assert result["my_opener"] == ""
    assert result["recent_subjects"] == []


def test_the_get_contact_description_names_both_fields():
    spec = next(t for t in tools.TOOL_SCHEMAS if t["name"] == "get_contact")
    assert "my_opener" in spec["description"]
    assert "recent_subjects" in spec["description"]


def _reply_thread(user, contact, pairs, *, gap_days=2):
    """`pairs` send/reply cycles on one contact, each reply `gap_days` after
    its send and each cycle a week apart."""
    start = timezone.now() - timedelta(days=90)
    existing = Touch.objects.for_user(user).filter(contact=contact).count()
    for i in range(pairs):
        sent = start + timedelta(days=7 * (existing + i))
        Touch(user=user, contact=contact, kind="outreach", channel="email",
              ts=sent).save()
        Touch(user=user, contact=contact, kind="reply_received", channel="email",
              ts=sent + timedelta(days=gap_days)).save()


def test_get_contact_reports_a_reply_median_only_once_there_are_three_pairs(
    user, contact
):
    """Two numbers have a midpoint, not a habit.

    `median_reply_days` is the one figure here that is a claim about a PERSON
    rather than a record of an event, and "she usually replies in two days"
    said off a single exchange is exactly the confident-and-thin sentence P1
    exists to stop. `reply_pairs` goes out at every sample size so the
    absence is explicable rather than silent.

    Measured on the founder's live account 2026-09-02: 16 of 265 contacts
    have at least one reply pair (18 pairs, median 0.71 days) and none has
    three, so this field is dark on his data today. That is the intended
    failure direction.
    """
    _reply_thread(user, contact, 2)
    result, is_error = _call(user, "get_contact", {"contact_id": contact.id})
    assert not is_error
    assert result["reply_pairs"] == 2
    assert "median_reply_days" not in result

    _reply_thread(user, contact, 1)
    result, _ = _call(user, "get_contact", {"contact_id": contact.id})
    assert result["reply_pairs"] == 3
    assert result["median_reply_days"] == 2.0


def test_get_contact_does_not_count_an_unanswered_send_as_a_reply(user, contact):
    """A send with no reply after it is not a pair, and a reply with no send
    before it is not a reply to anything the student did."""
    now = timezone.now()
    Touch(user=user, contact=contact, kind="outreach", channel="email",
          ts=now - timedelta(days=9)).save()
    Touch(user=user, contact=contact, kind="outreach", channel="email",
          ts=now - timedelta(days=2)).save()

    result, _ = _call(user, "get_contact", {"contact_id": contact.id})

    assert result["reply_pairs"] == 0
    assert "median_reply_days" not in result


@pytest.mark.outreach_blackout
def test_date_facts_reports_the_outreach_blackout_in_crm_todays_own_words(user):
    """The advisor and the queue must not disagree about December.

    Marked `outreach_blackout` so the real calendar rule is in play: the
    suite's autouse default answers "ordinary weekday" for every test that is
    not about this feature (see `coverage_web/conftest.py`), and this one is.

    The holiday blackout shipped at the view level (`crm.today
    .outreach_blackout`, Dec 20 to Jan 2 plus every weekend), so the Today
    page holds every cold card on those days and says why, while this tool
    would answer "Christmas Eve is 113 days away" with nothing to suggest the
    product itself would not send then.

    The second assertion is the load-bearing one: the string comes from
    `crm.today.outreach_blackout`, not from a literal in `assistant/tools.py`
    (P5). Patching that function changes this tool's answer.
    """
    from crm import today as crm_today

    result, is_error = _call(user, "date_facts", {"query": "2026-12-24"})
    assert not is_error
    assert result["outreach_blackout"] == crm_today.outreach_blackout(
        date(2026, 12, 24)
    ) == "holiday"
    assert result["outreach_resumes"] == "Jan 4"

    with mock.patch.object(tools, "outreach_blackout", return_value="whenever"):
        patched, _ = _call(user, "date_facts", {"query": "2026-12-24"})
    assert patched["outreach_blackout"] == "whenever"


@pytest.mark.outreach_blackout
def test_date_facts_says_nothing_about_a_blackout_on_an_ordinary_weekday(user):
    """None, not an empty string, and no resume day at all — an ordinary
    Thursday answers exactly what it answered before this existed (P3)."""
    result, _ = _call(user, "date_facts", {"query": "2026-09-03"})

    assert result["outreach_blackout"] is None
    assert "outreach_resumes" not in result


@pytest.mark.outreach_blackout
def test_date_facts_answers_the_blackout_window_itself(user):
    """"When should I stop emailing over Christmas" is a real question, and
    both edges are read off `crm.today`'s own constants."""
    from crm.today import HOLIDAY_BLACKOUT_END, HOLIDAY_BLACKOUT_START

    result, is_error = _call(user, "date_facts", {"query": "holiday_blackout"})

    assert not is_error
    starts, ends = date.fromisoformat(result["starts"]), date.fromisoformat(result["ends"])
    assert (starts.month, starts.day) == HOLIDAY_BLACKOUT_START
    assert (ends.month, ends.day) == HOLIDAY_BLACKOUT_END
    assert result["outreach_resumes"]
