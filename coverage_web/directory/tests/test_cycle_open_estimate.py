"""When the student's OWN cycle opens, and the honest empty rail beside it.

Two items, one file, because they are one sentence on the page. The Picked
column's header already said "2028 Summer Internship postings haven't opened
in your regions yet" and then stopped, leaving the only question that sentence
raises unanswered while `FirmDate` held the answer the whole time. And the
column that carries it did not render at all when the scorer returned nothing
— the one profile most in need of an explanation got the least.

WHAT THE SENTENCE MAY CLAIM. Every SA 2028 date in the corpus is a forecast
(`research-us-ib-calendar.md §7`, Grade A/B), so the copy says "estimated"
whenever every row behind it is `precision="estimated"` and never presents a
date as the firm's own. A market with fewer than `CYCLE_OPEN_MIN_FIRMS`
sources says nothing at all rather than turning one firm's date into a
market's calendar — the same posture `research-us-ib-calendar.md §10` takes
from the other direction.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from django.utils import timezone

from directory.models import Firm, FirmDate, Opportunity
from directory.views import CYCLE_OPEN_MIN_FIRMS, cycle_open_estimate

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()
_STYLE_RE = re.compile(r"<style.*?</style>", re.S)


def _profile(django_user_model, email, **kw):
    user = django_user_model.objects.create_user(email=email, password="x" * 14)
    for k, v in kw.items():
        setattr(user, k, v)
    user.save()
    return user


def _app_open(firm, *, region, when, cycle="sa2028", track="",
              precision="estimated", confidence=0.6):
    return FirmDate.objects.create(
        firm=firm, cycle=cycle, track=track, region=region,
        event_kind="app_open", date=when, precision=precision,
        confidence=confidence, source_url="seed:historical-pattern",
    )


def _market(prefix, *, region, months, n=CYCLE_OPEN_MIN_FIRMS, **kw):
    """`n` firms in one market, their opens spread across `months`."""
    for i in range(n):
        firm = Firm.objects.create(slug=f"{prefix}{i}", name=f"{prefix.upper()} {i}")
        _app_open(firm, region=region, when=months[i % len(months)], **kw)


# ---------------------------------------------------------------------------
# THE SENTENCE ITSELF
# ---------------------------------------------------------------------------

def test_it_names_both_markets_with_their_month_ranges(django_user_model):
    """The founder's own shape: hk+us, "2028 Summer Internship". On the live
    corpus that is 7 Hong Kong firms clustered on September 2027 and 16 US
    firms spread from December 2026 to April 2027 — two genuinely different
    calendars, which is the whole reason the sentence is per market."""
    user = _profile(django_user_model, "both@example.com",
                    regions=["hk", "us"], tracks=["ib", "st"],
                    class_year=2029, target_cycles=["2028 Summer Internship"])
    _market("hk", region="hk", months=[date(2027, 9, 1), date(2027, 10, 1)])
    _market("us", region="us", months=[date(2026, 12, 1), date(2027, 4, 1)])

    note = cycle_open_estimate(user)
    assert "Hong Kong" in note and "United States" in note
    assert "Sep 2027 to Oct 2027" in note
    assert "Dec 2026 to Apr 2027" in note


def test_the_word_estimated_appears_when_every_source_row_is_one(django_user_model):
    user = _profile(django_user_model, "est@example.com", regions=["hk"],
                    target_cycles=["2028 Summer Internship"])
    _market("est", region="hk", months=[date(2027, 9, 1)])

    note = cycle_open_estimate(user)
    assert "Estimated to open" in note
    assert "Not a firm's own published date." in note


def test_a_firmer_row_drops_the_word_but_still_never_claims_confirmed(django_user_model):
    """One row published by a firm does not make the RANGE a published date —
    it is still built from several firms' worth of history — so the wording
    softens from "estimated" to "expected" and the closing caveat stays."""
    user = _profile(django_user_model, "firm@example.com", regions=["us"],
                    target_cycles=["2028 Summer Internship"])
    _market("firmer", region="us", months=[date(2027, 2, 1)])
    published = Firm.objects.create(slug="gs-pub", name="Goldman")
    _app_open(published, region="us", when=date(2027, 3, 1),
              precision="", confidence=1.0)

    note = cycle_open_estimate(user)
    assert "Expected to open" in note
    assert "Estimated" not in note
    assert "confirmed" not in note.lower()
    assert "Not a firm's own published date." in note


def test_a_market_below_the_floor_says_nothing(django_user_model):
    """One firm's date is that firm's date. Printing it as "when your cycle
    opens in Hong Kong" turns one observation into a market-wide claim."""
    user = _profile(django_user_model, "thin@example.com", regions=["hk"],
                    target_cycles=["2028 Summer Internship"])
    _market("thin", region="hk", months=[date(2027, 9, 1)],
            n=CYCLE_OPEN_MIN_FIRMS - 1)

    assert cycle_open_estimate(user) == ""


