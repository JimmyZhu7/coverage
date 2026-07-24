"""Golden-fixture tests for coverage_domain.scoring — the fit-score engine.

New code, no upstream port (the existing confidence.py scores sources, not
people). These tests pin the four load-bearing guarantees from the brief:

  1. DETERMINISM — identical inputs produce a byte-identical serialized
     result and an identical inputs_hash; a changed input (including as_of)
     changes the hash.
  2. RECENCY DECAY — the ~45-day half-life is exact: a reply 45d ago carries
     half the weight of one today; 90d a quarter; ~240d ~2.5%.
  3. DEPTH READS THE LOG, NOT THE STORED WARMTH COLUMN — a contact stored
     as 'advocate' whose log shows only a reply scores Depth as 'replied'.
  4. Firm score — advocate overweighting in network strength; momentum
     trend; and the "opens soon, zero warm contacts -> low, and says so"
     timeline-readiness case.

Pure functions over plain dicts — no DB, no clock, no fixtures needed.
"""

import json
from datetime import datetime, timedelta, timezone

from coverage_domain import scoring

UTC = timezone.utc
AS_OF = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def touch(cid, kind, days_ago, note=None):
    return {"contact_id": cid, "kind": kind, "ts": AS_OF - timedelta(days=days_ago), "note": note}


# --------------------------------------------------------------------------
# 1. Determinism.
# --------------------------------------------------------------------------
def _golden_contact():
    c = {"id": 1, "role": "Vice President", "school_affiliation": "Alpha University"}
    touches = [
        touch(1, "outreach", 40),
        touch(1, "reply_received", 38),
        touch(1, "chat_scheduled", 30),
        touch(1, "chat", 12),
    ]
    return c, touches


def test_determinism_same_inputs_byte_identical_snapshot():
    c, touches = _golden_contact()
    a = scoring.score_contact(c, touches, as_of=AS_OF)
    b = scoring.score_contact(c, list(reversed(touches)), as_of=AS_OF)  # order must not matter
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["inputs_hash"] == b["inputs_hash"]


def test_inputs_hash_changes_with_as_of():
    c, touches = _golden_contact()
    a = scoring.score_contact(c, touches, as_of=AS_OF)
    later = scoring.score_contact(c, touches, as_of=AS_OF + timedelta(days=1))
    assert a["inputs_hash"] != later["inputs_hash"]


def test_inputs_hash_changes_with_data():
    c, touches = _golden_contact()
    a = scoring.score_contact(c, touches, as_of=AS_OF)
    b = scoring.score_contact(c, touches + [touch(1, "chat", 1)], as_of=AS_OF)
    assert a["inputs_hash"] != b["inputs_hash"]


def test_contact_golden_values():
    """A fully hand-computable case pins the exact axis math and reasoning.

    role=Analyst, no school, one reply exactly at as_of:
      depth          reply_received -> level 1 -> 40
      responsiveness ratio 1.0, no measurable latency -> 100*(0.6*1) = 60
      recency        0 days -> 0.5^0 * 100 = 100
      leverage       'analyst' -> 30, no school bonus -> 30
      composite      .3*40 + .2*60 + .3*100 + .2*30 = 60.0 -> band 'warm'
    """
    c = {"id": 9, "role": "Analyst", "school_affiliation": None}
    res = scoring.score_contact(c, [touch(9, "reply_received", 0)], as_of=AS_OF)
    assert res["axes"]["depth"]["score"] == 40.0
    assert res["axes"]["depth"]["level"] == 1
    assert res["axes"]["responsiveness"]["score"] == 60.0
    assert res["axes"]["recency"]["score"] == 100.0
    assert res["axes"]["leverage"]["score"] == 30.0
    assert res["composite"] == 60.0
    assert res["band"] == "warm"
    assert res["reasoning"] == "replied, no chat yet; last today"
    assert res["params_version"] == "scoring-v1"


# --------------------------------------------------------------------------
# 2. Recency decay — the 45-day half-life, exactly.
# --------------------------------------------------------------------------
def test_recency_half_life_is_45_days():
    c = {"id": 1, "role": None}

    def recency(days_ago):
        r = scoring.score_contact(c, [touch(1, "reply_received", days_ago)], as_of=AS_OF)
        return r["axes"]["recency"]["score"]

    assert recency(0) == 100.0
    assert recency(45) == 50.0       # one half-life
    assert recency(90) == 25.0       # two half-lives
    assert recency(240) == 2.5       # ~8 months -> ~2.5% (the brief's example)


