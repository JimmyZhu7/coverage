# Hand-written, 2026-09-02. One stored cycle value that no parser has ever
# recognised, mapped onto the vocabulary the product actually speaks.
#
# `User.target_cycles` holds the strings `directory.recommend.cycle_choices()`
# offers ("2028 Summer Internship"). One row — the demo account, seeded by
# `accounts/management/commands/seed_demo.py` before that vocabulary existed —
# held `"sa2028_ib"`, an internal-looking token combining a cycle and a track.
# `parse_target_cycle` returns None for it, which by its own contract costs
# the student nothing: the 15-point cycle bonus and the level gate are simply
# off. Silently. On the account every demo the founder gives runs on.
#
# THE MAPPING DROPS THE TRACK HALF, and that is correct rather than lossy:
# `target_cycles` is a list of CYCLES and `User.tracks` is where a track
# belongs (the demo row already carries `["ib"]`). Encoding a track inside a
# cycle string is what made the value unparseable in the first place.
#
# NARROW ON PURPOSE. Only `sa<4-digit-year>` and `sa<4-digit-year>_<track>`
# are touched, and only when the whole string matches. Anything else — a
# value already in the dropdown's words, a blank, wording nobody here has
# seen — is left exactly as it is: a cycle this migration cannot read is not
# a cycle it may guess at (P1).
#
# Reversible in the shape that matters (the column keeps working either way),
# and deliberately NOT reversible in the value: turning "2028 Summer
# Internship" back into "sa2028_ib" would need the track, would re-break the
# parser, and would be undoing a repair rather than undoing a change. The
# backward step is a no-op that says so.

from __future__ import annotations

import re

from django.db import migrations

# "sa2028" or "sa2028_ib". The year is the only part carried forward.
_LEGACY_CYCLE_RX = re.compile(r"^sa(20\d\d)(?:_[a-z-]+)?$", re.IGNORECASE)

# The label half of the value this maps to. NOT imported from
# `directory.recommend`: a migration has to keep meaning the same thing when
# the application code around it moves on, and a data migration that reads a
# live constant re-runs differently on a fresh database a year from now.
# `accounts/tests/test_seed_demo.py` pins the two against each other, which
# is where a drift belongs — in a failing test, not in a silent rewrite of
# somebody's settings.
_SUMMER_INTERNSHIP = "Summer Internship"


def parse_legacy(value: str) -> str | None:
    """`"sa2028_ib"` -> `"2028 Summer Internship"`, or None for anything
    this migration does not recognise."""
    match = _LEGACY_CYCLE_RX.match((value or "").strip())
    return f"{match.group(1)} {_SUMMER_INTERNSHIP}" if match else None


def rewrite_legacy_cycles(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.exclude(target_cycles=[]).iterator():
        rewritten, changed = [], False
        for value in user.target_cycles or []:
            mapped = parse_legacy(value)
            if mapped is None:
                rewritten.append(value)
                continue
            changed = True
            # Deduplicated: a row holding both the legacy token and the
            # dropdown value for the same intake must not end up with the
            # same cycle twice.
            if mapped not in rewritten:
                rewritten.append(mapped)
        if changed:
            user.target_cycles = rewritten
            user.save(update_fields=["target_cycles"])


def noop_backward(apps, schema_editor):
    """Deliberately nothing. See the module note."""


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0016_user_pro_trial_notice_dismissed_at"),
    ]

    operations = [
        migrations.RunPython(rewrite_legacy_cycles, noop_backward),
    ]
