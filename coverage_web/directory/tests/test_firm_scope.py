"""What a firm page counts as "Open Roles", and whether it says so.

THE DEFECT. `firm_detail()` was the only user-facing queryset in the app that
filtered `status="open"` and stopped there. Every other surface — the feed,
the contacts board, Today, My Applications — scopes to `TARGET_BUCKETS`. On
live data that made /firms/barclays/ head its section "Open Roles 925" while
/app/contacts/ headed the same firm's tile "Barclays 13 Open", and 912 of the
925 were experienced requisitions grouped under a heading that read "Other"
and explained nothing.

These tests assert against RENDERED HTML rather than the view dict, because
the defect a reader hit was a heading and a missing sentence, not a count in
a context variable. A test that read `resp.context["total"]` would have gone
green on the old code the moment someone scoped the count without scoping the
rows, or vice versa.
"""

from __future__ import annotations

import pytest

from directory.classify import BUCKET_LABELS, OTHER
from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db


def _firm(slug="barclays", name="Barclays"):
    return Firm.objects.create(slug=slug, name=name)


def _opp(firm, n, *, bucket, title=None, region=""):
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{n}", bucket=bucket, status="open",
        title=title or f"Role {n}", region=region,
    )


def _page(client, firm, qs=""):
    res = client.get(f"/firms/{firm.slug}/{qs}")
    assert res.status_code == 200
    return res.content.decode()


@pytest.fixture
def barclays(db):
    """13 campus rows and 912-in-miniature: the live shape, scaled down."""
    firm = _firm()
    for i in range(13):
        _opp(firm, i, bucket="internship", title=f"Summer Analyst {i}")
    for i in range(20):
        _opp(firm, 100 + i, bucket=OTHER,
             title=f"Active Directory Engineer {i}")
    return firm


# ---------------------------------------------------------------------------
# 1. The count is the campus count — the same number every other surface shows
# ---------------------------------------------------------------------------
def test_open_roles_counts_campus_only(client, barclays):
    body = _page(client, barclays)
    assert 'Open Roles <span class="scrub-count">13</span>' in body


def test_experienced_rows_are_not_rendered_by_default(client, barclays):
    body = _page(client, barclays)
    assert "Summer Analyst 0" in body
    assert "Active Directory Engineer 0" not in body


# ---------------------------------------------------------------------------
# 2. The page says what it is not showing, and links out of it
# ---------------------------------------------------------------------------
def test_default_scope_states_itself_and_offers_the_optin(client, barclays):
    body = _page(client, barclays)
    assert "Showing campus roles only" in body
    assert "20 experienced roles hidden" in body
    assert 'href="/firms/barclays/?role=all"' in body


def test_scope_line_is_silent_when_there_is_nothing_to_disclose(client):
    """A firm with no experienced rows has no scope gap, so it gets no
    sentence. The disclosure exists to explain a difference; printed
    unconditionally it would be noise on the majority of firms."""
    firm = _firm(slug="gs", name="Goldman Sachs")
    _opp(firm, 1, bucket="internship")
    body = _page(client, firm)
    assert "Showing campus roles only" not in body
    assert "experienced role" not in body


# ---------------------------------------------------------------------------
# 3. The opt-in is real: ?role=all shows everything and says so
# ---------------------------------------------------------------------------
def test_role_all_shows_everything_and_names_both_halves(client, barclays):
    body = _page(client, barclays, "?role=all")
    assert 'Open Roles <span class="scrub-count">33</span>' in body
    assert "Active Directory Engineer 0" in body
    assert "Showing everything we scraped" in body
    assert "13 campus, 20 experienced" in body


def test_role_other_shows_only_the_experienced_half(client, barclays):
    body = _page(client, barclays, "?role=other")
    assert 'Open Roles <span class="scrub-count">20</span>' in body
    assert "Summer Analyst 0" not in body
    assert "13 campus roles hidden" in body


