"""directory/sponsorship.py — the one precedence rule (posting beats firm
policy beats unknown) every surface that answers "does this firm sponsor?"
now shares: the pill (`_sponsorship_tag`), the feed's sponsorship filter and
facet, `_eligibility`'s visa verdict, and the onboarding preview's work-auth
bars.

docs/founder-decisions-2026-08-20.md, Decision 3: 319 open campus rows had a
firm-level answer the pill already showed while the filter, the eligibility
verdict and the onboarding preview counted them as silent. These tests pin
the fix at the shared helper and at each of the surfaces that now call it.
"""

from __future__ import annotations

import pytest

from directory.models import Firm, Opportunity
from directory.sponsorship import effective_sponsorship, firm_policy_map, firm_policy_q
from directory.views import _apply_sponsorship_filter, _sponsorship_facet

pytestmark = pytest.mark.django_db


def _firm(slug="evercore", name="Evercore", **kw):
    return Firm.objects.create(slug=slug, name=name, **kw)


def _opp(firm, url, *, region="us", bucket="internship", **kw):
    return Opportunity.objects.create(
        firm=firm, url=url, title="Summer Analyst", bucket=bucket,
        status="open", region=region, **kw,
    )


# ---------------------------------------------------------------------------
# effective_sponsorship — the precedence rule itself
# ---------------------------------------------------------------------------

def test_a_stated_posting_wins_over_a_contradicting_firm_policy():
    firm = _firm(sponsors={"us": False})
    o = _opp(firm, "https://x/1", sponsorship="yes")
    assert effective_sponsorship(o) == ("yes", "posting")


def test_silence_falls_back_to_the_firms_regional_policy():
    firm = _firm(sponsors={"us": True, "hk": False})
    us_role = _opp(firm, "https://x/1", region="us")
    hk_role = _opp(firm, "https://x/2", region="hk")
    assert effective_sponsorship(us_role) == ("yes", "firm")
    assert effective_sponsorship(hk_role) == ("no", "firm")


def test_no_firm_data_and_no_posting_statement_is_unknown():
    firm = _firm()
    o = _opp(firm, "https://x/1")
    assert effective_sponsorship(o) == ("unknown", "unknown")


def test_a_blank_region_never_borrows_any_of_the_firms_policy():
    firm = _firm(sponsors={"us": True, "hk": True})
    o = _opp(firm, "https://x/1", region="")
    assert effective_sponsorship(o) == ("unknown", "unknown")


def test_a_firm_answering_one_region_does_not_answer_another():
    firm = _firm(sponsors={"hk": True})
    o = _opp(firm, "https://x/1", region="us")
    assert effective_sponsorship(o) == ("unknown", "unknown")


# ---------------------------------------------------------------------------
# firm_policy_map / firm_policy_q — the bulk companions
# ---------------------------------------------------------------------------

def test_firm_policy_map_only_carries_resolved_answers():
    firm = _firm(sponsors={"us": True, "hk": "unknown", "sg": False})
    policy = firm_policy_map()
    assert policy[(firm.id, "us")] == "yes"
    assert policy[(firm.id, "sg")] == "no"
    assert (firm.id, "hk") not in policy  # "unknown" carries no answer


def test_firm_policy_q_matches_only_the_stated_pairs():
    yes_firm = _firm(slug="yes-firm", sponsors={"us": True})
    no_firm = _firm(slug="no-firm", sponsors={"us": False})
    us_at_yes = _opp(yes_firm, "https://x/1", region="us")
    us_at_no = _opp(no_firm, "https://x/2", region="us")
    matched = set(Opportunity.objects.filter(firm_policy_q("yes")).values_list("id", flat=True))
    assert matched == {us_at_yes.id}
    matched_no = set(Opportunity.objects.filter(firm_policy_q("no")).values_list("id", flat=True))
    assert matched_no == {us_at_no.id}


# ---------------------------------------------------------------------------
# The feed's sponsorship filter and facet
# ---------------------------------------------------------------------------

def test_the_sponsorship_filter_includes_firm_answered_rows():
    """A role with no posting statement but a firm policy must pass the
    posting-level "yes"/"no" filter — the exact 319-row gap Decision 3
    measured: the pill already showed the answer, the filter didn't."""
    firm = _firm(sponsors={"us": False})
    firm_answered = _opp(firm, "https://x/1", region="us")  # silent posting
    stated_no = _opp(firm, "https://x/2", region="us", sponsorship="no")
    other_firm = _firm(slug="other", sponsors={})
    unrelated_unknown = _opp(other_firm, "https://x/3", region="us")

    no_qs = _apply_sponsorship_filter(Opportunity.objects.all(), "no")
    assert set(no_qs.values_list("id", flat=True)) == {firm_answered.id, stated_no.id}

    unknown_qs = _apply_sponsorship_filter(Opportunity.objects.all(), "unknown")
    assert set(unknown_qs.values_list("id", flat=True)) == {unrelated_unknown.id}