def test_recency_zero_when_no_meaningful_touch():
    """Only outbound touches -> no meaningful interaction -> recency 0."""
    c = {"id": 1, "role": None}
    r = scoring.score_contact(c, [touch(1, "outreach", 2), touch(1, "follow_up", 1)], as_of=AS_OF)
    assert r["axes"]["recency"]["score"] == 0.0


def test_decay_lives_only_in_recency():
    """Aging every touch by 100 days must move ONLY the recency axis; depth,
    responsiveness and leverage are time-invariant."""
    c = {"id": 1, "role": "Analyst"}
    fresh = [touch(1, "outreach", 5), touch(1, "reply_received", 4), touch(1, "chat", 3)]
    old = [touch(1, "outreach", 105), touch(1, "reply_received", 104), touch(1, "chat", 103)]
    rf = scoring.score_contact(c, fresh, as_of=AS_OF)["axes"]
    ro = scoring.score_contact(c, old, as_of=AS_OF)["axes"]
    assert rf["depth"]["score"] == ro["depth"]["score"]
    assert rf["responsiveness"]["score"] == ro["responsiveness"]["score"]
    assert rf["leverage"]["score"] == ro["leverage"]["score"]
    assert ro["recency"]["score"] < rf["recency"]["score"]


# --------------------------------------------------------------------------
# 3. Depth reads the log, not the stored warmth column.
# --------------------------------------------------------------------------
def test_depth_ignores_stored_warmth_column():
    """Contact stored as 'advocate' but the log shows only a reply -> Depth
    is 'replied' (level 1), NOT advocate."""
    c = {"id": 1, "role": None, "warmth": "advocate", "thread_state": "advocate"}
    r = scoring.score_contact(c, [touch(1, "reply_received", 3)], as_of=AS_OF)
    assert r["axes"]["depth"]["level"] == 1
    assert r["axes"]["depth"]["score"] == 40.0


def test_depth_advocate_from_manual_override_audit_touch():
    """The one place advocacy is evidenced in the append-only log:
    pipeline.set_state's audit touch, note 'manual override: warmth=advocate'."""
    c = {"id": 1, "role": None}
    touches = [
        touch(1, "reply_received", 20),
        touch(1, "chat", 15),
        touch(1, "manual_override", 10, note="manual override: warmth=advocate"),
    ]
    r = scoring.score_contact(c, touches, as_of=AS_OF)
    assert r["axes"]["depth"]["level"] == 3
    assert r["axes"]["depth"]["score"] == 100.0


def test_depth_levels_progression():
    c = {"id": 1, "role": None}
    none = scoring.score_contact(c, [touch(1, "outreach", 1)], as_of=AS_OF)
    replied = scoring.score_contact(c, [touch(1, "reply_received", 1)], as_of=AS_OF)
    chatted = scoring.score_contact(c, [touch(1, "chat", 1)], as_of=AS_OF)
    assert none["axes"]["depth"]["level"] == 0
    assert replied["axes"]["depth"]["level"] == 1
    assert chatted["axes"]["depth"]["level"] == 2


# --------------------------------------------------------------------------
# Responsiveness and leverage.
# --------------------------------------------------------------------------
def test_responsiveness_rewards_fast_replies():
    c = {"id": 1, "role": None}
    fast = [touch(1, "outreach", 10), touch(1, "reply_received", 9.5),   # ~12h
            touch(1, "outreach", 5), touch(1, "reply_received", 4.5)]
    slow = [touch(1, "outreach", 10), touch(1, "reply_received", 4)]     # ~6 days
    none = [touch(1, "outreach", 10), touch(1, "follow_up", 5)]          # never replied
    rf = scoring.score_contact(c, fast, as_of=AS_OF)["axes"]["responsiveness"]["score"]
    rs = scoring.score_contact(c, slow, as_of=AS_OF)["axes"]["responsiveness"]["score"]
    rn = scoring.score_contact(c, none, as_of=AS_OF)["axes"]["responsiveness"]["score"]
    assert rf > rs > rn
    assert rn == 0.0


