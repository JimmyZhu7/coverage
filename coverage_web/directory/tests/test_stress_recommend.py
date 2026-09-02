"""Adversarial invariant suite for the Opportunities personalization path:
`directory.recommend`, `directory.dupes`, and the view-layer pairing between
`recommend()`'s class axis and `views._eligibility`'s blocking verdict.

WHAT MAKES THIS DIFFERENT from `test_recommend.py` and `test_dupes.py`. Those
are example-based: each pins one behaviour against one hand-built fixture, and
between them they document what the ranker is FOR. This file asks the opposite
question — what input breaks a property that is supposed to hold for EVERY
input — and generates the inputs rather than choosing them.

Same discipline and the same tooling decision as `crm/tests/test_stress_crm.py`
and `coverage_domain/tests/test_stress_invariants.py`: NO `hypothesis` (see
that file's header for the three reasons). The interesting spaces here are
small enumerated cross-products — 3 buckets x 5 cohort offsets x 4 class
years, 6 silence cases per filter, 9 region codes — and are walked
EXHAUSTIVELY, which is strictly stronger than sampling them. The one genuinely
unbounded question, "does the ranking depend on the order rows come back from
a queryset with no ORDER BY", uses a seeded shuffle so a counterexample
reproduces.

Nothing here needs a database except the two tests that pair the ranker
against `views._eligibility`, which is where a query genuinely is the thing
under test.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, replace
from datetime import date, timedelta

import pytest

from directory import recommend as R
from directory.dupes import fold_duplicates

SEED = 20260828
TODAY = date(2026, 8, 28)

ALL_BUCKETS = ("internship", "entry_level", "insight")
ALL_TRACKS = ("ib", "st", "pe", "am", "consulting", "corp-strat")
# Every value `Opportunity.region` actually carries on the live board, plus the
# blank. "other" and "global" are stated-but-not-yours; "" is unstated.
ALL_REGIONS = ("", "us", "eu", "hk", "sg", "jp", "cn", "other", "global")


def cand(cid, **kw):
    # `region="global"`, REWRITTEN 2026-09-01 from the `region="other"` that
    # replaced the original blank. The invariants here move one input at a
    # time, so the default has to be the value the region axis is SILENT
    # about, and that value has changed as the axis learned to speak: a blank
    # costs `W_REGION_UNKNOWN`, and "other" — a location we read and placed
    # outside the student's markets — now costs `W_REGION_MISMATCH`. "global"
    # is the posting saying it has no single place, which is the one thing
    # left that is not a market to be right or wrong about. The blank and the
    # mismatch are tested where `ALL_REGIONS` is iterated explicitly and in
    # test_recommend.py / test_picks_personalization.py.
    base = dict(id=cid, firm_id=1, firm_name="Acme Partners", firm_slug="acme",
                title="Summer Analyst", url=f"https://x/{cid}", region="global")
    base.update(kw)
    return R.Candidate(**base)


def profile(**kw):
    base = dict(class_year=2029, school="USC", regions=("us",), tracks=("ib",),
                firm_tiers={1: 1})
    base.update(kw)
    return R.Profile(**base)


# ===========================================================================
# INVARIANT 1 — the ranking is a pure function of the candidate SET.
#
# `directory.views` builds the pick pool from `Opportunity.objects.filter(
# status="open")` with NO `ORDER BY`, so Postgres may hand the same rows back
# in a different order on the next request. A student who reloads and sees a
# different "Picked for you" has been told the ranking means nothing.
# ===========================================================================
def test_recommend_is_invariant_under_input_permutation():
    rng = random.Random(SEED)
    p = profile(regions=("us", "hk"), tracks=("ib", "st"),
                firm_tiers={i: rng.choice([1, 2, 3, None]) for i in range(1, 9)},
                warm_firms={1: "warm", 3: "replied"})
    pool = [
        cand(i, firm_id=rng.randint(1, 8),
             firm_name=rng.choice(["Acme", "Acme", "Beta Bank"]),
             title=rng.choice(["Summer Analyst", "Investment Banking Analyst",
                               "Internal Audit Analyst", "Sales & Trading Intern"]),
             bucket=rng.choice(ALL_BUCKETS), cohort=rng.choice(["", "2026", "2027"]),
             region=rng.choice(ALL_REGIONS), firm_tracks=("ib", "st"),
             deadline=rng.choice([None, TODAY + timedelta(days=3),
                                  TODAY + timedelta(days=90)]))
        for i in range(1, 61)
    ]
    expected = [r.candidate.id for r in R.recommend(p, pool, today=TODAY, limit=99)]
    assert expected, "fixture must actually produce picks or this proves nothing"
    for _ in range(60):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        got = [r.candidate.id for r in R.recommend(p, shuffled, today=TODAY, limit=99)]
        assert got == expected


def test_exact_ties_resolve_the_same_way_round_every_time():
    """Six candidates identical on every scoring axis AND every tiebreak but
    the id. `_sort_key` ends in the id precisely so this cannot shuffle."""
    p = profile()
    tied = [cand(i, firm_id=i, title="Investment Banking Summer Analyst",
                 region="us", firm_tracks=("ib",)) for i in range(1, 7)]
    for firm_id in range(1, 7):
        p = replace(p, firm_tiers={**p.firm_tiers, firm_id: 1})
    expected = [r.candidate.id for r in R.recommend(p, tied, today=TODAY, limit=99)]
    assert expected == [1, 2, 3, 4, 5, 6]
    rng = random.Random(SEED)
    for _ in range(50):
        shuffled = tied[:]
        rng.shuffle(shuffled)
        assert [r.candidate.id for r in
                R.recommend(p, shuffled, today=TODAY, limit=99)] == expected


def test_sort_key_is_a_total_order_over_the_whole_reachable_space():
    """Build every (score, deadline state, firm name, id) combination and
    assert no two distinct candidates ever produce the same key — a key that
    ties is a key that lets the input order decide."""
    keys = {}
    n = 0
    for score, d, name in itertools.product(
        (0, 25, 61, 98),
        (None, TODAY - timedelta(days=1), TODAY, TODAY + timedelta(days=30)),
        ("acme", "Acme", "Beta"),
    ):
        n += 1
        rec = R.Recommendation(candidate=cand(n, firm_name=name, deadline=d),
                               score=score, reasons=())
        key = R._sort_key(rec, TODAY)
        assert key not in keys, f"tie between candidates {keys.get(key)} and {n}"
        keys[key] = n
    assert n == 48


# ===========================================================================
# INVARIANT 2 — the caps actually cap.
# ===========================================================================
def test_limit_and_max_per_firm_hold_across_the_whole_argument_space():
    p = profile(firm_tiers={1: 1, 2: 1, 3: 1})
    pool = [cand(i, firm_id=(i % 3) + 1, title="Investment Banking Analyst",
                 region="us", firm_tracks=("ib",)) for i in range(1, 25)]
    for limit, mpf in itertools.product((0, 1, 2, 6, 24, 1000), (0, 1, 2, 5)):
        out = R.recommend(p, pool, today=TODAY, limit=limit, max_per_firm=mpf)
        assert len(out) <= limit, f"limit={limit} returned {len(out)}"
        per_firm = {}
        for r in out:
            per_firm[r.candidate.firm_id] = per_firm.get(r.candidate.firm_id, 0) + 1
        assert all(v <= mpf for v in per_firm.values()), f"max_per_firm={mpf}"


def test_limit_zero_returns_nothing():
    """`if len(picked) >= limit: break` used to sit at the FOOT of the loop,
    so it tested a list that had already grown: limit=0 returned one
    recommendation and limit=-1 returned one too."""
    p = profile()
    pool = [cand(i, title="Investment Banking Analyst", region="us",
                 firm_tracks=("ib",)) for i in range(1, 5)]
    assert R.recommend(p, pool, today=TODAY, limit=0) == []
    assert R.recommend(p, pool, today=TODAY, limit=-1) == []


def test_min_score_floors_everything_returned():
    rng = random.Random(SEED)
    p = profile(regions=("us",), tracks=("ib", "st"), firm_tiers={1: 1, 2: 2, 3: 3})
    pool = [cand(i, firm_id=rng.randint(1, 4), region=rng.choice(ALL_REGIONS),
                 title=rng.choice(["Summer Analyst", "Investment Banking Analyst",
                                   "Internal Audit Analyst"]),
                 firm_tracks=("ib",), bucket="internship",
                 cohort=rng.choice(["", "2027"])) for i in range(1, 41)]
    for floor in (-100, 0, 25, 40, 60, 999):
        for r in R.recommend(p, pool, today=TODAY, limit=99, min_score=floor):
            assert r.score >= floor


# ===========================================================================
# INVARIANT 3 — three kinds of role can never be a pick, whatever they score.
# ===========================================================================
def test_a_blocked_candidate_is_never_returned_at_any_score():
    p = profile(warm_firms={1: "warm"})
    for title, region, cohort in itertools.product(
        ("Summer Analyst", "Investment Banking Summer Analyst"),
        ("us", "hk", ""), ("", "2027"),
    ):
        c = cand(1, title=title, region=region, cohort=cohort, bucket="internship",
                 firm_tracks=("ib", "st"), blocked=True)
        assert R.recommend(p, [c], today=TODAY, min_score=-999) == []


def test_a_passed_deadline_is_never_returned():
    p = profile(warm_firms={1: "warm"})
    for days in (1, 2, 30, 400):
        c = cand(1, title="Investment Banking Analyst", region="us",
                 firm_tracks=("ib",), deadline=TODAY - timedelta(days=days))
        assert R.recommend(p, [c], today=TODAY, min_score=-999) == []
    # ...and the boundary: closing TODAY is still open.
    today_c = cand(2, title="Investment Banking Analyst", region="us",
                   firm_tracks=("ib",), deadline=TODAY)
    assert [r.candidate.id for r in R.recommend(p, [today_c], today=TODAY)] == [2]


def test_a_stated_class_mismatch_is_never_returned_however_much_else_stacks():
    """The posting's own words settle who it is for, including settling it
    against this student. `W_CLASS_STATED_MISMATCH` is only -25 and every
    other axis stacked together reaches +86, so the subtraction alone cannot
    carry this — the veto in `recommend()` does."""
    p = profile(school="USC", regions=("us",), tracks=("ib",),
                firm_tiers={1: 1}, warm_firms={1: "warm"},
                target_cycles=("2027 Summer Internship",))
    for stated, grad in (("2026", ()), ("2030", ()), ("", ("2026", "2027")),
                         ("", ("2031",))):
        c = cand(1, class_year=stated, grad_years=grad, region="us",
                 bucket="internship", cohort="2027",
                 title="Investment Banking Summer Analyst", firm_tracks=("ib",))
        assert R.score_candidate(p, c)[0] >= R.MIN_SCORE, "fixture must score high"
        assert R.recommend(p, [c], today=TODAY) == [], (stated, grad)


def test_the_veto_fires_only_on_STATED_words_never_on_the_intake_year():
    """Silence never hides, and an ADJACENT intake year still shows.

    REWRITTEN 2026-09-01. This pinned every intake from 2024 to 2030 as a
    pick for a 2029 student, on the argument that an implied class is
    inference and inference must not exclude. Two years off and more is
    now excluded by `role_matches_level` inside `recommend()` — the exact
    ladder `_class_fit` already scored (0 match, 1 near, 2+ nothing) and
    the exact rule the advisor's snapshot and the digest already filtered
    on, so the picks stop being the one surface that disagreed. What the
    old test was really protecting survives in three parts: a role with
    NO intake year shows; an adjacent year shows (labelled); and a posting
    whose own STATED words name the student's class shows however far off
    its intake year reads, because stated words outrank the inference."""
    p = profile(class_year=2029, tracks=("ib",), firm_tiers={1: 1})

    def picked(**kw):
        c = cand(1, bucket="internship", region="us", firm_tracks=("ib",),
                 title="Investment Banking Summer Analyst", **kw)
        return [r.candidate.id for r in R.recommend(p, [c], today=TODAY)] == [1]

    assert picked(cohort="")                       # silence never hides
    for cohort in ("2027", "2028", "2029"):        # implied 2028/2029/2030
        assert picked(cohort=cohort), cohort       # exact or one off: shows
    for cohort in ("2024", "2025", "2026", "2030"):
        assert not picked(cohort=cohort), cohort   # two or more off: out
        # ...unless the posting states the student's class in so many words.
        assert picked(cohort=cohort, class_year="2029"), cohort
        assert picked(cohort=cohort, grad_years=("2028", "2029")), cohort


def test_stated_class_mismatch_agrees_with_the_class_axis_sign():
    """`stated_class_mismatch` and `_class_fit` must never disagree: they are
    the same question, and the whole reason the window was factored out is
    that two features reading one fact and answering differently is the exact
    inconsistency this module keeps having to fix."""
    for cy, stated, grad in itertools.product(
        (None, 2027, 2029),
        ("", "2026", "2029", "not-a-year"),
        ((), ("2029",), ("2026", "2027"), ("2027", "2029")),
    ):
        p = profile(class_year=cy, firm_tiers={})
        c = cand(1, class_year=stated, grad_years=grad, bucket="internship")
        points, _ = R._class_fit(p, c)
        assert R.stated_class_mismatch(p, c) == (points == R.W_CLASS_STATED_MISMATCH)


def test_a_mismatch_chip_never_reads_like_a_match_chip():
    """`Recommendation.why` joins reasons on `.text` alone, so a chip
    arguing AGAINST a role must not be byte-identical to one arguing for it."""
    for year in ("2026", "2027", "2030"):
        bad = R._class_fit(profile(class_year=2029), cand(1, class_year=year))
        good = R._class_fit(profile(class_year=int(year)), cand(1, class_year=year))
        assert bad[1][0].text != good[1][0].text


# ===========================================================================
# INVARIANT 4 — silence never hides, at every layer that documents it.
# ===========================================================================
def test_role_matches_tracks_silence_rules_exhaustively():
    for tracks in ((), ("ib",), ("ib", "st"), ALL_TRACKS):
        # A student who stated nothing filters nothing, whatever the title.
        for title in ("", "Summer Analyst", "Internal Audit Analyst",
                      "Investment Banking Analyst"):
            if not tracks:
                assert R.role_matches_tracks(title, tracks) is True
    # A silent TITLE has not earned "relevant" once the student HAS spoken —
    # this filter is an allowlist by design (see its docstring).
    assert R.role_matches_tracks("Summer Analyst", ("ib",)) is False
    assert R.role_matches_tracks("Investment Banking Analyst", ("ib",)) is True
    assert R.role_matches_tracks("Investment Banking Analyst", ("st",)) is False
    assert R.role_matches_tracks("Internal Audit Analyst", ("ib",)) is False


def test_role_matches_regions_silence_rules_exhaustively():
    for region in ALL_REGIONS:
        assert R.role_matches_regions(region, ()) is True
    for region in ALL_REGIONS:
        assert R.role_matches_regions(region, ("us",)) is (region == "us")


def test_role_matches_level_passes_whenever_either_side_is_silent():
    for bucket, derived, cycles, cy in itertools.product(
        ("", *ALL_BUCKETS), ("", "2028"), ((), ("nonsense",)), (None, 2029),
    ):
        # No bucket, or no parseable cycle, or no stated class year on one
        # side of each check: nothing stated means nothing filtered.
        assert R.role_matches_level(bucket, derived, cycles, cy) is True


def test_role_matches_level_only_excludes_what_both_sides_stated():
    assert R.role_matches_level("entry_level", "", ("2028 Summer Internship",), 2029) is False
    assert R.role_matches_level("internship", "", ("2028 Summer Internship",), 2029) is True
    # gap of 1 is "worth a look", 2+ is out — the same ladder `_class_fit` scores.
    assert R.role_matches_level("internship", "2028", (), 2029) is True
    assert R.role_matches_level("internship", "2027", (), 2029) is False


# ===========================================================================
# INVARIANT 5 — `role_function` is a claim about the JOB, and the blocklist
# is not allowed to answer a question it was not asked.
# ===========================================================================
@pytest.mark.parametrize("title,expected", [
    # A blocklist word INSIDE the phrase that names the track is one word of a
    # longer, more specific claim — not a competing one. `\boperations?\b`
    # sits inside the consulting pattern's own `\bstrategy (and|&)
    # operations\b`, which made that clause unreachable for every one of the 8
    # open campus rows carrying it (PwC, Deloitte).
    ("Consulting - Associate - Strategy & Operations (Talent Pool)", "consulting"),
    ("Consulting - Finance Strategy & Operations Off Cycle Internship", "consulting"),
    ("Strategy and Operations Summer Analyst", "consulting"),
    # A blocklist word that only names the HIRING PROCESS is not a claim about
    # the job either — but only when the title separately names a track.
    ("2027 Campus Recruiting - Investment Banking Summer Associate - Houston", "ib"),
    ("Graduate Recruitment 2027 - Sales & Trading", "st"),
    ("Quantitative Strategies & Data Group | Recruitment Event", "st"),
    # ...and these must STAY "none": the hiring-process words are the whole job.
    ("Campus Recruiting Coordinator", "none"),
    ("Early Careers Recruitment - Coordination Specialist", "none"),
    ("Audit Trainee - Recruitment Days 8&9th of October 2026", "none"),
    ("APAC Virtual Recruitment Event | A Career with Bank of America", "none"),
    # The documented behaviour that must not regress: where you sit is not
    # what you do, and a blocklist word OUTSIDE the track phrase still wins.
    ("2027 Commercial & Investment Bank Risk Management Summer Analyst", "none"),
    ("Trading Operations Analyst Internship: Summer 2027", "none"),
    ("Wealth Management Operations - Seasonal/Off Cycle Internship", "none"),
    ("Capital Markets & Accounting Advisory (CMAA) - Associate", "none"),
    ("Relationship Banker | Meadowbrook Branch", "none"),
    # And silence stays silence — not "none", which would be a claim.
    ("Summer Analyst", ""),
    ("Intern", ""),
    ("", ""),
])
def test_role_function_verdicts(title, expected):
    assert R.role_function(title) == expected


def test_role_function_is_total_and_never_raises():
    for title in ("", "   ", "\x00", "🙂", "a" * 5000, "-" * 100,
                  "Investment Banking / Sales & Trading / Private Equity"):
        assert R.role_function(title) in ("", "none", *ALL_TRACKS)


# ===========================================================================
# INVARIANT 6 — parsers are total: they answer or they decline, never raise,
# and the dropdown the settings page renders always round-trips.
# ===========================================================================
@pytest.mark.parametrize("value", [
    "", "   ", None, "2028", "SA", "SA 2028", "sa 2028", "SA-2028", "SA 2028\n",
    "2028 Summer Internship", " 2028 Summer Internship ", "2028\tSummer Internship",
    "Summer Internship 2028", "Off-Cycle / Immediate", "off-cycle / immediate",
    "2028 Summer Internship extra", "20281 Summer Internship", "SA 20281",
    "Full-Time / Graduate 2028", "2028 Full-Time / Graduate", "\x00", "🙂",
    "sa2028_ib", "a" * 500 + " 2028",
])
def test_parse_target_cycle_never_raises(value):
    got = R.parse_target_cycle(value)
    assert got is None or (got[0] in ALL_BUCKETS and isinstance(got[1], int))


def test_every_dropdown_choice_round_trips():
    """`cycle_choices` is the producer and `parse_target_cycle` the consumer,
    sharing `CYCLE_LABELS` so they cannot drift. Before that they had: all
    eight year-first choices scored zero on the 15-point cycle axis for every
    user because only the legacy "SA 2028" shape parsed."""
    for base in (2024, 2026, 2027, 2030, 2090):
        for value, _label in R.cycle_choices(base_year=base)[1:]:
            assert R.parse_target_cycle(value) is not None, (base, value)


# ===========================================================================
# INVARIANT 7 — `school_region` adds signal for names it knows and abstains
# everywhere else. It must never guess, and it must never raise.
# ===========================================================================
@pytest.mark.parametrize("school,expected", [
    # The founder's own account stores the spelled-out name, and it used to
    # resolve to "" — zeroing the highest-weighted region signal (20) for the
    # only real user of the product.
    ("University of Southern California", "us"),
    ("USC", "us"), ("USC Marshall", "us"),
    ("Massachusetts Institute of Technology", "us"),
    ("University of Pennsylvania", "us"),
    ("Carnegie Mellon University", "us"),
    ("University of Oxford", "eu"),
    ("University of Cambridge", "eu"),
    # The other word order for the same two names — "Oxford University,"
    # "Cambridge University" — is at least as common as "University of X"
    # and was missing entirely: same silent-zero bug as USC above, for the
    # same reason (only one spelling of a two-spelling name was in the
    # table). Safe without the bare-word ambiguity guard: nothing else on
    # earth calls itself "Oxford University" or "Cambridge University".
    ("Oxford University", "eu"),
    ("Cambridge University", "eu"),
    ("London School of Economics and Political Science", "eu"),
    ("Nanyang Technological University", "sg"),
    ("The University of Hong Kong", "hk"),
    ("Tsinghua University", "cn"),
    # The deliberate abstentions, which must STAY abstentions: a name that is
    # only unambiguous in its long form gets no answer in its short one.
    ("Cambridge", ""), ("Oxford", ""), ("SMU", ""), ("", ""), ("   ", ""),
])
def test_school_region_known_names_and_deliberate_abstentions(school, expected):
    assert R.school_region(school) == expected


def test_school_region_is_total_and_never_raises():
    for s in (None, "", "   ", "\x00", "🙂", "x" * 5000, "-" * 80, "12345"):
        assert R.school_region(s) in ("", "other", "global", *R.REGION_FULL)


# ===========================================================================
# INVARIANT 8 — the weight ladder. These are the orderings the module states
# in prose; a future retune that breaks one breaks a documented promise.
# ===========================================================================
def test_the_documented_weight_ladder_holds():
    # Evidence outranks inference: a track the ROLE states beats the ceiling
    # on one merely inherited from the firm's coverage.
    assert R.W_TRACK_STATED > R.W_TRACK_CAP
    # No amount of inferred track accumulation reaches a stated class match.
    assert R.W_TRACK_CAP < R.W_CLASS_STATED
    assert R.W_TRACK_FIRST + R.W_TRACK_EXTRA * 5 >= R.W_TRACK_CAP  # cap can bind
    # A statement outranks an inference on the class axis too.
    assert R.W_CLASS_STATED > R.W_CLASS_DERIVED > R.W_CLASS_DERIVED_NEAR
    # The student's own campus market outranks a region they merely named.
    assert R.W_REGION_SCHOOL > R.W_REGION_TARGET
    # A conversation outranks a reply; neither outruns the class axis.
    assert R.W_NETWORK_WARM > R.W_NETWORK_REPLIED > 0
    assert R.W_NETWORK_WARM < R.W_CLASS_STATED
    # Tier 1 alone clears the bar; tier 3 alone does not.
    assert R.TIER_POINTS[1] >= R.MIN_SCORE > R.TIER_POINTS[3]
    assert R.TIER_POINTS[1] > R.TIER_POINTS[2] > R.TIER_POINTS[3] > R.W_TARGET_UNTIERED
    # No single weak input is a recommendation on its own.
    assert R.W_TRACK_FIRST < R.MIN_SCORE
    assert R.W_REGION_SCHOOL < R.MIN_SCORE


def test_track_cap_binds_so_broad_coverage_is_not_a_better_match():
    """A firm covering all six tracks must not out-score one covering the
    student's exact two."""
    p = profile(tracks=ALL_TRACKS, firm_tiers={})
    broad = cand(1, title="Summer Analyst", firm_tracks=ALL_TRACKS)
    narrow = cand(2, title="Summer Analyst", firm_tracks=("ib", "st"))
    assert R.score_candidate(p, broad)[0] == R.W_TRACK_CAP
    assert R.score_candidate(p, broad)[0] - R.score_candidate(p, narrow)[0] <= 1


