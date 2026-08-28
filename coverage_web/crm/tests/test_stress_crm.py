"""Adversarial invariant suite for the CRM's view-layer half of the cadence:
`crm.coverage`, `crm.relevance`, `crm.recruitment`, `crm.campaigns`.

Companion to `coverage_domain/tests/test_stress_invariants.py`, which does the
same job for the pure engines. Same discipline and the same reasoning about
tooling: NO `hypothesis` (see that file's header for the three reasons), so
the finite input spaces here — relevance reasons x warmth x action kinds,
verdict ladder rungs, gap states x tiers — are walked EXHAUSTIVELY, and the
one genuinely unbounded question (does a sort depend on input order?) uses a
seeded shuffle so a counterexample reproduces.

Most of this file needs no database: `crm.coverage` is pure by design, and
`crm.relevance` / `crm.recruitment` were written to be pure functions of a
plain dict precisely so they could be tested without one. The `django_db`
marker appears only where a query is genuinely the thing under test.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from crm import campaigns, coverage, recruitment, relevance

SEED = 20260827
TODAY = date(2026, 8, 27)


# ===========================================================================
# INVARIANT 1 — `rank_gaps` is a strict weak ordering AND a total one: the
# same firms in any input order produce the same strip.
#
# Its docstring promises "the strip is stable render to render". Before the
# `firm_id` tiebreak that was true only down to the fourth key: six firms
# identical on all four, shuffled 50 times, produced 47 distinct strips, and
# `crm.views` builds the list from a queryset with no ORDER BY.
# ===========================================================================
def _firm(fid, **kw):
    base = {"firm_id": fid, "name": "Acme", "tier": 1, "warmths": [],
            "app_close": None, "open": 0}
    base.update(kw)
    return base


def test_rank_gaps_is_invariant_under_input_permutation():
    rng = random.Random(SEED)
    firms = [
        _firm(i,
              name=rng.choice(["Acme", "Acme", "Beta"]),
              tier=rng.choice([1, 2, 3]),
              warmths=rng.choice([[], ["cold"], ["replied"], ["advocate"],
                                  ["cold", "advocate"]]),
              app_close=rng.choice([None, TODAY + timedelta(days=5),
                                    TODAY + timedelta(days=40)]),
              open=rng.choice([0, 0, 3]))
        for i in range(30)
    ]
    expected = [g["firm_id"] for g in coverage.rank_gaps(firms, today=TODAY, limit=99)]
    for _ in range(60):
        shuffled = firms[:]
        rng.shuffle(shuffled)
        got = [g["firm_id"] for g in coverage.rank_gaps(shuffled, today=TODAY, limit=99)]
        assert got == expected


def test_two_indistinguishable_firms_always_rank_the_same_way_round():
    a, b = _firm(1), _firm(2)
    assert [g["firm_id"] for g in coverage.rank_gaps([a, b], today=TODAY)] == [1, 2]
    assert [g["firm_id"] for g in coverage.rank_gaps([b, a], today=TODAY)] == [1, 2]


def test_rank_gaps_sort_key_is_a_strict_weak_ordering_over_the_whole_space():
    """Build every reachable (tier, gap state, deadline band, open count)
    combination and assert the emitted order is non-decreasing under the
    documented key — i.e. the comparator is transitive and total."""
    pool = []
    warmths_for = {"no_contacts": [], "all_cold": ["cold"],
                   "no_advocate": ["replied"], "below_target": ["advocate"]}
    for tier in (1, 2, 3):
        for state, warmths in warmths_for.items():
            for close in (None, TODAY + timedelta(days=3),
                          TODAY + timedelta(days=20), TODAY + timedelta(days=50),
                          TODAY - timedelta(days=1)):
                for opens in (0, 7):
                    pool.append(_firm(len(pool), name=f"F{len(pool) % 3}",
                                      tier=tier, warmths=warmths,
                                      app_close=close, open=opens))
    ranked = coverage.rank_gaps(pool, today=TODAY, limit=len(pool))
    keys = [(-g["exposure"], g["tier"], -g["open"], str(g["name"]), str(g["firm_id"]))
            for g in ranked]
    assert keys == sorted(keys)
    assert len({g["firm_id"] for g in ranked}) == len(ranked)


# ===========================================================================
# INVARIANT 2 — the exposure formula's ordering claims, stated in the module
# docstring, hold for every combination rather than the three examples in it.
# ===========================================================================
def test_a_covered_firm_is_never_a_gap():
    for tier in (1, 2, 3):
        for advocates in range(2, 6):
            f = _firm(1, tier=tier, warmths=["advocate"] * advocates)
            assert coverage.rank_gaps([f], today=TODAY, target=2) == []


def test_exposure_is_monotone_in_tier_for_a_fixed_gap_state():
    for state, warmths in (("no_contacts", []), ("all_cold", ["cold"]),
                           ("no_advocate", ["replied"]), ("below_target", ["advocate"])):
        scores = []
        for tier in (1, 2, 3):
            g = coverage.rank_gaps([_firm(1, tier=tier, warmths=warmths)],
                                   today=TODAY)[0]
            assert g["state"] == state
            scores.append(g["exposure"])
        assert scores == sorted(scores, reverse=True), (state, scores)


def test_a_missed_deadline_scores_the_maximum_urgency_not_the_minimum():
    """The docstring's claim, pinned: a deadline you already missed at a firm
    you have no coverage at is the MOST exposed a firm can be."""
    for days_late in (1, 30, 400):
        assert coverage.deadline_bonus(-days_late) == coverage.DEADLINE_BONUS[0][1]


@pytest.mark.parametrize("days_out,expected", [
    (0, 3), (14, 3), (15, 2), (30, 2), (31, 1), (60, 1), (61, 0), (9999, 0),
])
def test_deadline_bonus_bands_are_exact_at_every_boundary(days_out, expected):
    assert coverage.deadline_bonus(days_out) == expected


def test_deadline_bonus_of_none_is_zero_not_a_guess():
    assert coverage.deadline_bonus(None) == 0


# ===========================================================================
# INVARIANT 3 — degenerate / malformed firm rows must not take down the board.
# `Firm.sponsors` as a non-dict already caused two live 500s; the same class
# of "a field read as though its shape were guaranteed" lives in every
# caller-built dict.
# ===========================================================================
@pytest.mark.parametrize("firms", [
    [],
    [_firm(1)],
    [_firm(i) for i in range(10_000)],
    [_firm(1, tier=None)], [_firm(1, tier="1")], [_firm(1, tier=0)], [_firm(1, tier=9)],
    [_firm(1, warmths=None)], [_firm(1, warmths=())],
    [_firm(1, name=None)], [_firm(1, name="")],
    [_firm(1, open=None)], [_firm(1, open="")],
    [_firm(1, app_close=None)],
])
def test_rank_gaps_survives_degenerate_rows(firms):
    result = coverage.rank_gaps(firms, today=TODAY, limit=6)
    assert len(result) <= 6


def test_rank_gaps_survives_a_non_string_name_mixed_with_strings():
    """One integer name used to crash the whole sort with a TypeError, taking
    down the Network board rather than mislabelling one card."""
    firms = [_firm(1, name=7), _firm(2, name="Acme"), _firm(3, name=None)]
    assert len(coverage.rank_gaps(firms, today=TODAY, limit=6)) == 3


@pytest.mark.parametrize("assets", [
    None, {}, [], "nope", 0, {"advocate_target": 0}, {"advocate_target": -1},
    {"advocate_target": True}, {"advocate_target": "3"}, {"advocate_target": 2.5},
    {"advocate_target": None},
])
def test_advocate_target_never_propagates_a_nonsense_yardstick(assets):
    """A target of 0 would make every firm permanently 'covered'; a negative
    one, permanently short. Both are silent, so the fallback has to be total."""
    user = type("U", (), {"assets": assets})()
    assert coverage.advocate_target(user) == coverage.DEFAULT_ADVOCATE_TARGET


def test_advocate_target_accepts_a_real_answer():
    assert coverage.advocate_target(type("U", (), {"assets": {"advocate_target": 5}})()) == 5


@pytest.mark.parametrize("cards", [
    [], [{"advocates": None, "contact_count": 1}], [{"contact_count": 0}],
    [{"advocates": 2, "contact_count": 3}, {"advocates": None}],
])
def test_tier_cost_survives_missing_and_null_advocate_counts(cards):
    """An annotated queryset hands back `advocates=None` for a firm with no
    matching rows, which satisfies `.get("advocates", 0)` and then raises
    inside `sum`."""
    cost = coverage.tier_cost(cards, target=2)
    assert cost["firms"] == len(cards)
    assert cost["remaining"] >= 0
    assert cost["have"] >= 0


def test_tier_cost_arithmetic_is_internally_consistent():
    cards = [{"advocates": 3, "contact_count": 4}, {"advocates": 0, "contact_count": 0},
             {"advocates": 1, "contact_count": 2}]
    cost = coverage.tier_cost(cards, target=2)
    assert cost["needed"] == cost["firms"] * cost["target"]
    assert cost["remaining"] == max(0, cost["needed"] - cost["have"])
    assert cost["uncovered"] == 1


# ===========================================================================
# INVARIANT 4 — `gap_state` is total: every (warmths, advocates, target) lands
# on exactly one rung, and the rungs are ordered by how hard they are to close.
# ===========================================================================
@pytest.mark.parametrize("target", [1, 2, 3, 5])
def test_gap_state_is_total_and_points_are_monotone(target):
    seen = set()
    for advocates in range(0, 6):
        for warmths in ([], ["cold"], ["cold", "cold"], ["replied"],
                        ["chatted"], ["advocate"], ["cold", "advocate"]):
            adv = min(advocates, len(warmths))
            state = coverage.gap_state(warmths, adv, target)
            assert state in coverage.GAP_POINTS
            assert state in coverage.GAP_LABELS
            seen.add(state)
    # A firm at or above target scores zero and is not a gap; every other rung
    # scores something.
    assert coverage.GAP_POINTS[coverage.COVERED] == 0
    ladder = [coverage.NO_CONTACTS, coverage.ALL_COLD,
              coverage.NO_ADVOCATE, coverage.BELOW_TARGET, coverage.COVERED]
    points = [coverage.GAP_POINTS[s] for s in ladder]
    assert points == sorted(points, reverse=True)


def test_gap_state_accepts_a_generator_not_just_a_list():
    """`warmths` is documented as an iterable; a caller passing a generator
    must not have it consumed by the first `if`."""
    assert coverage.gap_state((w for w in ["replied"]), 0, 2) == coverage.NO_ADVOCATE
    assert coverage.gap_state((w for w in []), 0, 2) == coverage.NO_CONTACTS


# ===========================================================================
# INVARIANT 5 — relevance. Exhaustive over the gate ladder: the campaign gate
# and the recruitment gate must BOTH be beatable only by the inbound override,
# and by nothing else.
# ===========================================================================
_GATE_FLAGS = ("campaign_excluded", "recruitment_hidden")


@pytest.mark.parametrize("campaign_excluded", [True, False])
@pytest.mark.parametrize("recruitment_hidden", [True, False])
@pytest.mark.parametrize("tiered", [True, False])
@pytest.mark.parametrize("school", [True, False])
@pytest.mark.parametrize("owed_reply", [True, False])
def test_the_relevance_ladder_over_its_whole_input_space(
    campaign_excluded, recruitment_hidden, tiered, school, owed_reply
):
    contact = {
        "firm_id": 1 if tiered else 99,
        "school_affiliation": school,
        "campaign_excluded": campaign_excluded,
        "recruitment_hidden": recruitment_hidden,
    }
    got = relevance.contact_relevance(contact, {1: 1}, owed_reply=owed_reply)

    if campaign_excluded or recruitment_hidden:
        # A gated contact gets exactly one thing — an answer, if they wrote.
        expected = relevance.REL_INBOUND if owed_reply else relevance.REL_NONE
    elif tiered:
        expected = relevance.REL_TIERED
    elif school:
        expected = relevance.REL_SCHOOL
    elif owed_reply:
        expected = relevance.REL_INBOUND
    else:
        expected = relevance.REL_NONE
    assert got == expected


def test_a_gate_can_never_be_beaten_by_a_tier_or_a_school_tie():
    """The founder's rule, stated as an invariant rather than an example: no
    amount of tier makes club-panel outreach a job search, and a school tie
    does not make a professor part of recruiting."""
    for flag in _GATE_FLAGS:
        contact = {"firm_id": 1, "school_affiliation": True, flag: True}
        assert relevance.contact_relevance(contact, {1: 1}, owed_reply=False) \
            is relevance.REL_NONE


@pytest.mark.parametrize("tier", [1, 2, 3, None, "1", 99])
def test_relevance_weight_is_total_over_every_tier_shape(tier):
    """`UserFirm.tier` is nullable and the Unranked lane writes None
    deliberately. Every shape must produce a usable float, not a KeyError."""
    w = relevance.relevance_weight(relevance.REL_TIERED, tier)
    assert isinstance(w, float) and w > 0


def test_tier_weights_are_ordered_and_unranked_sits_between_tier_3_and_a_stranger():
    t1 = relevance.relevance_weight(relevance.REL_TIERED, 1)
    t2 = relevance.relevance_weight(relevance.REL_TIERED, 2)
    t3 = relevance.relevance_weight(relevance.REL_TIERED, 3)
    unranked = relevance.relevance_weight(relevance.REL_TIERED, None)
    school = relevance.relevance_weight(relevance.REL_SCHOOL, None)
    inbound = relevance.relevance_weight(relevance.REL_INBOUND, None)
    assert t1 > t2 > t3 > unranked > school > inbound > 0


# ===========================================================================
# INVARIANT 6 — expected_value. For a FIXED relevance and a FIXED "why now",
# EV is monotone non-decreasing in warmth.
#
# NOTE the claim this does NOT make. "A cold contact never outranks an engaged
# one" is NOT an invariant of this scorer and must not be asserted as one: the
# score is deliberately multiplicative across three independent terms, and
# `crm/relevance.py`'s own docstring says a strong trigger on a stranger may
# beat a weak one on an advocate. `test_a_cold_contact_can_outrank_an_advocate`
# below pins that as intended behaviour so nobody "fixes" it later.
# ===========================================================================
_WARMTH_LADDER = ("cold", "replied", "chatted", "advocate")


@pytest.mark.parametrize("rel", [relevance.REL_TIERED, relevance.REL_SCHOOL,
                                 relevance.REL_INBOUND])
@pytest.mark.parametrize("action_kind", ["advance", "follow_up", "thank_you",
                                         "keep_warm", "maintain", "first_outreach",
                                         "park", "confirm_chat", "reping"])
def test_ev_is_monotone_in_warmth_for_a_fixed_relevance_and_trigger(rel, action_kind):
    scores = []
    for warmth in _WARMTH_LADDER:
        action = {
            "contact": {"warmth": warmth},
            "relevance": rel, "relevance_tier": 1,
            "action": action_kind, "priority": 1,
        }
        scores.append(relevance.expected_value(action))
    assert scores == sorted(scores), (rel, action_kind, scores)


@pytest.mark.parametrize("warmth", _WARMTH_LADDER)
def test_ev_is_monotone_in_relevance_for_a_fixed_warmth_and_trigger(warmth):
    ladder = [
        (relevance.REL_INBOUND, None),
        (relevance.REL_SCHOOL, None),
        (relevance.REL_TIERED, None),
        (relevance.REL_TIERED, 3),
        (relevance.REL_TIERED, 2),
        (relevance.REL_TIERED, 1),
    ]
    scores = [
        relevance.expected_value({
            "contact": {"warmth": warmth}, "relevance": rel,
            "relevance_tier": tier, "action": "keep_warm", "priority": 2,
        })
        for rel, tier in ladder
    ]
    assert scores == sorted(scores), (warmth, scores)


def test_an_unanswered_inbound_outranks_every_other_trigger_at_equal_footing():
    """The product's stated rule: somebody who wrote to you and is waiting is
    the most live thing on the board."""
    base = {"contact": {"warmth": "cold"}, "relevance": relevance.REL_TIERED,
            "relevance_tier": 1}
    inbound = relevance.expected_value({**base, "owed_reply": True, "action": "advance"})
    for other in ("advance", "thank_you", "keep_warm", "maintain", "follow_up", "park"):
        assert inbound >= relevance.expected_value({**base, "action": other})


def test_a_cold_contact_can_outrank_an_advocate_and_that_is_the_design():
    """Pinned deliberately. The candidate invariant 'a cold contact never
    outranks an engaged one' is FALSE here on purpose — `expected_value` is a
    product of relevance x strength x why-now, and a tier-1 stranger who just
    wrote to you IS more urgent today than an advocate with nothing happening.
    If this test ever fails, someone has made the scorer additive or clamped
    the trigger term, and the ranking's whole thesis changed with it."""
    cold_inbound = relevance.expected_value({
        "contact": {"warmth": "cold"}, "relevance": relevance.REL_TIERED,
        "relevance_tier": 1, "owed_reply": True, "action": "advance",
    })
    advocate_idle = relevance.expected_value({
        "contact": {"warmth": "advocate"}, "relevance": relevance.REL_SCHOOL,
        "relevance_tier": None, "action": "maintain",
    })
    assert cold_inbound > advocate_idle


