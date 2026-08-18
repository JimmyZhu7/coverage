import django.contrib.postgres.fields
from django.db import migrations, models


def _copy_target_cycle_forward(apps, schema_editor):
    """The one thing this migration must not do is silently drop a student's
    existing answer. `target_cycle` was single-select, so each non-blank row
    becomes a one-item list on `target_cycles`."""
    User = apps.get_model("accounts", "User")
    for user in User.objects.exclude(target_cycle="").only("id", "target_cycle"):
        user.target_cycles = [user.target_cycle]
        user.save(update_fields=["target_cycles"])


def _copy_target_cycle_backward(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.exclude(target_cycles=[]).only("id", "target_cycles"):
        user.target_cycle = (user.target_cycles or [""])[0]
        user.save(update_fields=["target_cycle"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_user_plan"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="target_cycles",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=32), blank=True, default=list, size=None
            ),
        ),
        migrations.RunPython(_copy_target_cycle_forward, _copy_target_cycle_backward),
        migrations.RemoveField(
            model_name="user",
            name="target_cycle",
        ),
    ]
