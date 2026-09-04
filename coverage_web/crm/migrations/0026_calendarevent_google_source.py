"""`CalendarEvent` learns about a connected Google Calendar.

Three changes, one feature (capture/gcal_live.py):

* `external_id` — Google's own event id, and the key a re-sync updates on
  instead of duplicating. Blank on every existing row, which is correct:
  nothing in this table came from a calendar sync before this ran.
* `source` gains "gcal", a choices-only alter with no column change and no
  row touched. A third source rather than a variant of "capture" because
  the page says different things about them — one is what we found in the
  mail, the other is the student's own calendar restated, read-only.
* A unique constraint on (user, external_id) excluding blanks, on the same
  terms as the thread and ics_uid constraints already on this model: blanks
  are not a key, and Postgres treats empty strings as equal where it treats
  NULLs as distinct, so the condition has to say so explicitly.

Numbered 0026 behind 0025_contact_region_source_web, which is the leaf this
was generated against — checked rather than assumed, after the 0024 fork.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0025_contact_region_source_web'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='calendarevent',
            name='external_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AlterField(
            model_name='calendarevent',
            name='source',
            field=models.CharField(choices=[('manual', 'Added by you'), ('capture', 'From your mailbox'), ('gcal', 'From your calendar')], default='manual', max_length=16),
        ),
        migrations.AddConstraint(
            model_name='calendarevent',
            constraint=models.UniqueConstraint(condition=models.Q(('external_id', ''), _negated=True), fields=('user', 'external_id'), name='uniq_calendar_event_user_external_id'),
        ),
    ]