def test_expected_value_is_total_over_junk_action_dicts():
    """The scorer reads keys off a dict another module built. A missing or
    unrecognised value must score, not raise — a 500 on the Today page is a
    worse failure than a mis-ranked card."""
    for action in ({}, {"contact": None}, {"contact": {}},
                   {"contact": {"warmth": None}}, {"contact": {"warmth": "lukewarm"}},
                   {"relevance": "nonsense"}, {"opening": {"kind": "unknown"}},
                   {"action": "not_a_real_action"}):
        value = relevance.expected_value(action)
        assert isinstance(value, float) and value >= 0


def test_an_unknown_warmth_scores_no_higher_than_cold():
    """A warmth string the ladder does not know must not be promoted by a
    generous default."""
    cold = relevance.expected_value({
        "contact": {"warmth": "cold"}, "relevance": relevance.REL_TIERED,
        "relevance_tier": 1, "action": "keep_warm"})
    for junk in (None, "", "lukewarm", "warm", 3):
        got = relevance.expected_value({
            "contact": {"warmth": junk}, "relevance": relevance.REL_TIERED,
            "relevance_tier": 1, "action": "keep_warm"})
        assert got <= cold


# ===========================================================================
# INVARIANT 7 — recruitment. The module's stated asymmetry, enforced: a role
# naming a covered TRACK is kept whatever else the string contains, because a
# wrongly hidden banker is the expensive error.
# ===========================================================================
_REAL_SEATS = [
    # ib
    "IB Analyst", "TMT IB Associate", "M&A Analyst", "Leveraged Finance Associate",
    "Restructuring Analyst", "ECM Associate", "DCM Analyst", "Fintech IB Associate",
    "Investment Banking Summer Analyst",
    # st — every one of these carries a word the hide list bans
    "Equities Sales", "Credit Sales", "FX Sales", "Rates Sales",
    "Prime Brokerage Sales", "Structured Products Sales", "Fixed Income Sales",
    "Institutional Sales", "Securities Sales", "Cross-Asset Sales",
    "Emerging Markets Sales", "Municipal Sales", "Convertible Sales",
    "Sales & Trading Summer Analyst", "Sales and Trading Analyst",
    "Global Markets Analyst", "Equity Research Associate", "S&T Associate",
    "Derivatives Structurer", "Commodities Trader", "Foreign Exchange Analyst",
    # recruiting function
    "Campus recruiting manager", "Talent Acquisition Manager", "Recruiting",
    "University Recruiter", "Recruiter",
]

