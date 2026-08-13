"""reopen_truncated_oracle_closures — report-only by default, reopens only
rows the live provider itself confirms as still open. No live network: the
command module's `verify` import is monkeypatched, same convention as
test_reverify.py."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from coverage_connectors.models import VerificationResult

from directory.management.commands import (
    reopen_truncated_oracle_closures as cmd_mod,
)
from directory.models import Firm, Opportunity


def _closed_opp(firm, url, *, source="oracle"):
    o = Opportunity.objects.create(
        firm=firm, url=url, title="Analytics Solutions Manager, Vice President",
        bucket="entry_level", status="closed", source=source,
    )
    then = timezone.now() - timedelta(days=1)
    Opportunity.objects.filter(pk=o.pk).update(closed_at=then, last_checked=then,
                                                last_verified=then)
    o.refresh_from_db()
    return o


def _result(url, verdict, evidence="test"):
    return VerificationResult(
        provider="oracle", url=url, result=verdict, evidence=evidence, deadline_dates=[]
    )


@pytest.mark.django_db
def test_default_run_reports_and_writes_nothing(monkeypatch):
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    live = _closed_opp(firm, "https://x/live")
    monkeypatch.setattr(cmd_mod, "verify",
                        lambda url: _result(url, "verified-open", 'title="x"'))
    call_command("reopen_truncated_oracle_closures")

    live.refresh_from_db()
    assert live.status == "closed"  # reported, not written


@pytest.mark.django_db
def test_apply_reopens_only_confirmed_live_rows(monkeypatch):
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    live = _closed_opp(firm, "https://x/live")
    still_gone = _closed_opp(firm, "https://x/gone")
    undecidable = _closed_opp(firm, "https://x/vague")

    verdicts = {
        "https://x/live": _result("https://x/live", "verified-open", 'title="x"'),
        "https://x/gone": _result("https://x/gone", "closed"),
        "https://x/vague": _result("https://x/vague", "needs-verification"),
    }
    monkeypatch.setattr(cmd_mod, "verify", lambda url: verdicts[url])
    call_command("reopen_truncated_oracle_closures", apply=True)

    live.refresh_from_db(); still_gone.refresh_from_db(); undecidable.refresh_from_db()
    assert live.status == "open"
    assert live.closed_at is None
    assert still_gone.status == "closed"       # a "closed" verdict changes nothing here
    assert undecidable.status == "closed"      # neither does an undecidable one


@pytest.mark.django_db
def test_only_oracle_sourced_closed_rows_are_considered(monkeypatch):
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    other_source = _closed_opp(firm, "https://x/gh", source="greenhouse")
    monkeypatch.setattr(cmd_mod, "verify",
                        lambda url: pytest.fail("should not be called for non-oracle rows"))
    call_command("reopen_truncated_oracle_closures", apply=True)
    other_source.refresh_from_db()
    assert other_source.status == "closed"


@pytest.mark.django_db
def test_a_verify_error_is_reported_and_changes_nothing(monkeypatch):
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    flaky = _closed_opp(firm, "https://x/flaky")

    def boom(url):
        raise OSError("connection reset")
    monkeypatch.setattr(cmd_mod, "verify", boom)
    call_command("reopen_truncated_oracle_closures", apply=True)

    flaky.refresh_from_db()
    assert flaky.status == "closed"
