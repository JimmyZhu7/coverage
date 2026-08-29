"""backfill_contact_regions --revert must actually restore blank.

THE BUG: `_revert` sets `c.region = ""` and calls `c.save(update_fields=
["region"])`. But `Contact.save()` always re-runs `resolve_region()`, and for
a contact whose firm has an unambiguous single deadline market (exactly the
"firm footprint" signal `region_inference._from_firm_regions` exists to
materialise), tier 4 of `resolve_region` re-derives the SAME region the
instant the row is saved blank — so the row never actually goes blank, the
command's own "Reverted N contacts to blank" claim is false, and the module's
documented contract ("Revert restores a row... " / crm/region_inference.py's
"a one-time materialisation... reviewed before a single row is written")
silently fails for the exact case it was built to handle.

`crm.regions.unplace_declared_regions` already solved this the right way for
the same reason (see its own comment): a plain `.update()` bypasses `save()`
so resolution cannot immediately re-place what was just cleared.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from crm.models import Contact
from directory.models import Firm

User = get_user_model()


def _user(email="backfill@example.com", regions=None):
    return User.objects.create_user(email=email, password="x", regions=list(regions or []))


def _firm(regions, slug="hk-only", name="Hong Kong Only Bank"):
    return Firm.objects.create(slug=slug, name=name, regions=list(regions))


def _blank_region_contact_at(user, firm):
    """A contact that is blank-region TODAY even though its firm now implies
    exactly one market — the real-world shape `region_inference`'s firm-
    footprint signal exists for: the firm's markets were attached/changed
    after this row's own `save()` last ran, so resolution never saw them.
    Built with `.update()` (not `.save()`) on purpose, to bypass
    `resolve_region` the same way that real-world drift does."""
    contact = Contact.all_objects.create(user=user, name="Pat", firm=None)
    Contact.objects.for_user(user).filter(id=contact.id).update(firm=firm)
    contact.refresh_from_db()
    assert contact.region == "" and contact.region_source == ""
    return contact


@pytest.mark.django_db
def test_revert_restores_a_firm_derived_region_to_true_blank(tmp_path):
    user = _user(regions=[])
    firm = _firm(regions=["hk"])
    contact = _blank_region_contact_at(user, firm)

    undo_file = tmp_path / "undo.json"
    call_command(
        "backfill_contact_regions",
        user=user.email, apply=True, undo_file=str(undo_file),
    )

    contact.refresh_from_db()
    assert contact.region == "hk"  # the firm-footprint signal fired

    call_command("backfill_contact_regions", user=user.email, revert=str(undo_file))

    contact.refresh_from_db()
    assert contact.region == "", (
        "revert must restore true blank, not let Contact.save()'s own "
        "resolve_region() immediately re-derive the same firm-implied region"
    )
    # The documented invariant: region_source is blank exactly when region
    # is blank (crm/models.py's Contact.region_source comment).
    assert contact.region_source == ""


@pytest.mark.django_db
def test_revert_leaves_a_hand_corrected_row_alone(tmp_path):
    """The command's own contract: a region the user corrected AFTER the
    backfill is their word now, and revert must not touch it."""
    user = _user(regions=[])
    firm = _firm(regions=["hk"])
    contact = _blank_region_contact_at(user, firm)

    undo_file = tmp_path / "undo.json"
    call_command(
        "backfill_contact_regions",
        user=user.email, apply=True, undo_file=str(undo_file),
    )
    contact.refresh_from_db()
    assert contact.region == "hk"

    # The student looks at the row and corrects it by hand.
    contact.region = "us"
    contact.save(update_fields=["region"])
    assert contact.region_source == Contact.REGION_SOURCE_USER

    call_command("backfill_contact_regions", user=user.email, revert=str(undo_file))

    contact.refresh_from_db()
    assert contact.region == "us"
