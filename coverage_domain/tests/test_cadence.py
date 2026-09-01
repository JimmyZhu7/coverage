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
    """An HK app_close must not re-ping a US contact at the same firm.

    Regions are stated explicitly. This test used to express them through
    `source` strings ("Apollo direct search" -> us, "Apollo HK campaign" ->
    hk) because it predates the `region` column and `infer_region` was the
    only answer available. That inference is retired from the read path
    (cadence.contact_region), so the strings no longer decide anything —
    which is the point of the change, not a gap in this test: what it
    asserts, that a close in one region leaves the other region's contacts
    alone, is unchanged and now keys off the field that actually means
    region.
    """
    close_date = TODAY + timedelta(days=5)
    firm_dates = [{
        "firm_id": "dualfirm", "event_kind": "app_close", "region": "hk",
        "date": close_date, "confidence": "confirmed_official",
    }]
    us = contact(1, name="US Contact", firm_id="dualfirm", warmth="chatted",
                 thread_state="replied", region="us")
    hk = contact(2, name="HK Contact", firm_id="dualfirm", warmth="chatted",
                 thread_state="replied", region="hk")
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


def test_other_region_contact_matches_no_us_hk_close():
    """A contact explicitly OUTSIDE both markets (region "other" — the third
    Network bucket, 2026-08-25) is knowledge, not ignorance: a person in
    London must not be re-pinged for a Hong Kong close, and must not inherit
    the unknown row's both-regions fallback either."""
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                region="other")
    actions = cadence.due_actions([c], [touch(1, "chat", "2026-06-01 10:00")],
                                  _hk_close_firm_dates(), as_of=AS_OF, firms=FIRMS)
    assert "reping" not in kinds_for(actions, 1)


def test_hand_added_contact_is_not_guessed_us_from_source():
    """The bug this field exists for: a hand-added contact is written with
    source="manual", which `infer_region` read as 'us' — so an HK contact was
    skipped for HK deadlines forever.

    Both contacts now re-ping, for two different reasons, and the second one
    is the fix that lands here. The first has region="hk" and matches the HK
    close directly. The second has NO region: with the source inference
    retired it resolves to None, which takes the both-regions fallback — the
    engine matches the soonest close at the firm in any region rather than
    withholding the highest-value nudge it makes on the strength of a guess
    about a provenance string. Under-scoping shows a re-ping the user can
    ignore; over-scoping hid it entirely.
    """
    hk = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                 source="manual", region="hk")
    unknown = contact(2, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                      source="manual", region="")
    touches = [touch(1, "chat", "2026-06-01 10:00"), touch(2, "chat", "2026-06-01 10:00")]
    actions = cadence.due_actions([hk, unknown], touches, _hk_close_firm_dates(),
                                  as_of=AS_OF, firms=FIRMS)
    assert "reping" in kinds_for(actions, 1)
    assert "reping" in kinds_for(actions, 2)


def test_blank_region_is_unknown_not_inferred_from_source():
    """A blank region means UNKNOWN, whatever the provenance text says.

    This replaces `test_blank_region_still_falls_back_to_source`, which
    pinned the opposite. `infer_region` answers for ANY non-empty string, so
    while it backed the read path a blank region was never once unknown — it
    was 'hk' for the few sources containing those two letters and 'us' for
    everything else, including "Gmail USC discovery". Here the contact
    re-pings via the both-regions fallback, and would do so identically if
    the source read "manual" or "Apollo direct search": the string no longer
    votes.
    """
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                source="Apollo HK campaign", region="")
    actions = cadence.due_actions([c], [touch(1, "chat", "2026-06-01 10:00")],
                                  _hk_close_firm_dates(), as_of=AS_OF, firms=FIRMS)
    assert "reping" in kinds_for(actions, 1)
    assert cadence.contact_region(c) is None


def test_us_source_string_no_longer_hides_an_hk_close():
    """The live-data case that forced the retirement, in miniature.

    19 of the founder's 51 blank-region contacts resolved to 'us' purely
    because their source read "Gmail USC discovery" — the substring "us" is
    not even what `infer_region` keys on; it simply returns 'us' for
    everything that doesn't say "hk". Each of those rows was then skipped for
    every HK close at their firm. The re-ping is the highest-value nudge the
    cadence makes, and it was being withheld on the basis of a provenance
    label nobody wrote as a region.
    """
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="replied",
                source="Gmail USC discovery", region="")
    actions = cadence.due_actions([c], [touch(1, "chat", "2026-06-01 10:00")],
                                  _hk_close_firm_dates(), as_of=AS_OF, firms=FIRMS)
    assert "reping" in kinds_for(actions, 1)


