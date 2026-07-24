"""refresh — the one command a scheduler runs: scrape, classify, re-verify.

    python manage.py refresh
    python manage.py refresh --no-reverify      # fetch + classify only
    python manage.py refresh --reverify-limit 50

This is what "the calendar updates itself" means concretely (build-plan §7,
M1's definition of done). It chains the three existing commands rather than
reimplementing them, so each stays independently runnable and tested:

1. `scrape`      — fetch every catalog board once, upsert the shared table
                   (new rows are classified at ingest).
2. `reclassify`  — re-derive bucket/cohort across ALL rows, so a classifier
                   improvement shipped since the last run reaches old rows.
3. `reverify`    — liveness-check the stalest open rows (capped).

A failure in one stage is reported but does not stop the later stages — a
broken board must not leave yesterday's staleness unre-checked.
"""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Scrape + reclassify + reverify — the scheduled freshness pass."

    def add_arguments(self, parser):
        parser.add_argument("--no-reverify", action="store_true")
        parser.add_argument("--reverify-limit", type=int, default=200)

    def handle(self, *args, **opts):
        failures = []
        for label, runner in (
            ("scrape", lambda: call_command("scrape")),
            ("reclassify", lambda: call_command("reclassify")),
            ("reverify", (None if opts["no_reverify"]
                          else lambda: call_command("reverify", limit=opts["reverify_limit"]))),
        ):
            if runner is None:
                continue
            self.stdout.write(f"── {label} ──")
            try:
                runner()
            except Exception as exc:  # noqa: BLE001 — carry on; report at the end
                failures.append((label, str(exc)))
                self.stderr.write(self.style.WARNING(f"{label} failed: {exc}"))
        if failures:
            raise SystemExit(f"refresh finished with failures: {[f[0] for f in failures]}")

        # Freshness sanity check: a "successful" pass that leaves zero open
        # roles means the scrape silently produced nothing (every connector
        # broke, or a shared dependency did). Exit non-zero so the scheduler
        # flags the run — stale data presented as fresh is the one failure
        # mode this product can't afford.
        from directory.models import Opportunity

        open_count = Opportunity.objects.filter(status="open").count()
        if open_count == 0:
            raise SystemExit(
                "refresh completed but zero opportunities are open — "
                "treating as a failed pass (all connectors likely broken)."
            )
        self.stdout.write(self.style.SUCCESS(f"refresh complete. {open_count} open."))
