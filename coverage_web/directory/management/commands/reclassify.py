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
from django.utils.dateparse import parse_date

from directory.boards import BOARDS
from directory.classify import (
    board_is_campus, classify_role, clean_title, contract_is_campus, extract_class_year, extract_cohort,
    extract_deadline_from_text, extract_sponsorship, normalize_region, region_from_prose,
    posting_text,
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
                # Board-level hint OR the row's own provider-stated contract
                # type — same pair as ingest, and reclassify must mirror
                # ingest exactly or a nightly re-run silently undoes what
                # ingest classified (the drift this command's docstring warns
                # about, in the other direction).
                row_hint = contract_is_campus((opp.raw or {}).get("contract_type"))
                bucket = classify_role(title, campus_hint=hint or row_hint)
                cohort = opp.cohort or extract_cohort(title)
                class_year = extract_class_year(title)
                region = normalize_region(opp.location)
                # Title fallback, and ONLY when the row carries no location at
                # all (327 open campus rows on live data — boards that route
                # the city into the title instead: "2027 Investment Banking
                # Summer Internship Program - Singapore", "Financial Advisor
                # Trainee (Macon, GA)"). Those rows are invisible to every
                # concrete Region filter, which is a worse answer than a
                # derived one.
                #
                # Gated on an EMPTY location rather than an unresolved one,
                # because a title's market word is often the desk it covers
                # and not the office it sits in: "Analyst - UKCB Origination"
                # is based in Noida, "Equity Research, China Industrials" in
                # Hong Kong, "Head of Compliance, Hong Kong" in Singapore. A
                # row that stated a location we could not parse has already
                # told us the title is not where it is.
                if not region and not (opp.location or "").strip():
                    region = normalize_region(title)
                # Still nothing: the posting's own cached prose, through the
                # location-anchored extractor. Fill-only like everything else
                # in this loop, and honest about its limits — on live data 18
                # of 240 unregioned roles state a market this way, 43 state a
                # location OUTSIDE the four markets (correctly left blank),
                # and the rest never say where they are at all.
                if not region:
                    from directory.facts import _clean

                    region = region_from_prose(
                        _clean((opp.raw or {}).get("detail_text") or ""))
                counts[bucket] = counts.get(bucket, 0) + 1

                # Prose re-extraction over the STORED raw payload — the whole
                # point of keeping it. Fill-only, same rules as ingest: prose
                # answers only where nothing is recorded, so a re-run can
                # improve rows and can never overwrite a provider's field.
                text = posting_text(title, opp.raw)
                sponsorship = opp.sponsorship
                if sponsorship in ("", "unknown"):
                    extracted = extract_sponsorship(text)
                    if extracted != "unknown":
                        sponsorship = extracted
                deadline = opp.deadline
                precision = opp.deadline_precision
                confidence = opp.confidence
                if deadline is None:
                    from_text = extract_deadline_from_text(text)
                    if from_text:
                        parsed = parse_date(from_text)
                        if parsed:
                            deadline, precision, confidence = parsed, "day", 0.6
                elif confidence == 0.0 and precision == "day":
                    # Backfill: rows dated BEFORE confidence had meaning all
                    # got their date from a provider API field (prose
                    # extraction didn't exist yet), which is the 1.0 tier.
                    confidence = 1.0

                if (bucket != opp.bucket or cohort != opp.cohort
                        or class_year != opp.class_year
                        or region != opp.region or title != opp.title
                        or sponsorship != opp.sponsorship
                        or deadline != opp.deadline
                        or confidence != opp.confidence):
                    changed += 1
                    if not dry:
                        opp.title = title
                        opp.bucket = bucket
                        opp.cohort = cohort
                        opp.class_year = class_year
                        opp.region = region
                        opp.sponsorship = sponsorship
                        opp.deadline = deadline
                        opp.deadline_precision = precision
                        opp.confidence = confidence
                        opp.save(update_fields=[
                            "title", "bucket", "cohort", "class_year", "region",
                            "sponsorship", "deadline", "deadline_precision",
                            "confidence",
                        ])
            if dry:
                transaction.set_rollback(True)

        total = sum(counts.values())
        label = "would change" if dry else "changed"
        self.stdout.write(f"{total} opportunities scanned, {changed} {label}.")
        for bucket in ("insight", "internship", "entry_level", "other"):
            if bucket in counts:
                self.stdout.write(f"  {bucket:12} {counts[bucket]}")
