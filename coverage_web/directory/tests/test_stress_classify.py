"""Adversarial invariant suite for `directory.classify` — the bucket/region
classifier upstream of everything the Opportunities feed and every firm page
render.

Companion to `directory/tests/test_stress_recommend.py` and
`crm/tests/test_stress_crm.py`. Same discipline, same reasoning about
tooling: NO `hypothesis` (see `test_stress_recommend.py`'s header for the
three reasons). The interesting spaces here are small enumerated
cross-products — ~19 senior/experienced words x ~14 campus signals, the
entire `_REGION_KEYS` table (a few hundred entries) against itself, every
`_CAMPUS_CONTRACT_KEYS` filing — and are walked EXHAUSTIVELY, which is
strictly stronger than sampling them.

Nothing here needs a database or the boards catalog beyond the pure functions
`directory.classify` and `directory.boards` already export; `campus_hint_pairs`
is exercised against small hand-built board lists, not the live catalog,
because the invariant under test is about the AGGREGATION rule, not any one
firm's current board shape.
"""

from __future__ import annotations

import itertools

import pytest

from directory.boards import BOARDS
from directory.classify import (
    ENTRY_LEVEL,
    INSIGHT,
    INTERNSHIP,
    OTHER,
    TRACKED_REGIONS,
    _EXACT_CITY,
    _GLOBAL_KEYS,
    _OTHER_MARKET_KEYS,
    _PLACELESS_EXACT,
    _REGION_KEYS,
    _US_STATE_KEYS,
    board_is_campus,
    bucket_from_contract,
    campus_hint_pairs,
    classify_role,
    normalize_region,
)


# ===========================================================================
# INVARIANT 1 — the experienced/HR veto beats every full-time-campus signal,
# for every word-form of each, checked in the actual rule order
# (`classify_role`'s step 3 runs before step 4).
#
# This is precision's whole promise stated as code: "Graduate Recruitment
# Manager" (the module's own worked example) must land in `other`, and so
# must every OTHER senior/experienced word paired with every OTHER campus
# phrase — not just the one pair someone remembered to write an example-based
# test for. A regression here (say, a future edit that moves the veto below
# the entry check "to catch one more graduate title") would silently put
# experienced hires back on the campus feed across the whole matrix at once,
# which is exactly the "false positive costs trust" failure classify.py's
# own module docstring names as the thing precision-first exists to prevent.
# ===========================================================================

_SENIOR_WORDS = [
    "Senior", "Sr.", "SVP", "EVP", "MD", "Executive Director", "C15", "VP",
    "Vice President", "Director", "Principal", "Head of Growth", "Chief",
    "Manager", "Experienced", "Mid-Level", "Lateral", "Recruiter",
    "Talent Acquisition",
]
_ENTRY_WORDS = [
    "Graduate", "New Grad", "Campus", "Entry-Level", "Full-Time Analyst",
    "Analyst Program", "Rotational Program", "Trainee", "Apprenticeship",
    "University Hire", "Early Careers", "New Analyst", "WMP Analyst",
    "Class of 2027",
]


@pytest.mark.parametrize("senior,entry", list(itertools.product(_SENIOR_WORDS, _ENTRY_WORDS)))
def test_senior_veto_beats_every_entry_signal(senior, entry):
    assert classify_role(f"{senior} {entry}") == OTHER


# The reverse pairing is equally load-bearing and equally deliberate: an
# internship or insight phrase runs BEFORE the veto, so "2027 Campus
# Recruiting - Investment Banking Summer Associate" (a real Piper Sandler
# row — "Recruiting" is a veto word) is still an internship, and "Chief
# Financial Officer | Virtual Insight Event" (a real Bank of America row —
# "Chief" is a veto word) is still an insight event. Losing this the other
# way — the veto swallowing internships/insight events named after a
# senior-sounding department — would silently empty those buckets of any
# posting whose HOST function has an executive-sounding name.
_INTERNSHIP_PHRASES = ["Summer Analyst", "Summer Associate", "Off-Cycle Internship",
                        "Co-op", "Intern"]
_INSIGHT_PHRASES = ["Insight Day", "Insight Programme", "Spring Week",
                     "Virtual Event", "Recruitment Event"]


@pytest.mark.parametrize("senior,phrase", list(itertools.product(_SENIOR_WORDS, _INTERNSHIP_PHRASES)))
def test_internship_signal_beats_senior_veto(senior, phrase):
    assert classify_role(f"{senior} {phrase}") == INTERNSHIP


