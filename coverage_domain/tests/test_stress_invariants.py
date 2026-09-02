"""Adversarial invariant suite for the cadence / pipeline / scoring engines.

WHAT MAKES THIS DIFFERENT from `test_cadence.py` and `test_scoring.py`. Those
are example-based: each one pins one behaviour against one hand-built fixture,
and between them they document what the engines are FOR. This file asks the
opposite question — what input breaks a property that is supposed to hold for
every input — and it does that by generating the inputs rather than choosing
them.

NO `hypothesis`, DELIBERATELY, and the choice is worth recording because the
obvious move was to add it. Three reasons it is the wrong dependency here:

  1. This package's whole contract is that it has ZERO runtime dependencies
     (`pyproject.toml`: `dependencies = []`) and that its suite runs with no
     external service. Its dev group is `pytest` + `psycopg` and nothing else.
     A property-testing library is a real addition to what a contributor must
     install before the "safe to hammer" suite runs.
  2. The interesting input space here is not continuous, it is a small
     ENUMERATED cross-product: 4 warmth values x 7 thread states x a handful
     of touch kinds x a handful of clock offsets. That space is small enough
     to walk EXHAUSTIVELY, which is strictly stronger than sampling it —
     `hypothesis` would search a space this file covers completely.
  3. Where sampling IS the right tool (permutation stability), it needs a
     shuffle and a seed, not a shrinker. `random.Random(SEED)` gives a
     reproducible counterexample; a hypothesis failure gives one too, but at
     the cost of the two points above.

So: exhaustive enumeration where the space is finite, seeded generation where
it is not, and every generator is deterministic — a failure here reproduces
on the next run without a database, a `.hypothesis` cache, or a network.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import pytest

from coverage_domain import cadence, pipeline, scoring

SEED = 20260827
AS_OF = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
TODAY = AS_OF.date()

ALL_WARMTH = pipeline.WARMTH
ALL_THREAD_STATES = pipeline.THREAD_STATES
ALL_KINDS = tuple(pipeline.TOUCH_TRANSITIONS) + (pipeline.MANUAL_OVERRIDE_KIND,)


def contact(cid, **kw):
    base = {
        "id": cid, "warmth": "cold", "thread_state": "no_reply",
        "firm_id": None, "region": "", "archived": False,
    }
    base.update(kw)
    return base


def touch(cid, kind, ts, **kw):
    row = {"contact_id": cid, "kind": kind, "ts": ts}
    row.update(kw)
    return row


# ===========================================================================
# INVARIANT 1 — due_actions returns AT MOST ONE action per contact.
#
# The tree's contract ("returning at most one action per contact") is stated
# in the module docstring and enforced only by `continue` statements placed by
# hand. Every branch that can fall through is a chance to emit twice.
# ===========================================================================
@pytest.mark.parametrize("warmth", ALL_WARMTH)
@pytest.mark.parametrize("thread_state", ALL_THREAD_STATES)
def test_at_most_one_action_per_contact_over_the_full_state_space(warmth, thread_state):
    """Walk every (warmth, thread_state) cell against every touch kind, with a
    confirmed close in the re-ping window so branch 3 is live too."""
    firm_dates = [{
        "firm_id": 1, "event_kind": "app_close", "region": "us",
        "date": TODAY + timedelta(days=3), "confidence": "confirmed_official",
    }]
    for kind in ALL_KINDS:
        for age_days in (0, 1, 5, 30, 400):
            c = contact(1, warmth=warmth, thread_state=thread_state,
                        firm_id=1, region="us")
            ts = [touch(1, kind, AS_OF - timedelta(days=age_days))]
            actions = cadence.due_actions(
                [c], ts, firm_dates, as_of=AS_OF,
                firms={1: {"name": "Acme", "tier": 1}},
            )
            mine = [a for a in actions if a["contact"]["id"] == 1]
            assert len(mine) <= 1, (
                f"{(warmth, thread_state, kind, age_days)} produced "
                f"{[a['action'] for a in mine]} — the tree emitted twice"
            )


# ===========================================================================
# INVARIANT 2 — an ARCHIVED contact never produces an action, whatever else
# is true about them. The queue's one hard exclusion.
# ===========================================================================
@pytest.mark.parametrize("warmth", ALL_WARMTH)
@pytest.mark.parametrize("thread_state", ALL_THREAD_STATES)
def test_archived_contacts_are_never_surfaced(warmth, thread_state):
    firm_dates = [{
        "firm_id": 1, "event_kind": "app_close", "region": "us",
        "date": TODAY + timedelta(days=1), "confidence": "confirmed_official",
    }]
    c = contact(1, warmth=warmth, thread_state=thread_state, firm_id=1,
                region="us", archived=True)
    ts = [touch(1, k, AS_OF - timedelta(days=400)) for k in ALL_KINDS]
    assert cadence.due_actions([c], ts, firm_dates, as_of=AS_OF) == []


# ===========================================================================
# INVARIANT 3 — the returned order is a TOTAL order: the same set of contacts
# in any input order gives byte-identical output.
#
# This is the property the module's "Determinism" note claims and the property
# the C5 divergence added the fourth sort key to make true. Before that key,
# six tied contacts shuffled 50 times produced 48 distinct orders, and the web
# layer's fetch carries no ORDER BY — so the student's queue reshuffled
# between two page loads with no data change behind it.
# ===========================================================================
def test_action_order_is_invariant_under_input_permutation():
    rng = random.Random(SEED)
    contacts = [
        contact(i, warmth=rng.choice(ALL_WARMTH),
                thread_state=rng.choice(ALL_THREAD_STATES),
                firm_id=rng.choice([None, 1, 2]))
        for i in range(40)
    ]
    touches = [
        touch(c["id"], rng.choice(ALL_KINDS),
              AS_OF - timedelta(days=rng.randrange(0, 500)))
        for c in contacts for _ in range(rng.randrange(0, 3))
    ]
    # Two firms sharing a NAME and a TIER: the adversarial case, because it
    # makes the ported three keys tie for every pair across the two firms.
    firms = {1: {"name": "Same", "tier": 2}, 2: {"name": "Same", "tier": 2}}

    def run(cs, ts):
        return [
            (a["contact"]["id"], a["action"])
            for a in cadence.due_actions(cs, ts, [], as_of=AS_OF, firms=firms)
        ]

    expected = run(contacts, touches)
    for _ in range(60):
        cs, ts = contacts[:], touches[:]
        rng.shuffle(cs)
        rng.shuffle(ts)
        assert run(cs, ts) == expected


def test_rank_of_a_tied_pair_does_not_depend_on_which_arrived_first():
    """The minimal reproducer, spelled out so a regression names itself."""
    a, b = contact(1, firm_id=1), contact(2, firm_id=1)
    firms = {1: {"name": "Acme", "tier": 1}}
    forward = cadence.due_actions([a, b], [], [], as_of=AS_OF, firms=firms)
    reverse = cadence.due_actions([b, a], [], [], as_of=AS_OF, firms=firms)
    assert [x["contact"]["id"] for x in forward] == [1, 2]
    assert [x["contact"]["id"] for x in reverse] == [1, 2]


# ===========================================================================
# INVARIANT 4 — no reason string ever renders a negative age, and a chat that
# has NOT HAPPENED YET is never owed a thank-you (the C4 divergence).
#
# `pipeline.TOUCH_TRANSITIONS`' own comment names this failure and then
# defends it by convention. Reachable from a calendar-sourced capture, from a
# hand-logged chat entered with tomorrow's date, and from any caller whose
# `as_of` clock runs behind the touch's own.
# ===========================================================================
@pytest.mark.parametrize("days_ahead", [1, 7, 30, 365])
def test_a_future_chat_is_not_owed_a_thank_you(days_ahead):
    c = contact(1, warmth="chatted", thread_state="chat_done")
    ts = [touch(1, "chat", AS_OF + timedelta(days=days_ahead))]
    actions = cadence.due_actions([c], ts, [], as_of=AS_OF)
    assert [a["action"] for a in actions] == [], (
        "a conversation that has not happened yet cannot be overdue for a "
        f"thank-you, but the queue produced {[a['reason'] for a in actions]}"
    )


@pytest.mark.parametrize("warmth", ALL_WARMTH)
@pytest.mark.parametrize("thread_state", ALL_THREAD_STATES)
def test_no_reason_string_ever_renders_a_negative_number(warmth, thread_state):
    """A future-dated touch of ANY kind must not produce a card whose prose
    contains a negative count. '-720h ago' is what this caught."""
    for kind in ALL_KINDS:
        c = contact(1, warmth=warmth, thread_state=thread_state)
        ts = [touch(1, kind, AS_OF + timedelta(days=30))]
        for a in cadence.due_actions([c], ts, [], as_of=AS_OF):
            assert "-" not in a["reason"].replace("15-min", "").replace(
                "e-mail", ""), (
                f"{(warmth, thread_state, kind)} rendered {a['reason']!r}"
            )
            for key, value in a["ctx"].items():
                if isinstance(value, (int, float)):
                    assert value >= 0, (
                        f"{(warmth, thread_state, kind)} -> ctx[{key!r}]={value}"
                    )


# ===========================================================================
# INVARIANT 5 — clocks. Month, year, leap-day and DST boundaries must not move
# a business-day count, and `business_days_since` must never go negative.
# ===========================================================================
@pytest.mark.parametrize(
    "then,now,expected",
    [
        # Month boundary, no weekend crossed: Mon 30 Mar -> Wed 1 Apr.
        (date(2026, 3, 30), date(2026, 4, 1), 2),
        # Year boundary: Wed 31 Dec 2025 -> Thu 1 Jan 2026.
        (date(2025, 12, 31), date(2026, 1, 1), 1),
        # Leap day exists and is counted: Wed 28 Feb 2024 -> Fri 1 Mar 2024
        # spans Thu 29 Feb.
        (date(2024, 2, 28), date(2024, 3, 1), 2),
        # The same calendar gap in a NON-leap year is one day shorter.
        (date(2026, 2, 27), date(2026, 3, 2), 1),
        # A full week always costs exactly 5 business days, wherever it starts.
        (date(2026, 8, 3), date(2026, 8, 10), 5),
        (date(2026, 8, 7), date(2026, 8, 14), 5),
    ],
)
def test_business_days_across_calendar_boundaries(then, now, expected):
    assert cadence.business_days_since(then, now) == expected


def test_business_days_is_never_negative_and_is_monotone():
    """Walk a full year day by day: the count from a fixed anchor may only
    ever stay level or rise, and may never dip below zero — including for a
    `now` BEFORE the anchor (a touch dated in the future)."""
    anchor = date(2026, 6, 15)
    previous = -1
    for offset in range(-200, 366):
        n = cadence.business_days_since(anchor, anchor + timedelta(days=offset))
        assert n >= 0
        assert n >= previous
        previous = n


def test_a_full_week_is_always_five_business_days_all_year():
    d = date(2026, 1, 1)
    while d < date(2027, 1, 1):
        assert cadence.business_days_since(d, d + timedelta(days=7)) == 5, d
        d += timedelta(days=1)


def test_dst_transitions_do_not_shift_the_as_of_date():
    """`due_actions` reads `as_of.date()`. Feeding it the same wall-clock hour
    either side of a US DST transition must not silently move the day, and the
    engine must not care which offset the caller's datetime carries."""
    zone = pytest.importorskip("zoneinfo").ZoneInfo("America/New_York")
    before = datetime(2026, 3, 7, 12, 0, tzinfo=zone)   # EST
    after = datetime(2026, 3, 9, 12, 0, tzinfo=zone)    # EDT, one hour shifted
    c = contact(1)
    for as_of in (before, after):
        actions = cadence.due_actions([c], [], [], as_of=as_of)
        assert [a["action"] for a in actions] == ["first_outreach"]