def test_a_date_already_past_is_not_an_answer_to_when_it_opens(django_user_model):
    """And this is what makes the sentence robust to the mislabelled rows
    WS-CRM-02 will fix. One live `app_open` row (Nomura, Hong Kong) carries
    `cycle=sa2028` with a 2026-09-01 date, a year early — the same
    off-by-one-cycle defect as the six HK closes. A past date cannot answer
    "when does it open", so it is dropped here rather than dragging the Hong
    Kong range back twelve months, and when those rows are relabelled nothing
    in this function changes."""
    user = _profile(django_user_model, "past@example.com", regions=["hk"],
                    target_cycles=["2028 Summer Internship"])
    _market("future", region="hk", months=[date(2027, 9, 1)])
    stale = Firm.objects.create(slug="nomura-stale", name="Nomura")
    _app_open(stale, region="hk", when=TODAY - timedelta(days=1))

    note = cycle_open_estimate(user)
    assert "Sep 2027" in note
    assert f"{TODAY:%b %Y}" not in note


def test_a_desk_the_student_did_not_name_is_not_their_cycle(django_user_model):
    """`FirmDate.track` was split out of `cycle` precisely so a student's
    stated desks could be matched. A `pe` date is a real date about a
    different pipeline; a blank track is cycle-wide and counts for everyone."""
    user = _profile(django_user_model, "desk@example.com", regions=["us"],
                    tracks=["ib"], target_cycles=["2028 Summer Internship"])
    _market("ibfirm", region="us", months=[date(2027, 2, 1)], track="ib")
    pe = Firm.objects.create(slug="kkr-pe", name="KKR")
    _app_open(pe, region="us", when=date(2026, 12, 1), track="pe")

    note = cycle_open_estimate(user)
    assert "Feb 2027" in note
    assert "Dec 2026" not in note


def test_two_rows_at_one_firm_are_one_firms_opinion(django_user_model):
    """Goldman files a cycle-wide US row and an `ib` one. Counting rows
    rather than firms would let a single firm clear a floor that exists to
    require several."""
    user = _profile(django_user_model, "onefirm@example.com", regions=["us"],
                    tracks=["ib"], target_cycles=["2028 Summer Internship"])
    gs = Firm.objects.create(slug="gs-two", name="Goldman")
    _app_open(gs, region="us", when=date(2027, 3, 1))
    _app_open(gs, region="us", when=date(2027, 2, 1), track="ib")

    assert cycle_open_estimate(user) == ""


@pytest.mark.parametrize("kw", [
    {"regions": [], "target_cycles": ["2028 Summer Internship"]},
    {"regions": ["us"], "target_cycles": []},
    {"regions": ["us"], "target_cycles": ["something unparseable"]},
])
def test_a_thin_profile_gets_todays_behaviour(django_user_model, kw):
    """P3. No regions, no cycle, or a cycle nothing can parse: silence, which
    is exactly what the page did before this function existed."""
    user = _profile(django_user_model, "thin-prof@example.com", **kw)
    _market("anything", region="us", months=[date(2027, 2, 1)])
    assert cycle_open_estimate(user) == ""


