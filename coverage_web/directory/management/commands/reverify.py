"""reverify — liveness-check stale open opportunities against their source.

    python manage.py reverify                  # oldest stale rows, capped
    python manage.py reverify --limit 50
    python manage.py reverify --max-age-days 3
    python manage.py reverify --dry-run
    python manage.py reverify --ids 9446,9579   # scoped, audit-named backfill

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

Every move this pass makes — a `deadline` corrected from the provider's own
answer, a posting flipped closed — is also recorded row-by-row in
`directory.models.OpportunityChange`. The deadline overwrite above was
silent until then: the stored date was gone the instant the fresh one was
assigned, so nothing downstream could tell a student tracking that role
that its deadline had moved at all.

Volume: capped at --limit rows per run (default 200), oldest first, fetched
through a small thread pool — the same politeness posture as fetch_many.
Each run is recorded in scrape_runs (connector="reverify").

`--ids` is a targeted backfill for rows a specific audit already named —
mirrors `enrich_postings --ids` exactly, and exists for the same reason:
`deadline_checked_at` was NULL catalog-wide the moment it was introduced
(nothing had ever run the deep check before), so a routine scheduled pass
reaches the oldest-NULL rows in `--limit`-sized batches over many runs. A
row a live audit has already named and confirmed wrong (e.g. a stale
deadline sitting on-screen next to the firm's own current one) should not
wait its turn in that queue — `--ids` bypasses the staleness cutoff and
`--limit` entirely and checks exactly the named rows, now.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db.models import F, Q
from django.utils import timezone

from coverage_connectors import verify

from directory.models import Opportunity, OpportunityChange, ScrapeRun


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
        parser.add_argument("--ids", type=str, default="",
                            help="Comma-separated Opportunity ids to force-recheck "
                                 "immediately, bypassing the staleness cutoff and "
                                 "--limit entirely — a scoped backfill for rows a "
                                 "specific audit already named, not a general run.")

    def handle(self, *args, **opts):
        now = timezone.now()
        cutoff = now - timedelta(days=opts["max_age_days"])

        # An explicit --ids list is a targeted backfill for rows already named
        # by an audit — it bypasses the staleness cutoff and --limit on
        # purpose, since the whole point is rechecking THESE rows now rather
        # than waiting for them to reach the front of the oldest-NULL queue.
        ids = [int(x) for x in opts["ids"].split(",") if x.strip()]
        if ids:
            # `status="open"` on purpose, same as the default queue below:
            # this command's whole contract (module docstring, every branch
            # below) is a check over OPEN rows, and it has no path that
            # reopens one — only `reopen_confirmed_live_rows` does,
            # deliberately, with its own audit trail (flipping `status`
            # back, clearing `closed_at`). Without this filter, an
            # `--ids` list that happened to name a closed row would still
            # call `verify()` for it, and a "verified-open" result would
            # silently stamp `last_verified`/`deadline` on a row every other
            # surface still reads as closed — a "closed" posting wearing a
            # same-day "verified" timestamp, with no status change to
            # explain it.
            candidates = list(
                Opportunity.objects.filter(id__in=ids, status="open").order_by("id")
            )
        else:
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
                 "unreachable": 0, "needs_verification": 0,
                 # Row-level moves written to OpportunityChange this pass.
                 "changes_recorded": 0}
        # This command is the ONLY place a stored deadline gets corrected
        # from the provider's own fresh answer, and until now it did so
        # silently — the old date was gone the instant the new one was
        # assigned, so a student tracking the role could not be told it had
        # moved. Accumulated unsaved, written in one bulk_create below.
        changes: list[OpportunityChange] = []

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
                    changes.append(OpportunityChange.entry(
                        opp.pk, "deadline", opp.deadline, fresh,
                        stage=OpportunityChange.STAGE_REVERIFY, at=now,
                        note="provider's verify endpoint states a different date",
                    ))
                    opp.deadline = fresh
                    opp.deadline_precision = "day"
                    update_fields += ["deadline", "deadline_precision"]
                opp.save(update_fields=update_fields)
            elif verdict == "closed":
                changes.append(OpportunityChange.entry(
                    opp.pk, "status", opp.status or "open", "closed",
                    stage=OpportunityChange.STAGE_REVERIFY, at=now,
                    note="provider's verify endpoint reports the posting closed",
                ))
                opp.status = "closed"
                opp.closed_at = now
                opp.save(update_fields=update_fields + ["status", "closed_at"])
            else:
                # unreachable / needs-verification: we looked, we can't say —
                # last_verified deliberately does NOT move.
                opp.save(update_fields=update_fields)

        # One write for the whole pass. A --dry-run never reaches this with
        # anything queued: the loop `continue`s before any of it.
        if changes:
            OpportunityChange.objects.bulk_create(changes, batch_size=1000)
        stats["changes_recorded"] = len(changes)

        run.finished = timezone.now()
        run.stats = stats
        run.status = "ok" if not opts["dry_run"] else "dry-run"
        run.save()

        label = "would update" if opts["dry_run"] else "updated"
        self.stdout.write(self.style.SUCCESS(
            f"reverify [{label}]: {stats['checked']} checked — "
            f"{stats['verified_open']} open, {stats['closed']} closed, "
            f"{stats['unreachable']} unreachable, {stats['needs_verification']} undecidable, "
            f"{stats['changes_recorded']} changes recorded"
        ))
