"""Seed `Firm.recruiting_style = "assessment"` for the firms on the board that
hire off a test rather than off a conversation.

WHY A DATA MIGRATION. `recruiting_style` is a firm-level fact the Coverage
Gaps strip, the "Who to find" panel and the Network summary all read
(`crm.coverage`, `crm.sourcing`). Set by hand in a shell it would be true on
one database and silently false on the next; written here it is reproducible
on every database that runs the migration, and the slug list below is the
record of exactly which firms were tagged and why.

THE EVIDENCE. Jane Street's public FAQ answers "Can I schedule a phone call
or coffee?" with "unfortunately, no". Citadel Securities' campus funnel is
Datathons and Invitationals. The practitioner finding, verbatim: "if you
can't pass their tests it doesn't matter who you know". For these firms a
coffee chat does not move the process, so the product must stop ranking them
as networking gaps to fill and stop proposing "an analyst to chat with".

THE SLUGS. Prop-trading and quant market-making names, tagged where they
exist on the board (`filter(slug__in=...)` ignores absent ones, so a fresh
database that has not scraped a firm yet is fine):

    janestreet, citadel, citadelsecurities / citadel-securities, sig, imc,
    jump, drw, hrt, optiver, akuna, belvedere, fiverings, flowtraders,
    tower / towerresearch, virtu, xtx, squarepoint, qube

Two firms are listed under both the spelling the catalog uses and the one
the request used — Citadel Securities is `citadel-securities` on the live
board, Tower Research is `towerresearch` — so the seed lands whichever a
database carries. On the founder's board 2026-09-01 that is 18 rows.

DELIBERATELY NOT TAGGED: Millennium, Point72, AQR and the other multi-strat
hedge funds. They run analyst programmes with real networking (referrals,
campus events, coffee chats that reach a hiring manager), so they stay
`campus` and keep every networking prompt.

A FRESH DEPLOY runs migrations before `seed_directory`/`scrape` create any
of these rows, so this seed finds nothing there; the firm rows `scrape`
pre-creates from `directory.boards.DEFAULT_TRACKS` arrive as `campus` until
the pre-create learns the style too. On every existing database this is the
whole seed.

Reverse resets exactly these slugs to `campus`, and only where the value is
still `assessment` — a firm an admin re-tagged by hand is left alone.
"""

from __future__ import annotations

from django.db import migrations

ASSESSMENT_SLUGS = (
    "janestreet",
    "citadel",
    "citadelsecurities",
    "citadel-securities",
    "sig",
    "imc",
    "jump",
    "drw",
    "hrt",
    "optiver",
    "akuna",
    "belvedere",
    "fiverings",
    "flowtraders",
    "tower",
    "towerresearch",
    "virtu",
    "xtx",
    "squarepoint",
    "qube",
)


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
