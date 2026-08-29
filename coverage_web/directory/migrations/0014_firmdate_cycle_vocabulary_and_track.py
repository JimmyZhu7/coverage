"""Close the `firm_dates.cycle` vocabulary and split the desk into its own column.

`cycle` was the last free-text key on `FirmDate`. The live 41 rows held four
spellings of one idea — `sa2028_ib` (18), `SA 2028` (11), `sa2028_hk` (7),
`sa2028_pe` (2) — plus one `insight` and two blanks. Nothing could group a
programme across firms, because no two writers agreed on the key.

WHY THIS ONE CARRIES DATA, WHERE 0011/0012 DELIBERATELY DID NOT
---------------------------------------------------------------
`firm_slug_not_blank` and `firm_dates_confidence_in_range` are schema-only on
purpose: each had exactly ONE offending row, hand-entered through the admin,
and `migrate` refusing to run until a human fixed it was the point. This is the
opposite case. 38 of 41 rows are "offending" and every one of them is offending
because two legitimate writers used two legitimate conventions. There is no
human fix to wait for — the mapping is mechanical, and it is spelled out in
`directory.timeline.parse_cycle` so the importers apply exactly the same rule
going forward.

WHAT THE BACKFILL PRESERVES
---------------------------
  - The desk. `sa2028_ib` -> `cycle="sa2028", track="ib"`. Both halves kept,
    in the two columns that can each be queried.
  - The market. `sa2028_hk` -> `cycle="sa2028", track=""`. The `hk` is NOT
    dropped: it is already in the row's own `region` column, on all 7 rows,
    with the same value. Verified before writing this (see the guard below,
    which refuses to drop a suffix that disagrees with the column).
  - The original string. Every row this migration rewrites gets an append-only
    `history` entry naming what `cycle` used to say, because `history` is this
    model's own provenance mechanism and "a date that moved can be explained
    rather than just being different from what someone remembers".

WHAT IT REFUSES TO GUESS
------------------------
Three rows cannot be confidently mapped and are NOT forced into a bucket:

  - id 27, Morgan Stanley, `cycle="insight"`. Its `history` note describes the
    "2026 Morgan Stanley Asia Virtual Event Series ... 2027 Internship
    Recruitment Processes and Tips" — so it is about the 2027 intake, not the
    2028 one every other row here belongs to. Filing it as `sa2028` would be
    inventing a fact. Filing it as `sa2027` would be inferring one from a note
    that describes an event series, not an intake. It becomes `cycle=""`
    (not stated) with the original value recorded, and is listed for review.
  - ids 47 and 48, J.P. Morgan and Goldman Sachs `app_close`, already blank.
    Their history says "Entered by hand by the founder, not scraped." A date
    whose cycle the founder did not state stays a date whose cycle nobody
    stated.

`manage.py review_firm_date_cycles` prints the current list; it is idempotent
and read-only, and exists so this migration's docstring does not become the
only record of which rows still need a human.
"""

from django.db import migrations, models

# Duplicated from `directory.timeline` on purpose. A migration must keep
# behaving the way it did the day it was written even after the module it was
# derived from changes, which is why Django migrations never import app code.
_TRACKS = ("ib", "st", "pe", "am", "consulting", "corp-strat")
_REGIONS = ("hk", "us", "sg", "eu", "cn", "jp", "other", "global")
_SEASONS = ("sa", "ft")


def _split(raw):
    """`"sa2028_ib"` -> `("sa2028", "ib", "")`; the third slot is a market
    suffix seen and dropped, so the caller can check it against `region`.
    Returns None when the value does not name a cycle at all."""
    text = str(raw or "").strip()
    if not text:
        return "", "", ""

    head, sep, tail = text.partition(" ")
    if sep:                                        # human: "SA 2028"
        season, year = head.strip().lower(), tail.strip()
        if season in _SEASONS and year.isdigit() and len(year) == 4:
            return f"{season}{year}", "", ""
        return None

    head, sep, tail = text.lower().partition("_")
    if len(head) != 6 or head[:2] not in _SEASONS or not head[2:].isdigit():
        return None
    if not sep:
        return head, "", ""
    if tail in _TRACKS:
        return head, tail, ""
    if tail in _REGIONS:
        return head, "", tail
    return None


