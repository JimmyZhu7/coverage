"""Regression tests for coverage_domain.cadence.

Ported from campaign/tests/test_cadence.py (semantics, not style — pytest
here; the original was plain stdlib asserts run by a manual runner, and it
built a throwaway CAMPAIGN_HOME + temp sqlite DB per test). This port needs
neither: the cadence engine is now a PURE function over plain data, so each
test just builds lists of contact / touch / firm_date dicts and an as-of
datetime and asserts on the returned actions — no DB, no filesystem, no clock.

The five ported cases keep their original intent verbatim:
  - a chat_done contact re-enters the cadence (maintain) once thanked,
    instead of dropping out forever
  - a second chat after an earlier thank-you prompts a NEW thank-you
  - a firm's closing-soon re-ping is region-scoped (an HK app_close must not
    re-ping a US contact at the same firm, and vice versa)
  - an unknown-region contact keeps the both-regions fallback
  - a brand-new zero-touch contact is queued for first outreach, not treated
    as a stale thread

Additional cases cover the remaining branches of the 7-branch tree
(confirm_chat / follow_up / park / advance) and the backward planner
(tasks_from_change confirmed-only guard + per-event lead times;
plan_task_write's <=3-day in-place-update rule).
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from coverage_domain import cadence

UTC = timezone.utc
TODAY = date(2026, 7, 22)
AS_OF = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)

FIRMS = {
    "usfirm": {"name": "US Firm", "tier": 1},
    "hkfirm": {"name": "HK Firm", "tier": 1},
    "dualfirm": {"name": "Dual Firm", "tier": 1},
}


def contact(cid, **kw):
    base = dict(id=cid, firm_id="usfirm", warmth="cold", thread_state="no_reply", source=None)
    base.update(kw)
    return base


def touch(cid, kind, ts, **kw):
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=UTC) if isinstance(ts, str) else ts
    base = dict(contact_id=cid, kind=kind, ts=dt, channel="email", note=None)
    base.update(kw)
    return base


def kinds_for(actions, cid):
    return {a["action"] for a in actions if a["contact"]["id"] == cid}


# --------------------------------------------------------------------------
# Ported regression cases.
# --------------------------------------------------------------------------
def test_chat_done_reenters_after_thank_you():
    """Once a thank-you is logged for the latest chat, an advocate must fall
    through to the maintain cadence, not be dropped forever."""
    c = contact(1, warmth="advocate", thread_state="chat_done")
    touches = [
        touch(1, "chat", "2026-06-01 10:00"),
        touch(1, "thank_you", "2026-06-01 12:00"),
    ]
    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
    k = kinds_for(actions, 1)
    assert "thank_you" not in k, f"thank-you already sent — must not re-fire, got {k}"
    assert "maintain" in k, f"advocate with a thanked chat must still get maintain, got {k}"


def test_second_chat_prompts_new_thank_you():
    """A second chat after an earlier thank-you prompts a new thank-you."""
    c = contact(1, warmth="chatted", thread_state="chat_done")
    touches = [
        touch(1, "chat", "2026-06-01 10:00"),
        touch(1, "thank_you", "2026-06-01 12:00"),
        touch(1, "chat", "2026-07-20 10:00"),
    ]
    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
    assert "thank_you" in kinds_for(actions, 1)


def test_thank_you_prompt_expires_after_a_week():
    """A chat older than `thank_you_expires_after_days` must NOT prompt a
    thank-you: the note has missed its moment, and nagging produces a wall of
    stale prompts the moment any historical data is imported. The contact falls
    through to the rest of the cadence rather than dropping out."""
    c = contact(1, warmth="advocate", thread_state="chat_done")
    touches = [touch(1, "chat", "2026-06-01 10:00")]  # ~51 days before AS_OF
    k = kinds_for(cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1)
    assert "thank_you" not in k, f"chat is 51 days old — must not prompt, got {k}"
    assert "maintain" in k, f"must fall through to the maintain cadence, got {k}"


def test_thank_you_still_prompts_inside_the_window():
    """The boundary the test above pins from the other side: a chat two days
    old is overdue (>24h) but well inside the expiry, so it still prompts."""
    c = contact(1, warmth="chatted", thread_state="chat_done")
    touches = [touch(1, "chat", "2026-07-20 09:00")]  # 2 days before AS_OF
    assert "thank_you" in kinds_for(
        cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1
    )


def test_thank_you_expiry_is_tunable():
    """The window is a parameter, not a constant — a caller can restore the
    original never-expires behaviour."""
    c = contact(1, warmth="chatted", thread_state="chat_done")
    touches = [touch(1, "chat", "2026-06-01 10:00")]
    actions = cadence.due_actions(
        [c], touches, [], as_of=AS_OF, firms=FIRMS,
        params={"thank_you_expires_after_days": 3650},
    )
    assert "thank_you" in kinds_for(actions, 1)


def test_closing_soon_reping_is_region_filtered():
    """An HK app_close must not re-ping a US contact at the same firm."""
    close_date = TODAY + timedelta(days=5)
    firm_dates = [{
        "firm_id": "dualfirm", "event_kind": "app_close", "region": "hk",
        "date": close_date, "confidence": "confirmed_official",
    }]
    us = contact(1, name="US Contact", firm_id="dualfirm", warmth="chatted",
                 thread_state="replied", source="Apollo direct search")
    hk = contact(2, name="HK Contact", firm_id="dualfirm", warmth="chatted",
                 thread_state="replied", source="Apollo HK campaign")
    touches = [touch(1, "chat", "2026-06-01 10:00"), touch(2, "chat", "2026-06-01 10:00")]
    actions = cadence.due_actions([us, hk], touches, firm_dates, as_of=AS_OF, firms=FIRMS)
    assert "reping" not in kinds_for(actions, 1), "US contact must not re-ping off an HK close"
    assert "reping" in kinds_for(actions, 2), "HK contact should re-ping off the HK close"


def test_closing_soon_reping_applies_both_regions_when_source_unknown():
    """A contact whose region can't be inferred keeps the both-regions
    fallback (matches the soonest close date across any region)."""
    close_date = TODAY + timedelta(days=5)
    firm_dates = [{
        "firm_id": "dualfirm", "event_kind": "app_close", "region": "hk",
        "date": close_date, "confidence": "confirmed_official",
    }]
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied", source=None)
    actions = cadence.due_actions([c], [touch(1, "chat", "2026-06-01 10:00")],
                                  firm_dates, as_of=AS_OF, firms=FIRMS)
    assert "reping" in kinds_for(actions, 1)


# --------------------------------------------------------------------------
# Explicit contact region (the `source`-substring bug).
# --------------------------------------------------------------------------
def _hk_close_firm_dates():
    return [{
        "firm_id": "dualfirm", "event_kind": "app_close", "region": "hk",
        "date": TODAY + timedelta(days=5), "confidence": "confirmed_official",
    }]


def test_explicit_region_beats_source_inference():
    """An explicit `region` wins over whatever `infer_region` would have made
    of `source`. Here source says "direct search" (-> 'us' by inference), but
    the contact is marked HK, so the HK close re-pings them."""
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                source="Apollo direct search", region="hk")
    actions = cadence.due_actions([c], [touch(1, "chat", "2026-06-01 10:00")],
                                  _hk_close_firm_dates(), as_of=AS_OF, firms=FIRMS)
    assert "reping" in kinds_for(actions, 1)


def test_explicit_region_excludes_other_regions_close():
    """The other direction: source mentions HK (-> 'hk' by inference) but the
    contact is explicitly US, so the HK close must not touch them."""
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                source="Apollo HK campaign", region="us")
    actions = cadence.due_actions([c], [touch(1, "chat", "2026-06-01 10:00")],
                                  _hk_close_firm_dates(), as_of=AS_OF, firms=FIRMS)
    assert "reping" not in kinds_for(actions, 1)


def test_hand_added_contact_is_not_guessed_us_from_source():
    """The bug this field exists for: a hand-added contact is written with
    source="manual", which `infer_region` reads as 'us' — so an HK contact was
    skipped for HK deadlines forever. With the region set, the HK close fires;
    without it, the legacy inference still (wrongly) says US."""
    hk = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                 source="manual", region="hk")
    legacy = contact(2, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                     source="manual", region="")
    touches = [touch(1, "chat", "2026-06-01 10:00"), touch(2, "chat", "2026-06-01 10:00")]
    actions = cadence.due_actions([hk, legacy], touches, _hk_close_firm_dates(),
                                  as_of=AS_OF, firms=FIRMS)
    assert "reping" in kinds_for(actions, 1)
    assert "reping" not in kinds_for(actions, 2)


def test_blank_region_still_falls_back_to_source():
    """Legacy rows keep their exact previous meaning: a blank region falls
    through to `infer_region(source)`, so a row whose source says HK still
    behaves as it did before the column existed."""
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                source="Apollo HK campaign", region="")
    actions = cadence.due_actions([c], [touch(1, "chat", "2026-06-01 10:00")],
                                  _hk_close_firm_dates(), as_of=AS_OF, firms=FIRMS)
    assert "reping" in kinds_for(actions, 1)


def test_contact_region_helper_resolution_order():
    assert cadence.contact_region({"region": "hk", "source": "manual"}) == "hk"
    assert cadence.contact_region({"region": " HK ", "source": "manual"}) == "hk"
    assert cadence.contact_region({"region": "", "source": "Apollo HK"}) == "hk"
    assert cadence.contact_region({"region": "", "source": "manual"}) == "us"
    assert cadence.contact_region({"region": "", "source": ""}) is None
    assert cadence.contact_region({}) is None


def test_zero_touch_contact_not_treated_as_stale():
    """A brand-new, never-touched contact is flagged for first outreach."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    actions = cadence.due_actions([c], [], [], as_of=AS_OF, firms=FIRMS)
    my = [a["action"] for a in actions if a["contact"]["id"] == 1]
    assert my == ["first_outreach"], my


