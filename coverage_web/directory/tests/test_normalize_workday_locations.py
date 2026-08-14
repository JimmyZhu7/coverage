"""normalize_workday_locations — the catch-up for Workday rows ingested before
the connector punctuated `locationsText`. Dry-run by default, no network, and
it can only ever write what the connector itself would have written."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from directory.models import Firm, Opportunity


def _opp(firm, location, **kw):
    return Opportunity.objects.create(
        firm=firm, title=kw.pop("title", "Summer Analyst"), status=kw.pop("status", "open"),
        url=kw.pop("url", f"https://citi.example/job/{location}"),
        source=kw.pop("source", "workday"), location=location, **kw,
    )


@pytest.mark.django_db
def test_default_run_reports_and_writes_nothing():
    firm = Firm.objects.create(slug="citi", name="Citi")
    opp = _opp(firm, "Hong Kong  Hong Kong")

    out = StringIO()
    call_command("normalize_workday_locations", stdout=out)

    opp.refresh_from_db()
    assert opp.location == "Hong Kong  Hong Kong", "the live DB is read-only by default"
    body = out.getvalue()
    assert "[dry-run]" in body
    assert "'Hong Kong  Hong Kong' -> 'Hong Kong, Hong Kong'" in body


@pytest.mark.django_db
def test_apply_writes_exactly_what_the_connector_would_have():
    firm = Firm.objects.create(slug="td", name="TD Securities")
    doubled = _opp(firm, "Singapore  Singapore")
    street = _opp(firm, "890 Herron Road, Montreal, Quebec", url="https://td.example/job/2")

    call_command("normalize_workday_locations", apply=True)

    doubled.refresh_from_db()
    street.refresh_from_db()
    assert doubled.location == "Singapore, Singapore"
    assert street.location == "Montreal, Quebec"


@pytest.mark.django_db
def test_rows_the_normalizer_would_not_change_are_left_alone():
    """The single-space run carries no slot boundary, so there is nothing to
    segment on and the command must not invent one. This is the row class the
    "duplicated city token" reading of the bug would have corrupted."""
    firm = Firm.objects.create(slug="citi", name="Citi")
    ny = _opp(firm, "New York New York United States")
    plain = _opp(firm, "Singapore", url="https://citi.example/job/plain")

    out = StringIO()
    call_command("normalize_workday_locations", stdout=out)

    ny.refresh_from_db()
    plain.refresh_from_db()
    assert ny.location == "New York New York United States"
    assert plain.location == "Singapore"
    assert "Nothing to normalize" in out.getvalue()


@pytest.mark.django_db
def test_other_boards_are_out_of_scope():
    """The empty-slot rule is a fact about Workday's own field. A sitemap or
    Greenhouse row that happens to hold two spaces means something else."""
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    other = _opp(firm, "Central  Hong Kong", source="sitemap")

    call_command("normalize_workday_locations", apply=True)

    other.refresh_from_db()
    assert other.location == "Central  Hong Kong"


@pytest.mark.django_db
def test_ids_narrows_the_run():
    firm = Firm.objects.create(slug="citi", name="Citi")
    a = _opp(firm, "Hong Kong  Hong Kong")
    b = _opp(firm, "London  United Kingdom", url="https://citi.example/job/b")

    call_command("normalize_workday_locations", apply=True, ids=str(a.id))

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.location == "Hong Kong, Hong Kong"
    assert b.location == "London  United Kingdom"