def test_unrecognised_role_falls_back_to_campus(client, barclays):
    """`?role=banana` — and `?role=internship`, which firm detail does not
    offer — must land on the scope the page's own sentence describes, or the
    line and the rows disagree."""
    for value in ("banana", "internship"):
        body = _page(client, barclays, f"?role={value}")
        assert 'Open Roles <span class="scrub-count">13</span>' in body
        assert "Showing campus roles only" in body


# ---------------------------------------------------------------------------
# 4. The group heading names what it groups
# ---------------------------------------------------------------------------
def test_the_experienced_group_is_called_experienced(client, barclays):
    body = _page(client, barclays, "?role=all")
    assert "Experienced" in body
    assert BUCKET_LABELS[OTHER] == "Experienced"


# ---------------------------------------------------------------------------
# 5. An empty campus scope is not an empty firm
# ---------------------------------------------------------------------------
def test_no_campus_roles_says_so_without_claiming_the_firm_is_quiet(client):
    firm = _firm(slug="db", name="Deutsche Bank")
    for i in range(4):
        _opp(firm, i, bucket=OTHER)
    body = _page(client, firm)
    assert "No campus roles open right now." in body
    assert "Nothing open right now." not in body
    assert "4 experienced roles open here" in body
    assert 'href="/firms/db/?role=all"' in body


def test_a_genuinely_empty_firm_still_reads_as_empty(client):
    firm = _firm(slug="ms", name="Morgan Stanley")
    body = _page(client, firm)
    assert "Nothing open right now." in body


# ---------------------------------------------------------------------------
# 6. A row whose last check couldn't reconfirm it live is marked here too —
# firm_detail's own row is the second surface (besides the feed card) a
# student can leave Coverage from without opening the drawer.
# ---------------------------------------------------------------------------
def test_a_row_our_last_check_could_not_reconfirm_is_marked(client):
    import re
    from django.utils import timezone
    from datetime import timedelta

    firm = _firm(slug="hsbc", name="HSBC")
    o = _opp(firm, 1, bucket="internship", title="Unconfirmed Analyst")
    now = timezone.now()
    Opportunity.objects.filter(pk=o.pk).update(
        last_verified=now - timedelta(days=1), last_checked=now)

    body = _page(client, firm)
    assert "is-unconfirmed" in body
    assert "(Not recently confirmed live)" in body


def test_a_freshly_confirmed_row_wears_no_caution(client):
    import re
    from django.utils import timezone

    firm = _firm(slug="hsbc2", name="HSBC 2")
    o = _opp(firm, 1, bucket="internship", title="Confirmed Analyst")
    now = timezone.now()
    Opportunity.objects.filter(pk=o.pk).update(last_verified=now, last_checked=now)

    body = re.sub(r"<style.*?</style>", "", _page(client, firm), flags=re.S)
    assert "Confirmed Analyst" in body
    assert "is-unconfirmed" not in body


# ---------------------------------------------------------------------------
# 7. The labels backing the region/track vocabulary are complete
#
# The firm page used to carry an eyebrow ("Recruits: Hong Kong · Investment
# Banking") built from these same maps; it was removed along with every other
# page's hero eyebrow and subtitle. The maps themselves are still live — the
# Opportunities facets read the same REGION_LABELS/TRACK_LABELS — so the
# completeness guard stays: a slug added to firms.yaml without a label would
# silently print raw wherever a facet renders it, exactly as `pipeline` once
# did in the old eyebrow.
# ---------------------------------------------------------------------------
def test_every_slug_the_live_data_holds_has_a_label(client):
    from directory.classify import REGION_LABELS
    from directory.views import TRACK_LABELS

    for slug in ("hk", "us"):
        assert slug in REGION_LABELS
    for slug in ("ib", "st", "am", "pe", "corp-strat", "consulting", "pipeline"):
        assert TRACK_LABELS.get(slug), f"track {slug!r} has no human label"


