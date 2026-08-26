"""`Firm.slug` may not be blank — enforced in Postgres, not in a form.

`unique=True` permits exactly one blank row, and one is all it takes. A
blank-slugged firm cannot be fetched by `Firm.objects.get(slug=...)`, falls
out of every slug-keyed map the app builds (`seed_firm_dates`'s
`firm_by_slug`, `import_email_patterns`'s `by_slug`), and sits in the path of
`seed_directory`'s `update_or_create(slug=...)`, which would silently adopt it
for any YAML row carrying a blank `id`.

The row that prompted this (id 218, "Citadel Securities") came through a
`manage.py shell` insert that omitted the field, not through any code path in
this repo — every one of those passes an explicit non-empty slug. That is the
whole argument for a database constraint over a model validator: a validator
runs in `full_clean()`, i.e. in ModelForms, i.e. nowhere near a shell.
"""

from __future__ import annotations

from importlib import import_module

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils.text import slugify

from directory._mail_domains import CREATABLE_FIRMS
from directory.models import Firm

pytestmark = pytest.mark.django_db

CONSTRAINT = "firm_slug_not_blank"


# ---------------------------------------------------------------------------
# The constraint
# ---------------------------------------------------------------------------
def test_a_blank_slug_is_rejected_on_insert():
    with pytest.raises(IntegrityError), transaction.atomic():
        Firm.objects.create(slug="", name="Citadel Securities")


def test_a_slug_cannot_be_blanked_by_an_update_either():
    firm = Firm.objects.create(slug="citadel-securities", name="Citadel Securities")
    with pytest.raises(IntegrityError), transaction.atomic():
        Firm.objects.filter(pk=firm.pk).update(slug="")


def test_the_constraint_holds_for_raw_sql():
    """The path the original row actually came through — a write that never
    went near a Django form or `full_clean()`."""
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO firms (slug, name, domains, regions, tracks, "
                "sponsors, status) "
                "VALUES ('', 'Blank Co', '{}', '{}', '{}', '{}', 'active')"
            )


def test_an_ordinary_slug_is_untouched():
    Firm.objects.create(slug="citadel-securities", name="Citadel Securities")
    assert Firm.objects.get(slug="citadel-securities").name == "Citadel Securities"


# ---------------------------------------------------------------------------
# The backfill in `0011`. The migration has already applied to the test
# database, so its function is exercised directly against rows made blank with
# the constraint lifted for the duration — the only way to reproduce the state
# it was written for now that the state is impossible.
# ---------------------------------------------------------------------------
class _Apps:
    """The two-argument `apps` shim `RunPython` would hand the function."""

    @staticmethod
    def get_model(app_label, model_name):
        assert (app_label, model_name) == ("directory", "Firm")
        return Firm


def _blank_the_slug(pk):
    with connection.cursor() as cur:
        cur.execute(f"ALTER TABLE firms DROP CONSTRAINT {CONSTRAINT}")
        cur.execute("UPDATE firms SET slug = '' WHERE id = %s", [pk])
        cur.execute(
            f"ALTER TABLE firms ADD CONSTRAINT {CONSTRAINT} "
            "CHECK (NOT (slug = '')) NOT VALID"
        )


def _fill():
    module = import_module("directory.migrations.0011_firm_slug_not_blank")
    module.fill_blank_slugs(_Apps(), None)


def test_the_backfill_derives_the_slug_from_the_rows_own_name():
    """And for the row that prompted this, that lands on exactly the slug
    `directory/_mail_domains.py` already declares — so `seed_mail_domains`
    resolves it by slug afterwards instead of falling back to a name match."""
    firm = Firm.objects.create(slug="tmp", name="Citadel Securities")
    _blank_the_slug(firm.pk)

    _fill()

    firm.refresh_from_db()
    assert firm.slug == "citadel-securities"
    assert firm.slug in CREATABLE_FIRMS
    assert slugify(firm.name) == firm.slug


def test_the_backfill_deduplicates_against_a_slug_already_taken():
    Firm.objects.create(slug="citadel-securities", name="Citadel Securities")
    stray = Firm.objects.create(slug="tmp", name="Citadel Securities")
    _blank_the_slug(stray.pk)

    _fill()

    stray.refresh_from_db()
    assert stray.slug == "citadel-securities-2"
    assert not Firm.objects.filter(slug="").exists()


def test_a_name_that_slugifies_to_nothing_still_gets_an_addressable_slug():
    firm = Firm.objects.create(slug="tmp", name="株式会社")
    _blank_the_slug(firm.pk)

    _fill()

    firm.refresh_from_db()
    assert firm.slug == f"firm-{firm.pk}"


def test_the_backfill_leaves_a_healthy_table_alone():
    firm = Firm.objects.create(slug="citadel-securities", name="Citadel Securities")

    _fill()

    firm.refresh_from_db()
    assert firm.slug == "citadel-securities"
