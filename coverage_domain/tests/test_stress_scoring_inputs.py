"""Adversarial invariant suite for `scoring.py`'s INPUT BOUNDARY and its
TEXT MATCHING — the two things the axis-level properties in
`test_stress_invariants.py` cannot see.

WHY A SECOND SCORING FILE. `test_stress_invariants.py`'s INVARIANT 9-11 ask
whether the SHAPE holds: does every axis stay in [0, 100], is the band
monotone, does a zero-valued param raise. Those are properties of the
arithmetic once the arguments are already good. This file asks the two
questions on either side of that:

  - what happens when an argument is NOT good (the caller passed a string
    where a datetime belongs, or nothing at all), and
  - is the keyword table that turns a free-text job title into a leverage
    number actually right about the strings the live board contains?

The second one is not a shape question at all. `_seniority_from_role` is
regex-adjacent text matching over prose somebody else wrote, and text
matching is where this codebase keeps finding real defects — a phrase that
matches more than it means. A score can stay inside [0, 100] for every input
and still be confidently wrong for a whole class of them.

Same discipline as its companion: NO `hypothesis`, exhaustive walks over the
enumerated spaces, seeded generation where the space is not finite.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import pytest

from coverage_domain import scoring

SEED = 20260828
AS_OF = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
TODAY = AS_OF.date()


def touch(cid, kind, ts, **kw):
    row = {"contact_id": cid, "kind": kind, "ts": ts}
    row.update(kw)
    return row


HISTORY = [
    touch(1, "outreach", AS_OF - timedelta(days=20)),
    touch(1, "reply_received", AS_OF - timedelta(days=18)),
    touch(1, "chat", AS_OF - timedelta(days=9)),
]


# ===========================================================================
# INVARIANT 1 — `as_of` is refused at the boundary, by name.
#
# `_as_dt` answers "parse this if you can" and returns None when it cannot,
# which is the right contract for a touch's `ts`: one unreadable timestamp
# among twenty should be skipped, not fatal. It is the WRONG contract for
# `as_of`, which every axis reads and the result serializes — and both
# scorers used to run it through `_as_dt` and carry the None onward. The
# failure surfaced sixty lines later as `'NoneType' object has no attribute
# 'astimezone'` (or, with any touch history, a TypeError from a subtraction),
# naming the serializer rather than the argument, identically for `None`,
# `""`, `"not a date"` and an int.
#
# There is no defensible default for "when is now", so the fix is a named
# refusal, not a fallback — the one boundary in this module where guessing
# would be worse than stopping.
# ===========================================================================
BAD_AS_OF = [None, "", "   ", "not a date", "tomorrow", 0, 12345, 3.5,
             [], {}, object(), "2026-13-45", "2026/08/27"]


@pytest.mark.parametrize("bad", BAD_AS_OF)
@pytest.mark.parametrize("touches", [[], HISTORY])
def test_score_contact_refuses_an_unusable_as_of_by_name(bad, touches):
    with pytest.raises(ValueError) as excinfo:
        scoring.score_contact({"id": 1, "role": "Analyst"}, touches, as_of=bad)
    assert "as_of" in str(excinfo.value)


@pytest.mark.parametrize("bad", BAD_AS_OF)
def test_score_firm_refuses_an_unusable_as_of_by_name(bad):
    with pytest.raises(ValueError) as excinfo:
        scoring.score_firm(
            {"id": 1, "regions": ["us"], "tracks": ["ib"]},
            {"id": 1, "regions": ["us"], "tracks": ["ib"], "sponsors": True},
            [{"id": 1, "role": "Analyst"}], HISTORY, [], as_of=bad,
        )
    assert "as_of" in str(excinfo.value)


@pytest.mark.parametrize("good", [
    AS_OF,
    datetime(2026, 8, 27, 12, 0),                 # naive -> assumed UTC
    date(2026, 8, 27),
    "2026-08-27",
    "2026-08-27 12:00:00",
    "2026-08-27T12:00:00+08:00",
])
def test_every_shape_the_docstring_promises_still_works(good):
    """The refusal must not have narrowed what a legitimate caller may pass.
    `_as_dt` documents datetime / date / ISO-8601 string, and the web layer
    passes a Django aware datetime."""
    result = scoring.score_contact({"id": 1, "role": "Analyst"}, HISTORY, as_of=good)
    assert 0.0 <= result["composite"] <= 100.0
    assert result["as_of"].endswith("+00:00")


def test_one_unreadable_touch_timestamp_is_still_skipped_not_fatal():
    """The other half of the same distinction: `ts` keeps its lenient
    contract. Tightening `as_of` must not have tightened this."""
    messy = HISTORY + [
        touch(1, "reply_received", "not a date"),
        touch(1, "chat", None),
        touch(1, "outreach", {}),
    ]
    result = scoring.score_contact({"id": 1, "role": "Analyst"}, messy, as_of=AS_OF)
    assert 0.0 <= result["composite"] <= 100.0


# ===========================================================================
# INVARIANT 2 — the leverage keyword table means what it says.
#
# THE DEFECT THIS PINS. `intern` was matched as a bare SUBSTRING, and it
# sits inside "internal" and "international". An "Internal Audit Manager"
# and an "International Equities" seat both scored 20.0 — the intern tier.
# The direction is the bad one: not a missed promotion but an active
# DEMOTION, below the 30.0 an entirely blank role string would have scored,
# on nothing but a spelling coincidence.
# ===========================================================================
INTERN_TIER = 20.0
UNKNOWN_BASELINE = scoring.DEFAULT_PARAMS["leverage_unknown_role"]

# Real seats whose titles happen to contain the letters "intern".
NOT_INTERNS = [
    "Internal Audit Manager",
    "Internal Audit",
    "International Equities",
    "Head of International Markets",     # also hits "head of" — checked anyway
    "International Private Bank",
    "Internal Communications",
    "Internal Consulting",
    "Internationalisation Lead",
]

# Seats that genuinely ARE the intern tier.
REAL_INTERNS = [
    "Intern",
    "intern",
    "Interns",
    "Internship",
    "Summer Internship",
    "Summer Intern, IBD",
    "Off-cycle Intern",
    "2027 Internship Programme",
]


@pytest.mark.parametrize("role", NOT_INTERNS)
def test_a_role_that_merely_contains_the_letters_intern_is_not_an_intern(role):
    assert scoring._seniority_from_role(role) != INTERN_TIER, role


@pytest.mark.parametrize("role", NOT_INTERNS)
def test_such_a_role_never_scores_below_a_blank_one(role):
    """The property that makes this a bug rather than a mis-tuning: a role
    string carrying real information must never leave a contact worse off
    than saying nothing at all."""
    seniority = scoring._seniority_from_role(role)
    effective = UNKNOWN_BASELINE if seniority is None else seniority
    assert effective >= UNKNOWN_BASELINE, role


@pytest.mark.parametrize("role", REAL_INTERNS)
def test_a_real_intern_still_scores_the_intern_tier(role):
    """The fix only tightens. Every shape the tier was written for still
    lands on it."""
    assert scoring._seniority_from_role(role) == INTERN_TIER, role


@pytest.mark.parametrize("role,expected", [
    ("Managing Director", 100.0),
    ("managing director, TMT", 100.0),
    ("Global Head of M&A", 100.0),
    ("Head of TMT", 95.0),
    ("Executive Director", 90.0),
    ("Director, Coverage", 80.0),
    ("Associate Director", 80.0),      # "director" wins; documented first-hit-wins
    ("Senior Vice President", 65.0),
    ("Vice President, IBD", 65.0),
    ("Principal", 65.0),
    ("Associate", 40.0),
    ("Analyst", 30.0),
    ("Summer Analyst", 30.0),
])
def test_the_rest_of_the_table_is_unchanged(role, expected):
    """The intern entry is the only one that stopped being a plain substring
    match. Every other tier must behave exactly as it did."""
    assert scoring._seniority_from_role(role) == expected, role


@pytest.mark.parametrize("role", [
    None, "", "   ", "admin", "administrator", "junk 123", "?!", "—",
    "Ünïcödé", "x" * 5000, "MDs and VPs of the world",
])
def test_seniority_is_total_over_junk_role_text(role):
    got = scoring._seniority_from_role(role)
    assert got is None or 0.0 <= got <= 100.0


def test_an_abbreviation_still_matches_only_a_whole_token():
    """`_ROLE_TOKENS`' own stated reason for existing: "md" must not fire
    inside "admin". Pinned so the phrase-table refactor cannot have moved
    it."""
    assert scoring._seniority_from_role("admin") is None
    assert scoring._seniority_from_role("administrator") is None
    assert scoring._seniority_from_role("MD") == 100.0
    assert scoring._seniority_from_role("md, healthcare") == 100.0
    assert scoring._seniority_from_role("VP") == 65.0


def test_role_given_and_role_recognised_are_two_different_answers():
    """`seniority is None` answers both "there is no role" and "the role did
    not match the table", which is why `role_given` exists beside it. A
    display layer needs to tell them apart — the contact rail printed "role
    unknown" on pages whose own header printed the role."""
    blank = scoring.score_contact({"id": 1}, HISTORY, as_of=AS_OF)["axes"]["leverage"]
    unmatched = scoring.score_contact(
        {"id": 1, "role": "Internal Audit Manager"}, HISTORY, as_of=AS_OF
    )["axes"]["leverage"]
    assert blank["seniority"] is None and blank["role_given"] is False
    assert unmatched["seniority"] is None and unmatched["role_given"] is True
    # Both fall back to the same baseline score — the DIFFERENCE is only
    # what a page may say about them.
    assert blank["score"] == unmatched["score"]