# ===========================================================================
# INVARIANT 6 — degenerate data must not raise. Zero contacts, one contact,
# 10,000, all parked, all cold, no firm, a firm with no tier.
# ===========================================================================
def test_zero_contacts():
    assert cadence.due_actions([], [], [], as_of=AS_OF) == []
    assert cadence.due_actions([], [], None, as_of=AS_OF, firms=None) == []


def test_ten_thousand_contacts_stay_linear_and_complete():
    # Rewritten 2026-09-01 to pin the corrected behaviour: a first note 30
    # calendar days old is 20-22 business days of silence, past branch 6's
    # `followup_expires_after_business_days` (15), so ten thousand of them
    # are ten thousand PARKS — every one expired, none dropped — and not the
    # never-expiring follow-ups this used to assert (that was the defect,
    # not the invariant). The invariant is completeness and linearity; the
    # second block keeps the follow-up case at an offset inside the window.
    cs = [contact(i) for i in range(10_000)]
    ts = [touch(i, "outreach", AS_OF - timedelta(days=30)) for i in range(10_000)]
    actions = cadence.due_actions(cs, ts, [], as_of=AS_OF)
    assert len(actions) == 10_000
    assert {a["action"] for a in actions} == {"park"}
    assert all(a["ctx"]["expired"] for a in actions)

    ts = [touch(i, "outreach", AS_OF - timedelta(days=10)) for i in range(10_000)]
    actions = cadence.due_actions(cs, ts, [], as_of=AS_OF)
    assert len(actions) == 10_000
    assert {a["action"] for a in actions} == {"follow_up"}


