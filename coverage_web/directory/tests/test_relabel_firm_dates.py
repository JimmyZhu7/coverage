"""`relabel_firm_dates` — the one-off repair of the three things wrong with
the stored firm dates, and the ordering that keeps the repair from eating the
data it is repairing.

The live shapes it was written against, measured 2026-09-01:

  * six Hong Kong closes stamped `sa2028` by `import_firm_dates`' old
    `--cycle "SA 2028"` default, all of them the SA 2027 HK intake (Grade A,
    scratchpad/research-hongkong.md §1);
  * two `confidence=1.0` rows with no source, no market and no cycle, one of
    which was the second item on the founder's Today rail with two phone
    alarms behind it;
  * seed files re-dated from the same research, whose HK `app_close` rows want
    exactly the key those six mislabelled rows are sitting on.

The third bullet is why the command has an order and why that order is tested:
seeding before relabelling would overwrite Morgan Stanley's genuine 27 Sep
2026 deadline with a 2027 estimate.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.core.management import call_command

from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db


def _firm(slug="hsbc", name="HSBC"):
    return Firm.objects.create(slug=slug, name=name)


def _date(firm, **kw):
    kw.setdefault("cycle", "sa2028")
    kw.setdefault("track", "")
    kw.setdefault("region", "hk")
    kw.setdefault("event_kind", "app_close")
    kw.setdefault("date", dt.date(2026, 10, 30))
    kw.setdefault("precision", "")
    kw.setdefault("confidence", 1.0)
    kw.setdefault("source_url", "https://apply.careers.hsbc.com/x")
    kw.setdefault("history", [{"date": "2026-10-30", "confidence": "confirmed_official"}])
    return FirmDate.objects.create(firm=firm, **kw)


@pytest.fixture
def empty_seeds(tmp_path):
    """A seed directory with no firm dates in it.

    Sections 1 and 2 are about rows already in the database and have nothing
    to do with the shipped seeds; without this they would re-seed the real
    `directory/seeds/` files into every fixture that happens to name a firm
    those files also name, and the assertions would be counting somebody
    else's rows."""
    d = tmp_path / "empty-seeds"
    d.mkdir()
    for name, region in (("timeline_hk.yaml", "hk"), ("timeline_us.yaml", "us")):
        (d / name).write_text(f"cycle: SA 2028\nregion: {region}\n", encoding="utf-8")
    return d


def _run(capsys, *args, seeds=None, **opts):
    call_command("relabel_firm_dates", *args, seeds=str(seeds), **opts)
    return capsys.readouterr()


# ---------------------------------------------------------------------------
# 1. Cycle relabels
# ---------------------------------------------------------------------------

def test_a_hong_kong_autumn_close_labelled_sa2028_is_moved_to_sa2027(capsys, empty_seeds):
    """HSBC id 40, exactly: "Application close: 30 October 2026" is the SA
    2027 Hong Kong intake, and it was filed under the cycle the founder is
    recruiting for."""
    fd = _date(_firm())
    _run(capsys, "--apply", seeds=empty_seeds)
    fd.refresh_from_db()
    assert fd.cycle == "sa2027"
    assert fd.date == dt.date(2026, 10, 30), "the date itself is not in question"


def test_the_relabel_is_recorded_in_history(capsys, empty_seeds):
    fd = _date(_firm())
    before = len(fd.history)
    _run(capsys, "--apply", seeds=empty_seeds)
    fd.refresh_from_db()
    assert len(fd.history) == before + 1
    assert fd.history[-1]["outcome"] == "cycle_relabelled"
    assert "sa2027" in fd.history[-1]["note"]


def test_a_dry_run_reports_the_six_and_writes_nothing(capsys, empty_seeds):
    fd = _date(_firm())
    out = _run(capsys, seeds=empty_seeds).out
    assert "sa2028   -> sa2027" in out
    assert "Nothing was written" in out
    fd.refresh_from_db()
    assert fd.cycle == "sa2028"


def test_a_row_already_on_the_right_cycle_is_left_alone(capsys, empty_seeds):
    fd = _date(_firm(), cycle="sa2027")
    _run(capsys, "--apply", seeds=empty_seeds)
    fd.refresh_from_db()
    assert fd.cycle == "sa2027"
    assert len(fd.history) == 1, "no observation for a no-op"


def test_a_us_autumn_close_is_two_summers_out_and_already_right(capsys, empty_seeds):
    """`infer_cycle` reads US differently from HK, so the US rows in the same
    table must not be dragged along. MLT's 1 Aug 2026 close is SA 2028."""
    fd = _date(_firm("mlt", "MLT"), region="us", date=dt.date(2026, 8, 1))
    _run(capsys, "--apply", seeds=empty_seeds)
    fd.refresh_from_db()
    assert fd.cycle == "sa2028"