# ===========================================================================
# INVARIANT 3 — an axis payload has the same keys for every contact.
#
# `axes` is serialized into a snapshot. A field that is present for most
# contacts and absent for the newest one is a KeyError waiting for its first
# reader; `reply_ratio` was written only past the no-touches early return.
# ===========================================================================
CONTACT_HISTORIES = [
    [],
    [touch(1, "outreach", AS_OF - timedelta(days=5))],
    [touch(1, "reply_received", AS_OF - timedelta(days=5))],
    [touch(1, "chat", AS_OF - timedelta(days=5))],
    [touch(1, "thank_you", AS_OF - timedelta(days=5))],
    [touch(1, "manual_override", AS_OF - timedelta(days=5),
           note="manual override: warmth=advocate, thread_state=chat_done")],
    HISTORY,
]


def test_every_contact_axis_carries_the_same_keys_whatever_the_history():
    shapes = set()
    for touches in CONTACT_HISTORIES:
        axes = scoring.score_contact({"id": 1, "role": "Analyst"}, touches,
                                     as_of=AS_OF)["axes"]
        shapes.add(tuple(sorted((a, tuple(sorted(p))) for a, p in axes.items())))
    assert len(shapes) == 1, "an axis payload's key set depends on the history"


def test_every_firm_axis_carries_the_same_keys_whatever_the_inputs():
    user = {"id": 1, "regions": ["us"], "tracks": ["ib"]}
    firm = {"id": 1, "regions": ["us"], "tracks": ["ib"], "sponsors": True}
    dated = [{"event_kind": "app_close", "region": "us",
              "date": TODAY + timedelta(days=10),
              "confidence": "confirmed_official"}]
    shapes = set()
    for contacts, touches, fdates in (
        ([], [], []),
        ([{"id": 1, "role": "Analyst"}], [], []),
        ([{"id": 1, "role": "Analyst"}], HISTORY, []),
        ([{"id": 1, "role": "Analyst"}], HISTORY, dated),
        ([], [], dated),
    ):
        axes = scoring.score_firm(user, firm, contacts, touches, fdates,
                                 as_of=AS_OF)["axes"]
        shapes.add(tuple(sorted((a, tuple(sorted(p))) for a, p in axes.items())))
    assert len(shapes) == 1, "a firm axis payload's key set depends on the inputs"


