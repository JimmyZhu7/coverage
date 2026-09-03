"""Today page: the honesty rules, the capacity policy, and the ordering.

Everything here pins a claim the page makes to a student. The page's whole
pitch is that it is more trustworthy than the spreadsheet they already keep,
so a number that over-claims is not a cosmetic bug — each of these tests
corresponds to a measured over-claim on the founder's live data.

`transaction=True` on the module: several cases go through `crm.services`,
which opens its own psycopg connection outside Django's test transaction and
therefore cannot see uncommitted rows.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from crm.models import CalendarEvent, ChatDebrief, Contact, Touch, UserFirm
from crm.views import (
    PACE_TOUCH_KINDS,
    TODAY_PLAN_MAX,
    TODAY_PLAN_MIN,
    _cockpit_context,
    _daily_cap,
    _dashboard_context,
    _workdays_left,
)
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="today@example.com", **kw):
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", **kw
    )


def _contact(*, user, **kw):
    """A contact the queue is ALLOWED to speak about.

    The Today queue gained a relevance gate on 2026-08-22 (`crm.relevance`): a
    contact only generates a daily action if they are at one of the student's
    tiered firms, share the student's school, or wrote and are still waiting on
    an answer. Every test in this file is about some OTHER rule — the pace
    ring, the cap, the ordering, the honesty of a number — so the fixtures
    default to the school tie. It is the cheapest way to be relevant (one
    boolean, no Firm and no UserFirm row) and it changes nothing else the
    assertions look at.

    Tests that exercise the gate ITSELF pass `school_affiliation=False`
    explicitly, which this respects; see the gate's own section at the foot of
    the file.
    """
    kw.setdefault("school_affiliation", True)
    return Contact.all_objects.create(user=user, **kw)


def _touch(user, contact, kind, *, days_ago=0, channel="email"):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel=channel,
        ts=timezone.now() - timedelta(days=days_ago),
    )


# ---------------------------------------------------------------------------
# E1. The pace ring counts YOUR work, and nothing else.
# ---------------------------------------------------------------------------
def test_a_purely_inbound_week_reads_zero():
    """The measured bug, reproduced. The founder's ring read 9/14 in a week he
    had sent nothing: replies and scheduling confirmations written by the
    capture pipeline off INBOUND mail, plus the audit rows the system writes
    to itself. A progress meter that fills while you do nothing is worthless."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ada Lovelace")
    for kind in ("reply_received", "chat_scheduled", "chat_scheduled",
                 "chat_scheduled", "reply_received", "chat_scheduled",
                 "chat_scheduled", "chat_scheduled", "manual_override"):
        _touch(user, c, kind)

    pace = _cockpit_context(user)["pace"]
    assert pace["done"] == 0, "other people's actions must not fill your ring"
    assert pace["goal"] == 14
    assert pace["remaining"] == 14


def test_your_own_work_does_count():
    """The other side of the boundary — this must not become a ring that
    never moves. Every kind the ratchet knows about counts except the two
    inbound ones."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ethan Gao")
    for kind in ("outreach", "follow_up", "thank_you", "maintain", "reping", "chat"):
        _touch(user, c, kind)

    assert _cockpit_context(user)["pace"]["done"] == 6


def test_manual_override_audit_rows_never_count():
    """`set_state`'s audit row records that the SYSTEM wrote something down.
    It is excluded structurally: pipeline keeps it out of TOUCH_TRANSITIONS,
    and PACE_TOUCH_KINDS is derived from TOUCH_TRANSITIONS."""
    assert "manual_override" not in PACE_TOUCH_KINDS
    assert "reply_received" not in PACE_TOUCH_KINDS
    assert "chat_scheduled" not in PACE_TOUCH_KINDS
    assert {"outreach", "follow_up", "thank_you", "maintain", "reping", "chat"} <= PACE_TOUCH_KINDS


def test_last_weeks_work_does_not_count_toward_this_week():
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Old Work")
    _touch(user, c, "outreach", days_ago=14)
    assert _cockpit_context(user)["pace"]["done"] == 0


# ---------------------------------------------------------------------------
# E9. The pace note's wording (2026-08-31, the founder's own phrasing:
# "outreach this week" / "N more to go", with "touches" dropped). The old
# copy needed a dedicated test because `pluralize` with no argument on
# "touch" yields "touchs", not "touches" -- dropping the word retires that
# gotcha along with the sentence, so this now just pins the current copy.
# ---------------------------------------------------------------------------
def test_pace_note_reads_more_to_go(client):
    user = _user(weekly_touch_goal=14)
    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "outreach this week" in body
    # The figure is bolded, so the sentence is not one contiguous string.
    assert "<b>14</b> more to go" in body


# ---------------------------------------------------------------------------
# B. Capacity: the cap derives from the existing weekly goal, and it caps.
# ---------------------------------------------------------------------------
def test_workdays_left_counts_mon_to_fri_through_sunday():
    assert _workdays_left(date(2026, 7, 27)) == 5   # Monday
    assert _workdays_left(date(2026, 7, 29)) == 3   # Wednesday
    assert _workdays_left(date(2026, 7, 31)) == 1   # Friday
    # A weekend has no workdays left; the floor of 1 keeps the plan sized as
    # "everything still owed, today" rather than dividing by zero.
    assert _workdays_left(date(2026, 8, 1)) == 1    # Saturday
    assert _workdays_left(date(2026, 8, 2)) == 1    # Sunday


def test_daily_cap_spreads_the_weekly_goal_over_the_days_left():
    # The founder's measured case: goal 14, nothing done, Wednesday.
    assert _daily_cap(14, 0, date(2026, 7, 29)) == 5
    # Same goal on Monday spreads wider.
    assert _daily_cap(14, 0, date(2026, 7, 27)) == 3
    # Behind on a Friday: the cap climbs to catch up, like Linear capacity —
    # but only as far as TODAY_PLAN_MAX, which came down from 12 to 5 when the
    # queue learned to rank by expected value. Twelve was a ceiling on an
    # unranked list; with the top five actually being the five best things
    # available, a longer plan buys volume rather than value, and the rest is
    # one click away under "Up next".
    assert _daily_cap(14, 0, date(2026, 7, 31)) == TODAY_PLAN_MAX == 5


def test_daily_cap_respects_its_floor_and_ceiling():
    assert _daily_cap(1, 0, date(2026, 7, 27)) == TODAY_PLAN_MIN
    assert _daily_cap(14, 14, date(2026, 7, 27)) == TODAY_PLAN_MIN
    assert _daily_cap(500, 0, date(2026, 7, 31)) == TODAY_PLAN_MAX


def test_the_cap_actually_caps_and_the_remainder_is_stated_exactly():
    """A wall of identical cards converts to mass one-click logging (fabricated
    data) or abandonment. The plan is capped; the rest is visible and counted,
    never dropped."""
    user = _user(weekly_touch_goal=14)
    for i in range(30):
        c = _contact(user=user, name=f"Cold {i:02d}")
        _touch(user, c, "outreach", days_ago=20)

    ctx = _cockpit_context(user)
    cap = ctx["daily_cap"]
    assert ctx["queue_total"] == 30
    assert ctx["planned_total"] == cap
    # The remainder is the exact arithmetic complement — nothing vanished.
    assert ctx["held_total"] == 30 - cap
    assert ctx["planned_total"] + ctx["held_total"] == ctx["queue_total"]


def test_held_items_are_still_reachable_in_full(client):
    """E3: held is not gone. Show all expands to the complete queue."""
    user = _user(weekly_touch_goal=14)
    for i in range(30):
        c = _contact(user=user, name=f"Coldperson {i:02d}")
        _touch(user, c, "outreach", days_ago=20)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    for i in range(30):
        assert f"Coldperson {i:02d}" in body, "the cap paces, it must never filter"
    assert "pacing out at" in body


def test_a_capped_lane_header_carries_its_denominator(client):
    """E2: never a count that mixes shown with hidden."""
    user = _user(weekly_touch_goal=14)
    for i in range(30):
        c = _contact(user=user, name=f"Cold {i:02d}")
        _touch(user, c, "outreach", days_ago=20)

    client.force_login(user)
    ctx = client.get(reverse("crm:week")).context
    cold = [lane for lane in ctx["lanes"] if lane["key"] == "cold"][0]
    assert cold["capped"] is True
    assert cold["count"] == ctx["daily_cap"]
    assert cold["total"] == 30


# ---------------------------------------------------------------------------
# B. Ordering: momentum beats tier. The thesis, enforced in the sort key.
# ---------------------------------------------------------------------------
def test_a_warm_contact_at_an_unranked_firm_outranks_a_cold_one_at_tier_one():
    """Measured: positions 1-29 were cold non-repliers at Citi/Goldman, every
    warm contact below the fold, because all six common action kinds share
    cadence priority 1 and the tiebreak was firm alphabet."""
    user = _user(weekly_touch_goal=14)
    citi = Firm.objects.create(name="Citi", slug="citi")
    UserFirm.all_objects.create(user=user, firm=citi, tier=1)

    for i in range(10):
        c = _contact(user=user, name=f"Cold {i:02d}", firm=citi)
        _touch(user, c, "outreach", days_ago=20)

    warm = _contact(
        user=user, name="Warm Alum", firm_text="USC",
        warmth="replied", thread_state="replied",
    )
    _touch(user, warm, "reply_received", days_ago=10)

    ctx = _cockpit_context(user)
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    names = [a["contact"]["name"] for a in planned]
    assert "Warm Alum" in names, "the replied human must make today's plan at all"
    assert names[0] == "Warm Alum", f"momentum must outrank tier, got {names}"


def test_within_a_class_the_longest_silent_goes_first():
    """Ordering only — every contact here must already be DUE.

    The offsets are calendar days but the cadence threshold
    (`followup_after_business_days`, 6) is in BUSINESS days, and the two
    diverge by weekday: 8 calendar days is 6 business days Mon-Fri but only 5
    on a Saturday. The original 8 therefore made this test pass five days a
    week and fail on the weekend — a real failure, seen on Sat 2026-08-01,
    not flakiness. 10 is the smallest offset that clears 6 business days on
    every weekday (and still sits under the park threshold), so the test now
    measures the ordering it is named for rather than the day it runs on.

    The top offset is 20, not 30 (rewritten 2026-09-01, pinning the corrected
    behaviour): a first note has a shelf life now
    (`followup_expires_after_business_days`, 15, strict `>`), and 30 calendar
    days is 20-22 business days — a PARK, off the plan entirely. 20 calendar
    days is 14 or 15 business days on every weekday, so it is the longest
    silence that is still a follow-up whatever day this runs.
    """
    user = _user(weekly_touch_goal=14)
    for days in (10, 20, 15):
        c = _contact(user=user, name=f"Silent {days:02d}")
        _touch(user, c, "outreach", days_ago=days)

    ctx = _cockpit_context(user)
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert [a["contact"]["name"] for a in planned] == ["Silent 20", "Silent 15", "Silent 10"]


def test_a_confirmed_deadline_is_never_capped_away():
    """Fill rule 1: class 0 shows in full even past the cap. A pre-deadline
    re-ping is the highest-value nudge the engine has."""
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(name="Moelis", slug="moelis")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    from directory.models import FirmDate
    FirmDate.objects.create(
        firm=firm, event_kind="app_close", region="us",
        date=timezone.localdate() + timedelta(days=5), confidence=1.0, precision="day",
    )
    for i in range(30):
        c = _contact(user=user, name=f"Cold {i:02d}")
        _touch(user, c, "outreach", days_ago=20)
    warm = _contact(
        user=user, name="Deadline Person", firm=firm, region="us",
        warmth="chatted", thread_state="replied",
    )
    _touch(user, warm, "chat", days_ago=40)

    ctx = _cockpit_context(user)
    critical = [lane for lane in ctx["lanes"] if lane["key"] == "critical"][0]
    assert [a["action"] for a in critical["items"]] == ["reping"]
    assert ctx["planned_total"] > ctx["daily_cap"] or ctx["daily_cap"] >= 1


# ---------------------------------------------------------------------------
# C1 reaching the page: the chatted dead end is closed.
# ---------------------------------------------------------------------------
def test_a_chatted_contact_reappears_on_today(client):
    """13 of the founder's 14 chatted contacts produced nothing at all: the
    thank-you prompt had expired and no branch covered them afterwards."""
    user = _user(weekly_touch_goal=14)
    c = _contact(
        user=user, name="Grace Hopper", warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=40)
    _touch(user, c, "thank_you", days_ago=39)

    client.force_login(user)
    resp = client.get(reverse("crm:week"))
    body = resp.content.decode()
    assert "Grace Hopper" in body
    planned = [a for lane in resp.context["lanes"] for a in lane["items"]]
    assert [a["action"] for a in planned] == ["keep_warm"]


def test_keep_warm_ranks_above_a_cold_follow_up():
    user = _user(weekly_touch_goal=14)
    citi = Firm.objects.create(name="Citi", slug="citi")
    UserFirm.all_objects.create(user=user, firm=citi, tier=1)
    for i in range(10):
        cold = _contact(user=user, name=f"Cold {i:02d}", firm=citi)
        _touch(user, cold, "outreach", days_ago=20)

    warm = _contact(
        user=user, name="Chatted Human", warmth="chatted", thread_state="chat_done",
    )
    _touch(warm.user, warm, "chat", days_ago=40)
    _touch(warm.user, warm, "thank_you", days_ago=39)

    ctx = _cockpit_context(user)
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert planned[0]["contact"]["name"] == "Chatted Human"


def test_keep_warm_logs_an_existing_touch_kind_and_moves_no_state(client):
    """`keep_warm` maps to the `maintain` touch kind, whose TOUCH_TRANSITIONS
    entry is (None, None) — the ratchet is untouched by this feature."""
    user = _user(weekly_touch_goal=14)
    c = _contact(
        user=user, name="Marie Curie", warmth="chatted", thread_state="chat_done",
    )
    _touch(user, c, "chat", days_ago=40)
    _touch(user, c, "thank_you", days_ago=39)

    client.force_login(user)
    resp = client.post(
        reverse("crm:today_act", args=[c.id, "sent"]), {"kind": "maintain"}
    )
    assert resp.status_code == 200
    c.refresh_from_db()
    assert (c.warmth, c.thread_state) == ("chatted", "chat_done")
    assert Touch.all_objects.filter(user=user, contact=c, kind="maintain").exists()


# ---------------------------------------------------------------------------
# F3 / D. The card says WHY, and shows its evidence.
# ---------------------------------------------------------------------------
def test_the_card_renders_the_reason_and_the_last_touch(client):
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ethan Gao")
    _touch(user, c, "outreach", days_ago=20)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "No reply" in body and "follow up" in body.lower()
    assert "Last: Reached out" in body
    assert "business day" in body


def test_a_queue_row_keeps_its_three_zones(client):
    """The queue is a ledger: identity, context, actions, in that order on one
    row. The zones are what make the row readable at a glance and what the
    stylesheet lays out — a card that loses one of them silently reverts to
    the poster layout this replaced."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ethan Gao")
    _touch(user, c, "outreach", days_ago=20)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    row = body.split('class="act-card', 1)[1]
    for zone in ("act-ident", "act-context", "act-quick"):
        assert zone in row, zone
    assert row.index("act-ident") < row.index("act-context") < row.index("act-quick")


def test_a_contact_with_no_touches_says_so_rather_than_guessing(client):
    user = _user(weekly_touch_goal=14)
    _contact(user=user, name="Brand New")
    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "No touches on record" in body


def test_an_audit_row_is_not_shown_as_a_touch(client):
    """The evidence line reads the same real-touch clock the engine does: a
    state correction is not something you did to the relationship."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Corrected Person")
    _touch(user, c, "outreach", days_ago=20)
    _touch(user, c, "manual_override", days_ago=1)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Last: Reached out" in body
    assert "Last: Manual_override" not in body


# ---------------------------------------------------------------------------
# F5 / D. Verb honesty.
# ---------------------------------------------------------------------------
def test_the_log_button_does_not_claim_to_have_sent_anything(client):
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ethan Gao")
    _touch(user, c, "outreach", days_ago=20)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert ">Done<" in body
    assert ">Sent<" not in body
    assert ">They replied<" in body
    assert ">Reply<" not in body


def test_every_quick_action_names_its_contact_for_a_screen_reader(client):
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ethan Gao", email="e@x.com")
    _touch(user, c, "outreach", days_ago=20)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    for label in (
        'aria-label="Mark follow up to Ethan Gao done"',
        'aria-label="Record that Ethan Gao replied"',
        'aria-label="Compose an email to Ethan Gao"',
        'aria-label="Snooze Ethan Gao for 3 days"',
        'aria-label="Dismiss Ethan Gao for today"',
    ):
        assert label in body, label


def test_confirm_chat_is_a_two_step_and_never_one_click_logs_a_chat(client):
    """One click asserting a conversation happened is the biggest claim on the
    page, made on the one card that exists because nobody knows if it did."""
    user = _user(weekly_touch_goal=14)
    c = _contact(
        user=user, name="Rosalind Franklin", warmth="replied", thread_state="chat_scheduled",
    )
    _touch(user, c, "chat_scheduled", days_ago=14)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Log the chat" in body
    assert '"kind": "chat"}' not in body, "a chat must never be one POST away"
    # The two-step lands on a pre-filled form, not a fait accompli.
    assert f"/app/contacts/{c.id}/?log=chat#contact-live" in body
    detail = client.get(reverse("crm:contact_detail", args=[c.id]), {"log": "chat"})
    assert '<option value="chat" selected>' in detail.content.decode()


def test_compose_flags_a_ready_draft_and_stays_quiet_otherwise(client):
    """Inverted 2026-08-02. The badge used to read "no draft" and fire when
    the opener was blank — true of 115 of 115 real contacts, so it appeared
    on every card on the page and distinguished nothing. It now marks the
    exception, which is the state actually worth seeing."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="No Draft", email="nd@x.com")
    _touch(user, c, "outreach", days_ago=20)
    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Draft ready" not in body, "the common case earns no badge"
    assert "no draft" not in body, "and no badge for its negation either"

    c.opener = "Hi there, I'm a sophomore at USC..."
    c.save(update_fields=["opener"])
    assert "Draft ready" in client.get(reverse("crm:week")).content.decode()


def test_the_firm_slot_only_says_alum_for_an_actual_alum(client):
    """A hand-added contact has no firm_id whether the free text says "USC" or
    "HSBC". Keying the chip on a missing firm_id labelled eight HSBC bankers
    alumni on the live page."""
    user = _user(weekly_touch_goal=14)
    alum = _contact(
        user=user, name="Kristin Welty", firm_text="USC", school_affiliation=True,
    )
    banker = _contact(
        user=user, name="Hsbc Banker", firm_text="HSBC", school_affiliation=False,
    )
    _touch(user, alum, "outreach", days_ago=20)
    _touch(user, banker, "outreach", days_ago=20)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Usc · alum" in body or "USC · alum" in body.replace("Usc", "USC")
    assert "Hsbc · alum" not in body
    assert "HSBC · alum" not in body