# ---------------------------------------------------------------------------
# 8. How MANY rows print — the second half of the same defect
#
# Section 1 fixed WHICH rows the page counts. It did not fix how many it
# prints, and on the live corpus that was the bigger number: PwC's campus
# scope is 716 rows and the page rendered all 716, measured at 66,832px at
# 1280 and 97,229px at 375 (74 and 120 screens). `?role=all` was 1,496 rows
# and 126,639px. Everything else on the page — subnav, header, the network
# slice, the timeline — measured 318px, so the role list was 99.5% of it.
#
# It was also a duplicate: /opportunities/?firm=pwc renders those same 716
# rows in 1,194px inside the firm column's scroll window, with a filter bar,
# facets and search. So each kind group now prints ROLE_ROWS_PER_GROUP rows
# and hands the rest to that surface.
#
# These assert the two ends of the measured distribution: the eight firms
# above 60 campus roles, and the median firm at 10 — which must come through
# completely untouched, no cap, no overflow link, no market line.
# ---------------------------------------------------------------------------
from directory.views import ROLE_ROWS_PER_GROUP  # noqa: E402


@pytest.fixture
def pwc(db):
    """The outlier shape in miniature: two campus groups, both over the cap,
    plus an experienced pile. PwC's live proportions are 397 internships,
    319 entry-level and 780 experienced."""
    firm = _firm(slug="pwc", name="PwC")
    for i in range(20):
        _opp(firm, i, bucket="internship", title=f"Summer Analyst {i:02d}")
    for i in range(40, 55):
        _opp(firm, i, bucket="entry_level", title=f"Graduate Associate {i}")
    for i in range(100, 130):
        _opp(firm, i, bucket=OTHER, title=f"Directory Engineer {i}")
    return firm


def test_a_group_over_the_cap_prints_the_cap_and_not_the_rest(client, pwc):
    body = _page(client, pwc)
    assert "Summer Analyst 00" in body
    assert f"Summer Analyst {ROLE_ROWS_PER_GROUP - 1:02d}" in body
    assert f"Summer Analyst {ROLE_ROWS_PER_GROUP:02d}" not in body
    assert "Summer Analyst 19" not in body


def test_the_heading_still_states_the_whole_group_not_the_printed_slice(
        client, pwc):
    """The count is the page's answer to "how many are open here", and every
    other surface answers it with the same number. A heading that counted
    what it printed would be the 925-vs-13 bug rebuilt: two true numbers,
    neither of them the one the reader asked for."""
    body = _page(client, pwc)
    assert 'Open Roles <span class="scrub-count">35</span>' in body
    assert '>Internship <span class="scrub-count">20</span>' in body
    assert '>Entry-Level <span class="scrub-count">15</span>' in body


def test_the_hidden_rows_are_counted_and_linked_to_the_feed(client, pwc):
    """A cap is hiding, and this page states what it hides. The count is the
    page's own arithmetic (group total minus rows printed) and the link lands
    on the same firm and the same kind in the surface built for the volume."""
    body = _page(client, pwc)
    assert f"Show the other {20 - ROLE_ROWS_PER_GROUP} in Opportunities" in body
    assert f"Show the other {15 - ROLE_ROWS_PER_GROUP} in Opportunities" in body
    assert 'href="/opportunities/?firm=pwc&amp;role=internship"' in body
    assert 'href="/opportunities/?firm=pwc&amp;role=entry_level"' in body


def test_the_cap_is_per_group_so_no_kind_vanishes(client, pwc):
    """THE REGRESSION GUARD. `cards` arrives campus-buckets-first, so a flat
    page-level cap of 12 would have printed 12 internships at PwC and dropped
    all 319 entry-level rows with nothing saying so — the page's original bug
    (a scope nobody was told about) rebuilt inside its own fix."""
    body = _page(client, pwc)
    assert "Summer Analyst 00" in body
    assert "Graduate Associate 40" in body


def test_the_experienced_optin_is_capped_by_the_same_rule(client, pwc):
    body = _page(client, pwc, "?role=other")
    assert 'Open Roles <span class="scrub-count">30</span>' in body
    assert "Directory Engineer 100" in body
    assert f"Show the other {30 - ROLE_ROWS_PER_GROUP} in Opportunities" in body
    assert 'href="/opportunities/?firm=pwc&amp;role=other"' in body


