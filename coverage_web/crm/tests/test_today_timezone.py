"""The queue is scored against the STUDENT'S calendar day, not UTC's.

`settings.TIME_ZONE` is UTC; `accounts.middleware` activates the account's own
zone per request, and the founder's is Asia/Hong_Kong (+8). So for eight hours
out of every twenty-four — HK midnight to HK 8am, which is precisely when a
student on that clock opens the page — `timezone.now().date()` is still
YESTERDAY in Hong Kong.

`_build_actions` handed that raw UTC instant to `cadence.due_actions` as
`as_of`, and the engine does `today = as_of.date()` on it. Every business-day
threshold in the queue therefore ran a day behind during that window: the
follow-up clock, the park-after clock, and — the one these tests pin — the
`closes_on` filter that decides what counts as CRITICAL.

The fix is `timezone.localtime(...)`: the same instant, expressed in the
account's zone, so `.date()` yields the day the student is actually living in.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from crm.models import CalendarEvent, Contact, Touch, UserFirm
from crm.today import _build_actions
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db(transaction=True)

HK = ZoneInfo("Asia/Hong_Kong")

# 2026-08-27 20:00 UTC is 2026-08-28 04:00 in Hong Kong. The two calendar
# dates genuinely differ, which is the whole point of the fixture — pick any
# instant in the HK 00:00-08:00 band and the same disagreement holds.
INSTANT = datetime(2026, 8, 27, 20, 0, tzinfo=ZoneInfo("UTC"))
UTC_DAY = date(2026, 8, 27)
HK_DAY = date(2026, 8, 28)


def _hk_user(email):
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", timezone="Asia/Hong_Kong",
    )


def _frozen():
    """Patch the clock at the module Django itself reads, so `timezone.now`,
    `timezone.localtime` and `timezone.localdate` all agree — a test that
    freezes only one of them proves nothing about the other two."""
    return mock.patch("django.utils.timezone.now", return_value=INSTANT)


def test_the_fixture_actually_straddles_a_date_boundary():
    """Guard the guard. If this instant ever stops disagreeing between the two
    zones, every test below would pass while testing nothing."""
    assert INSTANT.astimezone(ZoneInfo("UTC")).date() == UTC_DAY
    assert INSTANT.astimezone(HK).date() == HK_DAY
    assert UTC_DAY != HK_DAY


def test_the_engine_is_asked_about_the_students_day_not_utcs():
    """The contract, stated directly: whatever instant reaches
    `cadence.due_actions`, `as_of.date()` must be the day the student is in.
    Everything the engine computes hangs off that one line."""
    user = _hk_user("tz-contract@example.com")
    Contact.all_objects.create(user=user, name="Anyone", school_affiliation=True)

    seen = {}
    from crm import today as today_mod

    real = today_mod.cadence.due_actions

    def spy(*args, **kw):
        seen["as_of"] = kw["as_of"]
        return real(*args, **kw)

    timezone.activate(HK)
    try:
        with _frozen(), mock.patch.object(today_mod.cadence, "due_actions", spy):
            _build_actions(user)
    finally:
        timezone.deactivate()

    assert seen["as_of"].date() == HK_DAY, (
        "the engine was handed a UTC calendar date; every business-day "
        "threshold in the queue is running a day behind"
    )
    # Same moment in time, only re-expressed. A fix that shifted the instant
    # would silently move every hours-based clock in the engine too.
    assert seen["as_of"] == INSTANT


def test_a_deadline_that_passed_last_night_is_not_a_critical_card(client):
    """THE MEASURED CONSEQUENCE, and why this belongs with the severity work.

    `cadence._closing_soon` drops an application close with `d < today`. A
    close dated 2026-08-27 is yesterday in Hong Kong and must be gone. Read
    against the UTC date it is still "today", survives the filter, and fires
    a `reping` — which `_is_critical` exempts from the daily cap and from
    Snooze, so the page's loudest, most un-dismissable card was being spent
    on a deadline the student had already missed.

    FAILS BEFORE THE FIX: revert `_build_actions`'s `now` to a bare
    `timezone.now()` and this test reports a `reping`.
    """
    user = _hk_user("tz-closed@example.com")
    firm = Firm.objects.create(name="Closed Bank", slug="closed-bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="us", event_kind="app_close",
        date=UTC_DAY, confidence=1.0,
    )
    contact = Contact.all_objects.create(
        user=user, name="Closed Person", firm=firm, warmth="replied",
        region="us", school_affiliation=True,
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="email", channel="email",
        ts=INSTANT - timedelta(days=40),
    )

    timezone.activate(HK)
    try:
        with _frozen():
            actions, _ = _build_actions(user)
    finally:
        timezone.deactivate()

    kinds = [a["action"] for a in actions if a["contact"]["id"] == contact.id]
    assert "reping" not in kinds, (
        f"a deadline that closed yesterday in the student's own timezone "
        f"still produced a critical re-ping: {kinds}"
    )


def test_a_deadline_still_open_today_keeps_its_critical_card(client):
    """The other half, so the fix is not just "drop more things". A close
    dated today in Hong Kong is live and must still raise its re-ping —
    otherwise this change would trade a false critical for a missed one,
    which is the strictly worse error."""
    user = _hk_user("tz-open@example.com")
    firm = Firm.objects.create(name="Open Bank", slug="open-bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="us", event_kind="app_close",
        date=HK_DAY, confidence=1.0,
    )
    contact = Contact.all_objects.create(
        user=user, name="Open Person", firm=firm, warmth="replied",
        region="us", school_affiliation=True,
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="email", channel="email",
        ts=INSTANT - timedelta(days=40),
    )

    timezone.activate(HK)
    try:
        with _frozen():
            actions, _ = _build_actions(user)
    finally:
        timezone.deactivate()

    kinds = [a["action"] for a in actions if a["contact"]["id"] == contact.id]
    assert "reping" in kinds, (
        f"a deadline still open today in the student's timezone lost its "
        f"critical card: {kinds}"
    )


# ---------------------------------------------------------------------------
# ONE CARD, ONE EVENT, TWO DIFFERENT NUMBERS.
# ---------------------------------------------------------------------------
# The section above fixed the AS-OF side: the engine is asked about the
# student's day. It left the other side of the same comparison alone. Touch
# timestamps still arrived in UTC, and `cadence` derives a touch's day with
# `_as_date(t["ts"])` — a `.date()` on whatever zone it was handed.
#
# So the engine counted from a UTC day and the act card's own ledger line
# counted from a local one (`crm.today`'s `last_on`, always
# `localtime(ts).date()`). Measured on the founder's live account 2026-08-31,
# his account on America/Los_Angeles: Youqi Chen's `chat_scheduled` touch is
# stored 2026-08-24 01:37Z, which is 2026-08-23 18:37 where he lives. The
# engine's sentence said "5 business days" and the row directly beneath it
# said "6 business days ago", about the same touch, in the same render.
#
# Six is the right answer — this product has a `TimezoneMiddleware` and this
# very file establishing that "today" means the user's today — and the fix is
# `crm.utils._touch_dicts`, which now localizes every `ts` at the one boundary
# they all cross rather than at any single call site.
#
# The zone here is the founder's real one for this incident and the boundary
# runs the OTHER way from the HK cases above: LA is behind UTC, so the local
# date is the EARLIER one and the un-fixed engine under-counted. Both
# directions are the same defect.
LA = ZoneInfo("America/Los_Angeles")
# Deliberately mid-morning in both zones, so `today` is not in question and
# the ONLY boundary in play is the touch's own.
LA_NOW = datetime(2026, 8, 31, 17, 0, tzinfo=ZoneInfo("UTC"))
# Youqi Chen's actual touch timestamp.
LA_TOUCH = datetime(2026, 8, 24, 1, 37, tzinfo=ZoneInfo("UTC"))


def test_the_touch_fixture_actually_straddles_a_date_boundary():
    """Guard the guard, same as the HK fixture above. If this timestamp ever
    stops disagreeing between the two zones, the test below passes while
    testing nothing."""
    assert LA_TOUCH.astimezone(ZoneInfo("UTC")).date() == date(2026, 8, 24)
    assert LA_TOUCH.astimezone(LA).date() == date(2026, 8, 23)
    assert LA_NOW.astimezone(ZoneInfo("UTC")).date() == LA_NOW.astimezone(LA).date()


def _la_confirm_chat_card():
    """One `confirm_chat` action for an LA student whose only touch lands on
    the wrong side of midnight in UTC. Returns the dressed action dict."""
    user = get_user_model().objects.create_user(
        email="tz-ledger@example.com", password="pw12345!",
        timezone="America/Los_Angeles",
    )
    contact = Contact.all_objects.create(
        user=user, name="Youqi Chen", warmth="replied",
        thread_state="chat_scheduled", school_affiliation=True,
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="chat_scheduled", channel="email",
        ts=LA_TOUCH,
    )
    timezone.activate(LA)
    try:
        with mock.patch("django.utils.timezone.now", return_value=LA_NOW):
            actions, _ = _build_actions(user)
    finally:
        timezone.deactivate()
    cards = [a for a in actions if a["action"] == "confirm_chat"]
    assert len(cards) == 1, f"expected one confirm_chat card, got {actions}"
    return cards[0]


def test_the_engines_business_days_and_the_ledgers_cannot_disagree():
    """The 5-vs-6 bug, pinned.

    FAILS BEFORE THE FIX: revert `crm.utils._touch_dicts` to passing `t.ts`
    raw and `ctx["business_days"]` reports 5 while `last_business_days`
    reports 6."""
    card = _la_confirm_chat_card()
    engine = card["ctx"]["business_days"]
    ledger = card["last_business_days"]
    assert engine == ledger, (
        f"one card is printing two different ages for one touch: the engine's "
        f"sentence says {engine} business days, the ledger row says {ledger}"
    )
    assert ledger == 6, (
        "and they agree on the STUDENT'S answer, not UTC's: the touch landed "
        "2026-08-23 in America/Los_Angeles"
    )
    assert card["last_on"] == date(2026, 8, 23)


def test_the_confirm_chat_card_never_states_a_scheduling_date_it_lacks():
    """Youqi Chen again, on the copy side.

    She has no `CalendarEvent` — the ordinary state, since Coverage only
    learns a chat's time from an .ics DTSTART — so the card must not name a
    day. It used to say "chat was scheduled 6 business days ago", rendering
    business days since the LAST TOUCH as if it were the day a booking was
    made, about a booking that never existed."""
    card = _la_confirm_chat_card()
    reason = card["reason"]
    assert "scheduled for" not in reason, "no chat time is on record"
    assert "was scheduled 6 business days ago" not in reason
    assert "nothing logged in 6 business days" in reason, (
        f"the card should say what it knows, and only that: {reason!r}"
    )
    assert card["ctx"]["scheduled_on"] is None


def test_a_real_calendar_event_lets_the_card_name_the_day():
    """The other half: when Coverage DOES hold a time, the card says it, in
    the student's own zone. `_prose_dates` then renders the ISO day as the
    "Aug 23" the rest of the card already speaks."""
    user = get_user_model().objects.create_user(
        email="tz-sched@example.com", password="pw12345!",
        timezone="America/Los_Angeles",
    )
    contact = Contact.all_objects.create(
        user=user, name="Booked Person", warmth="replied",
        thread_state="chat_scheduled", school_affiliation=True,
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="chat_scheduled", channel="email",
        ts=LA_TOUCH,
    )
    # Same boundary instant, so a UTC read would name Aug 24 and a local one
    # Aug 23. The card must speak the student's day here too.
    CalendarEvent.all_objects.create(
        user=user, contact=contact, title="Chat with Booked Person",
        starts_at=LA_TOUCH, kind=CalendarEvent.KIND_CHAT,
        source=CalendarEvent.SOURCE_CAPTURE,
    )
    timezone.activate(LA)
    try:
        with mock.patch("django.utils.timezone.now", return_value=LA_NOW):
            actions, _ = _build_actions(user)
    finally:
        timezone.deactivate()

    card = [a for a in actions if a["action"] == "confirm_chat"][0]
    assert card["ctx"]["scheduled_on"] == "2026-08-23"
    assert "Aug 23" in card["reason"], card["reason"]


def test_a_chat_still_ahead_of_the_student_raises_no_card_at_all():
    """"Did it happen?" is the wrong question about a meeting that has not.

    A chat booked three weeks out, with the thread quiet since the invite,
    used to get the full stale-chat nag because the only clock consulted was
    the last touch's."""
    user = get_user_model().objects.create_user(
        email="tz-future@example.com", password="pw12345!",
        timezone="America/Los_Angeles",
    )
    contact = Contact.all_objects.create(
        user=user, name="Future Person", warmth="replied",
        thread_state="chat_scheduled", school_affiliation=True,
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="chat_scheduled", channel="email",
        ts=LA_TOUCH,
    )
    CalendarEvent.all_objects.create(
        user=user, contact=contact, title="Chat with Future Person",
        starts_at=LA_NOW + timedelta(days=14), kind=CalendarEvent.KIND_CHAT,
        source=CalendarEvent.SOURCE_CAPTURE,
    )
    timezone.activate(LA)
    try:
        with mock.patch("django.utils.timezone.now", return_value=LA_NOW):
            actions, _ = _build_actions(user)
    finally:
        timezone.deactivate()

    assert [a["action"] for a in actions if a["contact"]["id"] == contact.id] == []