# ===========================================================================
# INVARIANT 4 — the reasoning line is a template over the axis facts, so it
# must never assert something the axes do not say, and must never raise.
# ===========================================================================
def test_the_reasoning_line_never_raises_over_generated_histories():
    rng = random.Random(SEED)
    kinds = ["outreach", "follow_up", "reping", "reply_received",
             "chat_scheduled", "chat", "thank_you", "maintain", "manual_override"]
    for _ in range(300):
        touches = [
            touch(1, rng.choice(kinds),
                  AS_OF - timedelta(days=rng.randrange(-40, 900)),
                  note="manual override: warmth="
                       + rng.choice(["cold", "replied", "chatted", "advocate", "junk"]))
            for _ in range(rng.randrange(0, 7))
        ]
        result = scoring.score_contact(
            {"id": 1,
             "role": rng.choice([None, "", "Analyst", "Internal Audit",
                                 "Managing Director", "Intern", "admin"]),
             "school_affiliation": rng.choice([True, False, None])},
            touches, as_of=AS_OF,
        )
        line = result["reasoning"]
        assert isinstance(line, str)
        clauses = [c.strip() for c in line.split(";") if c.strip()]
        # No sentence may argue with itself. Each of these pairs is a real
        # contradiction the module has produced at some point.
        assert not ("replied, no chat yet" in clauses and any(
            c.startswith("no reply to") for c in clauses)), line
        assert not ("advocate" in clauses and "no advocate yet" in clauses), line
        assert not ("no reply yet" in clauses and any(
            c.startswith("no reply to") for c in clauses)), line
        # "0 chats" as the evidence that the conversation happened.
        assert "0 chats" not in clauses, line