@pytest.mark.parametrize("senior,phrase", list(itertools.product(_SENIOR_WORDS, _INSIGHT_PHRASES)))
def test_insight_signal_beats_senior_veto(senior, phrase):
    assert classify_role(f"{senior} {phrase}") == INSIGHT


# ===========================================================================
# INVARIANT 2 — every key in the region tables round-trips to its own code
# when it is the WHOLE location string.
#
# `_REGION_KEYS` is a few hundred hand-curated substrings, each added on a
# specific date to fix a specific live row (the comments above the table are
# a changelog of exactly that). The thing that keeps a table like this safe
# to keep growing is that no later addition can silently shadow an earlier
# one — a `", rou"` added for Romania must not also fire for a Hong Kong
# addresses, an `"india"` guard must not eat a real Indian city. Walking
# every key against `normalize_region` and asserting it answers its OWN code
# is a full-table round trip: it would have caught the ", de"/", denmark"
# collision the code comments describe as "caught once by hand" — from now
# on it's caught by the suite, not by a live row surfacing in the wrong
# market.
#
# US state-suffix keys (", ny", ...) are excluded: `normalize_region`
# deliberately does not test them as bare substrings (that was the live bug
# the `_STATE_SUFFIX` regex replaced), so they are not valid inputs to this
# particular round trip on their own.
# ===========================================================================

_REGION_KEY_CASES = [
    (code, key)
    for code, keys in _REGION_KEYS
    for key in keys
    if key not in _US_STATE_KEYS
]


@pytest.mark.parametrize("code,key", _REGION_KEY_CASES)
def test_region_key_round_trips_to_its_own_code(code, key):
    assert normalize_region(key) == code


@pytest.mark.parametrize("key", _OTHER_MARKET_KEYS)
def test_other_market_key_round_trips_to_other(key):
    assert normalize_region(key) == "other"


@pytest.mark.parametrize("key,code", sorted(_EXACT_CITY.items()))
def test_exact_city_round_trips_to_its_own_code(key, code):
    assert normalize_region(key) == code


@pytest.mark.parametrize("key", _GLOBAL_KEYS)
def test_global_key_round_trips_to_global(key):
    assert normalize_region(key) == "global"


def test_placeless_exact_vocabulary_round_trips_to_global():
    for word in _PLACELESS_EXACT:
        assert normalize_region(word) == "global"


# Every tracked region code actually appears at least once in the key table
# — a code a student can select in Settings (`TRACKED_REGIONS`) that no key
# maps to would be a filter nothing can ever satisfy.
def test_every_tracked_region_has_at_least_one_key():
    codes_present = {code for code, keys in _REGION_KEYS if keys}
    for code in TRACKED_REGIONS:
        assert code in codes_present, f"{code} has no key in _REGION_KEYS"


# ===========================================================================
# INVARIANT 3 — `campus_hint_pairs` requires EVERY board sharing a (slug,
# provider) pair to agree, not just one of them.
#
# The bug this guards: `reclassify` only knows a stored row's provider
# ("workday", "greenhouse"), never which of a firm's several boards on that
# provider produced it. Solomon Partners and Citi each run one campus board
# and one non-campus board on the same provider in the LIVE catalog — see
# `test_live_catalog_collisions_stay_unhinted` below, which pins today's
# actual two. This section instead walks the general rule with synthetic
# boards, so the invariant holds regardless of how the live catalog changes:
# add a third firm with a split board tomorrow and this still catches it if
# `all()` is ever weakened back to `any()`.
# ===========================================================================


class _FakeBoard:
    def __init__(self, provider, **kw):
        self.provider = provider
        for k, v in kw.items():
            setattr(self, k, v)


# Every combination of 1-3 boards' campus-ness for one (slug, provider) pair.
# A pair counts iff every board in the combination is campus-scoped.
@pytest.mark.parametrize(
    "verdicts",
    [combo for n in (1, 2, 3) for combo in itertools.product([True, False], repeat=n)],
)
def test_campus_hint_pairs_requires_unanimous_agreement(verdicts):
    boards = [
        ("acme", _FakeBoard("workday", site="Campus_Careers" if v else "External"))
        for v in verdicts
    ]
    result = campus_hint_pairs(boards)
    expected = frozenset({("acme", "workday")}) if all(verdicts) else frozenset()
    assert result == expected


def test_campus_hint_pairs_keeps_unrelated_pairs_independent():
    # A collision on one (slug, provider) pair must never leak into another
    # pair, whether it differs by slug or by provider.
    boards = [
        ("acme", _FakeBoard("workday", site="Campus_Careers")),
        ("acme", _FakeBoard("workday", site="External")),          # collides -> dropped
        ("acme", _FakeBoard("greenhouse", token="acme-campus")),   # different provider
        ("beta", _FakeBoard("workday", site="Campus_Careers")),    # different slug
    ]
    assert campus_hint_pairs(boards) == frozenset({
        ("acme", "greenhouse"),
        ("beta", "workday"),
    })


