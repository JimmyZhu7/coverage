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
    _workdays_left,
)
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="today@example.com", **kw):
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", **kw
    )


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
    c = Contact.all_objects.create(user=user, name="Ada Lovelace")
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
    c = Contact.all_objects.create(user=user, name="Ethan Gao")
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
    c = Contact.all_objects.create(user=user, name="Old Work")
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
    # Behind on a Friday: the cap climbs to catch up, like Linear capacity.
    assert _daily_cap(14, 0, date(2026, 7, 31)) == 12


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
        c = Contact.all_objects.create(user=user, name=f"Cold {i:02d}")
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
        c = Contact.all_objects.create(user=user, name=f"Coldperson {i:02d}")
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
        c = Contact.all_objects.create(user=user, name=f"Cold {i:02d}")
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
        c = Contact.all_objects.create(user=user, name=f"Cold {i:02d}", firm=citi)
        _touch(user, c, "outreach", days_ago=20)

    warm = Contact.all_objects.create(
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
        c = Contact.all_objects.create(user=user, name=f"Silent {days:02d}")
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
        c = Contact.all_objects.create(user=user, name=f"Cold {i:02d}")
        _touch(user, c, "outreach", days_ago=20)
    warm = Contact.all_objects.create(
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
    c = Contact.all_objects.create(
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
        cold = Contact.all_objects.create(user=user, name=f"Cold {i:02d}", firm=citi)
        _touch(user, cold, "outreach", days_ago=20)

    warm = Contact.all_objects.create(
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
    c = Contact.all_objects.create(
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
    c = Contact.all_objects.create(user=user, name="Ethan Gao")
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
    c = Contact.all_objects.create(user=user, name="Ethan Gao")
    _touch(user, c, "outreach", days_ago=20)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    row = body.split('class="act-card', 1)[1]
    for zone in ("act-ident", "act-context", "act-quick"):
        assert zone in row, zone
    assert row.index("act-ident") < row.index("act-context") < row.index("act-quick")


def test_a_contact_with_no_touches_says_so_rather_than_guessing(client):
    user = _user(weekly_touch_goal=14)
    Contact.all_objects.create(user=user, name="Brand New")
    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "No touches on record" in body


def test_an_audit_row_is_not_shown_as_a_touch(client):
    """The evidence line reads the same real-touch clock the engine does: a
    state correction is not something you did to the relationship."""
    user = _user(weekly_touch_goal=14)
    c = Contact.all_objects.create(user=user, name="Corrected Person")
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
    c = Contact.all_objects.create(user=user, name="Ethan Gao")
    _touch(user, c, "outreach", days_ago=20)

    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert ">Log it<" in body
    assert ">Sent<" not in body
    assert ">They replied<" in body
    assert ">Reply<" not in body


def test_every_quick_action_names_its_contact_for_a_screen_reader(client):
    user = _user(weekly_touch_goal=14)
    c = Contact.all_objects.create(user=user, name="Ethan Gao", email="e@x.com")
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
    c = Contact.all_objects.create(
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
    c = Contact.all_objects.create(user=user, name="No Draft", email="nd@x.com")
    _touch(user, c, "outreach", days_ago=20)
    client.force_login(user)
    body = client.get(reverse("crm:week")).content.decode()
    assert "draft ready" not in body, "the common case earns no badge"
    assert "no draft" not in body, "and no badge for its negation either"

    c.opener = "Hi there, I'm a sophomore at USC..."
    c.save(update_fields=["opener"])
    assert "draft ready" in client.get(reverse("crm:week")).content.decode()


def test_the_firm_slot_only_says_alum_for_an_actual_alum(client):
    """A hand-added contact has no firm_id whether the free text says "USC" or
    "HSBC". Keying the chip on a missing firm_id labelled eight HSBC bankers
    alumni on the live page."""
    user = _user(weekly_touch_goal=14)
    alum = Contact.all_objects.create(
        user=user, name="Kristin Welty", firm_text="USC", school_affiliation=True,
    )
    banker = Contact.all_objects.create(
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
    c = Contact.all_objects.create(user=user, name="Snoozed Cold")
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
    c = Contact.all_objects.create(
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
    c = Contact.all_objects.create(
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
    c = Contact.all_objects.create(
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
    c = Contact.all_objects.create(
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
    c = Contact.all_objects.create(
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
    # own "Manual override" line — this checks the QUEUE, not the whole page.
    assert c.archived is False
    ctx = _cockpit_context(user)
    queued_names = [a["contact"]["name"] for lane in ctx["lanes"] for a in lane["items"]]
    assert "Shelby Dibs" not in queued_names


def test_the_gone_quiet_lane_does_not_get_a_second_park_button(client):
    """`a.action == "park"` already renders Park it as the PRIMARY button.
    The new ghost version is gated on `a.action != "park"` specifically so
    that card doesn't show the same verb twice."""
    user = _user(weekly_touch_goal=14)
    c = Contact.all_objects.create(
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
    c = Contact.all_objects.create(user=user, name="Ethan Gao", email="e@x.com")
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
    c = Contact.all_objects.create(user=user, name="Ethan Gao")
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
        c = Contact.all_objects.create(user=user, name=f"Quiet {i:02d}")
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
        c = Contact.all_objects.create(user=user, name=f"Quiet {i:02d}")
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
        c = Contact.all_objects.create(user=user, name=f"Quiet {i:02d}")
        _touch(user, c, "outreach", days_ago=40)
        _touch(user, c, "follow_up", days_ago=30)
    assert _cockpit_context(user)["park_bulk"] is False
    client.force_login(user)
    assert "Park all" not in client.get(reverse("crm:week")).content.decode()


def test_bulk_park_is_tenant_scoped(client):
    a = _user("a@example.com")
    b = _user("b@example.com")
    theirs = Contact.all_objects.create(user=b, name="Not Yours")
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
        c = Contact.all_objects.create(user=user, name=f"Cold {i:02d}")
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
        c = Contact.all_objects.create(user=user, name=f"Quiet {i:02d}")
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
    c = Contact.all_objects.create(
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
    c = Contact.all_objects.create(
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
    c = Contact.all_objects.create(
        user=user, name="Stale Chat", warmth="replied", thread_state="chat_scheduled",
    )
    _touch(user, c, "chat_scheduled", days_ago=21)

    ctx = _cockpit_context(user)
    assert ctx["schedule"] == []
    actions = [a for lane in ctx["lanes"] for a in lane["items"]]
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
    c = Contact.all_objects.create(user=user, name="Ethan Gao")
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
# The renovation: deadlines by name, the silent bucket, and chat prep.
# ---------------------------------------------------------------------------
def test_deadlines_are_named_not_just_counted(client):
    """The ribbon counts these. A count creates a click; a name creates an
    action you can take this morning."""
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    today = timezone.localdate()
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="us",
                            event_kind="insight_deadline",
                            date=today + timedelta(days=2), confidence=1.0)

    ctx = _cockpit_context(user)
    assert [d["firm"].name for d in ctx["deadlines"]] == ["Morgan Stanley"]
    assert ctx["deadlines"][0]["when"] == "2d"
    assert ctx["deadlines"][0]["urgent"] is True

    body = _login_and_get(client, user)
    assert "insight deadline" in body


def test_an_unconfirmed_date_never_reaches_the_rail():
    """Same bar the cadence engine acts on. A countdown built on a rumour is
    worse than no countdown."""
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="hk",
                            event_kind="app_close",
                            date=timezone.localdate() + timedelta(days=3),
                            confidence=0.3)
    assert _cockpit_context(user)["deadlines"] == []


def test_a_past_deadline_is_not_upcoming():
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="us",
                            event_kind="app_close",
                            date=timezone.localdate() - timedelta(days=1),
                            confidence=1.0)
    assert _cockpit_context(user)["deadlines"] == []


def test_waiting_on_reply_names_people_the_queue_is_silent_about(client):
    """The gap between "you sent it" and "the follow-up is due", where the
    cadence engine correctly says nothing and the contact vanished entirely."""
    user = _user(weekly_touch_goal=14)
    c = Contact.all_objects.create(user=user, name="Recently Emailed")
    _touch(user, c, "outreach", days_ago=1)

    ctx = _cockpit_context(user)
    assert [p.name for p in ctx["waiting"]["people"]] == ["Recently Emailed"]
    assert ctx["waiting"]["total"] == 1

    body = _login_and_get(client, user)
    assert "Waiting on reply" in body
    assert "Recently Emailed" in body


def test_waiting_never_repeats_somebody_already_on_the_page():
    """A contact with a card six inches up must not also appear in the quiet
    bucket — the page would be asking and reassuring about one person."""
    user = _user(weekly_touch_goal=14)
    due = Contact.all_objects.create(user=user, name="Due For Followup")
    _touch(user, due, "outreach", days_ago=20)

    ctx = _cockpit_context(user)
    queued = {a["contact"]["id"] for lane in ctx["lanes"] for a in lane["items"]}
    assert due.id in queued, "precondition: this contact is in the queue"
    assert [p.id for p in ctx["waiting"]["people"]] == []


def test_waiting_ignores_people_who_never_got_a_first_note():
    """`no_reply` is also the default for a contact nobody has written to.
    "Waiting on reply" from someone you never emailed is a lie."""
    user = _user(weekly_touch_goal=14)
    Contact.all_objects.create(user=user, name="Never Contacted")
    assert _cockpit_context(user)["waiting"]["total"] == 0


def test_waiting_counts_the_overflow_rather_than_truncating_silently():
    user = _user(weekly_touch_goal=14)
    for i in range(15):
        c = Contact.all_objects.create(user=user, name=f"Sent {i:02d}")
        _touch(user, c, "outreach", days_ago=1)

    waiting = _cockpit_context(user)["waiting"]
    assert waiting["total"] == 15
    assert len(waiting["people"]) == 12
    assert waiting["more"] == 3


def test_a_chat_today_gets_a_prep_card_with_what_you_learned_last_time(client):
    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    c = Contact.all_objects.create(user=user, name="Ada Lovelace", firm=firm,
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
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="us",
                            event_kind="app_close",
                            date=timezone.localdate() + timedelta(days=9),
                            confidence=1.0)

    ctx = _cockpit_context(user)
    assert len(ctx["chat_prep"]) == 1
    prep = ctx["chat_prep"][0]
    assert prep["contact"].name == "Ada Lovelace"
    assert "TMT desk" in prep["learned"]
    assert prep["firm_date_days"] == 9
    assert prep["firm_date_label"] == "applications close"

    body = _login_and_get(client, user)
    assert "Chat today" in body
    assert "TMT desk" in body


def test_prep_only_covers_today_and_only_timed_chats():
    user = _user(weekly_touch_goal=14)
    c = Contact.all_objects.create(user=user, name="Tomorrow Person")
    tomorrow = timezone.localtime(timezone.now()).replace(
        hour=15, minute=0, second=0, microsecond=0) + timedelta(days=1)
    CalendarEvent.all_objects.create(
        user=user, contact=c, title="Chat with Tomorrow Person",
        starts_at=tomorrow, kind="chat", thread_id="t-1")
    allday = Contact.all_objects.create(user=user, name="Allday Person")
    CalendarEvent.all_objects.create(
        user=user, contact=allday, title="Superday",
        starts_at=timezone.localtime(timezone.now()).replace(hour=0, minute=0),
        all_day=True, kind="event", thread_id="t-2")

    ctx = _cockpit_context(user)
    assert ctx["chat_prep"] == [], "prep is for a conversation happening today"
    assert len(ctx["schedule"]) == 2, "both still appear on the schedule"


def test_a_dismissed_debrief_is_not_used_as_prep_material():
    user = _user(weekly_touch_goal=14)
    c = Contact.all_objects.create(user=user, name="Ada Lovelace")
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
        c = Contact.all_objects.create(user=user, name=f"{name} Person")
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
    c = Contact.all_objects.create(user=user, name="Early Bird")
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
    c = Contact.all_objects.create(user=user, name="All Day")
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
    c = Contact.all_objects.create(user=user, name="Far Out")
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
        c = Contact.all_objects.create(user=user, name=f"Busy {i}")
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
    c = Contact.all_objects.create(
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


# ---------------------------------------------------------------------------
# "New at your firms" must not let two genuinely different postings read as
# one exact duplicate.
# ---------------------------------------------------------------------------
def test_new_at_firms_carries_location_so_same_title_rows_are_told_apart():
    """Reproduces the live bug: Goldman Sachs posted the same campus title
    ("Internal Audit — Summer Analyst") in both London and Birmingham on the
    same day. Both are real, distinct rows with distinct URLs, but the widget
    built its row dict from `o.title`/`o.firm.name` only, so the two entries
    rendered byte-for-byte identical text with nothing to tell them apart.
    `o.location` was sitting right there on the queryset, unused.
    """
    from directory.models import Opportunity

    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    UserFirm.all_objects.create(user=user, firm=firm)

    # An older posting so GS is not treated as a board debut (a firm whose
    # OLDEST row is itself inside the 7-day window gets excluded entirely —
    # see `_new_at_your_firms`'s debut filter).
    anchor = Opportunity.objects.create(
        firm=firm, title="Older Anchor Role", bucket="internship",
        status="open", location="New York",
        url="https://gs.com/careers/anchor")
    Opportunity.objects.filter(pk=anchor.pk).update(
        first_seen=timezone.now() - timedelta(days=30))

    Opportunity.objects.create(
        firm=firm, title="Internal Audit — Summer Analyst", bucket="internship",
        status="open", location="London",
        url="https://gs.com/careers/181882_gs_campus")
    Opportunity.objects.create(
        firm=firm, title="Internal Audit — Summer Analyst", bucket="internship",
        status="open", location="Birmingham",
        url="https://gs.com/careers/181880_gs_campus")

    roles = _cockpit_context(user)["new_at_firms"]["roles"]
    same_title = [r for r in roles if r["title"] == "Internal Audit — Summer Analyst"]
    assert len(same_title) == 2, "both distinct postings must reach the widget"
    assert {r["location"] for r in same_title} == {"London", "Birmingham"}, (
        "the two rows must carry their real, DIFFERENT locations, not go "
        "unlabeled and read as one duplicate"
    )


def test_new_at_firms_widget_renders_both_locations_in_the_page(client):
    """Same fixture, through the actual template: `_cockpit.html` must print
    both cities, not just carry them in the context dict."""
    from directory.models import Opportunity

    user = _user(weekly_touch_goal=14)
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    UserFirm.all_objects.create(user=user, firm=firm)

    anchor = Opportunity.objects.create(
        firm=firm, title="Older Anchor Role", bucket="internship",
        status="open", location="New York",
        url="https://gs.com/careers/anchor")
    Opportunity.objects.filter(pk=anchor.pk).update(
        first_seen=timezone.now() - timedelta(days=30))

    Opportunity.objects.create(
        firm=firm, title="Internal Audit — Summer Analyst", bucket="internship",
        status="open", location="London",
        url="https://gs.com/careers/181882_gs_campus")
    Opportunity.objects.create(
        firm=firm, title="Internal Audit — Summer Analyst", bucket="internship",
        status="open", location="Birmingham",
        url="https://gs.com/careers/181880_gs_campus")

    body = _login_and_get(client, user)
    assert "London" in body
    assert "Birmingham" in body
