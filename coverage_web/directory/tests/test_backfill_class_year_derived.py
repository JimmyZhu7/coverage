"""backfill_class_year_derived — dry-run by default, recomputes
Opportunity.class_year_derived from classify.derive_class_year and reports
(or, with --commit, writes) rows where the stored column has drifted."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from io import StringIO

from directory.classify import ENTRY_LEVEL, INSIGHT, INTERNSHIP, derive_class_year
from directory.management.commands.backfill_class_year_derived import (
    expected_class_year_derived,
)
from directory.models import Firm, Opportunity


def _firm(slug="hsbc", name="HSBC"):
    return Firm.objects.create(slug=slug, name=name)


def _run(**opts):
    out = StringIO()
    call_command("backfill_class_year_derived", stdout=out, **opts)
    return out.getvalue()


# ---------------------------------------------------------------------------
# blank -> value: the shape the live measurement actually found (247/2,662).
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_default_run_reports_blank_to_value_and_writes_nothing():
    firm = _firm()
    opp = Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst Program", status="open",
        url="https://hsbc.example/job/1", bucket=INTERNSHIP, cohort="2027",
        class_year="", class_year_derived="",
    )
    out = _run()

    opp.refresh_from_db()
    assert opp.class_year_derived == ""  # untouched
    assert "[dry-run]" in out
    assert "BLANK -> VALUE" in out
    assert "'' -> '2028'" in out
    assert "Nothing was written" in out


@pytest.mark.django_db
def test_commit_writes_blank_to_value():
    firm = _firm()
    opp = Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst Program", status="open",
        url="https://hsbc.example/job/1", bucket=INTERNSHIP, cohort="2027",
        class_year="", class_year_derived="",
    )
    out = _run(commit=True)

    opp.refresh_from_db()
    assert opp.class_year_derived == "2028"
    assert "[dry-run]" not in out
    assert "1 repaired" in out


@pytest.mark.django_db
def test_apply_flag_is_accepted_as_an_alias_for_commit():
    """The reclassify_inbound_touches convention: --commit and --apply both
    work so neither habit fails silently."""
    firm = _firm()
    opp = Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst Program", status="open",
        url="https://hsbc.example/job/1", bucket=INTERNSHIP, cohort="2027",
        class_year="", class_year_derived="",
    )
    _run(apply=True)

    opp.refresh_from_db()
    assert opp.class_year_derived == "2028"


@pytest.mark.django_db
def test_closed_rows_are_repaired_too():
    """A closed posting's derived year still feeds My Applications
    (directory.views._my_applications_context keeps closed rows on a
    student's tracked list rather than dropping them), so scoping this
    command to status='open' would leave that history wrong on purpose."""
    firm = _firm()
    opp = Opportunity.objects.create(
        firm=firm, title="2026 Graduate Programme", status="closed",
        url="https://hsbc.example/job/2", bucket=ENTRY_LEVEL, cohort="2026",
        class_year="", class_year_derived="",
    )
    _run(commit=True)

    opp.refresh_from_db()
    assert opp.class_year_derived == "2026"


# ---------------------------------------------------------------------------
# value -> blank and value -> changed: unsafe, must be reported individually
# and never folded into the safe blank->value count.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_value_to_blank_is_reported_individually_not_swept_in():
    """A stated class_year landing on a row that used to carry only a
    derived one forces the derived column back to blank — ingest's own
    rule, `"" if class_year else derive_class_year(...)[0]`."""
    firm = _firm()
    opp = Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst Program (Class of 2028)",
        status="open", url="https://hsbc.example/job/3", bucket=INTERNSHIP,
        cohort="2027", class_year="2029", class_year_derived="2028",
    )
    out = _run()

    assert "VALUE -> BLANK" in out
    assert "#%d" % opp.id in out
    assert "'2028' -> ''" in out
    assert "BLANK -> VALUE" not in out  # nothing safe in this run

    opp.refresh_from_db()
    assert opp.class_year_derived == "2028"  # dry-run: untouched


@pytest.mark.django_db
def test_value_to_blank_is_written_with_commit():
    firm = _firm()
    opp = Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst Program", status="open",
        url="https://hsbc.example/job/3", bucket=INTERNSHIP, cohort="2027",
        class_year="2029", class_year_derived="2028",
    )
    out = _run(commit=True)

    opp.refresh_from_db()
    assert opp.class_year_derived == ""
    assert "1 value->blank" in out


@pytest.mark.django_db
def test_value_to_changed_is_reported_individually_not_swept_in():
    """The inferred year itself moved (a cohort/title correction) — must
    never be folded into the safe blank->value block."""
    firm = _firm()
    opp = Opportunity.objects.create(
        firm=firm, title="2028 Graduate Programme", status="open",
        url="https://hsbc.example/job/4", bucket=ENTRY_LEVEL, cohort="2028",
        class_year="", class_year_derived="2027",  # stale: pre-correction cohort
    )
    out = _run()

    assert "VALUE -> CHANGED" in out
    assert "'2027' -> '2028'" in out
    assert "BLANK -> VALUE" not in out

    opp.refresh_from_db()
    assert opp.class_year_derived == "2027"  # dry-run: untouched


@pytest.mark.django_db
def test_value_to_changed_is_written_with_commit():
    firm = _firm()
    opp = Opportunity.objects.create(
        firm=firm, title="2028 Graduate Programme", status="open",
        url="https://hsbc.example/job/4", bucket=ENTRY_LEVEL, cohort="2028",
        class_year="", class_year_derived="2027",
    )
    out = _run(commit=True)

    opp.refresh_from_db()
    assert opp.class_year_derived == "2028"
    assert "1 value->changed" in out


# ---------------------------------------------------------------------------
# Idempotency.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_running_twice_is_a_noop_the_second_time():
    firm = _firm()
    opp1 = Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst Program", status="open",
        url="https://hsbc.example/job/1", bucket=INTERNSHIP, cohort="2027",
        class_year="", class_year_derived="",
    )
    opp2 = Opportunity.objects.create(
        firm=firm, title="2028 Graduate Programme", status="open",
        url="https://hsbc.example/job/4", bucket=ENTRY_LEVEL, cohort="2028",
        class_year="", class_year_derived="2027",
    )

    first = _run(commit=True)
    assert "2 repaired" in first  # exactly the 2 rows created above

    second = _run(commit=True)
    assert "Nothing stale" in second

    opp1.refresh_from_db()
    opp2.refresh_from_db()
    assert opp1.class_year_derived == "2028"
    assert opp2.class_year_derived == "2028"


# ---------------------------------------------------------------------------
# Tuple-return handling: a test that would fail if someone compared the
# tuple derive_class_year returns to the stored string instead of its
# first element. This is the exact bug the module docstring describes —
# a first pass at measuring staleness that reported 100% stale because
# every tuple fails `==` against a plain string.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_correctly_populated_row_is_not_flagged_stale():
    """derive_class_year returns a (year, justification) tuple. A row
    already holding the correct STRING must read as up to date — if the
    comparison were done against the raw tuple instead of tuple[0], this
    row would incorrectly show up as stale on every single run."""
    firm = _firm()
    year, _justification = derive_class_year(
        INTERNSHIP, "2027 Summer Analyst Program", "2027")
    assert year == "2028"  # sanity on the fixture itself

    opp = Opportunity.objects.create(
        firm=firm, title="2027 Summer Analyst Program", status="open",
        url="https://hsbc.example/job/5", bucket=INTERNSHIP, cohort="2027",
        class_year="", class_year_derived=year,
    )
    out = _run()

    assert "Nothing stale" in out
    opp.refresh_from_db()
    assert opp.class_year_derived == "2028"


@pytest.mark.django_db
def test_expected_class_year_derived_returns_the_bare_string():
    """Direct unit test of the helper: it must return derive_class_year's
    first tuple element, never the tuple itself."""
    result = expected_class_year_derived(
        INTERNSHIP, "2027 Summer Analyst Program", "2027", "")
    assert result == "2028"
    assert isinstance(result, str)


@pytest.mark.django_db
def test_stated_class_year_always_wins_to_blank_even_when_derivable():
    """Mirrors ingest's own rule: `"" if class_year else
    derive_class_year(...)[0]`. A stated class_year suppresses the derived
    column even though the shape would otherwise derive one."""
    result = expected_class_year_derived(
        INTERNSHIP, "2027 Summer Analyst Program", "2027", "2028")
    assert result == ""


@pytest.mark.django_db
def test_insight_bucket_never_derives_a_year():
    firm = _firm()
    opp = Opportunity.objects.create(
        firm=firm, title="Summer Insight Day 2027", status="open",
        url="https://hsbc.example/job/6", bucket=INSIGHT, cohort="2027",
        class_year="", class_year_derived="",
    )
    out = _run()

    assert "Nothing stale" in out
    opp.refresh_from_db()
    assert opp.class_year_derived == ""


# ---------------------------------------------------------------------------
# --limit is a spot-check knob, not a scoping guarantee — sanity only.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_limit_caps_rows_examined():
    firm = _firm()
    for i in range(3):
        Opportunity.objects.create(
            firm=firm, title="2027 Summer Analyst Program", status="open",
            url=f"https://hsbc.example/job/{i}", bucket=INTERNSHIP,
            cohort="2027", class_year="", class_year_derived="",
        )
    out = _run(limit=1)

    assert "1 row(s) examined" in out