def test_a_row_outside_the_rule_is_never_touched(capsys, empty_seeds):
    """An OPENING is a distribution, not a rule, and a market the research
    never measured is not a market this command has an opinion about."""
    firm = _firm()
    opening = _date(firm, event_kind="app_open", date=dt.date(2027, 9, 1),
                    precision="estimated", confidence=0.6)
    other = _date(_firm("dbs", "DBS"), region="sg", date=dt.date(2026, 9, 30))
    _run(capsys, "--apply", seeds=empty_seeds)
    opening.refresh_from_db()
    other.refresh_from_db()
    assert opening.cycle == "sa2028"
    assert other.cycle == "sa2028"


def test_a_relabel_that_would_collide_is_skipped_and_named(capsys, empty_seeds):
    """`cycle` is part of the unique key, so this is a move. Two rows for one
    scope is a question about which date is right, and a relabelling command
    does not get to answer it."""
    firm = _firm()
    wrong = _date(firm, cycle="sa2028", date=dt.date(2026, 10, 30))
    _date(firm, cycle="sa2027", date=dt.date(2026, 10, 15))
    res = _run(capsys, "--apply", seeds=empty_seeds)
    wrong.refresh_from_db()
    assert wrong.cycle == "sa2028", "left alone rather than merged"
    assert "SKIPPED" in res.err
    assert FirmDate.objects.count() == 2


# ---------------------------------------------------------------------------
# 2. Unverifiable confirmed dates
# ---------------------------------------------------------------------------

def _unverifiable(firm, **kw):
    kw.setdefault("cycle", "")
    kw.setdefault("region", "")
    kw.setdefault("source_url", "")
    kw.setdefault("confidence", 1.0)
    kw.setdefault("date", dt.date(2026, 9, 22))
    return _date(firm, **kw)


def test_an_unsourced_unscoped_confirmed_date_is_downgraded_not_deleted(capsys, empty_seeds):
    """gs id 48. The date is kept — somebody saw something — but the claim
    that a firm published it is not."""
    fd = _unverifiable(_firm("gs", "Goldman Sachs"))
    out = _run(capsys, "--apply", seeds=empty_seeds).out
    assert "unverifiable, confidence should be estimated" in out
    fd.refresh_from_db()
    assert fd.confidence == 0.3
    assert fd.date == dt.date(2026, 9, 22)
    assert fd.history[-1]["outcome"] == "downgraded_unverifiable"


def test_the_downgraded_row_leaves_the_calendar_and_the_rail(capsys, empty_seeds):
    """The whole point. `crm.utils.confirmed_firm_dates` is the one bar the
    grid, the .ics feed and the deadlines rail share, and a 1.0 with no source
    behind it cleared it."""
    from crm.utils import confirmed_firm_dates

    _unverifiable(_firm("gs", "Goldman Sachs"))
    assert confirmed_firm_dates().count() == 1
    _run(capsys, "--apply", seeds=empty_seeds)
    assert confirmed_firm_dates().count() == 0


def test_a_confirmed_date_with_a_source_is_left_alone(capsys, empty_seeds):
    fd = _date(_firm(), cycle="", region="",
               source_url="https://apply.careers.hsbc.com/x")
    _run(capsys, "--apply", seeds=empty_seeds)
    fd.refresh_from_db()
    assert fd.confidence == 1.0


def test_a_confirmed_date_that_states_its_market_is_left_alone(capsys, empty_seeds):
    """The three blanks together are what make a row unverifiable. A sourceless
    row that at least says which market and which cycle it is about is a
    different, weaker problem and is not this command's business."""
    fd = _date(_firm(), source_url="", region="hk", cycle="sa2027")
    _run(capsys, "--apply", seeds=empty_seeds)
    fd.refresh_from_db()
    assert fd.confidence == 1.0


def test_a_dry_run_downgrades_nothing(capsys, empty_seeds):
    fd = _unverifiable(_firm("gs", "Goldman Sachs"))
    _run(capsys, seeds=empty_seeds)
    fd.refresh_from_db()
    assert fd.confidence == 1.0


# ---------------------------------------------------------------------------
# 3. Seeded estimates, and the ordering that protects the real dates
# ---------------------------------------------------------------------------

