"""reclassify — re-derive `bucket`, `cohort` and `class_year` for every
existing opportunity.

    python manage.py reclassify            # apply
    python manage.py reclassify --dry-run  # report what would change

Why a command and not a data migration: the classifier
(directory/classify.py) will keep evolving as new title shapes show up on the
boards; each improvement needs a cheap, idempotent way to re-run over rows
already in the shared table. A migration runs once — this runs whenever the
rules change. New rows are classified at ingest; this backfills the rest.

The board-level campus hint is reconstructed from the boards.py catalog: a
(firm slug, provider) pair whose catalog board is campus-scoped (e.g.
Blackstone's Blackstone_Campus_Careers Workday site) gets the same
`campus_hint=True` that a live ingest of that board would use. Rows whose
firm/provider pair is not in the catalog fall back to no hint, which only
means neutral Analyst titles stay in `other` — never a false promotion.

Cohort handling mirrors ingest: connector-supplied cohorts don't exist for
these providers (always ""), so a cohort already present that doesn't look
title-derived is left alone; blanks and stale title-derived values are
re-extracted from the title.

`class_year` (the graduation year a posting states outright, as opposed to
`cohort`'s programme year) is re-derived unconditionally rather than with
cohort's `existing or …` guard: nothing but the title has ever written it, so
there is no external value to preserve, and an unconditional pass is what lets a
tightened rule actually CLEAR a wrong value instead of only filling blanks.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from directory.boards import BOARDS
from directory.classify import (
    board_is_campus, classify_role, clean_title, extract_class_year, extract_cohort,
    normalize_region,
)
from directory.models import Opportunity


class Command(BaseCommand):
    help = "Re-run the role classifier over every opportunities row (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]

        campus_pairs: set[tuple[str, str]] = {
            (slug, board.provider) for slug, board in BOARDS if board_is_campus(board)
        }

        counts: dict[str, int] = {}
        changed = 0
        with transaction.atomic():
            qs = Opportunity.objects.select_related("firm").order_by("id")
            for opp in qs.iterator():
                hint = (opp.firm.slug, opp.source) in campus_pairs
                title = clean_title(opp.title)[:255]
                bucket = classify_role(title, campus_hint=hint)
                cohort = opp.cohort or extract_cohort(title)
                class_year = extract_class_year(title)
                region = normalize_region(opp.location)
                counts[bucket] = counts.get(bucket, 0) + 1
                if (bucket != opp.bucket or cohort != opp.cohort
                        or class_year != opp.class_year
                        or region != opp.region or title != opp.title):
                    changed += 1
                    if not dry:
                        opp.title = title
                        opp.bucket = bucket
                        opp.cohort = cohort
                        opp.class_year = class_year
                        opp.region = region
                        opp.save(update_fields=[
                            "title", "bucket", "cohort", "class_year", "region",
                        ])
            if dry:
                transaction.set_rollback(True)

        total = sum(counts.values())
        label = "would change" if dry else "changed"
        self.stdout.write(f"{total} opportunities scanned, {changed} {label}.")
        for bucket in ("insight", "internship", "entry_level", "other"):
            if bucket in counts:
                self.stdout.write(f"  {bucket:12} {counts[bucket]}")