# --------------------------------------------------------------------------
# Remaining branches of the decision tree.
# --------------------------------------------------------------------------
def test_reping_suppressed_when_already_reped_in_window():
    """Branch 3 fires at most once per window: a reping already sent inside
    [close - reping_days, close] suppresses a new one."""
    close_date = TODAY + timedelta(days=5)
    firm_dates = [{
        "firm_id": "dualfirm", "event_kind": "app_close", "region": "hk",
        "date": close_date, "confidence": "confirmed_official",
    }]
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                source="Apollo HK campaign")
    touches = [touch(1, "chat", "2026-06-01 10:00"), touch(1, "reping", "2026-07-18 10:00")]
    actions = cadence.due_actions([c], touches, firm_dates, as_of=AS_OF, firms=FIRMS)
    assert "reping" not in kinds_for(actions, 1)


def test_reping_ignores_unconfirmed_close_date():
    """Only confirmed_official app_close dates drive a re-ping (a reported /
    rumor date must not)."""
    close_date = TODAY + timedelta(days=5)
    firm_dates = [{
        "firm_id": "dualfirm", "event_kind": "app_close", "region": "hk",
        "date": close_date, "confidence": "reported",
    }]
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                source="Apollo HK campaign")
    actions = cadence.due_actions([c], [touch(1, "chat", "2026-06-01 10:00")],
                                  firm_dates, as_of=AS_OF, firms=FIRMS)
    assert "reping" not in kinds_for(actions, 1)


