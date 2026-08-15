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
rows whose `deadline_checked_at` is oldest (NULL first) and asks the
provider's own verify endpoint, one URL at a time:

`deadline_checked_at`, not `last_checked`, drives candidate selection —
deliberately. `scrape`'s routine list-refetch bumps `last_checked` on every
successful pass for EVERY provider, including ones whose list endpoint
carries no deadline field at all (Workday's `fetch()`, by its own
docstring). A firm scraped more often than this command's `--max-age-days`
cutoff would therefore never go `last_checked`-stale, so a candidate query
keyed on it would starve those rows forever — `reverify` running on a
schedule would still never reach the one posting whose `deadline` has sat
frozen since first ingest while `last_verified` reads as today. Only this
command's own detail-level check moves `deadline_checked_at`, so the cutoff
means what it says.

Every branch below stamps `last_checked` + `deadline_checked_at` — a check
happened this pass either way, and both fields exist to mean exactly that.

- "verified-open"       -> also stamp last_verified (fresh again), and
                           refresh `deadline` from the verify result's own
                           `deadline_dates` when it reports one -- a
                           provider's verify endpoint is read fresh every
                           pass, so it is the one place a stale `deadline`
                           (set once at first ingest, never revisited by
                           `scrape`'s list-endpoint fetch for a provider
                           whose list API carries no deadline field, e.g.
                           Workday) gets to be corrected once the firm bakes
                           an updated one into the posting.
- "closed"              -> status="closed"
- "unreachable"         -> nothing else — an error is never evidence of
                           life OR death (same rule as ingest's failed-board
                           guard)
- "needs-verification"  -> nothing else; the URL doesn't carry enough to
                           decide deterministically

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
from django.db.models import F, Q
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
            .filter(Q(deadline_checked_at__isnull=True) | Q(deadline_checked_at__lt=cutoff))
            .order_by(F("deadline_checked_at").asc(nulls_first=True))[: opts["limit"]]
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
            # A detail-level check happened this pass regardless of verdict —
            # this is the ONLY thing that should ever move this field (see
            # module docstring). Stamping it unconditionally here is what
            # lets a genuinely-unreachable or undecidable row age out of the
            # next run's candidates too, instead of being retried forever.
            opp.deadline_checked_at = now
            update_fields = ["last_checked", "deadline_checked_at"]
            if verdict == "verified-open":
                opp.last_verified = now
                update_fields.append("last_verified")
                fresh = _fresh_deadline(result.deadline_dates) if result is not None else None
                if fresh is not None and fresh != opp.deadline:
                    opp.deadline = fresh
                    opp.deadline_precision = "day"
                    update_fields += ["deadline", "deadline_precision"]
                opp.save(update_fields=update_fields)
            elif verdict == "closed":
                opp.status = "closed"
                opp.closed_at = now
                opp.save(update_fields=update_fields + ["status", "closed_at"])
            else:
                # unreachable / needs-verification: we looked, we can't say —
                # last_verified deliberately does NOT move.
                opp.save(update_fields=update_fields)

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
