"""Backfill `Opportunity.class_year` from titles already in the table.

`class_year` is populated at ingest from this point on, but the shared
opportunities table already holds thousands of rows scraped before the column
existed. Without a backfill the new Year filter would report "no class year
stated" for the handful of postings that do state one, which is the exact
false-blank the column was added to eliminate.

The rule here is identical to `classify.extract_class_year` and just as
narrow: the title must say "Class of 2028" / "Class 2028" in those words. A
programme year ("2027 Summer Analyst Program") is NOT a class year and is left
blank — `cohort` already carries it. Nothing is derived, offset, or guessed;
on the live set this touches roughly 3 rows out of 4,000, and that is the
correct number.

The pattern is spelled out here rather than imported from directory.classify
for the same reason crm/0005 spells out its region set: a migration must keep
producing the same result years from now, against the historical model state,
even after the live classifier's rules move on. When the rules do move on,
`manage.py reclassify` is the tool that re-derives across all rows — a
migration runs once, by design.

Reverse clears the column, which is safe precisely because nothing else writes
it: every value is reproducible from the title by re-running the forward pass
or `reclassify`. (Contrast crm/0005, whose reverse is a no-op because those
values could have been hand-corrected.)
"""

from __future__ import annotations

import re

from django.db import migrations

# Mirrors classify._CLASS_YEAR as of this migration. The lookbehind is what
# keeps "world-class 2027" and the live "Investment Banking, Classic — Summer
# Analyst" rows out of the field.
CLASS_YEAR = re.compile(
    r"(?<![\w-])class\s+(?:of\s+)?(20(?:2[4-9]|3[0-5]))\b", re.IGNORECASE
)


def backfill_class_year(apps, schema_editor):
    Opportunity = apps.get_model("directory", "Opportunity")

    # Only titles containing the word at all are candidates, so this is one
    # narrow scan instead of a full-table pass in Python.
    updates = []
    for pk, title in Opportunity.objects.filter(
        title__icontains="class"
    ).values_list("id", "title"):
        m = CLASS_YEAR.search(title or "")
        if m:
            updates.append(Opportunity(id=pk, class_year=m.group(1)))
    if updates:
        Opportunity.objects.bulk_update(updates, ["class_year"], batch_size=500)


def clear_class_year(apps, schema_editor):
    Opportunity = apps.get_model("directory", "Opportunity")
    Opportunity.objects.exclude(class_year="").update(class_year="")


class Migration(migrations.Migration):

    dependencies = [
        ("directory", "0002_opportunity_class_year"),
    ]

    operations = [
        migrations.RunPython(backfill_class_year, clear_class_year),
    ]