# ===========================================================================
# INVARIANT 9 — degenerate profiles. A brand-new account must get an honest
# empty state, not a crash and not six generic cards pretending to be
# tailored.
# ===========================================================================
def test_every_empty_profile_shape_returns_an_honest_nothing():
    pool = [cand(i, title="Investment Banking Analyst", region="us",
                 firm_tracks=("ib",), bucket="internship", cohort="2027")
            for i in range(1, 8)]
    assert R.Profile().is_empty is True
    assert R.recommend(R.Profile(), pool, today=TODAY) == []
    # Whitespace-only school is still empty — `is_empty` strips it.
    assert R.Profile(school="   ").is_empty is True
    assert R.recommend(R.Profile(school="   "), pool, today=TODAY) == []


def test_partially_empty_profiles_score_without_crashing():
    """Every single-signal profile: no school, no tracks, no regions, no
    graduation year, no firms — each on its own, and none of them may raise."""
    pool = [cand(i, title="Investment Banking Analyst", region="us",
                 firm_tracks=("ib",), bucket="internship", cohort="2027",
                 grad_years=("2029",), deadline=TODAY + timedelta(days=10))
            for i in range(1, 5)]
    singles = [
        dict(class_year=2029), dict(target_cycles=("2027 Summer Internship",)),
        dict(school="USC"), dict(regions=("us",)), dict(tracks=("ib",)),
        dict(firm_tiers={1: 1}), dict(firm_tiers={1: None}),
        dict(warm_firms={1: "warm"}), dict(warm_firms={1: "replied"}),
        dict(school="Nowhere Polytechnic"), dict(class_year=1900),
        dict(regions=("nowhere",)), dict(tracks=("nonsense",)),
    ]
    for kw in singles:
        p = R.Profile(**kw)
        assert p.is_empty is False, kw
        out = R.recommend(p, pool, today=TODAY, min_score=-999)
        assert all(isinstance(r.score, int) for r in out), kw


