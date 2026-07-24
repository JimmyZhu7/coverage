"""Unit tests for the ingest upsert / closed-detection / reopen logic.

No live network: `directory.ingest.fetch_many` is monkeypatched to return
crafted `FetchResult`s built from real connector `Opportunity` dataclasses, so
these exercise the DB upsert path exactly as a real scrape would drive it.
"""

from __future__ import annotations

from datetime import date

import pytest

from coverage_connectors import FetchResult, GreenhouseBoard
from coverage_connectors.models import Opportunity as ConnOpp

from directory import ingest
from directory.models import Firm, Opportunity, ScrapeRun

BOARD = GreenhouseBoard(firm="William Blair", token="williamblair")
U1 = "https://boards.greenhouse.io/williamblair/jobs/1"
U2 = "https://boards.greenhouse.io/williamblair/jobs/2"


def _opp(url, *, title="Summer Analyst", location="Chicago", deadline=None, firm="William Blair"):
    return ConnOpp(firm=firm, title=title, location=location, url=url, source="greenhouse", deadline=deadline)


def _result(opps, *, board=BOARD, ok=True, error=None):
    return FetchResult(board=board, ok=ok, opportunities=list(opps), raw_count=len(list(opps)), error=error)


def _patch(monkeypatch, results):
    monkeypatch.setattr(ingest, "fetch_many", lambda *a, **k: results)


@pytest.mark.django_db
def test_first_scrape_creates_rows_and_records_run(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.count() == 2
    assert run.status == "ok"
    assert run.stats["created"] == 2
    assert run.finished is not None
    assert ScrapeRun.objects.count() == 1
    o = Opportunity.objects.get(url=U1)
    assert o.status == "open"
    assert o.source == "greenhouse"
    assert o.content_hash != ""
    assert o.last_verified is not None and o.last_checked is not None
    # Ingest derives the role bucket from the title (default _opp title is
    # "Summer Analyst") — this is the classification seam the calendar's
    # role filter depends on.
    assert o.bucket == "internship"


@pytest.mark.django_db
def test_ingest_stamps_bucket_and_cohort(monkeypatch):
    _patch(
        monkeypatch,
        [_result([
            _opp(U1, title="2027 Summer Analyst Program"),
            _opp(U2, title="Vice President, Fund Finance"),
        ])],
    )
    ingest.ingest_boards([BOARD], label="greenhouse")

    campus_role = Opportunity.objects.get(url=U1)
    assert campus_role.bucket == "internship"
    assert campus_role.cohort == "2027"     # derived from the title, connector gave none

    experienced = Opportunity.objects.get(url=U2)
    assert experienced.bucket == "other"
    assert experienced.cohort == ""


@pytest.mark.django_db
def test_campus_board_promotes_neutral_titles(monkeypatch):
    # On a campus-scoped board (token says "students"), a plain Analyst
    # posting classifies as entry_level; on a general board it stays other.
    campus_board = GreenhouseBoard(firm="Solomon Partners", token="solomonpartnersstudentsgraduates")
    url = "https://boards.greenhouse.io/solomonpartners/jobs/9"
    _patch(monkeypatch, [_result(
        [_opp(url, title="Investment Banking Analyst", firm="Solomon Partners")],
        board=campus_board,
    )])
    ingest.ingest_boards([campus_board], label="greenhouse")
    assert Opportunity.objects.get(url=url).bucket == "entry_level"

    _patch(monkeypatch, [_result([_opp(U1, title="Investment Banking Analyst")])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.get(url=U1).bucket == "other"


@pytest.mark.django_db
def test_reclassify_backfills_existing_rows(monkeypatch):
    from django.core.management import call_command

    _patch(monkeypatch, [_result([_opp(U1, title="Graduate Analyst Programme 2026")])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    # Simulate a pre-classifier row: blank out the derived fields.
    Opportunity.objects.filter(url=U1).update(bucket="", cohort="")

    call_command("reclassify")
    o = Opportunity.objects.get(url=U1)
    assert o.bucket == "entry_level"
    assert o.cohort == "2026"


@pytest.mark.django_db
def test_rerun_is_idempotent_no_duplicates(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    first = Opportunity.objects.get(url=U1)
    first_seen, last_checked = first.first_seen, first.last_checked

    run2 = ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.count() == 2  # no duplication on the (firm, url) key
    first.refresh_from_db()
    assert first.first_seen == first_seen  # stamped once, never moves
    assert first.last_checked >= last_checked  # refreshed every run
    assert run2.stats["created"] == 0
    assert run2.stats["unchanged"] == 2
    assert run2.stats["closed"] == 0


@pytest.mark.django_db
def test_disappeared_posting_is_closed_then_reopened(monkeypatch):
    both = [_result([_opp(U1), _opp(U2)])]
    _patch(monkeypatch, both)
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([_opp(U1)])])  # U2 no longer returned
    run = ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.get(url=U2).status == "closed"
    assert Opportunity.objects.get(url=U1).status == "open"
    assert run.stats["closed"] == 1

    _patch(monkeypatch, both)  # U2 reappears
    run3 = ingest.ingest_boards([BOARD], label="greenhouse")
    assert Opportunity.objects.get(url=U2).status == "open"
    assert run3.stats["reopened"] == 1
    assert run3.stats["closed"] == 0


@pytest.mark.django_db
def test_failed_board_never_closes_its_postings(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([], ok=False, error="HTTP 503 from boards-api")])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.get(url=U1).status == "open"
    assert Opportunity.objects.get(url=U2).status == "open"
    assert run.stats["closed"] == 0
    assert run.status == "error"  # the only board failed
    assert "HTTP 503" in run.error


@pytest.mark.django_db
def test_empty_but_successful_board_closes_all(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([])])  # live board, lists nothing now
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.filter(status="closed").count() == 2
    assert run.stats["closed"] == 2


@pytest.mark.django_db
def test_content_change_updates_in_place_and_bumps_hash(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1, title="Summer Analyst")])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    h1 = Opportunity.objects.get(url=U1).content_hash

    _patch(monkeypatch, [_result([_opp(U1, title="Summer Analyst — IBD")])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    o = Opportunity.objects.get(url=U1)
    assert o.title == "Summer Analyst — IBD"
    assert o.content_hash != h1
    assert run.stats["updated"] == 1
    assert run.stats["unchanged"] == 0
    assert Opportunity.objects.count() == 1


@pytest.mark.django_db
def test_firm_resolved_to_existing_row_not_forked(monkeypatch):
    firm = Firm.objects.create(slug="williamblair", name="William Blair")
    _patch(monkeypatch, [_result([_opp(U1, deadline="2027-01-15")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    assert Firm.objects.count() == 1  # matched by name, not duplicated
    o = Opportunity.objects.get(url=U1)
    assert o.firm_id == firm.id
    assert o.deadline == date(2027, 1, 15)
    assert o.deadline_precision == "day"


@pytest.mark.django_db
def test_null_deadline_stored_as_null(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1, deadline=None)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    o = Opportunity.objects.get(url=U1)
    assert o.deadline is None
    assert o.deadline_precision == ""


@pytest.mark.django_db
def test_unseeded_firm_is_autocreated(monkeypatch):
    _patch(monkeypatch, [_result([_opp(U1)])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")
    assert Firm.objects.filter(name="William Blair").exists()
    assert run.stats["created_firms"] == ["william-blair"]
