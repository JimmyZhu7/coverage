"""The write path the weekly radar uses to put scanned dates into Coverage.

Before this command existed the radar wrote to a `tracker.md` in a separate
folder that nothing here reads, so a scan could confirm a real deadline and
Coverage would never learn it.

The two rules under test are what make it safe to point a scheduled agent at
this: a weaker claim must never silently overwrite a stronger stored one, and
every observation must survive in history so a date that moved can be
explained.
"""

from __future__ import annotations

import json

import pytest
from django.core.management import call_command

from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db


@pytest.fixture
def run(tmp_path):
    """Call the command with `findings` written to a real file.

    Simpler than driving stdin, and it exercises the same path the scheduled
    agent uses (it writes a file, then points --findings at it).

    `--cycle` is supplied here rather than left to the command, because the
    command no longer has a default to leave it to. It used to default to
    "SA 2028" and that default mislabelled six real Hong Kong deadlines (see
    `import_firm_dates.infer_cycle`); it is now an explicit fallback for
    findings the region+date rule cannot place, which is what every finding
    in this fixture's callers is. Tests that exercise the rule itself pass
    `cycle=None` to turn the fallback off.
    """
    counter = {"n": 0}

    def _run(findings, **opts):
        counter["n"] += 1
        path = tmp_path / f"findings-{counter['n']}.json"
        path.write_text(json.dumps(findings), encoding="utf-8")
        opts.setdefault("cycle", "SA 2028")
        call_command("import_firm_dates", findings=str(path), **opts)

    return _run


@pytest.fixture
def gs():
    return Firm.objects.create(slug="gs", name="Goldman Sachs", regions=["us", "hk"])


def test_a_new_date_is_created_with_its_history(run, gs):
    run([{
        "firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
        "cycle": "SA 2028", "region": "us", "confidence": "confirmed_official",
        "source": "https://gs.example/careers",
    }])
    fd = FirmDate.objects.get(firm=gs, event_kind="app_close")
    assert str(fd.date) == "2026-09-15"
    assert fd.confidence == 1.0
    assert len(fd.history) == 1
    assert fd.history[0]["source"] == "https://gs.example/careers"


def test_a_rumor_never_overwrites_a_confirmed_date(run, gs):
    """Rule 1. The cadence engine acts only on `confirmed_official`, so a bad
    week of scanning must not be able to demote the dates it trusts most."""
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
           "confidence": "confirmed_official"}])
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-11-01",
           "confidence": "rumor"}])

    fd = FirmDate.objects.get(firm=gs, event_kind="app_close")
    assert str(fd.date) == "2026-09-15", "the confirmed date must stand"
    assert fd.confidence == 1.0
    # But the rumor is not thrown away — it is on the record, marked.
    assert len(fd.history) == 2
    assert fd.history[-1]["outcome"] == "not_applied_lower_confidence"
    assert fd.history[-1]["date"] == "2026-11-01"


def test_force_lets_a_retraction_through(run, gs):
    """The escape hatch: sometimes a firm really does walk a date back."""
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
           "confidence": "confirmed_official"}])
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-11-01",
           "confidence": "reported"}], force=True)

    fd = FirmDate.objects.get(firm=gs, event_kind="app_close")
    assert str(fd.date) == "2026-11-01"
    assert fd.confidence == 0.6


def test_a_confirmed_update_moves_the_date_and_keeps_both_observations(run, gs):
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
           "confidence": "reported"}])
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-20",
           "confidence": "confirmed_official"}])

    fd = FirmDate.objects.get(firm=gs, event_kind="app_close")
    assert str(fd.date) == "2026-09-20"
    assert fd.confidence == 1.0
    assert [h["date"] for h in fd.history] == ["2026-09-15", "2026-09-20"]


def test_an_unknown_firm_is_skipped_not_invented(run, gs):
    """The directory is shared by every user. A typo'd slug that auto-created
    a firm would pollute what everyone else reads."""
    run([{"firm": "not-a-real-firm", "event_kind": "app_close",
           "date": "2026-09-15", "confidence": "confirmed_official"}])
    assert Firm.objects.count() == 1
    assert FirmDate.objects.count() == 0


def test_an_unknown_event_kind_is_skipped(run, gs):
    run([{"firm": "gs", "event_kind": "app_clsoe", "date": "2026-09-15",
           "confidence": "confirmed_official"}])
    assert FirmDate.objects.count() == 0


def test_a_blank_date_is_a_legitimate_finding(run, gs):
    """"This event exists and we don't yet know when" is real information —
    it is what the radar reports before a firm publishes."""
    run([{"firm": "gs", "event_kind": "app_open", "date": "",
           "confidence": "reported", "note": "opens after Labor Day"}])
    fd = FirmDate.objects.get(firm=gs, event_kind="app_open")
    assert fd.date is None
    assert fd.history[0]["note"] == "opens after Labor Day"


def test_dry_run_writes_nothing(run, gs):
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
           "confidence": "confirmed_official"}], dry_run=True)
    assert FirmDate.objects.count() == 0


