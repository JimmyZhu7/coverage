"""The recommendation scorer (directory/recommend.py) and the bar it feeds.

Most of this file needs no database and no request: the scorer is a pure
function over two dataclasses, which is the point of it living in its own
module. The last section exercises the view, because "what does a signed-out
visitor see" is a question about the page, not the arithmetic.

Two properties matter more than any individual number here:

  * determinism — same inputs, same order, every run; and
  * separability — each of the four inputs must be able to move the ranking on
    its own. A scorer where only one axis ever decides anything is a scorer
    with three decorative parameters, and the "why" chips on the cards would be
    quietly lying about what did the work.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from crm.models import UserFirm
from directory.models import Firm, Opportunity
from directory.recommend import (
    MIN_SCORE, Candidate, Profile, parse_target_cycle, recommend,
    school_region, score_candidate,
)

from .test_tracking import _user

# Jimmy's real profile shape, which is what the feature was specified against.
JIMMY = Profile(
    class_year=2029,
    target_cycle="SA 2028",
    school="USC Marshall",
    regions=("us", "hk"),
    tracks=("ib", "st", "pe"),
    firm_tiers={1: 1, 2: 3, 3: None},
)


def _cand(cid, **kw):
    base = dict(
        id=cid, firm_id=99, firm_name=f"Firm {cid}", firm_slug=f"firm{cid}",
        title="Summer Analyst", url=f"https://x/{cid}", bucket="internship",
    )
    base.update(kw)
    return Candidate(**base)


def _score(profile, **kw):
    return score_candidate(profile, _cand(1, **kw))[0]


def _reasons(profile, **kw):
    return [r.text for r in score_candidate(profile, _cand(1, **kw))[1]]


# ---------------------------------------------------------------------------
# School -> region. The one place a name has to become a place.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("school,expected", [
    ("USC Marshall", "us"),               # abbreviation table
    ("University of Hong Kong", "hk"),    # falls through to the location parser
    ("HKUST", "hk"),
    ("London School of Economics", "eu"),
    ("National University of Singapore", "sg"),
    ("", ""),
])
def test_school_region_is_deterministic_and_documented(school, expected):
    assert school_region(school) == expected
    assert school_region(school) == school_region(school)


def test_an_unknown_school_declines_to_answer():
    """The table's job is to add signal where it has some, never to have an
    opinion about every string. An unknown school scores zero on this axis
    rather than being guessed into a market."""
    assert school_region("Institute of Somewhere") == ""
    assert _score(Profile(school="Institute of Somewhere"), region="us") == 0


def test_ambiguous_abbreviations_are_left_out():
    """"SMU" is both Singapore Management University and Southern Methodist.
    It is deliberately absent from the table, and the location parser correctly
    refuses to guess."""
    assert school_region("SMU") == ""


# ---------------------------------------------------------------------------
# Class / cycle fit — where the cohort-vs-class-year distinction lives.
# ---------------------------------------------------------------------------

def test_a_stated_class_year_beats_everything_derived():
    """Regression: an earlier cut returned as soon as it saw a stated class
    year, which meant a stated match (30) lost to a derived match that also
    picked up the target-cycle bonus (18+15=33). A posting that says your class
    out loud must never rank below one that merely implies it."""
    stated = _score(JIMMY, class_year="2029", cohort="2028")
    derived = _score(JIMMY, cohort="2028")
    assert stated > derived

    # ...and the stated match does not ALSO collect a derived one on top.
    assert "likely Class of 2029" not in _reasons(JIMMY, class_year="2029", cohort="2028")


def test_a_stated_class_year_for_another_class_is_a_penalty():
    """The firm said out loud who this is for. Nothing inferred gets to argue
    with that, and a tier-1 firm must not be able to outrun it."""
    assert _score(JIMMY, class_year="2026") < 0
    on_target_firm = _score(JIMMY, class_year="2026", firm_id=1)   # tier 1
    assert on_target_firm < _score(JIMMY, firm_id=1)


def test_a_programme_year_is_inference_and_is_labelled_as_such():
    """A 2028 summer internship implies 2029 graduates. That mapping is a
    convention, so the reason chip says "likely" and the tooltip admits the
    posting never stated it."""
    points, reasons = score_candidate(JIMMY, _cand(1, cohort="2028"))
    class_reason = next(r for r in reasons if r.kind == "class" and "Class" in r.text)
    assert class_reason.text == "likely Class of 2029"
    assert "does not state a class year" in class_reason.detail
    assert "inferred" in class_reason.detail
    assert points > 0


def test_the_grad_window_differs_by_bucket():
    """An entry-level programme in year N is joined by N's graduates; a summer
    internship in year N is done by N+1's. Same cohort string, different
    implication — conflating them would mislabel the whole feed."""
    assert _score(JIMMY, cohort="2029", bucket="entry_level") > 0
    assert _score(JIMMY, cohort="2029", bucket="internship") < _score(
        JIMMY, cohort="2028", bucket="internship"
    )


def test_an_adjacent_year_scores_but_only_just():
    exact = _score(JIMMY, cohort="2028")
    near = _score(JIMMY, cohort="2027")
    far = _score(JIMMY, cohort="2024")
    assert exact > near > far == 0


@pytest.mark.parametrize("raw,expected", [
    ("SA 2028", ("internship", 2028)),
    ("sa 2028", ("internship", 2028)),
    ("FT 2029", ("entry_level", 2029)),
    ("Spring Week 2027", ("insight", 2027)),
    ("", None),
    ("someday", None),
    ("XX 2028", None),
])
def test_target_cycle_parsing(raw, expected):
    assert parse_target_cycle(raw) == expected


def test_the_named_target_cycle_adds_on_top_of_the_class_fit():
    """"SA 2028" and "graduating 2029" are two different statements that happen
    to agree; both should count."""
    with_cycle = _score(JIMMY, cohort="2028", bucket="internship")
    without = _score(replace(JIMMY, target_cycle=""), cohort="2028", bucket="internship")
    assert with_cycle > without

    # And the cycle only fires on the right KIND of programme: a 2028
    # entry-level role is not the SA 2028 cycle.
    assert "SA 2028" not in _reasons(JIMMY, cohort="2028", bucket="entry_level")
    assert "SA 2028" in _reasons(JIMMY, cohort="2028", bucket="internship")


# ---------------------------------------------------------------------------
# Each of the four inputs, moving the ranking on its own.
# ---------------------------------------------------------------------------

def _order(profile, candidates):
    """Pure ranking order: threshold and the per-firm cap turned off, so these
    tests see the sort and nothing else."""
    return [
        r.candidate.id
        for r in recommend(profile, candidates, min_score=0, max_per_firm=99)
    ]


def test_firm_tier_moves_the_ranking():
    """Tier 1 outranks tier 3, all else equal."""
    tier1 = _cand(1, firm_id=1, firm_name="A Firm")
    tier3 = _cand(2, firm_id=2, firm_name="A Firm")
    assert _order(JIMMY, [tier3, tier1]) == [1, 2]
    assert score_candidate(JIMMY, tier1)[0] > score_candidate(JIMMY, tier3)[0]


def test_industry_preference_moves_the_ranking():
    matches = _cand(1, firm_tracks=("ib",))
    doesnt = _cand(2, firm_tracks=("consulting",))
    assert _order(JIMMY, [doesnt, matches]) == [1, 2]
    # More overlap beats less, but with diminishing returns.
    two = score_candidate(JIMMY, _cand(3, firm_tracks=("ib", "st")))[0]
    one = score_candidate(JIMMY, matches)[0]
    assert two > one


def test_university_location_moves_the_ranking():
    """USC is in the US, so a US role outranks an otherwise identical HK one
    even though HK is also on the profile's region list."""
    us = _cand(1, region="us")
    hk = _cand(2, region="hk")
    eu = _cand(3, region="eu")
    assert _order(JIMMY, [eu, hk, us]) == [1, 2, 3]