# The first three are the founder's OWN live rows, copied verbatim rather
# than paraphrased. That matters: an earlier draft of this list used the
# shorthand the module docstring uses for them ("Dornsife First-Year
# Advising"), which `_CAMPUS_ROLE_RE` does not match — the regex keys on
# "on-campus staff" and "academic advis", both of which the real row carries
# and the paraphrase does not. The test failed and the code was right. A
# classifier test that invents its own inputs is testing the author's memory
# of the data, not the data.
_NOT_RECRUITING = [
    "Professor (USC Dornsife, WRIT 150)",
    "Professor (USC Marshall, BUAD 306)",
    "USC on-campus staff — Assistant Director, Dornsife First-Year Advising "
    "(academic advising, not career services)",
    "Academic Advisor", "Lecturer", "Registrar", "Admissions Counselor",
    "Account Manager, AWS", "Sales", "Customer Success Manager",
    "Software Engineer", "Product Manager", "Marketing Associate",
]


@pytest.mark.parametrize("role", _REAL_SEATS)
def test_a_covered_track_seat_is_never_hidden(role):
    verdict = recruitment.classify_person(role=role)
    assert verdict.verdict == recruitment.KEEP, (
        f"{role!r} is a seat on a track Coverage covers and was hidden as "
        f"{verdict.code}: {verdict.reason}"
    )