def test_contact_region_helper_reads_only_the_explicit_field():
    assert cadence.contact_region({"region": "hk", "source": "manual"}) == "hk"
    assert cadence.contact_region({"region": " HK ", "source": "manual"}) == "hk"
    # Blank is UNKNOWN regardless of provenance text — these three lines used
    # to assert "hk" / "us" / None respectively.
    assert cadence.contact_region({"region": "", "source": "Apollo HK"}) is None
    assert cadence.contact_region({"region": "", "source": "manual"}) is None
    assert cadence.contact_region({"region": "", "source": ""}) is None
    assert cadence.contact_region({}) is None


def test_infer_region_is_kept_but_unused_by_the_read_path():
    """`infer_region` stays in the module for a one-time backfill and as the
    record of the old rule — so it must keep working — but `contact_region`
    must not consult it. The second assertion is the one that matters: if the
    fallback is ever reinstated, these two lines disagree."""
    assert cadence.infer_region("Apollo HK campaign") == "hk"
    assert cadence.infer_region("manual") == "us"
    assert cadence.infer_region("") is None
    assert cadence.contact_region({"region": "", "source": "Apollo HK campaign"}) is None


def test_zero_touch_contact_not_treated_as_stale():
    """A brand-new, never-touched contact is flagged for first outreach."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    actions = cadence.due_actions([c], [], [], as_of=AS_OF, firms=FIRMS)
    my = [a["action"] for a in actions if a["contact"]["id"] == 1]
    assert my == ["first_outreach"], my


def test_firm_known_is_false_only_for_the_terminal_no_firm_fallback():
    """A hand-added contact with no firm_id AND no firm_text hits the
    terminal "No firm listed" placeholder (cadence.py's own comment: this
    replaced an even worse literal "?"). `firm_known` travels alongside so a
    template can style the placeholder differently from a real firm name —
    confirmed live: contact id=473 (Giulia Savino) rendered "NO FIRM LISTED"
    through the same span/class used for real firms like ACCRACARE."""
    c = contact(1, firm_id=None, firm_text="", warmth="cold", thread_state="no_reply")
    actions = cadence.due_actions([c], [], [], as_of=AS_OF, firms=FIRMS)
    mine = [a for a in actions if a["contact"]["id"] == 1]
    assert mine and mine[0]["firm_name"] == "No firm listed"
    assert mine[0]["firm_known"] is False

    # A contact with free-text firm_text but no firm_id (the common shape —
    # 33 of 34 firm-less contacts in the live audit) is NOT the fallback:
    # the free text is a real, if unranked, employer name.
    c2 = contact(2, firm_id=None, firm_text="West Monroe", warmth="cold",
                 thread_state="no_reply")
    actions2 = cadence.due_actions([c2], [], [], as_of=AS_OF, firms=FIRMS)
    mine2 = [a for a in actions2 if a["contact"]["id"] == 2]
    assert mine2 and mine2[0]["firm_name"] == "West Monroe"
    assert mine2[0]["firm_known"] is True


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


# --------------------------------------------------------------------------
# The confirm-chat sentence may only state facts it holds.
#
# Youqi Chen, founder's live account, 2026-08-31. Her notes read "Replied to
# your email: coffee in HK, offered same-day meetup" — an offer, never
# confirmed, no CalendarEvent anywhere. Her Today card read "chat was
# scheduled 6 business days ago". That number is business days since the LAST
# TOUCH, and the last touch IS the `chat_scheduled` touch, so the sentence
# dated a booking off a clock that only knows when Coverage last wrote
# something down — about a booking that never existed.
#
# Coverage holds a chat's real time only when a capture read an .ics DTSTART,
# which is rare (`capture.gmail._upsert_scheduled_chat`: "A finding with no
# time is not an error — most are"). So the no-time sentence below is the
# ORDINARY path and is the one that must not name a day.
# --------------------------------------------------------------------------
def test_confirm_chat_never_claims_a_scheduling_date_it_does_not_have():
    c = contact(1, warmth="replied", thread_state="chat_scheduled")
    actions = cadence.due_actions([c], [touch(1, "chat_scheduled", "2026-07-08 10:00")],
                                  [], as_of=AS_OF, firms=FIRMS)
    my = [a for a in actions if a["contact"]["id"] == 1][0]
    assert my["action"] == "confirm_chat"
    assert "was scheduled 10 business days ago" not in my["reason"]
    assert "scheduled for" not in my["reason"], "no day is held, so none is named"
    # What IS known, and all that is claimed: how long the thread has been
    # quiet. The count itself is still carried, in the reason and in ctx.
    assert "nothing logged in 10 business days" in my["reason"]
    assert my["ctx"]["business_days"] == 10
    assert my["ctx"]["scheduled_on"] is None


def test_confirm_chat_names_the_day_when_a_real_time_is_on_record():
    """The one case where a scheduling date is a fact, threaded in from the
    Django side as `chat_scheduled_at` (a `crm.CalendarEvent` start time)."""
    c = contact(1, warmth="replied", thread_state="chat_scheduled",
                chat_scheduled_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC))
    actions = cadence.due_actions([c], [touch(1, "chat_scheduled", "2026-07-08 10:00")],
                                  [], as_of=AS_OF, firms=FIRMS)
    my = [a for a in actions if a["contact"]["id"] == 1][0]
    assert my["action"] == "confirm_chat"
    assert "chat was scheduled for 2026-07-09" in my["reason"]
    assert my["ctx"]["scheduled_on"] == "2026-07-09"


def test_confirm_chat_stays_quiet_about_a_chat_still_in_the_future():
    """"Did it happen?" is the wrong question about a meeting that has not.

    Same guard and same reasoning as branch 1's `not_yet`. Reachable whenever
    the booked time is further out than the silence window: a chat set three
    weeks ahead, then nothing logged for two."""
    c = contact(1, warmth="replied", thread_state="chat_scheduled",
                chat_scheduled_at=datetime(2026, 8, 12, 15, 0, tzinfo=UTC))
    actions = cadence.due_actions([c], [touch(1, "chat_scheduled", "2026-07-08 10:00")],
                                  [], as_of=AS_OF, firms=FIRMS)
    assert kinds_for(actions, 1) == set()


def test_confirm_chat_with_no_dateable_touch_names_no_day_either():
    c = contact(1, warmth="replied", thread_state="chat_scheduled")
    actions = cadence.due_actions([c], [], [], as_of=AS_OF, firms=FIRMS)
    my = [a for a in actions if a["contact"]["id"] == 1][0]
    assert my["reason"].startswith("a chat was being arranged")
    assert "scheduled for" not in my["reason"]
    assert my["ctx"]["scheduled_on"] is None


# --------------------------------------------------------------------------
# The engine's calendar is decided by the zone its inputs arrive in.
#
# On the founder's live account the web layer handed `as_of` in local time
# (America/Los_Angeles) while touch rows still arrived in UTC. A touch stored
# 2026-08-24 01:37Z is 2026-08-23 18:37 local, so the engine counted from Aug
# 24 and the card's own ledger line counted from Aug 23: one card, one render,
# "5 business days" in the sentence and "6 business days ago" in the row
# beneath it. The engine cannot detect the skew — both are valid aware
# datetimes — so the contract is that every timestamp arrives in ONE zone.
# This test pins the arithmetic that contract protects; the web side's half
# is `crm/tests/test_cadence_timezone_agreement.py`.
# --------------------------------------------------------------------------
def test_business_day_count_follows_the_zone_the_caller_hands_in():
    la = timezone(timedelta(hours=-7))
    instant = datetime(2026, 8, 24, 1, 37, tzinfo=UTC)
    assert instant.astimezone(la).date() == date(2026, 8, 23), "the boundary case"
    as_of = datetime(2026, 8, 31, 9, 0, tzinfo=UTC).astimezone(la)

    def bd_for(ts):
        c = contact(1, warmth="replied", thread_state="chat_scheduled")
        actions = cadence.due_actions(
            [c], [touch(1, "chat_scheduled", ts)], [], as_of=as_of, firms=FIRMS
        )
        return [a for a in actions if a["contact"]["id"] == 1][0]["ctx"]["business_days"]

    # Localized input (what `crm.utils._touch_dicts` now always hands over)
    # agrees with the ledger line's own `localtime(ts).date()`.
    assert bd_for(instant.astimezone(la)) == cadence.business_days_since(
        instant.astimezone(la).date(), as_of.date()
    ) == 6
    # The raw-UTC input that used to arrive is off by exactly the one day the
    # boundary crosses. Pinned so a regression is a failing assert, not a
    # discrepancy somebody has to notice on a card.
    assert bd_for(instant) == 5


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
# The follow-up cadence (branch 6). AS_OF is Wed 2026-07-22.
#
# HISTORY: this used to stage a longer window before a second follow-up
# (2026-07-27), then dropped it the next day in favor of a stricter rule —
# never send a second follow-up at all; one unanswered follow-up is enough
# evidence to park. `max_cold_touches` is capped at 2 in
# crm.views.TUNABLE_CADENCE_PARAMS specifically so that can't be configured
# back open. These tests pin the current, simpler rule.
# --------------------------------------------------------------------------


def test_only_one_followup_is_ever_sent():
    """However long the silence after the one follow-up, no second one fires —
    even well past what the old staged window would have required."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    touches = [
        touch(1, "outreach", "2026-05-01 10:00"),
        touch(1, "follow_up", "2026-05-15 10:00"),   # long silent since; still no 2nd follow_up
    ]
    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
    assert "follow_up" not in kinds_for(actions, 1)