def test_confirm_chat_when_scheduled_and_stale():
    c = contact(1, warmth="replied", thread_state="chat_scheduled")
    actions = cadence.due_actions([c], [touch(1, "chat_scheduled", "2026-07-08 10:00")],
                                  [], as_of=AS_OF, firms=FIRMS)
    assert "confirm_chat" in kinds_for(actions, 1)


def test_scheduled_but_fresh_gets_no_action():
    """chat_scheduled within 4 business days -> no action yet (still on the
    calendar), and the contact still short-circuits (branch 2 always continues)."""
    c = contact(1, warmth="replied", thread_state="chat_scheduled")
    actions = cadence.due_actions([c], [touch(1, "chat_scheduled", "2026-07-21 10:00")],
                                  [], as_of=AS_OF, firms=FIRMS)
    assert kinds_for(actions, 1) == set()


def test_follow_up_when_one_cold_touch_and_stale():
    c = contact(1, warmth="cold", thread_state="no_reply")
    actions = cadence.due_actions([c], [touch(1, "outreach", "2026-07-10 10:00")],
                                  [], as_of=AS_OF, firms=FIRMS)
    assert "follow_up" in kinds_for(actions, 1)


def test_park_after_max_cold_touches_and_long_silence():
    c = contact(1, warmth="cold", thread_state="no_reply")
    touches = [touch(1, "outreach", "2026-06-15 10:00"), touch(1, "follow_up", "2026-06-22 10:00")]
    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
    my = [a for a in actions if a["contact"]["id"] == 1]
    assert [a["action"] for a in my] == ["park"]
    assert my[0]["priority"] == 3


def test_advance_when_replied_and_idle():
    c = contact(1, warmth="replied", thread_state="replied")
    actions = cadence.due_actions([c], [touch(1, "reply_received", "2026-07-15 10:00")],
                                  [], as_of=AS_OF, firms=FIRMS)
    assert "advance" in kinds_for(actions, 1)


def test_parked_contact_is_skipped():
    c = contact(1, warmth="replied", thread_state="parked")
    actions = cadence.due_actions([c], [touch(1, "reply_received", "2026-05-01 10:00")],
                                  [], as_of=AS_OF, firms=FIRMS)
    assert kinds_for(actions, 1) == set()


def test_archived_contact_is_skipped():
    c = contact(1, warmth="cold", thread_state="no_reply", archived=True)
    actions = cadence.due_actions([c], [], [], as_of=AS_OF, firms=FIRMS)
    assert actions == []


def test_actions_sorted_by_priority_then_tier():
    """Overdue thank-you (priority 0) ranks above a follow_up (priority 1)."""
    overdue = contact(1, warmth="chatted", thread_state="chat_done", firm_id="usfirm")
    followup = contact(2, warmth="cold", thread_state="no_reply", firm_id="hkfirm")
    touches = [
        # Overdue (>24h) but still inside the 7-day expiry window, so the
        # prompt is live. Past that window branch 1 falls through entirely —
        # see test_thank_you_prompt_expires_after_a_week.
        touch(1, "chat", "2026-07-19 10:00"),        # chat_done, never thanked -> overdue thank_you
        touch(2, "outreach", "2026-07-10 10:00"),    # -> follow_up
    ]
    actions = cadence.due_actions([followup, overdue], touches, [], as_of=AS_OF, firms=FIRMS)
    assert actions[0]["action"] == "thank_you"
    assert actions[0]["priority"] == 0
    assert actions[0]["priority"] <= actions[-1]["priority"]


