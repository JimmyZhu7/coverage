"""backfill_sponsorship — re-run classify.extract_sponsorship over every open
row's already-cached posting text, filling in rows the extractor could not
yet answer.

    python manage.py backfill_sponsorship           # report only (default)
    python manage.py backfill_sponsorship --apply    # write the recovered answers

WHY THIS EXISTS
---------------
`docs/founder-decisions-2026-08-20.md`, Decision 3, measured 2,304 of 2,435
open campus rows reading `sponsorship="unknown"` even though 636 of them
(PwC's Workday postings) carry a structured "Available for Work Visa
Sponsorship? Yes/No" field, and roughly 50 more use declarative phrasings
the extractor's phrase lists didn't cover — both readable straight out of
`raw["detail_text"]`, already sitting on the row, no network call needed.

`extract_sponsorship` in directory/classify.py now reads both. This command
is the one-time catch-up for rows that were classified before that fix and
are sitting on cached text the extractor never got a second look at — the
same "no network, safe to re-run whenever the pattern changes" shape as
`extract_facts`, scoped to one column.

`reclassify` already re-derives sponsorship as one of several fields it
touches (title, bucket, cohort, region, deadline, sponsorship). This command
exists alongside it, not instead of it, so a sponsorship-only backfill can be
measured and reported on its own — reclassify remains the one to run when any
of those OTHER fields need a fresh pass.

Fill-only, same rule as ingest and reclassify: a value is only written where
the row currently reads "" or "unknown", and only when the extractor found an
actual answer. A posting's own stated sponsorship answer — or a prior run's
recovered answer — is never overwritten, and "unknown" is never written over
an existing "unknown" (there is nothing to gain and nothing to prove idempotent
about a no-op write). Re-running after a first `--apply` finds nothing left to
do.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from directory.classify import extract_sponsorship, posting_text
from directory.models import Opportunity


class Command(BaseCommand):
    help = ("Re-run extract_sponsorship over cached posting text for open rows "
            "still unknown. Reports by default.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Write the recovered sponsorship answers. Default is report-only.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after N rows (0 = all).",
        )

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        tag = "" if apply_ else "[dry-run] "

        candidates: list[tuple[Opportunity, str]] = []
        qs = (Opportunity.objects.filter(status="open")
              .filter(sponsorship__in=("", "unknown"))
              .select_related("firm")
              .order_by("firm__name", "id"))
        for o in qs.iterator(chunk_size=200):
            text = posting_text(o.title, o.raw or {})
            if not text:
                continue
            extracted = extract_sponsorship(text)
            # Fill-only: never write "unknown" over "unknown" — there is
            # nothing recovered, and it would make every run look like it
            # changed something.
            if extracted == "unknown":
                continue
            candidates.append((o, extracted))

        if opts["limit"]:
            candidates = candidates[: opts["limit"]]

        if not candidates:
            self.stdout.write("Nothing to backfill.")
            return

        yes = sum(1 for _, v in candidates if v == "yes")
        no = sum(1 for _, v in candidates if v == "no")

        for o, new_value in candidates:
            self.stdout.write(
                f"{tag}{o.firm.name} #{o.id} — {o.title[:52]}: "
                f"{o.sponsorship!r} -> {new_value!r}")
            if apply_:
                o.sponsorship = new_value
                o.save(update_fields=["sponsorship"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{len(candidates)} row(s) {'updated' if apply_ else 'would be updated'} "
            f"({yes} yes / {no} no)."))