def test_a_raised_max_cold_touches_still_only_sends_one_followup():
    """due_actions itself has no whitelist on `params` — it trusts whatever the
    caller hands in, by design (the whitelist lives one layer up, in
    crm.views.TUNABLE_CADENCE_PARAMS, tested there). So if a caller passed a
    raised override anyway, this is what would happen: a SECOND follow-up
    would in fact fire, because branch 6 has nothing else stopping it. That's
    exactly why the range is capped at the web layer rather than left to this
    module to enforce — pinned here so the two layers' responsibilities don't
    blur, not as a claim that this module blocks it itself."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    touches = [
        touch(1, "outreach", "2026-06-25 10:00"),
        touch(1, "follow_up", "2026-07-13 10:00"),   # 7 business days before AS_OF
    ]
    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS,
                                  params={"max_cold_touches": 3})
    assert "follow_up" in kinds_for(actions, 1), (
        "confirms the cap must live in crm.views, not here"
    )


def test_park_still_fires_once_its_own_window_elapses():
    """At max_cold touches (the one follow-up already sent) and 10 business
    days of silence, park still takes the contact, at priority 3."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    touches = [
        touch(1, "outreach", "2026-06-15 10:00"),
        touch(1, "follow_up", "2026-07-08 10:00"),   # 10 bd: park's window
    ]
    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
    my = [a for a in actions if a["contact"]["id"] == 1]
    assert [a["action"] for a in my] == ["park"]
    assert my[0]["priority"] == 3