# ===========================================================================
# INVARIANT 10 — `fold_duplicates` is idempotent and never folds two
# genuinely distinct postings. A false SPLIT costs a student a scroll; a
# false FOLD costs them a job they never saw.
# ===========================================================================
@dataclass
class Row:
    id: int
    firm_id: int = 1
    title: str = "Summer Analyst"
    location: str = "London"
    url: str = ""
    deadline: date | None = None
    cohort: str = ""
    sponsorship: str = ""
    first_seen: date | None = None


def test_fold_duplicates_is_idempotent_over_the_whole_cross_product():
    rows = [
        Row(i, firm_id=f, title=t, location=loc, deadline=d, cohort=co)
        for i, (f, t, loc, d, co) in enumerate(itertools.product(
            (1, 2),
            ("Summer Analyst", "Summer Analyst, London", "Spring Week"),
            ("London", "New York", ""),
            (None, TODAY, TODAY + timedelta(days=5)),
            ("", "2026", "2027"),
        ), start=1)
    ]
    once, n1 = fold_duplicates(rows)
    twice, n2 = fold_duplicates(once)
    assert [r.id for r in once] == [r.id for r in twice]
    assert n2 == 0, "a second fold found more to fold — the first was not complete"
    assert n1 == len(rows) - len(once)