def test_a_cancelled_chat_contributes_no_date_to_the_card():
    """`thread_state` stays "chat_scheduled" through a cancellation, so naming
    the called-off time would print "chat was scheduled for Aug 23, did it
    happen?" about a meeting nobody attended. The card falls back to the
    no-day sentence, whose "log the chat or reschedule" is exactly right, and
    the contact does NOT get suppressed as a future chat either."""
    user = get_user_model().objects.create_user(
        email="tz-cancelled@example.com", password="pw12345!",
        timezone="America/Los_Angeles",
    )
    contact = Contact.all_objects.create(
        user=user, name="Cancelled Person", warmth="replied",
        thread_state="chat_scheduled", school_affiliation=True,
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="chat_scheduled", channel="email",
        ts=LA_TOUCH,
    )
    CalendarEvent.all_objects.create(
        user=user, contact=contact, title="Chat with Cancelled Person",
        starts_at=LA_NOW + timedelta(days=14), kind=CalendarEvent.KIND_CHAT,
        source=CalendarEvent.SOURCE_CAPTURE, cancelled_at=LA_NOW,
    )
    timezone.activate(LA)
    try:
        with mock.patch("django.utils.timezone.now", return_value=LA_NOW):
            actions, _ = _build_actions(user)
    finally:
        timezone.deactivate()

    card = [a for a in actions if a["action"] == "confirm_chat"][0]
    assert card["ctx"]["scheduled_on"] is None
    assert "nothing logged in 6 business days" in card["reason"]