def test_followup_default_clears_a_full_calendar_week():
    """The default exists to put the follow-up BEYOND one week. 5 business
    days is exactly 7 calendar days, so the default must exceed 5 — this pins
    the intent against a well-meaning future tweak back to 5."""
    assert cadence.CADENCE_DEFAULTS["followup_after_business_days"] > 5
    # And the arithmetic that default rests on, asserted rather than trusted:
    # a Monday touch + 6 business days is the following Tuesday, 8 days later.
    assert cadence.business_days_since(date(2026, 7, 13), date(2026, 7, 21)) == 6
    assert (date(2026, 7, 21) - date(2026, 7, 13)).days == 8


def test_ten_calendar_days_is_the_weekday_proof_followup_offset():
    """A cold contact silent for 10 CALENDAR days is due a follow-up on every
    day of the week; at 8 or 9 it is not.

    This is the arithmetic every web-layer test rests on and the one nobody
    can see from the fixture. Those tests run against the real wall clock —
    `crm.today._build_actions` reads `timezone.now()` and has no `as_of` seam
    — so they express staleness as a calendar-day offset while the engine
    measures it in business days. The gap is not constant: a fixed calendar
    offset buys FEWER business days when the span swallows an extra weekend,
    which is exactly what happens when the suite runs on a Saturday or Sunday.
    An 8-day offset is 6 business days Mon-Fri and 5 on Sat/Sun, so a fixture
    built on 8 silently stops being overdue two days in seven, and the test
    above it fails for a reason that has nothing to do with what it asserts
    (twice now: test_today on Sat 2026-08-01, test_coverage_gaps on Sat
    2026-08-15).

    10 is the fix because a 10-day span is one full week plus 3 days, so it
    contains 2, 3, or 4 weekend days and therefore never fewer than 6 business
    days, whatever weekday it ends on. 9 can still land on 5. Asserted here,
    once, over a full week of as-of days, so a fixture author can take the
    number on trust and a future change to `followup_after_business_days`
    breaks THIS test — which explains itself — rather than a distant one about
    lane ordering.
    """
    # A full week of as-of days, so no weekday alignment goes unchecked.
    week = [AS_OF + timedelta(days=i) for i in range(7)]
    fires_on: dict[int, list[str]] = {}
    for days_ago in (8, 9, 10):
        for as_of in week:
            c = contact(1)
            touches = [touch(1, "outreach", as_of - timedelta(days=days_ago))]
            actions = cadence.due_actions([c], touches, [], as_of=as_of, firms=FIRMS)
            if "follow_up" in kinds_for(actions, 1):
                fires_on.setdefault(days_ago, []).append(as_of.strftime("%a"))

    assert fires_on[10] == ["Wed", "Thu", "Fri", "Sat", "Sun", "Mon", "Tue"], (
        "10 calendar days must be overdue on every day of the week — that is "
        f"the whole reason fixtures use it; got {fires_on[10]}"
    )
    # The two that are NOT safe, and precisely where each one drops out. An
    # 8-day fixture is quietly a weekday-only fixture; 9 survives Saturday and
    # dies on Sunday. Spelled out rather than merely "< 7" so a future change
    # to the window shows up as a specific, readable diff.
    assert sorted(fires_on[8]) == sorted(["Wed", "Thu", "Fri", "Mon", "Tue"]), (
        f"8 calendar days must miss BOTH weekend days; got {fires_on[8]}"
    )
    assert "Sun" not in fires_on[9] and len(fires_on[9]) == 6, (
        f"9 calendar days must miss Sunday and nothing else; got {fires_on[9]}"
    )

    # And the span arithmetic the paragraph above argues from, checked
    # directly rather than inferred from the branch behaviour.
    worst = min(
        cadence.business_days_since(d - timedelta(days=10), d)
        for d in (date(2026, 7, 20) + timedelta(days=i) for i in range(7))
    )
    assert worst == 6, f"10 calendar days must never buy fewer than 6 business days, got {worst}"


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
# C2 (2026-07-30 divergence): idle clocks read the last REAL touch. A
# `manual_override` audit row is the system writing to itself and must not
# restart a relationship clock.
# --------------------------------------------------------------------------
def test_manual_override_does_not_reset_the_advocate_clock():
    """The measured bug: promoting someone to advocate writes an audit row,
    and branch 5's "last touch of any kind" clock read it as a fresh touch —
    so the promotion itself silenced the advocate for four weeks."""
    c = contact(1, warmth="advocate", thread_state="advocate")
    touches = [
        touch(1, "chat", "2026-05-01 10:00"),                  # real, 82d before AS_OF
        touch(1, "manual_override", "2026-07-20 10:00",        # audit row, 2d before
              note="warmth=advocate thread_state=advocate"),
    ]
    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
    my = [a for a in actions if a["contact"]["id"] == 1]
    assert [a["action"] for a in my] == ["maintain"], (
        "the audit row restarted the 4-week clock and hid the advocate"
    )
    # ...and the day count is measured from the chat, not the audit row.
    assert my[0]["ctx"]["days_since"] == 82


