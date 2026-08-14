"""close_confirmed_dead_rows — close specific open rows a gauntlet audit has
already re-verified live as gone (report-only by default).

    python manage.py close_confirmed_dead_rows            # report only (default)
    python manage.py close_confirmed_dead_rows --apply     # write the closes

WHY THIS EXISTS
---------------
The routine staleness sweep (`reverify`, default `--max-age-days 7`) only
picks up a row once its `last_checked` is a week old, oldest-first, capped at
--limit per run. A row re-verified as dead by a one-off audit in between
those windows sits open and visible on the board until the routine sweep
happens to reach it — sometimes days later. This command is the targeted,
same-day catch-up for rows a specific audit has ALREADY confirmed live
against the provider, rather than a fresh full-table scan (which would be
both slow and impolite to re-run against every provider for one row).

Round 2, 2026-08-14: Opportunity id=8885 ("Marshall Wace",
"Recruitment Assistant", stored status='open',
url=https://job-boards.greenhouse.io/marshallwace/jobs/8501520002) —
`coverage_connectors.greenhouse.verify()` on that exact URL returned
`result='closed', evidence='boards-api 404 for job 8501520002 — no longer
listed'`. A direct `curl` against `boards-api.greenhouse.io` (bypassing the
connector) reproduced the same 404, and the firm's own board listing
(`.../v1/boards/marshallwace/jobs`) returned `{"jobs":[], "meta":{"total":0}}`
— a clean empty list, not an error, ruling out a board-wide outage. Applied
and now closed in the live DB, so it has dropped out of DEFAULT_IDS below.

Round 3, 2026-08-14: Opportunity id=10630 ("Raymond James", "Senior
Registered Client Service Associate (Boca Raton, FL)", stored status='open',
last_checked=last_verified=2026-08-13, url=https://raymondjames.wd1.
myworkdayjobs.com/raymondjamescareers/job/Boca-Raton-Florida---United-States/
Senior-Registered-Client-Service-Associate--Boca-Raton--FL-_R-0011569) — the
row's own `last_checked` timestamp shows it was touched by that day's routine
`scrape` run, which stamps `last_verified` off nothing stronger than "the
board's bulk list endpoint still returned this URL" (see `directory.ingest.
_apply_opportunity`). Workday's list/search index can lag its own detail
endpoint — confirmed here: `coverage_connectors.workday.verify()`, which
checks the posting's OWN job-detail endpoint (falling back to the posting
page's `postingAvailable` flag on a blocked CxS call, per this repo's earlier
TD-Securities fix), returns `result='closed', evidence="CxS job-detail HTTP
403; posting page's own postingAvailable flag reads false"` — a stronger,
per-posting signal the list-based scrape never checks. `reverify`'s routine
7-day sweep would eventually catch this on its own, but not for days; this
command is the same-day catch-up an audit-confirmed finding deserves,
same as round 2's.

Round 7, 2026-08-14: Opportunities id=9376 ("BMO", "Director, Intraday
Liquidity Risk Oversight"), id=9389 ("BMO", "Senior Relationship Manager,
Multinational Companies"), id=9522 ("BMO", "Sr. Treasury Sales Consultant")
— all three stored status='open', deadline=2026-08-13 (a passed date),
last_checked=last_verified=2026-08-14T03:47:13Z (today's routine `scrape`
run, same list-endpoint-lag mechanism as round 3's Raymond James row above).
`coverage_connectors.workday.verify()` on all three returned `result=
'closed', evidence="CxS job-detail HTTP 422; posting page's own
postingAvailable flag reads false"` — the same per-posting signal, just a
422 instead of round 3's 403 on the blocked structured call, falling back
to the same `postingAvailable` page flag. Same-day catch-up, same as
round 3's.

The DEFAULT_IDS list below is exactly the rows the current audit round
confirmed and not yet applied; --ids overrides it for any future one-off
audit finding, so this command doesn't need to be re-written each time. Live
network, read-only against the provider unless --apply is given; the live
database itself is never written to without it, per this repo's
read-only-DB-by-default rule.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from coverage_connectors import verify

from directory.models import Opportunity

# Confirmed dead by the current round's read-only audit — see module
# docstring. Round 2's id=8885 was applied and dropped once closed.
DEFAULT_IDS = [10630, 9376, 9389, 9522]


class Command(BaseCommand):
    help = ("Re-verify specific open rows against the live provider and report "
            "(or, with --apply, close) the ones confirmed gone.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--ids", type=int, nargs="+", default=None,
            help="Opportunity ids to re-check. Defaults to this round's confirmed list.",
        )
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Close the confirmed-dead rows. Default is report-only.",
        )

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        tag = "" if apply_ else "[dry-run] "
        ids = opts["ids"] or DEFAULT_IDS

        rows = list(
            Opportunity.objects.filter(id__in=ids, status="open").select_related("firm")
        )
        missing = set(ids) - {o.id for o in rows}
        for m in missing:
            self.stdout.write(f"  #{m} — not found, or not currently status='open'; skipped")
        if not rows:
            self.stdout.write("No matching open rows to check.")
            return

        now = timezone.now()
        closed = checked = 0
        for o in rows:
            checked += 1
            try:
                result = verify(o.url)
            except Exception as exc:  # noqa: BLE001 — one bad URL must not kill the run
                self.stdout.write(f"  {o.firm.name} #{o.id} — unreachable: {exc}")
                continue
            if result.result != "closed":
                self.stdout.write(
                    f"  {o.firm.name} #{o.id} — still {result.result}, left alone: {result.evidence}")
                continue
            closed += 1
            self.stdout.write(
                f"{tag}{o.firm.name} #{o.id} — {o.title[:60]}: {result.evidence}")
            if apply_:
                o.status = "closed"
                o.closed_at = now
                o.last_checked = now
                o.save(update_fields=["status", "closed_at", "last_checked"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{checked} checked, {closed} confirmed gone "
            f"{'and closed' if apply_ else '(would be closed)'}."))
