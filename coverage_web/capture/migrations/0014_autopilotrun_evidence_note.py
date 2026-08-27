from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('capture', '0013_alter_autopilotdecision_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='autopilotrun',
            name='evidence_note',
            field=models.CharField(blank=True, default='', max_length=300),
        ),
    ]