# ---------------------------------------------------------------------------
# E8. Snooze hides a nag; it must not be able to hide a deadline.
# ---------------------------------------------------------------------------
def test_snooze_hides_the_follow_up_it_was_clicked_on():
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Snoozed Cold")
    _touch(user, c, "outreach", days_ago=20)
    Contact.all_objects.filter(pk=c.pk).update(
        snoozed_until=timezone.now() + timedelta(days=3)
    )
    assert _cockpit_context(user)["queue_total"] == 0


def test_snooze_cannot_swallow_a_pre_deadline_reping():
    """The old implementation dropped the CONTACT from the engine's input, so
    a 3-day snooze on a nagging follow-up also ate any priority-0 re-ping that
    fell inside the window — silently, and it is the most valuable action the
    engine produces."""
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(name="Moelis", slug="moelis")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    from directory.models import FirmDate
    FirmDate.objects.create(
        firm=firm, event_kind="app_close", region="us",
        date=timezone.localdate() + timedelta(days=5), confidence=1.0, precision="day",
    )
    c = _contact(
        user=user, name="Snoozed Warm", firm=firm, region="us",
        warmth="chatted", thread_state="replied",
    )
    _touch(user, c, "chat", days_ago=40)
    Contact.all_objects.filter(pk=c.pk).update(
        snoozed_until=timezone.now() + timedelta(days=3)
    )

    ctx = _cockpit_context(user)
    actions = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert [a["action"] for a in actions] == ["reping"]


def test_skip_dismisses_a_confirm_chat_card_for_the_day():
    """confirm_chat used to sit in the snooze-exempt set alongside reping, so
    the card's own Skip button wrote snoozed_until and then re-rendered the
    exact same card — a control that visibly did nothing (reported
    2026-08-07). A re-ping guards an external deadline; confirm-chat is a
    question, and "ask me tomorrow" is a legitimate answer to a question."""
    user = _user(weekly_touch_goal=14)
    c = _contact(
        user=user, name="Cindy So", warmth="replied", thread_state="chat_scheduled",
    )
    _touch(user, c, "chat_scheduled", days_ago=12)
    ctx = _cockpit_context(user)
    actions = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert "confirm_chat" in [a["action"] for a in actions], "card present before skip"

    Contact.all_objects.filter(pk=c.pk).update(
        snoozed_until=timezone.now() + timedelta(days=1)
    )
    ctx = _cockpit_context(user)
    actions = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert "confirm_chat" not in [a["action"] for a in actions], "skip now works"


def test_a_reping_card_offers_no_skip_because_skip_would_lie(client):
    """The exempt kind draws no Snooze/Skip at all: clicking them wrote
    snoozed_until (silently snoozing the contact's OTHER actions) while the
    visible card stayed put. No buttons is honest; broken buttons are not."""
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(name="Moelis", slug="moelis")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    from directory.models import FirmDate
    FirmDate.objects.create(
        firm=firm, event_kind="app_close", region="us",
        date=timezone.localdate() + timedelta(days=5), confidence=1.0, precision="day",
    )
    c = _contact(
        user=user, name="Reping Target", firm=firm, region="us",
        warmth="chatted", thread_state="replied",
    )
    _touch(user, c, "chat", days_ago=40)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    import re
    card = re.search(r"Reping Target.*?</article>", body, re.S).group(0)
    assert "Skip" not in card and "Snooze" not in card
    # Park it is the SAME exemption for a stronger reason: parking silences
    # every future reminder about this person, not just today's, and the
    # one card that exists because a confirmed deadline is imminent is
    # exactly the one card that must never be permanently dismissable.
    assert "Park it" not in card


# ---------------------------------------------------------------------------
# The manual "never see this again" — Park it as a ghost button on an
# ordinary (non-quiet) card, alongside Snooze/Skip rather than gated behind
# the engine already deciding the contact has gone stale.
# ---------------------------------------------------------------------------
def test_an_ordinary_card_offers_park_it_next_to_snooze_and_skip(client):
    """The only way to permanently stop a reminder used to be waiting for
    the cadence engine to decide a contact had gone quiet. A student who
    simply does not want to keep seeing a reminder for someone had no
    control that said so — only Snooze (3 days) and Skip (1 day), both of
    which come back."""
    user = _user(weekly_touch_goal=14)
    c = _contact(
        user=user, name="Shelby Dibs", warmth="cold", thread_state="no_reply",
    )
    _touch(user, c, "outreach", days_ago=20)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    import re
    card = re.search(r"Shelby Dibs.*?</article>", body, re.S).group(0)
    assert "Snooze" in card and "Skip" in card
    assert "Park it" in card
    assert f'{reverse("crm:today_act", args=[c.id, "park"])}' in card


def test_the_ghost_park_it_button_actually_parks(client):
    """Not just markup — the button has to do what the primary Park it
    button does: the audited override, one manual_override touch, contact
    stays on the board."""
    user = _user(weekly_touch_goal=14)
    c = _contact(
        user=user, name="Shelby Dibs", warmth="cold", thread_state="no_reply",
    )
    _touch(user, c, "outreach", days_ago=20)
    client.force_login(user)

    client.post(reverse("crm:today_act", args=[c.id, "park"]))

    c.refresh_from_db()
    assert c.thread_state == "parked"
    assert Touch.all_objects.filter(
        user=user, contact=c, kind="manual_override"
    ).exists()
    # And they are OFF today's queue, not deleted or archived. Scoped to
    # the cockpit specifically: the activity feed further down the SAME
    # page is a log of what happened and correctly still names her in its
    # own "Updated manually" line (crm.utils.TOUCH_KIND_LABELS) — this
    # checks the QUEUE, not the whole page.
    assert c.archived is False
    ctx = _cockpit_context(user)
    queued_names = [a["contact"]["name"] for lane in ctx["lanes"] for a in lane["items"]]
    assert "Shelby Dibs" not in queued_names


def test_the_gone_quiet_lane_does_not_get_a_second_park_button(client):
    """`a.action == "park"` already renders Park it as the PRIMARY button.
    The new ghost version is gated on `a.action != "park"` specifically so
    that card doesn't show the same verb twice."""
    user = _user(weekly_touch_goal=14)
    c = _contact(
        user=user, name="Gone Quiet Guy", warmth="advocate",
        thread_state="replied",
    )
    _touch(user, c, "chat", days_ago=90)  # well past the advocate keep-warm window

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    import re
    card = re.search(r"Gone Quiet Guy.*?</article>", body, re.S).group(0)
    assert card.count("Park it") == 1


# ---------------------------------------------------------------------------
# E5 / E6. What the page must not write.
# ---------------------------------------------------------------------------
def test_compose_is_a_link_and_writes_nothing(client):
    """A `mailto:` is not a send. Compose must never log a touch, or the ring
    fills and the warmth clock resets for an email that was never written."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ethan Gao", email="e@x.com")
    _touch(user, c, "outreach", days_ago=20)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    start = body.index('aria-label="Compose an email to Ethan Gao"')
    tag = body[body.rindex("<", 0, start):body.index(">", start) + 1]
    assert tag.startswith("<a "), tag
    assert "hx-post" not in tag
    assert Touch.all_objects.filter(user=user, contact=c).count() == 1


def test_pacing_a_follow_up_out_never_produces_a_second_one(client):
    """E6. Holding a follow-up back and surfacing it days later must leave it
    follow-up #1-and-only. Once it is logged, the next thing that contact can
    ever produce is a park — never another follow-up."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ethan Gao")
    # 10 calendar days, not 40 (rewritten 2026-09-01, pinning the corrected
    # behaviour): 10 is the weekday-proof "follow-up is due" offset the
    # engine's own tests use, and it sits inside the follow-up's shelf life.
    # At 40 the first note is 28-30 business days old, and since branch 6
    # gained `followup_expires_after_business_days` (15) that thread is a
    # `park`, not a follow-up — the fixture was relying on the very
    # never-expires defect the change removed.
    _touch(user, c, "outreach", days_ago=10)

    ctx = _cockpit_context(user)
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert [a["action"] for a in planned] == ["follow_up"]

    client.force_login(user)
    client.post(reverse("crm:today_act", args=[c.id, "sent"]), {"kind": "follow_up"})

    # Age the whole thread past every window and look again: the note to 40
    # days, the freshly logged follow-up to 30. Both, not just the follow-up
    # (rewritten 2026-09-01): the note above now starts at 10 days so it is
    # still a follow-up rather than an expired park, and the engine's park
    # clock reads the LATEST real touch, so a follow-up dated before the
    # note it follows would leave the 10-day-old note as the clock and park
    # nothing. Aging both keeps the order a real thread has.
    Touch.all_objects.filter(user=user, contact=c, kind="outreach").update(
        ts=timezone.now() - timedelta(days=40)
    )
    Touch.all_objects.filter(user=user, contact=c, kind="follow_up").update(
        ts=timezone.now() - timedelta(days=30)
    )
    again = [a["action"] for lane in _cockpit_context(user)["lanes"]
             for a in lane["items"]]
    assert "follow_up" not in again
    assert [a["action"] for a in _cockpit_context(user)["park_actions"]] == ["park"]


# ---------------------------------------------------------------------------
# F9. The park wall.
# ---------------------------------------------------------------------------
def test_park_never_occupies_a_plan_slot_and_gets_a_bulk_button(client):
    user = _user(weekly_touch_goal=14)
    for i in range(8):
        c = _contact(user=user, name=f"Quiet {i:02d}")
        _touch(user, c, "outreach", days_ago=40)
        _touch(user, c, "follow_up", days_ago=30)

    ctx = _cockpit_context(user)
    assert ctx["park_total"] == 8
    assert ctx["planned_total"] == 0
    assert ctx["held_total"] == 0
    assert ctx["park_bulk"] is True

    client.force_login(user)
    assert "Park all" in client.get(reverse("crm:week")).content.decode()


def test_bulk_park_goes_through_the_audited_override_per_contact(client):
    user = _user(weekly_touch_goal=14)
    made = []
    for i in range(8):
        c = _contact(user=user, name=f"Quiet {i:02d}")
        _touch(user, c, "outreach", days_ago=40)
        _touch(user, c, "follow_up", days_ago=30)
        made.append(c)

    client.force_login(user)
    resp = client.post(reverse("crm:today_park_all"))
    assert resp.status_code == 200
    for c in made:
        c.refresh_from_db()
        assert c.thread_state == "parked"
        # One audit row each: the ratchet stays the only writer, and the log
        # has no gap saying who parked these people.
        assert Touch.all_objects.filter(
            user=user, contact=c, kind="manual_override"
        ).count() == 1
    assert _cockpit_context(user)["queue_total"] == 0


def test_a_small_park_group_gets_no_bulk_button(client):
    user = _user(weekly_touch_goal=14)
    for i in range(2):
        c = _contact(user=user, name=f"Quiet {i:02d}")
        _touch(user, c, "outreach", days_ago=40)
        _touch(user, c, "follow_up", days_ago=30)
    assert _cockpit_context(user)["park_bulk"] is False
    client.force_login(user)
    assert "Park all" not in client.get(reverse("crm:week")).content.decode()


def test_bulk_park_is_tenant_scoped(client):
    a = _user("a@example.com")
    b = _user("b@example.com")
    theirs = _contact(user=b, name="Not Yours")
    _touch(b, theirs, "outreach", days_ago=40)
    _touch(b, theirs, "follow_up", days_ago=30)

    client.force_login(a)
    assert client.post(reverse("crm:today_park_all")).status_code == 200
    theirs.refresh_from_db()
    assert theirs.thread_state == "no_reply"


# ---------------------------------------------------------------------------
# A. Zero states — three of them, and they must not contradict each other.
# ---------------------------------------------------------------------------
def test_done_for_today_is_not_all_caught_up(client):
    """E9: "You're all caught up" while 27 items pace out is the page arguing
    with the line beneath it."""
    user = _user(weekly_touch_goal=14)
    for i in range(30):
        c = _contact(user=user, name=f"Cold {i:02d}")
        _touch(user, c, "outreach", days_ago=20)
        Contact.all_objects.filter(pk=c.pk).update(
            snoozed_until=timezone.now() + timedelta(days=1)
        )
    # Un-snooze nothing: the whole queue is snoozed away, so the plan is empty
    # and so is the remainder -> genuinely caught up.
    body = _login_and_get(client, user)
    assert "You're all caught up." in body


def test_an_empty_plan_with_a_queue_behind_it_says_done_for_today(client):
    user = _user(weekly_touch_goal=14)
    for i in range(8):
        # Parked-eligible contacts: they populate the queue but never the plan.
        c = _contact(user=user, name=f"Quiet {i:02d}")
        _touch(user, c, "outreach", days_ago=40)
        _touch(user, c, "follow_up", days_ago=30)

    body = _login_and_get(client, user)
    assert "Done for today." in body
    assert "You're all caught up." not in body
    # It names what's left instead of implying the database is empty.
    assert "8 contacts have gone quiet" in body


def test_no_contacts_still_says_no_contacts(client):
    user = _user(weekly_touch_goal=14)
    body = _login_and_get(client, user)
    assert "No contacts yet." in body


def _login_and_get(client, user) -> str:
    client.force_login(user)
    return client.get(reverse("crm:week")).content.decode()


# ---------------------------------------------------------------------------
# A6 / E4. The Schedule rail: a real time when one is known, and never
# otherwise. This was "Coming Up", which could only ever report when a chat
# was SET UP because no chat datetime was stored anywhere. CalendarEvent
# changed that — for chats somebody knows the time of. These pin both halves.
# ---------------------------------------------------------------------------
def test_a_scheduled_chat_with_no_event_still_shows_and_claims_no_time(client):
    user = _user(weekly_touch_goal=14)
    c = _contact(
        user=user, name="Grace Hopper", warmth="replied", thread_state="chat_scheduled",
    )
    _touch(user, c, "chat_scheduled", days_ago=1)

    ctx = _cockpit_context(user)
    assert [r["contact"].name for r in ctx["schedule"]] == ["Grace Hopper"]
    assert ctx["schedule"][0]["when"] == "no time yet"

    body = _login_and_get(client, user)
    assert "Schedule" in body
    # "chat agreed", not "chat set up" (2026-09-02, fourth copy pass). The
    # row sat beside "no time yet" and contradicted it: a chat that is set up
    # is a chat with a time. "agreed" is all `thread_state="chat_scheduled"`
    # asserts when no CalendarEvent exists for it, which is the entry
    # condition for this branch of `_schedule`.
    assert "chat agreed" in body
    assert "chat set up" not in body
    # Nobody stated a time, so the page must not imply one.
    for invented in ("chat tomorrow", "Chat tomorrow", "chat at "):
        assert invented not in body


def test_an_event_with_a_real_time_states_it(client):
    user = _user(weekly_touch_goal=14)
    c = _contact(
        user=user, name="Ada Lovelace", warmth="replied", thread_state="chat_scheduled",
    )
    _touch(user, c, "chat_scheduled", days_ago=1)
    at = timezone.localtime(timezone.now()).replace(
        hour=15, minute=0, second=0, microsecond=0) + timedelta(days=1)
    CalendarEvent.all_objects.create(
        user=user, contact=c, title="Chat with Ada Lovelace",
        starts_at=at, kind="chat", thread_id="t-1")

    ctx = _cockpit_context(user)
    assert [r["when"] for r in ctx["schedule"]] == ["3pm tmrw"]
    # And the contact is not double-listed as an untimed chat.
    assert len(ctx["schedule"]) == 1


def test_a_scheduled_chat_drops_off_the_schedule_once_it_goes_stale():
    """The exact complement of cadence branch 2: past 4 business days this
    stops being upcoming and becomes a confirm_chat action instead."""
    user = _user(weekly_touch_goal=14)
    c = _contact(
        user=user, name="Stale Chat", warmth="replied", thread_state="chat_scheduled",
    )
    _touch(user, c, "chat_scheduled", days_ago=21)

    ctx = _cockpit_context(user)
    assert ctx["schedule"] == []
    # Read off the whole page, not just the plan lanes. 21 calendar days is 15
    # business days, which is exactly where staleness decay moves a `confirm_chat`
    # out of the critical lane and into the "Still open" strip (see
    # CRITICAL_STALE_BUSINESS_DAYS and its own tests at the foot of this file).
    # That is a different rule than the one under test here: this case is about
    # branch 2 firing at all and the chat leaving the schedule, and the card
    # existing is the whole of that claim.
    actions = [a for lane in ctx["lanes"] for a in lane["items"]] + ctx["still_open"]
    assert [a["action"] for a in actions] == ["confirm_chat"]