def test_fold_duplicates_never_folds_a_genuinely_distinct_posting():
    """Each pair below differs on exactly one thing the module treats as a
    hard divider, and must survive as two rows."""
    pairs = [
        ("different firm", Row(1, firm_id=1), Row(2, firm_id=2)),
        ("different city", Row(1, location="London"), Row(2, location="New York")),
        ("different words", Row(1, title="Summer Analyst"),
         Row(2, title="Spring Week")),
        ("two stated deadlines", Row(1, deadline=TODAY),
         Row(2, deadline=TODAY + timedelta(days=20))),
        ("two stated cohorts", Row(1, cohort="2026"), Row(2, cohort="2027")),
    ]
    for label, a, b in pairs:
        kept, folded = fold_duplicates([a, b])
        assert folded == 0, label
        assert [r.id for r in kept] == [1, 2], label


def test_fold_duplicates_survivor_does_not_depend_on_input_order():
    rng = random.Random(SEED)
    rows = [Row(i, deadline=None if i % 3 else TODAY,
                location="" if i % 4 == 0 else "London",
                sponsorship="yes" if i % 5 == 0 else "",
                first_seen=TODAY - timedelta(days=i))
            for i in range(1, 13)]
    expected = sorted(r.id for r in fold_duplicates(rows)[0])
    for _ in range(50):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        assert sorted(r.id for r in fold_duplicates(shuffled)[0]) == expected


