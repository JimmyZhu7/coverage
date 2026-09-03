"""repair_blanked_regions — the catch-up for the rows a silent re-scrape
blanked (see `directory.ingest`'s location block and the command's own
docstring). Dry-run by default, no network, and it may only ever write a
region something in the database can be shown to have stated."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from directory.models import Firm, Opportunity, OpportunityChange


def _firm(slug="raymondjames", name="Raymond James", regions=None):
    return Firm.objects.create(slug=slug, name=name, regions=regions or [])


def _opp(firm, **kw):
    return Opportunity.objects.create(
        firm=firm,
        title=kw.pop("title", "2027 Equity Research Associate"),
        status=kw.pop("status", "open"),
        url=kw.pop("url", "https://raymondjames.wd1.myworkdayjobs.com/x/job/1"),
        source=kw.pop("source", "workday"),
        location=kw.pop("location", ""),
        region=kw.pop("region", ""),
        raw=kw.pop("raw", {}),
        **kw,
    )


def _run(**opts):
    out = StringIO()
    call_command("repair_blanked_regions", stdout=out, **opts)
    return out.getvalue()


@pytest.mark.django_db
def test_the_default_run_reports_and_writes_nothing():
    """Shared directory data: the command has to be asked twice."""
    firm = _firm()
    opp = _opp(firm, location="Saint Petersburg Florida, United States")

    body = _run()

    opp.refresh_from_db()
    assert opp.region == "", "a dry run may not touch the row"
    assert "[dry-run]" in body
    assert not OpportunityChange.objects.exists()


@pytest.mark.django_db
def test_a_workday_path_places_a_row_whose_location_was_blanked_too():
    """The Raymond James shape: BOTH columns were wiped, so the only thing
    left on the row is the provider's own payload — whose `externalPath`
    still starts with the posting's city."""
    firm = _firm()
    opp = _opp(firm, raw={
        "title": "2027 Equity Research Associate",
        "externalPath": "/job/Saint-Petersburg-Florida---United-States/"
                        "XMLNAME-2027-Equity-Research-Associate_R-0012398",
    })

    body = _run(apply=True)

    opp.refresh_from_db()
    assert opp.region == "us"
    assert "payload" in body


@pytest.mark.django_db
def test_the_rows_own_surviving_location_is_read_first():
    firm = _firm()
    opp = _opp(firm, location="London, United Kingdom")

    _run(apply=True)

    opp.refresh_from_db()
    assert opp.region == "eu"


@pytest.mark.django_db
def test_the_change_log_outranks_everything_below_it():
    """Our own record of what the row held beats any re-derivation: a posting
    logged as `hk` stays `hk`, even where a weaker rung would have said
    otherwise."""
    firm = _firm(regions=["us"])
    opp = _opp(firm)
    OpportunityChange.objects.create(
        opportunity=opp, field="region", old_value="hk", new_value="",
        stage=OpportunityChange.STAGE_SCRAPE, observed_at=timezone.now())

    _run(apply=True)

    opp.refresh_from_db()
    assert opp.region == "hk"


@pytest.mark.django_db
def test_a_sibling_at_the_same_address_places_the_row():
    firm = _firm(slug="barclays", name="Barclays")
    _opp(firm, location="Birmingham, One Snow Hill", region="eu",
         url="https://barclays.example/job/sibling")
    opp = _opp(firm, location="Birmingham, One Snow Hill",
               url="https://barclays.example/job/blanked")

    _run(apply=True)

    opp.refresh_from_db()
    assert opp.region == "eu"


@pytest.mark.django_db
def test_disagreeing_siblings_answer_nothing():
    """Workday files multi-city roles under one placeholder string, and the
    same string sits in three markets. Two answers is not an answer."""
    firm = _firm(slug="td", name="TD Securities")
    _opp(firm, location="2 Locations", region="us", url="https://td.example/job/a")
    _opp(firm, location="2 Locations", region="eu", url="https://td.example/job/b")
    opp = _opp(firm, location="2 Locations", url="https://td.example/job/c")

    body = _run(apply=True)

    opp.refresh_from_db()
    assert opp.region == ""
    assert "unrecoverable" in body


@pytest.mark.django_db
def test_a_single_market_firm_places_its_own_rows():
    firm = _firm(regions=["us"])
    _opp(firm, location="New York, NY", region="us",
         url="https://raymondjames.example/job/sibling")
    opp = _opp(firm, url="https://raymondjames.example/job/blanked")

    _run(apply=True)

    opp.refresh_from_db()
    assert opp.region == "us"