def test_manual_override_only_contact_reads_as_no_dateable_touch():
    """A contact whose ONLY touch is an audit row has no relationship history
    at all — the honest reading is "nothing on record", not "touched today"."""
    c = contact(1, warmth="advocate", thread_state="advocate")
    touches = [touch(1, "manual_override", "2026-07-21 10:00", note="warmth=advocate")]
    my = [a for a in cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
          if a["contact"]["id"] == 1][0]
    assert my["action"] == "maintain"
    assert my["ctx"]["days_since"] is None
    assert "no dateable touch on record" in my["reason"]


def test_manual_override_does_not_reset_the_cold_followup_clock():
    """Branch 6 reads the same clock: parking-adjacent bookkeeping on a cold
    contact must not push their follow-up back out of range."""
    c = contact(1, warmth="cold", thread_state="no_reply")
    touches = [
        touch(1, "outreach", "2026-07-10 10:00"),              # 8 business days before
        touch(1, "manual_override", "2026-07-21 10:00", note="thread_state=no_reply"),
    ]
    assert "follow_up" in kinds_for(
        cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1
    )


def test_manual_override_does_not_reset_the_confirm_chat_clock():
    """Branch 2's staleness clock, same rule."""
    c = contact(1, warmth="replied", thread_state="chat_scheduled")
    touches = [
        touch(1, "chat_scheduled", "2026-07-08 10:00"),
        touch(1, "manual_override", "2026-07-21 10:00", note="warmth=replied"),
    ]
    assert "confirm_chat" in kinds_for(
        cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1
    )