# ---------------------------------------------------------------------------
# A / E7. The page's shape: queue above the commodity stats, no count-up.
# ---------------------------------------------------------------------------
def test_the_stats_lead_the_page_and_the_queue_follows_immediately(client):
    """The stats sat at the foot for a while, because a hero plus four stat
    cards had spent the whole fold on the commodity layer. They lead again
    now that they are one hairline strip instead — but nothing may grow
    between them and the queue, which is the rule that stretch actually
    bought."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ethan Gao")
    _touch(user, c, "outreach", days_ago=20)

    body = _login_and_get(client, user)
    # `id="today-cockpit"`, not the bare id — the page inlines its stylesheet,
    # which names #today-cockpit in a rule long before the markup appears.
    ribbon, cockpit = body.index('class="ribbon"'), body.index('id="today-cockpit"')
    assert ribbon < cockpit
    between = body[body.index("</section>", ribbon):cockpit]
    assert "<section" not in between and "<article" not in between


def test_the_today_stats_are_not_count_animated(client):
    """base.html count-animates every `.dash-num` from 0 on first paint, and
    the intermediate frames are wrong numbers that screenshot as data."""
    user = _user(weekly_touch_goal=14)
    body = _login_and_get(client, user)
    # Matched as a class ATTRIBUTE, not a substring: `_styles.html` names
    # `.dash-num` in a comment explaining precisely why it isn't used here.
    assert 'class="dash-num"' not in body
    assert 'class="ribbon-num"' in body


# ---------------------------------------------------------------------------
# The closing-soon cell says what kind of evidence it is counting.
#
# `Opportunity.confidence` is the one provenance carrier: 1.0 is a deadline a
# provider published as a structured field, anything under it is Coverage's
# own regex reading the posting's prose. Measured on the live board, 96% of
# dated open campus roles are the second kind — so a bare urgent number on the
# busiest page was presenting our reading as the market's calendar, the exact
# claim `views.deadline_provenance`, the .ics SUMMARY, the feed card, the
# drawer and both My Applications lenses each refuse to make.
# ---------------------------------------------------------------------------
def _campus_role(n, *, days, confidence):
    from directory.models import Opportunity

    firm = Firm.objects.get_or_create(
        slug=f"close-{n}", defaults={"name": f"Close Co {n}"})[0]
    return Opportunity.objects.create(
        firm=firm, url=f"https://example.test/close/{n}", title=f"SA {n}",
        bucket="internship", status="open",
        deadline=timezone.localdate() + timedelta(days=days),
        confidence=confidence,
    )


def _ribbon(client, user) -> str:
    """Just the stat strip. The page inlines its whole stylesheet, whose
    comments use the word "reported" in an unrelated sense, so a
    "not in body" assertion about the marker has to be scoped to the markup
    that carries it."""
    body = _login_and_get(client, user)
    start = body.index('class="ribbon"')
    return body[start:body.index("</section>", start)]


def test_the_closing_cell_names_how_many_of_its_dates_are_our_own_reading(client):
    """Same word the rest of the product uses for this fact, on the count
    itself rather than in a caveat sentence a stat cell has no room for."""
    _campus_role(1, days=1, confidence=0.6)   # prose-read
    _campus_role(2, days=2, confidence=0.6)   # prose-read
    _campus_role(3, days=3, confidence=1.0)   # the board published a field

    ribbon = _ribbon(client, _user(weekly_touch_goal=14))

    assert "Closing in 10 days, 2 reported" in ribbon
    # The urgent figure itself is unchanged: this qualifies the count, it does
    # not shrink it.
    assert '<span class="ribbon-num">3</span>' in ribbon


def test_the_closing_cell_carries_the_provenance_sentence_on_hover(client):
    """The page HAS hover, unlike the digest email, so the full provenance
    rides in a `title` instead of eating the label."""
    _campus_role(1, days=1, confidence=0.6)

    ribbon = _ribbon(client, _user(weekly_touch_goal=14))

    assert ("1 of these dates were read from the posting's own text, not a "
            "field the board published") in ribbon


def test_a_closing_cell_of_published_dates_claims_no_reading_of_its_own(client):
    """Zero reported is a real state and gets no marker at all — the label
    goes back to the bare count rather than saying "0 reported"."""
    _campus_role(1, days=1, confidence=1.0)
    _campus_role(2, days=2, confidence=1.0)

    ribbon = _ribbon(client, _user(weekly_touch_goal=14))

    assert "Closing in 10 days<" in ribbon
    assert "reported" not in ribbon


def test_an_empty_funnel_says_so_in_words_rather_than_drawing_zeroes(client):
    user = _user(weekly_touch_goal=14)
    body = _login_and_get(client, user)
    assert "Nothing submitted yet." in body
    assert "0 › 0 › 0" not in body


# ---------------------------------------------------------------------------
# The funnel names its stages the way the rest of the product does.
#
# Live, /app/ read "2 › 0 › 0" under "Submitted › Interview › Offer" while
# /opportunities/mine/ showed "2 Applied · 0 Interviewing" for the identical
# rows — same field, same value, same count, two vocabularies. The ribbon's
# label was a hardcoded literal spelling the raw `applied_status` keys.
# ---------------------------------------------------------------------------
def _tracked(user, status):
    from analytics.models import UserOpportunity
    from directory.models import Opportunity

    firm = Firm.objects.get_or_create(slug="ec", defaults={"name": "Evercore"})[0]
    opp = Opportunity.objects.create(
        firm=firm, url=f"https://example.test/{status}", title=f"SA {status}",
        bucket="internship", status="open",
    )
    UserOpportunity.all_objects.create(
        user=user, opportunity=opp, applied_status=status)


def test_the_funnel_label_uses_the_products_stage_names(client):
    """A populated funnel, so the label actually renders."""
    user = _user(weekly_touch_goal=14)
    _tracked(user, "submitted")
    body = _login_and_get(client, user)
    assert "Applied › Interviewing › Offer" in body
    assert "Submitted › Interview › Offer" not in body


def test_the_funnel_label_is_read_from_the_one_stage_vocabulary(client):
    """Not merely re-spelled: the label is BUILT from `_STAGE_LABELS`, the
    same map My Applications' stage tiles and the feed's track pill read, so
    renaming a stage there renames it here and the two cannot drift apart."""
    from directory.views import _FUNNEL_STATES, _STAGE_LABELS

    user = _user(weekly_touch_goal=14)
    _tracked(user, "submitted")
    expected = " › ".join(_STAGE_LABELS[s] for s in _FUNNEL_STATES)

    ctx = _dashboard_context(user)
    assert ctx["dash"]["funnel_label"] == expected
    assert expected in _login_and_get(client, user)


def test_the_funnel_counts_still_match_the_stage_the_label_names(client):
    """The wording changed; the arithmetic must not. Two submitted rows have
    to reach the ribbon as 2, the number My Applications' Applied tile shows
    for the same user."""
    user = _user(weekly_touch_goal=14)
    _tracked(user, "submitted")
    _tracked(user, "interview")
    ctx = _dashboard_context(user)
    assert ctx["dash"]["funnel"] == {"submitted": 1, "interview": 1, "offer": 0}
    assert "1 › 1 › 0" in _login_and_get(client, user)


# ---------------------------------------------------------------------------
# The renovation: deadlines by name, the silent bucket, and chat prep.
# ---------------------------------------------------------------------------
def test_deadlines_are_named_not_just_counted(client):
    """The ribbon counts these. A count creates a click; a name creates an
    action you can take this morning.

    Until 2026-08-31 the NAME landed on a "Your board" card instead of the
    rail, and the rail carried only the overflow the board hadn't already
    shown. The board was removed (not practically useful, per the founder);
    the rail is back to naming every confirmed date on its own.

    The countdown reads "in 2d" rather than "2d" as of 2026-09-02, and the
    reason is the row directly under it: `open_run` prints an ELAPSED day
    count, and two bare "Nd" figures an inch apart pointing in opposite
    directions of time was the defect the founder read off this card.
    Rewritten and not weakened — the assertion still pins the same two days
    and the same urgency, which is what "named, not just counted" is about.
    """
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    today = timezone.localdate()
    FirmDate.objects.create(firm=firm, cycle="sa2028", region="us",
                            event_kind="insight_deadline",
                            date=today + timedelta(days=2), confidence=1.0)

    ctx = _cockpit_context(user)
    assert [(d["firm"].name, d["when"], d["urgent"]) for d in ctx["deadlines"]] == [
        ("Morgan Stanley", "in 2d", True)]

    body = _login_and_get(client, user)
    # "Insight programme deadline", not the old "Insight deadline" — see
    # `crm.utils.FIRM_DATE_LABELS`, which now derives from (and can no
    # longer drift from) `directory.timeline.EVENT_LABELS`.
    assert "Insight programme deadline" in body


def test_the_rail_names_every_confirmed_date_a_firm_has(client):
    """Until 2026-08-31 a firm's SECOND confirmed date had nowhere to go: a
    "Your board" lane showed one card per firm, so the rail carried only the
    date the board had folded away. With the board removed, the rail names
    both — dropping either one to tidy the list would be deleting a
    confirmed deadline."""
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="gs2", name="Goldman Sachs")
    today = timezone.localdate()
    FirmDate.objects.create(firm=firm, cycle="sa2028", region="us",
                            event_kind="insight_open",
                            date=today + timedelta(days=2), confidence=1.0)
    FirmDate.objects.create(firm=firm, cycle="sa2028", region="us",
                            event_kind="app_close",
                            date=today + timedelta(days=25), confidence=1.0)

    ctx = _cockpit_context(user)
    assert [d["event_kind"] for d in ctx["deadlines"]] == [
        "insight_open", "app_close"]


# ---------------------------------------------------------------------------
# The rail's own order, 2026-08-31. The founder's own words: "outreach this
# week goes on top, then deadlines, then unplaced." Before this the rail read
# Unplaced, Pace, Schedule, Deadlines — a standing question above the pace
# ring and the confirmed dates it should trail.
# ---------------------------------------------------------------------------
def test_the_rail_orders_pace_before_deadlines_and_unplaced_rides_with_pace(client):
    """REWRITTEN 2026-09-02; the third rung of its premise no longer exists.

    It read `..._orders_pace_before_deadlines_before_unplaced` and pinned
    three rail cards in the founder's own 2026-08-31 order. On 2026-09-02 he
    asked for the first and the third to become one ("Combine this with
    unsorted contacts, make into one widget"), so "then unplaced" is no
    longer an order this rail can express: the unplaced block is inside the
    pace card, which means it necessarily renders ABOVE Deadlines rather
    than below it.

    Both halves of what the old test was protecting survive and are pinned
    here. Pace still leads (`pace < deadlines`), and the unplaced block is
    still positioned rather than loose — it now has to sit inside the pace
    card's own element, which is a stricter claim than "somewhere after
    Deadlines" and is what would actually break if a future pass pulled it
    back out into a card of its own without saying so.

    Getting Started is not part of this claim — it is gated to an unfinished
    setup and, by its own rule, outranks everything whenever it renders at
    all — so this fixture leaves it unfinished-but-absent (no
    Firm/UserFirm/Gmail link needed to make that true for a fresh test user).
    """
    user = _user(weekly_touch_goal=14, regions=["hk", "us"])
    firm = Firm.objects.create(slug="gs4", name="Goldman Sachs",
                                regions=["hk", "us"])
    FirmDate.objects.create(firm=firm, cycle="sa2028", region="us",
                            event_kind="app_close",
                            date=timezone.localdate() + timedelta(days=5),
                            confidence=1.0)
    _contact(user=user, name="Jude Yoon", firm=firm, source="capture")

    ctx = _cockpit_context(user)
    assert ctx["deadlines"], "precondition: a deadline exists"
    # `unplaced_arrival_count` since 2026-09-02: the card counts the week's
    # arrivals instead of naming up to five of them. The precondition is the
    # same one — something unplaced arrived — only the key and its shape
    # changed.
    assert ctx["unplaced_arrival_count"], "precondition: an unplaced arrival exists"

    body = _login_and_get(client, user)
    # No closing quote: the rail card also wears the shared panel primitive
    # since 2026-09-02 (D-13). The card's own name still identifies it.
    pace = body.index('class="rail-card pace-card')
    deadlines = body.index('<h3 class="rail-title">Deadlines')
    assert pace < deadlines, (
        f"expected pace ({pace}) before deadlines ({deadlines}) in rendered "
        "order"
    )
    # And the merged half is INSIDE the pace card, not a card of its own.
    # `unplaced-card` with no `rail-card` in front of it is the whole
    # difference, so the assertion is on both facts at once.
    unplaced = body.index('class="unplaced-card')
    assert 'class="rail-card unplaced-card' not in body, (
        "the unplaced block is back to being its own rail card; the founder "
        "asked for one widget"
    )
    assert pace < unplaced < deadlines, (
        f"expected the unplaced block ({unplaced}) between the pace card's "
        f"opening tag ({pace}) and Deadlines ({deadlines}), i.e. inside the "
        "pace card"
    )


def test_an_unconfirmed_date_never_reaches_the_rail():
    """Same bar the cadence engine acts on. A countdown built on a rumour is
    worse than no countdown."""
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    FirmDate.objects.create(firm=firm, cycle="sa2028", region="hk",
                            event_kind="app_close",
                            date=timezone.localdate() + timedelta(days=3),
                            confidence=0.3)
    assert _cockpit_context(user)["deadlines"] == []


def test_a_past_deadline_is_not_upcoming():
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    FirmDate.objects.create(firm=firm, cycle="sa2028", region="us",
                            event_kind="app_close",
                            date=timezone.localdate() - timedelta(days=1),
                            confidence=1.0)
    assert _cockpit_context(user)["deadlines"] == []


def test_an_estimated_date_never_reaches_the_rail_however_confident():
    """`_next_deadlines` spelled its bar as `confidence=1.0` alone, while
    `directory.views._firm_date_row` — the page that renders these same rows
    with their provenance attached — has always required BOTH halves:
    `confidence >= 0.8 AND precision in ("day", "month", "")`.

    The two halves say different things. `confidence` is how sure we are the
    firm holds this date; `precision` is how exactly the stored day locates
    it. `precision="estimated"` means a month-level guess, printed on the firm
    timeline as "~ Nov 2026" — and printed by the rail as a hard "5d"
    countdown. `import_firm_dates` reads the two from independent keys of one
    YAML entry, so a single seed line saying `confidence: confirmed_official`
    / `precision: estimated` produces exactly this row.

    (Moved here from test_plays.py, 2026-08-31, when that module's own
    subject — the coverage-gap lane — was retired; this pins the Deadlines
    rail's own confidence bar, which survives the lane it was originally
    fixtured alongside.)
    """
    from crm.today import _next_deadlines

    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    today = timezone.localdate()
    guess = FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="us", event_kind="app_close",
        date=today + timedelta(days=5), confidence=1.0,
    )
    FirmDate.objects.filter(pk=guess.pk).update(precision="estimated")

    assert _next_deadlines(user, today) == []


def test_a_month_precision_date_still_reaches_the_rail():
    """The over-reach guard. "month" is confirmed — the firm timeline calls it
    confirmed too — and dropping it would silently delete a real date from
    the rail to fix a different one."""
    from crm.today import _next_deadlines

    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    today = timezone.localdate()
    real = FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="us", event_kind="insight_deadline",
        date=today + timedelta(days=5), confidence=1.0,
    )
    FirmDate.objects.filter(pk=real.pk).update(precision="month")

    assert len(_next_deadlines(user, today)) == 1


def test_a_chat_today_gets_a_prep_card_with_what_you_learned_last_time(client):
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    c = _contact(user=user, name="Ada Lovelace", firm=firm,
                                   warmth="chatted", thread_state="chat_done")
    chat = _touch(user, c, "chat", days_ago=30)
    ChatDebrief.all_objects.create(
        user=user, contact=c, touch=chat,
        learned="She runs the TMT desk and offered to introduce me to Ben.")
    at = timezone.localtime(timezone.now()).replace(
        hour=15, minute=0, second=0, microsecond=0)
    CalendarEvent.all_objects.create(
        user=user, contact=c, title="Chat with Ada Lovelace",
        starts_at=at, kind="chat", thread_id="t-prep")
    FirmDate.objects.create(firm=firm, cycle="sa2028", region="us",
                            event_kind="app_close",
                            date=timezone.localdate() + timedelta(days=9),
                            confidence=1.0)

    ctx = _cockpit_context(user)
    assert len(ctx["chat_prep"]) == 1
    prep = ctx["chat_prep"][0]
    assert prep["contact"].name == "Ada Lovelace"
    assert "TMT desk" in prep["learned"]
    assert prep["firm_date_days"] == 9
    assert prep["firm_date_label"] == "Applications close"

    body = _login_and_get(client, user)
    assert "Chat today" in body
    assert "TMT desk" in body


def test_prep_only_covers_today_and_only_timed_chats():
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Tomorrow Person")
    tomorrow = timezone.localtime(timezone.now()).replace(
        hour=15, minute=0, second=0, microsecond=0) + timedelta(days=1)
    CalendarEvent.all_objects.create(
        user=user, contact=c, title="Chat with Tomorrow Person",
        starts_at=tomorrow, kind="chat", thread_id="t-1")
    allday = _contact(user=user, name="Allday Person")
    CalendarEvent.all_objects.create(
        user=user, contact=allday, title="Superday",
        starts_at=timezone.localtime(timezone.now()).replace(hour=0, minute=0),
        all_day=True, kind="event", thread_id="t-2")

    ctx = _cockpit_context(user)
    assert ctx["chat_prep"] == [], "prep is for a conversation happening today"
    assert len(ctx["schedule"]) == 2, "both still appear on the schedule"


def test_a_dismissed_debrief_is_not_used_as_prep_material():
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ada Lovelace")
    chat = _touch(user, c, "chat", days_ago=30)
    ChatDebrief.all_objects.create(user=user, contact=c, touch=chat,
                                   learned="Should not surface.", dismissed=True)
    CalendarEvent.all_objects.create(
        user=user, contact=c, title="Chat with Ada Lovelace",
        starts_at=timezone.localtime(timezone.now()).replace(hour=15, minute=0),
        kind="chat", thread_id="t-3")

    assert _cockpit_context(user)["chat_prep"][0]["learned"] == ""


def test_the_page_header_outranks_page_content_in_paint_order(client):
    """Quick add opens a menu out of the header, and the header sits above a
    sticky rail card that comes later in the document.

    `.pagehead-actions` carries a `rise-in` animation with fill `both`, which
    leaves a real transform on the element permanently — and a transformed
    element is a stacking context, so any z-index set on the menu inside it is
    sealed at the parent's level. The menu rendered behind the rail card until
    `.pagehead` itself was raised. Asserted on the stylesheet because paint
    order is not observable from a Django test client.
    """
    user = _user(weekly_touch_goal=14)
    client.force_login(user)
    css = (settings.BASE_DIR / "static" / "css" / "coverage.css").read_text()
    head = css.split(".pagehead {", 1)[1].split("}", 1)[0]
    assert "z-index" in head, (
        "The page header must declare its own stacking level; without it a "
        "menu opened from the header paints behind page content."
    )

    body = client.get(reverse("crm:week")).content.decode()
    assert "quickadd-menu" in body
    assert "Quick add" in body


def test_the_day_track_places_today_on_an_eight_to_eight_axis(client):
    """Shape, not just contents: a list says what is on today, the track says
    whether it is stacked into one morning or spread across the day."""
    user = _user(weekly_touch_goal=14)
    for hour, minute, name in [(8, 0, "Dawn"), (14, 0, "Midday"), (20, 0, "Dusk")]:
        c = _contact(user=user, name=f"{name} Person")
        CalendarEvent.all_objects.create(
            user=user, contact=c, title=f"Chat with {name}",
            starts_at=timezone.localtime(timezone.now()).replace(
                hour=hour, minute=minute, second=0, microsecond=0),
            kind="chat", thread_id=f"t-{name}")

    bar = _cockpit_context(user)["daybar"]
    assert bar["show"] is True
    assert [d["pct"] for d in bar["dots"]] == [0.0, 50.0, 100.0]


def test_times_outside_the_window_clamp_instead_of_vanishing():
    """A 7am call is genuinely "first thing". Dropping it to keep the axis
    tidy would lose an event to protect a scale."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Early Bird")
    CalendarEvent.all_objects.create(
        user=user, contact=c, title="Chat with Early Bird",
        starts_at=timezone.localtime(timezone.now()).replace(
            hour=6, minute=0, second=0, microsecond=0),
        kind="chat", thread_id="t-early")

    bar = _cockpit_context(user)["daybar"]
    assert [d["pct"] for d in bar["dots"]] == [0.0]
    assert len(bar["dots"]) == 1, "clamped, not dropped"


def test_the_track_stays_away_when_nothing_is_timed_today():
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="All Day")
    CalendarEvent.all_objects.create(
        user=user, contact=c, title="Superday", all_day=True,
        starts_at=timezone.localtime(timezone.now()).replace(hour=0, minute=0),
        kind="event", thread_id="t-allday")

    bar = _cockpit_context(user)["daybar"]
    assert bar["show"] is False
    assert bar["dots"] == []


