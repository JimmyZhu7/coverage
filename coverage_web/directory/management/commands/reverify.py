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
                           an updated one into the posting. `confidence`
                           moves WITH it, to the tier the answering
                           connector actually earns (`_verify_confidence`) --
                           greenhouse/oracle read a structured field and get
                           1.0, everyone else (tal.net, Workday) is reading
                           the posting's own text by regex and gets 0.6, the
                           same split `ingest._apply_opportunity` draws at
                           first ingest. A date that did not move keeps the
                           BETTER of its old confidence and this tier, never
                           the worse.
- "closed"              -> status="closed"
- "unreachable"         -> nothing else — an error is never evidence of
                           life OR death (same rule as ingest's failed-board
                           guard)
- "needs-verification"  -> nothing else; the URL doesn't carry enough to
                           decide deterministically

The honesty contract: `last_verified` moves ONLY on a positive liveness
signal. The card's "Verified N Days Ago" pill therefore never lies.

AND THE SAME CONTRACT FROM THE OTHER SIDE, for a row that is already
`status="closed"`. The routine candidate query never selects one, but `--ids`
is unfiltered by design, so both verdicts have to have an answer:

- "verified-open" on a closed row -> stamp the check fields and say so,
  nothing more. `last_verified` does not move (it would put "verified today"
  on a card the board shows as closed) and `deadline` is not refreshed (it
  would put a live countdown there). Reopening on that evidence is
  `reopen_confirmed_live_rows`' job — it exists for exactly this, and records
  what it did. Two commands reading one provider answer and writing two
  different stories about the same row is how a posting ends up both closed
  and freshly verified.
- "closed" on a closed row -> a re-check, not a move. No `OpportunityChange`
  is written and `closed_at` is left where it is, so re-running the command
  is idempotent instead of logging a `closed -> closed` "change" into the
  tracked-role timeline and walking the actual close date forward every run.

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


# Providers whose `verify()` reads `deadline_dates` off a genuinely
# structured field: `greenhouse.py` reads `application_deadline` straight out
# of the board's own JSON API, `oracle.py` reads `PostingEndDate` the same
# way. Every other connector that ever populates `deadline_dates` gets there
# by regex over the posting's own rendered text -- `talnet.py` matches
# "Event Date"/"Registration Deadline" out of an HTML meta description,
# `workday.py`'s `_deadline_from_description` matches a stated "Application
# Deadline" out of the job description HTML. That is the SAME tier
# `directory.ingest._apply_opportunity` calls 0.6 ("Deadline-from-prose"),
# not the 1.0 tier a structured API field earns -- the fact that the regex
# is reading a clearly-labelled field on the page does not make it any less
# a regex reading OUR code performed, the same distinction the walkthrough
# already drew for HSBC's own "Closing Date:" label at ingest time.
#
# `verified-open` used to leave `confidence` untouched entirely, so a date
# `reverify` corrected inherited whatever confidence the row already held --
# usually 1.0, from an original structured-field ingest, now standing next
# to a value verify()'s own regex just supplied. Confirmed live: of the 31
# open rows whose most recent deadline write came from this command, 29 sat
# at confidence 1.0 while their date came from tal.net's or Workday's own
# text read. This is `ingest._apply_opportunity`'s "a prose reading
# inherited a board-published confidence" bug, on this command's write path
# instead of that one.
_STRUCTURED_VERIFY_PROVIDERS = frozenset({"greenhouse", "oracle"})
#: The tier every other connector's `deadline_dates` earns -- matches
#: `directory.ingest`'s own prose tier so a date means the same thing
#: wherever it was last written.
_TEXT_VERIFY_CONFIDENCE = 0.6


def _verify_confidence(provider: str) -> float:
    """What confidence a date drawn from this provider's `verify()` deserves,
    on the same two-tier scale `ingest._apply_opportunity` uses: 1.0 for a
    genuine structured field, 0.6 for a regex reading of the posting's own
    text. An unrecognised provider defaults to the text tier -- the
    conservative side of the line, and the side every connector that has
    ever shipped `deadline_dates` other than greenhouse/oracle actually
    belongs on."""
    return 1.0 if provider in _STRUCTURED_VERIFY_PROVIDERS else _TEXT_VERIFY_CONFIDENCE


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
            if verdict == "verified-open" and opp.status == "closed":
                # A CLOSED ROW THIS COMMAND MUST NOT SPEAK FOR. The routine
                # candidate query is `status="open"`, so this is only
                # reachable through `--ids`, which is unfiltered by design.
                # Stamping `last_verified` here would break the module
                # docstring's own honesty contract from the other side: the
                # card's "Verified N days ago" pill would read "today" on a
                # posting the board still shows as closed, and refreshing
                # `deadline` would put a live-looking countdown on it too.
                # Reopening is the right correction, but it is
                # `reopen_confirmed_live_rows`' decision to make (it exists
                # for exactly this evidence, and records it), not a side
                # effect of a staleness sweep. So: record that we looked,
                # say so loudly, change nothing else.
                self.stdout.write(self.style.WARNING(
                    f"  #{opp.pk} — verified OPEN but stored status='closed'. "
                    f"Left alone; run: reopen_confirmed_live_rows --ids {opp.pk}"))
                opp.save(update_fields=update_fields)
            elif verdict == "verified-open":
                opp.last_verified = now
                update_fields.append("last_verified")
                fresh = _fresh_deadline(result.deadline_dates) if result is not None else None
                if fresh is not None:
                    tier = _verify_confidence(result.provider)
                    if fresh != opp.deadline:
                        # REPLACED: the claim is replaced with it, same rule
                        # `ingest._apply_opportunity` draws -- this provider's
                        # OWN tier, not whatever confidence the row happened
                        # to be carrying before this pass (see
                        # `_verify_confidence`'s docstring for why that used
                        # to be wrong).
                        changes.append(OpportunityChange.entry(
                            opp.pk, "deadline", opp.deadline, fresh,
                            stage=OpportunityChange.STAGE_REVERIFY, at=now,
                            note="provider's verify endpoint states a different date",
                        ))
                        opp.deadline = fresh
                        opp.deadline_precision = "day"
                        opp.confidence = tier
                    else:
                        # UNCHANGED: this pass merely corroborated the date
                        # already on file. `max()`, not an overwrite -- the
                        # no-downgrade rule `ingest._apply_opportunity` keeps
                        # for the identical case: a 1.0 board-published date
                        # reconfirmed only by a 0.6-tier text read has not
                        # become less certain.
                        opp.confidence = max(opp.confidence or 0.0, tier)
                    update_fields += ["deadline", "deadline_precision", "confidence"]
                opp.save(update_fields=update_fields)
            elif verdict == "closed" and opp.status == "closed":
                # ALREADY CLOSED — a re-check, not a move. Again only
                # reachable via `--ids`. Writing the transition anyway logged
                # a `status: closed -> closed` row in the tracked-role
                # timeline and reset `closed_at` to now, so a student
                # watching that role saw a fresh "closed" event every time an
                # audit re-ran the command, and the date it actually closed
                # walked forward one run at a time. Re-running is now a
                # no-op beyond the check stamps.
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
