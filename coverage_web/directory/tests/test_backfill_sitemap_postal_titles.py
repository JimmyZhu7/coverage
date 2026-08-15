"""backfill_sitemap_postal_titles — dry-run by default, re-runs the sitemap
connector's postal-code split against titles already stored in the DB and
reports (or, with --apply, writes) the corrected title/location."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from io import StringIO

from directory.models import Firm, Opportunity


@pytest.mark.django_db
def test_default_run_reports_and_writes_nothing():
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    opp = Opportunity.objects.create(
        firm=firm, title="New York Investment Banking Graduate NY 10001",
        status="open", url="https://hsbc.example/job/1", location="",
        source="sitemap",
    )
    out = StringIO()
    call_command("backfill_sitemap_postal_titles", stdout=out)

    opp.refresh_from_db()
    assert opp.title == "New York Investment Banking Graduate NY 10001"  # untouched
    assert opp.location == ""
    assert "NY 10001" in out.getvalue()
    assert "[dry-run]" in out.getvalue()


@pytest.mark.django_db
def test_apply_writes_the_split_title_and_location():
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    opp = Opportunity.objects.create(
        firm=firm, title="New York Investment Banking Graduate NY 10001",
        status="open", url="https://hsbc.example/job/1", location="",
        source="sitemap",
    )
    call_command("backfill_sitemap_postal_titles", apply=True)

    opp.refresh_from_db()
    assert opp.title == "New York Investment Banking Graduate"
    assert opp.location == "NY 10001"


@pytest.mark.django_db
def test_rows_with_an_existing_location_are_left_alone():
    """A row a later re-fetch (or a hand fix) already gave a real location
    must not be overwritten by a guess derived from its old title text."""
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    opp = Opportunity.objects.create(
        firm=firm, title="New York Investment Banking Graduate NY 10001",
        status="open", url="https://hsbc.example/job/1",
        location="New York, NY", source="sitemap",
    )
    call_command("backfill_sitemap_postal_titles", apply=True)

    opp.refresh_from_db()
    assert opp.title == "New York Investment Banking Graduate NY 10001"
    assert opp.location == "New York, NY"


@pytest.mark.django_db
def test_titles_with_no_recognizable_postal_code_are_left_alone():
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    opp = Opportunity.objects.create(
        firm=firm, title="Central Investment Banking Graduate Hong",
        status="open", url="https://hsbc.example/job/2", location="",
        source="sitemap",
    )
    call_command("backfill_sitemap_postal_titles", apply=True)

    opp.refresh_from_db()
    assert opp.title == "Central Investment Banking Graduate Hong"
    assert opp.location == ""


@pytest.mark.django_db
def test_non_sitemap_rows_are_untouched():
    """The split is only trustworthy for slug-derived titles; a board that
    hands over a real title must never have it mistaken for one."""
    firm = Firm.objects.create(slug="acme", name="Acme")
    opp = Opportunity.objects.create(
        firm=firm, title="Analyst Intern NY 10001", status="open",
        url="https://acme.example/job/1", location="", source="workday",
    )
    call_command("backfill_sitemap_postal_titles", apply=True)

    opp.refresh_from_db()
    assert opp.title == "Analyst Intern NY 10001"
    assert opp.location == ""


@pytest.mark.django_db
def test_ids_argument_scopes_the_run():
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    a = Opportunity.objects.create(
        firm=firm, title="New York Investment Banking Graduate NY 10001",
        status="open", url="https://hsbc.example/job/1", location="",
        source="sitemap",
    )
    b = Opportunity.objects.create(
        firm=firm, title="London Corporate Banking Graduate E14 5HQ",
        status="open", url="https://hsbc.example/job/2", location="",
        source="sitemap",
    )
    call_command("backfill_sitemap_postal_titles", apply=True, ids=str(a.id))

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.location == "NY 10001"
    assert b.location == ""  # out of scope, untouched
