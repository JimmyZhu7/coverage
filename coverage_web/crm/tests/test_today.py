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

from datetime import date, timedelta

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
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
# E9. "5 more touchs" — `pluralize` with no argument yields "touchs".
# ---------------------------------------------------------------------------
def test_pace_note_pluralizes_touches_correctly(client):
    user = _user(weekly_touch_goal=14)
    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "touchs" not in body
    # The figure is bolded now, so the sentence is not one contiguous string.
    assert "<b>14</b> more touches to go" in body


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
    """
    user = _user(weekly_touch_goal=14)
    for days in (10, 30, 15):
        c = _contact(user=user, name=f"Silent {days:02d}")
        _touch(user, c, "outreach", days_ago=days)

    ctx = _cockpit_context(user)
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert [a["contact"]["name"] for a in planned] == ["Silent 30", "Silent 15", "Silent 10"]


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
    assert ">Log it<" in body
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
        'aria-label="Log follow up to Ethan Gao"',
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
    _touch(user, c, "outreach", days_ago=40)

    ctx = _cockpit_context(user)
    planned = [a for lane in ctx["lanes"] for a in lane["items"]]
    assert [a["action"] for a in planned] == ["follow_up"]

    client.force_login(user)
    client.post(reverse("crm:today_act", args=[c.id, "sent"]), {"kind": "follow_up"})

    # Age the freshly logged follow-up past every window and look again.
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
    assert "chat set up" in body
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
    """
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    today = timezone.localdate()
    FirmDate.objects.create(firm=firm, cycle="sa2028", region="us",
                            event_kind="insight_deadline",
                            date=today + timedelta(days=2), confidence=1.0)

    ctx = _cockpit_context(user)
    assert [(d["firm"].name, d["when"], d["urgent"]) for d in ctx["deadlines"]] == [
        ("Morgan Stanley", "2d", True)]

    body = _login_and_get(client, user)
    assert "Insight deadline" in body


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


def test_the_eligibility_cell_never_congratulates_an_unchecked_user(client):
    """Zero eligible-unsaved means two different things: "you saved them all"
    and "you never stated a year, so nothing was checked". The day-zero
    walkthrough caught the ribbon telling a ten-minute-old account it was
    "all caught up on your year" when the check had never run. Three states:
    a count, a genuine all-clear, and an honest pointer to Settings."""
    user = _user(weekly_touch_goal=10)
    client.force_login(user)

    user.class_year = None
    user.save(update_fields=["class_year"])
    body = client.get(reverse("crm:week")).content.decode()
    assert "Add your class year" in body
    assert "All caught up on your year" not in body

    user.class_year = 2029
    user.save(update_fields=["class_year"])
    body = client.get(reverse("crm:week")).content.decode()
    # No open roles name 2029 in this fixture, so the all-clear is EARNED.
    assert "All caught up on your year" in body
    assert "Add your class year" not in body


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
    request, and no model call — for the rest of the day."""
    from assistant.models import DailyBrief

    settings.ANTHROPIC_API_KEY = "sk-test-key"
    user = _user("brief-cached@example.com", weekly_touch_goal=14)
    c = _contact(user=user, name="Ada Lovelace")
    _touch(user, c, "outreach", days_ago=20)
    DailyBrief.all_objects.create(
        user=user, date=timezone.localdate(), text="Already written today."
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
