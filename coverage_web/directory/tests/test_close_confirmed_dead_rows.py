"""close_confirmed_dead_rows — report-only by default, closes only rows the
live provider itself confirms are gone. No live network: the command
module's `verify` import is monkeypatched, same convention as
test_reopen_truncated_oracle_closures.py."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from coverage_connectors.models import VerificationResult

from directory.management.commands import close_confirmed_dead_rows as cmd_mod
from directory.models import Firm, Opportunity


def _open_opp(firm, url):
    o = Opportunity.objects.create(
        firm=firm, url=url, title="Recruitment Assistant",
        bucket="entry_level", status="open",
    )
    then = timezone.now() - timedelta(days=3)
    Opportunity.objects.filter(pk=o.pk).update(last_checked=then, last_verified=then)
    o.refresh_from_db()
    return o


def _result(url, verdict, evidence="test"):
    return VerificationResult(
        provider="greenhouse", url=url, result=verdict, evidence=evidence, deadline_dates=[]
    )


@pytest.mark.django_db
def test_default_run_reports_and_writes_nothing(monkeypatch):
    """Regression test for the confirmed Marshall Wace defect (Opportunity
    id=8885): coverage_connectors.greenhouse.verify() confirms the row is
    gone (boards-api 404), but a bare (no --apply) run must only report it,
    never write."""
    firm = Firm.objects.create(slug="marshallwace", name="Marshall Wace")
    dead = _open_opp(firm, "https://job-boards.greenhouse.io/marshallwace/jobs/8501520002")
    monkeypatch.setattr(cmd_mod, "verify",
                        lambda url: _result(url, "closed", "boards-api 404 for job 8501520002"))
    call_command("close_confirmed_dead_rows", ids=[dead.id])

    dead.refresh_from_db()
    assert dead.status == "open"  # reported, not written


@pytest.mark.django_db
def test_apply_closes_only_confirmed_dead_rows(monkeypatch):
    firm = Firm.objects.create(slug="marshallwace", name="Marshall Wace")
    dead = _open_opp(firm, "https://x/gone")
    still_live = _open_opp(firm, "https://x/live")
    undecidable = _open_opp(firm, "https://x/vague")

    verdicts = {
        "https://x/gone": _result("https://x/gone", "closed", "boards-api 404"),
        "https://x/live": _result("https://x/live", "verified-open", 'title="x"'),
        "https://x/vague": _result("https://x/vague", "unreachable"),
    }
    monkeypatch.setattr(cmd_mod, "verify", lambda url: verdicts[url])
    call_command("close_confirmed_dead_rows",
                ids=[dead.id, still_live.id, undecidable.id], apply=True)

    dead.refresh_from_db(); still_live.refresh_from_db(); undecidable.refresh_from_db()
    assert dead.status == "closed"
    assert dead.closed_at is not None
    assert still_live.status == "open"       # a live verdict changes nothing here
    assert undecidable.status == "open"      # neither does an undecidable one


@pytest.mark.django_db
def test_rows_not_open_are_skipped_not_reverified(monkeypatch):
    firm = Firm.objects.create(slug="marshallwace", name="Marshall Wace")
    already_closed = Opportunity.objects.create(
        firm=firm, url="https://x/already-closed", title="x", status="closed")
    monkeypatch.setattr(cmd_mod, "verify",
                        lambda url: pytest.fail("should not verify a row that isn't open"))
    call_command("close_confirmed_dead_rows", ids=[already_closed.id], apply=True)
    already_closed.refresh_from_db()
    assert already_closed.status == "closed"


@pytest.mark.django_db
def test_a_verify_error_is_reported_and_changes_nothing(monkeypatch):
    firm = Firm.objects.create(slug="marshallwace", name="Marshall Wace")
    flaky = _open_opp(firm, "https://x/flaky")

    def boom(url):
        raise OSError("connection reset")
    monkeypatch.setattr(cmd_mod, "verify", boom)
    call_command("close_confirmed_dead_rows", ids=[flaky.id], apply=True)

    flaky.refresh_from_db()
    assert flaky.status == "open"