def test_beyond_a_week_the_schedule_names_the_date_not_the_weekday():
    """With 14 days in view there are two Fridays; "Fri" on the second one
    reads as the first. Past a week the date is the only honest label."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Far Out")
    at = timezone.localtime(timezone.now()).replace(
        hour=15, minute=0, second=0, microsecond=0) + timedelta(days=10)
    CalendarEvent.all_objects.create(
        user=user, contact=c, title="Chat with Far Out",
        starts_at=at, kind="chat", thread_id="t-far")

    row = _cockpit_context(user)["schedule"][0]
    assert row["when"] == f"{at.strftime('%b')} {at.day}"
    assert at.strftime("%a") not in row["when"], "no ambiguous weekday"


def test_a_seventh_event_today_still_gets_its_dot_and_its_prep():
    """The schedule rail shows six rows, but the day track and chat prep read
    the FULL list — capping at the source lost the seventh event's dot and
    prep card while the rail looked complete."""
    user = _user(weekly_touch_goal=14)
    for i in range(7):
        c = _contact(user=user, name=f"Busy {i}")
        CalendarEvent.all_objects.create(
            user=user, contact=c, title=f"Chat with Busy {i}",
            starts_at=timezone.localtime(timezone.now()).replace(
                hour=9 + i, minute=0, second=0, microsecond=0),
            kind="chat", thread_id=f"t-busy-{i}")

    ctx = _cockpit_context(user)
    assert len(ctx["schedule"]) == 6, "the rail stays capped"
    assert len(ctx["daybar"]["dots"]) == 7, "the track shows the whole day"
    assert len(ctx["chat_prep"]) == 7, "every chat gets its prep"


def test_the_picks_cell_never_congratulates_an_unchecked_user(client):
    """Zero unsaved means two different things: "you saved them all" and
    "nothing was picked for you, so there was nothing to save". The day-zero
    walkthrough caught the ribbon telling a ten-minute-old account it was
    "all caught up on your year" when the check had never run. Three states:
    a count, a genuine all-clear, and an honest pointer to Settings.

    REWRITTEN 2026-09-02 for the surface, not the rule. The cell reads the
    Picked column's unsaved count now rather than the retired bulk-save
    banner's year-gated one, so the state that earns the all-clear is "picks
    exist and are all saved" rather than "you stated a year and nothing
    names it". The failure mode it guards is identical."""
    from directory.models import Firm, Opportunity

    user = _user(weekly_touch_goal=10)
    client.force_login(user)

    # Nothing on the board, so nothing can be picked: the check never ran.
    body = client.get(reverse("crm:week")).content.decode()
    assert "Fill in Settings to get picks" in body
    assert "Every pick saved" not in body

    # A role this student is picked for, and then saved: the all-clear is
    # EARNED, because there was something to be caught up on.
    user.class_year = 2029
    user.save(update_fields=["class_year"])
    firm = Firm.objects.create(slug="picks-firm", name="Picks Bank")
    Opportunity.objects.create(
        firm=firm, url="https://picks/1", title="Summer Analyst",
        bucket="internship", status="open", class_year="2029")
    body = client.get(reverse("crm:week")).content.decode()
    assert "Picked for you, not saved yet" in body

    client.get(reverse("opportunities"))
    client.post(reverse("track_eligible"), {"confirmed": "1"})
    body = client.get(reverse("crm:week")).content.decode()
    assert "Every pick saved" in body
    assert "Fill in Settings to get picks" not in body


# ---------------------------------------------------------------------------
# One event, one unit. The thank-you prompt is the only string on this page
# measured in hours, and the window that justified the hours is stripped
# before it renders.
# ---------------------------------------------------------------------------
def test_the_thank_you_prompt_speaks_days_once_hours_stop_helping(client):
    """The measured bug: one chat (Ellen Chung, the contact's only touch)
    rendered three ways in one scroll of /app/ — "Chatted 2d ago" on the
    Debrief card, "Chat done 58h ago" on the Don't-lose-these card, and
    "2 business days ago" on that same card's ledger row. The engine formats
    the thank-you branch in hours because its window IS hours, but
    `_sentenceize` strips "(within 24h)"/"(OVERDUE)", so the anchor never
    reaches the screen and a bare hour count sits between two day counts."""
    user = _user(weekly_touch_goal=14)
    c = _contact(
        user=user, name="Ellen Chung", warmth="chatted", thread_state="chat_done"
    )
    Touch.all_objects.create(
        user=user, contact=c, kind="chat", channel="email",
        ts=timezone.now() - timedelta(hours=57),
    )
    client.force_login(user)

    body = client.get(reverse("crm:week")).content.decode()
    assert "Chat done" in body, "the thank-you prompt is on the page at all"
    assert "h ago" not in body, "no hour count survives on a day-based surface"
    assert "d ago. Send thank-you." in body


def test_a_fresh_chat_keeps_its_hours():
    """Hours are not banned, they are earned. Inside the 24h thank-you window
    an hour count is the honest unit and rounding it to "0d ago" would be
    worse than the bug."""
    from crm.today import _age_in_days

    now = timezone.now()
    assert _age_in_days("Chat done 6h ago. Send thank-you.", 6.0, now=now) == (
        "Chat done 6h ago. Send thank-you."
    )


def test_a_reping_reason_speaks_the_date_the_chip_already_speaks():
    """WATCHED LIVE (audit 2026-08-23): one card said "Closes Aug 30" in its
    chip and "app closes 2026-08-30" in its sentence — the engine's
    `close.isoformat()` reaching the screen raw. `_prose_dates` rewrites it
    at the same presentation layer `_age_in_days` uses; the year survives
    only when it is not this year."""
    from datetime import date

    from crm.today import _prose_dates

    today = date(2026, 8, 23)
    assert _prose_dates(
        "Nomura app closes 2026-08-30. Re-ping before you submit.", today=today
    ) == "Nomura app closes Aug 30. Re-ping before you submit."
    assert _prose_dates(
        "Nomura app closes 2027-01-15. Re-ping before you submit.", today=today
    ) == "Nomura app closes Jan 15, 2027. Re-ping before you submit."
    # Not a real calendar date -> left alone rather than mangled.
    assert _prose_dates("code 2026-99-99 stays", today=today) == "code 2026-99-99 stays"
    assert _prose_dates("", today=today) == ""


def test_the_day_count_comes_from_the_calendar_not_from_dividing_by_24():
    """"2d ago" here has to mean the same thing it means on the Debrief card
    two cards up, which counts calendar days. 57.6h / 24 rounds to 2 by luck;
    63h rounds to 3 while the chat is still two calendar days back, and the
    two cards would disagree again in a smaller way."""
    from crm.today import _age_in_days

    now = timezone.localtime(timezone.now()).replace(hour=18, minute=0, second=0, microsecond=0)
    # 63 hours back from 18:00 is 03:00 two calendar days earlier.
    out = _age_in_days("Chat done 63h ago. Send thank-you.", 63.0, now=now)
    assert out == "Chat done 2d ago. Send thank-you.", "round(63/24) would say 3"


def test_debrief_and_today_agree_on_days_ago_for_the_same_chat():
    """The round-9 recheck's live finding, pinned: Ellen Chung, Touch 558,
    one chat, ~58.46h elapsed under Asia/Hong_Kong — "Chatted 2d ago" on the
    Debrief card (`crm.debrief.pending`) vs "Chat done 3d ago" on the
    Don't-lose-these card (`crm.today._age_in_days`). The two disagreed
    because `debrief.pending()`'s `days_ago` was a raw timedelta floor
    (`(as_of - t.ts).days`, timezone-independent: 58h floors to 2 no matter
    where midnight falls) while `_age_in_days` already computed a calendar-
    date difference in the account's active timezone (58h back from just
    after HK midnight lands 3 local calendar days earlier). Same fact, two
    formulas, two answers. `debrief.pending()` now uses the same
    `timezone.localtime(...).date()` convention, so both read this single
    chat identically.

    58h is anchored at HK 00:30 rather than reusing 57h/63h from the tests
    above so the elapsed *duration* floors to 2 (`timedelta(hours=58).days
    == 2`) while the *calendar* difference is 3 — the exact shape that made
    the two formulas disagree.
    """
    from zoneinfo import ZoneInfo

    from crm import debrief as debrief_svc
    from crm.today import _age_in_days

    hk = ZoneInfo("Asia/Hong_Kong")
    user = _user(email="hk-ellen@example.com", weekly_touch_goal=14, timezone="Asia/Hong_Kong")
    contact = _contact(
        user=user, name="Ellen Chung", warmth="chatted", thread_state="chat_done",
    )

    as_of = timezone.now().astimezone(hk).replace(
        hour=0, minute=30, second=0, microsecond=0
    )
    touch = Touch.all_objects.create(
        user=user, contact=contact, kind="chat", channel="email",
        ts=as_of - timedelta(hours=58),
    )

    # Mirrors what TimezoneMiddleware does for a real request carrying this
    # account's timezone.
    timezone.activate(hk)
    try:
        pending = debrief_svc.pending(user, as_of=as_of)
        assert [p["touch"].id for p in pending] == [touch.id]
        debrief_days = pending[0]["days_ago"]

        today_reason = _age_in_days(
            "Chat done 58h ago. Send thank-you.", 58.0, now=as_of,
        )
    finally:
        timezone.deactivate()

    assert debrief_days == 3, "sanity: the calendar-date diff under HK is 3, not floor(58/24)=2"
    assert today_reason == f"Chat done {debrief_days}d ago. Send thank-you."


# ---------------------------------------------------------------------------
# "New at your firms" (crm.today._new_at_your_firms) was retired whole
# 2026-08-31: it duplicated the situation strip
# (assistant.situation.build_situation) without that strip's track/region/
# level/eligibility filtering, and measured on the founder's real account
# the two surfaced the identical firms. Every invariant pinned by the five
# tests that used to live here has a live successor in
# assistant/tests/test_situation.py, which pins the identical fold/cap/
# market/rung/track-mismatch behaviour for the surface that replaced it:
#
#   test_new_at_firms_folds_duplicates_but_not_two_real_distinct_postings
#     -> test_a_boards_debut_week_does_not_flood_the_new_role_event (fold)
#   test_new_at_firms_widget_caps_at_one_role_per_firm
#     -> test_a_boards_debut_week_does_not_flood_the_new_role_event (one per firm)
#   test_new_at_firms_drops_the_wrong_market_and_the_wrong_rung
#     -> test_new_role_drops_the_wrong_market_and_the_wrong_rung
#   test_new_at_firms_never_calls_a_silent_title_news
#     -> assistant/tests/test_situation.py's board-debut coverage (same fix)
#   test_new_at_firms_says_so_when_nothing_relevant_moved
#     -> situation.py's own track allowlist, `role_matches_tracks`
#
# Nothing about "don't call the wrong market, the wrong rung, a debut week,
# or an untracked role news" went untested; it is tested against the surface
# that is actually still on the page.
# ---------------------------------------------------------------------------


def test_the_daily_brief_renders_on_the_full_page(client, monkeypatch, settings):
    """The brief still reaches the page — but via the htmx endpoint, not
    inline, so the model's latency is never in front of the page. The first
    load asks for it; the endpoint renders it; every load after that has it
    inline from the cached row and stops asking."""
    import assistant.brief

    settings.ANTHROPIC_API_KEY = "sk-test-key"   # otherwise the feature is dark
    user = _user("brief-lazy@example.com", weekly_touch_goal=14)
    c = _contact(user=user, name="Ada Lovelace")
    _touch(user, c, "outreach", days_ago=20)

    monkeypatch.setattr(
        assistant.brief, "get_or_build", lambda u, actions, **kw: "Reach out to Ada today, her deadline is close."
    )
    client.force_login(user)

    # 1. Nothing cached: the page carries the lazy node, and does NOT block.
    first = client.get(reverse("crm:week")).content.decode()
    assert reverse("crm:daily_brief") in first
    assert "Reach out to Ada today, her deadline is close." not in first

    # 2. The endpoint produces the card.
    card = client.post(reverse("crm:daily_brief")).content.decode()
    assert "Reach out to Ada today, her deadline is close." in card


def test_a_cached_brief_renders_inline_and_the_page_stops_asking(client, settings):
    """Once the row exists the page must render it directly — no second
    request, and no model call — for the rest of the day.

    The row names the contact the queue is asking about. It used to name
    nobody, which is now a real signal rather than a fixture detail: a brief
    with no `contact_ids` was written from an EMPTY queue (the quiet-day
    line, or a situation-only sentence), so finding one in front of a queue
    that has work in it means the sentence has been overtaken and the page
    is right to go and ask for a better one. See
    `assistant.brief._is_stale` and
    `assistant/tests/test_brief.py::test_a_quiet_day_line_is_replaced_once
    _the_first_contact_lands`."""
    from assistant.models import DailyBrief

    settings.ANTHROPIC_API_KEY = "sk-test-key"
    user = _user("brief-cached@example.com", weekly_touch_goal=14)
    c = _contact(user=user, name="Ada Lovelace")
    _touch(user, c, "outreach", days_ago=20)
    DailyBrief.all_objects.create(
        user=user, date=timezone.localdate(), text="Already written today.",
        contact_ids=[c.id],
    )

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Already written today." in body
    assert reverse("crm:daily_brief") not in body


def test_the_page_does_not_ask_for_a_brief_when_the_feature_is_dark(client, settings):
    """No API key means no brief will ever come back, so the page must not
    draw a placeholder that would spin forever."""
    settings.ANTHROPIC_API_KEY = ""
    user = _user("brief-dark@example.com", weekly_touch_goal=14)
    c = _contact(user=user, name="Ada Lovelace")
    _touch(user, c, "outreach", days_ago=20)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert reverse("crm:daily_brief") not in body


def test_the_today_page_never_waits_on_the_model(client, monkeypatch, settings):
    """The regression this split exists to prevent. Generating the brief
    inline put the model's latency on the page 1:1 (measured: 55.7ms cached
    vs 2079.9ms on a 2.0s reply, and up to the 45s client timeout). The page
    must render without the model being reachable at all."""
    import assistant.brief

    settings.ANTHROPIC_API_KEY = "sk-test-key"
    user = _user("brief-nowait@example.com", weekly_touch_goal=14)
    c = _contact(user=user, name="Ada Lovelace")
    _touch(user, c, "outreach", days_ago=20)

    def explode(*a, **kw):
        raise AssertionError("the Today page called the model synchronously")

    monkeypatch.setattr(assistant.brief, "get_or_build", explode)
    monkeypatch.setattr(assistant.brief, "get_client", explode)

    client.force_login(user)
    assert client.get(reverse("crm:week")).status_code == 200


def test_no_brief_card_when_there_is_nothing_to_say(client, monkeypatch):
    import assistant.brief

    user = _user(weekly_touch_goal=14)
    monkeypatch.setattr(assistant.brief, "get_or_build", lambda u, actions, **kw: None)

    body = _login_and_get(client, user)
    assert '<p class="daily-brief-text">' not in body


def test_the_brief_receives_the_full_uncapped_queue_not_just_the_planned_slice(client, monkeypatch):
    """_cockpit_context caps `planned` to the daily cap, but the brief should
    see every action in the queue so it can pick the single most urgent one
    even when that person didn't make today's capped plan."""
    import assistant.brief

    user = _user(weekly_touch_goal=14)
    for i in range(30):
        c = _contact(user=user, name=f"Cold {i:02d}")
        _touch(user, c, "outreach", days_ago=20)

    seen = {}

    def fake_get_or_build(u, actions, **kw):
        seen["count"] = len(actions)
        return "stub brief"

    monkeypatch.setattr(assistant.brief, "get_or_build", fake_get_or_build)
    client.force_login(user)
    ctx = client.get(reverse("crm:week")).context
    # The brief is generated by the htmx endpoint now, not by the page, so
    # the queue it sees is rebuilt there — the invariant this test pins (the
    # FULL queue, not the capped plan) has to hold on that path instead.
    client.post(reverse("crm:daily_brief"))

    assert seen["count"] == ctx["queue_total"] == 30


def test_the_htmx_partial_refresh_never_calls_the_brief(client, monkeypatch):
    """today_park_all/today_act share _cockpit_context with week(), but only
    week() (the full page load) may spend a real model call on a brief —
    firing one as a side effect of parking a contact would be a surprise
    bill for an unrelated click."""
    import assistant.brief

    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ada Lovelace")
    _touch(user, c, "outreach", days_ago=20)

    def fail_if_called(*a, **kw):
        raise AssertionError("the partial refresh must never generate a brief")

    monkeypatch.setattr(assistant.brief, "get_or_build", fail_if_called)
    client.force_login(user)
    resp = client.post(reverse("crm:today_park_all"))
    assert resp.status_code == 200


def test_the_brief_never_leaks_into_the_partial_cockpit_template(client, monkeypatch):
    """Even if a brief happens to exist for today, the htmx partial must not
    render it — that card belongs to the full page only."""
    from assistant.models import DailyBrief

    user = _user(weekly_touch_goal=14)
    DailyBrief(user=user, date=timezone.localdate(), text="Should not appear here.").save()
    client.force_login(user)
    resp = client.post(reverse("crm:today_park_all"))
    assert "Should not appear here." not in resp.content.decode()


# ---------------------------------------------------------------------------
# The situation snapshot: deterministic cards under the daily brief,
# assistant.situation.build_situation. Same full-page-only invariant as the
# brief itself — see the guard test below.
# ---------------------------------------------------------------------------
def test_a_moved_deadline_on_a_tracked_role_renders_a_card(client):
    from analytics.models import UserOpportunity
    from directory.models import Opportunity, OpportunityChange

    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="north-bank", name="North Bank")
    opp = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        deadline=timezone.localdate() + timedelta(days=20),
        url="https://north.example/jobs/1",
    )
    UserOpportunity(user=user, opportunity=opp).save()
    OpportunityChange.objects.create(
        opportunity=opp, field="deadline", old_value="2026-08-01",
        new_value="2026-08-20", stage="reverify", observed_at=timezone.now(),
    )

    body = _login_and_get(client, user)

    assert "Summer Analyst" in body
    assert "North Bank" in body
    # Standardized 2026-08-31: every situation-card sentence leads with the
    # firm, so this reads "North Bank moved Summer Analyst's deadline",
    # not "Summer Analyst ... deadline moved" (see templates/crm/week.html).
    assert "moved" in body and "deadline" in body


