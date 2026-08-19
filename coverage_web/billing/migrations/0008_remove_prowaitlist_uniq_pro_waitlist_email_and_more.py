"""Waitlist dedupe key: (email, source), not email alone.

The Team card's "Run a club? Notify me" now posts to the same
`billing:waitlist_join` the Pro card does, tagged `pricing_page_team`. Under
the old email-only constraint that wiring would have been worse than the dead
anchor it replaced: `get_or_create` on email alone turns the second ask into
a silent no-op, so someone who joined for Pro and later asked about Team
would leave exactly one row, labelled Pro. Widening the key is what lets one
person express two intents and lets the founder read the Team list without
the Pro list in it.

Widening a unique constraint never fails on existing data — every row unique
on (email) is unique on (email, source) — so this is safe to apply forward
with the table populated. Reversing it is the direction that can fail, and
only if two rows share an email by then.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0007_alter_creditledger_kind'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='prowaitlist',
            name='uniq_pro_waitlist_email',
        ),
        migrations.AddConstraint(
            model_name='prowaitlist',
            constraint=models.UniqueConstraint(fields=('email', 'source'), name='uniq_pro_waitlist_email_source'),
        ),
    ]