# ---------------------------------------------------------------------------
# The Recent Activity rail must count on the student's clock (2026-09-01).
# It used `timesince(..., depth=1)`, a raw elapsed UTC floor, while every
# other CRM surface uses `crm.utils._calendar_days_ago` -- whose docstring
# exists to abolish exactly that drift. Live on the founder's board one
# touch read "4 days" in this rail and "5d since last touch" on the same
# contact's Network card. (The act card's "3 business days ago" is a
# deliberate difference: that is the cadence engine's own reasoning and the
# sentence says so.)
# ---------------------------------------------------------------------------
def test_the_activity_rail_counts_days_on_the_students_own_clock():
    from crm.today import _cockpit_context

    user = get_user_model().objects.create_user(
        email="tz-rail@example.com", password="pw12345!",
        timezone="America/Los_Angeles",
    )
    contact = Contact.all_objects.create(user=user, name="Rail Clock")
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        ts=LA_TOUCH,
    )

    timezone.activate(LA)
    try:
        with mock.patch("django.utils.timezone.now", return_value=LA_NOW):
            ctx = _cockpit_context(user)
    finally:
        timezone.deactivate()

    row = next(a for a in ctx["activity"] if a["name"] == "Rail Clock")
    # LA_TOUCH is Aug 23 in Los Angeles and LA_NOW is Aug 31: eight calendar
    # days. The raw elapsed floor would have said seven.
    assert row["ago"] == "8d ago", row["ago"]


