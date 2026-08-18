"""Tests for the row-level change seam — `directory.models.OpportunityChange`.

The gap being pinned: the scan has always computed, per row, whether the
posting moved (`ingest._apply_opportunity`'s `changed = existing.content_hash
!= h` and `was_closed = existing.status == "closed"`) and then spent that
knowledge entirely on `stats["updated"] += 1`. `ScrapeRun.stats` held counts
and nothing else, so a student's tracked role could have its deadline
overwritten or be flipped closed and nothing downstream could ever learn
which posting it was — or what the value had been before.

Same no-live-network posture as `test_ingest` and `test_reverify`:
`directory.ingest.fetch_many` and the reverify command's `verify` import are
monkeypatched.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from coverage_connectors import FetchResult, GreenhouseBoard
from coverage_connectors.models import Opportunity as ConnOpp, VerificationResult

from directory import ingest
from directory.management.commands import reverify as reverify_mod
from directory.models import Firm, Opportunity, OpportunityChange

BOARD = GreenhouseBoard(firm="William Blair", token="williamblair")
U1 = "https://boards.greenhouse.io/williamblair/jobs/1"
U2 = "https://boards.greenhouse.io/williamblair/jobs/2"


def _opp(url, *, title="Summer Analyst", location="Chicago", deadline=None,
         firm="William Blair"):
    return ConnOpp(firm=firm, title=title, location=location, url=url,
                   source="greenhouse", deadline=deadline)


def _result(opps, *, board=BOARD, ok=True, error=None):
    return FetchResult(board=board, ok=ok, opportunities=list(opps),
                       raw_count=len(list(opps)), error=error)


def _patch(monkeypatch, results):
    monkeypatch.setattr(ingest, "fetch_many", lambda *a, **k: results)


def _changes(url, field=None):
    """Every change row recorded against the posting at `url`, oldest first."""
    qs = OpportunityChange.objects.filter(opportunity__url=url)
    if field is not None:
        qs = qs.filter(field=field)
    return list(qs.order_by("observed_at", "id"))


# ---------------------------------------------------------------------------
# The scrape path.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_deadline_that_moves_is_recorded_with_both_the_old_and_new_date(monkeypatch):
    """The whole point of the table. A provider restating a posting with a
    later closing date used to overwrite `deadline` in place, and the date
    the student had been counting down to was gone with no trace anywhere —
    `stats["updated"] += 1` was the entire record of it."""
    _patch(monkeypatch, [_result([_opp(U1, deadline="2026-09-15")])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    assert OpportunityChange.objects.count() == 0  # a creation is not a move

    _patch(monkeypatch, [_result([_opp(U1, deadline="2026-10-01")])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    moved = _changes(U1, "deadline")
    assert len(moved) == 1
    assert moved[0].old_value == "2026-09-15"
    assert moved[0].new_value == "2026-10-01"
    assert moved[0].stage == OpportunityChange.STAGE_SCRAPE
    assert run.stats["changes_recorded"] == 1
    # And the row itself still moved — recording is not instead of writing.
    assert Opportunity.objects.get(url=U1).deadline == date(2026, 10, 1)


@pytest.mark.django_db
def test_an_unchanged_posting_records_nothing_at_all(monkeypatch):
    """Performance, and honesty about what "changed" means. A full pass
    touches ~17,000 rows and the overwhelming majority do not move; a change
    row apiece would be seventeen thousand writes a run recording precisely
    nothing. Re-scraping the identical payload must leave the table empty."""
    both = [_result([_opp(U1, deadline="2026-09-15"), _opp(U2)])]
    _patch(monkeypatch, both)
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, both)  # byte-identical fetch, twice more
    ingest.ingest_boards([BOARD], label="greenhouse")
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert run.stats["unchanged"] == 2
    assert run.stats["changes_recorded"] == 0
    assert OpportunityChange.objects.count() == 0


@pytest.mark.django_db
def test_a_close_via_the_bulk_path_is_recorded_for_every_row_it_touched(monkeypatch):
    """The mass-close is a bulk `.update()` that never loads the rows, so
    their ids were not in memory at all — which is how the single largest
    status change in the pipeline left nothing per-row behind. The ids have
    to be collected BEFORE the update or there is nothing to record."""
    _patch(monkeypatch, [_result([_opp(U1), _opp(U2)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([_opp(U1)])])  # U2 no longer listed
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert run.stats["closed"] == 1
    assert run.stats["changes_recorded"] == 1
    closed = _changes(U2, "status")
    assert len(closed) == 1
    assert (closed[0].old_value, closed[0].new_value) == ("open", "closed")
    assert closed[0].stage == OpportunityChange.STAGE_SCRAPE_CLOSE
    # The reason matters as much as the fact: absence from a board is an
    # inference, not the firm saying so.
    assert "absent from a complete" in closed[0].note
    # The posting that stayed live records nothing.
    assert _changes(U1) == []


@pytest.mark.django_db
def test_a_reopen_is_recorded(monkeypatch):
    """A posting coming back is a real event a consumer wants — the closed
    branch of every downstream reader has to be able to un-fire."""
    both = [_result([_opp(U1), _opp(U2)])]
    _patch(monkeypatch, both)
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([_opp(U1)])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, both)  # U2 reappears
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert run.stats["reopened"] == 1
    assert run.stats["changes_recorded"] == 1
    status_moves = _changes(U2, "status")
    assert [(c.old_value, c.new_value) for c in status_moves] == [
        ("open", "closed"), ("closed", "open"),
    ]
    assert status_moves[1].stage == OpportunityChange.STAGE_SCRAPE


@pytest.mark.django_db
def test_a_retitled_posting_records_the_title_it_used_to_have(monkeypatch):
    """`existing.title = title` overwrites with no read of the prior value,
    so the old title was unrecoverable the instant that line ran. It has to
    be captured before the assignment, not after."""
    _patch(monkeypatch, [_result([_opp(U1, title="Summer Analyst")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([_opp(U1, title="Summer Analyst — IBD")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    renamed = _changes(U1, "title")
    assert len(renamed) == 1
    assert renamed[0].old_value == "Summer Analyst"
    assert renamed[0].new_value == "Summer Analyst — IBD"


@pytest.mark.django_db
def test_a_retracted_prose_deadline_is_recorded_as_a_move_to_nothing(monkeypatch):
    """Ingest's deliberate drop-to-None path: a posting whose content moved
    while our stored date was only a reading of its prose. The date leaves
    the row, so it must arrive here — and with the note saying it was our
    reading being retracted, NOT the firm withdrawing a deadline, because a
    consumer told only "deadline -> ∅" would report a withdrawal that never
    happened."""
    _patch(monkeypatch, [_result([_opp(U1)])])
    ingest.ingest_boards([BOARD], label="greenhouse")
    Opportunity.objects.filter(url=U1).update(
        deadline=date(2026, 9, 30), deadline_precision="day", confidence=0.6,
    )

    _patch(monkeypatch, [_result([_opp(U1, title="Off-Cycle Analyst")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.get(url=U1).deadline is None
    dropped = _changes(U1, "deadline")
    assert len(dropped) == 1
    assert dropped[0].old_value == "2026-09-30"
    assert dropped[0].new_value == ""      # "" is how "no value" round-trips
    assert "our reading" in dropped[0].note


@pytest.mark.django_db
def test_a_row_whose_upsert_failed_records_no_change(monkeypatch):
    """The per-row savepoint rolls the write back, so anything that row
    queued describes a move the table never made. A change log that states
    changes which were undone is worse than no change log."""
    _patch(monkeypatch, [_result([_opp(U1, deadline="2026-09-15")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    real_apply = ingest._apply_opportunity

    def flaky_apply(firm, opp, now, stats, **kw):
        result = real_apply(firm, opp, now, stats, **kw)
        raise RuntimeError("transient, after the row was written")

    monkeypatch.setattr(ingest, "_apply_opportunity", flaky_apply)
    _patch(monkeypatch, [_result([_opp(U1, deadline="2026-10-01")])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert any("row failed" in e["error"] for e in run.stats["errors"])
    assert run.stats["changes_recorded"] == 0
    assert OpportunityChange.objects.count() == 0
    # The write really was rolled back — the change log and the row agree.
    assert Opportunity.objects.get(url=U1).deadline == date(2026, 9, 15)


@pytest.mark.django_db
def test_one_move_records_one_row_not_one_per_hashed_field(monkeypatch):
    """`content_hash` covers six fields at once, so recording off the hash
    would report a deadline move every time the LOCATION moved — exactly the
    false signal a downstream alert has no way to filter. What moved is what
    gets written."""
    _patch(monkeypatch, [_result([_opp(U1, location="Chicago")])])
    ingest.ingest_boards([BOARD], label="greenhouse")

    _patch(monkeypatch, [_result([_opp(U1, location="New York")])])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    assert run.stats["updated"] == 1        # the hash did move
    assert run.stats["changes_recorded"] == 0   # but no recorded field did
    assert _changes(U1) == []


# ---------------------------------------------------------------------------
# The reverify path — the silent overwrite.
# ---------------------------------------------------------------------------


def _stored(firm, url, *, deadline=None, days_old=10):
    o = Opportunity.objects.create(
        firm=firm, url=url, title="Summer Analyst", bucket="internship",
        status="open", deadline=deadline, deadline_precision="day" if deadline else "",
    )
    ts = timezone.now() - timedelta(days=days_old)
    Opportunity.objects.filter(pk=o.pk).update(
        last_checked=ts, last_verified=ts, deadline_checked_at=ts,
    )
    o.refresh_from_db()
    return o


def _verification(url, verdict, deadline_dates=None):
    return VerificationResult(provider="greenhouse", url=url, result=verdict,
                              evidence="test", deadline_dates=deadline_dates or [])


@pytest.mark.django_db
def test_reverify_records_the_deadline_it_overwrites(monkeypatch):
    """PINS THE SILENT OVERWRITE: this command is the one place a stale
    stored deadline gets corrected from the provider's own fresh answer, and
    it did so with no record whatsoever — `opp.deadline = fresh` and the old
    date was gone. A student tracking that role could not be told it moved,
    because nothing remembered that it had."""
    firm = Firm.objects.create(slug="bmo", name="BMO")
    stale = _stored(firm, "https://bmo.wd3.myworkdayjobs.com/x",
                    deadline=date(2026, 5, 24))

    monkeypatch.setattr(
        reverify_mod, "verify",
        lambda url: _verification(url, "verified-open", deadline_dates=["2026-08-30"]),
    )
    call_command("reverify")

    stale.refresh_from_db()
    assert stale.deadline == date(2026, 8, 30)
    recorded = list(stale.changes.all())
    assert len(recorded) == 1
    assert recorded[0].field == "deadline"
    assert recorded[0].old_value == "2026-05-24"
    assert recorded[0].new_value == "2026-08-30"
    assert recorded[0].stage == OpportunityChange.STAGE_REVERIFY


@pytest.mark.django_db
def test_reverify_records_a_close_and_counts_it_in_the_run(monkeypatch):
    firm = Firm.objects.create(slug="acme", name="Acme")
    dead = _stored(firm, "https://x/dead")

    monkeypatch.setattr(reverify_mod, "verify",
                        lambda url: _verification(url, "closed"))
    call_command("reverify")

    dead.refresh_from_db()
    assert dead.status == "closed"
    recorded = list(dead.changes.all())
    assert len(recorded) == 1
    assert (recorded[0].field, recorded[0].old_value, recorded[0].new_value) == (
        "status", "open", "closed")
    assert recorded[0].stage == OpportunityChange.STAGE_REVERIFY

    from directory.models import ScrapeRun
    assert ScrapeRun.objects.get(connector="reverify").stats["changes_recorded"] == 1


@pytest.mark.django_db
def test_reverify_records_nothing_when_the_provider_restates_the_same_date(monkeypatch):
    """A verify endpoint repeating the date we already hold is not a move.
    reverify walks 200 rows a run on a schedule; recording every re-confirmed
    date would drown the real ones."""
    firm = Firm.objects.create(slug="acme", name="Acme")
    _stored(firm, "https://x/steady", deadline=date(2026, 8, 30))

    monkeypatch.setattr(
        reverify_mod, "verify",
        lambda url: _verification(url, "verified-open", deadline_dates=["2026-08-30"]),
    )
    call_command("reverify")

    assert OpportunityChange.objects.count() == 0


@pytest.mark.django_db
def test_reverify_dry_run_records_no_changes(monkeypatch):
    """`--dry-run` reports without writing, and the change log is a write."""
    firm = Firm.objects.create(slug="acme", name="Acme")
    _stored(firm, "https://x/dead")
    monkeypatch.setattr(reverify_mod, "verify",
                        lambda url: _verification(url, "closed"))

    call_command("reverify", dry_run=True)

    assert OpportunityChange.objects.count() == 0


# ---------------------------------------------------------------------------
# Retention — an append-only table on a 6-hourly cron needs an answer.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_prune_drops_records_past_the_retention_window_and_keeps_the_rest():
    firm = Firm.objects.create(slug="acme", name="Acme")
    opp = _stored(firm, "https://x/one")
    now = timezone.now()
    for age_days in (5, 179, 181, 400):
        OpportunityChange.objects.create(
            opportunity=opp, field="status", old_value="open", new_value="closed",
            stage=OpportunityChange.STAGE_SCRAPE,
            observed_at=now - timedelta(days=age_days),
        )

    deleted = OpportunityChange.prune()

    assert deleted == 2                              # the 181- and 400-day rows
    assert OpportunityChange.objects.count() == 2
    oldest = OpportunityChange.objects.order_by("observed_at").first()
    assert (now - oldest.observed_at).days == 179


@pytest.mark.django_db
def test_prune_with_a_zero_window_is_an_explicit_keep_everything():
    """0 disables the sweep rather than deleting the whole table — an
    operator keeping a season for analysis should not have to delete the
    call site to do it, and the failure mode of the other reading is losing
    every record at once."""
    firm = Firm.objects.create(slug="acme", name="Acme")
    opp = _stored(firm, "https://x/one")
    OpportunityChange.objects.create(
        opportunity=opp, field="status", old_value="open", new_value="closed",
        stage=OpportunityChange.STAGE_SCRAPE,
        observed_at=timezone.now() - timedelta(days=5000),
    )

    assert OpportunityChange.prune(older_than_days=0) == 0
    assert OpportunityChange.objects.count() == 1


@pytest.mark.django_db
def test_refresh_prunes_even_when_a_stage_failed(monkeypatch):
    """The stretch where a board is broken is exactly the stretch nobody is
    watching the table grow, so retention runs before the failure exit."""
    import directory.management.commands.refresh as refresh_mod

    firm = Firm.objects.create(slug="acme", name="Acme")
    opp = _stored(firm, "https://x/live", days_old=0)
    OpportunityChange.objects.create(
        opportunity=opp, field="status", old_value="open", new_value="closed",
        stage=OpportunityChange.STAGE_SCRAPE,
        observed_at=timezone.now() - timedelta(days=400),
    )

    def fake_call(name, *a, **kw):
        if name == "scrape":
            raise RuntimeError("boards unreachable")

    monkeypatch.setattr(refresh_mod, "call_command", fake_call)
    with pytest.raises(SystemExit):
        call_command("refresh")

    assert OpportunityChange.objects.count() == 0


# ---------------------------------------------------------------------------
# The record's own shape.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_change_records_die_with_the_posting_they_describe():
    """CASCADE, not a dangling audit trail: a change row describing a
    posting that no longer exists cannot be rendered, joined, or acted on."""
    firm = Firm.objects.create(slug="acme", name="Acme")
    opp = _stored(firm, "https://x/one")
    OpportunityChange.objects.create(
        opportunity=opp, field="status", old_value="open", new_value="closed",
        stage=OpportunityChange.STAGE_SCRAPE, observed_at=timezone.now(),
    )

    opp.delete()

    assert OpportunityChange.objects.count() == 0


@pytest.mark.django_db
def test_values_round_trip_as_text_whatever_the_field_held():
    """A date, a status word and a title all land in the same two columns,
    which is why they are TEXT — and why `None` renders as `""` rather than
    an untyped null, following `FirmDate.history`'s convention for a
    known-unknown."""
    assert OpportunityChange.render_value(date(2026, 9, 30)) == "2026-09-30"
    assert OpportunityChange.render_value(None) == ""
    assert OpportunityChange.render_value("closed") == "closed"
    assert OpportunityChange.render_value("Summer Analyst") == "Summer Analyst"