# ===========================================================================
# INVARIANT 11 — the ranker and the eligibility lens must never disagree
# about the same posting. Two features reading one fact and answering
# differently is the defect this module keeps having to re-fix.
# ===========================================================================
@pytest.mark.django_db
def test_the_class_veto_and_the_eligibility_verdict_agree_row_by_row():
    from directory.models import Firm, Opportunity
    from directory import views as V

    firm = Firm.objects.create(name="Acme", slug="acme", tracks=["ib"])
    p = R.Profile(class_year=2029, tracks=("ib",), firm_tiers={firm.id: 1})
    elig = {"class_year": 2029, "work_auth": {}}

    for stated, years in itertools.product(
        ("", "2027", "2029"), (None, ["2029"], ["2026", "2027"], ["2027", "2029"]),
    ):
        raw = {"facts": {"grad": {"value": "x", "years": years,
                                  "phrase": "p"}}} if years else {}
        o = Opportunity.objects.create(
            firm=firm, title="Investment Banking Summer Analyst",
            url=f"https://x/{stated}/{years}", bucket="internship",
            class_year=stated, region="us", raw=raw, status="open",
        )
        verdict = V._eligibility(o, elig)
        blocking = bool(verdict and verdict["blocking"])
        mismatch = R.stated_class_mismatch(p, R.Candidate.from_opportunity(o))
        assert blocking == mismatch, (stated, years, verdict)


