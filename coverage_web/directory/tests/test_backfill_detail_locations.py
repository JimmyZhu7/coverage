"""backfill_detail_locations — dry-run by default, reads raw["detail_location"]
already stored on rows and reports (or, with --apply, writes) the recovered
location for any row still showing Workday's aggregate "N Locations" text."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from io import StringIO

from directory.models import Firm, Opportunity


@pytest.mark.django_db
def test_default_run_reports_and_writes_nothing():
    firm = Firm.objects.create(slug="td", name="TD Securities")
    opp = Opportunity.objects.create(
        firm=firm, title="Personal Banking Associate Trainee", status="open",
        url="https://td.example/job/1", location="2 Locations",
        raw={"detail_location": "Markham, Ontario, Canada; Scarborough, Ontario, Canada"},
    )
    out = StringIO()
    call_command("backfill_detail_locations", stdout=out)

    opp.refresh_from_db()
    assert opp.location == "2 Locations"  # untouched
    assert "Markham" in out.getvalue()
    assert "[dry-run]" in out.getvalue()


@pytest.mark.django_db
def test_apply_writes_the_recovered_location():
    firm = Firm.objects.create(slug="td", name="TD Securities")
    opp = Opportunity.objects.create(
        firm=firm, title="Personal Banking Associate Trainee", status="open",
        url="https://td.example/job/1", location="2 Locations",
        raw={"detail_location": "Markham, Ontario, Canada; Scarborough, Ontario, Canada"},
    )
    call_command("backfill_detail_locations", apply=True)

    opp.refresh_from_db()
    assert opp.location == "Markham, Ontario, Canada; Scarborough, Ontario, Canada"


@pytest.mark.django_db
def test_rows_without_a_placeholder_location_are_skipped():
    firm = Firm.objects.create(slug="acme", name="Acme")
    opp = Opportunity.objects.create(
        firm=firm, title="Analyst", status="open",
        url="https://acme.example/job/1", location="London, United Kingdom",
        raw={"detail_location": "Somewhere Else"},
    )
    call_command("backfill_detail_locations", apply=True)

    opp.refresh_from_db()
    assert opp.location == "London, United Kingdom"


@pytest.mark.django_db
def test_rows_without_detail_location_are_skipped():
    firm = Firm.objects.create(slug="acme", name="Acme")
    opp = Opportunity.objects.create(
        firm=firm, title="Analyst", status="open",
        url="https://acme.example/job/1", location="2 Locations", raw={},
    )
    out = StringIO()
    call_command("backfill_detail_locations", apply=True, stdout=out)

    opp.refresh_from_db()
    assert opp.location == "2 Locations"
    assert "Nothing to backfill" in out.getvalue()