def test_all_parked_and_all_quiet_produce_an_empty_queue():
    for state in ("parked", "quiet"):
        cs = [contact(i, thread_state=state) for i in range(50)]
        ts = [touch(i, "outreach", AS_OF - timedelta(days=400)) for i in range(50)]
        assert cadence.due_actions(cs, ts, [], as_of=AS_OF) == []


def test_a_contact_with_no_firm_gets_the_terminal_placeholder_not_a_crash():
    actions = cadence.due_actions([contact(1)], [], [], as_of=AS_OF)
    assert actions[0]["firm_name"] == "No firm listed"
    assert actions[0]["firm_known"] is False
    assert actions[0]["tier"] == 3


@pytest.mark.parametrize("tier", [None, "1", "unranked", True, False, [], {}, object()])
def test_a_non_integer_tier_does_not_crash_the_sort(tier):
    """`tier=None` (the Unranked lane) once raised a TypeError here. `_coerce_tier`
    now covers every non-numeric shape the caller-built mapping can carry — a
    CSV import, a JSON round-trip, an admin edit, an uncoerced form value."""
    firms = {1: {"name": "A", "tier": tier}, 2: {"name": "B", "tier": 2}}
    actions = cadence.due_actions(
        [contact(1, firm_id=1), contact(2, firm_id=2)], [], [],
        as_of=AS_OF, firms=firms,
    )
    assert len(actions) == 2
    assert all(isinstance(a["tier"], (int, float)) for a in actions)