def test_leverage_seniority_and_school_bonus():
    md = scoring.score_contact({"id": 1, "role": "Managing Director"}, [], as_of=AS_OF)
    analyst = scoring.score_contact({"id": 2, "role": "Analyst"}, [], as_of=AS_OF)
    unknown = scoring.score_contact({"id": 3, "role": None}, [], as_of=AS_OF)
    alum = scoring.score_contact(
        {"id": 4, "role": "Analyst", "school_affiliation": "Alpha University"}, [], as_of=AS_OF)
    assert md["axes"]["leverage"]["score"] == 100.0
    assert analyst["axes"]["leverage"]["score"] == 30.0
    assert unknown["axes"]["leverage"]["score"] == 30.0
    assert alum["axes"]["leverage"]["score"] == 50.0   # 30 + 20 school bonus


def test_leverage_vp_not_matched_as_president():
    """'Vice President' must score as mid (VP), not senior (president)."""
    vp = scoring.score_contact({"id": 1, "role": "Vice President"}, [], as_of=AS_OF)
    assert vp["axes"]["leverage"]["score"] == 65.0


def test_custom_params_require_version():
    import pytest
    with pytest.raises(ValueError):
        scoring.score_contact({"id": 1, "role": None}, [], as_of=AS_OF, params={"depth_level_scores": {}})


# --------------------------------------------------------------------------
# 4. Firm Fit Score.
# --------------------------------------------------------------------------
def _user(regions=("us",), tracks=("ib",), needs_sponsorship=False):
    return {"id": 1, "regions": list(regions), "tracks": list(tracks),
            "needs_sponsorship": needs_sponsorship}


def _firm(regions=("us",), tracks=("ib",), sponsors=True):
    return {"id": "acme", "regions": list(regions), "tracks": list(tracks), "sponsors": sponsors}


def test_network_advocate_overweighting():
    """Two contacts with identical histories except one carries the advocate
    audit touch: the advocate firm scores higher network strength, and the
    advocate is counted."""
    common = lambda cid: [touch(cid, "reply_received", 20), touch(cid, "chat", 10)]
    plain_contacts = [{"id": 1, "role": "Analyst"}]
    plain_touches = common(1)

    adv_contacts = [{"id": 1, "role": "Analyst"}]
    adv_touches = common(1) + [touch(1, "manual_override", 5, note="manual override: warmth=advocate")]

    plain = scoring.score_firm(_user(), _firm(), plain_contacts, plain_touches, as_of=AS_OF)
    adv = scoring.score_firm(_user(), _firm(), adv_contacts, adv_touches, as_of=AS_OF)
    assert adv["axes"]["network"]["score"] > plain["axes"]["network"]["score"]
    assert adv["axes"]["network"]["advocates"] == 1
    assert plain["axes"]["network"]["advocates"] == 0


def test_network_two_advocates_hit_target():
    """The advocate_target=2 yardstick: with two advocates in place, network
    strength approaches its ceiling (it equals the advocates' own warmth
    composite — the advocate_weight cancels exactly at the target count, so
    2 well-tended advocates read as a near-max network)."""
    contacts = [{"id": 1, "role": "Managing Director", "school_affiliation": "Alpha"},
                {"id": 2, "role": "Managing Director", "school_affiliation": "Alpha"}]
    touches = []
    for cid in (1, 2):
        touches += [touch(cid, "reply_received", 8), touch(cid, "chat", 4),
                    touch(cid, "manual_override", 2, note="manual override: warmth=advocate")]
    res = scoring.score_firm(_user(), _firm(), contacts, touches, as_of=AS_OF)
    assert res["axes"]["network"]["score"] >= 90.0
    assert res["axes"]["network"]["advocates"] == 2
    # One advocate alone is materially weaker than two.
    one = scoring.score_firm(_user(), _firm(), contacts[:1], touches[:3], as_of=AS_OF)
    assert one["axes"]["network"]["score"] < res["axes"]["network"]["score"]


def test_no_contacts_network_zero():
    res = scoring.score_firm(_user(), _firm(), [], [], as_of=AS_OF)
    assert res["axes"]["network"]["score"] == 0.0
    assert "no contacts yet" in res["reasoning"]


def test_momentum_trend_up_vs_stale():
    contacts = [{"id": 1, "role": None}]
    burst = [touch(1, "outreach", 6), touch(1, "reply_received", 4), touch(1, "chat", 2)]
    stale = [touch(1, "outreach", 44), touch(1, "reply_received", 40), touch(1, "chat", 35)]
    up = scoring.score_firm(_user(), _firm(), contacts, burst, as_of=AS_OF)["axes"]["momentum"]["score"]
    down = scoring.score_firm(_user(), _firm(), contacts, stale, as_of=AS_OF)["axes"]["momentum"]["score"]
    assert up == 100.0
    assert down == 0.0