# --------------------------------------------------------------------------
# C1 (2026-07-30 divergence): `keep_warm`, the branch that closes the chatted
# dead end. The ported tree said nothing about a contact once the thank-you
# window shut, which silenced the warmest people in the CRM.
# --------------------------------------------------------------------------
def test_chatted_contact_gets_keep_warm_once_the_thank_you_window_shuts():
    c = contact(1, warmth="chatted", thread_state="chat_done")
    touches = [
        touch(1, "chat", "2026-06-01 10:00"),        # 51d before AS_OF: expired
        touch(1, "thank_you", "2026-06-01 12:00"),
    ]
    my = [a for a in cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
          if a["contact"]["id"] == 1]
    assert [a["action"] for a in my] == ["keep_warm"]
    assert my[0]["priority"] == 2
    assert my[0]["ctx"]["days_since"] == 51


def test_chatted_contact_inside_the_window_is_left_alone():
    """The other side of the boundary: a chat three days ago is not a
    relationship going cold, and 5b must not nag it."""
    c = contact(1, warmth="chatted", thread_state="chat_done")
    touches = [
        touch(1, "chat", "2026-07-19 10:00"),
        touch(1, "thank_you", "2026-07-19 12:00"),
    ]
    assert kinds_for(cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1) == set()


def test_keep_warm_does_not_pre_empt_a_live_thank_you():
    """Branch 1 still owns the first week after a chat — a thank-you that is
    still timely must not be replaced by a keep-warm nudge."""
    c = contact(1, warmth="chatted", thread_state="chat_done")
    touches = [touch(1, "chat", "2026-07-20 09:00")]  # 2d before AS_OF, not thanked
    assert kinds_for(cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1) == {
        "thank_you"
    }


def test_keep_warm_window_is_a_parameter_and_renders_its_range():
    c = contact(1, warmth="chatted", thread_state="chat_done")
    touches = [touch(1, "chat", "2026-07-08 10:00")]  # 14d before AS_OF
    default = kinds_for(cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1)
    assert default == set(), "14d is inside the default 3-week window"

    tuned = [a for a in cadence.due_actions(
        [c], touches, [], as_of=AS_OF, firms=FIRMS,
        params={"chatted_touch_min_weeks": 1, "chatted_touch_max_weeks": 2},
    ) if a["contact"]["id"] == 1]
    assert [a["action"] for a in tuned] == ["keep_warm"]
    assert "1–2 weeks" in tuned[0]["reason"]
    assert "3–5 weeks" not in tuned[0]["reason"]


def test_keep_warm_reason_has_no_sentinel_when_undateable():
    c = contact(1, warmth="chatted", thread_state="chat_done")
    my = [a for a in cadence.due_actions([c], [], [], as_of=AS_OF, firms=FIRMS)
          if a["contact"]["id"] == 1][0]
    assert my["action"] == "keep_warm"
    assert my["ctx"]["days_since"] is None
    assert "no dateable touch on record" in my["reason"]


def test_keep_warm_clock_ignores_audit_rows_too():
    """C1 is born on the C2 clock: a promotion/correction row must not push a
    chatted contact's keep-warm date out."""
    c = contact(1, warmth="chatted", thread_state="chat_done")
    touches = [
        touch(1, "chat", "2026-06-01 10:00"),
        touch(1, "thank_you", "2026-06-01 12:00"),
        touch(1, "manual_override", "2026-07-21 10:00", note="thread_state=chat_done"),
    ]
    assert "keep_warm" in kinds_for(
        cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1
    )