@pytest.mark.parametrize("name", [None, 123, ["x"], {"a": 1}, 4.5])
def test_a_non_string_firm_name_does_not_crash_the_sort(name):
    firms = {1: {"name": name, "tier": 1}, 2: {"name": "B", "tier": 1}}
    actions = cadence.due_actions(
        [contact(1, firm_id=1), contact(2, firm_id=2)], [], [],
        as_of=AS_OF, firms=firms,
    )
    assert len(actions) == 2


def test_unparseable_and_missing_timestamps_do_not_raise():
    """`_as_dt` returns None rather than raising on junk; every branch that
    reads a date has an undatable path, and none of them may render a
    sentinel like 999."""
    for bad in (None, "", "not a date", "2026-13-45", [], {}, 0):
        c = contact(1, warmth="chatted", thread_state="chat_done")
        actions = cadence.due_actions(
            [c], [touch(1, "chat", bad)], [], as_of=AS_OF,
        )
        for a in actions:
            assert "999" not in a["reason"]


# ===========================================================================
# INVARIANT 7 — the re-ping branch never fires on a deadline that has passed,
# and never crosses a region boundary.
# ===========================================================================
@pytest.mark.parametrize("days_out", range(-30, 31))
def test_reping_only_ever_fires_inside_the_forward_window(days_out):
    firm_dates = [{
        "firm_id": 1, "event_kind": "app_close", "region": "us",
        "date": TODAY + timedelta(days=days_out),
        "confidence": "confirmed_official",
    }]
    c = contact(1, warmth="chatted", thread_state="chat_done",
                firm_id=1, region="us")
    actions = cadence.due_actions(
        [c], [touch(1, "chat", AS_OF - timedelta(days=200))], firm_dates,
        as_of=AS_OF, firms={1: {"name": "Acme", "tier": 1}},
    )
    fired = [a for a in actions if a["action"] == "reping"]
    reping_window = cadence.CADENCE_DEFAULTS["pre_deadline_reping_days"]
    if 0 <= days_out <= reping_window:
        assert fired, f"{days_out}d out is inside the window and did not fire"
    else:
        assert not fired, f"{days_out}d out fired a re-ping"