# ---------------------------------------------------------------------------
# The rail must not render a negative day count (2026-09-01). `recent` has no
# `ts__lte` guard the way `crm.debrief.pending` does, so a touch dated after
# `as_of` sorts to the top of the feed instead of being excluded from it.
# Nothing on the founder's live account is future-dated, but
# `coverage_domain.cadence` and `.scoring` both treat a future-dated touch as
# reachable rather than hypothetical (a chat hand-logged with tomorrow's
# date, or a caller whose clock runs behind the touch's) and guard it —
# this pins the rail to the same posture.
# ---------------------------------------------------------------------------
def test_the_activity_rail_does_not_render_a_negative_day_count():
    from crm.today import _cockpit_context

    user = get_user_model().objects.create_user(
        email="future-rail@example.com", password="pw12345!",
    )
    now = timezone.now()
    contact = Contact.all_objects.create(user=user, name="Future Chat")
    Touch.all_objects.create(
        user=user, contact=contact, kind="chat_scheduled", channel="email",
        ts=now + timedelta(days=2),
    )

    with mock.patch("django.utils.timezone.now", return_value=now):
        ctx = _cockpit_context(user)

    row = next(a for a in ctx["activity"] if a["name"] == "Future Chat")
    assert row["ago"] == "today", row["ago"]


