"""WS-CRM-07, 10, 11, 12, 13, 18, 19 — the view layer's half.

Every case here corresponds to a number measured on the founder's own live
account on 2026-09-02, read-only, and the docstrings carry the measurement
rather than an illustration:

  - 265 live contacts, 137 with no role text at all, 92 cold/no_reply rows of
    which ZERO parse to a seniority rung, so the cold-ask multiplier moves 0
    cards on his board today;
  - 12 debriefs, 0 of them recording an intro, so the promised-action branch
    is dark for him and produces 0 cards;
  - regions hk+us, so the Hong Kong cadence overlay does not touch his queue;
  - 544 touches whose channels are email 357, NULL 179, coffee_chat 6,
    linkedin 2, and zero on "call", "event" or "other";
  - no assessment firm on his tier list, so the apply-only gate marks 0 cards.

`transaction=True` for the same reason `test_relevance.py` uses it: the
promotion path goes through `crm.services`, which opens its own connection.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from accounts.forms import CADENCE_LABELS
from coverage_domain import cadence
from coverage_domain.pipeline import REFERRAL_KIND
from crm import debrief as debrief_svc, relevance as rel
from crm.models import ChatDebrief, Contact, Touch, UserFirm
from crm.today import (
    REGION_CADENCE_OVERLAY,
    TUNABLE_CADENCE_PARAMS,
    _build_actions,
    _cadence_params,
    _daybar,
    _send_windows,
)
from crm.utils import CHANNEL_LABELS
from directory.models import Firm

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="cad@example.com", **kw):
    kw.setdefault("weekly_touch_goal", 14)
    return get_user_model().objects.create_user(email=email, password="pw12345!", **kw)


def _firm(user, slug="nomura", name="Nomura", tier=1, style="campus"):
    firm = Firm.objects.create(slug=slug, name=name, recruiting_style=style)
    UserFirm.all_objects.create(user=user, firm=firm, tier=tier)
    return firm


def _touch(user, contact, kind, *, days_ago=0, channel="email"):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel=channel,
        ts=timezone.now() - timedelta(days=days_ago),
    )


def _by_name(user):
    actions, _ = _build_actions(user)
    return {a["contact"]["name"]: a for a in actions}


# ---------------------------------------------------------------------------
# WS-CRM-07 — the promised-action follow-up, plumbed.
# ---------------------------------------------------------------------------
def _chatted_with_promise(user, firm, *, days_ago, intro="Dana Reed"):
    contact = Contact.all_objects.create(
        user=user, name="Priya Nair", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    chat = _touch(user, contact, "chat", days_ago=days_ago)
    _touch(user, contact, "thank_you", days_ago=days_ago - 1)
    ChatDebrief.all_objects.create(
        user=user, contact=contact, touch=chat, intro_name=intro,
    )
    return contact, chat


def test_an_open_promise_produces_the_chase_card():
    user = _user()
    firm = _firm(user)
    _chatted_with_promise(user, firm, days_ago=10)
    card = _by_name(user)["Priya Nair"]
    assert card["action"] == "promised_followup"
    assert "an intro to Dana Reed" in card["reason"]
    assert card["label"] == "Chase the offer"


def test_a_debrief_with_no_intro_on_it_is_not_a_promise():
    """A debrief is not a promise. Only the intro question records the
    CONTACT committing to an action; `tracked_date` is a fact they mentioned
    and `advocate_answer` is the student's read of them."""
    user = _user()
    firm = _firm(user)
    contact = Contact.all_objects.create(
        user=user, name="Priya Nair", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    chat = _touch(user, contact, "chat", days_ago=50)
    _touch(user, contact, "thank_you", days_ago=49)
    ChatDebrief.all_objects.create(
        user=user, contact=contact, touch=chat, advocate_answer="yes",
    )
    assert _by_name(user)["Priya Nair"]["action"] == "keep_warm"


def test_a_chase_closes_the_promise():
    """Once the student sends something that could have chased it, the marker
    stops being passed and the card goes quiet."""
    user = _user()
    firm = _firm(user)
    contact, _chat = _chatted_with_promise(user, firm, days_ago=30)
    _touch(user, contact, "follow_up", days_ago=1)
    assert "Priya Nair" not in _by_name(user)


def test_the_thank_you_does_not_close_the_promise():
    """The cadence demands a thank-you within 24 hours of the same chat, so
    counting it would close every promise the day after it was made and the
    branch could never fire for a student who did what the queue said."""
    user = _user()
    firm = _firm(user)
    _chatted_with_promise(user, firm, days_ago=10)
    assert _by_name(user)["Priya Nair"]["action"] == "promised_followup"


def test_a_dismissed_debrief_is_not_a_promise():
    user = _user()
    firm = _firm(user)
    contact, chat = _chatted_with_promise(user, firm, days_ago=50)
    ChatDebrief.all_objects.filter(contact=contact).update(dismissed=True)
    assert _by_name(user)["Priya Nair"]["action"] == "keep_warm"


def test_every_tunable_cadence_key_still_has_a_label():
    """A key in TUNABLE_CADENCE_PARAMS with no CADENCE_LABELS entry is an
    immediate 500 on the Settings page (SYNTHESIS-PLAN.md A4). The new
    promise window is a product constant and deliberately NOT tunable, so
    this must still hold exactly."""
    for key in TUNABLE_CADENCE_PARAMS:
        assert key in CADENCE_LABELS, key
    assert "promised_followup_after_days" not in TUNABLE_CADENCE_PARAMS
    assert cadence.CADENCE_DEFAULTS["promised_followup_after_days"] == 7


# ---------------------------------------------------------------------------
# WS-CRM-10 — the cold ask's seniority ceiling.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role,expected", [
    ("Managing Director, GIB", "md"),
    ("MD", "md"),
    ("Partner", "md"),
    ("Head of Campus Recruiting", "md"),
    ("Executive Director", "director"),
    ("Director, Equity Research", "director"),
    ("Vice President", "vp"),
    ("VP, M&A", "vp"),
    ("Associate", "associate"),
    ("Summer Analyst", "analyst"),
    ("", ""),
    (None, ""),
    ("Something Nobody Ranked", ""),
])
def test_seniority_reads_the_rung_or_says_nothing(role, expected):
    assert rel.seniority(role) == expected