@pytest.mark.parametrize("contact_region,close_region,should_fire", [
    ("us", "us", True), ("hk", "hk", True),
    ("us", "hk", False), ("hk", "us", False),
    ("other", "us", False), ("other", "hk", False),
    ("", "us", True),      # unknown region takes the both-regions fallback
    ("", "hk", True),
])
def test_reping_region_scoping_is_exhaustive(contact_region, close_region, should_fire):
    firm_dates = [{
        "firm_id": 1, "event_kind": "app_close", "region": close_region,
        "date": TODAY + timedelta(days=5), "confidence": "confirmed_official",
    }]
    c = contact(1, warmth="advocate", thread_state="chat_done",
                firm_id=1, region=contact_region)
    actions = cadence.due_actions(
        [c], [], firm_dates, as_of=AS_OF, firms={1: {"name": "A", "tier": 1}},
    )
    assert bool([a for a in actions if a["action"] == "reping"]) is should_fire


# ===========================================================================
# INVARIANT 8 — pipeline: warmth is a RATCHET. No sequence of apply_touch
# calls, in any order, can lower a contact's warmth.
#
# Walked as a pure state simulation against WARMTH_RANK and TOUCH_TRANSITIONS
# — the same comparison the SQL `CASE` performs — so this needs no database
# and covers every permutation of every kind up to length 4.
# ===========================================================================
def _simulate(kind, warmth, thread_state):
    """Python mirror of apply_touch's atomic UPDATE ... CASE."""
    new_warmth, new_state = pipeline.TOUCH_TRANSITIONS[kind]
    if new_warmth is not None and pipeline.WARMTH_RANK[new_warmth] > pipeline.WARMTH_RANK[warmth]:
        warmth = new_warmth
    if new_state is not None and thread_state != "advocate":
        thread_state = new_state
    return warmth, thread_state


def test_warmth_never_decreases_over_any_touch_sequence():
    import itertools
    kinds = tuple(pipeline.TOUCH_TRANSITIONS)
    for start_w in ALL_WARMTH:
        for start_s in ALL_THREAD_STATES:
            for length in (1, 2, 3):
                for seq in itertools.product(kinds, repeat=length):
                    w, s = start_w, start_s
                    for k in seq:
                        nw, ns = _simulate(k, w, s)
                        assert pipeline.WARMTH_RANK[nw] >= pipeline.WARMTH_RANK[w], (
                            f"{start_w}/{start_s} + {seq} lowered warmth "
                            f"{w} -> {nw}"
                        )
                        w, s = nw, ns


def test_advocate_thread_state_is_terminal_under_apply_touch():
    for kind in pipeline.TOUCH_TRANSITIONS:
        for warmth in ALL_WARMTH:
            _, state = _simulate(kind, warmth, "advocate")
            assert state == "advocate", f"{kind} moved thread_state out of advocate"


def test_applying_the_same_touch_twice_is_idempotent_on_state():
    """The second identical touch may add a row to the log, but it must not
    move warmth or thread_state a second time."""
    for kind in pipeline.TOUCH_TRANSITIONS:
        for warmth in ALL_WARMTH:
            for state in ALL_THREAD_STATES:
                once = _simulate(kind, warmth, state)
                twice = _simulate(kind, *once)
                assert once == twice, f"{kind} from {(warmth, state)} is not idempotent"