# ---------------------------------------------------------------------------
# The rail is the student's work, not the system's bookkeeping (2026-09-01).
#
# Measured on the demo board: six of the six rows this rail rendered read
# "<name> · Updated manually" -- `manual_override` rows, the audit kind
# `crm.pipeline` writes when the SYSTEM changes a contact's state so the log
# has no gap. A card titled Recent Activity was presenting those as the
# week's relationship work, three cards below a pace ring reading 0/17 for
# the same week. Spec E1/C2: audit rows are not the student's work, and the
# ring already excludes this kind structurally (`PACE_TOUCH_KINDS`). The rail
# now agrees with the ring instead of contradicting it.
#
# The full log, with the richer note-aware wording from
# `crm.views._override_label`, is still on the contact's own History page,
# which is where an audit trail belongs.
# ---------------------------------------------------------------------------
def test_the_activity_rail_leaves_out_the_systems_own_audit_rows():
    from crm.today import _cockpit_context

    user = get_user_model().objects.create_user(
        email="audit-rail@example.com", password="pw12345!",
    )
    now = timezone.now()
    bookkeeping = Contact.all_objects.create(user=user, name="Audit Row")
    real = Contact.all_objects.create(user=user, name="Real Work")
    # The audit rows are NEWER, so a rail that did not filter would show
    # nothing else: six of them fill a six-row feed on their own.
    for i in range(6):
        Touch.all_objects.create(
            user=user, contact=bookkeeping, kind="manual_override",
            channel="other", ts=now - timedelta(hours=i + 1),
        )
    Touch.all_objects.create(
        user=user, contact=real, kind="outreach", channel="email",
        ts=now - timedelta(days=3),
    )

    with mock.patch("django.utils.timezone.now", return_value=now):
        ctx = _cockpit_context(user)

    names = [a["name"] for a in ctx["activity"]]
    assert names == ["Real Work"], names
    assert all(a["kind"] != "manual_override" for a in ctx["activity"])


def test_the_activity_rail_still_reports_mail_it_did_not_send():
    """`bulk_received` stays in: the rail reports what happened.

    The pace ring excludes it because a newsletter landing is not work the
    student did -- but this rail answers a different question ("what moved on
    my relationships since I last looked"), and a blast arriving is a real
    answer to it. Only the SYSTEM's own writes are silent here.
    """
    from crm.today import _cockpit_context

    user = get_user_model().objects.create_user(
        email="blast-rail@example.com", password="pw12345!",
    )
    now = timezone.now()
    contact = Contact.all_objects.create(user=user, name="Newsletter Sender")
    Touch.all_objects.create(
        user=user, contact=contact, kind="bulk_received", channel="email",
        ts=now - timedelta(days=1),
    )

    with mock.patch("django.utils.timezone.now", return_value=now):
        ctx = _cockpit_context(user)

    assert [a["name"] for a in ctx["activity"]] == ["Newsletter Sender"]