def test_rerunning_the_same_finding_is_idempotent(run, gs):
    """A scheduled agent re-reports what it already reported all the time."""
    finding = [{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
                "confidence": "confirmed_official"}]
    run(finding)
    run(finding)
    assert FirmDate.objects.count() == 1
    fd = FirmDate.objects.get(firm=gs, event_kind="app_close")
    assert str(fd.date) == "2026-09-15"


def test_an_unreadable_date_never_wipes_a_stored_one(run, gs):
    """The bug this guards, reproduced 2026-08-02 against a real row.

    A deliberately blank date and an unparseable one both used to parse to
    None, so a finding carrying "Dec 1 2026" read as "no date known" and
    overwrote a stored confirmed_official deadline with NULL — reported as a
    successful `MOVE ... -> (no date)`. A scheduled agent emitting one
    `12/01/2026` would have destroyed a date the cadence engine acts on.
    """
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
          "confidence": "confirmed_official"}])
    run([{"firm": "gs", "event_kind": "app_close", "date": "Dec 1 2026",
          "confidence": "confirmed_official"}])

    fd = FirmDate.objects.get(firm=gs, event_kind="app_close")
    assert str(fd.date) == "2026-09-15", "the stored date must survive a bad one"
    assert len(fd.history) == 1, "a rejected finding is not an observation"


def test_an_unreadable_date_does_not_create_a_row_either(run, gs):
    run([{"firm": "gs", "event_kind": "app_close", "date": "12/01/2026",
          "confidence": "confirmed_official"}])
    assert FirmDate.objects.count() == 0


def test_a_blank_date_is_still_accepted_after_the_fix(run, gs):
    """The distinction the fix turns on: blank is real information, and must
    keep working. Guarding against over-correction."""
    run([{"firm": "gs", "event_kind": "app_open", "date": "",
          "confidence": "reported"}])
    assert FirmDate.objects.get(firm=gs, event_kind="app_open").date is None


# ---------------------------------------------------------------------------
# Every vocabulary field is checked before it is written
#
# `event_kind` was always matched against a closed tuple. `cycle`, `precision`
# and `confidence` were not, and each silent failure below was reachable with a
# single typo in a findings file a scheduled agent writes unattended.
# ---------------------------------------------------------------------------
def test_a_human_cycle_is_normalised_to_the_stored_vocabulary(run, gs):
    """This command's own `--cycle` default is "SA 2028" while seed_directory
    writes `sa2028_ib`. Both now land on one key, which is what makes a cycle
    groupable across firms at all."""
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
          "cycle": "SA 2028", "region": "us", "confidence": "confirmed_official"}])
    fd = FirmDate.objects.get()
    assert (fd.cycle, fd.track) == ("sa2028", "")


def test_a_desk_suffix_lands_in_the_track_column(run, gs):
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
          "cycle": "sa2028_ib", "region": "us", "confidence": "confirmed_official"}])
    fd = FirmDate.objects.get()
    assert (fd.cycle, fd.track) == ("sa2028", "ib")


def test_an_explicit_track_is_read_too(run, gs):
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
          "cycle": "sa2028", "track": "pe", "confidence": "confirmed_official"}])
    assert FirmDate.objects.get().track == "pe"


def test_a_track_that_contradicts_the_cycle_suffix_is_skipped(run, gs):
    """A finding that says the desk twice, differently, must not have one of
    the two chosen for it."""
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
          "cycle": "sa2028_ib", "track": "pe", "confidence": "confirmed_official"}])
    assert FirmDate.objects.count() == 0


@pytest.mark.parametrize("bad", ["insight", "2028", "next spring", "sa2028_xx"])
def test_an_unreadable_cycle_is_skipped_and_never_written(run, gs, bad):
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
          "cycle": bad, "confidence": "confirmed_official"}])
    assert FirmDate.objects.count() == 0


def test_an_unreadable_cycle_leaves_a_stored_date_alone(run, gs):
    """The same posture `_parse_date` takes: a broken finding costs nothing,
    it does not damage what is already on file."""
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
          "cycle": "sa2028", "confidence": "confirmed_official"}])
    run([{"firm": "gs", "event_kind": "app_close", "date": "2027-01-01",
          "cycle": "insight", "confidence": "confirmed_official"}])
    fd = FirmDate.objects.get()
    assert str(fd.date) == "2026-09-15"


@pytest.mark.parametrize("bad", ["exact", "approx", "quarter"])
def test_an_unknown_precision_is_skipped_rather_than_stored(run, gs, bad):
    """`_firm_date_row` renders an unrecognised precision through its
    EXACT-DAY branch, so a typo here turns a guess into a specific date on a
    public page — the class of bug `firm_dates_confidence_in_range` was added
    for, one column over."""
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
          "cycle": "sa2028", "precision": bad, "confidence": "confirmed_official"}])
    assert FirmDate.objects.count() == 0


@pytest.mark.parametrize("good", ["", "day", "month", "estimated"])
def test_the_precision_vocabulary_is_accepted(run, gs, good):
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
          "cycle": "sa2028", "precision": good, "confidence": "confirmed_official"}])
    assert FirmDate.objects.get().precision == good