def decompose(apps, schema_editor):
    FirmDate = apps.get_model("directory", "FirmDate")
    unmapped = []
    for row in FirmDate.objects.all().iterator():
        original = row.cycle
        parsed = _split(original)

        if parsed is None:
            # Not a cycle. Blank it — "" already means "not stated" here — and
            # keep the string in history so nothing is destroyed.
            unmapped.append((row.pk, original))
            row.cycle = ""
            row.track = ""
            note = f"cycle {original!r} does not name a recruiting cycle; " \
                   f"recorded as not-stated and flagged for review"
        else:
            cycle, track, market = parsed
            # A suffix that DISAGREES with the row's own region column is a
            # data error, not a second fact. None exist (all 7 agree), but a
            # silent drop is exactly how one would go unnoticed if one did.
            if market and row.region and market != row.region.strip().lower():
                unmapped.append((row.pk, original))
                continue
            if cycle == original and track == row.track:
                continue
            row.cycle = cycle
            row.track = track
            note = f"cycle {original!r} normalised to cycle={cycle!r} track={track!r}"
            if market:
                note += f"; market suffix {market!r} dropped (already in region)"

        row.history = list(row.history or []) + [{
            "migration": "directory.0014_firmdate_cycle_vocabulary_and_track",
            "was_cycle": original,
            "note": note,
        }]
        row.save(update_fields=["cycle", "track", "history"])

    if unmapped:
        listing = ", ".join(f"#{pk} {value!r}" for pk, value in unmapped)
        print(f"\n  firm_dates rows whose cycle could not be mapped "
              f"(left blank, original kept in history): {listing}")
        print("  Run `manage.py review_firm_date_cycles` to see them again.\n")


def recompose(apps, schema_editor):
    """Re-fuse the desk back into `cycle` so 0014 can be reversed.

    Lossy in one direction only, and knowingly: a row whose cycle was `SA 2028`
    comes back as `sa2028`, not as the human spelling. The old column accepted
    both, so the reversed database is valid under the old constraint — it just
    reads in one voice instead of two, which was the problem.
    """
    FirmDate = apps.get_model("directory", "FirmDate")
    for row in FirmDate.objects.exclude(track="").iterator():
        row.cycle = f"{row.cycle}_{row.track}" if row.cycle else row.cycle
        row.track = ""
        row.save(update_fields=["cycle", "track"])


class Migration(migrations.Migration):

    dependencies = [
        # Re-parented onto 0013 at merge time. This was authored against 0012
        # in a worktree that predated the precision-vocabulary constraint;
        # both landing on 0012 would leave the graph with two leaf nodes and
        # every test erroring on `makemigrations --merge`. The two constraints
        # are independent (precision vs cycle/track), so ordering them is
        # purely a graph question, not a data one.
        ('directory', '0013_firmdate_precision_vocabulary'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='firmdate',
            name='uniq_firm_dates_firm_cycle_region_event',
        ),
        migrations.AddField(
            model_name='firmdate',
            name='track',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AlterField(
            model_name='firmdate',
            name='cycle',
            field=models.CharField(blank=True, default='', max_length=16),
        ),
        # Between the column and the constraints, in that order: the CHECKs
        # below would reject every `sa2028_ib` row if they were added first,
        # and the new uniqueness key needs `track` already populated to be
        # able to tell Goldman's two US `app_open` rows apart.
        migrations.RunPython(decompose, recompose),
        migrations.AddConstraint(
            model_name='firmdate',
            constraint=models.UniqueConstraint(fields=('firm', 'cycle', 'track', 'region', 'event_kind'), name='uniq_firm_dates_firm_cycle_track_region_event'),
        ),
        migrations.AddConstraint(
            model_name='firmdate',
            constraint=models.CheckConstraint(condition=models.Q(('cycle', ''), ('cycle__regex', '^(sa|ft)[0-9]{4}$'), _connector='OR'), name='firm_dates_cycle_vocabulary'),
        ),
        migrations.AddConstraint(
            model_name='firmdate',
            constraint=models.CheckConstraint(condition=models.Q(('track', ''), ('track__in', ['ib', 'st', 'pe', 'am', 'consulting', 'corp-strat']), _connector='OR'), name='firm_dates_track_vocabulary'),
        ),
    ]
