"""Tests for the reverify staleness pass — the honesty contract under test:
`last_verified` moves ONLY on a positive liveness signal; errors and
undecidable URLs stamp `last_checked` alone; a "closed" verdict flips status.
No live network: the command module's `verify` import is monkeypatched."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from coverage_connectors.models import VerificationResult

from directory.management.commands import reverify as reverify_mod
from directory.models import Firm, Opportunity, ScrapeRun


def _opp(firm, url, *, days_old=10):
    ts = timezone.now() - timedelta(days=days_old)
    o = Opportunity.objects.create(
        firm=firm, url=url, title="Summer Analyst", bucket="internship", status="open"
    )
    Opportunity.objects.filter(pk=o.pk).update(last_checked=ts, last_verified=ts)
    o.refresh_from_db()
    return o


def _result(url, verdict):
    return VerificationResult(
        provider="greenhouse", url=url, result=verdict, evidence="test", deadline_dates=[]
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
