"""Tests for `directory.cycle_trust` — the Phase 1 gate for
`FirmCycleObservation`: is a `scrape.close` row evidence, or an artifact of a
broken board?

Change rows and scrape runs are built directly against the models rather
than through `ingest.ingest_boards`, on purpose: `ingest.py`'s own guards
(`pair_all_ok`, the wipe guard) already refuse to WRITE a close for a board
that reported failure or returned nothing, so driving these scenarios
through the real pipeline can never produce the row this module needs to
classify — there would be nothing to test. These tests instead ask the
narrower question this module actually owns: given a `scrape.close` row and
the `ScrapeRun` that (per its timestamp) produced it, does `classify_closes`
call it correctly. `test_build_cycle_observations.py` covers the pipeline
integration (including the one case ingest.py's own guards do NOT catch —
a partial, not total, mass close).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from directory.cycle_trust import SUSPECT, TRUSTED, classify_closes
from directory.models import Firm, Opportunity, OpportunityChange, ScrapeRun


def _firm(slug="testbank", name="Test Bank"):
    return Firm.objects.create(slug=slug, name=name, status="active")


def _opp(firm, n, *, bucket="internship", source="greenhouse", status="open", first_seen=None):
    o = Opportunity.objects.create(
        firm=firm, title=f"Summer Analyst {n}", bucket=bucket, source=source,
        status=status, url=f"https://boards.greenhouse.io/{firm.slug}/jobs/{n}",
    )
    if first_seen is not None:
        Opportunity.objects.filter(pk=o.pk).update(first_seen=first_seen)
        o.refresh_from_db()
    return o


def _close_event(opp, *, at):
    return OpportunityChange.objects.create(
        opportunity=opp, field="status", old_value="open", new_value="closed",
        stage=OpportunityChange.STAGE_SCRAPE_CLOSE, observed_at=at,
        note="absent from a complete, successful fetch of the board",
    )


def _run(*, connector="all", started, finished, stats):
    return ScrapeRun.objects.create(
        connector=connector, started=started, finished=finished,
        status="ok", stats=stats,
    )


@pytest.mark.django_db
def test_a_close_from_a_run_whose_board_failed_is_suspect():
    """The real sample from the report: a run whose `stats` names this exact
    firm+provider as a fetch failure. A close event timestamped inside that
    run's window must never be counted as evidence."""
    firm = _firm()
    opp = _opp(firm, 1, status="closed")
    now = timezone.now()
    run = _run(
        started=now - timedelta(minutes=5), finished=now + timedelta(minutes=1),
        stats={
            "boards_total": 2, "boards_ok": 0, "boards_failed": 2,
            "firms_touched": [firm.slug],
            "errors": [{"firm": firm.name, "provider": "greenhouse",
                        "error": "GET https://boards-api.greenhouse.io/...: "
                                 "<urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING]>"}],
        },
    )
    change = _close_event(opp, at=now)

    [verdict] = classify_closes(OpportunityChange.objects.filter(pk=change.pk))
    assert verdict.verdict == SUSPECT
    assert str(run.id) in verdict.reason
    assert "failed" in verdict.reason


@pytest.mark.django_db
def test_a_close_from_a_healthy_run_is_trusted():
    """The ordinary case: the run's own stats say this firm's board was
    fetched fine, nothing else in the run is suspicious. This is the
    majority of live `scrape.close` rows (5,874/5,874 on the dev DB at the
    time this was written) and must not be second-guessed."""
    firm = _firm()
    opp = _opp(firm, 1, status="closed")
    now = timezone.now()
    _run(
        started=now - timedelta(minutes=5), finished=now + timedelta(minutes=1),
        stats={
            "boards_total": 1, "boards_ok": 1, "boards_failed": 0,
            "firms_touched": [firm.slug], "errors": [],
        },
    )
    change = _close_event(opp, at=now)

    [verdict] = classify_closes(OpportunityChange.objects.filter(pk=change.pk))
    assert verdict.verdict == TRUSTED


@pytest.mark.django_db
def test_a_row_level_error_for_a_different_posting_does_not_taint_the_close():
    """`stats["errors"]` is one list serving five different meanings (see
    the module docstring) — an unparseable-deadline note or an oversized-url
    skip on some OTHER row of the same successful board must not read as
    "this board's fetch failed" and drag an unrelated close down with it."""
    firm = _firm()
    opp = _opp(firm, 1, status="closed")
    now = timezone.now()
    _run(
        started=now - timedelta(minutes=5), finished=now + timedelta(minutes=1),
        stats={
            "boards_total": 1, "boards_ok": 1, "boards_failed": 0,
            "firms_touched": [firm.slug],
            "errors": [{"firm": firm.name, "provider": "greenhouse",
                        "error": "unparseable deadline 'soon' for .../jobs/9 "
                                 "— stored as no-deadline-posted"}],
        },
    )
    change = _close_event(opp, at=now)

    [verdict] = classify_closes(OpportunityChange.objects.filter(pk=change.pk))
    assert verdict.verdict == TRUSTED


@pytest.mark.django_db
def test_mass_close_is_suspect_even_when_the_run_reports_itself_healthy():
    """The gap `ingest.py`'s own wipe guard leaves open: that guard only
    fires on a LITERAL zero-rows fetch. A connector that returns a couple of
    real rows but silently drops most of a board (a shape change the
    connector doesn't recognise as failure) reports `boards_ok=1` with no
    errors at all, and closes nearly everything anyway. This module's own
    mass-close check has to catch that independently of what the run
    self-reports."""
    firm = _firm()
    now = timezone.now()
    earlier = now - timedelta(days=5)
    opps = [_opp(firm, n, first_seen=earlier) for n in range(5)]
    for o in opps:
        o.status = "closed"
        o.save(update_fields=["status"])
    _run(
        started=now - timedelta(minutes=5), finished=now + timedelta(minutes=1),
        stats={
            "boards_total": 1, "boards_ok": 1, "boards_failed": 0,
            "firms_touched": [firm.slug], "errors": [],
        },
    )
    changes = [_close_event(o, at=now) for o in opps]

    verdicts = classify_closes(
        OpportunityChange.objects.filter(pk__in=[c.pk for c in changes])
    )
    assert all(v.verdict == SUSPECT for v in verdicts)
    assert all("mass-close" in v.reason for v in verdicts)


@pytest.mark.django_db
def test_ordinary_partial_churn_is_not_flagged_as_a_mass_close():
    """The negative case for the same check: 2 closes out of 10 open
    postings is normal turnover, nowhere near the mass-close shape, and must
    stay trusted."""
    firm = _firm()
    now = timezone.now()
    earlier = now - timedelta(days=5)
    all_opps = [_opp(firm, n, first_seen=earlier) for n in range(10)]
    closing = all_opps[:2]
    for o in closing:
        o.status = "closed"
        o.save(update_fields=["status"])
    _run(
        started=now - timedelta(minutes=5), finished=now + timedelta(minutes=1),
        stats={
            "boards_total": 1, "boards_ok": 1, "boards_failed": 0,
            "firms_touched": [firm.slug], "errors": [],
        },
    )
    changes = [_close_event(o, at=now) for o in closing]

    verdicts = classify_closes(
        OpportunityChange.objects.filter(pk__in=[c.pk for c in changes])
    )
    assert all(v.verdict == TRUSTED for v in verdicts)
