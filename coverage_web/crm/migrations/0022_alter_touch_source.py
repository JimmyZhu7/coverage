"""Widen `Touch.source` to name WHICH DOOR moved a contact's state.

Choices-only: `source` is already a 16-char CharField with no database
constraint behind the choice list, so this is a no-op against Postgres and
exists purely so Django's model state and the migration graph agree.

The five new values (park_all, bulk, undo, unpark, replay) all land on
`manual_override` rows. Before them, every override on the founder's account
said "manual" — 179 of 179 — so "which tap parked these 44 people" was
answerable only by regex over the human half of the note. No existing row is
rewritten: "manual" keeps meaning exactly what it meant, a person, door
unrecorded.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0021_calendarevent_cancelled_at'),
    ]

    operations = [
        migrations.AlterField(
            model_name='touch',
            name='source',
            field=models.CharField(choices=[('manual', 'Manual'), ('capture', 'Capture'), ('import', 'Import'), ('assistant', 'Assistant'), ('park_all', 'Park all (Today)'), ('bulk', 'Bulk action (Network board)'), ('undo', 'Undo of a bulk park'), ('unpark', 'Un-park (Parked contacts)'), ('replay', 'State replayed from the ledger')], default='manual', max_length=16),
        ),
    ]
