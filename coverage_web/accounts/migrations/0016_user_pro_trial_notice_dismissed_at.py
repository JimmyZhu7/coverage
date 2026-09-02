"""When the "your Pro trial ended" banner was dismissed.

Nullable with no default and no backfill, deliberately: null means "not
dismissed", and every account that already exists has never been shown the
banner, so null is the correct state for all of them. On a database where a
trial has already lapsed (none today), the banner appears once on the next
Settings visit, which is the intended behaviour rather than a data problem.

See accounts/models.py's comment on the field and accounts/trials.py::
trial_ended_notice for the four conditions that render it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0015_languages_study_level_affiliations'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='pro_trial_notice_dismissed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