def test_live_catalog_collisions_stay_unhinted():
    """Pins the two real (slug, provider) pairs known to disagree across
    boards as of this audit — Solomon Partners' campus-vs-professionals
    Greenhouse boards, Citi's early-careers-vs-generic Workday sites. If the
    catalog changes so neither collides any more, this test should be
    revisited rather than deleted outright: the scenario it protects against
    (a false campus_hint promoting an experienced hire) is still real for
    whatever firm next runs two boards on the same provider."""
    pairs = campus_hint_pairs(BOARDS)
    assert ("solomonpartners", "greenhouse") not in pairs
    assert ("citi", "workday") not in pairs
    # And the sanity check: at least one campus-only firm (a single board on
    # its provider) still comes through, so the fix isn't just "always empty".
    assert ("blackstone", "workday") in pairs


def test_campus_hint_pairs_matches_board_is_campus_when_unambiguous():
    # Cross-check against the live catalog: every pair NOT flagged as a
    # known collision must equal the plain "does its one-and-only verdict
    # say campus" answer, i.e. this function must not diverge from
    # `board_is_campus` on the non-adversarial common case.
    from collections import defaultdict

    by_pair = defaultdict(list)
    for slug, board in BOARDS:
        by_pair[(slug, board.provider)].append(board_is_campus(board))
    result = campus_hint_pairs(BOARDS)
    for pair, verdicts in by_pair.items():
        assert (pair in result) == all(verdicts)


# ===========================================================================
# INVARIANT 4 — `bucket_from_contract` outranks the title rules exactly when
# the provider's filing names a campus programme, and its graduate/internship
# split is exhaustive over its own keyword vocabulary.
# ===========================================================================

_CAMPUS_CONTRACT_KEYS = (
    "internship", "trainee", "graduate", "apprentice", "vie", "stage",
    "alternance", "placement", "working student", "cooperative", "co-op",
)


@pytest.mark.parametrize("key", _CAMPUS_CONTRACT_KEYS)
def test_bucket_from_contract_recognises_every_campus_key(key):
    expected = ENTRY_LEVEL if "graduate" in key else INTERNSHIP
    assert bucket_from_contract(key) == expected
    # Case and surrounding text must not matter — this is a provider FILING,
    # not a title, so it is read as a whole classification, not parsed.
    assert bucket_from_contract(f"  {key.upper()} (with agreement)  ") == expected


@pytest.mark.parametrize("junk", ["", None, "full_time", "permanent", "contractor"])
def test_bucket_from_contract_is_silent_on_non_campus_filings(junk):
    assert bucket_from_contract(junk) == ""


# ===========================================================================
# INVARIANT 5 — `classify_role` only ever returns one of the four known
# buckets, for every title in a large deterministic grid built by splicing
# together fragments from every rule's own vocabulary (not just the words a
# human thought to combine by hand in `test_classify.py`'s CASES table).
# A stray rule that returns "" or a typo'd bucket string would otherwise
# surface only as a blank chip somewhere downstream.
# ===========================================================================

_TITLE_FRAGMENTS = (
    _SENIOR_WORDS + _ENTRY_WORDS + _INTERNSHIP_PHRASES + _INSIGHT_PHRASES
    + ["", "2027", "Analyst", "Associate", "Student", "Women in Banking",
       "Sophomore", "实习", "校招", "应届"]
)
_VALID_BUCKETS = {INSIGHT, INTERNSHIP, ENTRY_LEVEL, OTHER}


@pytest.mark.parametrize(
    "a,b,c",
    # A 3-word sample grid rather than the full O(n^3) product (that's
    # ~29^3 = ~24k cases, needlessly slow for what this checks) — every
    # fragment still appears in the FIRST position at least once, which is
    # what matters for a vocabulary-closure check.
    [(a, b, c) for a in _TITLE_FRAGMENTS for b in _TITLE_FRAGMENTS[:4]
     for c in _TITLE_FRAGMENTS[:3]],
)
def test_classify_role_never_returns_outside_the_bucket_vocabulary(a, b, c):
    title = f"{a} {b} {c}".strip()
    assert classify_role(title) in _VALID_BUCKETS
    assert classify_role(title, campus_hint=True) in _VALID_BUCKETS


# ===========================================================================
# INVARIANT 6 — "Class of YYYY" is an entry-level signal for exactly the
# plausible cohort window (2024-2035, the same window every other year
# extractor in this module honours) and for no year outside it.
# ===========================================================================