def test_the_strip_refuses_to_report_a_closed_role_even_one_he_applied_to(client):
    """What the strip now declines to say, pinned at the rendered page.

    The founder screenshotted two cards on 2026-09-02, both naming Bank of
    America, the first of them "Bank of America closed Bank of America
    Campus Insight Forum: The Power to Lead - Fall 2026" — a role his own
    `applied_status` said he had submitted to. His words: "why is it
    telling me the programs I applied for has closed, I don't care."

    A close names no act, so it earns no slot on a three-card strip. The
    fact is not lost: `directory.views._posting_closed_note` marks the same
    row on the pipeline, with copy that knows whether he applied. Fixture
    uses `submitted` deliberately — the stage that would have survived the
    obvious "keep it where they applied" rescue is the one that produced
    the complaint. See `assistant.situation`'s module docstring.
    """
    from analytics.models import UserOpportunity
    from directory.models import Opportunity, OpportunityChange

    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="bank-of-america", name="Bank of America")
    opp = Opportunity.objects.create(
        firm=firm, title="Campus Insight Forum: The Power to Lead - Fall 2026",
        bucket="internship", status="closed",
        url="https://bofa.example/jobs/forum",
    )
    UserOpportunity(user=user, opportunity=opp, applied_status="submitted").save()
    OpportunityChange.objects.create(
        opportunity=opp, field="status", old_value="open", new_value="closed",
        stage="reverify", observed_at=timezone.now(),
    )

    body = _login_and_get(client, user)

    assert 'class="situation-strip"' not in body, (
        "a confirmed close drew a card. The only news this strip carries is "
        "news the student can act on today."
    )
    assert "Campus Insight Forum" not in body
    assert "closed <b>" not in body


def test_no_situation_cards_when_nothing_changed(client):
    user = _user(weekly_touch_goal=14)
    body = _login_and_get(client, user)
    assert 'class="situation-strip"' not in body


def test_the_htmx_partial_refresh_never_builds_the_situation_snapshot(client, monkeypatch):
    """Same invariant as the brief itself (see
    test_the_htmx_partial_refresh_never_calls_the_brief above): the partial
    refresh shares _cockpit_context with week(), but only the full page load
    may spend the extra situation queries."""
    import assistant.situation

    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Ada Lovelace")
    _touch(user, c, "outreach", days_ago=20)

    def fail_if_called(*a, **kw):
        raise AssertionError("the partial refresh must never build the situation snapshot")

    monkeypatch.setattr(assistant.situation, "build_situation", fail_if_called)
    client.force_login(user)
    resp = client.post(reverse("crm:today_park_all"))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# The Today page's query count must not scale with the size of the student's
# network. It used to: `_schedule` fed contacts into _cockpit.html, which
# renders the contact's FIRM per row, so every person on the page cost their
# own firm SELECT; and `_chat_prep` ran two `.first()` queries per chat
# happening today. Measured before the fix, a tenant with 20 CRM rows took
# 36 queries and one with 60 took 50. After the fix: 32 and 32.
#
# Deliberately a COMPARISON rather than a hardcoded number — the page
# legitimately gains and loses queries as features move, and a magic constant
# here would just get edited to whatever the code happens to do, defending
# nothing. What must hold is that the count is FLAT in the row count.
# ---------------------------------------------------------------------------
def _today_query_count(client, user, n_contacts: int) -> int:
    from django.db import connection, reset_queries
    from django.test import override_settings

    firms = [
        Firm.objects.create(slug=f"qf-{user.pk}-{i}", name=f"QF {i}")
        for i in range(8)
    ]
    for i in range(n_contacts):
        # A chat happening today, which is what _chat_prep walks.
        c = _contact(
            user=user, name=f"Q {i:03d}", firm=firms[i % 8],
            thread_state=["no_reply", "replied", "chat_scheduled"][i % 3],
        )
        _touch(user, c, "outreach", days_ago=20)
        CalendarEvent.all_objects.create(
            user=user, contact=c, title=f"chat {i}",
            starts_at=timezone.now() + timedelta(hours=1), kind="chat",
        )

    client.force_login(user)
    client.get(reverse("crm:week"))          # warm
    with override_settings(DEBUG=True):
        reset_queries()
        client.get(reverse("crm:week"))
        return len(connection.queries)


def test_today_does_not_run_more_queries_as_the_network_grows(client):
    small = _today_query_count(
        client, _user("q-small@example.com", weekly_touch_goal=14), 6)
    big = _today_query_count(
        client, _user("q-big@example.com", weekly_touch_goal=14), 24)
    assert big <= small + 1, (
        f"Today ran {small} queries for 6 contacts but {big} for 24 — a query "
        f"count that grows with the network is an N+1. Check select_related on "
        f"_schedule and the batched lookups in _chat_prep."
    )


# ---------------------------------------------------------------------------
# The OTHER axis: open roles (2026-09-01)
#
# The guard above varies CONTACTS, and `_a_silent_today_does_not_grow_its_
# query_count_with_target_firms` varies FIRMS. Today's real N+1 varied with
# neither: the ribbon's "names your year and still unsaved" cell folds every
# open campus role on the board and asks `directory.views._eligibility` about
# each one, and that verdict reads `opp.firm.sponsors` for every posting whose
# own text is silent on sponsorship. With no `select_related("firm")` on the
# queryset that is one SELECT per ROLE.
#
# Measured on the founder's live board the morning this test was written:
# 1,332 firm SELECTs, 1,397 queries for one Today render, 373 ms in that one
# block. Both existing guards were green throughout, because their fixtures
# keep the board at a handful of roles. This one grows the board instead.
# ---------------------------------------------------------------------------
def _today_query_count_for_roles(client, user, n_roles: int) -> int:
    """Today's query count with `n_roles` open campus roles at the user's own
    target firms, every one of them silent on sponsorship in a market the user
    needs sponsored — which is exactly the shape that made `_eligibility`
    reach for the firm row."""
    from django.db import connection, reset_queries
    from django.test import override_settings

    from directory.models import Opportunity

    user.class_year = 2028
    user.work_authorization = {"us": "sponsorship"}
    user.save(update_fields=["class_year", "work_authorization"])

    firms = [
        Firm.objects.create(slug=f"rf-{user.pk}-{i}", name=f"RF {i}",
                            sponsors={"us": "no"})
        for i in range(4)
    ]
    for f in firms:
        UserFirm.all_objects.create(user=user, firm=f, tier=1)
    for i in range(n_roles):
        Opportunity.objects.create(
            firm=firms[i % 4], url=f"https://rf.example/{user.pk}/{i}",
            # Distinct titles: repeat listings at one firm fold into one row
            # (`directory.dupes`), and a fixture that folded would grow the
            # board without growing the loop under test.
            title=f"2027 Summer Analyst {i:03d}", bucket="internship",
            status="open", region="us", sponsorship="unknown",
            class_year="2028",
        )

    client.force_login(user)
    client.get(reverse("crm:week"))          # warm
    with override_settings(DEBUG=True):
        reset_queries()
        client.get(reverse("crm:week"))
        return len(connection.queries)


def test_today_does_not_run_more_queries_as_the_board_grows(client):
    """The budget on the axis the other two could not see.

    Five roles against fifty. The count must not move at all: every extra
    query here is a per-role database round trip, and the founder's board has
    16,029 open rows to multiply it by."""
    few = _today_query_count_for_roles(
        client, _user("q-few-roles@example.com", weekly_touch_goal=14), 5)
    many = _today_query_count_for_roles(
        client, _user("q-many-roles@example.com", weekly_touch_goal=14), 50)
    assert many == few, (
        f"Today ran {few} queries for 5 open roles but {many} for 50 — "
        f"{many - few} extra for 45 extra roles. That is the eligibility "
        f"loop fetching a firm per posting; `campus` in `crm.today."
        f"_dashboard_context` needs its `select_related(\"firm\")`."
    )


def test_the_ribbons_unsaved_count_is_the_picked_columns_own(client):
    """REWRITTEN 2026-09-02. It read `eligible_unsaved == 50` — the retired
    bulk-save banner's count, which was every open role naming the user's
    class year, unranked and uncapped. The banner is gone (merged into the
    Picked for you column, the founder's call), and a ribbon still reading a
    deleted function would be a number with nothing on the other end.

    The cell counts the COLUMN's unsaved picks now, so 50 eligible roles is
    not 50: `recommend()` keeps at most `MAX_PER_FIRM` per firm and
    `DEFAULT_LIMIT` overall. What the number has to be is whatever the column
    is showing, which is what this asserts — the chip and the column read one
    function (`directory.views.picked_roles`), so they cannot disagree the way
    this chip and the banner once did (209 here against the feed's 206).

    Both halves are asserted: the count is the column's, and the column is
    capped rather than silently equal to the board."""
    from directory.recommend import DEFAULT_LIMIT

    user = _user("q-roles-count@example.com", weekly_touch_goal=14)
    _today_query_count_for_roles(client, user, 50)

    ctx = _dashboard_context(user)
    client.force_login(user)
    column = client.get(reverse("opportunities")).context["pick_cluster"]

    assert ctx["dash"]["picked_unsaved"] == column["save_count"]
    assert ctx["dash"]["picked_unsaved"] == DEFAULT_LIMIT
    assert ctx["dash"]["at_your_firms"] == 50


# ---------------------------------------------------------------------------
# Staleness decay for the critical lane.
#
# THE MEASURED BUG (founder's live queue, 2026-08-24). Daily cap 3, and all
# three slots went to class 0 every single morning: one genuine re-ping
# against a confirmed Aug 30 close, plus two `confirm_chat` cards — HSBC and
# Macquarie — asking the identical question, "chat was scheduled 16 business
# days ago, did it happen?", for the sixteenth consecutive working day.
# Criticals are never capped, so those two held 2 of 3 slots permanently and
# nothing behind them could ever come out. A question unanswered for three
# working weeks is not urgent, it is stuck.
#
# Every fixture below reproduces that exact shape: two aged `confirm_chat`
# criticals, one re-ping with a live deadline, one high-value keep-warm
# waiting behind them.
# ---------------------------------------------------------------------------
from coverage_domain.cadence import business_days_since          # noqa: E402
from crm.today import (                                          # noqa: E402
    CRITICAL_STALE_BUSINESS_DAYS,
    _stale_critical,
)


def _bd_ago(n: int) -> date:
    """The date exactly `n` business days before today.

    Walks back until `business_days_since` — the engine's own counter, and the
    one the queue's idle clock reads — actually returns `n`. Computed rather
    than assumed because "n weekdays back" and "n business days since" are not
    the same date when today is itself a weekend, and these are boundary tests:
    they are worthless if the fixture is one day off on a Saturday CI run.
    """
    today = timezone.localdate()
    d = today
    while business_days_since(d, today) < n:
        d -= timedelta(days=1)
    return d


def _touch_bd_ago(user, contact, kind, n: int):
    from datetime import datetime, time as _time

    at = timezone.make_aware(datetime.combine(_bd_ago(n), _time(12, 0)))
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel="email", ts=at,
    )


def _stuck_queue(*, confirm_idle: int):
    """The founder's shape, with the two stuck prompts at `confirm_idle`
    business days of silence.

    `weekly_touch_goal=3` pins the daily cap at 3 on every weekday: `_daily_cap`
    floors at TODAY_PLAN_MIN, and ceil(3 / workdays-left) never exceeds it. A
    goal of 10 would have made the cap 5 on a Friday and 3 on a Monday, which
    would have made this suite pass or fail depending on the day it ran.

    `chatted_touch_min_weeks=6` is the founder's own dial, deliberately turned
    out from the default 3 to stop hollow prompts, and it is reproduced here
    rather than defaulted: it is what keeps engine branch 5b silent about Katy,
    so her card comes from `_opening_keep_warms` — priority 2, raised by a real
    date at her firm. Her position in the queue has nothing to do with the dial
    being wrong, and this fixture is what proves it.
    """
    user = _user(weekly_touch_goal=3, cadence_params={"chatted_touch_min_weeks": 6})
    today = timezone.localdate()

    # 1. The genuine critical: a confirmed close six days out.
    jpm = Firm.objects.create(name="J.P. Morgan", slug="jpm-stale")
    UserFirm.all_objects.create(user=user, firm=jpm, tier=1)
    FirmDate.objects.create(
        firm=jpm, event_kind="app_close", region="us",
        date=today + timedelta(days=6), confidence=1.0, precision="day",
    )
    nick = _contact(user=user, name="Nick Tehle", firm=jpm, region="us",
                    warmth="chatted", thread_state="replied")
    _touch_bd_ago(user, nick, "outreach", 7)

    # 2 + 3. The two stuck prompts. The `chat_scheduled` row is what put them
    # in the state; the later `outreach` is the last REAL touch, so the idle
    # clock — and therefore the decay — is measured off a send of the
    # student's own, exactly as on the live rows.
    for name, firm_name, slug, tier in (
        ("Leo Ziqiang Yuan", "HSBC", "hsbc-stale", 1),
        ("William Zhang", "Macquarie", "macq-stale", 2),
    ):
        firm = Firm.objects.create(name=firm_name, slug=slug)
        UserFirm.all_objects.create(user=user, firm=firm, tier=tier)
        c = _contact(user=user, name=name, firm=firm, warmth="chatted",
                     thread_state="chat_scheduled")
        _touch_bd_ago(user, c, "chat_scheduled", confirm_idle + 3)
        _touch_bd_ago(user, c, "outreach", confirm_idle)

    # 4. Katy: tier 1, already chatted, a confirmed date at her firm five weeks
    # out. Five weeks is inside `relevance.OPENING_HORIZON_DAYS` (45) and well
    # outside the engine's `pre_deadline_reping_days` (14), so it is a live
    # reason to write without being a re-ping — which is what the live row is.
    nomura = Firm.objects.create(name="Nomura", slug="nomura-stale")
    UserFirm.all_objects.create(user=user, firm=nomura, tier=1)
    FirmDate.objects.create(
        firm=nomura, event_kind="app_close", region="us",
        date=today + timedelta(days=37), confidence=1.0, precision="day",
    )
    katy = _contact(user=user, name="Katy Chen", firm=nomura, region="us",
                    warmth="chatted", thread_state="chat_done")
    _touch_bd_ago(user, katy, "chat", 16)
    return user


def _named(items):
    return [a["contact"]["name"] for a in items]


def _lane(ctx, key):
    for lane in ctx["lanes"]:
        if lane["key"] == key:
            return lane["items"]
    return []


def test_the_measured_bug_reproduces_one_day_below_the_threshold():
    """The boundary, low side. At one business day under the threshold the two
    prompts are still treated as urgent, they still hold two of the three
    slots, and Katy is still behind them. This is the live queue as it was."""
    user = _stuck_queue(confirm_idle=CRITICAL_STALE_BUSINESS_DAYS - 1)
    ctx = _cockpit_context(user)

    assert ctx["daily_cap"] == 3
    assert _named(_lane(ctx, "critical")) == [
        "Nick Tehle", "Leo Ziqiang Yuan", "William Zhang",
    ]
    assert ctx["still_open_total"] == 0
    assert "Katy Chen" in _named(ctx["held"]), (
        "the fixture must reproduce the bug before the fix can be said to fix "
        "anything"
    )


def test_one_day_past_the_threshold_the_stuck_prompts_release_their_slots():
    """The boundary, high side. Three working weeks of silence and the two
    prompts stop being emergencies: the critical lane holds only the card with
    a real deadline behind it, and the slots they were sitting on go to the
    queue."""
    user = _stuck_queue(confirm_idle=CRITICAL_STALE_BUSINESS_DAYS)
    ctx = _cockpit_context(user)

    assert _named(_lane(ctx, "critical")) == ["Nick Tehle"]
    assert _named(ctx["still_open"]) == ["Leo Ziqiang Yuan", "William Zhang"]


def test_katy_chen_reaches_the_plan_once_the_nags_stop_holding_the_slots():
    """The acceptance case. A tier-1 contact who has actually had the chat,
    with a confirmed date at her firm five weeks out, must not be permanently
    invisible behind two prompts nobody has answered since the start of the
    month."""
    user = _stuck_queue(confirm_idle=CRITICAL_STALE_BUSINESS_DAYS)
    ctx = _cockpit_context(user)

    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert "Katy Chen" in _named(planned)
    assert "Katy Chen" not in _named(ctx["held"])


def test_a_stuck_prompt_never_silently_vanishes():
    """Decay moves a card; it never resolves one. Nobody but the student can
    say whether the chat happened, so the question stays on the page with every
    control it had — it just stops spending a plan slot to ask again."""
    user = _stuck_queue(confirm_idle=CRITICAL_STALE_BUSINESS_DAYS)
    ctx = _cockpit_context(user)

    assert ctx["still_open_total"] == 2
    for a in ctx["still_open"]:
        assert a["action"] == "confirm_chat", "the ask is unchanged"
        assert a["snoozable"] is True
        assert a["touch_kind"] is None, (
            "confirm_chat still refuses to log a chat in one click"
        )
    # Not folded into the paced remainder: "more queued" promises a morning on
    # which they arrive, and there is no such morning.
    assert "Leo Ziqiang Yuan" not in _named(ctx["held"])


def test_a_stuck_prompt_stops_repeating_itself():
    """Two cards reading the identical "chat was scheduled 16 business days
    ago, did it happen?" render as two separate emergencies and say nothing
    true about either. The second telling names the date instead."""
    user = _stuck_queue(confirm_idle=CRITICAL_STALE_BUSINESS_DAYS)
    ctx = _cockpit_context(user)

    since = _bd_ago(CRITICAL_STALE_BUSINESS_DAYS)
    expected = f"Still unresolved from {since.strftime('%b')} {since.day}."
    for a in ctx["still_open"]:
        assert a["reason"].startswith(expected), a["reason"]
        assert "did it happen" not in a["reason"].lower()


def test_the_plan_never_grows_past_the_cap_because_of_a_stuck_prompt():
    """The strip is free. Whatever lands in it must not come out of the day's
    budget, or the fix has moved the cost rather than removed it."""
    user = _stuck_queue(confirm_idle=CRITICAL_STALE_BUSINESS_DAYS)
    ctx = _cockpit_context(user)

    assert ctx["planned_total"] <= ctx["daily_cap"]
    still_open_ids = {a["contact"]["id"] for a in ctx["still_open"]}
    planned_ids = {
        a["contact"]["id"] for lane in ctx["lanes"] for a in lane["items"]
    }
    assert not (still_open_ids & planned_ids), "a card cannot be in both"


def test_a_live_deadline_never_decays_however_long_it_has_been_quiet():
    """Requirement the whole design turns on: decay is by UNANSWERED AGE, not a
    blanket timer. The clock behind a re-ping belongs to the world, not to how
    long the student has been ignoring us, so silence cannot retire it."""
    user = _stuck_queue(confirm_idle=CRITICAL_STALE_BUSINESS_DAYS)
    nick = Contact.all_objects.get(user=user, name="Nick Tehle")
    Touch.all_objects.filter(contact=nick).delete()
    _touch_bd_ago(user, nick, "outreach", CRITICAL_STALE_BUSINESS_DAYS * 3)

    ctx = _cockpit_context(user)
    assert _named(_lane(ctx, "critical")) == ["Nick Tehle"]
    assert "Nick Tehle" not in _named(ctx["still_open"])


