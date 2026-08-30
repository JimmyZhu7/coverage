"""audit_close_trust — Phase 1 gate: how much of `scrape.close` is evidence?

    python manage.py audit_close_trust

Read-only report over `directory.cycle_trust.classify_closes`. Exists so the
question "can `FirmCycleObservation` be built on this" is answered by running
something, not by eyeballing `ScrapeRun.stats` by hand across 346 rows. See
`cycle_trust.py`'s module docstring for what TRUSTED/SUSPECT actually check.
"""

from __future__ import annotations

import collections

from django.core.management.base import BaseCommand

from directory.cycle_trust import SUSPECT, TRUSTED, classify_closes
from directory.models import Opportunity


class Command(BaseCommand):
    help = "Report TRUSTED vs SUSPECT scrape.close events, by firm and by connector."

    def handle(self, *args, **opts):
        verdicts = classify_closes()
        total = len(verdicts)
        if total == 0:
            self.stdout.write("No scrape.close events recorded yet.")
            return

        trusted = [v for v in verdicts if v.verdict == TRUSTED]
        suspect = [v for v in verdicts if v.verdict == SUSPECT]

        # One query for the (firm name, source) every verdict needs, rather
        # than N+1 — this command reads ~6,000 rows.
        opp_ids = [v.opportunity_id for v in verdicts]
        meta = {
            o.id: (o.firm.name, o.source)
            for o in Opportunity.objects.filter(id__in=opp_ids).select_related("firm")
        }

        self.stdout.write(self.style.SUCCESS(
            f"{total} close events: {len(trusted)} trusted "
            f"({len(trusted) / total:.1%}), {len(suspect)} suspect "
            f"({len(suspect) / total:.1%})"
        ))

        by_firm = collections.Counter()
        by_firm_suspect = collections.Counter()
        by_connector = collections.Counter()
        by_connector_suspect = collections.Counter()
        for v in verdicts:
            firm, source = meta.get(v.opportunity_id, ("?", "?"))
            by_firm[firm] += 1
            by_connector[source] += 1
            if v.verdict == SUSPECT:
                by_firm_suspect[firm] += 1
                by_connector_suspect[source] += 1

        self.stdout.write("\nBy connector (source):")
        for source, n in by_connector.most_common():
            s = by_connector_suspect.get(source, 0)
            self.stdout.write(f"  {source:20s} {n:5d} total, {s:5d} suspect")

        if suspect:
            self.stdout.write("\nFirms with any suspect close:")
            for firm, s in by_firm_suspect.most_common():
                self.stdout.write(f"  {firm:35s} {s:4d} suspect / {by_firm[firm]:4d} total")
            self.stdout.write("\nSample suspect reasons:")
            seen_reasons = set()
            for v in suspect:
                if v.reason in seen_reasons:
                    continue
                seen_reasons.add(v.reason)
                self.stdout.write(f"  {v.reason}")
                if len(seen_reasons) >= 15:
                    break
        else:
            self.stdout.write("\nNo suspect closes found in the current data.")