@pytest.mark.parametrize("role", _NOT_RECRUITING)
def test_the_verified_off_track_and_campus_rows_still_hide(role):
    assert recruitment.classify_person(role=role).verdict == recruitment.HIDE


@pytest.mark.parametrize("role", _REAL_SEATS + _NOT_RECRUITING + ["", None, "   "])
def test_the_user_override_wins_in_both_directions_over_everything(role):
    assert recruitment.classify_person(role=role, override=True).verdict == recruitment.KEEP
    assert recruitment.classify_person(role=role, override=False).verdict == recruitment.HIDE


@pytest.mark.parametrize("role", ["", None, "   ", "Analyst", "Associate", "Student"])
def test_no_signal_either_way_keeps(role):
    """The tie goes to keeping. Nothing on the row placing somebody outside
    recruiting is not evidence that they are outside it."""
    assert recruitment.classify_person(role=role).verdict == recruitment.KEEP


def test_free_prose_can_keep_somebody_but_can_never_hide_them():
    """`notes`/`angle` are scanned for keep-signals and must never be scanned
    for hide-markers: prose mentions everything ('PwC audit before CLSA')."""
    hidden_by_role = recruitment.classify_person(role="Software Engineer")
    assert hidden_by_role.verdict == recruitment.HIDE
    # The same hide-marker in prose, with a blank role, keeps.
    assert recruitment.classify_person(
        role="", notes="referencing her AWS software background",
    ).verdict == recruitment.KEEP
    # And prose naming a track rescues a role that would otherwise hide.
    assert recruitment.classify_person(
        role="Software Engineer", notes="moving into investment banking",
    ).verdict == recruitment.KEEP