def test_timeline_opens_soon_zero_warm_contacts_scores_low_and_says_so():
    """The brief's headline case: app opens in ~30 days with zero warm
    contacts -> timeline readiness LOW, and the reasoning explains why."""
    firm_dates = [{
        "firm_id": "acme", "event_kind": "app_open", "region": "us",
        "date": (AS_OF + timedelta(days=30)).date(), "confidence": "confirmed_official",
    }]
    contacts = [{"id": 1, "role": "Analyst"}]
    touches = [touch(1, "outreach", 3)]  # cold: no reply -> not warm
    res = scoring.score_firm(_user(), _firm(), contacts, touches, firm_dates, as_of=AS_OF)
    tl = res["axes"]["timeline"]
    assert tl["score"] < 40.0
    assert tl["warm_contacts"] == 0
    assert tl["next_event"] == "app_open"
    assert "opens" in res["reasoning"]
    assert "no warm contacts" in res["reasoning"]


def test_timeline_ready_when_advocates_in_place():
    """Same imminent opening, but with two advocates -> readiness high."""
    firm_dates = [{
        "firm_id": "acme", "event_kind": "app_open", "region": "us",
        "date": (AS_OF + timedelta(days=30)).date(), "confidence": "confirmed_official",
    }]
    contacts = [{"id": 1, "role": "MD"}, {"id": 2, "role": "MD"}]
    touches = []
    for cid in (1, 2):
        touches += [touch(cid, "reply_received", 8), touch(cid, "chat", 4),
                    touch(cid, "manual_override", 2, note="manual override: warmth=advocate")]
    res = scoring.score_firm(_user(), _firm(), contacts, touches, firm_dates, as_of=AS_OF)
    assert res["axes"]["timeline"]["score"] >= 90.0


def test_timeline_neutral_with_no_confirmed_date():
    res = scoring.score_firm(_user(), _firm(), [{"id": 1, "role": None}], [], [], as_of=AS_OF)
    assert res["axes"]["timeline"]["score"] == 60.0
    assert res["axes"]["timeline"]["next_event"] is None


def test_structural_rules_region_track_sponsorship():
    contacts, touches = [], []
    match = scoring.score_firm(_user(regions=("us",), tracks=("ib",)),
                               _firm(regions=("us",), tracks=("ib",)), contacts, touches, as_of=AS_OF)
    mismatch = scoring.score_firm(_user(regions=("hk",), tracks=("st",)),
                                  _firm(regions=("us",), tracks=("ib",)), contacts, touches, as_of=AS_OF)
    assert match["axes"]["structural"]["score"] > mismatch["axes"]["structural"]["score"]
    assert match["axes"]["structural"]["method"] == "rules_v1"
    assert match["axes"]["structural"]["region_overlap"] is True
    assert mismatch["axes"]["structural"]["region_overlap"] is False


def test_structural_sponsorship_conflict_penalized():
    """Needs sponsorship + firm doesn't sponsor -> sponsorship component fails
    and the reasoning flags it."""
    contacts, touches = [], []
    ok = scoring.score_firm(_user(needs_sponsorship=True), _firm(sponsors=True),
                            contacts, touches, as_of=AS_OF)
    bad = scoring.score_firm(_user(needs_sponsorship=True), _firm(sponsors=False),
                             contacts, touches, as_of=AS_OF)
    assert ok["axes"]["structural"]["sponsorship_ok"] is True
    assert bad["axes"]["structural"]["sponsorship_ok"] is False
    assert bad["axes"]["structural"]["score"] < ok["axes"]["structural"]["score"]
    assert "no sponsorship" in bad["reasoning"]


def test_firm_determinism_snapshot():
    firm_dates = [{
        "firm_id": "acme", "event_kind": "app_close", "region": "us",
        "date": (AS_OF + timedelta(days=20)).date(), "confidence": "confirmed_official",
    }]
    contacts = [{"id": 1, "role": "MD"}, {"id": 2, "role": "Analyst"}]
    touches = [touch(1, "reply_received", 10), touch(1, "chat", 5), touch(2, "outreach", 3)]
    a = scoring.score_firm(_user(), _firm(), contacts, touches, firm_dates, as_of=AS_OF)
    # Reordering contacts and touches must not change the result.
    b = scoring.score_firm(_user(), _firm(), list(reversed(contacts)), list(reversed(touches)),
                           firm_dates, as_of=AS_OF)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["inputs_hash"] == b["inputs_hash"]
