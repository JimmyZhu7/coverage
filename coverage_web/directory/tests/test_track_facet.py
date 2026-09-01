"""The Opportunities Track facet, after it stopped being a claim about the
EMPLOYER and became a claim about the JOB (2026-09-01).

The defect these pin: `?track=ib` filtered on `Firm.tracks`, so every opening
at a bank that happens to cover investment banking counted as an investment
banking role. Measured on the live board the day this changed, `?track=ib`
returned 1,125 open campus roles of which 189 named IB in their title; 215
named an explicitly non-track function (Risk, Controllers, Branch, IT, HR) and
198 named a DIFFERENT track. st was 17% title-confirmed, consulting 15%, pe 8%,
am 5%. Meanwhile `recommend._track_fit` had been reading the role's own
function for months — two surfaces, two different meanings of "IB".

THE CONSTRAINT THAT MATTERS MOST is the one `test_a_silent_title_still_counts_
under_its_firms_track` pins: 1,336 of those 2,723 open campus rows (49%) name
no function at all ("2027 Summer Analyst"). The rule is "drop rows that state a
DIFFERENT function", never "keep only rows that state THIS one" — the second
reading would delete half the board.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db


def _firm(slug, name, tracks):
    return Firm.objects.create(slug=slug, name=name, tracks=tracks)


def _opp(firm, n, title, *, bucket="internship", region="us"):
    return Opportunity.objects.create(
        firm=firm, url=f"https://example.test/{firm.slug}/{n}", title=title,
        bucket=bucket, status="open", region=region,
    )


def _get(client, **params):
    return client.get(reverse("opportunities"), params)


def _ids(resp):
    """Every opportunity id the feed actually rendered, across the firm
    columns it groups them into."""
    return {
        role["id"]
        for cluster in resp.context["clusters"]
        for role in cluster["roles"]
    }


def _counts(resp):
    return {o["value"]: o["count"] for o in resp.context["facets"]["tracks"]}


@pytest.fixture
def board(db):
    """One universal bank (`ib` + `st`) and one consultancy, carrying the four
    title shapes the classifier distinguishes: silent, on-track, off-track,
    and explicitly non-track."""
    bank = _firm("ms", "Morgan Stanley", ["ib", "st"])
    consultancy = _firm("bain", "Bain", ["consulting"])
    return {
        "bank": bank,
        "consultancy": consultancy,
        # Silent — no function stated. Inherits the bank's ib + st.
        "silent": _opp(bank, 1, "2027 Summer Analyst"),
        # States one of the bank's own tracks.
        "ib": _opp(bank, 2, "2027 Investment Banking Summer Analyst"),
        # States the OTHER one.
        "st": _opp(bank, 3, "2027 Sales & Trading Summer Analyst"),
        # States a function outside the track vocabulary entirely.
        "audit": _opp(bank, 4, "2027 Internal Audit Summer Analyst"),
        # ...and one more, under the name the banks actually use.
        "controllers": _opp(bank, 5, "Controllers - 2027 Summer Analyst"),
        # A consulting role posted by a firm tagged only `consulting`.
        "consulting": _opp(consultancy, 6, "2027 Consulting Summer Associate"),
    }


# ---------------------------------------------------------------------------
# The rule, one case at a time.
# ---------------------------------------------------------------------------

def test_a_silent_title_still_counts_under_its_firms_track(client, board):
    """THE LOAD-BEARING CASE. Half the board states no function in its title.
    Those rows are legitimate matches for a firm-level track and must keep
    surfacing exactly as they did before — the change removes rows that make a
    DIFFERENT claim, it does not require rows to make a positive one.

    If this test ever fails by returning fewer rows, the facet has quietly
    become an allowlist and roughly half the inventory has left the board."""
    assert board["silent"].id in _ids(_get(client, track="ib"))
    assert board["silent"].id in _ids(_get(client, track="st"))


def test_a_role_naming_its_own_track_counts_under_that_track(client, board):
    assert board["ib"].id in _ids(_get(client, track="ib"))


def test_a_role_naming_a_different_track_leaves_this_one(client, board):
    """The bank covers both ib and st, so before this change its Sales &
    Trading programme counted as an investment banking role and its IB
    programme counted as a Sales & Trading one."""
    ib = _ids(_get(client, track="ib"))
    st = _ids(_get(client, track="st"))
    assert board["st"].id not in ib
    assert board["st"].id in st
    assert board["ib"].id not in st


@pytest.mark.parametrize("row", ["audit", "controllers"])
def test_an_explicitly_non_track_role_leaves_every_track(client, board, row):
    """Audit and Controllers are real work and not one of these tracks. 675 of
    the 2,723 live open campus rows say something in this class, and every one
    of them used to inherit its bank's coverage."""
    for track in ("ib", "st", "consulting", "am", "pe"):
        assert board[row].id not in _ids(_get(client, track=track)), track