def test_role_hint_disqualified_can_never_disagree_with_the_board():
    """The capture-time half of the rule is `classify_person` on the hint
    alone, so the two must agree by construction for every string."""
    for role in _REAL_SEATS + _NOT_RECRUITING + ["", "  ", "Analyst"]:
        assert recruitment.role_hint_disqualified(role or "") == (
            recruitment.classify_person(role=role or "").verdict == recruitment.HIDE
        )


def test_a_firm_tier_can_never_rescue_an_off_track_person():
    """The AWS account manager at tiered Amazon: the person's own role beats
    the firm's tier."""
    v = recruitment.classify_person(role="Account Manager, AWS", tiered=True,
                                    firm_tracks=("corp-strat",), firm_label="Amazon")
    assert v.verdict == recruitment.HIDE
    assert v.code == "off_track"


def test_a_blank_role_at_a_tiered_firm_is_rescued_by_the_firm():
    v = recruitment.classify_person(role="", tiered=True, firm_label="Barclays")
    assert v.verdict == recruitment.KEEP
    assert v.code == "tiered_firm"


def test_every_verdict_cites_a_reason_and_a_stable_code():
    codes = set()
    for role in _REAL_SEATS + _NOT_RECRUITING + ["", "Analyst"]:
        for override in (None, True, False):
            for tiered in (True, False):
                v = recruitment.classify_person(role=role, override=override,
                                                tiered=tiered, firm_label="X")
                assert v.verdict in (recruitment.KEEP, recruitment.HIDE)
                assert v.code and isinstance(v.code, str)
                assert v.reason and isinstance(v.reason, str)
                codes.add(v.code)
    assert codes >= {"override", "recruiter", "track_role", "campus",
                     "off_track", "tiered_firm", "no_signal"}


