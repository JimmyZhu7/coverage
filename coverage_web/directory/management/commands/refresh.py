"""refresh — the one command a scheduler runs: scrape, classify, enrich, re-verify.

    python manage.py refresh
    python manage.py refresh --no-reverify      # fetch + classify only
    python manage.py refresh --no-enrich        # skip the detail-page pass
    python manage.py refresh --enrich-limit 150
    python manage.py refresh --reverify-limit 50

This is what "the calendar updates itself" means concretely (build-plan §7,
M1's definition of done). It chains the existing commands rather than
reimplementing them, so each stays independently runnable and tested:

1. `scrape`         — fetch every catalog board once, upsert the shared table
                      (new rows are classified at ingest).
2. `reclassify`     — re-derive bucket/cohort across ALL rows, so a classifier
                      improvement shipped since the last run reaches old rows.
3. `enrich_postings`— read each new posting's OWN page for the deadline and
                      sponsorship the list endpoints never carry.
4. `extract_facts`  — re-derive the application facts from the text stage 3
                      banked. No network; runs over every row, not just new
                      ones, so a pattern fixed today reaches a posting fetched
                      last month.
5. `reverify`       — liveness-check the stalest open rows (capped).

Then one piece of housekeeping, in-process rather than as a sixth command:
stages 1 and 5 both append to `opportunity_changes`, which is append-only
and would otherwise grow for as long as the cron runs. `OpportunityChange
.prune` drops rows past the retention window at the end of every pass —
`--prune-changes-older-than N` overrides the default (see
`OpportunityChange.RETENTION_DAYS` for why it is 180 days), and 0 keeps
everything, for an operator who wants a full season to analyse. This runs
even when a stage above failed: a broken board is no reason to let a table
grow unbounded.

Stages 3 and 4 were missing from this chain for exactly one day and the gap
was already visible: the newest posting on the board carried no description,
no deadline and no facts, because the only enrichment run had been a manual
one. A pipeline whose best data arrives by hand decays back to the list
endpoints, which carry ~250 characters and no closing date at all.

Order matters between 3 and 4 and nowhere else: extraction reads what
enrichment fetched, so a run that swapped them would derive facts from
yesterday's text.

A failure in one stage is reported but does not stop the later stages — a
broken board must not leave yesterday's staleness unre-checked.
"""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Scrape, classify, enrich, extract, re-verify — the scheduled freshness pass."

    def add_arguments(self, parser):
        parser.add_argument("--no-reverify", action="store_true")
        parser.add_argument("--reverify-limit", type=int, default=200)
        parser.add_argument("--no-enrich", action="store_true")
        # Bounded on purpose: enrichment is one HTTP request per posting at a
        # per-host crawl delay, so an unbounded first run on a fresh database
        # takes hours. 400 covers a normal day's new postings several times
        # over, and anything it does not reach is simply first in line
        # tomorrow — the queue is "rows with no detail_text yet", so it drains
        # by itself.
        parser.add_argument("--enrich-limit", type=int, default=400)
        # Retention for the append-only `opportunity_changes` table. Left
        # unset it uses `OpportunityChange.RETENTION_DAYS`; 0 disables the
        # sweep entirely and keeps every record.
        parser.add_argument("--prune-changes-older-than", type=int, default=None,
                            help="Delete OpportunityChange rows older than this "
                                 "many days (default: the model's RETENTION_DAYS; "
                                 "0 keeps everything).")

    def handle(self, *args, **opts):
        failures = []
        for label, runner in (
            ("scrape", lambda: call_command("scrape")),
            ("reclassify", lambda: call_command("reclassify")),
            ("enrich_postings", (None if opts["no_enrich"]
                                 else lambda: call_command("enrich_postings",
                                                           limit=opts["enrich_limit"]))),
            # Always runs, even when enrichment is skipped: the extractors
            # change far more often than the cached text does, and re-deriving
            # from text we already hold costs nothing but CPU.
            ("extract_facts", lambda: call_command("extract_facts")),
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
        # Housekeeping, and deliberately BEFORE the failure exit below:
        # stages 1 and 5 both append to `opportunity_changes`, so a pass
        # that skipped retention because some unrelated board broke would
        # let the table grow for as long as the breakage lasted — which is
        # exactly the stretch nobody is watching it.
        from directory.models import OpportunityChange

        pruned = OpportunityChange.prune(
            older_than_days=opts["prune_changes_older_than"]
        )
        if pruned:
            self.stdout.write(f"pruned {pruned} change record(s) past retention")

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

        # Board health, announced where a failure used to hide. These are
        # warnings, not failures: the pass succeeded, but a board that fails
        # every run (or has never yielded a row) means specific firms' data
        # is going stale while the overall run reports green.
        from directory.health import health_report

        for line in health_report():
            self.stderr.write(self.style.WARNING(line))

        self.stdout.write(self.style.SUCCESS(f"refresh complete. {open_count} open."))