def test_the_copy_carries_no_em_dash(django_user_model):
    """Founder's own rule (P7)."""
    user = _profile(django_user_model, "dash@example.com", regions=["hk", "us"],
                    target_cycles=["2028 Summer Internship"])
    _market("hkd", region="hk", months=[date(2027, 9, 1)])
    _market("usd", region="us", months=[date(2027, 2, 1)])
    assert "—" not in cycle_open_estimate(user)


# ---------------------------------------------------------------------------
# ON THE PAGE, AND THE HONEST EMPTY RAIL (WS-OPP-18)
# ---------------------------------------------------------------------------

def test_the_picked_column_prints_both_sentences(client, django_user_model):
    from crm.models import UserFirm

    user = _profile(django_user_model, "page@example.com", regions=["us"],
                    tracks=["ib"], class_year=2029,
                    target_cycles=["2028 Summer Internship"])
    client.force_login(user)

    firm = Firm.objects.create(slug="pjt-page", name="PJT", tracks=["ib"])
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst", bucket="internship",
        status="open", url="https://pjt.test/page", region="us",
        cohort="2027", location="New York")
    _market("pagefirm", region="us", months=[date(2027, 2, 1), date(2027, 3, 1)])

    body = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    # BOTH SENTENCES MOVED OFF THE CARD AND ONTO THE COLUMN'S HOVER
    # (2026-09-02, the founder's call). They stood between the heading and
    # the first role, so the column spent five lines explaining before it
    # showed anything to apply to. What they SAY has not changed and is
    # still on the page, on the header's `title`, and the weekly digest
    # still prints both in full — a digest has no hover to put them on.
    header = re.search(r'<header class="firmcol-head"[^>]*title="([^"]*)"', body)
    assert header, "the picked column's header carries no context hover"
    hover = header.group(1)
    assert "not open in your regions yet" in hover
    assert "Closest fits" in hover
    assert "Estimated to open" in hover
    assert "Feb 2027 to Mar 2027" in hover
    assert "From past cycles at" in hover
    assert "Not a firm&#x27;s own published date." in hover
    # And the card itself no longer spends a line on either.
    assert '<span class="firmcol-cycle-note"' not in body


def test_a_rail_that_scores_nothing_still_explains_itself(client, django_user_model):
    """The state `recommend()` has always been able to return and the page
    has never rendered. It used to drop the whole column, so the profile most
    in need of an explanation got no column, no sentence and no next step."""
    user = _profile(django_user_model, "empty@example.com", regions=["us"],
                    tracks=["ib"], class_year=2029,
                    target_cycles=["2028 Summer Internship"])
    client.force_login(user)

    # Untargeted, untiered, wrong region and off-track: nothing clears the bar.
    firm = Firm.objects.create(slug="nothing", name="Nothing", tracks=["consulting"])
    Opportunity.objects.create(
        firm=firm, title="Operations Programme", bucket="internship",
        status="open", url="https://nothing.test/1", region="eu",
        location="Paris")
    _market("emptyfirm", region="us", months=[date(2027, 2, 1)])

    body = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    assert "firmcol--picked" in body, "the column must not vanish"
    assert "Nothing scores high enough yet" in body
    # Paired with the cycle sentences, which on an early-cycle profile are
    # usually the whole of the answer.
    assert "Estimated to open" in body
    # And a next step, which is the other half of an honest empty state.
    assert "Settings" in body
    # No filler: the column offers no role cards it did not score.
    assert 'class="rolerow' not in body.split("firmcol--picked")[1].split("</article>")[0]


def test_an_anonymous_visitor_gets_no_picked_column(client):
    """Unchanged. `profile` is None for them and the branch never runs."""
    firm = Firm.objects.create(slug="anon-firm", name="Anon")
    Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        url="https://anon.test/1", region="us", location="New York")

    body = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    assert "firmcol--picked" not in body


