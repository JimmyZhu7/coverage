"""Backfill `Contact.region` from the contact's firm — but only where the
firm gives an UNAMBIGUOUS answer.

`region` exists because the cadence engine used to infer a contact's region by
substring-matching "hk" inside `contacts.source`, a provenance string. Every
row written before this migration therefore carries an inferred region that
may be wrong, and the whole point of the new column is to stop guessing. So
this backfill is deliberately narrow:

  - The contact has a directory firm, and that firm names exactly ONE region
    the product models (us / hk) -> set it. There is no other defensible
    answer for a contact at a US-only or HK-only firm.
  - Anything else — no firm, a firm covering both regions, a firm with no
    regions, a firm whose only regions are ones this product doesn't model
    (sg / eu) — is LEFT BLANK. Blank means "unknown", and the engine's
    unknown path (match a close date in either region) is the conservative
    behavior. Filling these in from `source` would just relaunder the bug
    this column was added to fix.

Reverse is a no-op rather than a blanket clear: by the time anyone reverses
this, some of these values may have been corrected by hand, and wiping user
edits is worse than leaving a column populated on a schema that is about to
drop it anyway.
"""

from __future__ import annotations

from django.db import migrations

# Mirrors crm.models.Contact.REGION_VALUES. Spelled out rather than imported:
# a migration must keep working against the historical model state even if the
# live model's choices change later.
MODELED_REGIONS = {"us", "hk"}


def backfill_region(apps, schema_editor):
    Contact = apps.get_model("crm", "Contact")
    Firm = apps.get_model("directory", "Firm")

    # One pass over firms, so this is two queries regardless of contact count.
    single_region_firms: dict[int, str] = {}
    for firm_id, regions in Firm.objects.values_list("id", "regions"):
        known = {(r or "").strip().lower() for r in (regions or [])} & MODELED_REGIONS
        if len(known) == 1:
            single_region_firms[firm_id] = next(iter(known))

    if not single_region_firms:
        return

    for region in MODELED_REGIONS:
        firm_ids = [fid for fid, r in single_region_firms.items() if r == region]
        if not firm_ids:
            continue
        # `all_objects`, not `objects`: a backfill is legitimately
        # cross-tenant, and it's the only manager the historical model has
        # (see the `managers=` block in crm/0001_initial). `region=""` only —
        # never overwrite a value someone already set.
        Contact.all_objects.filter(region="", firm_id__in=firm_ids).update(region=region)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0004_contact_opener_contact_region"),
        ("directory", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(backfill_region, noop),
    ]