@pytest.mark.parametrize("year", range(2024, 2036))
def test_class_of_year_in_window_is_entry_level(year):
    assert classify_role(f"Class of {year} Investment Analyst") == ENTRY_LEVEL


@pytest.mark.parametrize("year", [1999, 2019, 2023, 2036, 2050])
def test_class_of_year_outside_window_is_not_promoted(year):
    # Outside the plausible window, "class of YYYY" is not a campus signal
    # by itself, and nothing else in this neutral title is either.
    assert classify_role(f"Class of {year} Investment Analyst") == OTHER


# ===========================================================================
# INVARIANT 7 — the three shapes the bucket rules were missing, each pinned
# by the live title that found it (`audit-opportunities.md §A1` and D11,
# re-measured against the board on 2026-09-02).
#
# These are EXAMPLE-BASED on purpose and sit in the stress file rather than
# in `test_classify.py`'s CASES table because each one comes with a matrix:
# the shape has to hold across every spelling the live boards use for it, and
# the negative case beside it is what stops the new rule swallowing something
# real. A single row in a CASES list would pin one spelling and leave the
# other eleven free to regress.
# ===========================================================================

# 7a. A summer internship with its intake year written between the two words.
# Eleven live Bank of America rows in both orders were filed `entry_level` —
# a full-time graduate hire — because `summer analyst` is adjacency-only and
# the campus-board fallback then matched the bare word "Analyst".
_YEAR_IN_THE_MIDDLE = [
    "Corporate Audit, Summer 2027 Analyst - Chester",
    "Global Risk, Summer 2027 Analyst - London",
    "Global Technology, Cyber Security, Summer 2027 Analyst - Chester",
    "Global Quantitative Data Analytics, Summer 2027 Analyst - London",
    "Global Operations Operations Analyst Summer 2027 - London",
    "Global Operations, Operations Analyst Summer 2027 - Bromley",
    "Global Technology Software Engineer Summer 2027 Analyst - Bromley",
    "Chief People Organization, Summer 2027 Analyst - Chester",
]


@pytest.mark.parametrize("title", _YEAR_IN_THE_MIDDLE)
def test_summer_year_analyst_is_an_internship(title):
    assert classify_role(title) == INTERNSHIP
    # And the campus-board hint cannot change it: the hint's whole job is to
    # break ties on NEUTRAL titles, and this one is no longer neutral.
    assert classify_role(title, campus_hint=True) == INTERNSHIP


@pytest.mark.parametrize("year", [2024, 2027, 2031, 2035])
def test_summer_year_analyst_holds_for_every_plausible_intake(year):
    assert classify_role(f"Summer {year} Analyst") == INTERNSHIP
    assert classify_role(f"Analyst Summer {year}") == INTERNSHIP


@pytest.mark.parametrize("title", [
    # A four-digit token that is not a plausible intake year must not be read
    # as one — desk and requisition numbers run to four digits all over these
    # boards.
    "Analyst Summer 4021",
    "Summer 1999 Analyst",
])
def test_a_four_digit_non_year_does_not_make_an_internship(title):
    assert classify_role(title) != INTERNSHIP


# 7b. Campus EVENTS. Three live Morgan Stanley rows sat in `other` because
# the event vocabulary had no word for them: the conjunction in "Meet and
# Greet" hides the object from `meet\s+(our|the)`, and "hackathon" was in no
# list at all.
_EVENT_TITLES = [
    "Meet and Greet our Traders: Structuring, Corporates and Exotics Trading",
    "Meet & Greet our Quants",
    "2026 Morgan Stanley Hackathon - UK",
    "2026 Morgan Stanley Hackathon - France",
    "EU Campus - Aarhus Hackathon IMCity",
    "Virtual Career Information Session - 2027 Corporate Summer Analyst",
    "Women Who Lead: IB Networking Event",
]


@pytest.mark.parametrize("title", _EVENT_TITLES)
def test_campus_events_are_insight_not_a_job(title):
    assert classify_role(title) == INSIGHT
    assert classify_role(title, campus_hint=True) == INSIGHT


