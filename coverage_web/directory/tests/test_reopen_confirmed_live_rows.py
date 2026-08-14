"""reopen_confirmed_live_rows — report-only by default, reopens only rows
the live provider itself confirms are still live. No live network: the
command module's `verify` import is monkeypatched, same convention as
test_close_confirmed_dead_rows.py / test_reopen_truncated_oracle_closures.py.

Regression coverage for the two confirmed round-4 findings (since applied
and reopened, so no longer in DEFAULT_IDS, but still exercised generically
below to prove the command handles either source):
- Opportunity id=9595 (BMO, source="phenom") — closed by Phenom's search
  widget dropping the row while the underlying Workday tenant's own
  wday/cxs API and its posting page's `postingAvailable` flag both still
  say the requisition is live.
- Opportunity id=3979 (Millennium, source="eightfold") — closed because the
  job id fell out of career.mlp.com's own search listing while the exact
  same id's detail page keeps serving a live JobPosting JSON-LD block.
Both are exercised generically here (the command is source-agnostic, unlike
reopen_truncated_oracle_closures which is scoped to source="oracle") since
the fix for both is the same shape: trust the provider's own live verdict
over a list-based close.

Round 5 added six more BMO/phenom ids to DEFAULT_IDS (9530, 9490, 9539,
9361, 9526, 9544) — same mechanism, a second and third independent sample
of the same closed pool. Those six have since been applied and reopened too.

Round 6 fixed the actual code bug behind all of this (workday.py's
`classify_url` was capturing a trailing UI-route suffix like "/apply" into
job_path, so `verify()` 422'd on every BMO/phenom row's detail_url and could
never report anything but "unreachable" — see workday.py) and, with that
fixed, added two freshly-confirmed ids to DEFAULT_IDS (9514, 9433).
`test_default_ids_reports_the_current_round_rows` below exercises
DEFAULT_IDS as it stands today.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from coverage_connectors.models import VerificationResult

from directory.management.commands import reopen_confirmed_live_rows as cmd_mod
from directory.models import Firm, Opportunity


def _closed_opp(firm, url, *, source="phenom", title="Personal Banking Associate"):
    o = Opportunity.objects.create(
        firm=firm, url=url, title=title, bucket="entry_level",
        status="closed", source=source,
    )
    then = timezone.now() - timedelta(hours=6)
    Opportunity.objects.filter(pk=o.pk).update(closed_at=then, last_checked=then)
    o.refresh_from_db()
    return o


def _result(provider, url, verdict, evidence="test"):
    return VerificationResult(
        provider=provider, url=url, result=verdict, evidence=evidence, deadline_dates=[])


@pytest.mark.django_db
def test_default_run_reports_and_writes_nothing(monkeypatch):
    firm = Firm.objects.create(slug="bmo", name="BMO")
    live = _closed_opp(
        firm, "https://bmo.wd3.myworkdayjobs.com/External/job/Oshawa-ON-CAN/"
              "Personal-Banking-Associate_R260022457/apply")
    monkeypatch.setattr(
        cmd_mod, "verify",
        lambda url: _result("workday", url, "verified-open",
                            "CxS job-detail HTTP 422; posting page's own "
                            "postingAvailable flag reads true"))
    call_command("reopen_confirmed_live_rows", ids=[live.id])

    live.refresh_from_db()
    assert live.status == "closed"  # reported, not written


def test_default_ids_is_the_current_round_bmo_list():
    """Round 6: the literal DEFAULT_IDS list must be the two BMO/phenom ids
    this round's audit confirmed still live (9514, 9433), sampled from the
    90-row batch reverify closed on 2026-08-14 — round 4's ids (9595, 3979)
    and round 5's six ids (9530, 9490, 9539, 9361, 9526, 9544) have since
    been applied and reopened and must not linger here."""
    assert cmd_mod.DEFAULT_IDS == [9514, 9433]


@pytest.mark.django_db
def test_default_ids_reports_the_current_round_rows(monkeypatch):
    """DEFAULT_IDS must pick up this round's six confirmed BMO rows with no
    --ids argument. A test database assigns its own pk sequence, so
    DEFAULT_IDS is monkeypatched to point at these fixture rows rather than
    faking their real pks — same convention as round 4's test above."""
    firm = Firm.objects.create(slug="bmo", name="BMO")
    rows = [
        _closed_opp(firm, f"https://bmo.wd3.myworkdayjobs.com/External/job/x/Role-{i}_R{i}",
                    title=f"Role {i}")
        for i in range(6)
    ]
    monkeypatch.setattr(cmd_mod, "DEFAULT_IDS", [r.id for r in rows])
    monkeypatch.setattr(
        cmd_mod, "verify",
        lambda url: _result("workday", url, "verified-open",
                            "CxS job-detail HTTP 422; postingAvailable true"))
    call_command("reopen_confirmed_live_rows")  # no --ids: exercises DEFAULT_IDS

    for r in rows:
        r.refresh_from_db()
        assert r.status == "closed"  # report-only by default