def test_a_deadline_that_has_passed_confers_no_exemption():
    """A card pointing at an application that has already closed is not
    time-critical, it is over. Unreachable from the live path today —
    `cadence._closing_soon` drops a past close before an action exists — so it
    is pinned here directly: the invariant lives in another module, and a
    loosening there must not hand a dead date a permanent front-row slot."""
    today = timezone.localdate()
    yesterday = {"action": "reping", "priority": 0,
                 "closes_on": today - timedelta(days=1), "last_business_days": 1}
    tomorrow = {"action": "reping", "priority": 0,
                "closes_on": today + timedelta(days=1), "last_business_days": 999}
    assert _stale_critical(yesterday, today) is True
    assert _stale_critical(tomorrow, today) is False


def test_an_unknown_age_never_decays():
    """`last_business_days` is None when the contact carries no dateable touch
    at all. That reads "we cannot say how long this has been unanswered", never
    "forever" — nothing may retire a prompt on a number it does not have."""
    today = timezone.localdate()
    a = {"action": "confirm_chat", "priority": 1, "closes_on": None,
         "last_business_days": None}
    assert _stale_critical(a, today) is False


def test_decay_only_ever_touches_cards_that_were_critical():
    """The demotion is a qualifier on the never-capped exemption, so it can
    only subtract from the set that holds it. A cold follow-up silent for a
    year is paced by the cap like every other one; it does not acquire a strip
    of its own."""
    today = timezone.localdate()
    cold = {"action": "follow_up", "priority": 1, "closes_on": None,
            "last_business_days": 400}
    assert _stale_critical(cold, today) is False


def test_a_queue_of_only_stuck_prompts_is_not_a_silent_queue(client):
    """The seeds gate. A parked contact is a decision the student already made;
    an unanswered "did it happen?" is a question they still owe. Telling that
    student to go add three people would talk straight past the two things
    actually waiting on them."""
    user = _user(weekly_touch_goal=3)
    firm = Firm.objects.create(name="HSBC", slug="hsbc-silent")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    c = _contact(user=user, name="Leo Ziqiang Yuan", firm=firm,
                 warmth="chatted", thread_state="chat_scheduled")
    _touch_bd_ago(user, c, "outreach", CRITICAL_STALE_BUSINESS_DAYS)

    ctx = _cockpit_context(user)
    assert ctx["still_open_total"] == 1
    assert ctx["lanes"] == []
    assert ctx["seeds"] == [], "a stuck question is not an empty account"

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "Still open" in body
    assert "Leo Ziqiang Yuan" in body


# ---------------------------------------------------------------------------
# Clock-silent kinds on the VIEW layer (regressions found 2026-08-27)
# ---------------------------------------------------------------------------
def test_an_inbound_blast_never_fills_the_ring():
    """`bulk_received` joined TOUCH_TRANSITIONS after PACE_TOUCH_KINDS'
    derivation was written, so a newsletter LANDING counted as work the
    user did (live: 1 of the ring's 6 "done" was an inbound blast)."""
    assert "bulk_received" not in PACE_TOUCH_KINDS
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Newsletter Victim")
    _touch(user, c, "bulk_received")
    assert _cockpit_context(user)["pace"]["done"] == 0


def test_a_bulk_touch_after_a_reply_does_not_mask_the_owed_reply():
    """The engine calls `bulk_received` clock-silent; the view's own
    last-touch scan skipped only `manual_override`. Their OOO auto-reply
    landing seconds after a genuine reply became the "last real touch",
    `owed_reply` went False, and the person fell out of the queue with a
    reply still owed (live: contact replied Aug 21, masked by a same-day
    bulk touch). The last REAL touch must stay the reply."""
    from crm.today import _build_actions

    user = _user()
    c = _contact(user=user, name="Ebba Reply", email="ebba@firm.example")
    _touch(user, c, "reply_received", days_ago=1)
    _touch(user, c, "bulk_received", days_ago=0)

    actions, _ = _build_actions(user)
    mine = [a for a in actions if a["contact"]["id"] == c.id]
    assert mine, "the contact must still generate an action"
    assert all(a["owed_reply"] for a in mine), (
        "an inbound blast must not overwrite the fact that their reply is "
        "still unanswered"
    )
    assert mine[0]["last_kind"] and "bulk" not in mine[0]["last_kind"].lower()


