"""The Opportunities feed's Year filter (?year=).

The filter narrows on the PROGRAMME/intake year (`Opportunity.cohort`) and
also honours an explicitly stated graduation year (`Opportunity.class_year`)
where a posting has one. Two things these tests exist to pin down:

  1. The "no year stated" rows are reachable and countable. They are ~89% of
     the live open set, so an option that silently drops them would hide most
     of the feed behind a control that looks like a mild narrowing.
  2. The year facet's counts match what the filter actually returns, under
     whatever other filters are active. A count that disagrees with the
     resulting page is the kind of small lie the whole feed is built to avoid.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from directory.models import Firm, Opportunity


def _firm(slug="evercore", name="Evercore", **kw):
    return Firm.objects.create(slug=slug, name=name, **kw)


def _opp(firm, url, title="Summer Analyst", *, cohort="", class_year="",
         region="us", bucket="internship"):
    return Opportunity.objects.create(
        firm=firm, url=url, title=title, bucket=bucket, status="open",
        cohort=cohort, class_year=class_year, region=region,
    )


@pytest.fixture
def feed(db):
    """A small stand-in for the live shape: a few dated cohorts, one stated
    class year, and a majority that state no year at all."""
    f = _firm()
    other = _firm(slug="lazard", name="Lazard")
    _opp(f, "https://x/1", "2027 Summer Analyst Program", cohort="2027")
    _opp(f, "https://x/2", "2027 Off-Cycle Internship", cohort="2027", region="hk")
    _opp(f, "https://x/3", "2026 Summer Analyst Program", cohort="2026")
    # States a class year outright and carries no programme year.
    _opp(other, "https://x/4", "Class of 2028 Investment Analyst", class_year="2028")
    # No year of any kind — the majority case.
    _opp(other, "https://x/5", "Investment Banking Summer Analyst")
    _opp(other, "https://x/6", "Off-Cycle Internship, Hong Kong", region="hk")
    return f


def _get(client, **params):
    return client.get(reverse("opportunities"), params)


def _titles(resp):
    return {r["title"] for c in resp.context["clusters"] for r in c["roles"]}


def _facet(resp):
    return {y["value"]: y["count"] for y in resp.context["year_facet"]}


@pytest.mark.django_db
def test_unfiltered_feed_shows_everything(client, feed):
    resp = _get(client)
    assert resp.status_code == 200
    assert resp.context["total"] == 6
    # And the facet accounts for every row: 4 stated a year, 2 did not.
    assert _facet(resp) == {"": 6, "2028": 1, "2027": 2, "2026": 1, "none": 2}


@pytest.mark.django_db
def test_year_narrows_to_that_cohort(client, feed):
    resp = _get(client, year="2027")
    assert resp.context["total"] == 2
    assert _titles(resp) == {"2027 Summer Analyst Program", "2027 Off-Cycle Internship"}


@pytest.mark.django_db
def test_stated_class_year_is_matched_too(client, feed):
    """For the rare posting that names a class year, that year IS the truthful
    answer to "which year is this for?" — so it must be selectable."""
    resp = _get(client, year="2028")
    assert resp.context["total"] == 1
    assert _titles(resp) == {"Class of 2028 Investment Analyst"}


@pytest.mark.django_db
def test_no_year_stated_is_selectable_and_counted(client, feed):
    """The 'none' option is the guard against silently hiding most of the feed:
    the rows that state nothing must be both visible and countable."""
    resp = _get(client, year="none")
    assert resp.context["total"] == 2
    assert _titles(resp) == {
        "Investment Banking Summer Analyst",
        "Off-Cycle Internship, Hong Kong",
    }
    # Every 'none' row genuinely states neither year.
    for cl in resp.context["clusters"]:
        for r in cl["roles"]:
            assert not r["cohort"] and not r["class_year"]


@pytest.mark.django_db
def test_year_counts_match_what_the_filter_returns(client, feed):
    """Each option's count must equal the total you get after selecting it —
    otherwise the number in the dropdown is decoration."""
    for value, count in _facet(_get(client)).items():
        assert _get(client, year=value).context["total"] == count, value


@pytest.mark.django_db
def test_year_composes_with_region(client, feed):
    """Both filters apply; neither quietly resets the other."""
    resp = _get(client, year="2027", region="hk")
    assert resp.context["total"] == 1
    assert _titles(resp) == {"2027 Off-Cycle Internship"}
    assert resp.context["selected"]["year"] == "2027"
    assert resp.context["selected"]["region"] == "hk"
    # And the year facet is counted against the region filter, so its numbers
    # answer "under my current filters", not "in the whole table".
    assert _facet(resp) == {"": 2, "2027": 1, "none": 1}


@pytest.mark.django_db
def test_querystring_round_trip(client, feed):
    """The selection survives into the rendered control and into `has_filters`,
    so the htmx push-url state and a plain reload agree."""
    resp = _get(client, year="2027", region="hk")
    assert resp.context["has_filters"] is True
    html = resp.content.decode()
    assert '<option value="2027" selected>2027 (1)</option>' in html
    # The control is a normal GET input named `year`, which is what makes the
    # htmx hx-push-url state reloadable without JS.
    assert 'name="year"' in html
    # A bare `none` selection round-trips the same way.
    html = _get(client, year="none").content.decode()
    assert '<option value="none" selected>No Year Stated (2)</option>' in html


@pytest.mark.django_db
def test_unknown_year_value_is_a_no_op_not_an_empty_page(client, feed):
    """A hand-typed or stale querystring degrades to the unfiltered feed rather
    than an empty one, matching the role filter's fallthrough."""
    assert _get(client, year="banana").context["total"] == 6