def test_role_all_caps_every_group_and_still_names_both_halves(client, pwc):
    body = _page(client, pwc, "?role=all")
    assert 'Open Roles <span class="scrub-count">65</span>' in body
    assert "Showing everything we scraped" in body
    assert "35 campus, 30 experienced" in body
    # One sample from each of the three kinds, none of them whole.
    for lead in ("Summer Analyst 00", "Graduate Associate 40",
                 "Directory Engineer 100"):
        assert lead in body
    assert body.count("Show the other") == 3


# ---- The median firm: 10 campus roles, and nothing about it changes -------
@pytest.fixture
def cicc(db):
    """The median: 10 campus roles across the 81 firms that have any. The cap
    exists for the outliers and must be invisible here."""
    firm = _firm(slug="cicc", name="CICC")
    for i in range(10):
        _opp(firm, i, bucket="internship", title=f"Summer Analyst {i}")
    return firm


def test_the_median_firm_prints_every_row(client, cicc):
    body = _page(client, cicc)
    for i in range(10):
        assert f"Summer Analyst {i}" in body


def test_the_median_firm_grows_no_overflow_link_and_no_market_line(
        client, cicc):
    """"Simple, minimalistic, no extra things" is measured here, not on PwC.
    A firm small enough to print in full must render exactly what it rendered
    before the cap existed."""
    body = _page(client, cicc)
    assert "Show the other" not in body
    # The class ATTRIBUTE, not the string: _styles.html is inlined into every
    # one of these pages, so a bare "fd-markets" matches its own CSS rule and
    # this assertion would pass on a page that rendered the line.
    assert 'class="fd-markets"' not in body


def test_a_firm_at_exactly_the_cap_is_not_capped(client):
    """The off-by-one: `more` is total minus cap, so a group of exactly 12
    must print 12 rows and say nothing about a remainder of zero."""
    firm = _firm(slug="edge", name="Edge Capital")
    for i in range(ROLE_ROWS_PER_GROUP):
        _opp(firm, i, bucket="internship", title=f"Summer Analyst {i:02d}")
    body = _page(client, firm)
    assert f"Summer Analyst {ROLE_ROWS_PER_GROUP - 1:02d}" in body
    assert "Show the other" not in body


# ---- The market line: what makes the cap honest ---------------------------
def test_a_capped_page_says_which_markets_the_hidden_rows_are_in(client):
    """Twelve sampled rows cannot tell a student who needs Singapore that 136
    of PwC's campus roles are there, and 716 printed rows told her only by
    being unreadable. Words and order come from the feed's own region facet
    (REGION_LABELS / REGION_ORDER), so the two surfaces cannot drift."""
    firm = _firm(slug="pwc2", name="PwC 2")
    for i in range(14):
        o = _opp(firm, i, bucket="internship", title=f"Summer Analyst {i:02d}")
        Opportunity.objects.filter(pk=o.pk).update(
            region="sg" if i < 9 else "eu")
    for i in range(50, 53):
        o = _opp(firm, i, bucket="internship", title=f"Unplaced Analyst {i}")
        Opportunity.objects.filter(pk=o.pk).update(region="")

    body = _page(client, firm)
    assert 'class="fd-markets"' in body
    assert "Singapore 9" in body
    assert "Europe 5" in body
    # The posting never said, which is not the same fact as a market we do
    # not track — the facet's own word for it, not "Other".
    assert "Unstated 3" in body


def test_the_market_line_describes_the_scope_in_view(client, pwc):
    """On `?role=other` the reader is looking at experienced rows, so the
    line must count those and not the campus rows the page is hiding."""
    Opportunity.objects.filter(firm=pwc, bucket=OTHER).update(region="us")
    Opportunity.objects.filter(firm=pwc, bucket="internship").update(region="hk")
    body = _page(client, pwc, "?role=other")
    assert "United States 30" in body
    assert "Hong Kong" not in body
