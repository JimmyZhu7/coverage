"""Where a role IS, and whether Coverage's two role surfaces agree about it.

THE DEFECT. `_card.html` printed the literal "Location not listed" whenever
`Opportunity.location` was blank; `_rolecard.html` omitted its location span
for exactly the same rows. Live, /firms/hsbc/ said "Location not listed" 19
times and /firms/bofa/ 21 times, while /opportunities/ said it zero times and
rendered 51 cards with an entirely empty `.rolecard-sub`. One role therefore
read location-UNKNOWN on the firm page and location-SILENT on the feed.

The claim was also wrong on its own page. All 19 of HSBC's campus rows name
their city inside their own TITLE, so "New York Investment Banking Graduate NY
10001" sat directly above "Location not listed" — and `_card` was already
carrying `opp.region`, so the page held the market and denied it.

These assert against RENDERED HTML on both surfaces, because the defect lived
in the seam between one view dict and two templates: a helper-only test would
have gone green while the two pages kept disagreeing.
"""

from __future__ import annotations

import html
import re

import pytest
from django.utils import timezone

from directory.models import Firm, Opportunity
from directory.views import _place

pytestmark = pytest.mark.django_db

NOW = timezone.now()


def _firm(slug="hsbc", name="HSBC"):
    return Firm.objects.get_or_create(slug=slug, defaults={"name": name})[0]


def _opp(firm, *, location="", region="", title="Summer Analyst", n=1):
    return Opportunity.objects.create(
        firm=firm, url=f"https://example.test/{n}", title=title,
        bucket="internship", status="open", location=location, region=region,
    )


def _firm_row_meta(client, firm):
    """The meta line under a firm-page row, as a reader sees it."""
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    return [" ".join(html.unescape(re.sub(r"<[^>]+>", "", m)).split())
            for m in re.findall(r'<p class="frow-meta[^"]*"[^>]*>(.*?)</p>', body, re.S)]


def _feed_sub(client):
    """Each feed card's sub-line, as a reader sees it."""
    body = client.get("/opportunities/").content.decode()
    return [" ".join(html.unescape(re.sub(r"<[^>]+>", "", m)).split())
            for m in re.findall(r'<div class="rolecard-sub">(.*?)</div>', body, re.S)]


# ---------------------------------------------------------------------------
# 1. The resolver itself
# ---------------------------------------------------------------------------
def test_a_stated_location_is_used_verbatim():
    firm = _firm()
    p = _place(_opp(firm, location="New York, NY, United States", region="us"))
    assert p == {"text": "New York, NY, United States", "exact": True, "why": ""}


def test_a_blank_location_falls_back_to_the_market_we_hold():
    """The strengthening fact: the row was never place-less in the data."""
    firm = _firm()
    p = _place(_opp(firm, location="", region="us"))
    assert p["text"] == "United States"
    assert p["exact"] is False
    assert p["why"]                       # says WHY it is coarse


def test_a_row_with_neither_says_nothing_at_all():
    """596 of the 1,043 open blank-location rows have a blank region too, so
    the fallback rescues only some of them. The rest get silence, which is
    what the feed already did."""
    firm = _firm()
    assert _place(_opp(firm, location="", region=""))["text"] == ""


def test_other_markets_is_a_filter_bucket_not_a_place():
    """`other` means "outside the six markets we track". Printing "Other
    Markets" where a reader expects a city states nothing."""
    firm = _firm()
    assert _place(_opp(firm, location="", region="other"))["text"] == ""


def test_global_is_a_real_answer_and_is_kept():
    """BofA's virtual recruitment events and KKR's talent community are
    placeless BY DESIGN, and that is itself a stated fact."""
    firm = _firm()
    assert _place(_opp(firm, location="", region="global"))["text"] == "Global / Virtual"


# ---------------------------------------------------------------------------
# 2. The firm page stops claiming a location is missing
# ---------------------------------------------------------------------------
def test_the_firm_page_no_longer_says_location_not_listed(client):
    """The live hsbc row, title and all."""
    firm = _firm()
    _opp(firm, location="", region="us",
         title="New York Investment Banking Graduate NY 10001")
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert "Location not listed" not in body
    assert _firm_row_meta(client, firm) == ["United States"]


def test_a_firm_row_with_no_place_at_all_renders_no_meta_line(client):
    firm = _firm()
    _opp(firm, location="", region="")
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert "Location not listed" not in body
    assert _firm_row_meta(client, firm) == []


# The page inlines its stylesheet, which NAMES `.is-market` in a rule long
# before any markup appears — so these match the class ATTRIBUTE, not the
# substring, the same trap test_today.py flags for `.dash-num`.
def _has_market_mark(body):
    return bool(re.search(r'class="frow-meta[^"]*\bis-market\b', body))


def test_a_market_level_place_is_marked_as_one(client):
    """Distinguishable from a stated city without hovering, with the reason
    one hover away — the same affordance `.is-reported` uses."""
    firm = _firm()
    _opp(firm, location="", region="hk")
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert _has_market_mark(body)
    assert "did not state a city" in body


def test_a_stated_city_is_not_marked_as_market_level(client):
    firm = _firm()
    _opp(firm, location="Hong Kong", region="hk")
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert not _has_market_mark(body)


# ---------------------------------------------------------------------------
# 3. The two surfaces agree, which is the point
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("location, region, expected", [
    ("New York, NY, United States", "us", "New York, NY, United States"),
    ("", "us", "United States"),
    ("", "hk", "Hong Kong"),
    ("", "global", "Global / Virtual"),
    ("", "other", ""),
    ("", "", ""),
])
def test_both_surfaces_print_the_same_place_for_the_same_row(
        client, location, region, expected):
    """The regression this whole change exists to prevent: one role reading
    location-unknown on the firm page and location-silent on the feed."""
    firm = _firm()
    _opp(firm, location=location, region=region, title="Global Markets Analyst")

    firm_meta = _firm_row_meta(client, firm)
    feed_subs = [s for s in _feed_sub(client) if s]

    assert firm_meta == ([expected] if expected else [])
    if expected:
        assert any(expected in s for s in feed_subs), feed_subs
    else:
        assert not any("United States" in s or "Location" in s for s in feed_subs)


def test_neither_surface_invents_a_sentence_for_an_unknown_place(client):
    firm = _firm()
    _opp(firm, location="", region="")
    firm_body = client.get(f"/firms/{firm.slug}/").content.decode()
    feed_body = client.get("/opportunities/").content.decode()
    for body in (firm_body, feed_body):
        assert "Location not listed" not in body
        assert "not listed" not in body.lower()
