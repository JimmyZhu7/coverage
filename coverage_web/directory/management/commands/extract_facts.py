"""Re-read the descriptions we already hold and write down what they say.

`enrich_postings` does the expensive half — one HTTP request per posting — and
banks the full text in `raw["detail_text"]`. This command does the cheap half
over that cache: no network, no rate limit, safe to run as often as the
extractors change, which early on is often.

That split is the point. Every regex in directory/facts.py will be wrong about
something on first contact with 854 real job descriptions, and the fix cycle
has to be "edit the pattern, re-run, look at the diff" rather than "edit the
pattern, re-crawl 854 pages, hope". Nothing here touches a network.

    python manage.py extract_facts            # write facts for every open role
    python manage.py extract_facts --dry-run  # count what would change
    python manage.py extract_facts --show gpa # print the evidence for one kind
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand
from django.utils import timezone

from directory.classify import TARGET_BUCKETS
from directory.facts import EXTRACTORS, extract_facts
from directory.models import Opportunity


class Command(BaseCommand):
    help = "Extract application facts from cached posting descriptions."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Count what would change, write nothing.")
        parser.add_argument("--show", metavar="KIND",
                            help=f"Print every hit for one kind: {', '.join(EXTRACTORS)}")
        parser.add_argument("--limit", type=int, default=0,
                            help="Stop after N postings (for a quick look).")

    def handle(self, *args, **opts):
        dry, show, limit = opts["dry_run"], opts["show"], opts["limit"]
        if show and show not in EXTRACTORS:
            self.stderr.write(f"Unknown kind {show!r}. Known: {', '.join(EXTRACTORS)}")
            return

        qs = (Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
              .select_related("firm").order_by("id"))
        hits: Counter = Counter()
        seen = changed = 0

        for opp in qs.iterator(chunk_size=200):
            text = (opp.raw or {}).get("detail_text") or ""
            if not text:
                continue
            seen += 1
            facts = extract_facts(text)
            for kind, fact in facts.items():
                hits[kind] += 1
                if show == kind:
                    self.stdout.write(
                        f"{opp.firm.name} — {opp.title[:44]}\n"
                        f"    {fact['value']}  ·  {fact['phrase'][:120]}")

            # Rewrite wholesale rather than merging: these are DERIVED, and a
            # pattern that stops matching must be able to retract its fact. A
            # merge would make every extractor's output permanent from the
            # first run that produced it, which is the opposite of what a
            # re-runnable derivation is for.
            if (opp.raw or {}).get("facts") == facts:
                continue
            changed += 1
            if not dry:
                raw = dict(opp.raw or {})
                raw["facts"] = facts
                raw["facts_at"] = timezone.now().isoformat(timespec="seconds")
                opp.raw = raw
                opp.save(update_fields=["raw"])

            if limit and seen >= limit:
                break

        summary = " · ".join(f"{k} {n}" for k, n in hits.most_common()) or "nothing found"
        self.stdout.write(self.style.SUCCESS(
            f"{seen} descriptions read, {changed} row{'' if changed == 1 else 's'} "
            f"{'would change' if dry else 'updated'} — {summary}"))
