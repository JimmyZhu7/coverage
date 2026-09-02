"""`Firm.recruiting_style` (migration directory/0016) and its seed
(directory/0017): the one firm-level fact three networking surfaces read.

The seed is tested by calling the migration's own functions against the
live app registry rather than by replaying migration state — what matters
is that the slug list lands `assessment` on the right rows, leaves the
multi-strat funds alone, and reverses cleanly.
"""

from __future__ import annotations

import importlib
import inspect
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command

from directory.admin import FirmAdmin
from directory.boards import ASSESSMENT_RECRUITING
from directory.models import Firm

seed_migration = importlib.import_module(
    "directory.migrations.0017_seed_assessment_recruiting_style"
)


def test_the_default_is_campus_and_the_choices_are_exactly_two():
    field = Firm._meta.get_field("recruiting_style")
    assert field.default == Firm.RECRUITING_STYLE_CAMPUS == "campus"
    assert [value for value, _ in field.choices] == ["campus", "assessment"]
    assert field.blank is True
    assert "test" in field.help_text.lower()


@pytest.mark.django_db
def test_a_new_firm_is_campus_without_anyone_saying_so():
    firm = Firm.objects.create(slug="newco", name="NewCo")
    firm.refresh_from_db()
    assert firm.recruiting_style == "campus"


def test_the_admin_shows_and_filters_on_it():
    assert "recruiting_style" in FirmAdmin.list_display
    assert "recruiting_style" in FirmAdmin.list_filter


def test_the_migration_docstring_carries_the_evidence_and_names_its_source():
    """REWRITTEN for D-22 (2026-09-02). This used to require every seeded
    slug to appear in the migration's docstring, which was the right check
    while the migration owned its own copy of the list: the docstring was
    the record of what it wrote.

    The migration no longer owns a list — it reads
    `directory.boards.ASSESSMENT_RECRUITING` — so enumerating the slugs
    here would recreate, in prose, the second definition D-22 deleted, and
    would fail the day someone adds a firm to the constant. The evidence
    still has to be in the docstring, and so does the pointer at where the
    slugs actually live."""
    doc = seed_migration.__doc__
    assert "unfortunately, no" in doc
    assert "Datathons" in doc
    assert "doesn't matter who you know" in doc
    assert "directory.boards.ASSESSMENT_RECRUITING" in doc
    for untouched in ("Millennium", "Point72", "AQR"):
        assert untouched in doc


def test_the_seed_reads_the_boards_constant_and_does_not_copy_it():
    """D-22: one definition of `recruiting_style`. The migration's slug
    source must BE `directory.boards.ASSESSMENT_RECRUITING`, not a list
    that happens to equal it today — an equal copy is exactly the state
    that drifted, and a test asserting equality would pass right up until
    the moment someone edited one side."""
    assert set(seed_migration.ASSESSMENT_SLUGS) == set(ASSESSMENT_RECRUITING)
    src = inspect.getsource(seed_migration)
    assert "from directory.boards import ASSESSMENT_RECRUITING" in src
    # No literal slug survives in the migration: every name in the constant
    # must reach it through the import.
    body = src.split('"""', 2)[-1]
    for slug in ASSESSMENT_RECRUITING:
        assert f'"{slug}"' not in body, f"{slug} is spelled out a second time"


def test_the_slug_list_covers_the_request_and_both_spellings():
    slugs = set(seed_migration.ASSESSMENT_SLUGS)
    assert {
        "janestreet", "citadel", "sig", "imc", "jump", "drw", "hrt", "optiver",
        "akuna", "belvedere", "fiverings", "flowtraders", "virtu", "xtx",
        "squarepoint", "qube",
    } <= slugs
    assert {"citadelsecurities", "citadel-securities"} <= slugs
    assert {"tower", "towerresearch"} <= slugs
    for multi_strat in ("millennium", "point72", "aqr"):
        assert multi_strat not in slugs


@pytest.mark.django_db
def test_the_seed_tags_the_listed_slugs_and_nobody_else():
    jane = Firm.objects.create(slug="janestreet", name="Jane Street", tracks=["st"])
    cs = Firm.objects.create(slug="citadel-securities", name="Citadel Securities")
    tower = Firm.objects.create(slug="towerresearch", name="Tower Research")
    mlp = Firm.objects.create(slug="millennium", name="Millennium", tracks=["am", "st"])
    hsbc = Firm.objects.create(slug="hsbc", name="HSBC", tracks=["ib"])

    seed_migration.seed_assessment(apps, None)

    for firm in (jane, cs, tower, mlp, hsbc):
        firm.refresh_from_db()
    assert jane.recruiting_style == "assessment"
    assert cs.recruiting_style == "assessment"
    assert tower.recruiting_style == "assessment"
    assert mlp.recruiting_style == "campus"
    assert hsbc.recruiting_style == "campus"


