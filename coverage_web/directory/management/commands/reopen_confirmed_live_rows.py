"""reopen_confirmed_live_rows — find specific closed rows a gauntlet audit
has already re-verified live against the provider, and reopen them
(report-only by default). The mirror image of close_confirmed_dead_rows.

    python manage.py reopen_confirmed_live_rows            # report only (default)
    python manage.py reopen_confirmed_live_rows --apply     # write the reopens

WHY THIS EXISTS
---------------
`reverify` (the routine staleness sweep) only walks `status="open"` rows —
by design, a posting closed once is assumed to stay closed, so nothing ever
asks the provider again on a row's behalf once it's marked closed. That
assumption breaks whenever a board's LIST endpoint (what ingest's
closed-detection actually watches) disagrees with the SAME posting's own
DETAIL endpoint (what the provider actually serves an applicant) — the list
drops it, ingest closes it, and nothing ever re-opens it even while the
detail page stays live and applyable.

Round 5, 2026-08-14: a random 10-row sample of BMO's phenom-sourced closed
inventory (135 of the DB's 183 phenom+myworkdayjobs.com closed rows are
BMO's) re-verified 2 of 10 still genuinely live through
`coverage_connectors.workday.verify()` — ids 9530 ("Service Transition &
Operational Resiliency Leader") and 9490 ("Vice President, Commodities
Development"), both closed_at 2026-08-14 03:47:13 in the same batch as
several genuinely-dead rows in the same sample. A second, independent
4-row sample drawn from the same BMO/phenom closed pool went 4 for 4:
ids 9539 ("Customer Service Representative"), 9361 ("Associate Banker"),
9526 ("Senior Network and Cloud Penetration Tester"), and 9544 ("Commercial
Relationship Manager Associate") — all four `verified-open` via the same
CxS-422-then-postingAvailable-flag fallback, one of them (9539) additionally
corroborated by a forward-looking `Application Deadline: 08/30/2026` read
straight off the posting page, ruling out a stale/incorrect flag. All six
share the identical mechanism round 4's id=9595 already documented below:
Phenom's search widget desyncing from the live Workday tenant the same
posting resolves into. Round 4's two ids (9595, 3979) have since been
applied and reopened, so they have dropped out of DEFAULT_IDS below.

Round 4, 2026-08-14, two independent mechanisms produced this same
list-vs-detail split:

- Opportunity id=9595 (BMO, "Personal Banking Associate", source="phenom",
  closed_at=2026-08-14 03:47:13) — BMO's ingest source is Phenom's own
  careers.bmo.com search widget, structurally decoupled from the Workday
  tenant the row's stored URL actually resolves into. `coverage_connectors.
  workday.verify()` on the exact stored URL returns `result='verified-open'`,
  and a direct fetch of the posting page itself confirms `postingAvailable:
  true` plus a deadline five days out (08/19/2026) — a live, applyable
  Workday requisition Phenom's search widget had simply dropped. This is not
  a one-row fluke: of the 189 closed, phenom-sourced rows in the DB, 184
  resolve into a Workday tenant the same way (`enrich_postings.
  workday_api_url()` matches), meaning most of BMO's closed inventory has
  never actually been checked against the ATS a student would apply
  through — only against Phenom's own widget. This command's DEFAULT_IDS
  intentionally lists only the ids THIS round's audit confirmed live, same
  scoping convention as close_confirmed_dead_rows; the other ~183 are a
  known backlog for a future audit round or a dedicated sweep, not silently
  swept up here.

- Opportunity id=3979 (Millennium, "Data Operations Analyst - Systematic
  Trading", source="eightfold", closed_at=2026-08-12T17:30:36Z) — the
  posting's own detail page returns 200 with a live JobPosting JSON-LD
  block (validThrough 2026-08-22) while the job id is genuinely absent from
  every page of career.mlp.com's own search API, correctly re-walked at its
  true 10-per-page batch size. This is an eightfold-side inconsistency
  between Millennium's search index and its own detail-page cache — not a
  paging or truncation bug in `coverage_connectors/eightfold.py`, whose
  `fetch()` already advances by actual batch size — so there is no matching
  code fix to make here, only the same live-truth correction reverify would
  make if it ever looked at closed rows.

Both are the identical shape reopen_truncated_oracle_closures.py fixed for
Oracle's truncation-caused closes: a systemic mechanism can keep producing
new instances of the same wrong close, but each existing wrong close is a
plain re-verify-and-correct against the provider's OWN evidence, so this
command reuses that shape generically (any source, explicit ids) rather
than hard-coding to one connector.

Live network, read-only against the provider unless --apply is given; the
live database itself is never written to without it, per this repo's
read-only-DB-by-default rule.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from coverage_connectors import verify

from directory.models import Opportunity

# Confirmed live by the current round's read-only audit — see module
# docstring for why each one closed wrongly. Round 4's ids (9595, 3979) were
# applied and reopened, so they have dropped out of this list.
DEFAULT_IDS = [9530, 9490, 9539, 9361, 9526, 9544]


class Command(BaseCommand):
    help = ("Re-verify specific closed rows against the live provider and report "
            "(or, with --apply, reopen) the ones confirmed still live.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--ids", type=int, nargs="+", default=None,
            help="Opportunity ids to re-check. Defaults to this round's confirmed list.",
        )
        parser.add_argument(
            "--apply", action="store_true", default=False,
            help="Reopen the confirmed-live rows. Default is report-only.",
        )

    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        tag = "" if apply_ else "[dry-run] "
        ids = opts["ids"] or DEFAULT_IDS

        rows = list(
            Opportunity.objects.filter(id__in=ids, status="closed").select_related("firm")
        )
        missing = set(ids) - {o.id for o in rows}
        for m in missing:
            self.stdout.write(f"  #{m} — not found, or not currently status='closed'; skipped")
        if not rows:
            self.stdout.write("No matching closed rows to check.")
            return

        now = timezone.now()
        reopened = checked = 0
        for o in rows:
            checked += 1
            try:
                result = verify(o.url)
            except Exception as exc:  # noqa: BLE001 — one bad URL must not kill the run
                self.stdout.write(f"  {o.firm.name} #{o.id} — unreachable: {exc}")
                continue
            if result.result != "verified-open":
                self.stdout.write(
                    f"  {o.firm.name} #{o.id} — still {result.result}, left alone: {result.evidence}")
                continue
            reopened += 1
            self.stdout.write(
                f"{tag}{o.firm.name} #{o.id} — {o.title[:60]}: "
                f"closed_at={o.closed_at} but {result.evidence}")
            if apply_:
                o.status = "open"
                o.closed_at = None
                o.last_verified = now
                o.last_checked = now
                o.save(update_fields=["status", "closed_at", "last_verified", "last_checked"])

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{checked} checked, {reopened} confirmed still live "
            f"{'and reopened' if apply_ else '(would be reopened)'}."))
