"""Seed `Firm.recruiting_style = "assessment"` for the firms on the board that
hire off a test rather than off a conversation.

WHY A DATA MIGRATION. `recruiting_style` is a firm-level fact the Coverage
Gaps strip, the "Who to find" panel and the Network summary all read
(`crm.coverage`, `crm.sourcing`). Set by hand in a shell it would be true on
one database and silently false on the next; written here it is reproducible
on every database that runs the migration, and the constant it reads is the
record of exactly which firms are tagged and why.

THE EVIDENCE. Jane Street's public FAQ answers "Can I schedule a phone call
or coffee?" with "unfortunately, no". Citadel Securities' campus funnel is
Datathons and Invitationals. The practitioner finding, verbatim: "if you
can't pass their tests it doesn't matter who you know". For these firms a
coffee chat does not move the process, so the product must stop ranking them
as networking gaps to fill and stop proposing "an analyst to chat with".

THE SLUGS ARE NOT LISTED HERE. They are
`directory.boards.ASSESSMENT_RECRUITING`, the same set `scrape` reads when
it pre-creates a catalog firm, imported below rather than copied (D-22).
Prop-trading and quant market-making names, tagged where they exist on the
board (`filter(slug__in=...)` ignores absent ones, so a fresh database that
has not scraped a firm yet is fine). Two firms appear under both the
spelling the catalog uses and the one the request used — Citadel Securities
is `citadel-securities` on the live board, Tower Research is
`towerresearch` — so the seed lands whichever a database carries. On the
founder's board 2026-09-02 that is 18 rows from 20 slugs.

DELIBERATELY NOT TAGGED: Millennium, Point72, AQR and the other multi-strat
hedge funds. They run analyst programmes with real networking (referrals,
campus events, coffee chats that reach a hiring manager), so they stay
`campus` and keep every networking prompt.

A FRESH DEPLOY runs migrations before `seed_directory`/`scrape` create any
of these rows, so this seed finds nothing there; `scrape` tags them itself
from the same constant, at pre-create and on every later pass. On every
existing database this is the whole seed.

Reverse resets exactly these slugs to `campus`, and only where the value is
still `assessment` — a firm an admin re-tagged by hand is left alone.
"""

from __future__ import annotations

from django.db import migrations

from directory.boards import ASSESSMENT_RECRUITING

# THE LIST LIVES IN ONE PLACE (D-22, 2026-09-02): this migration used to
# carry its own frozen copy of the twenty slugs, and `directory.boards`
# carried the other one. Two lists of one fact drift, and the drift renders a
# coffee-chat prompt at a firm that refuses them in writing.
#
# IMPORTING A LIVE CONSTANT INTO A HISTORICAL MIGRATION is deliberate and is
# the exception this file argues for, not a pattern. The usual rule exists to
# stop a migration's SCHEMA depending on code that moves under it; this one
# writes no schema, and what it writes is a curated product fact whose whole
# purpose is to be current. A fresh database should be tagged with today's
# list, not with 2026-09-01's, which is exactly what "a fresh deploy and a
# migrated database produce the same answer" means. The slugs the seed
# actually used on any given database are recoverable from the row values
# and from this module's history.
ASSESSMENT_SLUGS = tuple(sorted(ASSESSMENT_RECRUITING))


def seed_assessment(apps, schema_editor):
    Firm = apps.get_model("directory", "Firm")
    Firm.objects.filter(slug__in=ASSESSMENT_SLUGS).update(recruiting_style="assessment")


def unseed_assessment(apps, schema_editor):
    Firm = apps.get_model("directory", "Firm")
    Firm.objects.filter(
        slug__in=ASSESSMENT_SLUGS, recruiting_style="assessment"
    ).update(recruiting_style="campus")


class Migration(migrations.Migration):

    dependencies = [
        ("directory", "0016_firm_recruiting_style"),
    ]

    operations = [
        migrations.RunPython(seed_assessment, unseed_assessment),
    ]