@pytest.mark.django_db
def test_the_seed_is_idempotent_and_reverses_without_touching_hand_edits():
    jane = Firm.objects.create(slug="janestreet", name="Jane Street")
    by_hand = Firm.objects.create(slug="pointy", name="Pointy", recruiting_style="assessment")

    seed_migration.seed_assessment(apps, None)
    seed_migration.seed_assessment(apps, None)
    jane.refresh_from_db()
    assert jane.recruiting_style == "assessment"

    seed_migration.unseed_assessment(apps, None)
    jane.refresh_from_db()
    by_hand.refresh_from_db()
    assert jane.recruiting_style == "campus"
    assert by_hand.recruiting_style == "assessment", "not on the list, not the seed's to undo"


@pytest.mark.django_db
def test_the_seed_finds_nothing_on_an_empty_board_and_does_not_mind():
    seed_migration.seed_assessment(apps, None)
    assert Firm.objects.count() == 0


# ---------------------------------------------------------------------------
# D-22: one definition, and both writers of it agree.
#
# The column has two writers. A FRESH DEPLOY migrates before any Firm row
# exists, so the seed finds nothing and `scrape`'s catalog pre-create is what
# tags the firms. A MIGRATED DATABASE already had the rows, so migration 0017
# tagged them. Before D-22 those two paths read two different lists and were
# free to disagree; these tests are the assertion that they cannot.
# ---------------------------------------------------------------------------
def _fake_boards(slugs):
    """`select_boards`' return shape: (catalog slug, BoardConfig)."""
    from coverage_connectors import GreenhouseBoard
    return [(slug, GreenhouseBoard(firm=slug.title(), token=slug)) for slug in slugs]


def _run_scrape(monkeypatch, slugs):
    """`scrape` with the network and the ingest service taken out: this
    exercises the firm pre-create and backfill block and nothing else."""
    from directory.management.commands import scrape as scrape_cmd

    monkeypatch.setattr(scrape_cmd, "select_boards", lambda **kw: _fake_boards(slugs))

    class _Run:
        id = 1
        status = "ok"
        stats = {
            "boards_ok": 0, "boards_total": 0, "fetched": 0, "created": 0,
            "updated": 0, "unchanged": 0, "reopened": 0, "closed": 0,
            "changes_recorded": 0, "created_firms": [], "errors": {},
            "boards": [],
        }

    monkeypatch.setattr(scrape_cmd.ingest, "ingest_boards", lambda *a, **kw: _Run())
    call_command("scrape", stdout=StringIO())


@pytest.mark.django_db
def test_a_fresh_deploy_and_a_migrated_database_tag_the_same_firms(monkeypatch):
    """D-22's actual promise, measured both ways over the same slugs.

    `millennium` is the control: a multi-strat fund that runs real
    networking, on the board and deliberately not on the list."""
    slugs = sorted(ASSESSMENT_RECRUITING) + ["millennium"]

    # A fresh deploy: no Firm rows at all, `scrape` creates them.
    _run_scrape(monkeypatch, slugs)
    fresh = dict(Firm.objects.values_list("slug", "recruiting_style"))

    # A migrated database: the rows already exist as `campus`, the seed runs.
    Firm.objects.all().delete()
    for slug in slugs:
        Firm.objects.create(slug=slug, name=slug.title())
    seed_migration.seed_assessment(apps, None)
    migrated = dict(Firm.objects.values_list("slug", "recruiting_style"))

    assert fresh == migrated
    assert {s for s, v in fresh.items() if v == "assessment"} == set(ASSESSMENT_RECRUITING)
    assert fresh["millennium"] == "campus"


@pytest.mark.django_db
def test_scrape_tags_a_firm_added_to_the_constant_after_its_row_existed(monkeypatch):
    """The drift D-22 names, closed. A firm added to
    `ASSESSMENT_RECRUITING` after the column shipped was tagged by neither
    writer — the seed had already run on that database, and the pre-create
    only fires for a row that does not exist yet — so it kept prompting
    coffee chats at a firm that refuses them in writing. `scrape` now
    corrects an existing row from the same constant.

    Add-only, and the second assertion is why: an admin's own `assessment`
    on a firm outside the list is a judgement this command has no evidence
    against, exactly as 0017's reverse already refuses to undo one."""
    late = Firm.objects.create(slug="xtx", name="XTX Markets")
    by_hand = Firm.objects.create(slug="pointy", name="Pointy",
                                  recruiting_style="assessment")
    assert late.recruiting_style == "campus"

    _run_scrape(monkeypatch, ["xtx", "pointy"])

    late.refresh_from_db()
    by_hand.refresh_from_db()
    assert late.recruiting_style == "assessment"
    assert by_hand.recruiting_style == "assessment"