def test_the_digest_reads_the_same_sentence_the_page_does(django_user_model):
    """P5. The email and the page are two renderings of one fact; the day the
    digest wrote its own month range is the day they could disagree about a
    date.

    THE PAGE SPLITS THE SENTENCE, THE DIGEST DOES NOT (2026-09-02), and both
    still read one function. `_cycle_open_parts` builds the claim and counts
    the firms; `cycle_open_estimate` joins them for the email, which has room
    for the disclaimer inline and no hover to put it on; `cycle_open_note`
    hands the column the claim and the provenance separately. A date can no
    more differ between the two than it could before, because neither one
    formats a date."""
    from crm.digest import _cycle_note
    from directory.recommend import Candidate, Recommendation

    user = _profile(django_user_model, "digest@example.com", regions=["us"],
                    tracks=["ib"], target_cycles=["2028 Summer Internship"])
    _market("digestfirm", region="us", months=[date(2027, 2, 1)])
    firm = Firm.objects.create(slug="lazard-d", name="Lazard", tracks=["ib"])

    rec = Recommendation(
        candidate=Candidate(id=1, firm_id=firm.id, firm_name=firm.name,
                            firm_slug=firm.slug, title="2027 Summer Analyst",
                            url="https://lazard.test/1", bucket="internship",
                            cohort="2027", region="us"),
        score=40, reasons=(),
    )
    note = _cycle_note(user, [rec])
    assert "Nothing yet for your 2028 Summer Internship cycle" in note
    assert cycle_open_estimate(user) in note


def test_the_digest_stays_quiet_when_the_picks_are_in_the_students_cycle(django_user_model):
    """A student whose picks ARE in their cycle has no question for this
    sentence to answer, and volunteering a date about a cycle already
    underway would be the email answering one nobody asked."""
    from crm.digest import _cycle_note
    from directory.recommend import Candidate, Recommendation

    user = _profile(django_user_model, "digest-ok@example.com", regions=["us"],
                    tracks=["ib"], target_cycles=["2028 Summer Internship"])
    _market("quietfirm", region="us", months=[date(2027, 2, 1)])
    firm = Firm.objects.create(slug="lazard-q", name="Lazard", tracks=["ib"])

    rec = Recommendation(
        candidate=Candidate(id=1, firm_id=firm.id, firm_name=firm.name,
                            firm_slug=firm.slug, title="2028 Summer Analyst",
                            url="https://lazard.test/2", bucket="internship",
                            cohort="2028", region="us"),
        score=40, reasons=(),
    )
    assert _cycle_note(user, [rec]) == ""


def test_the_page_and_the_digest_are_built_from_one_forecast(django_user_model):
    """The split is a presentation split and nothing else: joined back
    together, the column's two halves are the digest's sentence, character
    for character. If they ever stop being, the email and the board are free
    to disagree about a month range, which is the whole thing P5 forbids."""
    from directory.views import cycle_open_note

    user = _profile(django_user_model, "split@example.com", regions=["us"],
                    tracks=["ib"], target_cycles=["2028 Summer Internship"])
    _market("splitfirm", region="us", months=[date(2027, 2, 1)])

    whole = cycle_open_estimate(user)
    note = cycle_open_note(user)
    assert whole
    assert note["text"] == "Estimated to open United States Feb 2027"
    # The claim is the visible line, so the honesty word has to be on it.
    assert note["text"].startswith("Estimated to open")
    # The provenance is the hover, and it is a sentence rather than a fragment.
    assert note["why"] == "From past cycles at 3 firms. Not a firm's own published date."
    assert whole == f"{note['text']}, {note['why'][0].lower()}{note['why'][1:]}"


def test_a_corpus_that_cannot_say_gives_the_column_nothing_to_render():
    """`{}` rather than a dict of empty strings: the template guards on
    `pick_cluster.cycle_open.text`, and an empty-string `text` inside a truthy
    dict would render an empty italic line where the old code rendered
    nothing. P3, unchanged behaviour on thin data."""
    from directory.views import cycle_open_note

    class _Thin:
        target_cycles = ()
        regions = ()
        tracks = ()

    assert cycle_open_note(_Thin()) == {}
    assert cycle_open_estimate(_Thin()) == ""
