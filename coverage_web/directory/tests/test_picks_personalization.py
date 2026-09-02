"""The eight Picked-for-you defects measured on the founder's live board on
2026-09-01, each pinned to the behaviour that replaces it.

The founder's profile (class of 2029, target "2028 Summer Internship", HK/US,
IB/S&T, 54 tiered firms, USC) got six picks that day: four Hong Kong 2027
summer internships each carrying a chip that read "2027 intake" as if it were
a reason FOR the role, one Barclays full-time "Associate Graduate Program"
whose intake implies 2027 graduates, and a Nomura insight programme with no
region at all ranked #1 above every Hong Kong role on the board. The header
shared no reason across the six, so nothing rendered on any card. J.P.
Morgan's "Commercial & Investment Bank" division prefix had 20 of its 21
campus rows reading as IB, twelve of them Markets programmes. Sixty
Associate/MBA/PhD internships were unblocked for a sophomore, 45 cleared the
score bar. And four irrelevant board rows — two Evercore info sessions with
no region, two Canadian CPA co-ops — silenced the one header sentence that
would have said his own cycle had not opened yet.

Pure-function tests come first (no database); the view tests at the bottom
pin what the page renders.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from crm.models import UserFirm
from directory import recommend as R
from directory.models import Firm, Opportunity
from directory.recommend import (
    MIN_SCORE, W_CLASS_DERIVED_NEAR, W_REGION_UNKNOWN, W_TRACK_STATED,
    Candidate, Profile, level_mismatch, recommend, role_function, role_level,
    role_matches_level, score_candidate, student_level,
)

from .test_tracking import _user

# A 2029 undergraduate recruiting for the 2028 summer — the founder's shape.
SOPHOMORE = Profile(
    class_year=2029,
    target_cycles=("2028 Summer Internship",),
    school="University of Southern California",
    regions=("hk", "us"),
    tracks=("ib", "st"),
    firm_tiers={1: 1},
)


def _cand(cid=1, **kw):
    base = dict(
        id=cid, firm_id=1, firm_name="Acme", firm_slug="acme",
        title="Investment Banking Summer Analyst", url=f"https://x/{cid}",
        bucket="internship", region="hk", firm_tracks=("ib", "st"),
    )
    base.update(kw)
    return Candidate(**base)


def _ids(recs):
    return [r.candidate.id for r in recs]


def _chip(profile, kind, **kw):
    return next(
        (r for r in score_candidate(profile, _cand(**kw))[1] if r.kind == kind),
        None,
    )


# ===========================================================================
# P2 — year-off roles. The adjacent intake is a caveat, not a reason, and it
# scores nothing for a student who named the other year of the same cycle.
# ===========================================================================

def test_the_near_miss_chip_reads_as_a_caveat_in_its_own_text():
    """`Recommendation.why` joins chip TEXTS, and `crm.digest` prints that
    string in an email where there is no tooltip to hover. "2027 intake"
    alone read as a reason for the role; the caveat has to be in the words
    that print."""
    early = _chip(SOPHOMORE, "class", cohort="2027")
    assert early.text == "2027 intake, a year early for you"
    assert "not a fit" in early.detail
    # ...and the other direction says so too: a 2029 summer intake implies
    # 2030 graduates, a year after this student.
    late = _chip(SOPHOMORE, "class", cohort="2029")
    assert late.text == "2029 intake, a year late for you"


def test_why_carries_the_caveat_verbatim_for_the_digest():
    recs = recommend(SOPHOMORE, [_cand(cohort="2027")])
    assert len(recs) == 1
    assert "2027 intake, a year early for you" in recs[0].why
    # Never the bare positive-sounding form.
    assert "· 2027 intake ·" not in f"· {recs[0].why} ·"


def test_the_adjacent_intake_scores_nothing_when_the_student_named_the_other_year():
    """The founder typed "2028 Summer Internship" into Settings. A 2027
    summer internship is, by his own words, the intake he is not in — so
    it earns no class points at all, though it still shows and still says
    why."""
    named = score_candidate(SOPHOMORE, _cand(cohort="2027"))[0]
    unnamed = score_candidate(replace(SOPHOMORE, target_cycles=()),
                              _cand(cohort="2027"))[0]
    assert unnamed - named == W_CLASS_DERIVED_NEAR
    # A cycle naming a DIFFERENT bucket says nothing about this one: the
    # adjacent-year points survive.
    other_bucket = score_candidate(
        replace(SOPHOMORE, target_cycles=("2028 Spring Week / Insight",)),
        _cand(cohort="2027"))[0]
    assert other_bucket == unnamed


def test_the_wrong_rung_is_not_a_pick():
    """`role_matches_level` — the yes/no the advisor's snapshot and the
    digest already apply — now runs inside `recommend()`. The founder's #5
    pick was a full-time 2027 "Associate Graduate Program": wrong bucket
    for a student who named only internships, two years off, MBA rung."""
    full_time = _cand(1, bucket="entry_level", cohort="2027",
                      title="Electronic Trading Associate Graduate Program 2027")
    two_off = _cand(2, cohort="2025")
    adjacent = _cand(3, cohort="2027")
    exact = _cand(4, cohort="2028")
    assert _ids(recommend(SOPHOMORE, [full_time, two_off, adjacent, exact],
                          max_per_firm=9)) == [4, 3]


def test_a_stated_class_outranks_the_rung_filters_inference():
    """Evidence beats inference, the module's own rule - and for INSIGHT
    programmes the inference never refuses in the first place. Rewritten
    the day it landed: the original pinned that a silent insight programme
    is refused for a student who named only Summer Internship cycles. That
    refusal was the news-strip bucket rule applied to picks, and it hid every
    early-ID programme from nearly every student (Nomura Discover, the
    founder's #1 pick). An insight programme is the on-ramp to the internship
    the student declared, so both reach her; the one that states her class
    still ranks first because it earns the stated-class points."""
    stated = _cand(1, bucket="insight", cohort="2027", class_year="2029",
                   title="2027 Discover Programme - Insight")
    silent = _cand(2, bucket="insight", cohort="2027",
                   title="2027 Discover Programme - Insight")
    assert _ids(recommend(SOPHOMORE, [stated, silent], max_per_firm=9)) == [1, 2]
    # ...but a stated class does not override the posting's OWN rung word.
    mba = _cand(3, bucket="internship", cohort="2028", class_year="2029",
                title="Investment Banking Summer Associate")
    assert recommend(SOPHOMORE, [mba]) == []


def test_a_thin_profile_degrades_to_todays_behaviour():
    """No class year, no cycle, no regions: nothing to compare, nothing
    filtered, nothing penalised."""
    thin = Profile(tracks=("ib",), firm_tiers={1: 1})
    full_time = _cand(1, bucket="entry_level", cohort="2027", region="",
                      title="Electronic Trading Associate Graduate Program 2027")
    assert _ids(recommend(thin, [full_time])) == [1]
    assert score_candidate(thin, _cand(region=""))[0] == \
        score_candidate(thin, _cand(region="other"))[0]


# ===========================================================================
# P3 — level. The rung a title names, the rung a student is on, and the
# only default that turns a blank into an answer.
# ===========================================================================

@pytest.mark.parametrize("title,expected", [
    # The six on the founder's live top 30.
    ("2027 Capital Markets, Global Investment Banking Summer Associate", "mba"),
    ("2027 Guggenheim Securities Investment Banking Summer Associate – New York", "mba"),
    ("2027 PhD Summer Intern – Portfolio Management, Quantitative Research Analyst", "phd"),
    ("Banking Associate Summer Internship Program 2027 New York", "mba"),
    ("Quantitative Finance Associate Summer Internship Program 2027 New York", "mba"),
    ("Electronic Trading Associate Graduate Program 2027 New York", "mba"),
    # Degree words, in the spellings the board uses.
    ("2027 MBA Summer Intern – Account Manager, US", "mba"),
    ("Quantitative Researcher Intern, PhD or Postdoc", "phd"),
    ("Postgraduate Internship Program (Master's & MBA)", "mba"),
    ("Undergraduate Summer Internship", "undergrad"),
    # Rung words.
    ("Global Markets Quantitative Strategies Data Group 2027 Off-cycle Associate - Paris", "mba"),
    ("Sales & Trading Summer Analyst", "undergrad"),
    ("Experienced Associate - Deals", "experienced"),
    ("Vice President, Global Markets", "experienced"),
    # Two levels named: decline rather than pick one.
    ("2026 BSc/MSc/PhD Quantitative Research/Strat Internship", ""),
    # Bare "Associate" is not a level — undergraduate entry at PwC, MBA at
    # McKinsey, an undergraduate internship at Bridgewater.
    ("Consulting - Associate - Strategy & Operations", ""),
    ("2027 Investment Associate Intern", ""),
    ("Visiting Associate, Internship, Sweden", ""),
    ("Summer Senior Research Associate (Campus)", ""),
    ("Intern", ""),
    ("", ""),
])
def test_role_level_reads_the_rung_the_title_names(title, expected):
    assert role_level(title) == expected


def test_a_degree_word_wins_over_a_rung_word():
    """"PhD Summer Intern – ... Research Analyst" is a PhD internship; the
    job-title word "Analyst" is one step removed from who it is for."""
    assert role_level("2027 PhD Summer Intern – Quantitative Research Analyst") == "phd"
    # Bare "Associate" beside "Analyst" is one rung word, not two (see the
    # block comment in recommend.py); two real rung words decline.
    assert role_level("Summer Analyst / Associate Program") == "undergrad"
    assert role_level("2027 Summer Analyst / Summer Associate Program") == ""


@pytest.mark.parametrize("study_level,cycles,expected", [
    # Stated wins, in any spelling this can read.
    ("undergrad", ("2028 Full-Time / Graduate",), "undergrad"),
    ("Undergraduate", (), "undergrad"),
    ("MBA", ("2027 Summer Internship",), "mba"),
    ("PhD", (), "phd"),
    # A stated level this vocabulary cannot read is a statement, not a
    # blank: no default is applied and nothing is filtered.
    ("masters", ("2027 Summer Internship",), ""),
    # Blank, and every parseable cycle is an internship or insight week:
    # an undergraduate's plan.
    ("", ("2028 Summer Internship",), "undergrad"),
    ("", ("2027 Spring Week / Insight", "2028 Summer Internship"), "undergrad"),
    # Blank with a full-time cycle, or no parseable cycle at all: unknown.
    ("", ("2028 Full-Time / Graduate",), ""),
    ("", ("2028 Summer Internship", "2029 Full-Time / Graduate"), ""),
    ("", ("sa2028_ib",), ""),
    ("", (), ""),
    (None, None, ""),
])
def test_student_level_defaults_to_undergrad_only_on_an_undergraduates_plan(
    study_level, cycles, expected,
):
    assert student_level(study_level, cycles) == expected


def test_level_mismatch_needs_both_sides_and_admits_phds_to_the_associate_rung():
    assert level_mismatch("undergrad", "mba") is True
    assert level_mismatch("undergrad", "phd") is True
    assert level_mismatch("mba", "undergrad") is True
    assert level_mismatch("mba", "phd") is True
    assert level_mismatch("undergrad", "experienced") is True
    assert level_mismatch("phd", "mba") is False   # Summer Associate is the advanced-degree rung
    assert level_mismatch("undergrad", "undergrad") is False
    for unknown in ("", None):
        assert level_mismatch(unknown, "mba") is False
        assert level_mismatch("undergrad", unknown) is False


def test_role_matches_level_consults_the_rung_only_when_given_a_title():
    """The two callers that predate the title check (`assistant.situation`,
    `crm.relevance`) pass no title and keep their behaviour exactly."""
    args = ("internship", "2029", ("2028 Summer Internship",), 2029)
    assert role_matches_level(*args) is True
    assert role_matches_level(*args, title="Investment Banking Summer Associate") is False
    assert role_matches_level(*args, title="Investment Banking Summer Analyst") is True
    # A stated MBA student is the other way round.
    assert role_matches_level(*args, title="Investment Banking Summer Associate",
                              study_level="mba") is True
    assert role_matches_level(*args, title="Investment Banking Summer Analyst",
                              study_level="mba") is False


def test_recommend_skips_the_associate_rung_for_an_undergraduate():
    associate = _cand(1, cohort="2028", title="Investment Banking Summer Associate")
    phd = _cand(2, cohort="2028", title="2028 PhD Summer Intern – Quantitative Research")
    analyst = _cand(3, cohort="2028", title="Investment Banking Summer Analyst")
    assert _ids(recommend(SOPHOMORE, [associate, phd, analyst], max_per_firm=9)) == [3]
    # A stated MBA student gets the associate rung and not the analyst one.
    mba = replace(SOPHOMORE, study_level="mba", class_year=2028)
    assert _ids(recommend(mba, [associate, phd, analyst], max_per_firm=9)) == [1]
    # No level known either way (a cycle this cannot parse): today's
    # behaviour, every rung shows.
    unknown = replace(SOPHOMORE, target_cycles=("sa2028_ib",))
    assert set(_ids(recommend(unknown, [associate, phd, analyst], max_per_firm=9))) == {1, 2, 3}


def test_profile_from_user_reads_study_level_through_getattr():
    """The column is being added concurrently; a User without it must read
    as "not stated", never raise."""
    class _NoColumn:
        class_year = 2029
        target_cycles = ["2028 Summer Internship"]
        school = ""
        regions = []
        tracks = ["ib"]

    class _WithColumn(_NoColumn):
        study_level = "MBA"

    assert Profile.from_user(_NoColumn()).study_level == ""
    assert Profile.from_user(_NoColumn()).level == "undergrad"
    assert Profile.from_user(_WithColumn()).level == "mba"


# ===========================================================================
# P4 — "Commercial & Investment Bank" is where the job sits, not what it is.
# ===========================================================================

@pytest.mark.parametrize("title,expected", [
    # J.P. Morgan's live titles, 2026-09-01. Twelve Markets programmes read
    # as IB off the division prefix.
    ("2027 Commercial & Investment Bank - Markets Summer Analyst Program - Hong Kong", "st"),
    ("2027 Commercial & Investment Bank - Markets Program - Trading - Summer Analyst - Tokyo", "st"),
    ("2027 Commercial & Investment Bank - Markets Program - Research - Summer Analyst - Tokyo", "st"),
    # Custody and payments are plumbing, not a track.
    ("2027 Commercial & Investment Bank Securities Services Leadership Program - Summer Analyst Program", "none"),
    ("2027 Commercial & Investment Bank Global Payments Summer Analyst Program", "none"),
    ("Global Payments Solutions Summer Analyst - 2027 - Singapore", "none"),
    # The IB programmes under the same prefix still read as IB.
    ("2027 Commercial & Investment Bank - Global Investment Banking Program - Summer Analyst - Tokyo", "ib"),
    ("2027 Commercial & Investment Bank - Global Investment Banking Working Student Program - Off-Cycle Internship – Madrid", "ib"),
    # REWRITTEN 2026-09-01 (S4) from "": Global Corporate Banking is not
    # silent any more. `\bcorporate bank(ing|er)?\b` names a function outside
    # the six tracks, and until the `cb` track clears its supply gate (18
    # rows across 7 firms in the founder's markets, measured 2026-09-01, two
    # short) the honest answer for a corporate banking programme is "not one
    # of your tracks" rather than the investment bank's coverage inherited by
    # silence. 64 of the 66 open corporate-banking rows were silent this way,
    # and Barclays' fourteen GTB rows all read "matches IB".
    ("2027 Commercial & Investment Bank - Global Corporate Banking Program - Summer Analyst - Tokyo", "none"),
    # The documented behaviour that must not regress.
    ("2027 Commercial & Investment Bank Risk Management Summer Analyst Program", "none"),
    # Not a prefix: "Investment Bank" elsewhere in a title is still the job.
    ("Summer Analyst - Investment Bank", "ib"),
    ("Commercial & Investment Bank - Sales & Trading - Summer Analyst", "st"),
    # The S&T division under its own name, and "Markets" attached to a
    # programme word — but never the phrases that belong to other tracks.
    ("2027 Global Markets Summer Internship Program - Hong Kong", "st"),
    ("2027 APAC Markets Summer Analyst - Hong Kong", "st"),
    ("Capital Markets Summer Analyst", "ib"),
    ("Private Markets Summer Analyst", "pe"),
    # REWRITTEN 2026-09-01 (S4/D5) from "am". `\bwealth management\b` sat in
    # the am pattern, so 72 open campus rows of retail wealth advisory
    # answered "Asset Management" for every student who picked AM — the exact
    # conflation the corporate-banking/wealth research warns about. It now
    # answers "none"; the GS/JPM division name "Asset & Wealth Management"
    # keeps answering "am" and is tested in test_recommend.py.
    ("2027 Summer Intern - Global Wealth Management, Growth Markets Analyst, US", "none"),
    ("Global Markets Operations Summer Analyst", "none"),
])
def test_the_division_prefix_is_stripped_before_the_job_is_read(title, expected):
    assert role_function(title) == expected


def test_a_markets_programme_under_the_prefix_scores_as_st_not_ib():
    ib_only = replace(SOPHOMORE, tracks=("ib",))
    markets = _cand(title="2027 Commercial & Investment Bank - Markets Summer Analyst Program - Hong Kong")
    assert _chip(ib_only, "track", title=markets.title) is None
    chip = _chip(SOPHOMORE, "track", title=markets.title)
    assert chip.text == "S&T role"
    assert score_candidate(SOPHOMORE, markets)[0] >= W_TRACK_STATED


# ===========================================================================
# P5 — a blank region is charged to the product, not to the role.
# ===========================================================================

def test_a_blank_region_costs_a_profiled_student_and_says_why():
    points, reasons = R._region_fit(SOPHOMORE, _cand(region=""))
    assert points == W_REGION_UNKNOWN < 0
    (chip,) = reasons
    assert chip.kind == "region"
    assert chip.text == "Location not read"
    assert "could not tell" in chip.detail


def test_a_blank_region_costs_nothing_when_no_regions_were_named():
    assert R._region_fit(replace(SOPHOMORE, regions=()), _cand(region="")) == (0, [])


def test_a_blank_region_can_no_longer_tie_a_located_twin():
    """The founder's #1: an unlocated Nomura programme level with the Hong
    Kong roles beside it on every other axis.

    REWRITTEN 2026-09-01 (S1). The old order was located, elsewhere, blank —
    Japan ahead of the unread location, because a stated market outside the
    student's cost nothing at all while the unread one cost 8. That ordering
    said the product would rather recommend a role it knows is in the wrong
    place than one it could not place, which is backwards. The ladder now
    runs located > unread > elsewhere, and the two negatives are ordered the
    way the evidence is: our ignorance is cheaper than the posting's own
    statement that the job is somewhere the student did not name."""
    located = _cand(1, region="hk")
    blank = _cand(2, region="")
    elsewhere = _cand(3, region="jp")
    ranked = recommend(SOPHOMORE, [blank, located, elsewhere], max_per_firm=9)
    assert _ids(ranked) == [1, 2, 3]
    assert (score_candidate(SOPHOMORE, located)[0]
            > score_candidate(SOPHOMORE, blank)[0]
            > score_candidate(SOPHOMORE, elsewhere)[0])


def test_the_penalty_cannot_hide_a_role_two_statements_vouch_for():
    """Tier 1 (26) + a stated track (26) - 8 still clears the bar by a wide
    margin: "we could not place it" stops a role winning, never showing."""
    p = replace(SOPHOMORE, class_year=None, target_cycles=(), school="")
    assert score_candidate(p, _cand(region=""))[0] >= MIN_SCORE + 10


# ===========================================================================
# P6 — the cycle-not-open note listens to the board THIS student could be
# looking at.
# ===========================================================================

class _P:
    def __init__(self, *cycles, regions=()):
        self.target_cycles = list(cycles)
        self.regions = list(regions)


@pytest.mark.django_db
def test_irrelevant_rows_no_longer_silence_the_note():
    """The four live rows that did: two Evercore info sessions with no region,
    two Canadian CPA co-ops."""
    from directory.views import _cycle_not_open_note

    f = Firm.objects.create(slug="evercore", name="Evercore")
    Opportunity.objects.create(firm=f, url="https://x/1", status="open",
                               bucket="internship", cohort="2028", region="",
                               title="2028 Summer Analyst Intro to Evercore - UVA")
    Opportunity.objects.create(firm=f, url="https://x/2", status="open",
                               bucket="internship", cohort="2028", region="other",
                               title="January 2028 - Assurance CPA - Co-op - 4 months - Edmonton")
    # In region, but names a function outside the track vocabulary.
    Opportunity.objects.create(firm=f, url="https://x/3", status="open",
                               bucket="internship", cohort="2028", region="hk",
                               title="January 2028 - Tax CPA - Co-op - Hong Kong")
    qs = Opportunity.objects.filter(status="open")
    note = _cycle_not_open_note(_P("2028 Summer Internship", regions=("hk", "us")), qs)
    assert "2028 Summer Internship" in note
    assert "haven't opened in your regions yet" in note
    assert "—" not in note

    # A student who named no regions is asked the board-wide question, as
    # before — the blank-region info session counts for them.
    assert _cycle_not_open_note(_P("2028 Summer Internship"), qs) == ""

    # One in-region row whose title is merely SILENT on track is the cycle,
    # open: the note goes.
    Opportunity.objects.create(firm=f, url="https://x/4", status="open",
                               bucket="internship", cohort="2028", region="hk",
                               title="2028 Summer Analyst")
    assert _cycle_not_open_note(_P("2028 Summer Internship", regions=("hk", "us")), qs) == ""


# ===========================================================================
# P1, P7, P8 — what the page renders.
# ===========================================================================

def _student(**fields):
    user = _user()
    user.class_year = 2029
    user.target_cycles = ["2028 Summer Internship"]
    user.regions = ["hk"]
    user.tracks = ["ib"]
    for k, v in fields.items():
        setattr(user, k, v)
    user.save()
    return user


def _firm(slug, name, tracks, regions=()):
    return Firm.objects.create(slug=slug, name=name, tracks=tracks,
                               regions=list(regions))


def _opp(firm, n, title, **kw):
    base = dict(firm=firm, url=f"https://x/{firm.slug}/{n}", title=title,
                bucket="internship", status="open", region="hk", cohort="2027")
    base.update(kw)
    return Opportunity.objects.create(**base)


@pytest.mark.django_db
def test_each_pick_prints_its_own_reasons_on_its_card(client):
    """P1. Two tier-1 firms share no tier sentence, so "Tier 1" cannot rise
    to the header; before this it rendered nowhere. Now the header holds
    what every pick shares and each card holds the rest — with the
    year-off chip printed as the caveat it is."""
    alpha = _firm("alpha", "Alpha", ["ib"])
    beta = _firm("beta", "Beta", ["ib"])
    a = _opp(alpha, 1, "2027 Investment Banking Summer Analyst")
    b = _opp(beta, 2, "2027 Investment Banking Summer Analyst")
    user = _student()
    UserFirm.all_objects.create(user=user, firm=alpha, tier=1)
    UserFirm.all_objects.create(user=user, firm=beta, tier=2)
    client.force_login(user)

    resp = client.get(reverse("opportunities"))
    col = resp.context["pick_cluster"]
    assert {r["id"] for r in col["roles"]} == {a.id, b.id}
    shared = [r.text for r in col["reasons"]]
    assert "IB role" in shared and "HK" in shared
    assert "2027 intake, a year early for you" in shared
    by_id = {r["id"]: r for r in col["roles"]}
    assert by_id[a.id]["pick_why"] == "Tier 1"
    assert by_id[b.id]["pick_why"] == "Tier 2"
    assert "on your target list" in by_id[a.id]["pick_why_title"]

    body = resp.content.decode()
    assert body.count('class="rr-why"') == 2
    assert 'class="rr-why" title="Alpha is a Tier 1 firm on your target list."' in body
    # The firm columns' own copies of the same rows carry no why line.
    assert not any("pick_why" in role for cl in resp.context["clusters"]
                   for role in cl["roles"])
    # And nowhere on the page does the near miss print as a bare positive.
    assert "2027 intake<" not in body and "2027 intake ·" not in body


@pytest.mark.django_db
def test_the_column_order_match_reads_the_rows_not_the_firm(client):
    """P7. A firm whose record says "hk" and "ib" but whose only open row is
    an audit programme somewhere else does not float above a firm with an
    actual Hong Kong role."""
    hollow = _firm("hollow", "Hollow Bank", ["ib"], regions=["hk"])
    _opp(hollow, 1, "2027 Internal Audit Summer Analyst", region="other")
    real = _firm("real", "Real Partners", ["ib"])
    _opp(real, 2, "2027 Summer Analyst")          # silent title, HK: inherits ib
    client.force_login(_student())

    resp = client.get(reverse("opportunities"))
    match = {cl["firm_slug"]: cl["match"] for cl in resp.context["clusters"]}
    assert match == {"hollow": False, "real": True}
    assert [cl["firm_slug"] for cl in resp.context["clusters"]] == ["real", "hollow"]


@pytest.mark.django_db
def test_a_student_who_stated_nothing_matches_everything_which_sorts_as_nothing(client):
    hollow = _firm("hollow", "Hollow Bank", ["ib"], regions=["hk"])
    _opp(hollow, 1, "2027 Internal Audit Summer Analyst", region="other")
    real = _firm("real", "Real Partners", ["ib"])
    _opp(real, 2, "2027 Summer Analyst")
    client.force_login(_student(regions=[], tracks=[]))

    resp = client.get(reverse("opportunities"))
    assert {cl["match"] for cl in resp.context["clusters"]} == {True}
    # Alphabetical, the tie-breaker, since nothing else separates them.
    assert [cl["firm_slug"] for cl in resp.context["clusters"]] == ["hollow", "real"]


@pytest.mark.django_db
def test_a_prose_read_deadline_says_reported_in_words_on_the_picked_column(client):
    """P8. The dotted underline needs a hover; the Picked column is where a
    student acts on a countdown without one. The firm column keeps the
    screen-reader-only copy, so the page says "reported" about this row
    once per column and never twice in one."""
    alpha = _firm("alpha", "Alpha", ["ib"])
    _opp(alpha, 1, "2027 Investment Banking Summer Analyst",
         deadline=timezone.localdate() + timedelta(days=9), confidence=0.6)
    user = _student()
    UserFirm.all_objects.create(user=user, firm=alpha, tier=1)
    client.force_login(user)

    body = client.get(reverse("opportunities")).content.decode()
    assert body.count('class="rr-due-prov"') == 1
    assert '<span class="rr-due-prov" title="Read from the posting' in body
    assert body.count("(reported)") == 1
    # The visible word and the hidden copy sit on different rows.
    picked = body.split('class="firmcol firmcol--picked')[1].split("</article>")[0]
    assert "rr-due-prov" in picked and "(reported)" not in picked


@pytest.mark.django_db
def test_a_field_stated_deadline_prints_no_provenance_word(client):
    alpha = _firm("alpha", "Alpha", ["ib"])
    _opp(alpha, 1, "2027 Investment Banking Summer Analyst",
         deadline=timezone.localdate() + timedelta(days=9), confidence=1.0)
    user = _student()
    UserFirm.all_objects.create(user=user, firm=alpha, tier=1)
    client.force_login(user)

    body = client.get(reverse("opportunities")).content.decode()
    assert 'class="rr-due-prov"' not in body and "(reported)" not in body


@pytest.mark.django_db
def test_the_track_facet_files_the_markets_programme_under_st(client):
    """P4 on its public surface: the same `role_function` drives the Track
    facet, so the change is visible there too — intended."""
    jpm = _firm("jpm", "J.P. Morgan", ["ib", "st"])
    markets = _opp(jpm, 1, "2027 Commercial & Investment Bank - Markets Summer Analyst Program - Hong Kong")
    payments = _opp(jpm, 2, "2027 Commercial & Investment Bank Global Payments Summer Analyst Program")
    ib = _opp(jpm, 3, "2027 Commercial & Investment Bank - Global Investment Banking Program - Summer Analyst - Hong Kong")

    def ids(track):
        resp = client.get(reverse("opportunities"), {"track": track})
        return {r["id"] for cl in resp.context["clusters"] for r in cl["roles"]}

    assert ids("ib") == {ib.id}
    assert ids("st") == {markets.id}
    assert payments.id not in ids("ib") | ids("st")


# ---------------------------------------------------------------------------
# An insight programme is the on-ramp to the internship the student declared,
# never the wrong rung. The bucket check in `role_matches_level` was written
# for the news strip to keep FULL-TIME roles off a sophomore's radar; applied
# unchanged to picks it hid every early-ID programme from every student who
# only ticked an internship cycle - which is nearly every student, and cost
# the founder his #1 pick (Nomura Discover) the day it landed.
# ---------------------------------------------------------------------------

def test_an_insight_programme_is_a_pick_for_a_student_who_declared_only_an_internship():
    from directory.recommend import role_matches_level
    assert role_matches_level("insight", "", ["2028 Summer Internship"], 2029,
                              title="Discover Programme")
    # The rung ABOVE is still refused - that is what the check exists for.
    assert not role_matches_level("entry_level", "", ["2028 Summer Internship"], 2029,
                                  title="Analyst Program")


def test_an_insight_programme_survives_recommend_for_the_founders_profile():
    """End to end through `recommend()`: the fixture mirrors Nomura Discover -
    insight bucket, cohort 2027, no stated class, at a tier-1 firm."""
    from directory.recommend import Candidate, Profile, _stated_grad_window, role_matches_level, derive_class_year
    profile = Profile(class_year=2029, target_cycles=("2028 Summer Internship",))
    c = Candidate(id=1, firm_id=1, firm_name="Nomura", firm_slug="nomura",
                  title="2027 Discover Nomura Programme - Insight Programme",
                  url="https://x.test/discover", bucket="insight", cohort="2027")
    assert _stated_grad_window(profile, c) is None
    derived, _ = derive_class_year(c.bucket, c.title, c.cohort)
    assert role_matches_level(c.bucket, derived, profile.target_cycles, profile.class_year,
                              title=c.title, study_level=profile.study_level)
