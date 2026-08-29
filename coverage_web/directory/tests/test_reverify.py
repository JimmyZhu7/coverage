"""Tests for the reverify staleness pass — the honesty contract under test:
`last_verified` moves ONLY on a positive liveness signal; errors and
undecidable URLs stamp `last_checked` alone; a "closed" verdict flips status.
No live network: the command module's `verify` import is monkeypatched."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from coverage_connectors.models import VerificationResult

from directory.management.commands import reverify as reverify_mod
from directory.models import Firm, Opportunity, ScrapeRun


_UNSET = object()


def _opp(firm, url, *, days_old=10, deadline=None, deadline_checked_days_old=_UNSET):
    """`deadline_checked_days_old` defaults to mirroring `days_old` — both
    fields age together, the pre-split shape existing callers assume. Pass
    `None` explicitly to build the shape the routine `scrape` pass actually
    produces: `last_checked` bumped fresh by every list-refetch while
    `deadline_checked_at` stays NULL, because only `reverify`'s own
    detail-level check ever moves it."""
    ts = timezone.now() - timedelta(days=days_old)
    if deadline_checked_days_old is _UNSET:
        deadline_checked_ts = ts
    elif deadline_checked_days_old is None:
        deadline_checked_ts = None
    else:
        deadline_checked_ts = timezone.now() - timedelta(days=deadline_checked_days_old)
    o = Opportunity.objects.create(
        firm=firm, url=url, title="Summer Analyst", bucket="internship", status="open",
        deadline=deadline, deadline_precision="day" if deadline else "",
    )
    Opportunity.objects.filter(pk=o.pk).update(
        last_checked=ts, last_verified=ts, deadline_checked_at=deadline_checked_ts,
    )
    o.refresh_from_db()
    return o


def _result(url, verdict, deadline_dates=None):
    return VerificationResult(
        provider="greenhouse", url=url, result=verdict, evidence="test",
        deadline_dates=deadline_dates or [],
    )


@pytest.mark.django_db
def test_reverify_verdicts_update_rows_honestly(monkeypatch):
    firm = Firm.objects.create(slug="acme", name="Acme")
    live = _opp(firm, "https://x/live")
    dead = _opp(firm, "https://x/dead")
    flaky = _opp(firm, "https://x/flaky")
    undecidable = _opp(firm, "https://x/vague")
    fresh = _opp(firm, "https://x/fresh", days_old=0)  # not stale -> untouched

    verdicts = {
        "https://x/live": _result("https://x/live", "verified-open"),
        "https://x/dead": _result("https://x/dead", "closed"),
        "https://x/vague": _result("https://x/vague", "needs-verification"),
    }

    def fake_verify(url):
        if url == "https://x/flaky":
            raise OSError("connection reset")
        return verdicts[url]

    monkeypatch.setattr(reverify_mod, "verify", fake_verify)
    call_command("reverify")

    live.refresh_from_db(); dead.refresh_from_db()
    flaky.refresh_from_db(); undecidable.refresh_from_db(); fresh.refresh_from_db()

    # Positive signal: both stamps move, still open.
    assert live.status == "open"
    assert (timezone.now() - live.last_verified).days == 0

    # Definitive death: closed, last_verified NOT refreshed.
    assert dead.status == "closed"
    assert (timezone.now() - dead.last_verified).days >= 9

    # Error / undecidable: we looked (last_checked moves), but last_verified
    # must not — an error is never evidence of life.
    for o in (flaky, undecidable):
        assert o.status == "open"
        assert (timezone.now() - o.last_checked).days == 0
        assert (timezone.now() - o.last_verified).days >= 9

    # A fresh row was never a candidate.
    assert (timezone.now() - fresh.last_checked).days == 0

    run = ScrapeRun.objects.get(connector="reverify")
    assert run.status == "ok"
    assert run.stats["checked"] == 4
    assert run.stats["closed"] == 1
    assert run.stats["verified_open"] == 1
    assert run.stats["unreachable"] == 1
    assert run.stats["needs_verification"] == 1


@pytest.mark.django_db
def test_reverify_refreshes_a_stale_deadline_on_verified_open(monkeypatch):
    """PINS A FIXED BUG: a BMO/Workday row whose deadline was frozen at
    first-ingest (2026-05-24, long past) gets re-confirmed as verified-open
    every reverify pass without the stale deadline ever moving, because the
    old code only ever wrote last_checked/last_verified on that verdict. A
    provider's verify endpoint that reports a fresh deadline_dates entry
    must be allowed to correct it."""
    firm = Firm.objects.create(slug="bmo", name="BMO")
    stale = _opp(firm, "https://bmo.wd3.myworkdayjobs.com/x", deadline=date(2026, 5, 24))

    monkeypatch.setattr(
        reverify_mod, "verify",
        lambda url: _result(url, "verified-open", deadline_dates=["2026-08-30"]),
    )
    call_command("reverify")

    stale.refresh_from_db()
    assert stale.deadline == date(2026, 8, 30)
    assert stale.deadline_precision == "day"
    assert stale.status == "open"


@pytest.mark.django_db
def test_reverify_leaves_deadline_alone_when_the_provider_states_none(monkeypatch):
    """The honesty contract extends to deadline: a provider whose verify
    endpoint reports no deadline_dates (Workday tenants with no stated
    deadline, Lever, an unreachable/undecidable check) must not clear or
    guess at an existing stored deadline."""
    firm = Firm.objects.create(slug="acme", name="Acme")
    dated = _opp(firm, "https://x/no-signal", deadline=date(2026, 5, 24))

    monkeypatch.setattr(reverify_mod, "verify", lambda url: _result(url, "verified-open"))
    call_command("reverify")

    dated.refresh_from_db()
    assert dated.deadline == date(2026, 5, 24)


@pytest.mark.django_db
def test_reverify_selects_a_row_the_routine_scrape_keeps_falsely_fresh(monkeypatch):
    """PINS THE STARVATION BUG: a BMO/Workday row's `last_checked` is bumped
    to today by every routine `scrape` pass (list endpoint, no deadline
    field) even though its `deadline` was frozen at first ingest and never
    actually re-examined. A candidate query keyed on `last_checked` would
    skip this row forever — it always looks freshly checked — so `reverify`
    would never reach the one posting whose deadline needs correcting. The
    fix: candidate selection keys on `deadline_checked_at`, which only this
    command's own detail-level check moves, so a row with a stale deadline
    stays a candidate no matter how often the list scrape re-touches it."""
    firm = Firm.objects.create(slug="bmo", name="BMO")
    starved = _opp(
        firm, "https://bmo.wd3.myworkdayjobs.com/x",
        days_old=0,  # last_checked = today, exactly like today's routine scrape
        deadline=date(2026, 5, 28),
        deadline_checked_days_old=None,  # never deep-checked
    )

    monkeypatch.setattr(
        reverify_mod, "verify",
        lambda url: _result(url, "verified-open", deadline_dates=["2026-09-07"]),
    )
    call_command("reverify")

    starved.refresh_from_db()
    assert starved.deadline == date(2026, 9, 7)
    assert starved.deadline_checked_at is not None
    assert (timezone.now() - starved.deadline_checked_at).total_seconds() < 5


@pytest.mark.django_db
def test_reverify_ids_reaches_a_row_the_queue_would_not_get_to_yet(monkeypatch):
    """PINS THE FIX for bmo-associate-9446-deadline-drift-42-days: with
    `deadline_checked_at` NULL catalog-wide (the field was just introduced —
    nothing had ever run the deep check before), the default oldest-NULL
    `--limit`-capped queue only reaches a handful of rows per run. A row a
    live audit has already named and confirmed wrong should not have to wait
    its turn — `--ids` bypasses the cutoff/`--limit` and checks exactly the
    named row now, mirroring `enrich_postings --ids`.

    Also proves `--ids` does NOT pull in an unrelated fresh row that would
    never have been a queue candidate — it is a scoped backfill, not a
    second general run."""
    firm = Firm.objects.create(slug="bmo", name="BMO")
    named = _opp(
        firm, "https://bmo.wd3.myworkdayjobs.com/named",
        days_old=0, deadline=date(2026, 7, 24), deadline_checked_days_old=None,
    )
    # A fresh, never-audited row that --limit's default ordering would also
    # eventually reach — --ids must not sweep it in.
    untouched = _opp(
        firm, "https://bmo.wd3.myworkdayjobs.com/untouched",
        days_old=0, deadline=date(2026, 5, 1), deadline_checked_days_old=None,
    )

    monkeypatch.setattr(
        reverify_mod, "verify",
        lambda url: _result(url, "verified-open", deadline_dates=["2026-09-04"]),
    )
    call_command("reverify", ids=str(named.id))

    named.refresh_from_db()
    untouched.refresh_from_db()
    assert named.deadline == date(2026, 9, 4)
    assert named.deadline_checked_at is not None
    assert untouched.deadline_checked_at is None  # not swept in by --ids

    run = ScrapeRun.objects.get(connector="reverify")
    assert run.stats["checked"] == 1


@pytest.mark.django_db
def test_reverify_ids_does_not_touch_a_closed_row(monkeypatch):
    """`reverify` is, by its own module docstring, the routine check over
    OPEN rows — closed rows are `reopen_confirmed_live_rows`'s job, which
    exists specifically because reopening needs its own audit trail
    (flipping `status` back, clearing `closed_at`). Without a `status="open"`
    guard on the `--ids` path, naming a closed row's id here would call
    `verify()`, and a `verified-open` result would stamp `last_verified` and
    even rewrite `deadline` on a row still marked closed everywhere else —
    an inconsistent state (a "closed" posting with a same-day "verified"
    timestamp) that this command has no business creating."""
    firm = Firm.objects.create(slug="bmo", name="BMO")
    closed = _opp(firm, "https://bmo.wd3.myworkdayjobs.com/closed", days_old=30)
    closed.status = "closed"
    closed.closed_at = timezone.now() - timedelta(days=30)
    closed.save(update_fields=["status", "closed_at"])
    original_deadline_checked_at = closed.deadline_checked_at

    called = []
    monkeypatch.setattr(
        reverify_mod, "verify",
        lambda url: called.append(url) or _result(url, "verified-open", deadline_dates=["2026-09-04"]),
    )
    call_command("reverify", ids=str(closed.id))

    assert called == [], "a closed row must never even reach verify()"
    closed.refresh_from_db()
    assert closed.status == "closed", "reverify must never silently reopen a row"
    assert closed.deadline_checked_at == original_deadline_checked_at
    # Nothing was a candidate, so the command takes its early "nothing stale
    # enough" exit and never even opens a ScrapeRun for this pass.
    assert not ScrapeRun.objects.filter(connector="reverify").exists()


@pytest.mark.django_db
def test_reverify_dry_run_writes_nothing(monkeypatch):
    firm = Firm.objects.create(slug="acme", name="Acme")
    dead = _opp(firm, "https://x/dead")
    monkeypatch.setattr(
        reverify_mod, "verify", lambda url: _result(url, "closed")
    )
    call_command("reverify", dry_run=True)
    dead.refresh_from_db()
    assert dead.status == "open"  # reported, not written


@pytest.mark.django_db
def test_refresh_chains_and_survives_a_failing_stage(monkeypatch):
    # scrape blows up (network world); reclassify + reverify must still run.
    from django.core.management import get_commands  # noqa: F401 — sanity import
    import directory.management.commands.refresh as refresh_mod

    calls = []
    real_call = refresh_mod.call_command

    def fake_call(name, *a, **kw):
        calls.append(name)
        if name == "scrape":
            raise RuntimeError("boards unreachable")
        if name in ("reverify", "enrich_postings"):
            # reverify: nothing stale in the test DB anyway.
            # enrich_postings: one HTTP request per posting — never from a test.
            return
        return real_call(name, *a, **kw)

    monkeypatch.setattr(refresh_mod, "call_command", fake_call)
    with pytest.raises(SystemExit):
        call_command("refresh")
    assert calls == ["scrape", "reclassify", "enrich_postings",
                     "extract_facts", "reverify"]


@pytest.mark.django_db
def test_refresh_extracts_after_it_enriches(monkeypatch):
    """Order is load-bearing between exactly these two stages: extraction reads
    the text enrichment just fetched, so a run that swapped them would derive
    every fact from yesterday's copy of the page.

    The stages were absent from this chain entirely for one release, and the
    gap showed within a day — the newest posting on the board carried no
    description, no deadline and no facts, because the only enrichment run had
    been a manual one."""
    import directory.management.commands.refresh as refresh_mod

    _opp(Firm.objects.create(slug="acme", name="Acme"), "https://x/live")
    calls = []
    monkeypatch.setattr(refresh_mod, "call_command",
                        lambda name, *a, **kw: calls.append(name))
    call_command("refresh")
    assert calls.index("enrich_postings") < calls.index("extract_facts")


@pytest.mark.django_db
def test_refresh_can_skip_the_network_stage_but_still_extracts(monkeypatch):
    """`--no-enrich` is for a fast local pass. Extraction still runs: it is
    pure CPU over text already stored, and the patterns change far more often
    than the pages do."""
    import directory.management.commands.refresh as refresh_mod

    _opp(Firm.objects.create(slug="acme", name="Acme"), "https://x/live")
    calls = []
    monkeypatch.setattr(refresh_mod, "call_command",
                        lambda name, *a, **kw: calls.append(name))
    call_command("refresh", no_enrich=True)
    assert "enrich_postings" not in calls
    assert "extract_facts" in calls