def _cold_card(**contact):
    base = {"warmth": "cold", "role": "Managing Director"}
    base.update(contact)
    return {"action": "first_outreach", "contact": base}


def test_a_cold_md_is_halved():
    """Referrals flow downward: a VP referred nearly every networking email
    to an analyst, and no MD answers a cold pre-analyst who is not already in
    process (`research-networking-norms.md §2b`, Grade A)."""
    assert rel.senior_cold_factor(_cold_card()) == 0.5


def test_an_alum_md_is_not():
    """The ceiling is a function of connection strength: cold goes to
    analysts and associates, an alum can go to any level (§2d)."""
    assert rel.senior_cold_factor(_cold_card(school_affiliation=True)) == 1.0


def test_a_replied_md_is_not():
    assert rel.senior_cold_factor(_cold_card(warmth="replied")) == 1.0


def test_an_analyst_is_not():
    assert rel.senior_cold_factor(_cold_card(role="Summer Analyst")) == 1.0


def test_a_recruiter_is_not():
    """A senior recruiter is the one person a cold note is supposed to
    reach. `is_recruiting_contact` is the existing definition of that."""
    assert rel.senior_cold_factor(
        _cold_card(role="Head of Campus Recruiting")) == 1.0


def test_a_blank_role_scores_exactly_as_before():
    """P3, and the measured shape: 137 of the founder's 265 live rows carry
    no role text, and all 92 of his cold/no_reply rows do."""
    assert rel.senior_cold_factor(_cold_card(role="")) == 1.0
    assert rel.senior_cold_factor(_cold_card(role=None)) == 1.0


def test_the_multiplier_only_touches_the_two_cold_asks():
    for action in ("advance", "keep_warm", "maintain", "thank_you", "reping",
                   "confirm_chat", "park", "promised_followup"):
        card = _cold_card()
        card["action"] = action
        assert rel.senior_cold_factor(card) == 1.0, action


