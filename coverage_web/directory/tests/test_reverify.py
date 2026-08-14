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


def _opp(firm, url, *, days_old=10, deadline=None):
    ts = timezone.now() - timedelta(days=days_old)
    o = Opportunity.objects.create(
        firm=firm, url=url, title="Summer Analyst", bucket="internship", status="open",
        deadline=deadline, deadline_precision="day" if deadline else "",
    )
    Opportunity.objects.filter(pk=o.pk).update(last_checked=ts, last_verified=ts)
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
        if name == "reverify":
            return  # nothing stale in the test DB anyway
        return real_call(name, *a, **kw)

    monkeypatch.setattr(refresh_mod, "call_command", fake_call)
    with pytest.raises(SystemExit):
        call_command("refresh")
    assert calls == ["scrape", "reclassify", "reverify"]
