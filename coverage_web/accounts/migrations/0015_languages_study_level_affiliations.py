# Hand-written, 2026-09-01. Three profile inputs the research says gate roles
# and outreach, one data move, and one column drop.
#
# The founder's `User.assets` JSON held `languages` (['english', 'mandarin']),
# `current_status` ('rising sophomore') and `angles` (five outreach hooks),
# written by a cutover script, reachable from no form and read by nothing.
# Each becomes a real column here — `languages`, `study_level`,
# `affiliations` — and the data migration moves whatever a row has, then
# leaves `assets` otherwise intact (`advocate_target` stays where every
# reader of it expects).
#
# `User.language`, the interface-language column, goes. Its Settings control
# was removed on 2026-07-30 because nothing read the value (no
# LocaleMiddleware, no catalogs, no {% trans %}), and the column outlived the
# control by a month on "harmless". The demo account carried "fr" in it, set
# by that retired control by hand — no seed writes the column, and after this
# nothing can.
#
# Reversible: the backward step writes the three columns back into `assets`
# under their old keys, and Django restores `language` at its old default.

from __future__ import annotations

import re

import django.contrib.postgres.fields
from django.db import migrations, models

# The class-standing words that mean "undergraduate" and nothing else. A
# `current_status` that says anything different ("MBA candidate", "PhD") is
# left in `assets` untouched rather than guessed at: blank study_level means
# not stated, and not stated is the honest answer to wording this never saw.
_UNDERGRAD_WORDS = re.compile(
    r"\b(?:freshman|sophomore|junior|senior)\b", re.IGNORECASE
)


def _clean_list(values, *, lower: bool, max_len: int) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = " ".join(str(value).split())
        if lower:
            text = text.lower()
        text = text[:max_len]
        if text and text not in out:
            out.append(text)
    return out


def move_assets_forward(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.exclude(assets={}).iterator():
        assets = dict(user.assets or {})
        fields: list[str] = []
        if isinstance(assets.get("languages"), list):
            user.languages = _clean_list(
                assets.pop("languages"), lower=True, max_len=32
            )
            fields.append("languages")
        status = assets.get("current_status")
        if isinstance(status, str) and _UNDERGRAD_WORDS.search(status):
            user.study_level = "undergrad"
            assets.pop("current_status")
            fields.append("study_level")
        if isinstance(assets.get("angles"), list):
            user.affiliations = _clean_list(
                assets.pop("angles"), lower=False, max_len=160
            )
            fields.append("affiliations")
        if fields:
            user.assets = assets
            user.save(update_fields=fields + ["assets"])


def move_assets_backward(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.all().iterator():
        assets = dict(user.assets or {})
        changed = False
        if user.languages:
            assets["languages"] = list(user.languages)
            changed = True
        if user.study_level:
            assets["current_status"] = user.study_level
            changed = True
        if user.affiliations:
            assets["angles"] = list(user.affiliations)
            changed = True
        if changed:
            user.assets = assets
            user.save(update_fields=["assets"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0014_user_school_emails"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="languages",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=32),
                blank=True, default=list, size=None,
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="study_level",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Not stated"),
                    ("undergrad", "Undergraduate"),
                    ("masters", "Master's"),
                    ("mba", "MBA"),
                    ("phd", "PhD"),
                ],
                default="", max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="affiliations",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=160),
                blank=True, default=list, size=None,
            ),
        ),
        migrations.RunPython(move_assets_forward, move_assets_backward),
        migrations.RemoveField(
            model_name="user",
            name="language",
        ),
    ]
