"""Round 5 regression: the Opportunities feed's per-firm city-variant grouping
folded UNRELATED postings into another role's "+N more locations" disclosure.

Root cause: `views.opportunities()` builds `cl["roles"]` (display items) and
`cl["_opps"]` (the Opportunity each item came from) in lockstep, one pair per
row — so they start index-aligned. The per-firm role sort then reordered
`cl["roles"]` alone; `cl["_opps"]` stayed in its original queryset order.
`_group_city_variants(cl["roles"], cl.pop("_opps", []))` zips the two lists
positionally, so once the orders diverged, an item's family key (and its
underlying title/location for matching) was computed from the WRONG
Opportunity — confirmed live on Morgan Stanley's "2027 Technology Summer
Analyst Program (Hong Kong)" card, which picked up a Mumbai wealth-management
role and a Seattle Parametric role as if they were the same programme in
another city.

These tests hit the real `opportunities()` view end to end (not just the pure
`_group_city_variants` helper, which was never itself wrong) so the fix is
proven at the layer that actually broke: the sort that desynced the two
parallel lists.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone

from directory.models import Firm, Opportunity


def _opp(firm, url, title, location, *, seen_days_ago, bucket="internship"):
    o = Opportunity.objects.create(
        firm=firm, url=url, title=title, location=location,
        bucket=bucket, status="open",
    )
    # first_seen is auto_now_add — set the real "how long has this been on
    # the board" value directly via update(), which auto_now_add does not
    # intercept, so the freshness-driven re-sort below is exercisable.
    Opportunity.objects.filter(id=o.id).update(
        first_seen=timezone.now() - timezone.timedelta(days=seen_days_ago))
    return o


@pytest.fixture
def misaligning_feed(db):
    """Three same-firm rows whose SORTED display order differs from their
    original queryset (insertion) order — the exact precondition that used
    to desync `cl["roles"]` from `cl["_opps"]`.

    Queryset order (firm, deadline nulls-last, title — all three are
    undated, so this is purely alphabetical): London-Analyst, Toronto-
    Analyst, Zeta-Independent.

    Post-sort order (freshness then title, since none are dated): Zeta-
    Independent lands FIRST because it is the freshest row, ahead of both
    Analyst-Programme cities, which is the actual sort-key inversion that
    exposed the bug.
    """
    firm = Firm.objects.create(slug="msbank", name="MS Bank")
    london = _opp(firm, "https://x/london", "Analyst Program (London)",
                 "London, United Kingdom", seen_days_ago=15)
    toronto = _opp(firm, "https://x/toronto", "Analyst Program (Toronto)",
                   "Toronto, Ontario", seen_days_ago=14)
    zeta = _opp(firm, "https://x/zeta", "Zeta Independent Role (Chicago)",
               "Chicago, Illinois", seen_days_ago=2)
    return {"firm": firm, "london": london, "toronto": toronto, "zeta": zeta}


def _cluster(resp, firm_slug):
    return next(c for c in resp.context["clusters"] if c["firm_slug"] == firm_slug)


def _by_id(cluster):
    return {r["id"]: r for r in cluster["roles"]}


@pytest.mark.django_db
def test_the_unrelated_role_never_becomes_a_variant_of_the_programme(client, misaligning_feed):
    """The core defect: a genuinely unrelated role (different base title,
    different city, no shared programme) must never show up inside another
    role's variants list."""
    resp = client.get(reverse("opportunities"))
    items = _by_id(_cluster(resp, "msbank"))

    zeta_id = misaligning_feed["zeta"].id
    all_variant_ids = {
        v["id"] for item in items.values() for v in item["variants"]
    }
    assert zeta_id not in all_variant_ids
    assert items[zeta_id]["in_group"] is False


@pytest.mark.django_db
def test_the_two_real_city_variants_still_group_together(client, misaligning_feed):
    """The fix must not just stop the false grouping — the genuine
    same-programme, different-city pair (London/Toronto Analyst Program)
    must still fold into one card with a variants disclosure."""
    resp = client.get(reverse("opportunities"))
    items = _by_id(_cluster(resp, "msbank"))

    london_id = misaligning_feed["london"].id
    toronto_id = misaligning_feed["toronto"].id
    heads_holding_the_pair = [
        item for item in items.values()
        if {v["id"] for v in item["variants"]} & {london_id, toronto_id}
    ]
    assert len(heads_holding_the_pair) == 1
    head = heads_holding_the_pair[0]
    # One of the pair is the head itself, the other its variant — either
    # way, both real cities must be accounted for exactly once.
    grouped_ids = {head["id"]} | {v["id"] for v in head["variants"]}
    assert grouped_ids == {london_id, toronto_id}