@pytest.mark.django_db
def test_a_firm_in_two_markets_places_nothing():
    """The rung is "this firm recruits in exactly one place". A firm with two
    has told us nothing about which one THIS row is in."""
    firm = _firm(slug="hsbc", name="HSBC", regions=["hk", "eu"])
    opp = _opp(firm, url="https://hsbc.example/job/blanked")

    _run(apply=True)

    opp.refresh_from_db()
    assert opp.region == ""


@pytest.mark.django_db
def test_a_posted_market_contradicting_the_firms_own_field_stops_the_guess():
    """`Firm.regions` is curated and can be stale. A row of that firm sitting
    in a market the field does not list is the field being wrong, and a wrong
    field may not place anything."""
    firm = _firm(regions=["us"])
    _opp(firm, location="Hong Kong", region="hk",
         url="https://raymondjames.example/job/hk")
    opp = _opp(firm, url="https://raymondjames.example/job/blanked")

    _run(apply=True)

    opp.refresh_from_db()
    assert opp.region == ""


@pytest.mark.django_db
def test_a_place_outside_the_tracked_markets_is_filed_other_not_tracked():
    """REWRITTEN 2026-09-02, and the rewrite is the point of `other`.

    This test used to pin `region == ""` for "Regina, Saskatchewan" on the
    reasoning that blank is the honest reading of a place Coverage does not
    track. That conflates two different answers, which is exactly what
    `REGION_LABELS`' own comment says it must not: blank means the posting
    never said where it is, `other` means it said and the answer is outside
    the six tracked markets. Saskatchewan is the second case. The rule that
    matters — that a stated location outside the tracked markets may never
    be promoted INTO one — is what is asserted here now, and it still holds.

    (The example only stopped answering blank because the spelled-out
    Canadian provinces became keys on 2026-09-02; the eleven US-state names
    added in the same pass are why 238 open rows stopped being placeless.)"""
    firm = _firm(slug="td", name="TD Securities", regions=[])
    opp = _opp(firm, location="Regina, Saskatchewan",
               url="https://td.example/job/regina")

    _run(apply=True)

    opp.refresh_from_db()
    assert opp.region == "other"
    assert opp.region not in ("us", "hk", "sg", "eu", "cn", "jp")


@pytest.mark.django_db
def test_a_location_that_names_nowhere_stays_blank():
    """The half of the old assertion that was really about silence. A
    building name is not a place, so nothing in the database can be shown to
    have stated a market and the command must leave the row alone."""
    firm = _firm(slug="dbs", name="DBS", regions=[])
    opp = _opp(firm, location="Technology Centre",
               url="https://dbs.example/job/centre")

    body = _run(apply=True)

    opp.refresh_from_db()
    assert opp.region == ""
    assert "left blank for want of any" in body


@pytest.mark.django_db
def test_closed_rows_are_out_of_scope():
    """The feed shows open roles. A closed row is history, and rewriting
    history is a bigger claim than this command is making."""
    firm = _firm()
    opp = _opp(firm, status="closed", location="London, United Kingdom",
               url="https://raymondjames.example/job/closed")

    _run(apply=True)

    opp.refresh_from_db()
    assert opp.region == ""


@pytest.mark.django_db
def test_every_repair_names_its_evidence_in_the_change_log():
    """The damage was invisible because nothing wrote it down. The repair
    does not get to be invisible too."""
    firm = _firm()
    opp = _opp(firm, location="Saint Petersburg Florida, United States")

    _run(apply=True)

    row = OpportunityChange.objects.get(opportunity=opp, field="region")
    assert (row.old_value, row.new_value) == ("", "us")
    assert row.stage == OpportunityChange.STAGE_REPAIR
    assert "own_location" in row.note


@pytest.mark.django_db
def test_the_firm_filter_scopes_the_run():
    rj = _firm()
    other = _firm(slug="td", name="TD Securities")
    mine = _opp(rj, location="New York, NY")
    theirs = _opp(other, location="New York, NY", url="https://td.example/job/1")

    _run(apply=True, firm="raymondjames")

    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.region == "us"
    assert theirs.region == ""


@pytest.mark.django_db
def test_a_second_apply_changes_nothing():
    """Idempotent: the first pass leaves no blank row it could act on again,
    so the second writes no second change row."""
    firm = _firm()
    _opp(firm, location="Hong Kong")

    _run(apply=True)
    _run(apply=True)

    assert OpportunityChange.objects.filter(field="region").count() == 1
