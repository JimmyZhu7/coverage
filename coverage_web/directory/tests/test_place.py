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
    """Each feed row's place text, as a reader sees it. The card's own
    `.rolecard-sub` line is gone (see `_rolecard.html`); the compact row
    prints the same fact as a plain `.rr-loc` span on its one meta line."""
    body = client.get("/opportunities/").content.decode()
    return [" ".join(html.unescape(re.sub(r"<[^>]+>", "", m)).split())
            for m in re.findall(r'<span class="rr-loc[^"]*"[^>]*>(.*?)</span>', body, re.S)]


# ---------------------------------------------------------------------------
# 1. The resolver itself
# ---------------------------------------------------------------------------
def test_a_stated_location_is_used_verbatim():
    firm = _firm()
    p = _place(_opp(firm, location="New York, NY, United States", region="us"))
    assert p == {"text": "New York, NY, United States", "more": 0,
                 "exact": True, "why": ""}


# ---------------------------------------------------------------------------
# 1b. …and a stated location that is NOT a place, or is a place with source
#     plumbing welded to it, is tidied before it is printed.
#
# `location` stays raw in the database (evidence is never rewritten); this is
# a RENDER rule, and every part of it subtracts. Measured on the live corpus
# the day it shipped: 195 of 2,599 open campus place lines (7.5%) were one of
# the shapes below, and 1,960 of 16,561 open rows (11.8%).
#
# Neither existing repair path could close it. `normalize_workday_locations`
# has been run to exhaustion — 0 of 11,350 open Workday rows would change
# today — and its slot-gap rule cannot see a colon join.
# `backfill_detail_locations` needs a stored `detail_location`, which 0 of
# the 1,321 "N Locations" rows have.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, printed", [
    # A colon with no spaces is Workday's site-address/city seam, and the
    # city is the right half. (The founder's two screenshot bugs.)
    ("9-10 TAUNUSANLAGE:FRANKFURT AM MAIN", "FRANKFURT AM MAIN"),
    ("PERSIARAN IRC 2, IOI RESORT CITY IOI CITY TOWER ONE:PUTRAJAYA",
     "PUTRAJAYA"),
    ("RBC WATERPARK PLACE, 88 QUEENS QUAY W:TORONTO", "TORONTO"),
    # …but only when the right half is a place. Here the colon belongs to a
    # house number, so the string is left exactly as it arrived.
    ("Istanbul, Büyükdere Caddesi No:175", "Istanbul, Büyükdere Caddesi No:175"),
    # Spaces around the colon mean something else again — a room, not a city.
    ("UT Austin // Crum Auditorium : RRH 1.400",
     "UT Austin // Crum Auditorium : RRH 1.400"),
    # A postal code is not a city. The state it is welded to IS a fact.
    ("NY 10001", "NY"),
    ("Denver, CO, US, 80206", "Denver, CO, US"),
    ("Luxembourg, LU, L-1855", "Luxembourg, LU"),
    ("London, GB, E14 5EY", "London, GB"),
    # A street address beside a place name tells a scanning student nothing
    # the place name does not.
    ("New York, 745 7th Avenue", "New York"),
    ("Toronto - 18 York Street", "Toronto"),
    ("Singapore, Marina Bay Financial Tower 2", "Singapore"),
    # …unless the address is ALL there is, in which case it stays: an empty
    # place line is a worse answer than an imprecise one.
    ("1 New York Plaza", "1 New York Plaza"),
    # Entities leak out of scraped HTML.
    ("Online via Microsoft Teams&#160", "Online via Microsoft Teams"),
    # NOT TOUCHED. The dash split exists so the street rule can see "Toronto
    # - 18 York Street"; a line with nothing to drop keeps its own
    # punctuation rather than being restyled into commas.
    ("Singapore - Central", "Singapore - Central"),
    ("Mönchengladbach - Santander-Platz", "Mönchengladbach - Santander-Platz"),
    # NOT TOUCHED. The street words are deliberately the unambiguous ones and
    # each needs a digit beside it, so real places survive.
    ("St. Louis, MO", "St. Louis, MO"),
    ("Ave Maria, FL", "Ave Maria, FL"),
    ("Seoul, Korea, Republic of", "Seoul, Korea, Republic of"),
])
def test_a_stated_location_is_tidied_but_never_added_to(raw, printed):
    firm = _firm()
    p = _place(_opp(firm, location=raw, region="us"))
    assert p["text"] == printed
    assert p["exact"] is True
    # Whatever was dropped is still quoted back, so nothing goes silently.
    if printed != raw:
        assert raw in p["why"]


