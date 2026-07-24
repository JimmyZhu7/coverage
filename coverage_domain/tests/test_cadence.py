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
        touch(1, "chat", "2026-07-01 10:00"),        # chat_done, never thanked -> overdue thank_you
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