def test_region_tooltips_read_as_english():
    """Regression: the detail string used to be built with `.capitalize()`
    over an already-capitalised name, which rendered "The united states"."""
    detail = next(
        r.detail for r in score_candidate(JIMMY, _cand(1, region="us"))[1]
        if r.kind == "region"
    )
    assert detail.startswith("United States — the market your university (USC Marshall)")
    assert "united states" not in detail


def test_class_fit_moves_the_ranking():
    fits = _cand(1, cohort="2028")
    doesnt = _cand(2, cohort="2024")
    assert _order(JIMMY, [doesnt, fits]) == [1, 2]


def test_every_axis_is_independent():
    """Each axis on its own produces a non-zero score with no help from the
    others — nothing here is decorative."""
    bare = Profile(class_year=2029, target_cycle="SA 2028", school="USC Marshall",
                   regions=("us",), tracks=("ib",), firm_tiers={1: 1})
    assert _score(bare, firm_id=1) > 0                     # tier
    assert _score(bare, firm_tracks=("ib",)) > 0           # track
    assert _score(bare, region="us") > 0                   # region
    assert _score(bare, cohort="2028") > 0                 # class


# ---------------------------------------------------------------------------
# Determinism and the threshold.
# ---------------------------------------------------------------------------

def test_repeated_runs_are_identical():
    cands = [_cand(i, firm_id=(i % 3) + 1, firm_tracks=("ib",), region="us")
             for i in range(1, 12)]
    first = [(r.candidate.id, r.score, r.why) for r in recommend(JIMMY, cands)]
    for _ in range(5):
        assert [(r.candidate.id, r.score, r.why) for r in recommend(JIMMY, cands)] == first


