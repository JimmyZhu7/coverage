"""backfill_sitemap_postal_titles — apply sitemap.py's postal-code split to
sitemap-sourced rows already sitting in the DB from before that fix.

    python manage.py backfill_sitemap_postal_titles           # report only (default)
    python manage.py backfill_sitemap_postal_titles --apply    # write title/location

WHY THIS EXISTS
---------------
`coverage_connectors.sitemap._rows()` reconstructs a posting's title from its
URL slug (sitemaps carry no titles), and some slugs glue a postal code onto
the end — e.g. HSBC opportunity 17423's stored title was "New York
Investment Banking Graduate NY 10001" with `location` left blank. The
connector now recognizes a trailing US "<STATE> <ZIP>" pair or UK postcode
and moves it from the title into `location` on every future fetch — see
`sitemap._split_trailing_postal_code`. That fix only reaches rows the next
time they're fetched; this command is the one-time catch-up for whatever is
already stored, reading `Opportunity.title` directly and re-running the same
split function the connector itself uses (no network, no guessing beyond
what that function already does).

Scope is deliberately narrow: only `source="sitemap"` rows, and only where
the split actually recognizes a trailing postal code AND the row's stored
`location` is still blank (a row with a real location already filled in —
by hand, or by a later re-fetch — is left alone rather than overwritten).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from coverage_connectors.sitemap import _split_trailing_postal_code
from directory.models import Opportunity


class Command(BaseCommand):
    help = ("Move a recognized trailing postal code out of sitemap-sourced "
            "titles and into `location`. Reports by default.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Write the corrected title/location. Default is report-only.",
        )
        parser.add_argument(
            "--ids", type=str, default="",
            help="Comma-separated Opportunity ids to scope to (default: all "
                 "sitemap rows).",
        )

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        tag = "" if apply_ else "[dry-run] "

        qs = Opportunity.objects.filter(source="sitemap").select_related("firm")
        if opts["ids"]:
            ids = [int(x) for x in opts["ids"].split(",") if x.strip()]
            qs = qs.filter(id__in=ids)

        candidates = []
        for o in qs.order_by("firm__name", "id"):
            if o.location:
                continue  # already has a location — a later fetch or a hand fix
            new_title, new_location = _split_trailing_postal_code(o.title or "")
            if new_location:
                candidates.append((o, new_title, new_location))

        if not candidates:
            self.stdout.write("Nothing to backfill.")
            return

        for o, new_title, new_location in candidates:
            self.stdout.write(
                f"{tag}{o.firm.name} #{o.id}: title {o.title!r} -> {new_title!r}, "
                f"location '' -> {new_location!r}")
            if apply_:
                o.title = new_title
                o.location = new_location
                o.save(update_fields=["title", "location"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{len(candidates)} row(s) "
            f"{'updated' if apply_ else 'would be updated'}."))
