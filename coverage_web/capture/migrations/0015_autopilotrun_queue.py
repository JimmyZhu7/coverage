from django.conf import settings
from django.db import migrations, models


def _fail_abandoned_runs(apps, schema_editor):
    """Every pre-existing `running` row is abandoned by definition.

    Before this migration nothing could ever finish a `running` run: the
    only writer was a management command that either completed (leaving
    `reviewed`/`failed`) or died (leaving `running` forever, with no
    reclaim path — `capture.autopilot._skip_reason` names this case). Those
    rows also cannot coexist with `uniq_autopilot_active` if a user has
    more than one. Retiring them says what was already true, and unblocks
    the user for the first run they can actually start.
    """
    apps.get_model("capture", "AutopilotRun")._default_manager.filter(
        status="running"
    ).update(
        status="failed",
        failure_reason=(
            "This run stopped before it finished — nothing was added to "
            "your network. Start it again when you like."
        ),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('capture', '0014_autopilotrun_evidence_note'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='autopilotrun',
            name='failure_reason',
            field=models.CharField(blank=True, default='', max_length=300),
        ),
        migrations.AddField(
            model_name='autopilotrun',
            name='started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='autopilotrun',
            name='status',
            field=models.CharField(
                choices=[
                    ('queued', 'Queued'),
                    ('running', 'Deciding'),
                    ('reviewed', 'Reviewed — waiting for your tap'),
                    ('applied', 'Applied'),
                    ('failed', 'Failed'),
                ],
                default='running', max_length=16,
            ),
        ),
        migrations.RunPython(
            _fail_abandoned_runs, migrations.RunPython.noop, elidable=False
        ),
        migrations.AddConstraint(
            model_name='autopilotrun',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status__in', ['queued', 'running'])),
                fields=('user',), name='uniq_autopilot_active',
            ),
        ),
    ]