# 7c. THE RULE ORDER ITSELF. `classify_role`'s step 1 (insight) runs before
# step 2 (internship), which is what makes an information session ABOUT a
# summer analyst programme the session rather than the internship. That
# ordering is load-bearing and the new event words inherit it, so it is
# pinned here directly rather than left implicit in the cases above.
#
# The other half of the contract is what keeps the ordering safe: a word only
# earns a place in `_INSIGHT` if it titles NO job on these boards. An
# internship whose title carries no event word is untouched by every event
# word in the vocabulary, which is what "an event pattern can never demote a
# real internship" means in practice — and is the second test below.
@pytest.mark.parametrize("event", [
    "Information Session", "Meet and Greet", "Hackathon", "Coffee Chat",
    "Networking Evening", "Insight Day",
])
def test_insight_runs_before_internship(event):
    assert classify_role(f"{event}: 2027 Summer Analyst Programme") == INSIGHT
    assert classify_role(f"2027 Summer Analyst Programme {event}") == INSIGHT


@pytest.mark.parametrize("title", [
    "2027 Summer Analyst - Investment Banking",
    "Summer 2027 Analyst - Global Markets",
    "Off-Cycle Internship, M&A",
    "Software Engineering Co-op",
])
def test_a_plain_internship_is_never_demoted_by_the_event_vocabulary(title):
    assert classify_role(title) == INTERNSHIP


# 7d. "Campus" alone is the firm's word for the CHANNEL, not for the thing on
# offer, so it only reaches `entry_level` beside a role or level word. Five
# live rows left the bucket when this landed; nineteen kept it.
@pytest.mark.parametrize("title", [
    "Campus Quantitative Researcher, PhD (Full-Time)",
    "Campus Full Time 2027 - Quantitative Trader - Amsterdam",
    "Campus AI Research Engineer (Full-Time)",
    "Research Associate (Campus)",
    "Investment Banking Analyst (Campus 2026)",
    "Campus Systems Engineer (Full-Time)",
])
def test_campus_with_a_role_word_is_still_an_entry_level_hire(title):
    assert classify_role(title) == ENTRY_LEVEL


@pytest.mark.parametrize("title,expected", [
    # An HR job on the campus desk is not a campus hire.
    ("Campus Sourcer", OTHER),
    # A talent pool is not a posting.
    ("2027 EU Campus Programme Talent Community", OTHER),
    # An event. "Showcase" is in no vocabulary, so `other` is the honest
    # answer here — better than `entry_level`, which claimed it was a job.
    ("Morgan Stanley Infrastructure: 2026 Fall Campus Showcase", OTHER),
    # A careers-advice session, likewise.
    ("From Campus to Career: Interview Tips that Work (Virtual Session)", OTHER),
    # The live IMC row carries a typo ("Hackahton") that no vocabulary can
    # catch. It must at least stop claiming to be a full-time hire.
    ("EU Campus - Aarhus Hackahton IMCity", OTHER),
])
def test_campus_without_a_role_word_is_not_an_entry_level_hire(title, expected):
    assert classify_role(title) == expected


# 7e. The provider's filing is coarser than its own titles. Crédit Agricole
# files a management-trainee programme under the single value
# "Internship/Trainee" because that value covers both kinds; the title says
# "Graduate Trainee ... 管培生" out loud and that answer wins.
def test_a_graduate_title_outranks_an_ambiguous_campus_filing():
    title = "Graduate Trainee in CIB Coverage 企业金融部管培生-北京"
    assert bucket_from_contract("Internship/Trainee", title) == ENTRY_LEVEL
    # …and the Chinese spelling alone is enough, as it is in `_ENTRY`.
    assert bucket_from_contract("Trainee", "管培生 - 北京") == ENTRY_LEVEL


def test_an_explicit_internship_title_outranks_the_graduate_word():
    # EY's live row uses "graduate" to describe the STUDENT, not the role.
    # The internship rule runs first in `classify_role` and must run first
    # here too, or every graduate-student internship becomes a full-time hire.
    title = "Intern - EY-Parthenon - M&A Lead Advisory (graduate)"
    assert bucket_from_contract("Internship/Trainee", title) == INTERNSHIP


def test_the_title_never_overrides_a_filing_that_already_decided():
    # A filing that says "graduate" outright decides on its own, and a filing
    # that names no campus programme still decides nothing whatever the
    # title says.
    assert bucket_from_contract("Graduate Programme", "Summer Analyst") == ENTRY_LEVEL
    assert bucket_from_contract("Permanent", "Graduate Trainee") == ""
    assert bucket_from_contract("", "Graduate Trainee") == ""


@pytest.mark.parametrize("key", _CAMPUS_CONTRACT_KEYS)
def test_a_silent_title_leaves_every_filing_exactly_as_it_was(key):
    # P3: the title check may only ever ADD an answer where the title speaks.
    # A title with no graduate word must produce the same bucket the filing
    # produced before this parameter existed.
    assert bucket_from_contract(key, "Financial Analyst, Paris") == bucket_from_contract(key)
