"""`Firm.recruiting_style` (migration directory/0016) and its seed
(directory/0017): the one firm-level fact three networking surfaces read.

The seed is tested by calling the migration's own functions against the
live app registry rather than by replaying migration state — what matters
is that the slug list lands `assessment` on the right rows, leaves the
multi-strat funds alone, and reverses cleanly.
"""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps

from directory.admin import FirmAdmin
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


def test_the_migration_docstring_carries_the_evidence_and_the_slugs():
    doc = seed_migration.__doc__
    assert "unfortunately, no" in doc
    assert "Datathons" in doc
    assert "doesn't matter who you know" in doc
    for slug in seed_migration.ASSESSMENT_SLUGS:
        assert slug in doc, f"{slug} is seeded but not listed in the docstring"
    for untouched in ("Millennium", "Point72", "AQR"):
        assert untouched in doc


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