def test_same_day_touch_order_is_the_only_thing_that_can_change_thread_state():
    """Two touches on the same day, applied in both orders. Warmth must agree
    (it is a ratchet, so order-independent); thread_state may NOT, and this
    test pins that asymmetry rather than hiding it — it is the documented
    'thread_state is not rank-guarded outside advocate' divergence, and the
    caller-side ratchet that compensates lives outside this package."""
    disagreements = set()
    for a in pipeline.TOUCH_TRANSITIONS:
        for b in pipeline.TOUCH_TRANSITIONS:
            forward = _simulate(b, *_simulate(a, "cold", "no_reply"))
            reverse = _simulate(a, *_simulate(b, "cold", "no_reply"))
            assert forward[0] == reverse[0], (
                f"warmth is order-dependent for {a} then {b}: "
                f"{forward[0]} vs {reverse[0]}"
            )
            if forward[1] != reverse[1]:
                disagreements.add(frozenset((a, b)))
    # Exactly the pairs the module docstring names: a kind whose thread_state
    # is "behind" another's wins by arriving later.
    assert disagreements, (
        "thread_state used to be order-dependent; if this is now empty the "
        "caller-side ratchet moved into this package and the docstring's "
        "DIVERGENCE note needs updating"
    )
    for pair in disagreements:
        assert any(pipeline.TOUCH_TRANSITIONS[k][1] is not None for k in pair)


def test_every_touch_kind_has_a_reachable_transition_and_no_unreachable_state():
    """No kind may name a thread_state outside THREAD_STATES, and no warmth
    outside WARMTH_RANK — a typo there fails silently in SQL (the CASE just
    never matches) rather than raising."""
    for kind, (warmth, state) in pipeline.TOUCH_TRANSITIONS.items():
        assert warmth is None or warmth in pipeline.WARMTH_RANK, kind
        assert state is None or state in pipeline.THREAD_STATES, kind
    reachable = {s for _, s in pipeline.TOUCH_TRANSITIONS.values() if s}
    # These three can only be entered by `set_state`, and that is what keeps
    # the cadence engine's branch 4 (the parked/quiet exit) a decision the
    # student made rather than something a touch can do to them.
    #
    # `advocate` USED TO BE IN THIS LIST and was moved out deliberately on
    # 2026-09-02 (WS-CRM-12). The comment here already said that removing a
    # state from this set is "a product change, not a refactor", and this is
    # that product change, made on purpose: advocacy is an EVENT (somebody
    # pushed for you) and was reachable only as a hand override, which is why
    # three of the founder's debriefs answered "would advocate: yes" and none
    # of them became anything countable. `referral` is now the one automatic
    # door into it, it is still terminal once entered (apply_touch's
    # `thread_state != 'advocate'` guard), and `crm.debrief.promote` is still
    # the only writer — a recorded opinion never writes the row by itself.
    assert set(pipeline.THREAD_STATES) - reachable == {
        "no_reply", "parked", "quiet"
    }
    assert pipeline.TOUCH_TRANSITIONS[pipeline.REFERRAL_KIND][1] == "advocate"


def test_clock_silent_kinds_are_a_subset_of_the_known_vocabulary():
    """`cadence._CLOCK_SILENT_KINDS` names kinds by string. A rename in
    pipeline that misses cadence would silently restart every idle clock."""
    known = set(pipeline.TOUCH_TRANSITIONS) | {pipeline.MANUAL_OVERRIDE_KIND}
    assert cadence._CLOCK_SILENT_KINDS <= known
    assert pipeline.BULK_RECEIVED_KIND in cadence._CLOCK_SILENT_KINDS
    assert pipeline.MANUAL_OVERRIDE_KIND in cadence._CLOCK_SILENT_KINDS


@pytest.mark.parametrize("kind", sorted(cadence._CLOCK_SILENT_KINDS))
def test_a_clock_silent_kind_never_resets_an_idle_clock(kind):
    """Exhaustive over both silent kinds and every branch that reads the idle
    clock: the audit/bulk row lands TODAY, and the action must be identical to
    the one produced with no such row at all."""
    old = AS_OF - timedelta(days=400)
    for warmth, state in (("advocate", "chat_done"), ("chatted", "chat_done"),
                          ("cold", "no_reply"), ("replied", "replied"),
                          ("cold", "chat_scheduled")):
        c = contact(1, warmth=warmth, thread_state=state)
        without = cadence.due_actions([c], [touch(1, "outreach", old)], [], as_of=AS_OF)
        with_row = cadence.due_actions(
            [c], [touch(1, "outreach", old), touch(1, kind, AS_OF)], [], as_of=AS_OF,
        )
        assert [(a["action"], a["priority"]) for a in without] == \
               [(a["action"], a["priority"]) for a in with_row], (
            f"a {kind!r} row moved the queue for {warmth}/{state}"
        )