def test_an_unknown_confidence_band_is_skipped_not_scored_zero(run, gs):
    """It used to fall back to 0.0. On a new row that writes a real date every
    `>= 0.8` reader downstream discards; on an existing one, the
    never-downgrade rule reads it as a weaker claim and quietly keeps the old
    date. Both look exactly like a successful import."""
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-15",
          "cycle": "sa2028", "confidence": "confirmedofficial"}])
    assert FirmDate.objects.count() == 0


# ---------------------------------------------------------------------------
# The cycle a deadline belongs to, when the finding does not say
#
# `--cycle` used to default to "SA 2028" and a default is not a fact. On the
# 2026-08-02 radar run it stamped `sa2028` on six Hong Kong closes that are
# the SA 2027 intake, and the firm page then badged HSBC's 30 Oct 2026 close
# "your cycle" for a student recruiting for SA 2028. The rule and the research
# it rests on live in `import_firm_dates.infer_cycle`.
# ---------------------------------------------------------------------------

def test_a_hong_kong_close_in_the_autumn_is_the_next_summers_cycle(run, gs):
    """HK runs on the London calendar: apply the autumn before the summer.
    Grade A (scratchpad/research-hongkong.md §1) — MS HK SA 2027 closed
    27 Sep 2026, JPM and BofA 30 Sep 2026, Citi and HSBC 30 Oct 2026."""
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-30",
          "region": "hk", "confidence": "confirmed_official",
          "source": "https://gs.example/hk"}], cycle=None)
    assert FirmDate.objects.get().cycle == "sa2027"


def test_a_us_close_in_the_autumn_is_two_summers_out(run, gs):
    """US BB/EB first post a median 17-18 months before a June start
    (scratchpad/research-us-ib-calendar.md Rule 2), so an autumn close in
    2026 belongs to the SA 2028 intake, not SA 2027 — those classes were
    filled by the spring of 2026."""
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-10-15",
          "region": "us", "confidence": "confirmed_official",
          "source": "https://gs.example/us"}], cycle=None)
    assert FirmDate.objects.get().cycle == "sa2028"


def test_the_default_that_mislabelled_six_hong_kong_rows_is_now_refused(run, gs):
    """The exact 2026-08-02 shape: a HK close with no cycle of its own and a
    `--cycle SA 2028` on the command line. The market and the month place it
    in SA 2027, so a fallback that says otherwise is a contradiction rather
    than a fallback. Nothing is written and the firm is named."""
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-10-30",
          "region": "hk", "confidence": "confirmed_official"}], cycle="SA 2028")
    assert FirmDate.objects.count() == 0


def test_a_cycle_stated_on_the_finding_still_wins(run, gs):
    """P2: stated beats derived. A firm that genuinely moved its calendar is
    evidence, and the disagreement is printed rather than swallowed."""
    run([{"firm": "gs", "event_kind": "app_close", "date": "2026-09-30",
          "region": "hk", "cycle": "sa2028", "confidence": "confirmed_official"}],
        cycle=None)
    assert FirmDate.objects.get().cycle == "sa2028"


def test_no_cycle_and_nothing_to_infer_from_is_skipped_not_guessed(run, gs):
    """P1: silence beats a confident guess. `cycle` is part of this row's
    unique key, so a guess here does not merely mislabel the row, it files it
    under the wrong one."""
    run([{"firm": "gs", "event_kind": "app_open", "date": "2027-03-01",
          "region": "us", "confidence": "reported"}], cycle=None)
    assert FirmDate.objects.count() == 0


@pytest.mark.parametrize("region,event_kind,day,expected", [
    # Only closes, only the two markets the research measured, only inside
    # the month bands it measured them in.
    ("hk", "app_close", "2026-07-01", "sa2027"),
    ("hk", "app_close", "2026-10-31", "sa2027"),
    ("hk", "app_close", "2026-06-30", ""),      # before the HK window
    ("hk", "app_close", "2026-11-01", ""),      # after it
    ("us", "app_close", "2026-08-01", "sa2028"),
    ("us", "app_close", "2026-12-31", "sa2028"),
    ("us", "app_close", "2027-01-19", ""),      # the mid-January cluster is
                                                # Rule 4, a different rule
    ("hk", "app_open", "2026-07-07", ""),       # an opening is a distribution
    ("sg", "app_close", "2026-09-30", ""),      # a market the rule never measured
    ("", "app_close", "2026-09-30", ""),        # no market stated
])
def test_the_inference_rule_says_nothing_outside_its_evidence(
        region, event_kind, day, expected):
    from datetime import date as _date

    from directory.management.commands.import_firm_dates import infer_cycle

    y, m, d = (int(p) for p in day.split("-"))
    assert infer_cycle(region, event_kind, _date(y, m, d)) == expected


def test_the_inference_rule_says_nothing_about_a_dateless_finding():
    from directory.management.commands.import_firm_dates import infer_cycle

    assert infer_cycle("hk", "app_close", None) == ""
