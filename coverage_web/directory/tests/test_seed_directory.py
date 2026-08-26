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

import subprocess
from pathlib import Path

import pytest
from django.core.management import call_command

from directory.management.commands import seed_directory as cmd
from directory.models import Firm, FirmDate
from directory.seed_parsers import parse_firms_yaml, parse_timeline_yaml

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