@pytest.mark.django_db
def test_default_ids_reports_both_round4_rows_across_sources(monkeypatch):
    """DEFAULT_IDS is not scoped to one connector — round 4's two rows
    (BMO/phenom id=9595, Millennium/eightfold id=3979) both had to be picked
    up with no --ids argument, across two different sources — kept as a
    generic regression now that those two ids have moved out of the live
    DEFAULT_IDS list."""
    bmo = Firm.objects.create(slug="bmo", name="BMO")
    millennium = Firm.objects.create(slug="millennium", name="Millennium")
    phenom_row = _closed_opp(
        bmo, "https://bmo.wd3.myworkdayjobs.com/External/job/x/Personal-Banking-Associate_R1",
        source="phenom")
    eightfold_row = _closed_opp(
        millennium, "https://mlp.eightfold.ai/careers/job/755954667610",
        source="eightfold", title="Data Operations Analyst - Systematic Trading")
    # DEFAULT_IDS is hardcoded to the live rows' real ids (9595, 3979); a
    # test database assigns its own pk sequence, so DEFAULT_IDS is
    # monkeypatched to point at these two rows rather than faking their pks.
    monkeypatch.setattr(cmd_mod, "DEFAULT_IDS", [phenom_row.id, eightfold_row.id])
    verdicts = {
        phenom_row.url: _result("workday", phenom_row.url, "verified-open",
                                "CxS job-detail HTTP 422; postingAvailable true"),
        eightfold_row.url: _result("eightfold", eightfold_row.url, "verified-open",
                                   "posting page reachable"),
    }
    monkeypatch.setattr(cmd_mod, "verify", lambda url: verdicts[url])
    call_command("reopen_confirmed_live_rows")  # no --ids: exercises DEFAULT_IDS

    phenom_row.refresh_from_db()
    eightfold_row.refresh_from_db()
    assert phenom_row.status == "closed"    # report-only by default
    assert eightfold_row.status == "closed"


@pytest.mark.django_db
def test_apply_reopens_only_confirmed_live_rows(monkeypatch):
    firm = Firm.objects.create(slug="bmo", name="BMO")
    live = _closed_opp(firm, "https://x/live")
    still_gone = _closed_opp(firm, "https://x/gone")
    undecidable = _closed_opp(firm, "https://x/vague")

    verdicts = {
        "https://x/live": _result("workday", "https://x/live", "verified-open", "title=x"),
        "https://x/gone": _result("workday", "https://x/gone", "closed"),
        "https://x/vague": _result("workday", "https://x/vague", "needs-verification"),
    }
    monkeypatch.setattr(cmd_mod, "verify", lambda url: verdicts[url])
    call_command("reopen_confirmed_live_rows",
                ids=[live.id, still_gone.id, undecidable.id], apply=True)

    live.refresh_from_db(); still_gone.refresh_from_db(); undecidable.refresh_from_db()
    assert live.status == "open"
    assert live.closed_at is None
    assert live.last_verified is not None
    assert still_gone.status == "closed"       # a "closed" verdict changes nothing here
    assert undecidable.status == "closed"      # neither does an undecidable one


@pytest.mark.django_db
def test_rows_not_closed_are_skipped_not_reverified(monkeypatch):
    firm = Firm.objects.create(slug="bmo", name="BMO")
    already_open = Opportunity.objects.create(
        firm=firm, url="https://x/already-open", title="x", status="open")
    monkeypatch.setattr(cmd_mod, "verify",
                        lambda url: pytest.fail("should not verify a row that isn't closed"))
    call_command("reopen_confirmed_live_rows", ids=[already_open.id], apply=True)
    already_open.refresh_from_db()
    assert already_open.status == "open"


@pytest.mark.django_db
def test_a_verify_error_is_reported_and_changes_nothing(monkeypatch):
    firm = Firm.objects.create(slug="bmo", name="BMO")
    flaky = _closed_opp(firm, "https://x/flaky")

    def boom(url):
        raise OSError("connection reset")
    monkeypatch.setattr(cmd_mod, "verify", boom)
    call_command("reopen_confirmed_live_rows", ids=[flaky.id], apply=True)

    flaky.refresh_from_db()
    assert flaky.status == "closed"