# ---------------------------------------------------------------------------
# 2026-09-01 audit: an opening-raised keep-warm has to carry a SORTABLE tier.
# ---------------------------------------------------------------------------
def test_an_opening_keep_warm_at_an_unranked_firm_does_not_break_the_page():
    """`crm.views.set_firm_tier` writes `tier=None` on purpose when a firm is
    dragged to the "Unranked" lane — a real recorded value, not an absent key.

    `_build_actions` builds `firm_meta` as `tiers.get(fid, 3)`, so for an
    unranked firm the key is PRESENT holding None and the `, 3` default never
    fires. Every action the cadence engine returns has already been through
    `cadence._coerce_tier`; `_opening_keep_warms` builds its own action dicts
    by hand and used to copy the raw value straight in. `_today_sort_key`'s
    fourth key is `a["tier"]`, so one such card tying with any other action on
    (class, ev, priority) compared None against an int and took the whole Today
    page down with a TypeError.

    `_coerce_tier`'s own docstring names this exact failure and warns that
    "fixing the reported shape and leaving its siblings is how the same bug
    ships twice". This is the sibling. Latent rather than live: measured
    2026-09-01, the founder has 54 tiered firms and none unranked.
    """
    from crm.today import _build_actions, _today_sort_key

    user = _user("unranked-tier@example.com")
    # Dial the keep-warm clock out past the contact's own idle age, so engine
    # branch 5b stays quiet and the ONLY thing that can raise this card is
    # `_opening_keep_warms`.
    user.cadence_params = {"chatted_touch_min_weeks": 6}
    user.save(update_fields=["cadence_params"])

    firm = Firm.objects.create(slug="unranked-sortkey", name="Unranked Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=None)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="hk", event_kind="app_close",
        date=timezone.localdate() + timedelta(days=30),
        precision="day", confidence=1.0,
    )
    _contact(
        user=user, name="Unranked Warm", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    contact = Contact.all_objects.get(user=user, name="Unranked Warm")
    # Inside the 6-week dial, past OPENING_MIN_IDLE_DAYS (10).
    _touch(user, contact, "chat", days_ago=21)

    actions, _ = _build_actions(user)
    card = next(a for a in actions if a["contact"]["name"] == "Unranked Warm")
    assert card["from_opening"] is True
    # The engine's own answer for an unranked firm, not the raw column.
    assert card["tier"] == 3
    # THE ACTUAL FAILURE MODE, stated directly rather than staged: the sort key
    # has to be comparable against an ordinary tier. `None < 3` is the
    # TypeError that took the page down.
    assert card["tier"] < 4
    assert _today_sort_key(card)[3] < 4


# ---------------------------------------------------------------------------
# PER-FIRM DAILY PACE (`FIRM_DAILY_CONTACT_CAP`, `_pace_by_firm`).
#
# THE EVIDENCE. Practitioners, unprompted and unanimous: "Do not email
# multiple people on the same team in the same day, give it a couple of days
# or a week... The analysts talk to each other." A ceiling of "4-5 people max"
# per group, and "you don't need to network with every group at a bank, just
# the 1-2 you're most interested in". Separately, a bulge-bracket associate
# takes ~30 networking emails a week and forwards ~5 resumes a YEAR — the
# scarce resource is his referral budget, and student-side volume cannot
# expand it.
#
# THE DEFECT, measured on the founder's live account 2026-09-01: 44 queue
# actions across exactly four firms (12 J.P. Morgan, 11 Citi, 11 Goldman, 10
# Morgan Stanley), and 26 separate days in his own history with 3+ outbound
# touches into one bank — peaking at 13 Morgan Stanley contacts, 12 J.P.
# Morgan, 12 Citi and 11 Goldman on the same day. The queue was generating
# exactly the behaviour the evidence says damages the user.
# ---------------------------------------------------------------------------
def _paced_names(actions):
    return {a["contact"]["name"] for a in actions if a["firm_paced"]}


def test_a_firm_gets_two_cards_a_day_and_the_rest_pace_out():
    from crm.today import FIRM_DAILY_CONTACT_CAP, _build_actions

    user = _user("pace-one-firm@example.com")
    firm = Firm.objects.create(slug="pace-bulge", name="Bulge Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for i in range(6):
        _contact(user=user, name=f"Banker {i}", firm=firm)

    actions, _ = _build_actions(user)
    assert len(actions) == 6, "fixture must produce one card per contact"
    live = [a for a in actions if not a["firm_paced"]]
    assert len(live) == FIRM_DAILY_CONTACT_CAP == 2
    assert len(_paced_names(actions)) == 4

    # NEVER SILENTLY HIDDEN. Every one of the six is still in the queue, and
    # the four that wait say so in the sentence the student actually reads.
    for a in actions:
        if a["firm_paced"]:
            assert "Bulge Bank already has 2 today" in a["reason"]
            assert "better tomorrow" in a["reason"]
        else:
            assert "already has" not in a["reason"]


def test_one_contact_per_firm_sees_no_change_at_all():
    """Degrade to today's behaviour on thin data. A student whose network is
    one person per bank must not notice this feature exists."""
    from crm.today import _build_actions

    user = _user("pace-thin@example.com")
    for i, name in enumerate(("Alpha Bank", "Beta Bank", "Gamma Bank")):
        firm = Firm.objects.create(slug=f"pace-thin-{i}", name=name)
        UserFirm.all_objects.create(user=user, firm=firm, tier=1)
        _contact(user=user, name=f"Only Contact {i}", firm=firm)

    actions, _ = _build_actions(user)
    assert len(actions) == 3
    assert _paced_names(actions) == set()


def test_notes_already_sent_today_spend_the_firms_budget():
    """The cap is per firm per DAY, not per page load. A student who worked
    two cards this morning and reopens Today after lunch does not get a fresh
    allowance at that bank."""
    from crm.today import _build_actions

    user = _user("pace-sent-today@example.com")
    firm = Firm.objects.create(slug="pace-sent", name="Sent Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for i in range(3):
        _contact(user=user, name=f"Fresh {i}", firm=firm)

    # Baseline: three untouched contacts, two of them live.
    assert len(_paced_names(_build_actions(user)[0])) == 1

    # Two notes already sent into this firm today, to people whose own cards
    # the cadence is therefore quiet about. The inbox does not care that the
    # queue has nothing left to say about them.
    for i in range(2):
        already = _contact(user=user, name=f"Emailed {i}", firm=firm)
        _touch(user, already, "outreach", days_ago=0)

    actions, _ = _build_actions(user)
    assert {a["contact"]["name"] for a in actions} == {"Fresh 0", "Fresh 1", "Fresh 2"}
    assert len(_paced_names(actions)) == 3, (
        "the morning's two sends did not spend the firm's daily budget"
    )


def test_inbound_mail_does_not_spend_a_firms_budget():
    """Only what the STUDENT sent counts. Replies landing from a bank are
    that bank writing to them, and a queue that paced itself on other
    people's mail would go quiet exactly when a thread got warm."""
    from crm.today import _build_actions

    user = _user("pace-inbound@example.com")
    firm = Firm.objects.create(slug="pace-inbound-firm", name="Inbound Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for i in range(3):
        _contact(user=user, name=f"Fresh {i}", firm=firm)
    # Parked, so the queue has no card of its own about this person and the
    # only thing under test is whether their inbound mail spends the budget.
    noisy = _contact(user=user, name="Wrote To Me", firm=firm,
                     thread_state="parked")
    for kind in ("reply_received", "chat_scheduled", "bulk_received"):
        _touch(user, noisy, kind, days_ago=0)

    actions, _ = _build_actions(user)
    assert {a["contact"]["name"] for a in actions} == {"Fresh 0", "Fresh 1", "Fresh 2"}
    assert len(_paced_names(actions)) == 1, (
        "other people's mail spent the firm's daily budget"
    )


def test_a_confirmed_deadline_is_never_paced_and_still_spends_the_budget():
    """A confirmed close is never something the page decides you'll get to
    tomorrow — the same exemption the daily cap already grants. The banker's
    inbox does not care why the email was urgent, so the re-pings still spend
    the budget, and the cold card behind them waits."""
    from crm.today import _build_actions, _is_critical

    user = _user("pace-critical@example.com")
    firm = Firm.objects.create(slug="pace-crit", name="Closing Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    FirmDate.objects.create(
        firm=firm, event_kind="app_close", region="us",
        date=timezone.localdate() + timedelta(days=6),
        confidence=1.0, precision="day",
    )
    for i in range(3):
        warm = _contact(user=user, name=f"Warm {i}", firm=firm, region="us",
                        warmth="replied", thread_state="replied")
        _touch(user, warm, "reply_received", days_ago=20)
    _contact(user=user, name="Cold Stranger", firm=firm, region="us")

    actions, _ = _build_actions(user)
    criticals = [a for a in actions if _is_critical(a)]
    assert len(criticals) == 3, "fixture must produce three re-pings"
    assert not any(a["firm_paced"] for a in criticals)
    assert _paced_names(actions) == {"Cold Stranger"}


def test_a_contact_with_no_nameable_employer_is_never_paced():
    """A blank firm is a missing field, not a value. Pooling every hand-added
    contact under one empty key would pace people who work nowhere near each
    other — the same refusal `cadence.contact_region` makes about a blank
    region."""
    from crm.today import _build_actions

    user = _user("pace-blank@example.com")
    for i in range(5):
        _contact(user=user, name=f"Nowhere {i}")

    actions, _ = _build_actions(user)
    assert len(actions) == 5
    assert _paced_names(actions) == set()


def test_free_text_employers_pace_by_their_own_name():
    """No `firm_id` is not the same as no employer. Five people whose rows
    say the same bank in free text are five people at one bank."""
    from crm.today import _build_actions

    user = _user("pace-freetext@example.com")
    for i in range(4):
        _contact(user=user, name=f"Textual {i}", firm_text="Jefferies")
    _contact(user=user, name="Elsewhere", firm_text="Lazard")

    actions, _ = _build_actions(user)
    assert len(_paced_names(actions)) == 2
    assert "Elsewhere" not in _paced_names(actions)


def test_paced_cards_lose_the_plan_slot_and_keep_the_queue():
    """What the flag COSTS is a plan slot and nothing else. The card still
    renders under "Up next" with every button it had, and its own sentence
    says which bank is at pace."""
    from crm.today import _build_actions

    # `weekly_touch_goal=3` pins the daily cap at 3 on every weekday, so this
    # passes on a Monday and a Friday alike (see `_stuck_queue`).
    user = _user("pace-cockpit@example.com", weekly_touch_goal=3)
    for i, name in enumerate(("Alpha Bank", "Beta Bank")):
        firm = Firm.objects.create(slug=f"pace-plan-{i}", name=name)
        UserFirm.all_objects.create(user=user, firm=firm, tier=1)
        for j in range(5):
            _contact(user=user, name=f"{name} {j}", firm=firm)

    ctx = _cockpit_context(user)
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert planned, "the plan must not empty itself out"
    assert not any(a["firm_paced"] for a in planned)

    by_firm = {}
    for a in planned:
        by_firm[a["firm_name"]] = by_firm.get(a["firm_name"], 0) + 1
    assert max(by_firm.values()) <= 2, f"a firm took more than its day: {by_firm}"
    assert len(by_firm) >= 2, "pacing must spread the plan, not starve a firm"

    # Nobody vanished: ten contacts, ten cards, and the ones that waited say
    # why on the card the student can still open.
    assert len(planned) + ctx["held_total"] == 10
    paced_held = [a for a in ctx["held"] if a["firm_paced"]]
    assert paced_held
    for a in paced_held:
        assert a["pace_note"] in a["reason"]
        assert a["firm_name"] in a["reason"]


def test_the_budget_is_spent_by_sends_and_only_by_sends():
    """`FIRM_PACE_TOUCH_KINDS` is derived from the ratchet's own vocabulary,
    so a kind added later counts as a send by default and has to be excluded
    on purpose — the direction that fails safe. `bulk_received` is why: it
    joined `TOUCH_TRANSITIONS` without being named anywhere, and a hand-listed
    set would not have seen it."""
    from crm.today import FIRM_PACE_TOUCH_KINDS

    assert FIRM_PACE_TOUCH_KINDS == {
        "outreach", "follow_up", "thank_you", "maintain", "reping",
    }
    # A conversation is the student's own work (the pace ring counts it) but
    # it is not a message landing in an inbox.
    assert "chat" not in FIRM_PACE_TOUCH_KINDS
    assert "chat" in PACE_TOUCH_KINDS


def test_a_stale_critical_does_not_spend_a_firms_budget():
    """A critical the plan has stopped budgeting for is not an email the page
    is asking for today, so it must not charge the firm for one.

    THE CASE THAT CAUGHT IT (`test_today_order`'s own fixture): two
    three-week-old `confirm_chat` prompts at one bank, both already decayed to
    the "Still open" strip and costing no plan slot, spent the whole daily
    budget and silenced all twelve of that bank's cold follow-ups. The cold
    lane rendered empty."""
    from crm.today import _build_actions, _is_critical, _stale_critical

    user = _user("pace-stale-crit@example.com")
    firm = Firm.objects.create(slug="pace-stale", name="Stuck Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for i in range(2):
        stuck = _contact(user=user, name=f"Stuck {i}", firm=firm,
                         warmth="chatted", thread_state="chat_scheduled")
        _touch(user, stuck, "chat_scheduled", days_ago=60)
    for i in range(4):
        cold = _contact(user=user, name=f"Cold {i}", firm=firm)
        # 10-13 calendar days (rewritten 2026-09-01): due on every weekday
        # and inside the follow-up's 15-business-day shelf life, so these
        # stay the live cold follow-ups the test is about. At 40+ days
        # branch 6 now parks them (`followup_expires_after_business_days`),
        # and a park is not the sendable card this budget test is counting.
        _touch(user, cold, "outreach", days_ago=10 + i)

    today = timezone.localdate()
    actions, _ = _build_actions(user)
    stuck_cards = [a for a in actions if a["contact"]["name"].startswith("Stuck")]
    assert len(stuck_cards) == 2
    assert all(_is_critical(a) and _stale_critical(a, today) for a in stuck_cards)

    # The two stuck prompts spend nothing, so the four cold cards still get
    # the firm's full two-a-day.
    cold_live = [a for a in actions
                 if a["contact"]["name"].startswith("Cold") and not a["firm_paced"]]
    assert len(cold_live) == 2
    assert not any(a["firm_paced"] for a in stuck_cards)


# ---------------------------------------------------------------------------
# The per-firm cap must never defer a send the other side is expecting.
# Found by audit the day the cap shipped (2026-09-01): an owed reply and a
# thank-you inside its window were paced behind two cold follow-ups at the
# same bank, and the thank-you re-entered the next morning as OVERDUE and
# critical - the cap manufacturing the state it exempts.
# ---------------------------------------------------------------------------

def _pace_action(cid, firm_id, action, *, ev=2.4, owed_reply=False, **extra):
    a = {"contact": {"id": cid, "firm_id": firm_id}, "action": action,
         "ev": ev, "owed_reply": owed_reply, "reason": "r", "firm_name": "Citi",
         "class": 2, "priority": 1, "last_business_days": 5, "closes_on": None}
    a.update(extra)
    return a


def test_the_firm_cap_never_paces_an_owed_reply_or_a_thank_you():
    from crm.today import _pace_by_firm, FIRM_DAILY_CONTACT_CAP
    # Two cold follow-ups fill the firm's budget...
    cold = [_pace_action(i, 9, "follow_up", ev=2.4) for i in range(FIRM_DAILY_CONTACT_CAP)]
    # ...then an owed reply, a thank-you and an advance arrive, all lower ev.
    reply = _pace_action(90, 9, "follow_up", ev=1.0, owed_reply=True)
    thanks = _pace_action(91, 9, "thank_you", ev=1.0)
    advance = _pace_action(92, 9, "advance", ev=1.0)
    _pace_by_firm(cold + [reply, thanks, advance], sent_today={}, today=None)
    assert not reply["firm_paced"], "a reply someone is waiting on was told 'better tomorrow'"
    assert not thanks["firm_paced"], "a thank-you inside its window was deferred"
    assert not advance["firm_paced"]
    for a in (reply, thanks, advance):
        assert "better tomorrow" not in a["reason"]


def test_expected_sends_still_spend_the_firms_budget():
    """The exemption is not a free pass into the firm: two owed replies at
    Citi this morning mean a third COLD note there waits, because the
    banker's inbox does not care why the first two were expected."""
    from crm.today import _pace_by_firm, FIRM_DAILY_CONTACT_CAP
    replies = [_pace_action(i, 9, "follow_up", ev=1.0, owed_reply=True)
               for i in range(FIRM_DAILY_CONTACT_CAP)]
    cold = _pace_action(50, 9, "follow_up", ev=5.0)   # highest ev, still waits
    _pace_by_firm(replies + [cold], sent_today={}, today=None)
    assert all(not r["firm_paced"] for r in replies)
    assert cold["firm_paced"], "the cold note should wait behind the expected sends"


def test_only_self_initiated_kinds_are_in_the_paceable_set():
    """`promised_followup` joined the set on 2026-09-02 (cadence branch 5a,
    WS-CRM-07). It belongs: it is a message the student sends into a banker's
    inbox, and the whole point of the per-firm budget is that the inbox does
    not care why the email was sent. The exclusions below are unchanged and
    are what this test is really guarding — a kind that is NOT the student's
    own send must never start spending a firm's budget."""
    from crm.today import FIRM_PACEABLE_ACTIONS
    assert FIRM_PACEABLE_ACTIONS == {"first_outreach", "follow_up", "keep_warm",
                                     "maintain", "confirm_chat",
                                     "promised_followup"}
    for expected in ("thank_you", "advance", "reping", "park"):
        assert expected not in FIRM_PACEABLE_ACTIONS


# ---------------------------------------------------------------------------
# PACE BY FIRM AND MARKET (`_pace_firm_key`).
#
# The practitioner rule is per TEAM: "do not email multiple people on the
# same team in the same day, the analysts talk to each other".
# `Contact.region` is the closest honest proxy the data holds for a team (on
# the founder's live contacts, 2026-09-01: hk 94 / us 61 / blank 71).
# Measured before this: two HK and two US contacts at one bank shared one
# 2-a-day budget, so `us #3` and `#4` waited behind desks that never talk to
# them.
# ---------------------------------------------------------------------------
def _market_action(cid, firm_id, region, **kw):
    a = _pace_action(cid, firm_id, "follow_up", **kw)
    a["contact"]["region"] = region
    return a


def test_a_market_is_part_of_the_pace_key_and_a_blank_one_is_not():
    """A set region splits the firm's budget; a blank one is unknown, and
    unknown gets the firm's pool rather than a guess: the firm-only key, byte
    for byte what it was."""
    from crm.today import _pace_firm_key

    assert _pace_firm_key({"firm_id": 9}) == ("id", 9)
    assert _pace_firm_key({"firm_id": 9, "region": ""}) == ("id", 9)
    assert _pace_firm_key({"firm_id": 9, "region": "hk"}) == ("id", 9, "hk")
    assert _pace_firm_key({"firm_id": 9, "region": "us"}) == ("id", 9, "us")
    assert _pace_firm_key({"firm_id": None, "firm_text": "Jefferies"}) == ("text", "jefferies")
    assert _pace_firm_key(
        {"firm_id": None, "firm_text": "Jefferies", "region": "us"}
    ) == ("text", "jefferies", "us")
    assert _pace_firm_key({"firm_id": None, "firm_text": "", "region": "us"}) is None


def test_two_markets_at_one_bank_are_two_daily_budgets():
    from crm.today import _pace_by_firm

    hk = [_market_action(i, 9, "hk", ev=9.0) for i in range(2)]
    us = [_market_action(10 + i, 9, "us", ev=1.0) for i in range(3)]
    _pace_by_firm(hk + us, sent_today={}, today=None)
    assert not any(a["firm_paced"] for a in hk)
    # Two US cards go despite scoring below every HK card; the third waits on
    # its OWN desk's budget, and the note says which desk.
    assert [a["firm_paced"] for a in us] == [False, False, True]
    assert us[2]["pace_note"] == (
        "Citi (US) already has 2 today, so this one is better tomorrow"
    )
    assert not any("(" in a["reason"] for a in hk + us[:2])


def test_a_blank_region_paces_exactly_as_the_firm_alone_did():
    """The founder's live queue the day this landed: 44 actions at four banks
    (12 J.P. Morgan, 11 Citi, 11 Goldman, 10 Morgan Stanley), every one with
    a blank region. Not one of them may move: same 8 live, same 36 paced,
    same sentence, no market named."""
    from crm.today import _pace_by_firm

    def queue(region):
        out, cid = [], 0
        for fid, n in ((1, 12), (2, 11), (3, 11), (4, 10)):
            for _ in range(n):
                a = _pace_action(cid, fid, "follow_up", ev=2.4)
                if region is not None:
                    a["contact"]["region"] = region
                out.append(a)
                cid += 1
        _pace_by_firm(out, sent_today={}, today=None)
        return out

    no_field, blank = queue(None), queue("")
    assert sum(a["firm_paced"] for a in blank) == 36
    assert (
        [(a["firm_paced"], a["reason"]) for a in blank]
        == [(a["firm_paced"], a["reason"]) for a in no_field]
    )
    assert not any("(" in a["pace_note"] for a in blank)


def test_a_morning_of_hk_sends_does_not_spend_the_us_desks_budget():
    """Sends already logged today are tallied by the same key the cards use
    (`_build_actions` keys `sent_today` off `_pace_firm_key`), so a morning
    spent on Hong Kong leaves the New York desk its full day."""
    from crm.today import _build_actions

    user = _user("pace-market-sent@example.com")
    firm = Firm.objects.create(slug="pace-market", name="Split Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for i in range(2):
        done = _contact(user=user, name=f"HK Emailed {i}", firm=firm, region="hk")
        _touch(user, done, "outreach", days_ago=0)
    _contact(user=user, name="HK Fresh", firm=firm, region="hk")
    _contact(user=user, name="US Fresh", firm=firm, region="us")

    actions, _ = _build_actions(user)
    by_name = {a["contact"]["name"]: a for a in actions}
    assert by_name["HK Fresh"]["firm_paced"], (
        "two HK sends this morning did not spend the HK desk's budget"
    )
    assert "Split Bank (HK) already has 2 today" in by_name["HK Fresh"]["reason"]
    assert not by_name["US Fresh"]["firm_paced"], (
        "the US desk was paced behind Hong Kong sends"
    )


# ---------------------------------------------------------------------------
# OUTREACH BLACKOUT (`outreach_blackout`: Dec 20 to Jan 2, plus weekends).
#
# THE EVIDENCE (Grade A, two practitioners describing their own inboxes, Dec
# 2025): "None. Anyone sending emails right now is on my shit list honestly.
# This is one of the only quiet weeks. Wait until first week of January."
# The only window in the research set where outreach is called DAMAGING
# rather than low-yield. Weekdays over weekends: near-unanimous across five.
#
# THE DEFECT, measured on the founder's live account with the clock patched:
# 2026-12-24, 94 actions, daily cap 5, five planned. Saturday 2026-12-26,
# inside the window, the plan fired in full.
#
# The cockpit tests here run on the REAL calendar (`outreach_blackout`
# marker); every other test in the suite sees an ordinary weekday. See
# coverage_web/conftest.py for why that default exists.
# ---------------------------------------------------------------------------
_UTC = ZoneInfo("UTC")


def _frozen(day: date):
    """Pin the clock at noon UTC on `day`, at the module Django itself reads,
    so `timezone.now`, `localtime` and `localdate` all agree (the pattern
    test_today_timezone.py established). No zone is activated, so the local
    date IS `day`."""
    return mock.patch(
        "django.utils.timezone.now",
        return_value=datetime(day.year, day.month, day.day, 12, tzinfo=_UTC),
    )


def _blackout_queue(tag: str):
    """One confirmed-deadline re-ping (critical) plus three cold follow-ups at
    a second bank, of which the cap paces one, so a card that is BOTH
    firm-paced and blacked out is always in the fixture. Built inside the
    caller's frozen clock so every `days_ago` is relative to the pinned day."""
    user = _user(f"blackout-{tag}@example.com", weekly_touch_goal=14)
    closing = Firm.objects.create(slug=f"bo-closing-{tag}", name="Closing Bank")
    UserFirm.all_objects.create(user=user, firm=closing, tier=1)
    FirmDate.objects.create(
        firm=closing, event_kind="app_close", region="us",
        date=timezone.localdate() + timedelta(days=6),
        confidence=1.0, precision="day",
    )
    warm = _contact(user=user, name="Deadline Warm", firm=closing, region="us",
                    warmth="replied", thread_state="replied")
    _touch(user, warm, "reply_received", days_ago=20)
    cold = Firm.objects.create(slug=f"bo-cold-{tag}", name="Cold Bank")
    UserFirm.all_objects.create(user=user, firm=cold, tier=1)
    for i in range(3):
        _contact(user=user, name=f"Cold {i}", firm=cold)
    return user


@pytest.mark.outreach_blackout   # the helper itself, so the suite default must not answer for it
def test_outreach_blackout_names_the_holiday_window_and_weekends():
    from crm.today import outreach_blackout

    assert outreach_blackout(date(2026, 12, 19)) == "weekend"   # the Saturday before
    assert outreach_blackout(date(2026, 12, 20)) == "holiday"   # a Sunday: the holiday wins
    assert outreach_blackout(date(2026, 12, 24)) == "holiday"
    assert outreach_blackout(date(2026, 12, 25)) == "holiday"   # a business day to the engine
    assert outreach_blackout(date(2026, 12, 26)) == "holiday"   # the Saturday that fired in full
    assert outreach_blackout(date(2027, 1, 2)) == "holiday"     # inclusive
    assert outreach_blackout(date(2027, 1, 3)) == "weekend"     # a Sunday, just outside
    assert outreach_blackout(date(2027, 1, 4)) is None
    assert outreach_blackout(date(2026, 3, 3)) is None          # a Tuesday
    assert outreach_blackout(date(2031, 12, 25)) == "holiday"   # year-agnostic


def test_the_resume_day_is_the_first_weekday_after_the_window():
    """Not the window's edge. Jan 3, 2027 is a Sunday, and "resumes Jan 3"
    would have sent the student to email on a day the weekend rule then says
    not to, in this very cycle."""
    from crm.today import _blackout_resumes

    assert _blackout_resumes(date(2026, 12, 24), "holiday") == "Jan 4"
    assert _blackout_resumes(date(2027, 1, 1), "holiday") == "Jan 4"    # same window, from January
    assert _blackout_resumes(date(2027, 12, 24), "holiday") == "Jan 3"  # a Monday
    assert _blackout_resumes(date(2025, 12, 24), "holiday") == "Jan 5"  # Jan 3, 2026 is a Saturday
    assert _blackout_resumes(date(2026, 12, 5), "weekend") == "Monday"


@pytest.mark.outreach_blackout
def test_the_holiday_plans_confirmed_deadlines_only_and_marks_the_rest():
    with _frozen(date(2026, 12, 24)):
        assert timezone.localdate() == date(2026, 12, 24)
        user = _blackout_queue("holiday")
        ctx = _cockpit_context(user)
        body = render_to_string("crm/_cockpit.html", ctx)

    assert ctx["blackout"] == "holiday"
    assert ctx["blackout_resumes"] == "Jan 4"
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    # The confirmed deadline is never something the page decides you'll get
    # to in January.
    assert [a["action"] for a in planned] == ["reping"]
    assert planned[0]["blackout"] is None
    assert "Bankers are off" not in planned[0]["reason"]
    assert ctx["planned_total"] == 1
    # Everything else: MARKED, never dropped, and says so on the card.
    assert {a["contact"]["name"] for a in ctx["held"]} == {"Cold 0", "Cold 1", "Cold 2"}
    assert sum(1 for a in ctx["held"] if a["firm_paced"]) == 1, (
        "fixture must hold a firm-paced card"
    )
    for a in ctx["held"]:
        assert a["blackout"] == "holiday"
        assert a["reason"].endswith("Bankers are off until Jan 4. This one is better then.")
        assert "better tomorrow" not in a["reason"], (
            "a firm-pace clause contradicting the blackout survived on the card"
        )
    # The strip, and the two lines that used to quote the cap.
    assert 'data-blackout="holiday"' in body
    assert "Outreach resumes Jan 4." in body
    assert "Confirmed deadlines still show below." in body
    assert "It's the weekend" not in body
    assert "waiting for Jan 4" in body
    assert "pacing out at" not in body
    assert "Resumes Jan 4." in body
    assert "more to go" not in body


@pytest.mark.outreach_blackout
def test_a_weekend_holds_everything_but_confirmed_deadlines_until_monday():
    with _frozen(date(2026, 12, 5)):   # a Saturday outside the holiday window
        user = _blackout_queue("weekend")
        ctx = _cockpit_context(user)
        body = render_to_string("crm/_cockpit.html", ctx)

    assert ctx["blackout"] == "weekend"
    assert ctx["blackout_resumes"] == "Monday"
    assert [a["action"] for lane in ctx["lanes"] for a in lane["items"]] == ["reping"]
    assert len(ctx["held"]) == 3
    for a in ctx["held"]:
        assert a["blackout"] == "weekend"
        assert a["reason"].endswith("It's the weekend. Better Monday.")
        assert "better tomorrow" not in a["reason"]
    assert "It's the weekend. Outreach resumes Monday." in body
    assert "waiting for Monday" in body
    assert "Resumes Monday." in body


@pytest.mark.outreach_blackout
def test_the_strip_does_not_point_at_deadlines_that_are_not_there():
    """No critical in the queue, no "still show below": a strip pointing at a
    lane that is not rendered would be the page over-claiming in a new way.
    And held work is not an empty account: no seeds, and the "Done for today"
    line names the wait rather than a cap that is not what holds them."""
    with _frozen(date(2026, 12, 26)):   # the Saturday inside the window: the holiday wins
        user = _user("blackout-empty@example.com", weekly_touch_goal=14)
        for i in range(3):
            _contact(user=user, name=f"Cold {i}")
        ctx = _cockpit_context(user)
        body = render_to_string("crm/_cockpit.html", ctx)

    assert ctx["blackout"] == "holiday"
    assert ctx["lanes"] == []
    assert ctx["held_total"] == 3
    assert ctx["seeds"] == []
    assert "Only confirmed deadlines show until then." in body
    assert "Confirmed deadlines still show below." not in body
    assert "Done for today." in body
    assert "3 more are waiting for Jan 4." in body
    assert "pacing out" not in body


@pytest.mark.outreach_blackout
def test_an_ordinary_weekday_renders_byte_identical_with_the_blackout_in_place():
    """Degrade: outside the window and Mon-Fri, nothing changed.

    "Before" is the helper answering None, which is the only door the
    blackout has into the page (`_cockpit_context` calls it once, the
    template branches on the key it sets, nothing else reads the calendar).
    "After" is the live helper on a Tuesday in March. Same frozen clock, same
    fixture: the two renders must be the same bytes."""
    from crm import today as today_mod

    tuesday = date(2026, 3, 3)
    assert today_mod.outreach_blackout(tuesday) is None
    with _frozen(tuesday):
        user = _blackout_queue("tuesday")
        after = render_to_string("crm/_cockpit.html", _cockpit_context(user))
        with mock.patch.object(today_mod, "outreach_blackout", return_value=None):
            before = render_to_string("crm/_cockpit.html", _cockpit_context(user))
        ctx = _cockpit_context(user)

    assert after == before
    assert ctx["blackout"] is None
    assert ctx["blackout_resumes"] == ""
    assert "data-blackout" not in after
    assert "Outreach resumes" not in after
    assert "pacing out at" in after, "the cap's own line must still render"
    assert "more to go" in after
    everyone = [a for lane in ctx["lanes"] for a in lane["items"]] + ctx["held"]
    assert everyone and all(a["blackout"] is None for a in everyone)
    assert not any(
        "Bankers are off" in a["reason"] or "weekend" in a["reason"] for a in everyone
    )


def test_a_park_neither_paces_nor_spends_the_firms_budget():
    """A park is a prompt to stop chasing, not an email. With the follow-up
    expiry, 44 parks can land at one bank in a morning; if each charged the
    firm, every real cold note there would be paced behind cards that put
    nothing in anyone's inbox."""
    from crm.today import _pace_by_firm, FIRM_DAILY_CONTACT_CAP
    parks = [_pace_action(i, 9, "park", ev=0.5) for i in range(FIRM_DAILY_CONTACT_CAP + 3)]
    cold = _pace_action(50, 9, "follow_up", ev=2.4)
    _pace_by_firm(parks + [cold], sent_today={}, today=None)
    assert all(not p["firm_paced"] for p in parks)
    assert not cold["firm_paced"], "parks must not have spent the budget the cold note needs"
    assert "better tomorrow" not in cold["reason"]


def test_a_named_affiliation_lifts_a_live_card():
    """The 1.6x for a specific tie has to reach the queue, not just the
    function that computes it. `_gate_and_rank` takes the student's
    affiliations as a tuple (a derived fact, like `sent_today`), never the
    User, and hands them to `rel.expected_value`."""
    from crm.today import _gate_and_rank
    def act(cid, notes):
        return {"contact": {"id": cid, "firm_id": 9, "warmth": "replied", "notes": notes,
                            "school_affiliation": False},
                "action": "follow_up", "ev": 0, "owed_reply": False, "reason": "r",
                "firm_name": "Citi", "class": 2, "priority": 1,
                "last_business_days": 5, "closes_on": None, "relevance": 1.0}
    plain, tied = act(1, "met at a conference"), act(2, "Consulting Club e-board with me")
    _gate_and_rank([plain, tied], tiers={9: 1}, openings={}, sent_today={}, today=None,
                   affiliations=("Consulting Club",))
    assert tied["ev"] > plain["ev"], (tied["ev"], plain["ev"])
    ratio = tied["ev"] / plain["ev"] if plain["ev"] else 0
    assert abs(ratio - 1.6) < 0.05, f"expected the measured 1.6x named-tie lift, got {ratio:.2f}"


# ---------------------------------------------------------------------------
# What the daily brief reads. The sentence at the top of Today and the cards
# under it were two different rankings of the same day.
# ---------------------------------------------------------------------------
def test_the_brief_reads_the_plans_order_not_the_engines():
    """Measured on the demo account, 2026-09-01: the engine's list ran Jane
    Reyes then five Morgan Stanley first-outreach strangers then Grace Huang
    and Nick Tehle, while the plan on the same screen ran Jane Reyes, Nick
    Tehle (ev 13.7), two Apollo advances, Grace Huang. `_actions_for_brief`
    was the engine list, so the brief could lead with a stranger the plan was
    deliberately holding back.

    Pinned on the ORDER rather than on any one name: the plan's own head must
    be the brief's head."""
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="tier-one", name="Tier One")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    # Someone who replied and is owed an answer: the plan's own top rung.
    warm = _contact(user=user, name="Warm Human", firm=firm, warmth="replied",
                    thread_state="replied")
    _touch(user, warm, "reply_received", days_ago=6)
    # Strangers the engine happens to emit ahead of them.
    for i in range(3):
        cold = _contact(user=user, name=f"Cold Stranger {i}", firm=firm, warmth="cold")
        _touch(user, cold, "outreach", days_ago=10)

    ctx = _cockpit_context(user)
    brief_actions = ctx["_actions_for_brief"]
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]

    assert brief_actions[:len(planned)] == planned
    assert brief_actions[0]["contact"]["name"] == "Warm Human"


def test_the_brief_is_told_which_cards_are_actually_todays():
    """P4 applied to the sentence: the list handed over is the plan PLUS the
    queued remainder, so a brief that names eight people on a day the plan
    budgets three has to say which three. `plan_lane` is what
    `assistant.brief._summarize_actions` prints."""
    user = _user(weekly_touch_goal=14)
    # ONE FIRM EACH, deliberately: eight strangers at one bank would be held
    # by the per-firm pace cap, and `firm_paced` rows are dropped from this
    # list rather than marked (see
    # `test_a_firm_paced_card_never_reaches_the_brief`). What this test is
    # about is the cards the DAILY CAP holds, which still reach the brief.
    for i in range(8):
        firm = Firm.objects.create(slug=f"bank-{i}", name=f"Bank {i}")
        UserFirm.all_objects.create(user=user, firm=firm, tier=1)
        c = _contact(user=user, name=f"Cold Stranger {i}", firm=firm, warmth="cold")
        _touch(user, c, "outreach", days_ago=10)

    ctx = _cockpit_context(user)
    lanes = {a["plan_lane"] for a in ctx["_actions_for_brief"]}
    planned_ids = {a["contact"]["id"] for lane in ctx["lanes"] for a in lane["items"]}

    assert lanes == {"today", "up_next"}, "both sides of the cap must be marked"
    for a in ctx["_actions_for_brief"]:
        expected = "today" if a["contact"]["id"] in planned_ids else "up_next"
        assert a["plan_lane"] == expected


def test_a_firm_paced_card_never_reaches_the_brief():
    """`firm_paced` means the page itself says this reads better tomorrow.
    It still renders under "Up next" with its own sentence (P4 is about the
    page, and the page keeps it); what it must not do is be summarised as
    work the student is being asked for today."""
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="one-bank", name="One Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    # Well past FIRM_DAILY_CONTACT_CAP at one firm, so the fourth pass fires.
    for i in range(10):
        c = _contact(user=user, name=f"Banker {i:02d}", firm=firm, warmth="cold")
        _touch(user, c, "outreach", days_ago=10)

    ctx = _cockpit_context(user)
    paced = [a for a in ctx["held"] if a.get("firm_paced")]

    assert paced, "fixture must actually trip the per-firm pace cap"
    brief_ids = {a["contact"]["id"] for a in ctx["_actions_for_brief"]}
    assert not (brief_ids & {a["contact"]["id"] for a in paced})


def test_queue_silenced_contact_ids_names_everyone_the_queue_will_not_speak_about():
    """The second question `assistant.brief._is_stale` asks on a day the
    queue is empty and therefore cannot answer for itself. Parked, quiet,
    archived and snoozed: the same states `_build_actions` and
    `_opening_keep_warms` already drop on."""
    from crm.today import queue_silenced_contact_ids

    user = _user()
    live = _contact(user=user, name="Live One")
    parked = _contact(user=user, name="Parked One", thread_state="parked")
    quiet = _contact(user=user, name="Quiet One", thread_state="quiet")
    archived = _contact(user=user, name="Archived One", archived=True)
    snoozed = _contact(user=user, name="Snoozed One",
                       snoozed_until=timezone.now() + timedelta(days=2))
    # A snooze that has already run out is not a silence.
    expired = _contact(user=user, name="Expired Snooze",
                       snoozed_until=timezone.now() - timedelta(days=2))

    silenced = queue_silenced_contact_ids(user)

    assert silenced == {parked.id, quiet.id, archived.id, snoozed.id}
    assert live.id not in silenced and expired.id not in silenced


# ---------------------------------------------------------------------------
# The cold lane says what its cards ARE.
# ---------------------------------------------------------------------------
def test_a_first_outreach_card_is_not_filed_under_follow_ups():
    """Measured on a five-minute-old account, 2026-09-01: the first contact a
    student ever adds produces one `first_outreach` card, and the lane
    heading over it read "Cold follow-ups", telling somebody who has sent
    nothing to follow up."""
    user = _user(weekly_touch_goal=14)
    _contact(user=user, name="Never Contacted")

    lanes = {lane["key"]: lane for lane in _cockpit_context(user)["lanes"]}

    assert [a["action"] for a in lanes["cold"]["items"]] == ["first_outreach"]
    assert lanes["cold"]["label"] == "First outreach"


def test_the_cold_lane_still_says_follow_ups_when_that_is_what_it_holds():
    """The other side of the same rule, so the fix cannot become "never say
    follow-up"."""
    user = _user(weekly_touch_goal=14)
    c = _contact(user=user, name="Sent Nothing Back")
    _touch(user, c, "outreach", days_ago=10)

    lanes = {lane["key"]: lane for lane in _cockpit_context(user)["lanes"]}

    assert [a["action"] for a in lanes["cold"]["items"]] == ["follow_up"]
    assert lanes["cold"]["label"] == "Cold follow-ups"


def test_a_mixed_cold_lane_claims_neither():
    """Both kinds on screen at once: the heading must not assert either."""
    from crm.today import _lane_label
    items = [{"action": "first_outreach"}, {"action": "follow_up"}]
    assert _lane_label("cold", items, "Cold follow-ups") == "Cold outreach"
    # The other two lanes hold one kind of work each and keep their labels.
    assert _lane_label("critical", items, "Don't lose these") == "Don't lose these"


# ---------------------------------------------------------------------------
# Reschedule records a TIME, or it records nothing.
# ---------------------------------------------------------------------------
def _awaiting_chat(user, name="Youqi Chen"):
    c = _contact(user=user, name=name, warmth="replied",
                 thread_state="chat_scheduled")
    _touch(user, c, "chat_scheduled", days_ago=8)
    return c


def test_reschedule_without_a_time_writes_nothing(client):
    """The button used to post `kind=chat_scheduled` with no date at all. It
    logged a touch stamped at click time, which ratchets warmth to `replied`
    on the strength of the student pressing a button, and reset cadence
    branch 2's clock off the new touch: a five-working-day snooze labelled
    "Record that it moved to a new time". A reschedule with no new time is
    not a reschedule."""
    user = _user(weekly_touch_goal=14)
    c = _awaiting_chat(user)
    before = Touch.all_objects.filter(user=user, contact=c).count()

    client.force_login(user)
    resp = client.post(
        reverse("crm:today_act", args=[c.id, "sent"]), {"kind": "chat_scheduled"}
    )

    assert resp.status_code == 400
    assert Touch.all_objects.filter(user=user, contact=c).count() == before
    assert not CalendarEvent.all_objects.filter(user=user, contact=c).exists()


def test_reschedule_with_a_time_moves_the_chat_that_was_on_the_books(client):
    """The other half of the old defect: a captured invite's `starts_at` was
    left untouched, so the confirm-chat card came back later still asking
    about the superseded time. The row MOVES, and the touch is stamped now
    (a touch records something that happened; the chat is in the future)."""
    user = _user(weekly_touch_goal=14)
    c = _awaiting_chat(user)
    old_start = timezone.now() + timedelta(days=1)
    event = CalendarEvent.all_objects.create(
        user=user, contact=c, kind=CalendarEvent.KIND_CHAT,
        source=CalendarEvent.SOURCE_CAPTURE, title="Coffee chat",
        starts_at=old_start, ends_at=old_start + timedelta(hours=1),
        thread_id="t-1",
    )
    new_local = (timezone.localtime(timezone.now()) + timedelta(days=4)).replace(
        hour=14, minute=30, second=0, microsecond=0
    )

    client.force_login(user)
    resp = client.post(
        reverse("crm:today_act", args=[c.id, "sent"]),
        {"kind": "chat_scheduled",
         "scheduled_at": new_local.strftime("%Y-%m-%dT%H:%M")},
    )

    assert resp.status_code == 200
    event.refresh_from_db()
    assert timezone.localtime(event.starts_at).replace(
        second=0, microsecond=0
    ) == new_local
    assert event.ends_at is None, "the old end belonged to the old start"
    # One row, moved, not a second one beside it.
    assert CalendarEvent.all_objects.filter(user=user, contact=c).count() == 1
    logged = Touch.all_objects.filter(
        user=user, contact=c, kind="chat_scheduled"
    ).order_by("-ts").first()
    assert logged.ts <= timezone.now(), "a touch is never dated into the future"


def test_reschedule_with_no_event_on_file_creates_one_at_the_stated_time(client):
    """Most `chat_scheduled` contacts have no CalendarEvent at all: the
    capture pipeline writes one only for a finding carrying a real .ics
    DTSTART. The student typing a time is the first time the product knows
    one, so it gets recorded rather than dropped."""
    user = _user(weekly_touch_goal=14)
    c = _awaiting_chat(user)
    new_local = (timezone.localtime(timezone.now()) + timedelta(days=3)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )

    client.force_login(user)
    client.post(
        reverse("crm:today_act", args=[c.id, "sent"]),
        {"kind": "chat_scheduled",
         "scheduled_at": new_local.strftime("%Y-%m-%dT%H:%M")},
    )

    row = CalendarEvent.all_objects.get(user=user, contact=c)
    assert row.kind == CalendarEvent.KIND_CHAT
    assert timezone.localtime(row.starts_at).replace(
        second=0, microsecond=0
    ) == new_local


def test_reschedule_refuses_a_time_it_cannot_parse(client):
    """Silence beats a guess (P1): an unparseable value must not fall back to
    "now", which is the dateless write this whole change removed."""
    user = _user(weekly_touch_goal=14)
    c = _awaiting_chat(user)

    client.force_login(user)
    resp = client.post(
        reverse("crm:today_act", args=[c.id, "sent"]),
        {"kind": "chat_scheduled", "scheduled_at": "next tuesday"},
    )

    assert resp.status_code == 400
    assert not CalendarEvent.all_objects.filter(user=user, contact=c).exists()


def test_the_reschedule_control_asks_for_the_time_on_the_card(client):
    """The template half: the card must not offer a one-click reschedule
    again. It carries a datetime input inside a form that posts the kind."""
    user = _user(weekly_touch_goal=14)
    c = _awaiting_chat(user)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()

    assert "Reschedule" in body
    assert 'type="datetime-local"' in body
    assert 'name="scheduled_at"' in body


# ---------------------------------------------------------------------------
# The Gaps strip (`crm.today._gaps`, WS-AI-03).
#
# THE MEASURED DEFECT. On the night of 2026-09-01 the founder's Today page
# recommended chasing eight people he had parked hours earlier, while three
# true and actionable facts sat one query away (`audit-ai-mechanisms.md §3`):
# 25 of his 54 tiered firms have zero contacts, two of them tier 1; both of
# his two advocates are parked, so the engine's `advocate_touch_min_weeks`
# branch cannot fire on either; and whether a role clearing his track,
# region, level and eligibility was closing soon and unsaved had never been
# checked at all.
#
# It MARKS, it does not filter (P4): every row is a ledger line naming its own
# source, and none of them changes what the queue shows.
# ---------------------------------------------------------------------------
def _quiet_user(email):
    """An account with a real network and nothing due: six contacts just
    written to, so the follow-up window is days out. This is the founder's
    own shape and the one `would_be_quiet` is about.

    Six rather than one, deliberately: below `SEED_NETWORK_FLOOR` the page
    shows day-one starter seeds instead, and a page with seeds on it is not
    quiet. See `_starter_seeds`."""
    user = _user(email, weekly_touch_goal=14,
                 cadence_params={"followup_after_business_days": 7})
    for i in range(6):
        c = _contact(user=user, name=f"Just Written To {i}")
        _touch(user, c, "outreach", days_ago=0)
    return user


def test_the_gaps_strip_renders_at_most_three_rows():
    from crm.today import GAPS_MAX

    user = _quiet_user("gaps-three@example.com")
    # Five zero-contact tiered firms and two parked advocates: more sources
    # than the strip has slots.
    for i in range(5):
        firm = Firm.objects.create(slug=f"gap-firm-{i}", name=f"Gap Bank {i}",
                                   regions=["us"])
        UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for i in range(2):
        _contact(user=user, name=f"Advocate {i}", warmth="advocate",
                 thread_state="parked")

    ctx = _cockpit_context(user)

    assert not ctx["lanes"], "precondition: a quiet page"
    assert 0 < len(ctx["gaps"]) <= GAPS_MAX == 3
    kinds = [row["kind"] for row in ctx["gaps"]]
    assert "no_contacts" in kinds and "parked_advocates" in kinds
    # Every row names where it was measured from. A ledger line with no
    # source is the kind of confident, unattributable number P1 forbids.
    assert all(row["source"] for row in ctx["gaps"])

    # And the template actually draws them, with the source attached.
    body = render_to_string("crm/_cockpit.html", ctx)
    for row in ctx["gaps"]:
        assert row["text"] in body
        assert f"Source: {row['source']}." in body


def test_the_gaps_strip_renders_nothing_when_all_three_sources_are_empty():
    """P3: a student with no tiered firms, no advocates and no tracked roles
    gets exactly today's behaviour."""
    user = _quiet_user("gaps-none@example.com")

    ctx = _cockpit_context(user)

    assert not ctx["lanes"], "precondition: a quiet page"
    assert ctx["gaps"] == []
    assert "Source:" not in render_to_string("crm/_cockpit.html", ctx)


def test_the_gaps_strip_names_a_parked_advocate_pair_the_way_the_audit_read_it():
    """The founder's own row: two advocates, both parked. Parking writes
    `thread_state` and never `warmth`, so an advocate stays an advocate while
    the engine's keep-warm branch can no longer fire on them."""
    user = _quiet_user("gaps-advocates@example.com")
    for i in range(2):
        _contact(user=user, name=f"Advocate {i}", warmth="advocate",
                 thread_state="parked")

    row = next(r for r in _cockpit_context(user)["gaps"]
               if r["kind"] == "parked_advocates")

    assert row["text"].startswith("2 advocates, both parked.")


def test_the_gaps_strip_marks_a_reported_deadline_as_reported():
    """96% of dated open campus rows are Coverage's own reading of a
    posting's prose (`directory.views.deadline_provenance`). A bare date on
    this strip would be the page vouching for our regex as the firm's
    decision (P1)."""
    from directory.models import Opportunity

    user = _quiet_user("gaps-reported@example.com")
    user.tracks, user.regions, user.class_year = ["ib"], ["us"], 2028
    user.target_cycles = ["2028 Summer Internship"]
    user.save()
    firm = Firm.objects.create(slug="gap-reported", name="Reported Bank",
                               regions=["us"])
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    Opportunity.objects.create(
        firm=firm, title="2028 Investment Banking Summer Analyst",
        bucket="internship", region="us", status="open",
        deadline=timezone.localdate() + timedelta(days=3),
        # Below `directory.views._CONFIRMED_AT`: read out of the posting's
        # prose, not a field the board published.
        confidence=0.6,
        url="https://reported.example/jobs/1",
    )

    rows = [r for r in _cockpit_context(user)["gaps"] if r.get("role")]

    assert rows, "precondition: a dated live role at a zero-contact tiered firm"
    assert "(reported)" in rows[0]["role"]["when"]


def test_gaps_strip_costs_nothing_on_a_busy_day(monkeypatch):
    """The whole feature sits behind the quiet branch, the same cost
    discipline `_starter_seeds` and `_next_wave` already follow.

    `audit-perf-tests.md §1` measures `_cockpit_context` at 44 queries and 96
    to 149ms; a student with work on the page must pay exactly zero of the
    strip's queries. Asserted two ways, because either alone can pass by
    accident: `_gaps` is never CALLED, and the query count with it stubbed out
    is identical to the count with it live (within 0).
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from crm import today as today_module

    user = _user("gaps-busy@example.com", weekly_touch_goal=14)
    for i in range(6):
        c = _contact(user=user, name=f"Due {i:02d}")
        _touch(user, c, "outreach", days_ago=20)
    # A zero-contact tiered firm and a parked advocate: both sources are
    # LIVE, so the only reason the strip stays empty is the gate.
    firm = Firm.objects.create(slug="busy-gap", name="Busy Gap Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _contact(user=user, name="Parked Advocate", warmth="advocate",
             thread_state="parked")

    with CaptureQueriesContext(connection) as live:
        ctx = _cockpit_context(user)
    assert ctx["lanes"], "precondition: this queue has planned work in it"
    assert ctx["gaps"] == []

    calls = []
    monkeypatch.setattr(
        today_module, "_gaps",
        lambda *a, **k: (calls.append(1), [])[1],
    )
    with CaptureQueriesContext(connection) as stubbed:
        _cockpit_context(user)

    assert calls == [], "_gaps must not even be called on a busy day"
    assert len(live.captured_queries) == len(stubbed.captured_queries)