def test_expected_value_carries_the_multiplier():
    card = _cold_card()
    card.update({"relevance": rel.REL_TIERED, "relevance_tier": 1})
    junior = {**card, "contact": {**card["contact"], "role": "Analyst"}}
    assert rel.expected_value(card) == pytest.approx(
        rel.expected_value(junior) * 0.5)


# ---------------------------------------------------------------------------
# WS-CRM-11 — season, derived and never named.
# ---------------------------------------------------------------------------
def test_no_observations_means_no_mode_and_no_change():
    """P3: a student whose firms carry no measured activity gets exactly the
    order they had before this rule existed."""
    user = _user()
    _firm(user)
    assert rel.season_mode(user) is None
    assert rel.season_factor({"action": "first_outreach"}, None) == 1.0


def test_no_tiered_firms_means_no_mode():
    assert rel.season_mode(_user()) is None


def test_the_two_modes_weigh_the_two_cold_moves_oppositely():
    early = rel.SEASON_EARLY
    crowd = rel.SEASON_CROWD
    assert rel.season_factor({"action": "first_outreach"}, early) > 1.0
    assert rel.season_factor({"action": "follow_up"}, early) < 1.0
    assert rel.season_factor({"action": "first_outreach"}, crowd) < 1.0
    assert rel.season_factor({"action": "follow_up"}, crowd) > 1.0
    assert rel.season_factor({"action": "advance"}, crowd) > 1.0
    assert rel.season_factor({"action": "keep_warm"}, crowd) > 1.0


def test_no_month_is_named_anywhere_in_relevance():
    """The peak moved from March-to-May in 2021 to about November-to-January
    in 2026, and McKinsey's undergraduate deadline moved 3.5 months between
    consecutive cycles. Any constant is wrong for at least one firm-role pair
    within twelve months, so the module may not contain one."""
    import inspect

    source = inspect.getsource(rel).lower()
    for month in ("january", "february", "march", "april", "may 20", "june",
                  "july", "august", "september", "october", "november",
                  "december"):
        assert month not in source, month