def test_a_contact_with_no_history_at_all_still_reasons():
    result = scoring.score_contact({"id": 1}, [], as_of=AS_OF)
    assert result["reasoning"] == "no reply yet"
    assert result["band"] == "cold"


# ===========================================================================
# INVARIANT 5 — the arithmetic matches the documented formula. Recomputed by
# hand from the params bundle rather than pinned to a number, so a weight
# change moves the expectation with it.
# ===========================================================================
def test_the_contact_composite_is_exactly_its_documented_weighted_sum():
    p = scoring.DEFAULT_PARAMS
    result = scoring.score_contact(
        {"id": 1, "role": "Vice President", "school_affiliation": True},
        HISTORY, as_of=AS_OF,
    )
    axes, w = result["axes"], p["contact_weights"]
    expected = round(
        w["depth"] * axes["depth"]["score"]
        + w["responsiveness"] * axes["responsiveness"]["score"]
        + w["recency"] * axes["recency"]["score"]
        + w["leverage"] * axes["leverage"]["score"],
        1,
    )
    # Within one rounding step: the composite is computed from the unrounded
    # axis values and rounded once, so recomputing from the ROUNDED axes can
    # differ by at most the rounding of each term.
    assert abs(result["composite"] - expected) <= 0.1


def test_leverage_is_exactly_seniority_plus_the_alumni_bonus():
    p = scoring.DEFAULT_PARAMS
    plain = scoring.score_contact(
        {"id": 1, "role": "Vice President"}, HISTORY, as_of=AS_OF
    )["axes"]["leverage"]
    alum = scoring.score_contact(
        {"id": 1, "role": "Vice President", "school_affiliation": True},
        HISTORY, as_of=AS_OF,
    )["axes"]["leverage"]
    assert plain["score"] == 65.0
    assert alum["score"] == 65.0 + p["leverage_school_bonus"]


def test_the_alumni_bonus_cannot_push_leverage_past_the_ceiling():
    alum = scoring.score_contact(
        {"id": 1, "role": "Managing Director", "school_affiliation": True},
        HISTORY, as_of=AS_OF,
    )["axes"]["leverage"]
    assert alum["score"] == 100.0


@pytest.mark.parametrize("days,expected", [
    (0, 100.0),
    (45, 50.0),     # one half-life
    (90, 25.0),     # two
    (135, 12.5),    # three
])
def test_recency_decays_on_exactly_the_documented_half_life(days, expected):
    result = scoring.score_contact(
        {"id": 1},
        [touch(1, "reply_received", AS_OF - timedelta(days=days))],
        as_of=AS_OF,
    )
    assert result["axes"]["recency"]["score"] == pytest.approx(expected, abs=0.05)


def test_recency_is_the_only_axis_that_moves_with_the_clock():
    """Design guarantee 3, stated as a test: age out a history by a year and
    every other axis must be byte-identical."""
    now = scoring.score_contact({"id": 1, "role": "Analyst"}, HISTORY, as_of=AS_OF)
    later = scoring.score_contact(
        {"id": 1, "role": "Analyst"}, HISTORY, as_of=AS_OF + timedelta(days=365)
    )
    for axis in ("depth", "responsiveness", "leverage"):
        assert now["axes"][axis]["score"] == later["axes"][axis]["score"], axis
    assert later["axes"]["recency"]["score"] < now["axes"]["recency"]["score"]
