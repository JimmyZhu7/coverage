"""seed_logo_domains — give firms their own front-door domain.

    python manage.py seed_logo_domains --dry-run
    python manage.py seed_logo_domains

Most firms reached this directory through a board connector, and a connector
only ever needed the ATS host — a Greenhouse token, a Workday tenant. So 60 of
97 firms had NO domain at all, which meant `fetch_firm_logos` had nothing to
look up and the board showed monograms for Point72, Jane Street, Optiver, PwC
and forty others.

The map is hand-written and every entry was probed for a real logo before it
was committed (2026-08-05: 44 of 52 resolved). It only ever FILLS IN a firm
with no domains — it never edits one a connector or a seed already set, because
those are load-bearing for email-pattern matching, not just for logos.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from directory._logo_domains import DOMAINS
from directory.models import Firm


class Command(BaseCommand):
    help = "Fill in each firm's own domain so logo lookup has somewhere to go."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        tag = "[dry-run] " if opts["dry_run"] else ""
        added = skipped = unknown = 0
        for slug, domain in sorted(DOMAINS.items()):
            firm = Firm.objects.filter(slug=slug).first()
            if firm is None:
                unknown += 1
                self.stdout.write(f"{tag}?    no firm with slug {slug!r}")
                continue
            if domain in (firm.domains or []):
                skipped += 1
                continue
            # APPEND, never replace. `Firm.domains` also drives email-pattern
            # matching (a contact's address resolving to their firm), so
            # swapping `rbc.com` for the `jobs.rbc.com` that happens to serve
            # a better icon would quietly break contact matching to fix a
            # picture. Logo lookup tries every candidate and keeps the best,
            # so appending is all it needs.
            added += 1
            where = "only domain" if not firm.domains else "added for logo lookup"
            self.stdout.write(f"{tag}+    {firm.name}: {domain} ({where})")
            if not opts["dry_run"]:
                firm.domains = [*(firm.domains or []), domain]
                firm.save(update_fields=["domains"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{added} filled in, {skipped} already had one, {unknown} unknown slugs"))