# ---------------------------------------------------------------------------
# WS-CRM-12 — the advocate as an event.
# ---------------------------------------------------------------------------
def test_a_recorded_would_advocate_answer_promotes_nobody():
    """An opinion recorded in a debrief is not a referral. Three of the
    founder's debriefs answer "would advocate: yes" and none of them has ever
    moved a contact."""
    user = _user()
    firm = _firm(user)
    contact = Contact.all_objects.create(
        user=user, name="Priya Nair", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    chat = _touch(user, contact, "chat", days_ago=3)
    debrief, _made = debrief_svc.record(user, chat, advocate_answer="yes")
    contact.refresh_from_db()
    assert debrief.advocate_answer == "yes"
    assert debrief.promoted is False
    assert contact.warmth == "chatted"
    assert not Touch.all_objects.filter(contact=contact, kind=REFERRAL_KIND).exists()


def test_taking_the_promotion_writes_a_dated_referral_event():
    user = _user()
    firm = _firm(user)
    contact = Contact.all_objects.create(
        user=user, name="Priya Nair", firm=firm,
        warmth="chatted", thread_state="chat_done",
    )
    chat = _touch(user, contact, "chat", days_ago=3)
    debrief, _ = debrief_svc.record(user, chat, advocate_answer="yes")

    debrief_svc.promote(debrief)
    contact.refresh_from_db()
    debrief.refresh_from_db()

    assert contact.warmth == "advocate"
    assert debrief.promoted is True
    referral = Touch.all_objects.get(contact=contact, kind=REFERRAL_KIND)
    # Dated at the CHAT, not at the click: the promise was made then, and the
    # ledger has to read in the order things happened.
    assert referral.ts == chat.ts


def test_promoting_twice_is_a_no_op():
    user = _user()
    firm = _firm(user)
    contact = Contact.all_objects.create(
        user=user, name="Priya Nair", firm=firm, warmth="chatted",
    )
    chat = _touch(user, contact, "chat", days_ago=3)
    debrief, _ = debrief_svc.record(user, chat, advocate_answer="yes")
    debrief_svc.promote(debrief)
    assert debrief_svc.promote(debrief) == {}
    assert Touch.all_objects.filter(contact=contact, kind=REFERRAL_KIND).count() == 1


def test_a_referral_is_not_counted_as_the_students_own_work():
    """It records what the CONTACT did. Counting it in the pace ring would
    credit the student for somebody else's favour, which is the exact
    over-claim the bulk-received exclusion was written to stop."""
    from crm.today import FIRM_PACE_TOUCH_KINDS, PACE_TOUCH_KINDS

    assert REFERRAL_KIND not in PACE_TOUCH_KINDS
    assert REFERRAL_KIND not in FIRM_PACE_TOUCH_KINDS


# ---------------------------------------------------------------------------
# WS-CRM-13 — the Hong Kong overlay and the WeChat channel.
# ---------------------------------------------------------------------------
def test_a_single_market_hk_student_gets_the_overlay():
    user = _user(regions=["hk"])
    params = _cadence_params(user)
    assert params["max_cold_touches"] == 1
    assert params["followup_after_business_days"] == 10


def test_a_two_market_student_gets_the_global_defaults_byte_for_byte():
    """The founder is hk+us. His queue must be unchanged."""
    assert _cadence_params(_user(regions=["hk", "us"])) == {}
    assert _cadence_params(_user(email="us@example.com", regions=["us"])) == {}


def test_the_students_own_setting_beats_the_overlay():
    """P2: the overlay is a default, not a rule."""
    user = _user(regions=["hk"], cadence_params={"max_cold_touches": 2})
    assert _cadence_params(user)["max_cold_touches"] == 2


def test_the_overlay_only_names_keys_the_whitelist_knows():
    for region, overrides in REGION_CADENCE_OVERLAY.items():
        for key, value in overrides.items():
            assert key in TUNABLE_CADENCE_PARAMS, (region, key)
            low, high = TUNABLE_CADENCE_PARAMS[key]
            assert low <= value <= high, (region, key, value)


def test_wechat_is_loggable_and_counts_for_the_clock():
    user = _user()
    firm = _firm(user)
    contact = Contact.all_objects.create(user=user, name="Wen Li", firm=firm)
    touch = _touch(user, contact, "outreach", channel="wechat", days_ago=1)
    assert touch.channel == "wechat"
    # Not clock-silent: a WeChat message is a real interaction, which is the
    # whole reason the value exists (`research-hongkong.md §6`).
    assert "wechat" not in cadence._CLOCK_SILENT_KINDS


def test_the_channel_vocabulary_keeps_only_what_is_used():
    """Measured across all 544 of the founder's touches: email 357, NULL 179,
    coffee_chat 6, linkedin 2, and zero on call, event or other."""
    codes = [code for code, _label in CHANNEL_LABELS]
    assert codes == ["email", "linkedin", "wechat", "coffee_chat"]


def test_the_log_touch_form_accepts_wechat_and_refuses_a_retired_value(client):
    user = _user()
    firm = _firm(user)
    contact = Contact.all_objects.create(user=user, name="Wen Li", firm=firm)
    client.force_login(user)
    url = f"/app/contacts/{contact.id}/touch/"
    good = client.post(url, {"kind": "outreach", "channel": "wechat"})
    assert good.status_code == 200
    assert Touch.all_objects.filter(contact=contact, channel="wechat").exists()
    bad = client.post(url, {"kind": "outreach", "channel": "event"})
    assert bad.status_code == 200
    assert not Touch.all_objects.filter(contact=contact, channel="event").exists()


# ---------------------------------------------------------------------------
# WS-CRM-18 — the apply-only gate.
# ---------------------------------------------------------------------------
def _assessment_card(action="first_outreach", **kw):
    contact = {"warmth": "cold", "recruiting_style": "assessment"}
    contact.update(kw)
    return {"action": action, "contact": contact, "owed_reply": False}


def test_a_cold_first_outreach_at_an_assessment_firm_is_marked():
    assert rel.apply_only(_assessment_card()) is True
    assert rel.apply_only(_assessment_card("follow_up")) is True


def test_an_owed_reply_at_the_same_firm_is_not():
    """Answering a person who wrote to you is not networking, and
    `contact_relevance`'s own inbound override already makes that argument."""
    card = _assessment_card()
    card["owed_reply"] = True
    assert rel.apply_only(card) is False


def test_a_thank_you_and_a_confirm_chat_at_the_same_firm_are_not():
    for action in ("thank_you", "confirm_chat", "advance", "reping",
                   "keep_warm", "maintain"):
        assert rel.apply_only(_assessment_card(action)) is False, action


def test_a_campus_firm_is_never_marked():
    assert rel.apply_only(_assessment_card(recruiting_style="campus")) is False
    assert rel.apply_only(_assessment_card(recruiting_style="")) is False


def test_the_copy_never_says_networking_hurts():
    """No source shows networking is counterproductive at these firms;
    `research-st-quant.md` Q3 notes that explicitly. The copy says how the
    firm hires and stops."""
    text = f"{rel.APPLY_ONLY_LABEL} {rel.APPLY_ONLY_REASON}".lower()
    assert "assessment" in text
    for banned in ("hurt", "harm", "counterproductive", "waste", "annoy",
                   "do not network", "don't network"):
        assert banned not in text, banned


def test_the_queue_marks_the_card_and_keeps_it(client):
    """P4: mark, never drop. The card stays and says why."""
    user = _user()
    firm = _firm(user, slug="janestreet", name="Jane Street", style="assessment")
    contact = Contact.all_objects.create(user=user, name="Sam Trader", firm=firm)
    _touch(user, contact, "outreach", days_ago=20)
    card = _by_name(user)["Sam Trader"]
    assert card["apply_only"] is True
    assert card["label"] == rel.APPLY_ONLY_LABEL
    assert card["reason"] == rel.APPLY_ONLY_REASON


def test_the_same_queue_at_a_campus_firm_is_untouched():
    user = _user()
    firm = _firm(user)
    contact = Contact.all_objects.create(user=user, name="Sam Banker", firm=firm)
    _touch(user, contact, "outreach", days_ago=20)
    assert "apply_only" not in _by_name(user)["Sam Banker"]


# ---------------------------------------------------------------------------
# WS-CRM-19 — send-window hints per contact market.
# ---------------------------------------------------------------------------
def _with_contact(user, region):
    firm = _firm(user)
    return Contact.all_objects.create(
        user=user, name="Wen Li", firm=firm, region=region,
    )


def test_the_hint_renders_for_an_st_student_with_an_hk_contact():
    user = _user(tracks=["st"])
    _with_contact(user, "hk")
    _actions, contacts = _build_actions(user)
    hints = _send_windows(user, contacts, timezone.localdate())
    assert [h["market"] for h in hints] == ["HK"]
    assert hints[0]["count"] == 1
    assert hints[0]["good"] and hints[0]["avoid"]


def test_a_blank_region_contact_renders_no_hint():
    """P3, and the measured shape: 94 of the founder's 265 live rows state no
    region, and every one of them says nothing rather than guessing."""
    user = _user(tracks=["st"])
    _with_contact(user, "")
    _actions, contacts = _build_actions(user)
    assert _send_windows(user, contacts, timezone.localdate()) == []


def test_a_non_st_student_renders_no_hint():
    """The source is about a trading floor's day. An IB student's afternoon
    has no market close in it."""
    user = _user(tracks=["ib"])
    _with_contact(user, "hk")
    _actions, contacts = _build_actions(user)
    assert _send_windows(user, contacts, timezone.localdate()) == []


def test_the_hint_never_reorders_the_queue():
    """It is a send-time rule, not a cadence rule, so it changes copy and
    never the order."""
    user = _user(tracks=["st"])
    firm = _firm(user)
    for i, region in enumerate(("hk", "us", "")):
        c = Contact.all_objects.create(
            user=user, name=f"Person {i}", firm=firm, region=region,
        )
        _touch(user, c, "outreach", days_ago=20)
    actions, contacts = _build_actions(user)
    before = [a["contact"]["name"] for a in actions]
    bar = _daybar([], timezone.localtime(timezone.now()),
                  hints=_send_windows(user, contacts, timezone.localdate()))
    assert bar["show_hints"] is True
    after, _ = _build_actions(user)
    assert [a["contact"]["name"] for a in after] == before


def test_the_daybar_says_nothing_when_there_is_nothing_to_say():
    bar = _daybar([], timezone.localtime(timezone.now()))
    assert bar["hints"] == []
    assert bar["show_hints"] is False
