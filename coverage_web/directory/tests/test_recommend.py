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

import re

import pytest
from django.urls import reverse
from django.utils import timezone

from crm.models import UserFirm
from directory.classify import DERIVED_SUMMER
from directory.models import Firm, Opportunity
from directory.recommend import (
    MIN_SCORE, Candidate, Profile, parse_target_cycle, recommend,
    role_function, school_region, score_candidate,
)

from .test_tracking import _user

# Jimmy's real profile shape, which is what the feature was specified against.
JIMMY = Profile(
    class_year=2029,
    target_cycles=("SA 2028",),
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


def test_class_year_mismatch_chip_reads_as_a_reason_against_not_for():
    """A6: the mismatch chip used to read `f"Class of {stated}"`, byte-
    identical to the MATCH branch's chip text — and `Recommendation.why`
    joins on `.text` alone, so a reason AGAINST the role was typeset exactly
    like a reason FOR it (only the tooltip differed). The mismatch text must
    be its own, distinguishable string."""
    match_reasons = _reasons(JIMMY, class_year=str(JIMMY.class_year))
    mismatch_reasons = _reasons(JIMMY, class_year="2026")
    match_text = next(t for t in match_reasons if "Class of" in t)
    mismatch_text = next(t for t in mismatch_reasons if "Class" in t)
    assert mismatch_text != match_text
    assert mismatch_text == "Not Class of 2026"
    assert match_text == f"Class of {JIMMY.class_year}"


def test_a_programme_year_is_inference_and_is_labelled_as_such():
    """A 2028 summer internship implies 2029 graduates. That mapping is a
    convention, so the reason chip says "likely" and the tooltip admits the
    posting never stated it.

    REWRITTEN 2026-09-01, and the invariant is unchanged: the tooltip must
    say the posting did not state this and that Coverage inferred it. What
    changed is that the sentence is no longer composed here. `_class_fit`
    now takes both the derived year AND its justification from
    `classify.derive_class_year`, which is the product's one answer to
    "what class does this programme's shape imply" and the same sentence
    `views._eligibility` renders on its "Likely your year" chip. The old
    assertion pinned this module's private phrasing ("does not state a class
    year"), which is exactly the second wording the fix removed — so it is
    asserted through `DERIVED_SUMMER` rather than a literal, and a change to
    the canonical sentence now has to be made once."""
    points, reasons = score_candidate(JIMMY, _cand(1, cohort="2028"))
    class_reason = next(r for r in reasons if r.kind == "class" and "Class" in r.text)
    assert class_reason.text == "likely Class of 2029"
    assert class_reason.detail == DERIVED_SUMMER.format(cohort="2028", year=2029)
    # The two admissions the chip exists to make, asserted on the rendered
    # string and not on the template that produced it.
    assert "does not say this" in class_reason.detail
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


def test_year_first_cycle_shape_also_parses():
    """B1's fix: `accounts.forms.CYCLE_CHOICES` has ALWAYS produced this
    year-first shape ("2028 Summer Internship"), never the kind-first "SA
    2028" shape above — so every one of its choices used to parse to None
    and the 15-point cycle axis was dead for 100% of users. Both shapes
    must now parse."""
    assert parse_target_cycle("2028 Summer Internship") == ("internship", 2028)
    assert parse_target_cycle("2029 Full-Time / Graduate") == ("entry_level", 2029)
    assert parse_target_cycle("2027 Spring Week / Insight") == ("insight", 2027)


def test_every_cycle_choices_value_parses_to_something():
    """B1, exactly as specified: every value the settings dropdown can
    actually produce must parse to a real (bucket, year) — none of them may
    return None. `cycle_choices()` is the single source both
    `accounts.forms.CYCLE_CHOICES` and this parser draw from, so producer
    and consumer cannot drift the way "SA 2028" vs "2028 Summer Internship"
    once did."""
    from directory.recommend import cycle_choices

    for value, _label in cycle_choices():
        if value == "":
            continue  # the unselected placeholder — not a real cycle
        assert parse_target_cycle(value) is not None, value


def test_off_cycle_choice_parses_against_the_current_year():
    from directory.recommend import ENTRY_LEVEL, OFF_CYCLE_LABEL

    bucket, year = parse_target_cycle(OFF_CYCLE_LABEL)
    assert bucket == ENTRY_LEVEL
    assert year == date.today().year


def test_the_named_target_cycle_adds_on_top_of_the_class_fit():
    """"SA 2028" and "graduating 2029" are two different statements that happen
    to agree; both should count."""
    with_cycle = _score(JIMMY, cohort="2028", bucket="internship")
    without = _score(replace(JIMMY, target_cycles=()), cohort="2028", bucket="internship")
    assert with_cycle > without

    # And the cycle only fires on the right KIND of programme: a 2028
    # entry-level role is not the SA 2028 cycle.
    assert "SA 2028" not in _reasons(JIMMY, cohort="2028", bucket="entry_level")
    assert "SA 2028" in _reasons(JIMMY, cohort="2028", bucket="internship")


def test_a_second_named_cycle_scores_a_role_the_first_cycle_does_not_match():
    """A student recruiting for two programmes at once (regression for
    plural `target_cycles`): a 2027 Insight posting should get the same
    bonus a 2028 SA posting gets, from the SAME profile."""
    both = replace(JIMMY, target_cycles=("SA 2028", "2027 Spring Week / Insight"))
    assert _score(both, cohort="2028", bucket="internship") > _score(
        replace(JIMMY, target_cycles=()), cohort="2028", bucket="internship"
    )
    assert _score(both, cohort="2027", bucket="insight") > _score(
        replace(JIMMY, target_cycles=()), cohort="2027", bucket="insight"
    )
    assert "2027 Spring Week / Insight" in _reasons(both, cohort="2027", bucket="insight")


def test_matching_two_named_cycles_at_once_does_not_double_the_bonus():
    """Regression: the bonus must apply once per candidate, not once per
    selected cycle that happens to match — recruiting for both an Insight
    week and an SA cycle doesn't make one SA posting count twice."""
    single = replace(JIMMY, target_cycles=("SA 2028",))
    doubled = replace(JIMMY, target_cycles=("SA 2028", "SA 2028"))
    assert _score(single, cohort="2028", bucket="internship") == _score(
        doubled, cohort="2028", bucket="internship"
    )


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
    bare = Profile(class_year=2029, target_cycles=("SA 2028",), school="USC Marshall",
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
#
# The picks have moved twice: a horizontally scrolling rail, then a wrapping
# grid of per-firm blocks above the filter bar, and now the feed's pinned
# FIRST COLUMN — same shape as a firm column, accent-tinted, sitting in the
# same grid as the firms it recommends.
_HEADING = '<span class="firmcol-name" id="pickcol-h">Picked for you</span>'
_RAIL = '<article class="firmcol firmcol--picked'



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
    user.target_cycles = ["SA 2028"]
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
def test_the_scoring_does_not_respond_to_filters(client, live_board):
    """The RANKING is computed over the whole open campus set, so a filter can
    never promote a weaker pick or change the order.

    What the filter does reach is the DISPLAY — see the pinned-column tests
    below. Scoring and display were the same thing while the picks lived above
    the filter bar; they are separate now that the picks sit inside the
    filtered pile, and this is the half that must stay filter-blind."""
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


# ---------------------------------------------------------------------------
# The picks as the feed's pinned first column.
# ---------------------------------------------------------------------------
# They used to render as a band ABOVE the filter bar, which is what justified
# ignoring the filters entirely. Sitting inside the filtered pile inverts that
# reasoning: a column standing beside four filtered columns has to contain
# what it claims to contain. These pin the consequences.


_STYLE_RE = re.compile(r"<style>.*?</style>", re.S)


def _markup(resp) -> str:
    """The response with its `<style>` block removed.

    Position and count assertions over class names are meaningless against the
    raw response: the stylesheet names every selector it styles, in the
    `<head>`, ahead of all markup. `firmcol--picked` "appeared" before
    `class="firmcols"` for exactly that reason, and `rolecard-firm` counted
    three times on a page with one such card."""
    return _STYLE_RE.sub("", resp.content.decode())


def _picked_user(firm):
    user = _user()
    user.class_year = 2029
    user.regions = ["us"]
    user.tracks = ["ib"]
    user.save()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    return user


@pytest.mark.django_db
def test_the_picked_column_is_first_in_the_pile(client, live_board):
    """Pinned left. It is rendered ahead of the loop over `clusters`, not
    sorted into it, so no firm ordering can ever displace it."""
    client.force_login(_picked_user(live_board))
    body = _markup(client.get(reverse("opportunities")))

    grid = body.index('class="firmcols"')
    picked = body.index("firmcol--picked")
    first_firm = body.index('<article class="firmcol', picked + 1)
    assert grid < picked < first_firm


@pytest.mark.django_db
def test_the_picked_column_does_not_inflate_the_page_counts(client, live_board):
    """Every pick is ALSO listed under its own firm further along the row. If
    this column were folded into `clusters`, "N Open Roles" would count each
    pick twice and "N Firms" would gain a firm that does not exist."""
    client.force_login(_picked_user(live_board))
    resp = client.get(reverse("opportunities"))

    assert resp.context["pick_cluster"] is not None
    # The pinned column is deliberately not a member of `clusters`.
    assert all("firm_slug" in c for c in resp.context["clusters"])
    assert resp.context["total"] == sum(
        c["open_count"] for c in resp.context["clusters"]
    )
    assert len(resp.context["clusters"]) == 1


@pytest.mark.django_db
def test_the_picked_column_obeys_the_filters_and_says_what_it_hid(client, live_board):
    """A column showing internships while the page is filtered to Insight
    would be the only thing on screen lying about its own contents. It hides
    what the filter excludes — and never silently: the count it dropped is
    stated in the header."""
    client.force_login(_picked_user(live_board))

    shown = client.get(reverse("opportunities"))
    assert shown.context["pick_cluster"]["open_count"] == 1
    assert shown.context["pick_cluster"]["hidden_by_filter"] == 0

    hidden = client.get(reverse("opportunities"), {"q": "nothing matches this"})
    # Scoring is untouched — the pick is still ranked, just not displayed.
    assert len(hidden.context["picks"]) == 1
    # And the column STAYS, empty and saying so. A column that vanishes the
    # moment a filter is touched reads as breakage, not as an answer.
    col = hidden.context["pick_cluster"]
    assert col is not None and col["roles"] == []
    assert col["hidden_by_filter"] == 1
    assert "All 1 of your picks are hidden by your filters" in _markup(hidden)


@pytest.mark.django_db
def test_a_partly_filtered_column_states_the_number_it_dropped(client, live_board):
    """The honest middle case: some picks survive the filter, some don't."""
    Opportunity.objects.create(
        firm=live_board, url="https://x/insight-1", title="Spring Insight Week",
        bucket="insight", status="open", region="us", cohort="2028",
    )
    client.force_login(_picked_user(live_board))

    everything = client.get(reverse("opportunities"))
    n = everything.context["pick_cluster"]["open_count"]
    assert n >= 2, f"fixture should produce at least two picks, got {n}"

    narrowed = client.get(reverse("opportunities"), {"role": "insight"})
    col = narrowed.context["pick_cluster"]
    assert col["open_count"] < n
    assert col["hidden_by_filter"] == n - col["open_count"]
    assert f"{col['hidden_by_filter']} more hidden by your filters" in _markup(narrowed)


@pytest.mark.django_db
def test_only_the_picked_column_names_the_firm_on_its_cards(client, live_board):
    """The Picked column's cards come from several firms, so its header cannot
    name one — the cards do it instead. A firm's own column already said it,
    and repeating it per card would be the "first seen twice" bug again.

    The flag lives on a COPY of the card dict; if it were set on the shared
    item, the firm's own column would print the firm name too."""
    client.force_login(_picked_user(live_board))
    resp = client.get(reverse("opportunities"))

    assert all(r.get("show_firm") for r in resp.context["pick_cluster"]["roles"])
    for cluster in resp.context["clusters"]:
        assert not any(r.get("show_firm") for r in cluster["roles"])

    body = _markup(resp)
    assert body.count("rr-firm") == resp.context["pick_cluster"]["open_count"]


# ---------------------------------------------------------------------------
# Role-level track matching. Track used to be read purely off `Firm.tracks`,
# making it a property of the EMPLOYER: every opening at a bank covering IB
# scored as an IB match, so "2027 Internal Audit Analyst Program" and an M&A
# programme were indistinguishable and the audit role reached the live bar.
# ---------------------------------------------------------------------------

def _c(title, **kw):
    from directory.recommend import Candidate
    base = dict(id=1, firm_id=1, firm_name="Morgan Stanley", firm_slug="ms",
                title=title, url="https://ms.com/x", bucket="internship",
                region="us", firm_tracks=("ib", "st", "pe"))
    base.update(kw)
    return Candidate(**base)


def _p(**kw):
    from directory.recommend import Profile
    base = dict(class_year=2029, school="USC Marshall", regions=("hk", "us"),
                tracks=("ib", "st", "pe"), firm_tiers={1: 1})
    base.update(kw)
    return Profile(**base)


@pytest.mark.parametrize("title,expected", [
    ("Investment Banking, Classic — Summer Analyst", "ib"),
    ("Global Markets Sales & Trading Rotational Summer Analyst", "st"),
    ("Private Equity Summer Analyst", "pe"),
    # The function beats the division: this sits in the investment bank and
    # is a risk job, and reading the division first made it an IB match.
    ("2027 Commercial & Investment Bank Risk Management Summer Analyst", "none"),
    ("2027 Internal Audit Analyst Program", "none"),
    ("2027 Operations Summer Analyst Program (New York)", "none"),
    # The retail branch network. 229 open rows carried "branch" and not one
    # named a track, so each inherited its bank's firm-level coverage and
    # scored as an IB match — which is how a Jackson, Tennessee branch role
    # reached the top of a US/IB student's day-one brief.
    ("Branch Manager I - Woodstock Branch", "none"),
    ("Relationship Banker | Meadowbrook Branch", "none"),
    # ...but NOT by banning "retail", which is also an IB coverage group.
    ("Investment Banking - Consumer & Retail - Analyst", "ib"),
    # Internal technology and treasury departments. 55 live rows across
    # Morgan Stanley, Bank of America, UBS, Deutsche Bank, SocGen and
    # Goldman were silent on track and inherited their bank's ib/st coverage
    # by construction — Nomura's "2026 Insight Day: Corporate Infrastructure"
    # ("these are the teams that power and support our business every day")
    # was the founder's own #1 pick, ahead of every dated Morgan Stanley and
    # HSBC internship on the board.
    ("2027 Technology Summer Analyst Program (New York)", "none"),
    ("Global Technology Summer Analyst (Business Analyst) - 2027", "none"),
    ("2027 Graduate Talent Program - Group Technology Office - Singapore", "none"),
    ("Internship - Technology Process Analysis (f/m/x)", "none"),
    ("Global Technology Governance Intern", "none"),
    ("Global Technology & Engineering Analyst - Trainee", "none"),
    ("Corporate Treasury — Summer Analyst", "none"),
    ("Corporate Planning & Management — Summer Analyst", "none"),
    ("2026 Insight Day: Corporate Infrastructure", "none"),
    # ...but NOT by banning bare "technology" or "corporate": both are ALSO
    # coverage-group / division names a real IB posting states outright.
    ("Investment Banking Associate - Technology", "ib"),
    ("Investment Banking Financial Analyst | Boston Technology (Class of 2027)", "ib"),
    ("M&A intern - Large Cap Generalist / Technology team - Paris", "ib"),
    ("Investment Banking - Corporate Finance Associate", "ib"),
    ("Capital Markets, Corporate Banking Summer Analyst", "ib"),
    # Nothing stated: the firm's coverage is allowed to speak.
    ("Intern", ""),
    ("Summer Analyst Program", ""),
])
def test_role_function_reads_the_job_not_the_employer(title, expected):
    from directory.recommend import role_function
    assert role_function(title) == expected


@pytest.mark.parametrize("title,tracks,expected", [
    # Names a track they want: keep. This is the ONLY way in.
    ("2028 Investment Banking Summer Analyst", ("ib",), True),
    ("Investment Banking - Consumer & Retail - Analyst", ("ib",), True),
    # Names a track they are NOT recruiting for: drop.
    ("Sales & Trading Summer Analyst", ("ib",), False),
    # Names a function outside the vocabulary entirely: drop.
    ("Internal Audit Summer Analyst", ("ib",), False),
    ("Branch Manager I - Woodstock Branch", ("ib",), False),
    # NAMES NOTHING AT ALL: drop. The regression this rule exists for.
    # An earlier cut let a silent title through and leaned on the non-track
    # blocklist to catch the rest — and the Jackson, Tennessee branch
    # requisition came straight back to the top of a US/IB student's brief,
    # because its title is the single word "Intern" and there was never a
    # phrase for the blocklist to match. On the live board that rule left 33
    # roles of which 2 named investment banking; the other 31 were
    # Engineering, Risk, Controllers, Corporate Treasury, Human Capital
    # Management and Media Relations, all silent, all inheriting their bank's
    # firm-level coverage. These two surfaces claim "new AND relevant", so
    # the role has to say it.
    ("Intern", ("ib",), False),
    ("Summer Analyst Program", ("ib",), False),
    ("Engineering — Summer Analyst", ("ib",), False),
    ("Corporate Treasury — Summer Analyst", ("ib",), False),
    ("Human Capital Management — Summer Analyst", ("ib",), False),
    # A silent title is silent for everyone: a student recruiting BOTH of a
    # universal bank's two tracked lines still doesn't get its branch reqs,
    # which is what any "the firm is narrowly tracked" escape hatch would
    # have readmitted (Morgan Stanley carries exactly ["ib", "st"]).
    ("Intern", ("ib", "st"), False),
    # No stated tracks: nothing to be relevant TO, so filter nothing.
    ("Branch Manager I - Woodstock Branch", (), True),
    ("Internal Audit Summer Analyst", None, True),
])
def test_role_matches_tracks_filters_the_job_not_the_firm(title, tracks, expected):
    """The yes/no rule "new at your firms" surfaces filter on —
    `assistant.situation._new_role_events`, and until 2026-08-31 also the
    now-retired `crm.today._new_at_your_firms`. Both select purely on the
    FIRM, which is right for the firm axis and blind to the job: a student
    tiering
    a universal bank is tiering its investment bank, and the same firm also
    posts branch, audit and helpdesk reqs.

    An ALLOWLIST, deliberately unlike `_track_fit`'s three-case ranking rule:
    a feed row shows its score and its reasons and the student is browsing,
    while these two surfaces are the product saying "look at this now"."""
    from directory.recommend import role_matches_tracks
    assert role_matches_tracks(title, tracks) is expected


@pytest.mark.parametrize("region,regions,expected", [
    # Names a region they want: keep.
    ("hk", ("hk", "us"), True),
    # Names a region they don't: drop. Includes the two "stated but not one
    # of the six trackable markets" codes — a role that said WHERE it is,
    # just not somewhere the student targeted.
    ("other", ("hk", "us"), False),
    ("global", ("hk", "us"), False),
    ("sg", ("hk", "us"), False),
    # No region at all (the board couldn't place it): drop, once the
    # student has stated a preference — same as `_apply_region_filter` and
    # `_matching` already treat a blank region under a live filter.
    ("", ("hk", "us"), False),
    # No stated regions: nothing to filter to, so filter nothing — even a
    # role with a blank region passes, same "no stated X" posture as tracks.
    ("", (), True),
    ("other", (), True),
])
def test_role_matches_regions_filters_the_market_not_the_firm(region, regions, expected):
    """The Pune, India half of the customer walk: a role's own
    `Opportunity.region` compared against the student's stated regions,
    not the firm's `Firm.regions` list — a universal bank with an HK/US
    student's tier can still post an ops role in a market it does not
    track for that student."""
    from directory.recommend import role_matches_regions
    assert role_matches_regions(region, regions) is expected


@pytest.mark.parametrize(
    "bucket,class_year_derived,target_cycles,profile_class_year,expected",
    [
        # Bucket matches what the student is recruiting for: keep.
        ("internship", "", ("2028 Summer Internship",), 2028, True),
        # Bucket is a rung the student never picked a cycle for (a
        # full-time role shown to a student who has only ever picked
        # summer-internship cycles): drop. The Nashville Wealth Management
        # "New Associate (full-time)" half of the customer walk.
        ("entry_level", "", ("2028 Summer Internship",), 2028, False),
        # No target cycles stated: nothing to filter the bucket against.
        ("entry_level", "", (), 2028, True),
        ("entry_level", "", None, 2028, True),
        # A derived class year 2+ off the student's own: drop, even though
        # the bucket matches — the "MBA Summer Associate" half of the walk,
        # where the programme's own shape implies a class years away from
        # a sophomore's.
        ("internship", "2025", ("2028 Summer Internship",), 2028, False),
        # A derived class year exactly 1 off: `_class_fit` already calls
        # this "worth a look, not a fit" and scores it (not zero) — this
        # filter must not be stricter than the ranker it borrows the gap
        # from, so it still passes.
        ("internship", "2027", ("2028 Summer Internship",), 2028, True),
        # A derived class year that matches outright: keep.
        ("internship", "2028", ("2028 Summer Internship",), 2028, True),
        # No derived year at all (the common case: only 611 of ~21,700 rows
        # board-wide carry one) and no profile class year: nothing to
        # compare, so nothing is excluded on this axis.
        ("internship", "", (), None, True),
        ("internship", "2025", ("2028 Summer Internship",), None, True),
    ],
)
def test_role_matches_level_filters_the_rung_not_the_firm(
    bucket, class_year_derived, target_cycles, profile_class_year, expected,
):
    """The full-time-New-Associate and MBA-Summer-Associate halves of the
    customer walk: a role can name the student's track and sit in their
    region and still be the wrong rung of the ladder for them. Reuses the
    same gap `_class_fit` scores (0 match, 1 near, 2+ excluded) rather than
    inventing a second class-year rule, and the same `parse_target_cycle`
    the cycle bonus already uses for the bucket half."""
    from directory.recommend import role_matches_level
    assert role_matches_level(
        bucket, class_year_derived, target_cycles, profile_class_year,
    ) is expected


def test_a_stated_track_outranks_one_inferred_from_the_firm():
    """Evidence over inference. Without the gap, a generic "Intern" at a firm
    covering three of the student's tracks (18 + 3 + 3) outscored a posting
    that named their track outright — the scorer preferring what it guessed
    to what it was told."""
    from directory.recommend import score_candidate
    p = _p()
    named, _ = score_candidate(p, _c("Sales & Trading Summer Analyst"))
    generic, _ = score_candidate(p, _c("Intern"))
    assert named > generic, (named, generic)


def test_a_non_track_function_claims_no_track_match():
    """The card must not say "matches IB" about an audit job."""
    from directory.recommend import score_candidate
    score, reasons = score_candidate(_p(), _c("2027 Internal Audit Analyst Program"))
    assert not any(r.kind == "track" for r in reasons), [r.text for r in reasons]


def test_a_blocked_role_can_never_be_picked():
    """The feed's "Eligible only" filter hides roles whose own text excludes
    this student; the bar recommending what the filter hides would be the product
    contradicting itself. Hard exclusion, not a penalty — no amount of tier,
    track and region may outweigh the posting saying who it is not for."""
    from directory.recommend import recommend
    p = _p()
    strong = _c("Investment Banking, Classic — Summer Analyst", id=1)
    blocked = _c("Investment Banking, Classic — Summer Analyst", id=2, blocked=True)
    assert [r.candidate.id for r in recommend(p, [strong, blocked], max_per_firm=5)] == [1]
    assert recommend(p, [blocked]) == []


# ---------------------------------------------------------------------------
# The prose-stated graduation window (2026-08-09). The eligibility lens read
# `raw.facts.grad` and issued verdicts on it while ranking saw only the
# title-derived column — so SIG's Discovery Program, whose text states
# "graduate in the winter of 2028 or the spring of 2029" and which carried a
# real November deadline, scored 26 for the 2029 student it names and ranked
# below fifteen prior-cycle internships that merely failed to exclude him.
# ---------------------------------------------------------------------------


def test_a_body_stated_window_containing_the_student_scores_as_stated():
    hit = _score(JIMMY, bucket="insight", grad_years=("2028", "2029"))
    # The same posting with no window at all — everything else equal.
    silent = _score(JIMMY, bucket="insight")
    assert hit - silent == 30  # W_CLASS_STATED, the strongest signal
    texts = _reasons(JIMMY, bucket="insight", grad_years=("2028", "2029"))
    assert any("2028–2029" in t and "you" in t for t in texts)


def test_a_body_stated_window_excluding_the_student_is_a_veto():
    out = _score(JIMMY, bucket="internship", cohort="2027",
                 grad_years=("2027", "2028"))
    assert out < 0  # W_CLASS_STATED_MISMATCH, and nothing else may argue


def test_the_title_column_still_outranks_nothing_and_ties_the_window():
    """A single stated year and a window containing the student are the same
    kind of evidence and score identically."""
    via_column = _score(JIMMY, class_year="2029")
    via_window = _score(JIMMY, grad_years=("2029",))
    assert via_column == via_window


def test_the_stated_window_beats_a_prior_cycle_near_miss():
    """The audit's exact failure: a 2027 summer internship (derived-near, one
    year off) must not outrank an insight programme whose own text names the
    student's class."""
    near = _cand(1, bucket="internship", cohort="2027")
    stated = _cand(2, firm_id=98, bucket="insight", grad_years=("2028", "2029"))
    ranked = recommend(JIMMY, [near, stated], min_score=0)
    assert ranked[0].candidate.id == 2


def test_from_opportunity_reads_the_grad_fact(db):
    from directory.models import Firm, Opportunity
    f = Firm.objects.create(slug="sig", name="SIG")
    o = Opportunity.objects.create(
        firm=f, url="https://x/d", title="Discovery Program", bucket="insight",
        status="open",
        raw={"facts": {"grad": {"value": "2028–2029",
                                "years": ["2028", "2029"],
                                "phrase": "planning to graduate in the winter "
                                          "of 2028 or the spring of 2029"}}})
    c = Candidate.from_opportunity(o)
    assert c.grad_years == ("2028", "2029")


# ---------------------------------------------------------------------------
# The network axis (2026-08-09). 129 contacts, 23 chatted, 2 advocates — and
# ranking ignored every one of them, on the product whose stated moat is the
# networking CRM. A warm relationship changes what a listing is worth.
# ---------------------------------------------------------------------------


def test_a_warm_contact_at_the_firm_moves_the_ranking():
    warm_profile = Profile(
        class_year=2029, school="USC Marshall", tracks=("ib",),
        warm_firms={99: "warm"},
    )
    cold = Profile(class_year=2029, school="USC Marshall", tracks=("ib",))
    assert _score(warm_profile) - _score(cold) == 14  # W_NETWORK_WARM
    texts = _reasons(warm_profile)
    assert "You know someone here" in texts


def test_a_reply_counts_for_less_than_a_conversation():
    replied = Profile(class_year=2029, warm_firms={99: "replied"})
    warm = Profile(class_year=2029, warm_firms={99: "warm"})
    assert _score(warm) > _score(replied) > _score(Profile(class_year=2029))


def test_warmth_never_outruns_the_class_axis():
    """Who a programme is FOR still beats who you know there: a warm firm's
    wrong-class posting must rank below a cold firm's right-class one."""
    wrong_class = _cand(1, class_year="2028")             # stated, not Jimmy's
    right_class = _cand(2, firm_id=98, class_year="2029")
    prof = Profile(class_year=2029, warm_firms={99: "warm"})
    ranked = recommend(prof, [wrong_class, right_class], min_score=-100)
    assert ranked[0].candidate.id == 2


def test_stacked_signals_outrun_the_class_penalty_but_not_the_class_veto():
    """The arithmetic this test was written to pin is still true and still
    worth pinning: `W_CLASS_STATED_MISMATCH` (-25) is a SUBTRACTION, and it is
    not big enough to stop a wrong-class role clearing MIN_SCORE once tier,
    track, region and warmth stack on top of it. The weight's own comment
    claims it is "large negative so it cannot be outrun by a tier-1 firm the
    student happens to like"; the number says otherwise, and that gap is what
    the first assertion below records.

    WHAT CHANGED, AND WHAT THE OLD ASSERTION PROTECTED. This test used to end
    `assert [r.candidate.id for r in ranked] == [1]` — i.e. `recommend()`
    RETURNS the wrong-class role, and only `Candidate.blocked` stops it. That
    was a true description of the code and a deliberate one: it existed so
    nobody would mistake the -25 for the hard-exclusion guarantee and rip out
    the `blocked` plumbing in `directory.views`, which is what actually shipped
    the fix for the production bug (`_eligibility` never reading
    `Opportunity.class_year`).

    It is no longer true, because `recommend()` now vetoes a STATED class
    mismatch itself (`stated_class_mismatch`) rather than trusting every caller
    to have filtered first — a guarantee this module makes in prose about
    itself should not live only in its callers. The old assertion's real job
    survives below, made stronger rather than weaker: `blocked` is still
    separately exercised, and still on a role the class veto does NOT cover, so
    the plumbing it protected cannot be deleted on the grounds that the veto
    has made it redundant. It has not — `blocked` also carries the VISA
    verdict, which nothing in this module can see."""
    prof = replace(JIMMY, warm_firms={1: "warm"})  # firm_id=1 is Jimmy's tier 1
    wrong_class = _cand(
        1, firm_id=1, class_year="2026",  # stated, not Jimmy's 2029
        title="Investment Banking Summer Analyst", region="us",
    )
    # The subtraction alone does not sink it — the arithmetic is unchanged.
    assert score_candidate(prof, wrong_class)[0] >= MIN_SCORE

    # ...and `recommend()` refuses it anyway, on the posting's own words, with
    # the caller having flagged nothing (blocked=False, the default).
    assert recommend(prof, [wrong_class]) == []

    # `blocked` is NOT made redundant by that veto. A role whose text says
    # nothing about a class year but which refuses this student's visa carries
    # no stated mismatch for `recommend` to see, and is excluded only because
    # the caller said so.
    visa_wall = _cand(2, firm_id=1, title="Investment Banking Summer Analyst",
                      region="us")
    assert score_candidate(prof, visa_wall)[0] >= MIN_SCORE
    assert recommend(prof, [replace(visa_wall, blocked=True)]) == []


@pytest.mark.django_db
def test_a_title_stated_class_mismatch_is_blocked_end_to_end():
    """The real gap: `directory.views._eligibility` is the ONLY source of
    `Candidate.blocked` (see crm/digest.py and directory/views.py's own
    `track_eligible`/Picked-for-you callers), and until now it read the
    body-derived `facts.grad` window but never `Opportunity.class_year` — the
    rarer, MORE authoritative TITLE statement ("Class of 2027") that
    `recommend._class_fit` already treats as authoritative over the body
    text. A title-only mismatch therefore never produced a blocking verdict,
    so `_class_fit`'s -25 alone had to hold the line — and per the test
    above, stacked against a tier-1 target firm, a stated track match and a
    warm contact, it doesn't."""
    from directory.models import Firm, Opportunity
    from directory.views import _eligibility

    f = Firm.objects.create(slug="ms-eligtest", name="Morgan Stanley Test")
    o = Opportunity.objects.create(
        firm=f, url="https://x/eligtest",
        title="Investment Banking Summer Analyst (Class of 2026)",
        bucket="internship", status="open", region="us", class_year="2026",
    )
    verdict = _eligibility(o, {"class_year": 2029, "work_auth": {}})
    assert verdict is not None and verdict["blocking"] is True, verdict

    c = Candidate.from_opportunity(o, blocked=verdict["blocking"])
    prof = replace(JIMMY, warm_firms={f.id: "warm"}, firm_tiers={f.id: 1})
    assert recommend(prof, [c]) == []


def test_a_network_only_profile_is_not_empty():
    """A student who has filled in nothing but has live relationships still
    gets picks — the relationships ARE personalisation signal."""
    assert not Profile(warm_firms={7: "warm"}).is_empty


# ---------------------------------------------------------------------------
# Expired roles (2026-08-10). A listing may honestly stay on the board past
# its date — the firm still lists it, and Coverage does not close what the
# source has not. A PICK is different: it is the product pointing at a role
# and saying "do this one", and there is nothing to do about a closed
# application. Two HSBC roles reached ranks 3 and 4 this way.
# ---------------------------------------------------------------------------

from datetime import date as _date, timedelta as _td

_TODAY = _date(2026, 8, 10)


def test_a_passed_deadline_is_never_a_pick():
    live = _cand(1, deadline=_TODAY + _td(days=30))
    dead = _cand(2, firm_id=98, deadline=_TODAY - _td(days=2))
    ranked = recommend(JIMMY, [live, dead], min_score=0, today=_TODAY)
    assert [r.candidate.id for r in ranked] == [1]


def test_todays_deadline_still_counts_as_open():
    """Closing TODAY is the most urgent thing on the board, not the least."""
    ranked = recommend(JIMMY, [_cand(1, deadline=_TODAY)], min_score=0,
                       today=_TODAY)
    assert len(ranked) == 1


def test_an_expired_role_sorts_last_among_equal_scores():
    """Defence in depth for any caller that scores without the exclusion:
    `d or date.max` alone is ascending by date, so an expired role — holding
    the earliest date of all — sorted FIRST at equal score."""
    from directory.recommend import Recommendation, _sort_key
    dead = Recommendation(_cand(1, deadline=_TODAY - _td(days=2)), 50, ())
    soon = Recommendation(_cand(2, deadline=_TODAY + _td(days=3)), 50, ())
    rolling = Recommendation(_cand(3, deadline=None), 50, ())
    order = sorted([dead, soon, rolling], key=lambda r: _sort_key(r, _TODAY))
    assert [r.candidate.id for r in order] == [2, 3, 1]


# ---------------------------------------------------------------------------
# The audit of 2026-09-01. Four defects, each measured against the founder's
# live board (2,662 open campus rows, 26,163 rows board-wide) before it was
# called a defect. Every test below pins the RULE, never the arithmetic that
# happens to satisfy it today.
# ---------------------------------------------------------------------------

def test_the_derived_class_year_is_classifys_and_there_is_only_one_of_them():
    """`_class_fit` used to keep its own `_GRAD_WINDOW` table — bucket to
    offset window, internship +1, entry_level +0, insight +2..+3 — while
    `classify.derive_class_year` reads the bucket AND the title and refuses
    outright for every shape whose convention has more than one answer.

    Measured on the live open campus set: 737 of 2,662 rows (28%) were rows
    the scorer derived a graduation year for and `derive_class_year` refuses
    to. Each rendered "likely Class of 20XX" as a labelled inference the
    product had already measured as unsupportable. On the 642 rows where both
    derived, they agreed on every one — the disagreement was never the
    arithmetic, only whether there is an answer at all.

    So: whatever `derive_class_year` refuses, this axis must score zero on and
    make no claim about."""
    from directory.classify import derive_class_year

    refused = [
        # Off-cycle: a 3-6 month placement taken on a gap year, a placement
        # year, or after graduating. "2027 - Investment Banking Off Cycle
        # Internship – Paris (July start)", live.
        dict(bucket="internship", title="Off-Cycle Internship", cohort="2027"),
        # An internship naming no season. "ONE TD Intern / Co-Op (Winter
        # 2027)", live.
        dict(bucket="internship", title="Winter Intern / Co-Op", cohort="2027"),
        # The whole insight bucket: a first-year in year N graduates N+2 or
        # N+3 depending on the degree. This is the shape the old table was
        # most confident about (+2..+3, so it matched FOUR class years) and
        # the one classify.py refuses most explicitly.
        dict(bucket="insight", title="Spring Week", cohort="2027"),
        dict(bucket="insight", title="Discovery Programme", cohort="2027"),
        # Nobody graduates out of a talent community.
        dict(bucket="entry_level", title="Talent Community", cohort="2028"),
    ]
    for kw in refused:
        assert derive_class_year(kw["bucket"], kw["title"], kw["cohort"]) == ("", "")
        for year in (2026, 2027, 2028, 2029, 2030, 2031):
            who = replace(JIMMY, class_year=year, target_cycles=())
            reasons = [r for r in score_candidate(who, _cand(1, **kw))[1]
                       if r.kind == "class"]
            assert reasons == [], (kw, year, reasons)


def test_the_derived_chip_quotes_classifys_own_sentence():
    """The justification is not re-composed here. `views._eligibility`'s
    "Likely your year" chip and this rail's "likely Class of" chip are the
    same inference and now read the same sentence, so a student who meets the
    role on two surfaces is given one explanation of it rather than two that
    have to be kept in step by hand."""
    from directory.classify import DERIVED_GRAD

    _, reasons = score_candidate(
        replace(JIMMY, target_cycles=()),
        _cand(1, bucket="entry_level", title="Graduate Analyst Programme",
              cohort="2029"),
    )
    detail = next(r.detail for r in reasons if r.kind == "class")
    assert detail == DERIVED_GRAD.format(cohort="2029")


def test_an_adjacent_derived_year_still_says_it_is_not_a_fit():
    """The near-miss chip is a reason to look, explicitly not a reason to
    apply, and it still has to admit the year was inferred."""
    _, reasons = score_candidate(
        replace(JIMMY, target_cycles=()), _cand(1, cohort="2027"))
    r = next(r for r in reasons if r.kind == "class")
    assert r.text == "2027 intake"
    assert "inferred" in r.detail
    assert "not a fit" in r.detail


@pytest.mark.parametrize("school", [
    # Canada, keyed under "us" until this audit. "columbia" matched on a word
    # boundary inside "British Columbia"; "ivey" was simply filed under the
    # wrong country (Western University, London, Ontario).
    "University of British Columbia",
    "Ivey Business School",
    # An untracked market that `normalize_region` can place, which used to
    # come back as the literal code "other" — a BUCKET holding Toronto,
    # Sydney, Mumbai and Dubai at once, not a market.
    "University of Toronto",
    "University of Melbourne",
    "Indian Institute of Technology Bombay",
    # The placeless tier. A university is not "Remote".
    "Remote University",
])
def test_school_region_answers_only_with_a_tracked_market(school):
    """`_region_fit` compares the student's home code against the ROLE's code
    for equality and renders "{market} — the market your university sits in"
    as a fact. "other" on both sides makes that sentence a false claim about
    two different countries, and it collects the highest region weight on the
    board (20). 713 of 2,662 live open campus rows carry region="other"."""
    from directory.classify import TRACKED_REGIONS
    got = school_region(school)
    assert got == "" or got in TRACKED_REGIONS
    assert got not in ("other", "global")


def test_an_untracked_home_market_scores_zero_rather_than_matching_other():
    """The end-to-end shape of the bug: a Toronto student and a Dubai role,
    both region="other", scored W_REGION_SCHOOL and were told Dubai is the
    market their university sits in."""
    toronto = Profile(school="University of Toronto", tracks=("ib",))
    dubai = _cand(1, region="other", location="Dubai, United Arab Emirates")
    points, reasons = score_candidate(toronto, dubai)
    assert [r for r in reasons if r.kind == "region"] == []
    assert points == 0


def test_columbia_still_resolves_in_the_spellings_that_are_unambiguous():
    """Dropping the bare token must not cost the school it was there for.
    Same trade the table already makes for Oxford and Cambridge."""
    assert school_region("Columbia University") == "us"
    assert school_region("Columbia Business School") == "us"


@pytest.mark.parametrize("title,expected", [
    # The two live rows that ranked #2 and #3 on the founder's own rail,
    # chipped "matches IB + S&T" off Nomura's firm-level coverage because
    # nothing in either title was a phrase any blocklist matched.
    ("2027 - Risk - Industrial Placement - London", "none"),
    ("2027 - Information Technology - Industrial Placement - London", "none"),
    # The same division under the names the rest of the board uses for it.
    ("Controllers — Summer Analyst", "none"),
    ("2027 Corporate Risk Summer Internship (Workout) - Early Careers", "none"),
    ("Risk Assurance Information Technology Trainee", "none"),
    ("Enterprise Risk Summer Analyst", "none"),
    ("Liquidity Risk Intern", "none"),
    ("Risk — Summer Analyst", "none"),
    ("Internship – Risk", "none"),
    # ...and the front-office roles the narrow patterns must not touch. Bare
    # `\brisk\b` stays out of the blocklist because it is also a consulting
    # practice: 5 open campus rows state a real track alongside the word.
    ("Financial Services Risk Consulting Intern", "consulting"),
    ("Quant & Analytics Intern - Financial Services Risk Consulting", "consulting"),
    # "Capital Markets" is an IB phrase and must survive a title that merely
    # contains the letters of a risk word elsewhere.
    ("2027 Global Capital Markets Summer Analyst", "ib"),
    # The existing carve-outs, re-asserted so a new pattern cannot quietly
    # take them: retail as an IB coverage group, technology as an IB sector.
    ("Investment Banking - Consumer & Retail Summer Analyst", "ib"),
    ("Investment Banking Associate - Technology", "ib"),
])
def test_the_blocklist_additions_read_the_department_not_the_desk(title, expected):
    """2026-09-01 census. Each phrase added was checked against all 26,163
    rows on the board: "information technology" 26 rows / 1 co-stating a
    track, "controllers" 24 / 1, the delimited bare "Risk" segment 14 / 1,
    and "corporate risk" / "enterprise risk" / "risk assurance" /
    "liquidity risk" 0 collisions between them. Each of the three collisions
    is an experienced or support-function row that correctly degrades to
    "none" — the same "co-occurring non-track word, decline rather than
    guess" call this module already makes for "Trading Operations Analyst"."""
    assert role_function(title) == expected


def test_a_risk_or_it_req_never_inherits_its_banks_track_coverage():
    """The end-to-end claim, which is the one that reached the founder's
    screen: a universal bank's `Firm.tracks` must not put "matches IB + S&T"
    on the card of its risk and IT requisitions."""
    for title in ("2027 - Risk - Industrial Placement - London",
                  "2027 - Information Technology - Industrial Placement - London"):
        points, reasons = score_candidate(
            JIMMY, _cand(1, title=title, firm_tracks=("ib", "st")))
        assert [r for r in reasons if r.kind == "track"] == [], title


@pytest.mark.parametrize("track,expected", [
    ("ib", "an IB role"), ("st", "an S&T role"), ("am", "an AM role"),
    ("pe", "a PE role"), ("consulting", "a Consulting role"),
    ("corp-strat", "a Corp Strat role"),
])
def test_the_stated_track_tooltip_reads_as_english(track, expected):
    """It read "The posting itself is a IB role" on every live IB pick. The
    article follows the initial SOUND of the label, not its initial letter:
    "an IB" and "an S&T" and "an AM", but "a PE"."""
    title = {"ib": "Investment Banking Summer Analyst",
             "st": "Sales & Trading Summer Analyst",
             "am": "Asset Management Summer Analyst",
             "pe": "Private Equity Summer Analyst",
             "consulting": "Consulting Summer Analyst",
             "corp-strat": "Corporate Strategy Summer Analyst"}[track]
    who = replace(JIMMY, tracks=(track,), target_cycles=())
    _, reasons = score_candidate(who, _cand(1, title=title))
    detail = next(r.detail for r in reasons if r.kind == "track")
    assert detail == f"The posting itself is {expected}, which you're recruiting for."


def test_min_score_admits_statements_alone_and_inferences_only_in_pairs():
    """The bar's own comment used to claim "a track match alone (18) ... is
    not a recommendation, while a tier-1 target firm (26), or any two inputs
    together, is." Both halves were wrong: a title that STATES the student's
    track scores 26 and clears alone, and the two weakest inputs together sum
    to 10. This pins the corrected rule so the comment and the arithmetic
    cannot drift apart again."""
    from directory.recommend import (
        TIER_POINTS, W_CLASS_DERIVED_NEAR, W_CLASS_STATED, W_TARGET_UNTIERED,
        W_TRACK_STATED,
    )
    # Statements clear the bar alone.
    for alone in (W_CLASS_STATED, TIER_POINTS[1], W_TRACK_STATED):
        assert alone >= MIN_SCORE
    # The weakest pair of inferences does not.
    assert W_CLASS_DERIVED_NEAR + W_TARGET_UNTIERED < MIN_SCORE