def test_input_order_cannot_change_output_order():
    """Ties are broken on deadline, then firm name, then id — never on the
    order the queryset happened to hand rows over."""
    cands = [_cand(i, firm_tracks=("ib",), region="us", firm_name="Same Firm")
             for i in range(1, 8)]
    assert _order(JIMMY, cands) == _order(JIMMY, list(reversed(cands)))


def test_a_dated_role_outranks_a_rolling_one_at_the_same_score():
    soon = _cand(1, firm_tracks=("ib",), region="us", deadline=date(2030, 1, 1))
    rolling = _cand(2, firm_tracks=("ib",), region="us", deadline=None)
    assert _order(JIMMY, [rolling, soon]) == [1, 2]


def test_one_weak_signal_is_not_a_recommendation():
    """A region match alone, or a track match alone, does not clear the bar —
    otherwise the bar would just be "roles in your city"."""
    assert score_candidate(JIMMY, _cand(1, region="us"))[0] < MIN_SCORE
    assert score_candidate(JIMMY, _cand(1, firm_tracks=("ib",)))[0] < MIN_SCORE
    assert recommend(JIMMY, [_cand(1, region="us")]) == []
    # Two together do.
    assert recommend(JIMMY, [_cand(1, region="us", firm_tracks=("ib",))])


def test_nothing_is_padded_when_nothing_qualifies():
    """The honest empty state. A bar that always has six cards in it means
    nothing on the days it genuinely has six."""
    junk = [_cand(i, region="eu", firm_tracks=("consulting",)) for i in range(1, 30)]
    assert recommend(JIMMY, junk) == []


def test_one_firm_cannot_own_the_whole_bar():
    """Caught in the browser: every axis except class fit is a property of the
    FIRM, so one tier-1 firm matching the student's tracks and region scores
    identically across all its openings — the first live render was six Bank of
    America roles. Correct ranking, useless shortlist."""
    hoggy = [_cand(i, firm_id=1, firm_name="Big Bank", firm_tracks=("ib",), region="us")
             for i in range(1, 9)]
    others = [_cand(20 + i, firm_id=20 + i, firm_name=f"Other {i}",
                    firm_tracks=("ib",), region="us") for i in range(1, 5)]
    picks = recommend(JIMMY, hoggy + others)
    from collections import Counter
    counts = Counter(p.candidate.firm_id for p in picks)
    assert max(counts.values()) <= 2
    assert len(counts) >= 3

    # The cap changes WHICH roles show, never the ordering rule: the survivors
    # from a capped firm are still its top-ranked ones.
    kept = [p.candidate.id for p in picks if p.candidate.firm_id == 1]
    assert kept == sorted(kept)[:len(kept)]


def test_an_empty_profile_gets_no_recommendations():
    """Nothing to tailor on -> nothing claimed to be tailored."""
    blank = Profile()
    assert blank.is_empty
    assert recommend(blank, [_cand(1, region="us", firm_tracks=("ib",))]) == []


