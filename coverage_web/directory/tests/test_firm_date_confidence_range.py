"""`FirmDate.confidence` and `Opportunity.confidence` may not leave 0.0-1.0 —
enforced in Postgres, not in a form.

A live row (id 44, J.P. Morgan, app_close) carried `confidence=95.0`. Both real
writers (`import_firm_dates.py`, `seed_directory.py`) only ever assign a value
looked up out of a `{"rumor": 0.3, "reported": 0.6, "confirmed_official": 1.0}`
dict, so neither could have produced it; the row's `history=[]` and
`found_on=None` — both importers always populate them — point at
`FirmDateAdmin` (a plain `ModelAdmin`, no bounds on the raw float field) or an
equivalent `manage.py shell` write instead.

That is the whole argument for a database constraint over a model validator,
same as `firm_slug_not_blank` on `Firm`: a validator runs inside
`ModelForm.full_clean()`, and the admin's own change form is the ONE place
here that still goes through a `ModelForm` — every other writer
(`import_firm_dates`, `seed_directory`, `firm_merge`, a bare `manage.py
shell`) calls `.save()`/`.create()` directly, which never calls
`full_clean()`. A validator would have caught this one path and missed the
sibling that actually needs catching.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, connection, transaction

from directory.models import Firm, FirmDate, Opportunity

pytestmark = pytest.mark.django_db


@pytest.fixture
def firm():
    return Firm.objects.create(slug="jpm", name="J.P. Morgan")


# ---------------------------------------------------------------------------
# FirmDate.confidence
# ---------------------------------------------------------------------------
def test_a_percentage_written_as_a_raw_number_is_rejected_on_insert(firm):
    """The exact shape of the corrupt row: 95.0 where every sibling row reads
    0.3/0.6/1.0."""
    with pytest.raises(IntegrityError), transaction.atomic():
        FirmDate.objects.create(
            firm=firm, cycle="2027", region="us", event_kind="app_close", confidence=95.0
        )


def test_a_healthy_confidence_cannot_be_overwritten_with_one_out_of_range(firm):
    fd = FirmDate.objects.create(
        firm=firm, cycle="2027", region="us", event_kind="app_close", confidence=1.0
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        FirmDate.objects.filter(pk=fd.pk).update(confidence=95.0)


def test_the_constraint_holds_for_raw_sql(firm):
    """The path the corrupt row actually most plausibly came through — the
    admin's change form and `manage.py shell` both end up here, not in a
    validator's `full_clean()`."""
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO firm_dates (firm_id, cycle, region, event_kind, "
                "precision, confidence, source_url, history) "
                "VALUES (%s, '2027', 'us', 'app_close', '', 95.0, '', '[]')",
                [firm.pk],
            )


@pytest.mark.parametrize("value", [0.0, 0.3, 0.6, 1.0])
def test_the_three_band_vocabulary_is_untouched(firm, value):
    fd = FirmDate.objects.create(
        firm=firm, cycle="2027", region="us", event_kind="app_close", confidence=value
    )
    fd.refresh_from_db()
    assert fd.confidence == value


def test_a_negative_confidence_is_rejected_too(firm):
    """The bound is two-sided: a `0.3` fat-fingered as `-30` would be exactly
    as wrong as `95` is for `0.95`."""
    with pytest.raises(IntegrityError), transaction.atomic():
        FirmDate.objects.create(
            firm=firm, cycle="2027", region="us", event_kind="app_close", confidence=-30.0
        )


# ---------------------------------------------------------------------------
# Opportunity.confidence — the sibling column on the other shared table, same
# unrestricted ModelAdmin.
# ---------------------------------------------------------------------------
def test_opportunity_confidence_rejects_a_percentage(firm):
    with pytest.raises(IntegrityError), transaction.atomic():
        Opportunity.objects.create(
            firm=firm, title="Analyst", url="https://example.com/1", confidence=95.0
        )


def test_opportunity_confidence_accepts_the_documented_bands(firm):
    for value in (0.0, 0.6, 1.0):
        opp = Opportunity.objects.create(
            firm=firm, title="Analyst", url=f"https://example.com/{value}", confidence=value
        )
        opp.refresh_from_db()
        assert opp.confidence == value