# --------------------------------------------------------------------------
# C1b (2026-08-22 divergence): branch 5b gates on WARMTH, not on the
# warmth/thread_state PAIR. C1 closed the chatted dead end for contacts
# carrying thread_state 'chat_done' and left it open for every other chatted
# contact — and the two columns drift apart routinely, because warmth is a
# ratchet and thread_state is not.
# --------------------------------------------------------------------------
def test_chatted_contact_outside_chat_done_still_gets_keep_warm():
    """THE CRACK. warmth ratcheted to 'chatted' (they met you) while
    thread_state stayed 'no_reply' — reachable via CSV import, via
    `pipeline.set_state`, and via the backward thread_state move pipeline.py
    documents. Branch 6 tests warmth 'cold' and branch 7 tests thread_state
    'replied', so before C1b this contact matched NOTHING and left the cadence
    permanently."""
    c = contact(1, warmth="chatted", thread_state="no_reply")
    touches = [touch(1, "chat", "2026-06-01 10:00")]  # 51d before AS_OF
    my = [a for a in cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
          if a["contact"]["id"] == 1]
    assert [a["action"] for a in my] == ["keep_warm"]
    assert my[0]["ctx"]["days_since"] == 51


def test_chatted_contact_in_advocate_thread_state_gets_keep_warm():
    """The other half of the crack: thread_state promoted to 'advocate' while
    warmth stayed 'chatted'. Branch 5 tests WARMTH, so it does not claim this
    contact, and nothing below it did either."""
    c = contact(1, warmth="chatted", thread_state="advocate")
    touches = [touch(1, "chat", "2026-06-01 10:00")]
    assert kinds_for(cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1) == {
        "keep_warm"
    }


def test_widened_5b_does_not_steal_branch_seven():
    """`replied` is the one thread_state branch 5b must not claim: branch 7
    has something strictly better to say about it (C3)."""
    c = contact(1, warmth="chatted", thread_state="replied")
    touches = [touch(1, "reply_received", "2026-06-01 10:00")]
    assert kinds_for(cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1) == {
        "advance"
    }


def test_widened_5b_does_not_reopen_a_parked_contact():
    """Branch 4 is a deliberate exit and still returns first. Widening 5b must
    not turn Park into a button that undoes itself on the next render."""
    for state in ("parked", "quiet"):
        c = contact(1, warmth="chatted", thread_state=state)
        touches = [touch(1, "chat", "2026-06-01 10:00")]
        assert kinds_for(
            cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1
        ) == set(), state


def test_widened_5b_still_respects_the_clock_outside_chat_done():
    """The gate widened; the window did not. A chatted/no_reply contact
    touched last week is not due."""
    c = contact(1, warmth="chatted", thread_state="no_reply")
    touches = [touch(1, "chat", "2026-07-19 10:00")]  # 3d before AS_OF
    assert kinds_for(cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1) == set()


def test_advocates_keep_their_own_slower_clock():
    """5b sits AFTER branch 5 so an advocate whose thread_state is chat_done
    still gets `maintain`, on the advocate window, not `keep_warm`."""
    c = contact(1, warmth="advocate", thread_state="chat_done")
    touches = [touch(1, "chat", "2026-06-01 10:00")]
    assert kinds_for(cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS), 1) == {
        "maintain"
    }


def test_keep_warm_never_pre_empts_a_pre_deadline_reping():
    """Branch 3 outranks 5b: a confirmed close beats a keep-warm nudge."""
    firm_dates = [{
        "firm_id": "dualfirm", "event_kind": "app_close", "region": "hk",
        "date": TODAY + timedelta(days=5), "confidence": "confirmed_official",
    }]
    c = contact(1, firm_id="dualfirm", warmth="chatted", thread_state="chat_done",
                region="hk")
    touches = [touch(1, "chat", "2026-06-01 10:00")]
    assert kinds_for(
        cadence.due_actions([c], touches, firm_dates, as_of=AS_OF, firms=FIRMS), 1
    ) == {"reping"}


# --------------------------------------------------------------------------
# C3 (2026-07-30 divergence): branch 7 now covers warmth='chatted'. Replying
# again AFTER a chat used to make a contact LESS visible than never having
# chatted at all.
# --------------------------------------------------------------------------
def test_chatted_contact_who_replies_again_gets_advance():
    c = contact(1, warmth="chatted", thread_state="replied")
    touches = [
        touch(1, "chat", "2026-06-01 10:00"),
        touch(1, "reply_received", "2026-07-15 10:00"),
    ]
    my = [a for a in cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
          if a["contact"]["id"] == 1]
    assert [a["action"] for a in my] == ["advance"]
    assert my[0]["priority"] == 1


