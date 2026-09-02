"""reclassify — re-derive `bucket`, `cohort` and `class_year` for every
existing opportunity.

    python manage.py reclassify            # apply
    python manage.py reclassify --dry-run  # report what would change

Why a command and not a data migration: the classifier
(directory/classify.py) will keep evolving as new title shapes show up on the
boards; each improvement needs a cheap, idempotent way to re-run over rows
already in the shared table. A migration runs once — this runs whenever the
rules change. New rows are classified at ingest; this backfills the rest.

The board-level campus hint is reconstructed from the boards.py catalog via
`classify.campus_hint_pairs`: a (firm slug, provider) pair gets
`campus_hint=True` only when EVERY catalog board sharing that pair is
campus-scoped (e.g. Blackstone's Blackstone_Campus_Careers Workday site).
Rows whose firm/provider pair is not in the catalog, or whose catalog boards
disagree (Solomon Partners and Citi each run one campus board and one
non-campus board on the same provider), fall back to no hint, which only
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

import re

from directory.boards import BOARDS
from directory.classify import (
    bucket_from_contract, campus_hint_pairs, classify_role, clean_title, cohort_from_provider_title,
    derive_class_year, extract_class_year, extract_cohort,
    extract_deadline_from_text, extract_sponsorship, normalize_region, region_from_fields,
    region_from_prose, region_from_title_segments, posting_text,
)
from directory.models import Opportunity

# "/job/Copenhagen/..." -> "Copenhagen"; "/job/2-Locations/..." exists too,
# which is why the captured city still goes through normalize_region rather
# than being trusted as a place.
_EXTERNAL_PATH_CITY = re.compile(r"^/job/([^/]+)/")


class Command(BaseCommand):
    help = "Re-run the role classifier over every opportunities row (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]

        # See classify.campus_hint_pairs: a stored row only remembers its
        # provider ("workday", "greenhouse", ...), not which of a firm's
        # several boards on that provider produced it, so a pair counts only
        # when EVERY catalog board sharing it is campus-scoped — not just any
        # one of them (Solomon Partners and Citi each run one campus board
        # and one non-campus board on the same provider).
        campus_pairs = campus_hint_pairs(BOARDS)

        counts: dict[str, int] = {}
        changed = 0
        with transaction.atomic():
            qs = Opportunity.objects.select_related("firm").order_by("id")
            for opp in qs.iterator():
                hint = (opp.firm.slug, opp.source) in campus_pairs
                title = clean_title(opp.title)[:255]
                # Mirrors ingest exactly (a nightly re-run must not undo what
                # ingest classified): the provider's own filing decides
                # outright where it speaks, the title rules where it is silent.
                bucket = (bucket_from_contract(
                              (opp.raw or {}).get("contract_type"), title)
                          or classify_role(title, campus_hint=hint))
                cohort = (opp.cohort or extract_cohort(title)
                          or cohort_from_provider_title(opp.raw))
                class_year = extract_class_year(title)
                # The convention-derived graduation year, only where the
                # posting states none of its own — neither in the title nor
                # in the graduation window read from its description.
                stated_grad = ((opp.raw or {}).get("facts") or {}).get("grad")
                class_year_derived = "" if (class_year or stated_grad) else \
                    derive_class_year(bucket, title, cohort)[0]
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
                # Same gate, on the provider's ORIGINAL title: boards route
                # location into leading pipe-segments ("APAC | Singapore |
                # Global Markets Recruitment Event") which clean_title strips
                # — correctly for display, but the stored title has therefore
                # already lost the one place the row ever stated. The raw
                # payload still carries the uncleaned title.
                if not region and not (opp.location or "").strip():
                    region = region_from_title_segments(
                        (opp.raw or {}).get("title") or "")
                # Workday's list payload writes "2 Locations" into the
                # location field and hides the cities — but its own
                # externalPath starts with the PRIMARY posting city:
                # "/job/Copenhagen/Key-Account-Manager...". The provider's
                # routing, not a guess. Read only when the location field
                # said nothing usable.
                if not region:
                    path = (opp.raw or {}).get("externalPath") or ""
                    m = _EXTERNAL_PATH_CITY.match(path)
                    if m:
                        from urllib.parse import unquote

                        # A Workday slug's hyphen carries two different
                        # meanings and only the reader can tell them apart:
                        # it joins the words of one name
                        # ("United-States", "New-York") and it separates a
                        # city from its state ("OLATHE-KS", where the
                        # state-suffix rule needs a comma). Try both
                        # readings, space first — "Lafayette-Louisiana---
                        # United-States" resolves on the space reading and
                        # "OLATHE-KS" only on the comma one. Applied to this
                        # captured segment alone, whose shape we know; the
                        # same substitution against free text would make an
                        # American address of any hyphenated word ending in
                        # a state code.
                        seg = unquote(m.group(1))
                        region = (normalize_region(seg.replace("-", " "))
                                  or normalize_region(seg.replace("-", ", ")))
                # The provider's own location STRUCTURES, stored in raw and
                # never read until now: Goldman's city/state/country dicts
                # (the only honest way to place a bare "Birmingham" — its
                # country field says United Kingdom), McKinsey's parallel
                # cities/countries arrays ("San Jose" + "Costa Rica"),
                # Greenhouse's location.name, and the `detail_location` a
                # posting page's structured data states (enrich_postings).
                # Not gated on an empty location field: these ARE the
                # location field, stated with more words.
                if not region:
                    region = region_from_fields(opp.raw)
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
                elif confidence < 1.0 and (opp.raw or {}).get("detail_text"):
                    # A PROSE-derived date has to keep tracking the prose it
                    # came from. Everything else in this loop is fill-only,
                    # and for a value read out of a page that the page can
                    # later change, fill-only means wrong forever.
                    #
                    # Live example: HSBC's markets internship carried
                    # 2026-08-08, a date that appears nowhere in its current
                    # text, while the page itself says "Closing Date: Fri Oct
                    # 30, 2026" — so the board showed "Deadline passed" on a
                    # role open for another eleven weeks, and kept showing it
                    # because a set deadline was never re-read.
                    #
                    # Gated on actually HOLDING the text: with no
                    # detail_text there is nothing to disagree with, and
                    # clearing on that basis would delete good dates from
                    # rows whose page simply has not been fetched. Never
                    # touches confidence 1.0 — that is the provider's own
                    # field, and prose does not get a vote against it.
                    from_text = extract_deadline_from_text(text)
                    parsed = parse_date(from_text) if from_text else None
                    if parsed != deadline:
                        deadline, precision, confidence = (
                            (parsed, "day", 0.6) if parsed else (None, "", 0.0))

                if (bucket != opp.bucket or cohort != opp.cohort
                        or class_year != opp.class_year
                        or class_year_derived != opp.class_year_derived
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
                        opp.class_year_derived = class_year_derived
                        opp.region = region
                        opp.sponsorship = sponsorship
                        opp.deadline = deadline
                        opp.deadline_precision = precision
                        opp.confidence = confidence
                        opp.save(update_fields=[
                            "title", "bucket", "cohort", "class_year",
                            "class_year_derived", "region",
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
