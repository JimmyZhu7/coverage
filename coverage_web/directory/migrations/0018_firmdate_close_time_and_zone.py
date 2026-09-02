"""D-19: the hour a deadline actually closes, and the zone it was stated in.

Additive and empty on arrival. Both columns are nullable and nothing here
populates them: filling them is `set_firm_date_times`' job, which reads a
findings file, touches only `confirmed_official` rows whose own source states
a time, and defaults to `--dry-run`. A migration that guessed 23:59 for every
close would be exactly the false precision this decision was narrowed to
avoid — 25 of the 41 live rows are estimates.

The two constraints are the point of the change as much as the columns are.
`firm_dates_close_time_needs_a_zone` refuses a bare number ("23:59" is fifteen
hours wide without a zone, which is the bug being fixed). `firm_dates_close_
time_needs_a_day` refuses an hour on a month-level or estimated row, where
combining it with `date` would mint an instant nobody stated.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('directory', '0017_seed_assessment_recruiting_style'),
    ]

    operations = [
        migrations.AddField(
            model_name='firmdate',
            name='close_time',
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='firmdate',
            name='close_tz',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddConstraint(
            model_name='firmdate',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('close_time__isnull', True), ('close_tz', '')), models.Q(('close_time__isnull', False), models.Q(('close_tz', ''), _negated=True)), _connector='OR'), name='firm_dates_close_time_needs_a_zone'),
        ),
        migrations.AddConstraint(
            model_name='firmdate',
            constraint=models.CheckConstraint(condition=models.Q(('close_time__isnull', True), models.Q(('precision__in', ['', 'day']), ('date__isnull', False)), _connector='OR'), name='firm_dates_close_time_needs_a_day'),
        ),
    ]