def test_string_timestamps_are_accepted():
    """Touch ts may arrive as an ISO string (the original stored them as
    'YYYY-MM-DD HH:MM' text) or as a real datetime — both must work."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    actions = cadence.due_actions(
        [c], [{"contact_id": 1, "kind": "outreach", "ts": "2026-07-10 10:00"}],
        [], as_of=AS_OF, firms=FIRMS,
    )
    assert "follow_up" in kinds_for(actions, 1)


def test_params_override_changes_followup_window():
    """A per-call params override tightens the follow-up window."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    touches = [touch(1, "outreach", "2026-07-20 10:00")]  # ~2 business days ago
    default = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
    assert "follow_up" not in kinds_for(default, 1)
    tuned = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS,
                                params={"followup_after_business_days": 1})
    assert "follow_up" in kinds_for(tuned, 1)


# --------------------------------------------------------------------------
# Staged follow-up windows (branch 6). AS_OF is Wed 2026-07-22, so:
#   last touch Mon 2026-07-13 -> 7 business days ago
#   last touch Fri 2026-07-10 -> 8 business days ago
# 7 sits BETWEEN the two defaults (first 6, second 8), which is what makes it
# the discriminating case: it is due under the first window and not yet due
# under the second.
# --------------------------------------------------------------------------
# max_cold has to be raised for a SECOND follow-up to exist at all: branch 6
# only offers a follow_up while `outbound < max_cold`, and the default of 2
# means one outreach plus one follow-up, then park.
_THREE_TOUCHES = {"max_cold_touches": 3}


def test_first_and_second_followup_use_different_windows():
    """The whole point of staging: at an identical 7-business-day silence, a
    contact owed follow-up #1 is due and one owed #2 is not."""
    c = contact(1, warmth="cold", thread_state="no_reply")

    one_outbound = [touch(1, "outreach", "2026-07-13 10:00")]
    two_outbound = [
        touch(1, "outreach", "2026-06-25 10:00"),
        touch(1, "follow_up", "2026-07-13 10:00"),
    ]

    first = cadence.due_actions([c], one_outbound, [], as_of=AS_OF, firms=FIRMS,
                                params=_THREE_TOUCHES)
    second = cadence.due_actions([c], two_outbound, [], as_of=AS_OF, firms=FIRMS,
                                 params=_THREE_TOUCHES)

    assert "follow_up" in kinds_for(first, 1)
    assert "follow_up" not in kinds_for(second, 1)


def test_second_followup_fires_once_its_own_window_elapses():
    """One more business day of silence (8) and follow-up #2 comes due."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    touches = [
        touch(1, "outreach", "2026-06-25 10:00"),
        touch(1, "follow_up", "2026-07-10 10:00"),   # 8 business days before AS_OF
    ]
    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS,
                                  params=_THREE_TOUCHES)
    my = [a for a in actions if a["contact"]["id"] == 1]
    assert [a["action"] for a in my] == ["follow_up"]
    # The action reports WHICH window it was measured against, so the UI never
    # has to guess which stage of the cadence a prompt came from.
    assert my[0]["ctx"]["window_business_days"] == 8
    assert my[0]["ctx"]["outbound"] == 2


def test_second_window_is_tunable_independently_of_the_first():
    """Widening only the second window parks the prompt without touching #1."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    two_outbound = [
        touch(1, "outreach", "2026-06-25 10:00"),
        touch(1, "follow_up", "2026-07-10 10:00"),   # 8 business days ago
    ]
    one_outbound = [touch(1, "outreach", "2026-07-10 10:00")]

    widened = {**_THREE_TOUCHES, "second_followup_after_business_days": 20}
    assert "follow_up" not in kinds_for(
        cadence.due_actions([c], two_outbound, [], as_of=AS_OF, firms=FIRMS, params=widened), 1
    )
    # ...while follow-up #1 at the same silence is untouched by that override.
    assert "follow_up" in kinds_for(
        cadence.due_actions([c], one_outbound, [], as_of=AS_OF, firms=FIRMS, params=widened), 1
    )


def test_third_and_later_followups_reuse_the_second_window():
    """The second window governs #2 and everything after it — there is no
    third knob, so a 3-outbound contact is measured against the same gap."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    touches = [
        touch(1, "outreach", "2026-06-01 10:00"),
        touch(1, "follow_up", "2026-06-15 10:00"),
        touch(1, "follow_up", "2026-07-10 10:00"),   # 8 business days ago
    ]
    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS,
                                  params={"max_cold_touches": 4})
    my = [a for a in actions if a["contact"]["id"] == 1]
    assert [a["action"] for a in my] == ["follow_up"]
    assert my[0]["ctx"]["window_business_days"] == 8


def test_second_window_does_not_resurrect_a_maxed_out_contact():
    """A contact already at max_cold_touches must stay silent when the second
    window elapses. The follow_up branch is gated on `outbound < max_cold`, so
    at 8 business days (second window met, park's 10 not yet) NOTHING is due —
    staging must not smuggle an extra touch past the max."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    touches = [
        touch(1, "outreach", "2026-06-15 10:00"),
        touch(1, "follow_up", "2026-07-10 10:00"),   # 8 bd: >= second window
    ]
    # Default max_cold_touches of 2 is already reached by these two touches.
    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
    assert kinds_for(actions, 1) == set()