@pytest.mark.django_db
def test_year_filter_does_not_imply_a_class_year(client, feed):
    """The label and the data must agree: selecting 2027 returns programme-year
    rows, and none of them acquire a 'Class of' chip they never stated."""
    resp = _get(client, year="2027")
    for cl in resp.context["clusters"]:
        for r in cl["roles"]:
            assert r["cohort"] == "2027"
            assert r["class_year"] == ""
    # No class-year mark is rendered for any of them. (Matching the element
    # rather than the bare words: the inline stylesheet's own comment
    # legitimately contains "Class of".)
    assert 'class="rr-cls"' not in resp.content.decode()
    # …while the row that DID state one still gets its mark.
    assert 'class="rr-cls"' in _get(client, year="2028").content.decode()

    # The control itself is labelled for the programme year, never "Class".
    # (The old explainer sentence under the control — "Intake year in the
    # posting. Not a graduation year." — was removed at the founder's
    # request 2026-08-15; the label alone carries the distinction now.)
    html = _get(client).content.decode()
    assert "Programme Year" in html


# ---------------------------------------------------------------------------
# The convention-derived class year in the facet, and the one thing it is
# never allowed to do.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_derived_class_year_is_filterable_and_counted(client, db):
    f = _firm(slug="jpm", name="JPMorgan")
    o = Opportunity.objects.create(
        firm=f, url="https://x/1", title="2027 Markets Summer Analyst",
        bucket="internship", status="open", cohort="2027",
        class_year_derived="2028", region="us")
    # It answers to its own graduation year AND to its programme year: both
    # are years this posting is about.
    for year in ("2027", "2028"):
        r = client.get(reverse("opportunities"), {"year": year})
        assert o.title in r.content.decode(), year
    # And it is no longer part of the silent set.
    r = client.get(reverse("opportunities"), {"year": "none"})
    assert o.title not in r.content.decode()


@pytest.mark.django_db
def test_a_derived_year_never_blocks_a_student():
    """A guess may open a door; it may not close one. A summer 2027
    internship conventionally hires the 2028 class — worth surfacing to a
    2028 student, and NOT grounds for telling a 2029 student it isn't
    theirs, because the posting never said so."""
    from directory.views import _eligibility
    f = _firm(slug="ms", name="Morgan Stanley")
    o = Opportunity.objects.create(
        firm=f, url="https://x/2", title="2027 Summer Analyst",
        bucket="internship", status="open", cohort="2027",
        class_year_derived="2028", region="us")

    match = _eligibility(o, {"class_year": 2028, "work_auth": {}})
    assert match["kind"] == "year_likely"
    assert match["blocking"] is False
    assert "Likely" in match["label"]
    assert "inferred" in match["why"]

    # The mismatch is silence, not a blocking verdict.
    assert _eligibility(o, {"class_year": 2029, "work_auth": {}}) is None


@pytest.mark.django_db
def test_a_stated_year_always_outranks_a_derived_one():
    """Where the posting states a window, that window decides — including
    when it disagrees with the convention."""
    from directory.views import _eligibility
    f = _firm(slug="gs", name="Goldman Sachs")
    o = Opportunity.objects.create(
        firm=f, url="https://x/3", title="2027 Summer Analyst",
        bucket="internship", status="open", cohort="2027",
        class_year_derived="2028", region="us",
        raw={"facts": {"grad": {"value": "2029", "years": ["2029"],
                                "phrase": "graduating in 2029"}}})
    v = _eligibility(o, {"class_year": 2029, "work_auth": {}})
    assert v["kind"] == "year_ok"
    assert v["why"] == "graduating in 2029"