# ===========================================================================
# INVARIANT 8 — `is_recruiting_contact`: the student's own explicit answer
# beats the text, in BOTH directions, and `None` means unanswered.
# ===========================================================================
@pytest.mark.parametrize("role", ["Campus recruiting manager", "IB Analyst", "", None])
@pytest.mark.parametrize("explicit", [True, False, None])
def test_an_explicit_answer_always_beats_the_role_text(role, explicit):
    got = relevance.is_recruiting_contact({"role": role, "recruiting_contact": explicit})
    if explicit is not None:
        assert got is bool(explicit)
    else:
        assert got == relevance.is_recruiting_role(role)


def test_a_bare_recruiting_word_inside_a_longer_role_never_silences_a_banker():
    """The documented rejection: an analyst carrying campus-recruiting duty
    leads with the seat, and a false positive here silences their coffee chat
    invisibly."""
    assert relevance.is_recruiting_role("IB Analyst, campus recruiting captain") is True
    # ...but the WHOLE-role carve-out is anchored and cannot reach into one.
    assert relevance._WHOLE_ROLE_RECRUITING_RE.match(
        "IB Analyst, campus recruiting") is None
    assert relevance.is_recruiting_role("Recruiting") is True
    assert relevance.is_recruiting_role("recruitment") is True


