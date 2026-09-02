"""`seed_directory`: the step that makes a fresh deploy non-empty.

The bug these guard against was invisible from inside a developer checkout.
`docs/deploy.md` §2 tells a fresh Render shell to run `seed_directory`, the
command read `data/seeds/firms.yaml`, and `.gitignore` excluded the whole
`data/` directory under its "this repo is PUBLIC" block. So the file existed on
the founder's machine and nowhere else: on Render the command printed "firms
file not found", returned, and the app came up with zero firms and zero firm
dates — while the command's own docstring asserted that "a fresh clone can seed
itself without any external directory existing".

Every test below is written against that failure mode. The first section is the
one that actually catches it: the seeds must be TRACKED, not merely present.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import pytest
from django.core.management import call_command

from directory.management.commands import seed_directory as cmd
from directory.models import Firm, FirmDate
from directory.seed_parsers import (
    parse_firms_yaml,
    parse_timeline_phases,
    parse_timeline_yaml,
)

SEEDS = Path(cmd.__file__).resolve().parents[2] / "seeds"
SEED_FILES = ("firms.yaml", "timeline_us.yaml", "timeline_hk.yaml")


def _firm_rows() -> list[dict]:
    return parse_firms_yaml((SEEDS / "firms.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The seeds ship with the code
# ---------------------------------------------------------------------------
def test_the_command_defaults_to_the_packaged_seeds():
    """Not `data/`. The default path is the whole fix — a caller who passes no
    flags on a fresh deploy has to land on files git carries."""
    assert cmd._DEFAULT_SEEDS == SEEDS
    assert "data" not in SEEDS.parts[-3:]


@pytest.mark.parametrize("name", SEED_FILES)
def test_seed_file_exists(name):
    assert (SEEDS / name).is_file()


@pytest.mark.parametrize("name", SEED_FILES)
def test_seed_file_is_tracked_by_git(name):
    """THE regression test. `exists()` passed on the founder's machine for
    months while a fresh clone had nothing — only git's own index can tell the
    two apart, so ask it."""
    path = SEEDS / name
    try:
        out = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(path)],
            cwd=path.parent, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        pytest.skip("git unavailable")
    if "not a git repository" in out.stderr:  # pragma: no cover - tarball
        pytest.skip("not a git checkout")
    assert out.returncode == 0, (
        f"{path} is not tracked by git — a fresh clone would not have it, "
        f"which is exactly the bug this file exists for. git said: {out.stderr.strip()}"
    )


# ---------------------------------------------------------------------------
# What was stripped on the way in
# ---------------------------------------------------------------------------
def test_no_firm_carries_a_tier():
    """`tier` is the founder's hand-curated ranking of employers and the reason
    `data/` was not simply un-ignored. It was already discarded on import, so
    the tracked copy must not carry it into a public repo."""
    assert [r["id"] for r in _firm_rows() if "tier" in r] == []


def test_the_tracked_firms_file_holds_no_prestige_prose():
    """The private copy reasons in comments about which banks are prestigious
    and which moved up or down a tier. Those paragraphs are the other half of
    what the ignore rule protects."""
    text = (SEEDS / "firms.yaml").read_text(encoding="utf-8").lower()
    for phrase in ("tier 1", "tier 2", "tier 3", "prestige", "hand-picked",
                   "hand-curat", "target list", "second pass"):
        assert phrase not in text, f"{phrase!r} leaked into the tracked seeds"


# ---------------------------------------------------------------------------
# Internal consistency
# ---------------------------------------------------------------------------
def test_firm_ids_are_unique_and_named():
    rows = _firm_rows()
    ids = [r.get("id") for r in rows]
    assert all(ids), "every row needs an id — it is the natural key"
    assert len(set(ids)) == len(ids)
    assert all(str(r.get("name", "")).strip() for r in rows)


def test_every_timeline_entry_points_at_a_firm_that_exists():
    """A `key:` whose firm half has no row is silently counted as skipped by
    the command, so a typo would cost a date with no error anywhere."""
    known = {r["id"] for r in _firm_rows()}
    for name in ("timeline_us.yaml", "timeline_hk.yaml"):
        _, _, entries = parse_timeline_yaml((SEEDS / name).read_text(encoding="utf-8"))
        assert entries, f"{name} parsed to no firm_dates at all"
        for entry in entries:
            slug = str(entry.get("key", "")).split("/")[0]
            assert slug in known, f"{name}: {entry.get('key')!r} names no firm"


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_seeding_a_fresh_database_fills_both_tables():
    """The end the deploy actually cares about: no flags, empty database, and
    afterwards `/opportunities/` has firms to show."""
    assert not Firm.objects.exists()
    call_command("seed_directory", verbosity=0)

    assert Firm.objects.count() == len(_firm_rows())
    assert FirmDate.objects.exists()
    gs = Firm.objects.get(slug="gs")
    assert gs.name == "Goldman Sachs"
    assert "gs.com" in gs.domains
    assert gs.sponsors == {"us": True, "hk": True}


@pytest.mark.django_db
def test_no_timeline_entry_is_skipped():
    """`skipped` counts entries whose firm or key shape did not resolve. The
    shipped corpus must resolve completely, or the seeds disagree with
    themselves."""
    call_command("seed_directory", verbosity=0)
    expected = sum(
        len(parse_timeline_yaml((SEEDS / n).read_text(encoding="utf-8"))[2])
        for n in ("timeline_us.yaml", "timeline_hk.yaml")
    )
    assert FirmDate.objects.count() == expected


@pytest.mark.django_db
def test_running_twice_duplicates_nothing():
    call_command("seed_directory", verbosity=0)
    firms, dates = Firm.objects.count(), FirmDate.objects.count()
    call_command("seed_directory", verbosity=0)
    assert (Firm.objects.count(), FirmDate.objects.count()) == (firms, dates)


@pytest.mark.django_db
def test_dry_run_writes_nothing():
    call_command("seed_directory", "--dry-run", verbosity=0)
    assert not Firm.objects.exists()
    assert not FirmDate.objects.exists()


@pytest.mark.django_db
def test_a_missing_firms_file_reports_and_writes_nothing(tmp_path):
    """Still an error rather than a warning: seeding nothing empties the app,
    and this failure already survived all the way to a deploy once."""
    call_command("seed_directory", "--firms-file", str(tmp_path / "nope.yaml"),
                 verbosity=0)
    assert not Firm.objects.exists()


# ---------------------------------------------------------------------------
# The retired private copy
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_drifted_legacy_copy_is_reported_but_not_read(tmp_path, monkeypatch, capsys):
    """`data/seeds/` is an archive now. Editing it and expecting the deploy to
    notice is the mistake that produced the mail-domain bug, so a divergence is
    surfaced — and the archive's own rows still never reach the database."""
    legacy = tmp_path / "seeds"
    legacy.mkdir()
    (legacy / "firms.yaml").write_text(
        "firms:\n"
        "  - {id: ghost, name: Ghost Bank, tier: 1, tracks: [ib], regions: [us], "
        "status: active, domains: [ghost.example]}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cmd, "_LEGACY_SEEDS", legacy)

    call_command("seed_directory", verbosity=0)

    assert "drifted" in capsys.readouterr().err
    assert not Firm.objects.filter(slug="ghost").exists()


@pytest.mark.django_db
def test_an_absent_legacy_copy_says_nothing(tmp_path, monkeypatch, capsys):
    """The normal case everywhere but the founder's laptop."""
    monkeypatch.setattr(cmd, "_LEGACY_SEEDS", tmp_path / "does-not-exist")
    call_command("seed_directory", verbosity=0)
    assert "drifted" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# A RE-SEED IS NOT AN ERASER
#
# `_seed_firm_dates` used to end in a plain
# `update_or_create(defaults={date, precision, confidence, source_url,
# found_on, history})`. Every seed in this repo is `confidence: reported`
# (0.6), so a row the weekly radar had since upgraded to `confirmed_official`
# off the firm's own posting was silently demoted to a 2026-07-03 guess the
# next time anyone ran `seed_directory` — and the `history` that would have
# explained the move was replaced in the same save, so nothing was left to
# notice it by. `import_firm_dates` has refused both of those since it was
# written; this is the same table's other writer.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_re_seed_never_downgrades_a_confirmed_row():
    """The latent shape: a seed-keyed row the radar has since confirmed."""
    call_command("seed_directory", verbosity=0)
    row = FirmDate.objects.get(firm__slug="gs", region="hk", event_kind="app_open")
    row.date = dt.date(2027, 7, 1)
    row.precision = "day"
    row.confidence = 1.0
    row.source_url = "https://higher.gs.com/roles/170773"
    row.save()

    call_command("seed_directory", verbosity=0)

    row.refresh_from_db()
    assert row.confidence == 1.0, "the confirmed date must stand"
    assert str(row.date) == "2027-07-01"
    assert row.source_url == "https://higher.gs.com/roles/170773"


@pytest.mark.django_db
def test_the_rejected_seed_is_recorded_rather_than_dropped():
    """P4: mark, never drop. The seed still happened and belongs on the
    record, marked with why it was not applied — the same `outcome` key
    `import_firm_dates` writes."""
    call_command("seed_directory", verbosity=0)
    row = FirmDate.objects.get(firm__slug="gs", region="hk", event_kind="app_open")
    before = len(row.history or [])
    row.confidence = 1.0
    row.save()

    call_command("seed_directory", verbosity=0)

    row.refresh_from_db()
    assert len(row.history) == before + 1
    assert row.history[-1]["outcome"] == "not_applied_lower_confidence"


@pytest.mark.django_db
def test_history_is_appended_not_replaced_when_a_seed_changes_a_row():
    call_command("seed_directory", verbosity=0)
    row = FirmDate.objects.get(firm__slug="gs", region="hk", event_kind="app_open")
    before = list(row.history or [])
    assert before, "the first seed writes its own observation"
    row.date = dt.date(2030, 1, 1)
    row.save()

    call_command("seed_directory", verbosity=0)

    row.refresh_from_db()
    assert len(row.history) == len(before) + 1
    assert row.history[:len(before)] == before, "append-only"


@pytest.mark.django_db
def test_an_unchanged_re_seed_writes_no_new_history():
    """A re-seed is the same 2026-07-03 note being read again, not a new
    observation of the world. History that grows on every deploy is history
    nobody can read."""
    call_command("seed_directory", verbosity=0)
    lengths = {r.pk: len(r.history or []) for r in FirmDate.objects.all()}
    call_command("seed_directory", verbosity=0)
    assert {r.pk: len(r.history or []) for r in FirmDate.objects.all()} == lengths


# ---------------------------------------------------------------------------
# The `phases:` block is read, and its shape is reported rather than flattened
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_phases_block_is_reported_instead_of_silently_dropped(capsys):
    """`seed_parsers` called `phases:` "an ignored block". It is the only
    place either seed file states a WINDOW, and `FirmDate.date` is one
    `DateField` — so it is read and named, and explicitly not stored."""
    call_command("seed_directory")
    out = capsys.readouterr().out
    assert "phase (not stored" in out
    assert "apps_open" in out


def test_the_phases_parser_reads_the_hong_kong_applications_window():
    phases = parse_timeline_phases(
        (SEEDS / "timeline_hk.yaml").read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in phases}
    assert set(by_id) == {"relationship_building", "spring_insights",
                          "apps_open", "interviews_offers"}
    assert by_id["apps_open"]["start"] == "2027-07-01"
    assert by_id["apps_open"]["end"] == "2027-10-31"


@pytest.mark.parametrize("name", ("timeline_us.yaml", "timeline_hk.yaml"))
def test_the_phases_parser_stops_at_the_next_top_level_block(name):
    """`phases:` is followed by `firm_dates:`, so a parser that ran to the end
    of the file would return every firm date as a phase."""
    phases = parse_timeline_phases((SEEDS / name).read_text(encoding="utf-8"))
    assert phases
    assert all("key" not in p for p in phases)
    assert all("id" in p for p in phases)