def test_a_count_of_locations_is_not_a_place(client):
    """Workday's aggregate placeholder sat in the slot where a city goes on
    97 open campus cards and 1,321 open rows, styled as a confirmed location.
    It answers nothing a student asked, so the row falls through to the
    market it was filed under — marked coarse, with the count kept in the
    tooltip rather than deleted."""
    firm = _firm()
    p = _place(_opp(firm, location="2 Locations", region="us"))
    assert p["text"] == "United States"
    assert p["exact"] is False
    assert "2 Locations" in p["why"]


def test_a_count_of_locations_with_no_market_says_nothing():
    """Never invent: 108 of the 1,321 placeholder rows have no region either,
    and silence is the honest value for those."""
    firm = _firm()
    assert _place(_opp(firm, location="4 Locations", region=""))["text"] == ""


def test_a_list_of_places_names_the_first_and_counts_the_rest():
    """A six-city semicolon list is 160 characters in a slot that ellipsises
    at ~34, so what a student read was a word cut in half. The count rides
    outside `text` because it is copy we wrote — `smart_location` cases text
    a careers board wrote, and piping it through produced "+5 More"."""
    firm = _firm()
    p = _place(_opp(firm, region="us", location=(
        "Chicago, Illinois, United States; Greenwich, Connecticut, United "
        "States; Houston, Texas, United States")))
    assert p["text"] == "Chicago, Illinois, United States"
    assert p["more"] == 2
    assert "Houston" in p["why"], "the full list stays one hover away"


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


# ---------------------------------------------------------------------------
# 4. The title stops restating the place line
#
# 297 of 2,599 open campus cards ended their title in the very city their sub
# row names. 268 of those were hitting the two-line title clamp; without the
# echo, 207 are.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("title, location, shown", [
    ("2027 APAC Banking Summer Analyst - Hong Kong", "Hong Kong, Hong Kong",
     "2027 APAC Banking Summer Analyst"),
    ("May 2027 - Assurance CPA - Summer Internship/Co-op - Winnipeg",
     "Winnipeg", "May 2027 - Assurance CPA - Summer Internship/Co-op"),
    ("GCB Full Time Analyst - 2026 - Seoul", "Seoul",
     "GCB Full Time Analyst - 2026"),
    # A desk name is not a city, and this is the whole reason the rule tests
    # the tail against the PLACE rather than merely stripping a trailing dash.
    ("Internship - Cyber Security", "London", "Internship - Cyber Security"),
    # The tail is a city, but not THIS row's city.
    ("Summer Analyst - Frankfurt", "London", "Summer Analyst - Frankfurt"),
    # No place line to defer to means nothing may be dropped.
    ("Summer Analyst - Hong Kong", "", "Summer Analyst - Hong Kong"),
])
def test_a_title_does_not_restate_the_place_line_below_it(title, location, shown):
    from directory.views import _card

    firm = _firm()
    o = _opp(firm, location=location, title=title)
    assert _card(o, now=timezone.now(), today=timezone.localdate())["title"] == shown


def test_the_echo_is_only_cut_when_the_PRINTED_place_still_names_it():
    """The trap this rule was written around. `location` here contains
    "Whippany", so keying off the raw column (as `_family_key` does, for a
    different question) would strip it from the title — but the place line
    tidies the street segment away and prints "Jefferson Park", so the card
    would have ended up with no mention of the city at all."""
    from directory.views import _card

    firm = _firm()
    o = _opp(firm, title="Summer Analyst - Whippany",
             location="Building 400-Whippany Campus, Jefferson Park")
    card = _card(o, now=timezone.now(), today=timezone.localdate())
    assert card["place"]["text"] == "Jefferson Park"
    assert card["title"] == "Summer Analyst - Whippany"


def test_the_drawer_prints_the_same_place_as_the_card_that_opened_it(client):
    """A third surface reading `location` raw is the same defect `_place`
    exists to close, with one more place to notice it."""
    firm = _firm()
    o = _opp(firm, location="9-10 TAUNUSANLAGE:FRANKFURT AM MAIN", region="eu")
    body = client.get(f"/opportunities/{o.pk}/read/").content.decode()
    assert "TAUNUSANLAGE" not in body.replace(
        "9-10 TAUNUSANLAGE:FRANKFURT AM MAIN", "", 1), "raw is only the tooltip"
    assert "FRANKFURT AM MAIN" in body


def test_neither_surface_invents_a_sentence_for_an_unknown_place(client):
    firm = _firm()
    _opp(firm, location="", region="")
    firm_body = client.get(f"/firms/{firm.slug}/").content.decode()
    feed_body = client.get("/opportunities/").content.decode()
    for body in (firm_body, feed_body):
        assert "Location not listed" not in body
        assert "not listed" not in body.lower()
