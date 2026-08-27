"""`Contact.region_source` — where a contact's region came from.

The backfill is deliberately conservative: every row that already carries a
region is stamped "user", regardless of how it actually got there. Most of
them came from `default_region_from_firm` at save time, and calling those
"firm" would be more literally accurate — but it would also hand a future
re-derivation permission to overwrite them. The founder personally reviewed
the 2026-08-26 backfill of this column's sibling (`region`), so the values
sitting in the table now are human-approved. "user" is the code for
human-approved, and human-approved values are the ones nothing below tier 1
is ever allowed to touch.

Rows with a blank region keep a blank source — that is the invariant the
model keeps (blank `region_source` ⟺ blank `region`) and the state the
Unplaced tab exists to let a person answer.
"""

from django.db import migrations, models


def stamp_existing_regions_as_user(apps, schema_editor):
    # `_default_manager`, not `.objects`: every private-zone model names
    # `all_objects` as its default/base manager (coverage_web.tenancy), and a
    # historical model rendered by the migration state has no `.objects` at
    # all. A backfill is also the one place a tenant-scoped manager would be
    # wrong — this runs across every user's rows.
    Contact = apps.get_model("crm", "Contact")
    Contact._default_manager.exclude(region="").update(region_source="user")


def unstamp(apps, schema_editor):
    """Reverse leaves `region` alone — dropping the column is the whole
    reversal, and clearing regions on the way out would destroy answers this
    migration never wrote."""
    Contact = apps.get_model("crm", "Contact")
    Contact._default_manager.update(region_source="")


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0016_contactmerge"),
    ]

    operations = [
        migrations.AddField(
            model_name="contact",
            name="region_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("user", "Set by you"),
                    ("declared", "From your target regions"),
                    ("firm", "From the firm's markets"),
                ],
                default="",
                max_length=16,
            ),
        ),
        migrations.RunPython(stamp_existing_regions_as_user, unstamp),
    ]
