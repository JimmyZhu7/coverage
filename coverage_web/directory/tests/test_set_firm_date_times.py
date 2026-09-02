"""`set_firm_date_times` — the only writer of `FirmDate.close_time`.

Dry run is the default because `FirmDate` is shared directory data: a bad
write here is not one person's mistake, it is everybody's. The four refusals
are the substance of the command and most of this file — each exists because
writing anyway would produce a deadline that looks more certain than the
evidence behind it.
"""

from __future__ import annotations

from datetime import date, time
from io import StringIO

import pytest
from django.core.management import call_command

from directory.models import Firm, FirmDate
from directory.seed_parsers import parse_firm_date_times

pytestmark = pytest.mark.django_db

HK = "Asia/Hong_Kong"

FINDINGS = """\
entries:
- firm: citi
  cycle: sa2027
  region: hk
  event_kind: app_close
  date: 2026-10-30
  time: "23:59"
  tz: Asia/Hong_Kong
  source: https://jobs.citi.com/
  quote:
    Citi HK SA 2027 closed "Friday, October 30, 2026 at 23:59 HKT"
    (Grade A).
"""


@pytest.fixture
def findings(tmp_path):
    p = tmp_path / "firm_date_times.yaml"
    p.write_text(FINDINGS)
    return str(p)


@pytest.fixture
def firm():
    return Firm.objects.create(slug="citi", name="Citi")


def _row(firm, **kw):
    base = dict(firm=firm, cycle="sa2027", track="", region="hk",
                event_kind="app_close", date=date(2026, 10, 30),
                precision="", confidence=1.0)
    base.update(kw)
    return FirmDate.objects.create(**base)


def _run(findings, **kw):
    out = StringIO()
    call_command("set_firm_date_times", findings=findings, stdout=out, **kw)
    return out.getvalue()


# ---------------------------------------------------------------------------
# The findings file, and the parser it shares with the timeline seeds.
# ---------------------------------------------------------------------------

def test_the_shipped_findings_file_parses():
    """It rides the same parser `timeline_*.yaml` does, which is the point:
    this repo has no YAML library and two readers of one shape is what "one
    definition per fact" exists to prevent."""
    from pathlib import Path

    from directory.management.commands.set_firm_date_times import _DEFAULT_FINDINGS

    entries = parse_firm_date_times(Path(_DEFAULT_FINDINGS).read_text())

    assert [e["firm"] for e in entries] == ["ms", "citi"]
    assert entries[1]["time"] == "23:59" and entries[1]["tz"] == HK
    # The quote folds across two lines and must arrive whole — it is the
    # evidence, and a truncated quote is a citation nobody can check.
    assert "23:59 HKT" in entries[1]["quote"]


# ---------------------------------------------------------------------------
# The happy path.
# ---------------------------------------------------------------------------

def test_the_dry_run_writes_nothing(firm, findings):
    fd = _row(firm)

    out = _run(findings)
    fd.refresh_from_db()

    assert "would set a closing time on 1 row(s)" in out
    assert fd.close_time is None and fd.close_tz == ""


def test_apply_sets_the_pair(firm, findings):
    fd = _row(firm)

    _run(findings, apply=True)
    fd.refresh_from_db()

    assert fd.close_time == time(23, 59)
    assert fd.close_tz == HK
    assert fd.close_time_label("America/Los_Angeles") == "23:59 HKT, 08:59 your time"


def test_history_is_appended_with_the_evidence(firm, findings):
    """Append-only, and it carries the quote: the next reader weighs the
    claim rather than taking the column's word for it."""
    fd = _row(firm, history=[{"note": "an earlier entry"}])

    _run(findings, apply=True)
    fd.refresh_from_db()

    assert fd.history[0] == {"note": "an earlier entry"}
    assert fd.history[-1]["outcome"] == "close_time_set"
    assert fd.history[-1]["close_tz"] == HK
    assert "23:59 HKT" in fd.history[-1]["note"]


def test_a_second_run_is_a_no_op(firm, findings):
    _row(firm)
    _run(findings, apply=True)

    out = _run(findings, apply=True)

    assert "already set" in out
    assert "on 0 row(s)" in out


# ---------------------------------------------------------------------------
# The four refusals.
# ---------------------------------------------------------------------------

def test_it_refuses_a_row_that_is_not_confirmed_official(firm, findings):
    """The live case: Morgan Stanley's 27 Sep Hong Kong close states 23:55 HKT
    at Grade A and the stored row is `reported`. An hour on it would dress a
    rumour, so the hour waits for the row to be confirmed."""
    fd = _row(firm, confidence=0.6)

    out = _run(findings, apply=True)
    fd.refresh_from_db()

    assert "not confirmed_official" in out
    assert fd.close_time is None


def test_it_refuses_a_month_precision_row(firm, findings):
    fd = _row(firm, precision="month")

    out = _run(findings, apply=True)
    fd.refresh_from_db()

    assert "locates no single day" in out
    assert fd.close_time is None


def test_it_refuses_a_row_whose_day_has_moved(firm, findings):
    """A time is a fact about ONE close. The six Hong Kong cycle relabels of
    2026-09-02 are what a moved row looks like in practice, and attaching the
    hour anyway would put last cycle's time on this cycle's date."""
    fd = _row(firm, date=date(2027, 10, 30))

    out = _run(findings, apply=True)
    fd.refresh_from_db()

    assert "a different deadline" in out
    assert fd.close_time is None


def test_it_never_creates_a_row(firm, findings):
    """An entry matching nothing is reported and skipped. `import_firm_dates`
    owns creating deadlines; a file about times may not mint one."""
    out = _run(findings, apply=True)

    assert "no stored row with that scope" in out
    assert FirmDate.objects.count() == 0


def test_it_refuses_an_abbreviation_where_a_zone_key_belongs(firm, tmp_path):
    """"HKT" is not a zone `zoneinfo` can resolve and "CST" names three of
    them. The column stores the convertible fact; the label is rendered from
    it."""
    fd = _row(firm)
    p = tmp_path / "bad.yaml"
    p.write_text(FINDINGS.replace("tz: Asia/Hong_Kong", "tz: HKT"))

    out = _run(str(p), apply=True)
    fd.refresh_from_db()

    assert "is not an IANA zone key" in out
    assert fd.close_time is None


def test_it_refuses_an_unreadable_time(firm, tmp_path):
    fd = _row(firm)
    p = tmp_path / "bad.yaml"
    p.write_text(FINDINGS.replace('time: "23:59"', 'time: "end of day"'))

    out = _run(str(p), apply=True)
    fd.refresh_from_db()

    assert "unreadable time" in out
    assert fd.close_time is None


def test_a_sibling_row_in_another_market_is_not_touched(firm, findings):
    """Matched on the full unique key. Matching on less would let one entry
    land on the same firm's deadline in a different region."""
    hk = _row(firm)
    us = _row(firm, region="us")

    _run(findings, apply=True)
    hk.refresh_from_db()
    us.refresh_from_db()

    assert hk.close_time == time(23, 59)
    assert us.close_time is None