def test_park_still_fires_once_its_own_window_elapses():
    """Park is unchanged by staging: at max_cold touches and 10 business days
    of silence it still takes the contact, at priority 3."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    touches = [
        touch(1, "outreach", "2026-06-15 10:00"),
        touch(1, "follow_up", "2026-07-08 10:00"),   # 10 bd: park's window
    ]
    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
    my = [a for a in actions if a["contact"]["id"] == 1]
    assert [a["action"] for a in my] == ["park"]
    assert my[0]["priority"] == 3


def test_staged_defaults_both_clear_a_full_calendar_week():
    """The defaults exist to put every follow-up BEYOND one week. 5 business
    days is exactly 7 calendar days, so both windows must exceed 5 — this
    pins the intent against a well-meaning future tweak back to 5."""
    first = cadence.CADENCE_DEFAULTS["followup_after_business_days"]
    second = cadence.CADENCE_DEFAULTS["second_followup_after_business_days"]
    assert first > 5
    assert second > 5
    # Staged, not merely separate: the second gap is the longer of the two.
    assert second > first
    # And the arithmetic those defaults rest on, asserted rather than trusted:
    # a Monday touch + 6 business days is the following Tuesday, 8 days later.
    assert cadence.business_days_since(date(2026, 7, 13), date(2026, 7, 21)) == 6
    assert (date(2026, 7, 21) - date(2026, 7, 13)).days == 8


# --------------------------------------------------------------------------
# Backward planner: tasks_from_change.
# --------------------------------------------------------------------------
def _change(**kw):
    base = dict(kind="new_date", confidence="confirmed_official",
                key="usfirm/sa2028/app_open", new="2026-09-01", firm_id="usfirm", region="us")
    base.update(kw)
    return base


def test_task_fires_on_confirmed_app_open():
    tasks = cadence.tasks_from_change(_change(), today=TODAY, firms=FIRMS)
    assert [t["kind"] for t in tasks] == ["advocate_target"]
    assert tasks[0]["due"] == (date(2026, 9, 1) - timedelta(days=14)).isoformat()
    assert "US Firm" in tasks[0]["title"]


def test_no_task_for_unconfirmed_change():
    assert cadence.tasks_from_change(_change(confidence="rumor"), today=TODAY, firms=FIRMS) == []
    assert cadence.tasks_from_change(_change(confidence="reported"), today=TODAY, firms=FIRMS) == []


def test_no_task_for_past_date():
    assert cadence.tasks_from_change(_change(new="2026-07-01"), today=TODAY, firms=FIRMS) == []


def test_no_task_for_non_date_change_kind():
    assert cadence.tasks_from_change(_change(kind="note"), today=TODAY, firms=FIRMS) == []


def test_app_close_spawns_reping_and_submit():
    ch = _change(key="usfirm/sa2028/app_close", new="2026-10-01")
    tasks = cadence.tasks_from_change(ch, today=TODAY, firms=FIRMS)
    assert [t["kind"] for t in tasks] == ["reping", "submit"]
    assert tasks[0]["due"] == (date(2026, 10, 1) - timedelta(days=14)).isoformat()
    assert tasks[1]["due"] == (date(2026, 10, 1) - timedelta(days=5)).isoformat()


def test_insight_deadline_spawns_insight_app():
    ch = _change(key="usfirm/sa2028/insight_deadline", new="2026-08-20")
    tasks = cadence.tasks_from_change(ch, today=TODAY, firms=FIRMS)
    assert [t["kind"] for t in tasks] == ["insight_app"]
    assert tasks[0]["due"] == (date(2026, 8, 20) - timedelta(days=7)).isoformat()


def test_hk_region_tags_task_title():
    ch = _change(key="hkfirm/sa2028_hk/app_open", new="2026-09-01", firm_id="hkfirm", region="hk")
    tasks = cadence.tasks_from_change(ch, today=TODAY, firms=FIRMS)
    assert "(HK)" in tasks[0]["title"]


# --------------------------------------------------------------------------
# Backward planner: plan_task_write (<=3-day in-place-update rule).
# --------------------------------------------------------------------------
def _planned(due="2026-08-18"):
    return {"kind": "advocate_target", "firm_id": "usfirm",
            "source_key": "usfirm/sa2028/app_open", "due": due, "title": "t", "why": "w"}


def test_plan_task_write_inserts_when_no_match():
    out = cadence.plan_task_write(_planned(), existing=[])
    assert out["op"] == "insert"
    assert out["existing_id"] is None


def test_plan_task_write_updates_in_place_within_three_days():
    existing = [{"id": 7, "kind": "advocate_target",
                 "source_key": "usfirm/sa2028/app_open", "due": "2026-08-16"}]
    out = cadence.plan_task_write(_planned(due="2026-08-18"), existing=existing)
    assert out["op"] == "update_in_place"
    assert out["existing_id"] == 7
    assert out["delta_days"] == 2


def test_plan_task_write_reschedules_beyond_three_days():
    existing = [{"id": 7, "kind": "advocate_target",
                 "source_key": "usfirm/sa2028/app_open", "due": "2026-08-01"}]
    out = cadence.plan_task_write(_planned(due="2026-08-18"), existing=existing)
    assert out["op"] == "reschedule"
    assert out["existing_id"] == 7
    assert out["delta_days"] == 17


def test_plan_task_write_matches_on_source_key_and_kind():
    """A different kind at the same source_key is not a match -> insert."""
    existing = [{"id": 7, "kind": "submit",
                 "source_key": "usfirm/sa2028/app_open", "due": "2026-08-16"}]
    out = cadence.plan_task_write(_planned(), existing=existing)
    assert out["op"] == "insert"


# --------------------------------------------------------------------------
# Audit fix 1: a past confirmed app_close must not fire a re-ping, and must
# not mask a genuinely upcoming one in the same firm/region bucket.
# --------------------------------------------------------------------------
def test_closing_soon_drops_a_past_confirmed_close():
    """A confirmed app_close that has already passed must not re-fire a
    priority-0 re-ping — the deadline is over, there is nothing left to
    submit before."""
    past_close = TODAY - timedelta(days=3)
    firm_dates = [{
        "firm_id": "dualfirm", "event_kind": "app_close", "region": "hk",
        "date": past_close, "confidence": "confirmed_official",
    }]
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                source="Apollo HK campaign")
    actions = cadence.due_actions([c], [touch(1, "chat", "2026-06-01 10:00")],
                                  firm_dates, as_of=AS_OF, firms=FIRMS)
    assert "reping" not in kinds_for(actions, 1)


def test_closing_soon_future_date_beats_past_date_in_same_bucket():
    """The regression the `min()` bucket masked: a stale past close and a
    genuinely upcoming one for the SAME firm/region must not average out or
    let the past one win — the future date is the one that matters, and it
    must be the one reported."""
    past_close = TODAY - timedelta(days=10)
    future_close = TODAY + timedelta(days=5)
    firm_dates = [
        {"firm_id": "dualfirm", "event_kind": "app_close", "region": "hk",
         "date": past_close, "confidence": "confirmed_official"},
        {"firm_id": "dualfirm", "event_kind": "app_close", "region": "hk",
         "date": future_close, "confidence": "confirmed_official"},
    ]
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                source="Apollo HK campaign")
    actions = cadence.due_actions([c], [touch(1, "chat", "2026-06-01 10:00")],
                                  firm_dates, as_of=AS_OF, firms=FIRMS)
    my = [a for a in actions if a["contact"]["id"] == 1 and a["action"] == "reping"]
    assert my, "the future close must still fire a re-ping"
    assert my[0]["ctx"]["close_date"] == future_close.isoformat()


# --------------------------------------------------------------------------
# Audit fix 6: firm_date region case must not defeat the region match.
# --------------------------------------------------------------------------
def test_closing_soon_region_match_is_case_insensitive():
    """A firm_date written with region='HK' must still region-scope the
    re-ping to an 'hk' contact — the match must not silently fail on case."""
    firm_dates = [{
        "firm_id": "dualfirm", "event_kind": "app_close", "region": "HK",
        "date": TODAY + timedelta(days=5), "confidence": "confirmed_official",
    }]
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied", region="hk")
    actions = cadence.due_actions([c], [touch(1, "chat", "2026-06-01 10:00")],
                                  firm_dates, as_of=AS_OF, firms=FIRMS)
    assert "reping" in kinds_for(actions, 1)


# --------------------------------------------------------------------------
# Audit fix 2: a nullable firm tier must not raise, and must sort as tier 3.
# --------------------------------------------------------------------------
def test_null_tier_sorts_cleanly_with_int_tiers():
    """crm.UserFirm.tier is nullable, and set_firm_tier deliberately writes
    tier=None for the 'Unranked' lane. due_actions must not raise comparing
    None to int in its final sort, and an untiered firm must sort as tier 3
    (the same default `_firm_meta` already applies to a MISSING tier)."""
    firms = {
        "usfirm": {"name": "US Firm", "tier": 1},
        "unranked": {"name": "Unranked Firm", "tier": None},
    }
    tier1 = contact(1, firm_id="usfirm", warmth="cold", thread_state="no_reply")
    untiered = contact(2, firm_id="unranked", warmth="cold", thread_state="no_reply")
    # Both produce the same action/priority (first_outreach, prio 1), so the
    # tier is what breaks the tie in the final sort — exactly the comparison
    # that used to raise TypeError(None, int).
    actions = cadence.due_actions([tier1, untiered], [], [], as_of=AS_OF, firms=firms)
    by_id = {a["contact"]["id"]: a for a in actions}
    assert by_id[1]["tier"] == 1
    assert by_id[2]["tier"] == 3
    # Tier 1 sorts ahead of the (defaulted) tier-3 untiered firm.
    assert actions[0]["contact"]["id"] == 1


# --------------------------------------------------------------------------
# Audit fix 3: an undatable "chat_done" thank-you prompt must expire too,
# not loop forever because `hrs is None` never satisfies `hrs > threshold`.
# --------------------------------------------------------------------------
def test_chat_done_with_no_chat_touch_does_not_prompt_forever():
    """thread_state='chat_done' but no 'chat' touch at all (reachable via
    the import/reconciliation path) must fall through instead of pinning an
    immortal thank-you prompt that can never satisfy `hrs > expiry`."""
    c = contact(1, warmth="chatted", thread_state="chat_done")
    actions = cadence.due_actions([c], [], [], as_of=AS_OF, firms=FIRMS)
    assert "thank_you" not in kinds_for(actions, 1)


def test_chat_done_with_no_chat_touch_falls_through_to_maintain_for_advocate():
    """The fall-through destination for the case above: an advocate with an
    undatable chat still reaches the maintain cadence rather than vanishing."""
    c = contact(1, warmth="advocate", thread_state="chat_done")
    actions = cadence.due_actions([c], [], [], as_of=AS_OF, firms=FIRMS)
    assert kinds_for(actions, 1) == {"maintain"}


# --------------------------------------------------------------------------
# Audit fix 8: an ISO timestamp with a UTC offset must keep that offset,
# not have it silently truncated by the strptime fallback.
# --------------------------------------------------------------------------
def test_as_dt_keeps_utc_offset():
    dt = cadence._as_dt("2026-07-27 09:00:00+08:00")
    assert dt is not None
    assert dt.utcoffset() == timedelta(hours=8)
    assert dt.hour == 9  # the wall-clock hour in the string is preserved...
    assert dt.astimezone(timezone.utc).hour == 1  # ...and 09:00+08:00 is 01:00 UTC, not 09:00 UTC


def test_as_dt_t_separator_offset_also_preserved():
    dt = cadence._as_dt("2026-07-27T09:00:00+08:00")
    assert dt.utcoffset() == timedelta(hours=8)


def test_as_dt_still_parses_plain_strings_without_offset():
    """The fromisoformat-first change must not regress the plain, no-offset
    strings the strptime fallback (and most stored rows) actually use."""
    assert cadence._as_dt("2026-07-27 09:00:00") == datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
    assert cadence._as_dt("2026-07-27 09:00") == datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
    assert cadence._as_dt("2026-07-27") == datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    assert cadence._as_dt("not-a-date") is None


# --------------------------------------------------------------------------
# Audit fix 9: sentinel day-counts (999/9999) must never leak into a
# user-facing reason string when the last touch isn't dateable.
# --------------------------------------------------------------------------
def test_confirm_chat_reason_has_no_sentinel_when_undateable():
    c = contact(1, warmth="replied", thread_state="chat_scheduled")
    actions = cadence.due_actions([c], [], [], as_of=AS_OF, firms=FIRMS)
    my = [a for a in actions if a["contact"]["id"] == 1][0]
    assert my["action"] == "confirm_chat"
    assert "999" not in my["reason"]
    assert my["ctx"]["business_days"] is None


def test_maintain_reason_has_no_sentinel_when_undateable():
    c = contact(1, warmth="advocate", thread_state="no_reply")
    actions = cadence.due_actions([c], [], [], as_of=AS_OF, firms=FIRMS)
    my = [a for a in actions if a["contact"]["id"] == 1][0]
    assert my["action"] == "maintain"
    assert "9999" not in my["reason"]
    assert my["ctx"]["days_since"] is None


# --------------------------------------------------------------------------
# Audit fix 10: the maintain reason's "target every N-M weeks" copy must
# render from the tunable params, not a hardcoded "4-6".
# --------------------------------------------------------------------------
def test_maintain_reason_renders_tuned_advocate_week_range():
    c = contact(1, warmth="advocate", thread_state="no_reply")
    touches = [touch(1, "chat", "2026-05-01 10:00")]
    actions = cadence.due_actions(
        [c], touches, [], as_of=AS_OF, firms=FIRMS,
        params={"advocate_touch_min_weeks": 5, "advocate_touch_max_weeks": 9},
    )
    my = [a for a in actions if a["contact"]["id"] == 1][0]
    assert "5–9 weeks" in my["reason"]
    assert "4–6 weeks" not in my["reason"]


# --------------------------------------------------------------------------
# Audit fix 11a: the full warmth x thread_state cross-product, pinned. Every
# combination either fires an action or is on the explicit allow-list of
# CURRENT intentional silences below. This is the guard that would have
# caught the tier=None crash and the chat_done immortal-prompt bug before
# they shipped: nothing about this decision tree was ever checked over its
# full input space.
# --------------------------------------------------------------------------
_ALL_WARMTH = ("cold", "replied", "chatted", "advocate")
_ALL_THREAD_STATES = ("no_reply", "replied", "chat_scheduled", "chat_done", "advocate", "quiet", "parked")

# Pins TODAY's behavior — do NOT add or remove entries here to "fix" a
# silence; several of these (e.g. warmth=replied/chatted sitting idle in
# thread_state=no_reply/chat_done, or warmth=chatted having replied) look
# like real coverage gaps, but closing them is a product decision tracked
# separately from this audit. This set only makes the current silences
# visible and regression-tested, not correct.
_INTENTIONALLY_SILENT_COMBOS = {
    ("cold", "chat_done"), ("cold", "advocate"), ("cold", "quiet"), ("cold", "parked"),
    ("replied", "no_reply"), ("replied", "chat_done"), ("replied", "advocate"),
    ("replied", "quiet"), ("replied", "parked"),
    ("chatted", "no_reply"), ("chatted", "replied"), ("chatted", "chat_done"),
    ("chatted", "advocate"), ("chatted", "quiet"), ("chatted", "parked"),
    ("advocate", "quiet"), ("advocate", "parked"),
}


@pytest.mark.parametrize("warmth", _ALL_WARMTH)
@pytest.mark.parametrize("thread_state", _ALL_THREAD_STATES)
def test_warmth_thread_state_cross_product_is_fully_accounted_for(warmth, thread_state):
    c = contact(1, warmth=warmth, thread_state=thread_state)
    actions = cadence.due_actions([c], [], [], as_of=AS_OF, firms=FIRMS)
    fired = bool(kinds_for(actions, 1))
    combo = (warmth, thread_state)
    if combo in _INTENTIONALLY_SILENT_COMBOS:
        assert not fired, (
            f"{combo} is on the intentional-silence allow-list but now fires "
            f"{kinds_for(actions, 1)} — update the allow-list if this is a deliberate change"
        )
    else:
        assert fired, f"{combo} produced no action and isn't on the silence allow-list"


# --------------------------------------------------------------------------
# Audit fix 11b: pin the priority ordinal of each action kind, so a future
# reordering of the decision tree has something concrete to regress against.
# --------------------------------------------------------------------------
def test_priority_ordinal_pinned_per_action_kind():
    expected = {
        "reping": 0,
        "thank_you_overdue": 0,
        "thank_you_within_window": 1,
        "confirm_chat": 1,
        "first_outreach": 1,
        "follow_up": 1,
        "advance": 1,
        "maintain": 2,
        "park": 3,
    }

    def prio_of(kind, actions):
        matches = [a for a in actions if a["action"] == kind]
        assert matches, f"no {kind!r} action produced by this fixture"
        return matches[0]["priority"]

    firm_dates = [{
        "firm_id": "dualfirm", "event_kind": "app_close", "region": "hk",
        "date": TODAY + timedelta(days=5), "confidence": "confirmed_official",
    }]
    reping_actions = cadence.due_actions(
        [contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                 source="Apollo HK campaign")],
        [touch(1, "chat", "2026-06-01 10:00")], firm_dates, as_of=AS_OF, firms=FIRMS,
    )
    assert prio_of("reping", reping_actions) == expected["reping"]

    overdue_actions = cadence.due_actions(
        [contact(1, warmth="chatted", thread_state="chat_done")],
        [touch(1, "chat", "2026-07-19 10:00")], [], as_of=AS_OF, firms=FIRMS,
    )
    assert prio_of("thank_you", overdue_actions) == expected["thank_you_overdue"]

    within_window_actions = cadence.due_actions(
        [contact(1, warmth="chatted", thread_state="chat_done")],
        [touch(1, "chat", "2026-07-21 20:00")], [], as_of=AS_OF, firms=FIRMS,
    )
    assert prio_of("thank_you", within_window_actions) == expected["thank_you_within_window"]

    confirm_chat_actions = cadence.due_actions(
        [contact(1, warmth="replied", thread_state="chat_scheduled")],
        [touch(1, "chat_scheduled", "2026-07-08 10:00")], [], as_of=AS_OF, firms=FIRMS,
    )
    assert prio_of("confirm_chat", confirm_chat_actions) == expected["confirm_chat"]

    first_outreach_actions = cadence.due_actions(
        [contact(1, warmth="cold", thread_state="no_reply")], [], [], as_of=AS_OF, firms=FIRMS,
    )
    assert prio_of("first_outreach", first_outreach_actions) == expected["first_outreach"]

    follow_up_actions = cadence.due_actions(
        [contact(1, warmth="cold", thread_state="no_reply")],
        [touch(1, "outreach", "2026-07-10 10:00")], [], as_of=AS_OF, firms=FIRMS,
    )
    assert prio_of("follow_up", follow_up_actions) == expected["follow_up"]

    park_actions = cadence.due_actions(
        [contact(1, warmth="cold", thread_state="no_reply")],
        [touch(1, "outreach", "2026-06-15 10:00"), touch(1, "follow_up", "2026-06-22 10:00")],
        [], as_of=AS_OF, firms=FIRMS,
    )
    assert prio_of("park", park_actions) == expected["park"]

    advance_actions = cadence.due_actions(
        [contact(1, warmth="replied", thread_state="replied")],
        [touch(1, "reply_received", "2026-07-15 10:00")], [], as_of=AS_OF, firms=FIRMS,
    )
    assert prio_of("advance", advance_actions) == expected["advance"]

    maintain_actions = cadence.due_actions(
        [contact(1, warmth="advocate", thread_state="no_reply")],
        [touch(1, "chat", "2026-05-01 10:00")], [], as_of=AS_OF, firms=FIRMS,
    )
    assert prio_of("maintain", maintain_actions) == expected["maintain"]