@pytest.mark.django_db
def test_the_feed_and_the_picks_run_off_one_clock(client, monkeypatch):
    """`recommend()` keeps itself free of Django and so defaults `today` to
    `date.today()` — the SERVER's local date — while every other date-sensitive
    surface on this page reads `timezone.localdate()`, i.e. the date in
    `settings.TIME_ZONE`. On a host whose OS clock is not UTC those are a
    different day for part of every day (eight hours of it on the founder's own
    machine), and in that window the picks dropped a role as expired that the
    feed beside them still rendered as closing today. The view must pass its
    own clock in."""
    from django.utils import timezone
    from directory import views as V
    from .test_tracking import _user

    seen = {}
    real = V.recommend

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(V, "recommend", spy)
    user = _user()
    # `recommend()` is skipped entirely for an empty profile, so the spy needs
    # a student who has told the product something.
    user.class_year = 2029
    user.tracks = ["ib"]
    user.regions = ["us"]
    user.school = "University of Southern California"
    user.save()
    client.force_login(user)
    client.get("/opportunities/")
    assert seen.get("today") == timezone.localdate(), (
        "picks must be scored against the same date the feed renders")


@pytest.mark.django_db
def test_a_role_the_eligibility_lens_blocks_is_never_a_pick():
    from directory.models import Firm, Opportunity
    from directory import views as V

    firm = Firm.objects.create(name="Acme", slug="acme", tracks=["ib", "st"])
    p = R.Profile(class_year=2029, school="University of Southern California",
                  regions=("us",), tracks=("ib",), firm_tiers={firm.id: 1},
                  warm_firms={firm.id: "warm"})
    elig = {"class_year": 2029, "work_auth": {"us": "sponsorship"}}

    blocked_year = Opportunity.objects.create(
        firm=firm, title="Investment Banking Summer Analyst", url="https://x/1",
        bucket="internship", class_year="2026", region="us", status="open")
    blocked_visa = Opportunity.objects.create(
        firm=firm, title="Investment Banking Summer Analyst", url="https://x/2",
        bucket="internship", region="us", sponsorship="no", status="open")
    fine = Opportunity.objects.create(
        firm=firm, title="Investment Banking Summer Analyst", url="https://x/3",
        bucket="internship", region="us", status="open")

    cands = [R.Candidate.from_opportunity(
        o, blocked=bool((lambda v: v and v["blocking"])(V._eligibility(o, elig))))
        for o in (blocked_year, blocked_visa, fine)]
    got = [r.candidate.id for r in R.recommend(p, cands, today=TODAY)]
    assert got == [fine.id]
