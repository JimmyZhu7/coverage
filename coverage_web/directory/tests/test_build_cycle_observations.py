"""Tests for `build_cycle_observations` — the Phase 2 rebuild of
`FirmCycleObservation` from `OpportunityChange` + `ScrapeRun`.

Covers: onboarding-day exclusion (a firm's first scrape batch is backlog,
not an observed open), suspect closes staying out of `closed_count` while
still being counted, idempotent recompute, and the reposting question this
feature was told to investigate (see `test_a_relist_under_a_preserved_talnet_id_is_a_reopen_not_a_double_count`).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from coverage_connectors import FetchResult, TalnetBoard
from coverage_connectors.models import Opportunity as ConnOpp

from directory import ingest
from directory.models import Firm, FirmCycleObservation, Opportunity, OpportunityChange, ScrapeRun


def _firm(slug="testbank", name="Test Bank"):
    return Firm.objects.create(slug=slug, name=name, status="active")


def _opp(firm, n, *, bucket="internship", region="us", source="greenhouse",
         status="open", first_seen):
    o = Opportunity.objects.create(
        firm=firm, title=f"Summer Analyst {n}", bucket=bucket, region=region,
        source=source, status=status,
        url=f"https://boards.greenhouse.io/{firm.slug}/jobs/{n}",
    )
    Opportunity.objects.filter(pk=o.pk).update(first_seen=first_seen)
    o.refresh_from_db()
    return o


def _close_event(opp, *, at):
    return OpportunityChange.objects.create(
        opportunity=opp, field="status", old_value="open", new_value="closed",
        stage=OpportunityChange.STAGE_SCRAPE_CLOSE, observed_at=at,
    )


def _healthy_run(firm, *, at):
    return ScrapeRun.objects.create(
        connector="all", started=at - timedelta(minutes=5), finished=at + timedelta(minutes=1),
        status="ok",
        stats={"boards_total": 1, "boards_ok": 1, "boards_failed": 0,
               "firms_touched": [firm.slug], "errors": []},
    )


def _failed_run(firm, *, at, provider="greenhouse"):
    return ScrapeRun.objects.create(
        connector="all", started=at - timedelta(minutes=5), finished=at + timedelta(minutes=1),
        status="partial",
        stats={"boards_total": 1, "boards_ok": 0, "boards_failed": 1,
               "firms_touched": [firm.slug],
               "errors": [{"firm": firm.name, "provider": provider, "error": "SSL error"}]},
    )


def _observation(firm, region):
    return FirmCycleObservation.objects.get(firm=firm, region=region)


@pytest.mark.django_db
def test_onboarding_day_postings_are_not_counted_as_observed_opens():
    """Every posting first seen the day a firm's board was first fetched is
    backlog, not evidence that a cycle opened that day — the whole catalog
    lands on the same `first_seen` regardless of when any individual role
    actually went live."""
    firm = _firm()
    onboarding = timezone.now() - timedelta(days=20)
    for n in range(5):
        _opp(firm, n, first_seen=onboarding)
    # One genuinely new posting, days later.
    _opp(firm, 99, first_seen=onboarding + timedelta(days=3))

    call_command("build_cycle_observations")

    row = _observation(firm, "us")
    assert row.opened_count == 1
    assert row.open_window_first == row.open_window_last == (onboarding + timedelta(days=3)).date()
    assert row.onboarded_at == onboarding.date()
    # The onboarding batch still counts toward the live snapshot — it just
    # isn't claimed as an OBSERVED open.
    assert row.currently_open_count == 6


@pytest.mark.django_db
def test_a_firm_with_only_onboarding_backlog_gets_an_honest_zero_not_no_row():
    """No post-onboarding movement at all is a real, measured state ("we
    watched, nothing has moved yet") and is different from never having
    looked — the row should exist with onboarded_at set and both counts at
    their honest zero, not be silently omitted."""
    firm = _firm()
    onboarding = timezone.now() - timedelta(days=1)
    _opp(firm, 0, first_seen=onboarding)

    call_command("build_cycle_observations")

    row = _observation(firm, "us")
    assert row.opened_count == 0
    assert row.closed_count == 0
    assert row.open_window_first is None
    assert row.onboarded_at == onboarding.date()


@pytest.mark.django_db
def test_suspect_closes_are_excluded_from_closed_count_but_still_counted():
    """A close attributed to a failed board must not shrink the close
    window's credibility by pretending it never happened — it has to show up
    in `excluded_suspect_closes` so a reader can tell a thin window from a
    filtered one."""
    firm = _firm()
    onboarding = timezone.now() - timedelta(days=10)
    good = _opp(firm, 0, first_seen=onboarding, status="closed")
    bad = _opp(firm, 1, first_seen=onboarding, status="closed")

    trusted_at = timezone.now() - timedelta(days=2)
    _healthy_run(firm, at=trusted_at)
    _close_event(good, at=trusted_at)

    suspect_at = timezone.now() - timedelta(days=1)
    _failed_run(firm, at=suspect_at)
    _close_event(bad, at=suspect_at)

    call_command("build_cycle_observations")

    row = _observation(firm, "us")
    assert row.closed_count == 1
    assert row.close_window_first == row.close_window_last == trusted_at.date()
    assert row.excluded_suspect_closes == 1


@pytest.mark.django_db
def test_rebuild_is_idempotent():
    firm = _firm()
    onboarding = timezone.now() - timedelta(days=10)
    opp = _opp(firm, 0, first_seen=onboarding + timedelta(days=1))
    _healthy_run(firm, at=timezone.now())
    _opp(firm, 1, first_seen=onboarding)  # onboarding-day sibling

    # `id`/`computed_at` are expected to change under delete-then-recreate
    # (see `build_cycle_observations`'s docstring for why that's the right
    # rebuild strategy); idempotency is about every OTHER column matching.
    fields = [f for f in ["firm_id", "region", "opened_count", "open_window_first",
                          "open_window_last", "closed_count", "close_window_first",
                          "close_window_last", "excluded_suspect_closes",
                          "currently_open_count", "onboarded_at"]]

    call_command("build_cycle_observations")
    first = list(FirmCycleObservation.objects.order_by("firm_id", "region").values(*fields))

    call_command("build_cycle_observations")
    second = list(FirmCycleObservation.objects.order_by("firm_id", "region").values(*fields))

    assert first == second
    assert FirmCycleObservation.objects.count() == 1


@pytest.mark.django_db
def test_a_relist_under_a_preserved_talnet_id_is_a_reopen_not_a_double_count(monkeypatch):
    """The reposting question this feature was told to investigate: a firm
    pulling a role and relisting it under a new URL should not read as a
    false close paired with a false open.

    For tal.net (and iCIMS/Workday-with-a-requisition-id), it doesn't — this
    is a regression test for that existing protection, not new code.
    `directory.dupes.provider_identity`/`ingest._match_by_identity` already
    match a relisted tal.net URL back to its existing row by the `opp/<id>`
    it carries, BEFORE any `OpportunityChange` is written, so the relist
    surfaces as a plain reopen of the same row. `build_cycle_observations`
    therefore sees one lifecycle (opened once, closed once, reopened once)
    rather than two — there is nothing left here for this feature's own
    close/open counting to double-count.
    """
    board = TalnetBoard(firm="Test Bank", board_url="https://testbank.tal.net/candidate/jobboard/vacancy/1/adv/")
    pool1 = "https://testbank.tal.net/pl/1/en/opp/500/apply"
    pool2 = "https://testbank.tal.net/pl/2/en/opp/500/apply"  # same opp id, different pool
    other = "https://testbank.tal.net/pl/1/en/opp/999/apply"  # stays open throughout, so the
    # board's `seen` set is never empty — an empty `seen` trips ingest.py's
    # OWN "suspected shape change" wipe guard, which would then refuse to
    # close opp 500 at all and leave nothing for the relist half of this
    # test to reopen.

    def _result(opps, ok=True):
        return FetchResult(board=board, ok=ok, opportunities=list(opps),
                           raw_count=len(list(opps)))

    def _conn_opp(url):
        return ConnOpp(firm="Test Bank", title="Summer Analyst Internship", location="London",
                       url=url, source="talnet", deadline=None)

    monkeypatch.setattr(ingest, "fetch_many", lambda *a, **k: [_result([_conn_opp(pool1), _conn_opp(other)])])
    ingest.ingest_boards([board], label="talnet")
    assert Opportunity.objects.count() == 2

    monkeypatch.setattr(ingest, "fetch_many", lambda *a, **k: [_result([_conn_opp(other)])])
    ingest.ingest_boards([board], label="talnet")
    assert Opportunity.objects.get(url=pool1).status == "closed"

    monkeypatch.setattr(ingest, "fetch_many", lambda *a, **k: [_result([_conn_opp(pool2), _conn_opp(other)])])
    ingest.ingest_boards([board], label="talnet")

    # Still exactly two rows total — the relist matched opp 500 back by
    # provider identity rather than minting a third row. The matched row
    # keeps its ORIGINAL url (`_apply_opportunity` never rewrites `url` on
    # an identity-matched update — only content fields move), which is
    # itself evidence nothing new was created.
    assert Opportunity.objects.count() == 2
    live = Opportunity.objects.get(firm__slug="test-bank", url=pool1)
    assert live.status == "open"

    Opportunity.objects.update(bucket="internship")
    call_command("build_cycle_observations")

    row = _observation(Firm.objects.get(slug="test-bank"), live.region or "")
    # One close, one reopen, on ONE posting — never a phantom second close
    # from a "new" row the relist never actually created.
    assert row.closed_count == 1
