"""The stored READ-ONLY Google Calendar grant (capture/gcal_live.py).

A separate table from `gmail_connections`, not a widening of it. The two
grants are two consents: a student can give one and refuse the other, and
disconnecting either must leave the other running. One row per user, one
encrypted refresh token, and the same Fernet key as the mail grant so a key
rotation covers both.

Numbered 0017 behind 0016_alter_mailfact_kind, the leaf this was generated
against.
"""

import django.db.models.deletion
import django.db.models.manager
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('capture', '0016_alter_mailfact_kind'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='GoogleCalendarConnection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('google_email', models.EmailField(max_length=254)),
                ('refresh_token_encrypted', models.TextField()),
                ('calendar_id', models.CharField(default='primary', max_length=255)),
                ('sync_token', models.TextField(blank=True, default='')),
                ('status', models.CharField(choices=[('active', 'Active'), ('revoked', 'Revoked — needs reconnect')], default='active', max_length=16)),
                ('connected_at', models.DateTimeField(auto_now_add=True)),
                ('last_synced_at', models.DateTimeField(blank=True, null=True)),
                ('last_sync_stats', models.JSONField(blank=True, default=dict)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'google_calendar_connections',
                'abstract': False,
                'base_manager_name': 'all_objects',
                'default_manager_name': 'all_objects',
            },
            managers=[
                ('all_objects', django.db.models.manager.Manager()),
            ],
        ),
    ]
