"""reverify — liveness-check stale open opportunities against their source.

    python manage.py reverify                  # oldest stale rows, capped
    python manage.py reverify --limit 50
    python manage.py reverify --max-age-days 3
    python manage.py reverify --dry-run

This is the staleness layer's active half. `scrape` refreshes every posting a
board still returns, and its closed-detection catches postings that vanish
from a *successfully fetched* board — but a posting can also die in ways a
list fetch never shows (URL 404s while the board hides it, a board that has
rotated away, a firm whose fetch keeps failing). `reverify` walks the open
rows whose `last_checked` is oldest and asks the provider's own
verify endpoint, one URL at a time:

- "verified-open"       -> stamp last_verified + last_checked (fresh again),
                           and refresh `deadline` from the verify result's
                           own `deadline_dates` when it reports one -- a
                           provider's verify endpoint is read fresh every
                           pass, so it is the one place a stale `deadline`
                           (set once at first ingest, never revisited by
                           `scrape`'s list-endpoint fetch for a provider
                           whose list API carries no deadline field, e.g.
                           Workday) gets to be corrected once the firm bakes
                           an updated one into the posting.
- "closed"              -> status="closed" (with last_checked)
- "unreachable"         -> stamp last_checked only — an error is never
                           evidence of life OR death (same rule as ingest's
                           failed-board guard)
- "needs-verification"  -> stamp last_checked only; the URL doesn't carry
                           enough to decide deterministically

The honesty contract: `last_verified` moves ONLY on a positive liveness
signal. The card's "Verified N Days Ago" pill therefore never lies.

Volume: capped at --limit rows per run (default 200), oldest first, fetched
through a small thread pool — the same politeness posture as fetch_many.
Each run is recorded in scrape_runs (connector="reverify").
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from coverage_connectors import verify

from directory.models import Opportunity, ScrapeRun


def _fresh_deadline(deadline_dates: list[str]) -> date | None:
    """The first genuinely-parseable date in a verify result's
    `deadline_dates`, or `None`. `VerificationResult.deadline_dates` is
    documented (coverage_connectors/models.py) to hold only "genuine
    deadline-type dates the provider's verify endpoint exposed" -- this
    trusts that contract rather than re-guessing which entry is the real
    deadline. A value that fails to parse is skipped, never invented."""
    for value in deadline_dates or []:
        try:
            return date.fromisoformat((value or "")[:10])
        except (ValueError, TypeError):
            continue
    return None


class Command(BaseCommand):
    help = "Re-check stale open opportunities against their provider's verify endpoint."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200,
                            help="Max rows to check this run (oldest first).")
        parser.add_argument("--max-age-days", type=int, default=7,
                            help="Only rows not checked in this many days are candidates.")
        parser.add_argument("--workers", type=int, default=8)
        parser.add_argument("--dry-run", action="store_true",
                            help="Verify and report, but write nothing.")

    def handle(self, *args, **opts):
        now = timezone.now()
        cutoff = now - timedelta(days=opts["max_age_days"])

        candidates = list(
            Opportunity.objects.filter(status="open")
            .filter(last_checked__lt=cutoff)
            .order_by("last_checked")[: opts["limit"]]
        )
        if not candidates:
            self.stdout.write("Nothing stale enough to re-check.")
            return

        run = ScrapeRun.objects.create(connector="reverify", started=now, status="running")
        stats = {"checked": 0, "verified_open": 0, "closed": 0,
                 "unreachable": 0, "needs_verification": 0}

        def check(opp):
            try:
                return opp, verify(opp.url)
            except Exception:  # noqa: BLE001 — one bad URL must not kill the run
                return opp, None

        with ThreadPoolExecutor(max_workers=opts["workers"]) as pool:
            results = list(pool.map(check, candidates))

        for opp, result in results:
            stats["checked"] += 1
            verdict = result.result if result is not None else "unreachable"
            key = verdict.replace("-", "_")
            stats[key] = stats.get(key, 0) + 1
            if opts["dry_run"]:
                continue
            opp.last_checked = now
            if verdict == "verified-open":
                opp.last_verified = now
                update_fields = ["last_checked", "last_verified"]
                fresh = _fresh_deadline(result.deadline_dates) if result is not None else None
                if fresh is not None and fresh != opp.deadline:
                    opp.deadline = fresh
                    opp.deadline_precision = "day"
                    update_fields += ["deadline", "deadline_precision"]
                opp.save(update_fields=update_fields)
            elif verdict == "closed":
                opp.status = "closed"
                opp.closed_at = now
                opp.save(update_fields=["last_checked", "status", "closed_at"])
            else:
                # unreachable / needs-verification: we looked, we can't say —
                # last_verified deliberately does NOT move.
                opp.save(update_fields=["last_checked"])

        run.finished = timezone.now()
        run.stats = stats
        run.status = "ok" if not opts["dry_run"] else "dry-run"
        run.save()

        label = "would update" if opts["dry_run"] else "updated"
        self.stdout.write(self.style.SUCCESS(
            f"reverify [{label}]: {stats['checked']} checked — "
            f"{stats['verified_open']} open, {stats['closed']} closed, "
            f"{stats['unreachable']} unreachable, {stats['needs_verification']} undecidable"
        ))