# ===========================================================================
# INVARIANT 9 — campaigns. The signature normalizer is TOTAL, and the bulk
# floor cannot be reached by a person writing letters.
# ===========================================================================
@pytest.mark.parametrize("text", [
    None, "", "   ", "12345", "!!!", "\n\t", "Re: ", "[gmail:abc123]",
    "re: re: fwd: hello", "回复: 面试", "a" * 1000,
])
def test_normalize_subject_is_total_and_never_raises(text):
    got = campaigns.normalize_subject(text)
    assert isinstance(got, str)
    assert len(got) <= 240


def test_normalize_subject_collapses_a_merge_and_separates_real_letters():
    merged = [campaigns.normalize_subject(
        f"[gmail:{i:04x}] Fall 2026 ICC Alumni Digital Panel Outreach")
        for i in range(20)]
    assert len(set(merged)) == 1
    letters = [
        campaigns.normalize_subject("HK Jul 29-31 | Nomura | IBD - USC Coffee Chat"),
        campaigns.normalize_subject("HK Jul 29-31 | CLSA | CICC - USC Coffee Chat"),
    ]
    assert len(set(letters)) == 2


def test_a_reply_prefix_lands_in_its_own_campaigns_key_not_a_second_one():
    base = campaigns.normalize_subject("Fall 2026 ICC Alumni Panel")
    for prefix in ("Re: ", "RE:", "Fwd: ", "FW: ", "回复: ", "转发:"):
        assert campaigns.normalize_subject(prefix + "Fall 2026 ICC Alumni Panel") == base


@pytest.mark.parametrize("note", campaigns._APP_AUTHORED_NOTES)
def test_every_app_authored_template_is_refused_a_signature(note):
    """The 41-person phantom campaign: 40 touches predated the `subject`
    column, all fell through to a note Coverage had written itself, and the
    identical string read as a blast. Every template Coverage composes must be
    rejected, or absence of evidence becomes evidence of a blast again."""
    assert campaigns._is_app_authored(campaigns.normalize_subject(note))


def test_boilerplate_wrapped_around_a_real_send_still_groups():
    """The composition that matters: the ICC merge was detected through a note
    that was Coverage's own words WRAPPED AROUND something the sender wrote."""
    note = "Follow-up outreach sent for ICC alumni panel, no reply yet"
    assert not campaigns._is_app_authored(campaigns.normalize_subject(note))


def test_an_empty_note_is_never_treated_as_app_authored_boilerplate():
    """`all()` over an empty sequence is True; without the `bool(words)` guard
    every contentless touch would group into one giant phantom campaign."""
    assert campaigns._is_app_authored("") is False


def test_the_bulk_floor_clears_the_measured_personal_note_ceiling():
    """Raising the floor is safe; lowering it is not. The largest observed
    non-merge group on live data held 6."""
    assert campaigns.BULK_MIN_RECIPIENTS >= 7
    assert campaigns.BULK_WINDOW == timedelta(hours=24)


def test_burst_grouping_anchors_on_the_first_member_not_the_previous_one():
    """Anchoring on the previous member lets a subject the user reuses every
    week chain into one group spanning months — the opposite of a burst."""
    from datetime import datetime, timezone

    start = datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc)
    chain = [type("T", (), {"ts": start + timedelta(hours=12 * i)})()
             for i in range(10)]
    groups = campaigns._burst_groups(chain)
    assert len(groups) > 1, "a 5-day drip chained into a single 'burst'"
    for group in groups:
        assert max(t.ts for t in group) - min(t.ts for t in group) < campaigns.BULK_WINDOW


def test_the_campaign_label_is_the_same_string_in_every_process():
    """`max(set(...), key=...)` returns the first maximal element in SET
    iteration order, which derives from `hash()` — randomized per process.
    Two subjects tied on frequency therefore produced different labels in
    different web workers, so the Settings card asking the user to classify a
    send renamed itself between page loads.

    Asserted by running the tie in a subprocess under several hash seeds,
    because a single in-process call cannot observe the bug at all.
    """
    import os
    import subprocess
    import sys

    snippet = (
        "import sys; sys.path.insert(0, %r);"
        "import django, os;"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE','coverage_web.settings.local');"
        "django.setup();"
        "from crm.campaigns import _label_for;"
        "T=type('T',(),{});"
        "rows=[T() for _ in range(2)];"
        "rows[0].subject='Alpha panel invite'; rows[1].subject='Beta panel invite';"
        "[setattr(r,'note','') for r in rows];"
        "print(_label_for(rows))"
    ) % str(__import__("pathlib").Path(__file__).resolve().parents[2])

    results = set()
    for seed in range(8):
        env = dict(os.environ, PYTHONHASHSEED=str(seed))
        out = subprocess.run([sys.executable, "-c", snippet],
                             capture_output=True, text=True, env=env)
        assert out.returncode == 0, out.stderr[-2000:]
        results.add(out.stdout.strip())
    assert len(results) == 1, (
        f"_label_for returned {results} across 8 hash seeds — the campaign "
        "card's label depends on set iteration order"
    )