_HK_SEED = """cycle: SA 2028
region: hk
phases:
- id: apps_open
  name: HK SA applications open
  start: 2027-07-01
  end: 2027-10-31
  focus: window

firm_dates:
- key: ms/sa2028_hk/app_close
  date: 2027-09
  precision: estimated
  confidence: reported
  source: 'research:hongkong'
  found: '2026-09-01'
  note: MS HK SA 2027 closed 27 Sep 2026 (Grade A).
"""

_US_SEED = """cycle: SA 2028
region: us

firm_dates:
- key: gs/sa2028_ib/app_open
  date: 2027-01
  precision: estimated
  confidence: reported
  source: 'research:us-ib-calendar'
  found: '2026-09-01'
  note: 17 months before June 2028.
"""


@pytest.fixture
def seeds(tmp_path):
    (tmp_path / "timeline_hk.yaml").write_text(_HK_SEED, encoding="utf-8")
    (tmp_path / "timeline_us.yaml").write_text(_US_SEED, encoding="utf-8")
    return tmp_path


def test_the_real_hong_kong_deadline_survives_the_reseed(capsys, seeds):
    """The ordering bug this command has to not have. Morgan Stanley's SA 2027
    close sits at (ms, sa2028, "", hk, app_close) — the exact key the SA 2028
    seed estimate wants. Relabelling runs first, so the seed lands beside the
    real date instead of on top of it."""
    ms = _firm("ms", "Morgan Stanley")
    real = _date(ms, date=dt.date(2026, 9, 27), confidence=0.6,
                 source_url="https://morganstanley.tal.net/x")

    _run(capsys, "--apply", seeds=str(seeds))

    real.refresh_from_db()
    assert real.cycle == "sa2027"
    assert real.date == dt.date(2026, 9, 27), "the Grade-A deadline is untouched"
    estimate = FirmDate.objects.get(firm=ms, cycle="sa2028", event_kind="app_close")
    assert estimate.date == dt.date(2027, 9, 1)
    assert estimate.precision == "estimated"


def test_the_dry_run_shows_the_seed_as_new_not_as_an_overwrite(capsys, seeds):
    """A dry run that described the seed as overwriting a real date would be
    describing a database the apply path never produces."""
    _date(_firm("ms", "Morgan Stanley"), date=dt.date(2026, 9, 27),
          confidence=0.6, source_url="https://morganstanley.tal.net/x")
    out = _run(capsys, seeds=str(seeds)).out
    assert "(new)" in out
    assert "2026-09-27   -> 2027-09-01" not in out


def test_a_seeded_estimate_never_reaches_the_calendar(capsys, seeds):
    """P1 in the shape that matters here: a forecast is allowed on the firm
    page and nowhere a countdown lives. `precision: estimated` is what keeps
    it out, on every one of these rows."""
    from crm.utils import confirmed_firm_dates

    _firm("ms", "Morgan Stanley")
    _firm("gs", "Goldman Sachs")
    _run(capsys, "--apply", seeds=str(seeds))
    assert FirmDate.objects.filter(precision="estimated").count() == 2
    assert confirmed_firm_dates().count() == 0


def test_the_us_opening_moves_to_its_measured_lead_time(capsys, seeds):
    gs = _firm("gs", "Goldman Sachs")
    old = _date(gs, cycle="sa2028", track="ib", region="us",
                event_kind="app_open", date=dt.date(2027, 3, 1),
                precision="estimated", confidence=0.6,
                source_url="seed:historical-pattern")
    _run(capsys, "--apply", seeds=str(seeds))
    old.refresh_from_db()
    assert old.date == dt.date(2027, 1, 1)
    assert old.source_url == "research:us-ib-calendar"


def test_a_dry_run_seeds_nothing(capsys, seeds):
    _firm("ms", "Morgan Stanley")
    _firm("gs", "Goldman Sachs")
    _run(capsys, seeds=str(seeds))
    assert FirmDate.objects.count() == 0


def test_the_reseed_still_refuses_to_downgrade_a_confirmed_row(capsys, seeds):
    """Section 3 hands the writing to `seed_directory._seed_firm_dates`, so it
    inherits that command's never-downgrade rule rather than carrying a second
    copy of it."""
    gs = _firm("gs", "Goldman Sachs")
    confirmed = _date(gs, cycle="sa2028", track="ib", region="us",
                      event_kind="app_open", date=dt.date(2027, 1, 15),
                      precision="day", confidence=1.0,
                      source_url="https://higher.gs.com/roles/1")
    _run(capsys, "--apply", seeds=str(seeds))
    confirmed.refresh_from_db()
    assert confirmed.confidence == 1.0
    assert confirmed.date == dt.date(2027, 1, 15)