# --------------------------------------------------------------------------- #
# All five sources, read through ONE JSONB extraction (2026-09-01)
#
# `_year_facet` used to select `raw__facts__grad__years` and
# `raw__facts__start__years` as two separate JSON paths over one column, which
# is two Postgres detoasts of a TOASTed `raw` per row: 76 ms over the founder's
# 2,723 campus rows and 100 ms over all 16,029 open ones, against 52/72 ms for
# the single `raw__facts` read it does now. The counting is Python either way.
#
# The risk of moving the navigation from SQL into Python is silently losing a
# source, and it would be invisible — a facet that under-counts still renders.
# So this fixture states every one of the five sources the facet reads, and
# one row that states two of them at once, and pins the numbers exactly.
# --------------------------------------------------------------------------- #

@pytest.fixture
def five_sources(db):
    """One row per source the facet reads, plus one row carrying two."""
    f = _firm(slug="five-sources", name="Five Sources")
    _opp(f, "https://y/1", "2027 Summer Analyst", cohort="2027")
    _opp(f, "https://y/2", "Class of 2028 Analyst", class_year="2028")
    o3 = _opp(f, "https://y/3", "2026 Summer Analyst")
    o3.class_year_derived = "2026"
    o3.save(update_fields=["class_year_derived"])
    # Body-stated graduation window, the source `refresh_grad_facts` repairs.
    o4 = _opp(f, "https://y/4", "Analyst, silent title")
    o4.raw = {"facts": {"grad": {"value": "2029", "years": ["2029"],
                                 "phrase": "graduating in 2029"}}}
    o4.save(update_fields=["raw"])
    # Body-stated programme START year — the twin the title never carries.
    o5 = _opp(f, "https://y/5", "Analyst, silent title too")
    o5.raw = {"facts": {"start": {"value": "2030", "years": ["2030"],
                                  "phrase": "Start Date: Jun 2030"}}}
    o5.save(update_fields=["raw"])
    # BOTH body windows on one row, and a cohort as well: it belongs under
    # three years and must be counted once under each.
    o6 = _opp(f, "https://y/6", "2031 Summer Analyst", cohort="2031")
    o6.raw = {"facts": {"grad": {"value": "2032", "years": ["2032"],
                                 "phrase": "graduating in 2032"},
                        "start": {"value": "2033", "years": ["2033"],
                                  "phrase": "starts 2033"}}}
    o6.save(update_fields=["raw"])
    # States nothing at all: the "No Year Stated" bucket.
    _opp(f, "https://y/7", "Investment Banking Summer Analyst")
    return f


@pytest.mark.django_db
def test_every_year_source_still_reaches_the_facet(client, five_sources):
    """The exact facet, source by source. `2031`/`2032`/`2033` all come off
    the same row, which is why the counts sum past the total — see the
    docstring on `_year_facet`."""
    facet = _facet(_get(client))
    assert facet == {
        "": 7,            # every row
        "2033": 1,        # raw.facts.start.years
        "2032": 1,        # raw.facts.grad.years
        "2031": 1,        # cohort, on the same row as the two above
        "2030": 1,        # raw.facts.start.years, alone
        "2029": 1,        # raw.facts.grad.years, alone
        "2028": 1,        # class_year
        "2027": 1,        # cohort
        "2026": 1,        # class_year_derived
        "none": 1,        # states nothing
    }


@pytest.mark.django_db
def test_a_row_stating_both_body_windows_is_counted_under_both(
        client, five_sources):
    """The regression the single read could have caused: navigating to
    `facts["grad"]` and stopping there, or reading `facts` as a flat dict,
    would drop the start year and this row would count under two years
    instead of three."""
    facet = _facet(_get(client))
    for year in ("2031", "2032", "2033"):
        assert facet[year] == 1, year
        assert _get(client, year=year).context["total"] == 1, year


@pytest.mark.django_db
def test_the_facet_reads_raw_once_per_row(five_sources):
    """One JSON extraction in the SELECT, not two.

    Pinned on the compiled SQL rather than on a clock, because what this
    change removes is a second Postgres detoast of the same TOASTed column
    and nothing about the facet's OUTPUT can see it — the previous
    implementation returned exactly the numbers the tests above assert. A
    future edit that goes back to selecting `grad` and `start` as separate
    paths would be invisible except here."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from directory.models import Opportunity
    from directory.views import _year_facet

    with CaptureQueriesContext(connection) as captured:
        _year_facet(Opportunity.objects.filter(status="open"), "")
    sql = " ".join(q["sql"] for q in captured.captured_queries)
    assert sql.count('"raw"') == 1, sql