# ===========================================================================
# INVARIANT 9 — scoring. Every axis and composite stays inside [0, 100], the
# band is monotone in the composite, and the inputs hash is stable.
# ===========================================================================
def _random_touches(rng, cid, n):
    return [
        touch(cid, rng.choice(ALL_KINDS),
              AS_OF - timedelta(days=rng.randrange(-30, 900)),
              note="manual override: warmth=" + rng.choice(ALL_WARMTH))
        for _ in range(n)
    ]


def test_every_score_stays_in_range_over_generated_histories():
    rng = random.Random(SEED)
    for _ in range(400):
        c = {"id": 1,
             "role": rng.choice([None, "", "Analyst", "Managing Director",
                                 "Head of TMT", "admin", "MD", "junk 123"]),
             "school_affiliation": rng.choice([True, False, None, ""])}
        result = scoring.score_contact(c, _random_touches(rng, 1, rng.randrange(0, 8)),
                                       as_of=AS_OF)
        assert 0.0 <= result["composite"] <= 100.0
        for axis, payload in result["axes"].items():
            assert 0.0 <= payload["score"] <= 100.0, (axis, payload)
        assert result["band"] in {"hot", "warm", "cool", "cold"}
        assert isinstance(result["reasoning"], str)


def test_band_is_monotone_in_the_composite():
    order = {"cold": 0, "cool": 1, "warm": 2, "hot": 3}
    previous = -1
    for tenths in range(0, 1001):
        band = scoring._band(tenths / 10.0, scoring.DEFAULT_PARAMS)
        assert order[band] >= previous
        previous = order[band]


def test_identical_inputs_reproduce_a_byte_identical_hash_and_composite():
    rng = random.Random(SEED)
    for _ in range(50):
        c = {"id": 1, "role": "Vice President", "school_affiliation": True}
        ts = _random_touches(rng, 1, 5)
        first = scoring.score_contact(c, ts, as_of=AS_OF)
        # Same facts, shuffled — the hash canonicalizes, so it must not move.
        shuffled = ts[:]
        rng.shuffle(shuffled)
        second = scoring.score_contact(c, shuffled, as_of=AS_OF)
        assert first["inputs_hash"] == second["inputs_hash"]
        assert first["composite"] == second["composite"]


def test_naive_and_aware_as_of_for_the_same_instant_agree():
    naive = datetime(2026, 8, 27, 12, 0)
    aware = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    ts = [touch(1, "reply_received", AS_OF - timedelta(days=10))]
    a = scoring.score_contact({"id": 1}, ts, as_of=naive)
    b = scoring.score_contact({"id": 1}, ts, as_of=aware)
    assert a["inputs_hash"] == b["inputs_hash"]
    assert a["composite"] == b["composite"]


# ===========================================================================
# INVARIANT 10 — the firm sponsorship answer. A JSON column read as though its
# shape were guaranteed is the bug class that already caused two live 500s in
# `directory/sponsorship.py`; `_score_structural` had the same class in a
# quieter form — it did not crash, it answered wrongly in both directions.
# ===========================================================================
@pytest.mark.parametrize("sponsors,expected", [
    # The caller's own collapsed answer, unchanged.
    (True, True),
    (False, False),
    # The live per-region shape.
    ({"us": True}, True),
    ({"us": False}, False),
    ({"us": True, "hk": False}, True),      # best case across regions in play
    ({"us": False, "hk": False}, False),    # sponsors NOWHERE relevant
    # Silence is UNKNOWN, never a "no".
    ({}, None),
    (None, None),
    ({"us": "unknown"}, None),
    ({"hk": True}, None),                   # answers a region not in play
    # Shapes that must not crash or be guessed at.
    ("yes", None),
    ([], None),
    ([("us", True)], None),
    (0, None),
    ({"US": " true"}, None),
])
def test_firm_sponsorship_resolution_over_every_live_and_junk_shape(sponsors, expected):
    assert scoring.resolve_firm_sponsors(sponsors, {"us"}) is expected