def test_two_date_only_sends_on_consecutive_days_are_two_bursts():
    """Exactly 24h apart must NOT chain: 96 of 117 live outbound touches carry
    a date-only midnight stamp, so this is the common case, not the edge."""
    from datetime import datetime, timezone

    day1 = datetime(2026, 7, 6, tzinfo=timezone.utc)
    rows = [type("T", (), {"ts": day1})(), type("T", (), {"ts": day1 + timedelta(days=1)})()]
    assert len(campaigns._burst_groups(rows)) == 2


# ===========================================================================
# INVARIANT — `crm.today._today_sort_key` is a TOTAL order, the same
# guarantee `coverage_domain.cadence.due_actions`' own sort was given a
# fourth tiebreak key for (contact id, the "C5" divergence in that module's
# docstring): "the returned order is a TOTAL order rather than three keys
# plus list.sort's stability over whatever sequence the caller iterated."
#
# `crm.today._cockpit_context` re-sorts the engine's already-totally-ordered
# output in ITS OWN key (`_today_sort_key`, `class, -ev, priority, tier,
# -idle_business_days, firm_name`) for the view's own reasons (momentum over
# tier), and that key stops at `firm_name` — no contact-id tiebreak. Two
# actions tie on every one of those terms whenever they share class, ev,
# priority, tier, idle bucket and firm label, which is the ordinary case for
# two never-contacted strangers with no firm on file: same trigger (cold,
# due), same relevance weight (unranked), same idle bucket (`10**6`, "no
# dateable touch"), same firm_name ("No firm listed"). `sorted()` is stable,
# so a tie is resolved by whatever order the caller's `actions` list happened
# to arrive in — and for the actions `_opening_keep_warms` appends, that
# order traces back to `Contact.objects.for_user(user).filter(archived=False)`
# in `_build_actions`, which carries no `.order_by()`. That is exactly the
# "free to change after any UPDATE, and does" case cadence's C5 note
# measured for an unordered Postgres scan — reintroduced one layer up, in
# the view's own re-sort, by the same class of bug the engine was already
# fixed for. Concretely: which of two tied cold contacts sits inside today's
# capped plan and which one is bumped to "held" can flip between two renders
# of the same page with no data change in between.
# ===========================================================================
def _tied_today_action(cid, **overrides):
    base = {
        "action": "first_outreach", "ev": 0.96, "priority": 1, "tier": 3,
        "idle_business_days": 10 ** 6, "firm_name": "No firm listed",
        "contact": {"id": cid},
    }
    base.update(overrides)
    return base


def test_today_sort_key_breaks_every_tie_on_contact_id():
    from crm.today import _today_sort_key

    a, b = _tied_today_action(101), _tied_today_action(202)
    assert _today_sort_key(a) != _today_sort_key(b), (
        "two actions tied on class/ev/priority/tier/idle/firm_name must "
        "still resolve to a total order — the same guarantee cadence."
        "due_actions' own sort carries via its contact-id tiebreak (C5)"
    )


def test_today_sort_key_is_invariant_under_input_order_for_tied_actions():
    from crm.today import _today_sort_key

    rng = random.Random(SEED)
    actions = [_tied_today_action(cid) for cid in (301, 302, 303, 304, 305, 306)]
    expected = [a["contact"]["id"] for a in sorted(actions, key=_today_sort_key)]
    seen = set()
    for _ in range(60):
        shuffled = actions[:]
        rng.shuffle(shuffled)
        got = [a["contact"]["id"] for a in sorted(shuffled, key=_today_sort_key)]
        seen.add(tuple(got))
    assert seen == {tuple(expected)}, (
        f"{len(seen)} distinct orders produced for the same tied set across "
        "60 shuffles — the Today page's plan/held split (and the rendered "
        "card order within a lane) can flip between two renders with no "
        "data change behind it"
    )
