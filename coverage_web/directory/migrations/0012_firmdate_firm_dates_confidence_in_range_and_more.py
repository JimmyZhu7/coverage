"""Bound `confidence` to 0.0-1.0 on `firm_dates` and `opportunities`.

Schema-only, deliberately: a row that already violates this (J.P. Morgan
`FirmDate` id 44, `confidence=95.0`) must be corrected by hand first —
`manage.py migrate` will refuse to apply this migration while it stands,
which is the point. See `firm_dates_confidence_in_range`'s comment on
`FirmDate.Meta` for how that row got there and why a CHECK constraint is the
right guard, and `firm_slug_not_blank` (migration 0011) for the precedent.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('directory', '0011_firm_slug_not_blank'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='firmdate',
            constraint=models.CheckConstraint(condition=models.Q(('confidence__gte', 0.0), ('confidence__lte', 1.0)), name='firm_dates_confidence_in_range'),
        ),
        migrations.AddConstraint(
            model_name='opportunity',
            constraint=models.CheckConstraint(condition=models.Q(('confidence__gte', 0.0), ('confidence__lte', 1.0)), name='opportunities_confidence_in_range'),
        ),
    ]