def test_advocates_are_still_excluded_from_branch_seven():
    """Branch 5 owns advocates and returns before branch 7 — widening the
    warmth set must not have changed that."""
    c = contact(1, warmth="advocate", thread_state="replied")
    # A recent reply: branch 7 would fire `advance` here for any other warmth,
    # and branch 5's own 4-week window has NOT elapsed, so the honest result
    # is silence — the advocate is simply not due.
    fresh = [touch(1, "reply_received", "2026-07-15 10:00")]
    assert kinds_for(cadence.due_actions([c], fresh, [], as_of=AS_OF, firms=FIRMS), 1) == set()
    # And once the advocate window does elapse, it is `maintain` that fires,
    # not `advance` — branch 5 returned before branch 7 could be reached.
    stale = [touch(1, "reply_received", "2026-05-01 10:00")]
    assert kinds_for(cadence.due_actions([c], stale, [], as_of=AS_OF, firms=FIRMS), 1) == {
        "maintain"
    }


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
# silence; several of these (e.g. warmth=replied sitting idle in
# thread_state=no_reply/chat_done) look like real coverage gaps, but closing
# them is a product decision tracked separately from this audit. This set only
# makes the current silences visible and regression-tested, not correct.
#
# Two entries LEFT this set on 2026-07-30, both deliberately:
#   ("chatted", "chat_done") -> now fires `keep_warm` (C1). This was the
#     costliest silence in the table: the people who actually met you.
#   ("chatted", "replied")   -> now fires `advance` (C3).
#
# Two more LEFT it on 2026-08-22 (C1b), and the expected output legitimately
# changed for both:
#   ("chatted", "no_reply") -> now fires `keep_warm`.
#   ("chatted", "advocate") -> now fires `keep_warm`.
# C1 gated branch 5b on warmth AND thread_state=='chat_done'. Warmth is a
# ratchet and thread_state is not, so the pair drifts apart routinely (import,
# `set_state`, and the backward thread_state move pipeline.py documents), and
# a contact who came out of that drift as chatted/no_reply matched branch 5b's
# warmth test, failed its thread_state test, and then matched nothing else at
# all — branch 6 needs warmth 'cold', branch 7 needs thread_state 'replied'.
# These two rows were not "silences the product chose"; they were the C1 dead
# end still open one column over. Widening the gate to warmth-with-`replied`-
# carved-out closes them. ("chatted", "quiet") and ("chatted", "parked") stay
# on the list and stay silent: branch 4 is a deliberate exit from the cadence
# and returns long before 5b is reached.
_INTENTIONALLY_SILENT_COMBOS = {
    ("cold", "chat_done"), ("cold", "advocate"), ("cold", "quiet"), ("cold", "parked"),
    ("replied", "no_reply"), ("replied", "chat_done"), ("replied", "advocate"),
    ("replied", "quiet"), ("replied", "parked"),
    ("chatted", "quiet"), ("chatted", "parked"),
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
        "keep_warm": 2,
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

    keep_warm_actions = cadence.due_actions(
        [contact(1, warmth="chatted", thread_state="chat_done")],
        [touch(1, "chat", "2026-05-01 10:00")], [], as_of=AS_OF, firms=FIRMS,
    )
    assert prio_of("keep_warm", keep_warm_actions) == expected["keep_warm"]



def test_a_bulk_blast_does_not_reset_a_relationships_idle_clock():
    """A mass programme invite is recorded on the contact but is not evidence
    that anyone maintained a relationship, so it must not push a due contact
    back down the queue. Same argument the C2 divergence made for
    `manual_override`: a row the system wrote about itself is not a touch.

    Without this, the capture classifier added 2026-08-22 would have made
    things WORSE for a firm that mailshots its list — every blast would
    silently restart the advocate clock, and the contact would go quiet for
    another cycle each time the firm sent one.
    """
    old = (AS_OF - timedelta(days=40)).isoformat()
    yesterday = (AS_OF - timedelta(days=1)).isoformat()
    c = {
        "id": 1, "name": "Ada", "firm": "usfirm",
        "warmth": "advocate", "thread_state": "advocate", "region": "us",
    }
    # The only RECENT row is the blast; the last real touch is 40 days old,
    # past advocate_touch_min_weeks (4 weeks), so this contact is due.
    touches = [
        {"contact_id": 1, "kind": "outreach", "ts": old},
        {"contact_id": 1, "kind": "bulk_received", "ts": yesterday},
    ]

    actions = cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS)
    assert [a["action"] for a in actions] == ["maintain"], (
        "a bulk blast reset the advocate clock and silenced a due contact"
    )
