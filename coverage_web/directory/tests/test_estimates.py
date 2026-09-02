"""WS-CRM-08 — the estimate re-check trigger.

`FirmDate` is an assertion, `FirmCycleObservation` is a measurement, and
until this module nothing joined them. The defect
(`audit-calendar-firmdates.md §6` and D10): 25 estimated rows all carry
`found_on` 2026-07-03, nothing re-checks them, and the firm page renders the
guess forever with no date on it, while a declared date the scraper flatly
contradicts is never flagged anywhere.

The two live contradictions, measured 2026-09-02 read-only and reproduced in
`test_todays_contradictions_are_exactly_two` below: Nomura HK declares
`app_open` 2026-09-01 against six postings observed opening 3 to 20 August,
and UBS HK declares a close of 2026-08-03 against seven trusted closes
observed 19 to 26 August.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from directory import estimates
from directory.models import Firm, FirmCycleObservation, FirmDate

pytestmark = pytest.mark.django_db


def _firm(slug="nomura", name="Nomura"):
    return Firm.objects.create(slug=slug, name=name, regions=["hk"])


def _estimate(firm, date, *, region="hk", kind="app_open",
              found_on=dt.date(2026, 7, 3)):
    return FirmDate.objects.create(
        firm=firm, cycle="sa2028", region=region, event_kind=kind, date=date,
        precision="estimated", confidence=0.6,
        found_on=timezone.make_aware(dt.datetime.combine(found_on, dt.time())),
        source_url="seed:historical-pattern",
    )


def _declared(firm, date, *, region="hk", kind="app_open", confidence=1.0):
    return FirmDate.objects.create(
        firm=firm, cycle="sa2028", region=region, event_kind=kind, date=date,
        precision="", confidence=confidence,
        found_on=timezone.now(),
    )


def _obs(firm, *, region="hk", opened=0, o_first=None, o_last=None,
         closed=0, c_first=None, c_last=None):
    return FirmCycleObservation.objects.create(
        firm=firm, region=region,
        opened_count=opened, open_window_first=o_first, open_window_last=o_last,
        closed_count=closed, close_window_first=c_first, close_window_last=c_last,
    )


# ---------------------------------------------------------------------------
# Superseded: an estimate the board has overtaken.
# ---------------------------------------------------------------------------
def test_an_estimate_is_superseded_by_a_wave_in_its_own_cohort():
    """The acceptance case: an estimated open of 2027-09 with observed opens
    in 2027-07 renders as superseded, with BOTH windows and both sources."""
    firm = _firm()
    fd = _estimate(firm, dt.date(2027, 9, 1))
    obs = _obs(firm, opened=14, o_first=dt.date(2027, 7, 6),
               o_last=dt.date(2027, 7, 22))
    note = estimates.superseded_by(fd, obs)
    assert note == {
        "observed": "Jul 6 to Jul 22",
        "observed_count": 14,
        "estimated": dt.date(2027, 9, 1),
        "found_on": dt.date(2026, 7, 3),
    }


def test_the_previous_cycles_wave_never_supersedes():
    """A wave roughly a year away is a different cohort. Measured on today's
    data: every estimate on file is 187 to 371 days from the nearest observed
    opening, and none of them is superseded."""
    firm = _firm()
    fd = _estimate(firm, dt.date(2027, 9, 1))
    obs = _obs(firm, opened=91, o_first=dt.date(2026, 8, 14),
               o_last=dt.date(2026, 8, 25))
    assert estimates.superseded_by(fd, obs) is None


def test_a_thin_observation_supersedes_nothing():
    """P3: below `CYCLE_OBSERVATION_MIN_SAMPLE` the estimate stands alone,
    exactly as today."""
    firm = _firm()
    fd = _estimate(firm, dt.date(2027, 9, 1))
    obs = _obs(firm, opened=2, o_first=dt.date(2027, 7, 6),
               o_last=dt.date(2027, 7, 22))
    assert estimates.superseded_by(fd, obs) is None


def test_a_declared_date_is_never_superseded():
    """A date somebody read off a firm's page is not overtaken by evidence,
    it is either right or contradicted."""
    firm = _firm()
    fd = _declared(firm, dt.date(2027, 9, 1))
    obs = _obs(firm, opened=14, o_first=dt.date(2027, 7, 6),
               o_last=dt.date(2027, 7, 22))
    assert estimates.superseded_by(fd, obs) is None


def test_an_estimate_is_never_overwritten():
    """P1: the two facts sit side by side with their provenance. Nothing in
    this module writes."""
    firm = _firm()
    fd = _estimate(firm, dt.date(2027, 9, 1))
    obs = _obs(firm, opened=14, o_first=dt.date(2027, 7, 6),
               o_last=dt.date(2027, 7, 22))
    estimates.superseded_by(fd, obs)
    estimates.annotate(firm)
    fd.refresh_from_db()
    assert fd.date == dt.date(2027, 9, 1)
    assert fd.precision == "estimated"
    assert fd.confidence == 0.6


# ---------------------------------------------------------------------------
# Contradicted: a declared date the whole observed wave sits the wrong side of.
# ---------------------------------------------------------------------------
def test_a_declared_open_the_whole_wave_predates_is_contradicted():
    """Nomura HK, live."""
    firm = _firm()
    fd = _declared(firm, dt.date(2026, 9, 1))
    obs = _obs(firm, opened=6, o_first=dt.date(2026, 8, 3),
               o_last=dt.date(2026, 8, 20))
    clash = estimates.contradicted_by(fd, obs)
    assert clash is not None
    assert "2026-09-01" in clash
    assert "Aug 3 to Aug 20" in clash
    assert "6 postings" in clash


def test_a_declared_close_the_whole_wave_postdates_is_contradicted():
    """UBS HK, live."""
    firm = _firm(slug="ubs", name="UBS")
    fd = _declared(firm, dt.date(2026, 8, 3), kind="app_close")
    obs = _obs(firm, closed=7, c_first=dt.date(2026, 8, 19),
               c_last=dt.date(2026, 8, 26))
    clash = estimates.contradicted_by(fd, obs)
    assert clash is not None
    assert "Aug 19 to Aug 26" in clash


def test_a_wave_that_straddles_the_declared_date_is_not_a_contradiction():
    """Goldman US, live: declares 2026-08-15 against a wave running Jul 31 to
    Aug 26. A board carries roles that are not the programme the date is
    about, and an early single posting is a fact about that requisition."""
    firm = _firm(slug="gs", name="Goldman Sachs")
    fd = _declared(firm, dt.date(2026, 8, 15), region="us")
    obs = _obs(firm, region="us", opened=87, o_first=dt.date(2026, 7, 31),
               o_last=dt.date(2026, 8, 26))
    assert estimates.contradicted_by(fd, obs) is None


def test_one_posting_a_day_early_is_not_a_contradiction():
    """PIMCO US, live: declares 2026-08-15 against a wave running Aug 14 to
    Aug 25."""
    firm = _firm(slug="pimco", name="PIMCO")
    fd = _declared(firm, dt.date(2026, 8, 15), region="us", confidence=0.3)
    obs = _obs(firm, region="us", opened=23, o_first=dt.date(2026, 8, 14),
               o_last=dt.date(2026, 8, 25))
    assert estimates.contradicted_by(fd, obs) is None


def test_a_posting_closing_before_a_declared_close_is_ordinary():
    firm = _firm()
    fd = _declared(firm, dt.date(2026, 9, 30), kind="app_close")
    obs = _obs(firm, closed=7, c_first=dt.date(2026, 8, 19),
               c_last=dt.date(2026, 8, 26))
    assert estimates.contradicted_by(fd, obs) is None


def test_an_estimate_is_never_contradicted():
    firm = _firm()
    fd = _estimate(firm, dt.date(2026, 9, 1))
    obs = _obs(firm, opened=6, o_first=dt.date(2026, 8, 3),
               o_last=dt.date(2026, 8, 20))
    assert estimates.contradicted_by(fd, obs) is None


def test_a_region_mismatch_never_pairs():
    """The observation reader keys on (firm, region), so a US observation
    cannot speak about an HK declaration."""
    firm = _firm()
    _declared(firm, dt.date(2026, 9, 1), region="hk")
    _obs(firm, region="us", opened=6, o_first=dt.date(2026, 8, 3),
         o_last=dt.date(2026, 8, 20))
    assert estimates.annotate(firm) == {}


# ---------------------------------------------------------------------------
# The firm page.
# ---------------------------------------------------------------------------
def test_found_on_is_annotated_on_every_estimate():
    firm = _firm()
    fd = _estimate(firm, dt.date(2027, 9, 1))
    assert estimates.annotate(firm)[fd.id]["found_on"] == dt.date(2026, 7, 3)


def test_the_firm_page_prints_the_guessed_on_date(client):
    """The 25 estimated rows on file were all written on 2026-07-03 and the
    page said nothing about it."""
    firm = _firm()
    _estimate(firm, dt.date(2027, 9, 1))
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert "guessed Jul 3, 2026" in body


def test_the_firm_page_prints_the_contradiction(client):
    firm = _firm()
    _declared(firm, dt.date(2026, 9, 1))
    _obs(firm, opened=6, o_first=dt.date(2026, 8, 3), o_last=dt.date(2026, 8, 20))
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert "Coverage watched this board and disagrees" in body
    assert "Aug 3 to Aug 20" in body


def test_a_firm_with_nothing_to_say_says_nothing(client):
    firm = _firm()
    _declared(firm, dt.date(2026, 9, 1))
    body = client.get(f"/firms/{firm.slug}/").content.decode()
    assert "Coverage watched this board and disagrees" not in body
    assert 'title="The day this estimate was written down' not in body
    assert 'class="tl-observed' not in body


# ---------------------------------------------------------------------------
# The operator report.
# ---------------------------------------------------------------------------
def test_the_health_report_names_a_contradiction():
    from directory import health

    firm = _firm()
    _declared(firm, dt.date(2026, 9, 1))
    _obs(firm, opened=6, o_first=dt.date(2026, 8, 3), o_last=dt.date(2026, 8, 20))
    lines = [line for line in health.health_report()
             if "a stated date the board contradicts" in line]
    assert len(lines) == 1
    assert "Nomura" in lines[0]


def test_the_report_is_empty_when_nothing_disagrees():
    firm = _firm()
    _declared(firm, dt.date(2026, 8, 1))
    _obs(firm, opened=6, o_first=dt.date(2026, 8, 3), o_last=dt.date(2026, 8, 20))
    assert estimates.contradiction_report() == []