def test_every_recommendation_carries_its_reasons():
    picks = recommend(JIMMY, [_cand(1, firm_id=1, firm_tracks=("ib",), region="hk")])
    assert picks and picks[0].reasons
    assert picks[0].why == "Tier 1 · matches IB · HK"
    for r in picks[0].reasons:
        assert r.detail and r.detail != r.text


# ---------------------------------------------------------------------------
# The bar on the page.
# ---------------------------------------------------------------------------

@pytest.fixture
def live_board(db):
    firm = Firm.objects.create(name="Evercore", slug="evercore", tracks=["ib"])
    Opportunity.objects.create(
        firm=firm, url="https://x/1", title="2028 Summer Analyst",
        bucket="internship", status="open", region="us", cohort="2028",
        deadline=timezone.localdate() + timedelta(days=5),
    )
    return firm


# The page's CSS block mentions "Picked for you" in a comment, so the presence
# of the heading is asserted on its markup, not on the bare phrase.
_HEADING = "<h2 class=\"recbar-title\" id=\"recbar-h\">Picked for you</h2>"
# The picks used to render as a horizontally scrolling rail. They are now a
# wrapping grid of per-firm blocks — see _group_picks in directory/views.py.
_RAIL = "<div class=\"recbar-grid\">"


@pytest.mark.django_db
def test_signed_out_sees_no_tailored_bar(client, live_board):
    resp = client.get(reverse("opportunities"))
    body = resp.content.decode()
    assert resp.context["picks"] == []
    assert resp.context["has_profile"] is False
    assert _HEADING not in body and _RAIL not in body
    assert "you&#x27;re signed out" in body or "you're signed out" in body


@pytest.mark.django_db
def test_a_signed_in_user_with_no_survey_gets_the_same_honest_state(client, live_board):
    client.force_login(_user())
    resp = client.get(reverse("opportunities"))
    body = resp.content.decode()
    assert resp.context["has_profile"] is False
    assert _HEADING not in body and _RAIL not in body
    assert "nothing to" in body


@pytest.mark.django_db
def test_a_profiled_user_gets_scored_cards_with_visible_reasons(client, live_board):
    user = _user()
    user.class_year = 2029
    user.target_cycle = "SA 2028"
    user.school = "USC Marshall"
    user.regions = ["us", "hk"]
    user.tracks = ["ib"]
    user.save()
    UserFirm.all_objects.create(user=user, firm=live_board, tier=1)

    client.force_login(user)
    resp = client.get(reverse("opportunities"))
    body = resp.content.decode()
    assert resp.context["has_profile"] is True
    assert len(resp.context["picks"]) == 1
    assert _HEADING in body and _RAIL in body
    assert "Tier 1" in body and "matches IB" in body and "likely Class of 2029" in body


@pytest.mark.django_db
def test_a_profiled_user_with_no_matches_is_told_so(client):
    """Profile present, board irrelevant to it — say so rather than pad."""
    firm = Firm.objects.create(name="Bain & Co", slug="bain", tracks=["consulting"])
    Opportunity.objects.create(
        firm=firm, url="https://x/9", title="Summer Associate", bucket="internship",
        status="open", region="eu", cohort="2024",
    )
    user = _user()
    user.class_year = 2029
    user.regions = ["us"]
    user.tracks = ["ib"]
    user.save()
    client.force_login(user)

    resp = client.get(reverse("opportunities"))
    assert resp.context["has_profile"] is True
    assert resp.context["picks"] == []
    assert "scores high enough" in resp.content.decode()


@pytest.mark.django_db
def test_the_bar_does_not_respond_to_filters(client, live_board):
    """It sits above the filter bar and is scored over the whole open campus
    set, so a filter that empties the list below must not empty it."""
    user = _user()
    user.class_year = 2029
    user.regions = ["us"]
    user.tracks = ["ib"]
    user.save()
    UserFirm.all_objects.create(user=user, firm=live_board, tier=1)
    client.force_login(user)

    unfiltered = client.get(reverse("opportunities"))
    filtered = client.get(reverse("opportunities"), {"q": "nothing matches this"})
    assert filtered.context["total"] == 0
    assert [p["id"] for p in filtered.context["picks"]] == [
        p["id"] for p in unfiltered.context["picks"]
    ]
