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


def _opp(firm, n, *, bucket, title=None):
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{n}", bucket=bucket, status="open",
        title=title or f"Role {n}",
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
# 6. The eyebrow speaks the product's vocabulary, not firms.yaml's
#
# Live, the line above the firm's name joined the raw slug arrays under
# .pagehead-eyebrow's `text-transform: uppercase`: /firms/hsbc/ "HK · IB",
# /firms/bofa/ "US · IB, ST", /firms/alibaba/ "CORP-STRAT", /firms/mlt/
# "US · PIPELINE". The Opportunities facets spell the same concepts "Hong
# Kong", "Investment Banking", "Corporate Strategy" from the very same maps.
# ---------------------------------------------------------------------------
def _eyebrow(client, firm):
    """The eyebrow as a READER sees it — entities resolved, tags stripped."""
    import html
    import re
    m = re.search(r'<p class="pagehead-eyebrow">(.*?)</p>',
                  _page(client, firm), re.S)
    if not m:
        return None
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).split())


def test_the_eyebrow_labels_both_halves(client):
    firm = _firm(slug="hsbc", name="HSBC")
    firm.regions, firm.tracks = ["hk"], ["ib"]
    firm.save()
    assert _eyebrow(client, firm) == "Hong Kong · Investment Banking"


def test_a_multi_track_firm_labels_every_track(client):
    """bofa's live shape. `st` was the mildest instance and still had three
    spellings in the codebase — TRACK_LABELS "Sales &amp; Trading",
    _CYCLE_TRACKS "S&amp;T", and this page's bare ST."""
    firm = _firm(slug="bofa", name="Bank of America")
    firm.regions, firm.tracks = ["us"], ["ib", "st"]
    firm.save()
    assert _eyebrow(client, firm) == (
        "United States · Investment Banking, Sales & Trading")


def test_the_hyphenated_slug_is_the_worst_case_and_is_covered(client):
    """`corp-strat` reads as neither a word nor an abbreviation."""
    firm = _firm(slug="alibaba", name="Alibaba")
    firm.regions, firm.tracks = ["hk"], ["corp-strat"]
    firm.save()
    assert _eyebrow(client, firm) == "Hong Kong · Corporate Strategy"


def test_the_track_that_had_no_label_at_all_now_has_one(client):
    """`pipeline` (mlt, seo-career) was absent from TRACK_LABELS, so applying
    the existing maps and nothing else would have left PIPELINE rendering."""
    firm = _firm(slug="mlt", name="MLT")
    firm.regions, firm.tracks = ["us"], ["pipeline"]
    firm.save()
    eyebrow = _eyebrow(client, firm)
    assert eyebrow == "United States · Career Access Programme"
    assert "pipeline" not in eyebrow.lower()


def test_a_region_less_firm_renders_the_track_half_alone(client):
    """39 of 119 firms carry no region. The separator must not strand."""
    firm = _firm(slug="akuna", name="Akuna Capital")
    firm.regions, firm.tracks = [], ["st"]
    firm.save()
    assert _eyebrow(client, firm) == "Sales & Trading"


def test_a_firm_with_neither_renders_an_empty_eyebrow(client):
    firm = _firm(slug="none", name="Nowhere LLP")
    assert _eyebrow(client, firm) == ""


def test_every_slug_the_live_data_holds_has_a_label(client):
    """The guard that stops this regressing: a track or region added to
    firms.yaml without a label would silently print raw again, exactly as
    `pipeline` did. Asserted over the real vocabularies, so adding a slug to
    either map's domain without a label fails here."""
    from directory.classify import REGION_LABELS
    from directory.views import TRACK_LABELS

    for slug in ("hk", "us"):
        assert slug in REGION_LABELS
    for slug in ("ib", "st", "am", "pe", "corp-strat", "consulting", "pipeline"):
        assert TRACK_LABELS.get(slug), f"track {slug!r} has no human label"


def test_the_eyebrow_reads_the_same_maps_the_feed_facets_do(client):
    """Not a parallel copy of the words: the page renders the map's values."""
    from directory.views import TRACK_LABELS

    firm = _firm(slug="gs", name="Goldman Sachs")
    firm.regions, firm.tracks = ["us", "hk"], ["ib", "st"]
    firm.save()
    eyebrow = _eyebrow(client, firm)
    assert TRACK_LABELS["ib"] in eyebrow and TRACK_LABELS["st"] in eyebrow
    # Hong Kong first: the stored array order is arbitrary, so each half is
    # ordered the way its own facet lists it — REGION_ORDER for markets,
    # alphabetical by label for tracks.
    assert eyebrow == (
        "Hong Kong, United States · Investment Banking, Sales & Trading")