def test_a_stated_track_wins_over_the_firms_tag(client, board):
    """The rule points both ways: a role that names its function answers to
    THAT function wherever it is posted. On the live board this is what took
    the hk+us consulting facet from 9 rows to 16 — consulting roles at firms
    not tagged `consulting`."""
    bank = board["bank"]
    row = _opp(bank, 7, "2027 Management Consulting Summer Analyst")
    assert row.id in _ids(_get(client, track="consulting"))
    # ...and it does NOT also answer to the bank's own coverage.
    assert row.id not in _ids(_get(client, track="ib"))


# ---------------------------------------------------------------------------
# The count promise. A facet number that disagreed with the list under it is
# the same class of lie as the wrong rows.
# ---------------------------------------------------------------------------

def test_the_facet_counts_are_what_the_filter_returns(client, board):
    """`_track_facet` and `_apply_track_filter` both go through `_row_tracks`
    so they cannot drift; this asserts it end to end, per option."""
    for option in _get(client).context["facets"]["tracks"]:
        resp = _get(client, track=option["value"])
        assert resp.context["total"] == option["count"], option["value"]


def test_the_counts_are_the_role_level_numbers(client, board):
    """Six rows. Before this change the bank's five all counted under both ib
    and st, giving ib=5 / st=5 / consulting=1."""
    assert _counts(_get(client)) == {
        "": 6,
        "ib": 2,        # the silent row + the IB programme
        "st": 2,        # the silent row + the S&T programme
        "consulting": 1,
    }


def test_a_selected_track_survives_being_crossed_to_zero(client, board):
    """Same posture as the Region facet: cross-filtering can take a live
    selection to zero, and dropping the option would leave the <select> with
    nothing selected — which the next htmx GET would serialize as some other
    track entirely."""
    resp = _get(client, track="pe")
    assert _counts(resp)["pe"] == 0
    assert 'value="pe" selected' in resp.content.decode()


# ---------------------------------------------------------------------------
# What this change deliberately does NOT touch.
# ---------------------------------------------------------------------------

def test_the_role_type_facet_is_untouched(client, board):
    """Track is the vertical; BUCKET (insight / internship / entry-level) is
    the Role Type control, a separate dimension that must not re-merge with
    it. The audit row is not an IB role and IS an internship, and the Role
    Type counts still say so."""
    _opp(board["bank"], 8, "2027 Spring Insight Week", bucket="insight")
    counts = {s["value"]: s["count"] for s in _get(client).context["role_segments"]}
    assert counts["internship"] == 6
    assert counts["insight"] == 1
    assert counts[""] == 7


def test_the_region_facet_is_untouched(client, board):
    _opp(board["bank"], 9, "2027 Internal Audit Summer Analyst", region="hk")
    counts = {o["value"]: o["count"] for o in _get(client).context["facets"]["regions"]}
    assert counts["us"] == 6
    assert counts["hk"] == 1


def test_no_track_selected_still_returns_the_whole_board(client, board):
    assert _get(client).context["total"] == 6
    assert _counts(_get(client))[""] == 6
