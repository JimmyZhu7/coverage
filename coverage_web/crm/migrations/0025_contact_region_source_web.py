"""Add "web" to `Contact.region_source`.

Numbered 0025 behind the calendar branch's 0024_calendarevent_time_provenance,
which landed the same night: two 0024s are invisible to git and a forked graph
to Django.

Choices-only, exactly like 0022: `region_source` is a 16-char CharField with
no database constraint behind its choice list, so this is a no-op against
Postgres and exists so Django's model state and the migration graph agree.

The value is written only by `crm.region_enrich` (via the
`enrich_contact_regions` command) and carries the one kind of placement the
model's own docstring does not ban: a location stated about the PERSON on a
public page naming both them and their firm, with the URL kept in the undo
file. No existing row is rewritten.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0024_calendarevent_time_provenance"),
    ]

    operations = [
        migrations.AlterField(
            model_name="contact",
            name="region_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("user", "Set by you"),
                    ("declared", "From your target regions"),
                    ("firm", "From the firm\'s markets"),
                    ("web", "From their public profile"),
                ],
                default="",
                max_length=16,
            ),
        ),
    ]