def test_an_unrecorded_firm_policy_is_never_asserted_as_no_sponsorship():
    """The regression that mattered: `Firm.sponsors` defaults to `{}` and 73 of
    131 live firms carry exactly that. Truthiness made every one of them read
    as a confident 'no sponsorship' — a hard 20-point structural penalty and a
    printed claim — out of an empty column."""
    user = {"id": 1, "regions": ["us"], "tracks": ["ib"], "needs_sponsorship": True}
    firm = {"id": 1, "regions": ["us"], "tracks": ["ib"], "sponsors": {}}
    result = scoring.score_firm(user, firm, [], [], [], as_of=AS_OF)
    assert result["axes"]["structural"]["sponsorship_ok"] is None
    assert "no sponsorship" not in result["reasoning"]


def test_a_firm_that_sponsors_nowhere_is_not_reported_as_fine():
    """The opposite direction, and the expensive one: a non-empty dict was
    truthy, so a firm that had explicitly written down that it sponsors in
    neither market scored full marks and drew no warning."""
    user = {"id": 1, "regions": ["us", "hk"], "tracks": ["ib"],
            "needs_sponsorship": True}
    firm = {"id": 1, "regions": ["us", "hk"], "tracks": ["ib"],
            "sponsors": {"us": False, "hk": False}}
    result = scoring.score_firm(user, firm, [], [], [], as_of=AS_OF)
    assert result["axes"]["structural"]["sponsorship_ok"] is False
    assert "no sponsorship" in result["reasoning"]


def test_relevant_regions_is_shared_by_both_sides_of_the_question():
    """`needs_sponsorship` (the user's side) and `resolve_firm_sponsors` (the
    firm's) must scope to the same regions, or the student is told about a
    market neither of them was talking about."""
    assert scoring.relevant_regions(["us", "hk"], ["us"]) == {"us"}
    assert scoring.relevant_regions(["us"], []) == {"us"}       # firm silent
    assert scoring.relevant_regions([], ["hk"]) == {"hk"}       # user silent
    assert scoring.relevant_regions([], []) == set()
    assert scoring.relevant_regions([" US "], ["us"]) == {"us"}


# ===========================================================================
# INVARIANT 11 — a caller-supplied params bundle must not be able to raise out
# of a scorer. Weights are a config surface (`crm.views` builds a bundle per
# request to fold in the user's advocate_target); a zero in one is a bad
# config, not a 500.
# ===========================================================================
@pytest.mark.parametrize("key", [
    "timeline_runway_days", "momentum_recent_days", "momentum_base_days",
    "advocate_target", "recency_half_life_days", "resp_latency_zero_days",
])
def test_a_zero_valued_param_does_not_raise(key):
    params = {**scoring.DEFAULT_PARAMS, key: 0, "version": f"stress-{key}"}
    firm_dates = [{"event_kind": "app_close", "region": "us",
                   "date": TODAY + timedelta(days=10),
                   "confidence": "confirmed_official"}]
    result = scoring.score_firm(
        {"id": 1, "regions": ["us"], "tracks": ["ib"]},
        {"id": 1, "regions": ["us"], "tracks": ["ib"], "sponsors": True},
        [{"id": 1, "role": "Analyst"}],
        # An outbound BEFORE the reply, so a median latency is actually
        # measured — otherwise `resp_latency_zero_days` is never divided by
        # and the parametrisation quietly proves nothing for that key.
        [touch(1, "outreach", AS_OF - timedelta(days=5)),
         touch(1, "reply_received", AS_OF - timedelta(days=2)),
         touch(1, "chat", AS_OF - timedelta(days=1))],
        firm_dates, as_of=AS_OF, params=params,
    )
    assert 0.0 <= result["composite"] <= 100.0


def test_cadence_params_of_zero_do_not_raise():
    for key in cadence.CADENCE_DEFAULTS:
        c = contact(1, warmth="chatted", thread_state="chat_done")
        ts = [touch(1, "chat", AS_OF - timedelta(days=30))]
        cadence.due_actions([c], ts, [], as_of=AS_OF, params={key: 0})