def test_the_facet_counts_a_firm_answered_row_as_answered_not_silent():
    firm = _firm(sponsors={"hk": True})
    _opp(firm, "https://x/1", region="hk")  # silent posting, firm says yes
    facet = _sponsorship_facet(Opportunity.objects.all(), "")
    per = {row["value"]: row["count"] for row in facet}
    assert per["yes"] == 1
    assert per["unknown"] == 0


# ---------------------------------------------------------------------------
# _eligibility — firm-sourced "no" is a warning, not a wall
# ---------------------------------------------------------------------------

def test_a_postings_own_no_still_blocks():
    from directory.views import _eligibility

    firm = _firm()
    o = _opp(firm, "https://x/1", region="hk", sponsorship="no")
    verdict = _eligibility(o, {"class_year": None, "work_auth": {"hk": "sponsorship"}})
    assert verdict["kind"] == "visa_out"
    assert verdict["blocking"] is True


def test_a_firm_sourced_no_warns_but_does_not_block():
    """docs/founder-decisions-2026-08-20.md, Decision 3: 'Firm-level "no" is
    a warning chip, not a blocking verdict... the product's rule is never to
    block on a guess.'"""
    from directory.views import _eligibility

    firm = _firm(sponsors={"hk": False})
    o = _opp(firm, "https://x/1", region="hk")  # posting itself says nothing
    verdict = _eligibility(o, {"class_year": None, "work_auth": {"hk": "sponsorship"}})
    assert verdict["kind"] == "visa_firm_no"
    assert verdict["blocking"] is False


def test_no_verdict_at_all_when_neither_side_has_an_answer():
    from directory.views import _eligibility

    firm = _firm()
    o = _opp(firm, "https://x/1", region="hk")
    assert _eligibility(o, {"class_year": None, "work_auth": {"hk": "sponsorship"}}) is None


# ---------------------------------------------------------------------------
# The FEED CARD — the surface where the filter runs and the decision is made,
# and the one that used to show a firm-policy row nothing at all.
# ---------------------------------------------------------------------------

def _chip_labels(o, verdict=None):
    from directory.views import _fact_chips
    return [c["label"] for c in _fact_chips(o, verdict=verdict)]


def test_the_card_says_when_a_sponsorship_answer_came_from_firm_policy():
    """The gap that made "Sponsors visas" a silent filter.

    `_apply_sponsorship_filter` counts firm-policy rows (304 of them live) and
    `_sponsorship_tag` had a label for them — but the tag only ever rendered on
    /firms/<slug>/, while the feed's card built its sponsorship chip off the raw
    `Opportunity.sponsorship` column. So a student filtering "Sponsors visas"
    got 304 cards with NO sponsorship chip: matched by a fact the card never
    showed. The label has to be where the decision is."""
    firm = _firm(sponsors={"us": True})
    o = _opp(firm, "https://x/1", region="us")   # the posting itself is silent
    assert _chip_labels(o) == ["Sponsors · firm policy"]

    no_firm = _firm(slug="lazard", name="Lazard", sponsors={"us": False})
    o2 = _opp(no_firm, "https://x/2", region="us")
    assert _chip_labels(o2) == ["No sponsorship · firm policy"]


def test_a_posting_stated_answer_never_wears_the_firm_policy_label():
    """The two claims must not read the same: a posting's own words are a
    statement about THIS role, a firm's policy is a general fact about the
    firm — which is exactly why `_eligibility` blocks on one and only warns on
    the other. The source is in the LABEL, not only the tooltip: tooltips are
    hover-only and this product's students are on phones."""
    firm = _firm(sponsors={"us": False})
    o = _opp(firm, "https://x/1", region="us", sponsorship="yes")
    assert _chip_labels(o) == ["Sponsors visas"]


def test_the_chip_stands_down_when_the_verdict_beside_it_already_said_it():
    """Both visa verdicts, not just the blocking one. `visa_firm_no` renders
    "Firm policy: may not sponsor here" — printing "No sponsorship · firm
    policy" next to it is the same sentence twice, and `_FACT_CHIPS_MAX` is 2,
    so the repeat costs a real fact its slot."""
    from directory.views import _eligibility

    firm = _firm(sponsors={"us": False})
    o = _opp(firm, "https://x/1", region="us")
    verdict = _eligibility(o, {"class_year": None, "work_auth": {"us": "sponsorship"}})
    assert verdict["kind"] == "visa_firm_no"
    assert _chip_labels(o, verdict=verdict) == []


def test_the_firm_page_states_the_answer_once_not_twice():
    """/firms/<slug>/ rendered `_card`'s tags AND its facts, so a posting that
    stated "no" printed the pill "No Sponsorship" beside the chip "No
    sponsorship" — one field, two visual languages, on two Barclays rows.
    One carrier now."""
    from django.utils import timezone

    from directory.views import _card

    firm = _firm()
    o = _opp(firm, "https://x/1", region="us", sponsorship="no")
    card = _card(o, now=timezone.now(), today=timezone.localdate())
    assert [c["label"] for c in card["facts"]] == ["No sponsorship"]
    assert not [t for t in card["tags"] if "ponsor" in t["label"]]
