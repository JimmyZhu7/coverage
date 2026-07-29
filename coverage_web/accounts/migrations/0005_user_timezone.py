"""Per-user IANA timezone (docs/specs/settings-page.md audit #5).

Backfills nothing on purpose. Every existing row gets "" — UNSET — which the
middleware reads as "use settings.TIME_ZONE", i.e. exactly the UTC behaviour
those users already had. Guessing a zone from `regions` here would silently
move the week boundary of every existing account on an inference they never
made, which is the failure this column exists to fix, not repeat.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_user_cadence_params_user_weekly_touch_goal_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='timezone',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
