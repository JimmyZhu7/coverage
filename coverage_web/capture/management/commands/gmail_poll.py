"""gmail_poll — Gmail Live without Pub/Sub, without Google Cloud credentials.

THIS IS POLLING, NOT PUSH, AND THE DIFFERENCE IS LATENCY.
---------------------------------------------------------
`gmail_pubsub_listen` is woken by Gmail the instant a mailbox changes: its
latency is a second or two. This command asks, on a timer, "anything since
the cursor I stored last time" — so its latency is anywhere from zero to
one `--interval`. That is the entire trade, and it is the only thing this
command does worse. Everything downstream is identical, because both
commands end in the same call: `gmail_live.sync_connection(connection)`,
which owns the history window, the 404 re-anchor, classification and
`apply_findings`. Nothing about how a message becomes a touch is
reimplemented here. This file is a scheduler and an error policy, nothing
more.

WHAT IT BUYS, AND WHY IT EXISTS AT ALL
---------------------------------------
Pub/Sub is only the doorbell. `sync_connection` builds its Gmail client
from the stored per-user OAuth refresh token and calls
`users().history().list(...)` — there is no Pub/Sub anywhere in that path,
and no Google Cloud credential of any kind beyond the OAuth client already
in `.env`. Pulling FROM Pub/Sub is the one step that needs Application
Default Credentials, and ADC is exactly what `gmail_pubsub_listen` dies on:

    google.auth.exceptions.DefaultCredentialsError

Two real, un-fixable-from-here reasons that happens (both hit on this
project, which is why this command was written):

  1. `gcloud auth application-default login` completes the browser consent
     and then cannot hand the token back — the flow returns it to a
     loopback listener on the local machine, and a captive/filtered campus
     network blocks that hop. `--no-launch-browser` does not help; the
     hand-back is the part that fails, not the browser.
  2. The obvious workaround, a service-account JSON key, is refused by the
     Google Workspace org policy `iam.disableServiceAccountKeyCreation`.
     Only a domain admin can lift it, which is not a thing a student can
     do on a university tenant.

So for a single-user or small deployment, this command removes the Pub/Sub
half of setup entirely: no topic, no pull subscription, no ADC, no
service-account key, no `gcloud` at all. `gmail_live.is_configured()` — the
gate this command holds itself to — does NOT require
`GMAIL_LIVE_PUBSUB_TOPIC`; only real-time push
(`gmail_live.is_push_configured()`, held by `register_watch`,
`renew_watches`, and `gmail_pubsub_listen`) does. A topic is free and
creatable in the Cloud Console in about a minute whenever you do want push
back; until then this command is the entire feature. See
docs/gmail-live-setup.md §5.

RUN IT EITHER WAY
------------------
    python manage.py gmail_poll                    # one pass, exits (cron)
    python manage.py gmail_poll --interval 120     # loop until killed
    python manage.py gmail_poll --dry-run          # report, write nothing
    python manage.py gmail_poll --email you@x.com  # one mailbox

A single pass is the cron-friendly shape and records a `JobRun`
("gmail-poll") the way every other cron command here does. The loop does
NOT record one per pass — at the default interval that is 720 rows a day of
pure noise — which is the same (accepted) monitoring gap
`gmail_pubsub_listen` has as a long-running worker.

THE FOUR OPERATIONAL RULES
---------------------------
1. ONE MAILBOX MUST NOT TAKE DOWN THE OTHERS. Every per-mailbox failure is
   caught, reported, and stepped over. A grant that Google reports as
   `invalid_grant` is not an error at all but a known end-state: the row is
   marked `revoked`, exactly as `register_watch` does on a 401/403, and the
   user sees "needs reconnect" on the Settings page. Anything else is
   logged and retried on the next pass, because the alternative — marking a
   connection dead on a transient token-endpoint 5xx — tells a working user
   to go re-authorise for nothing.

2. NO THUNDERING HERD. Mailboxes are synced sequentially with a gap
   between them (`--spacing`, defaulting to the interval spread across the
   mailboxes and capped at `MAX_SPACING`), so a hundred users do not become
   a hundred simultaneous Gmail calls at the top of every minute. Sequential
   rather than a bounded thread pool on purpose: Django database connections
   are thread-local and this process has no request cycle to recycle them
   (see `close_old_connections` below), so threads here would buy latency we
   do not need and a whole class of connection bug we would.

3. SAFE TO OVERLAP. Two copies running at once do not double-log a touch.
   The load-bearing guard is NOT `history_id` — that only covers the
   SEQUENTIAL case, where the second run starts after the first has already
   advanced the cursor and so legitimately finds nothing. Two runs that
   read the same cursor CONCURRENTLY both list the same messages, and what
   stops the second one writing anything is `apply_findings`' own thread
   ratchet (`thread_stage_rank` in capture/gmail.py) refusing to log a
   stage a thread already reached. That leaves one narrow race — both runs
   reading the ratchet before either writes — which the Postgres advisory
   lock below closes: a pass that cannot take a mailbox's lock skips that
   mailbox instead of racing it. The lock is `pg_try_advisory_lock`, never
   the blocking variant, and never a row lock: a row lock on
   `gmail_connections` would be held for the length of a sync and would
   block the "Scan Now" button's own `UPDATE` in a web request.

4. `--dry-run` WRITES NOTHING. No `history_id` advance, no
   `last_notification_at`, no `revoked` transition, no `JobRun`, no touches.
   It still talks to Google read-only (`gmail_live.preview_sync`), because a
   dry run that skipped the network could not tell a live connection from a
   revoked one.

DATABASE CONNECTIONS ARE RECYCLED BY HAND, per mailbox — for the same
reason `gmail_pubsub_listen` does it, and that command's `_process`
docstring is the long version. Short version: `CONN_MAX_AGE` and
`CONN_HEALTH_CHECKS` are enforced only from Django's
`request_started`/`request_finished` signals, this process has no request
cycle, and a poller sleeping between passes is precisely the workload where
a managed Postgres' idle reaper drops the connection underneath it.
"""

from __future__ import annotations

import signal
import threading
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from google.auth.exceptions import RefreshError

from analytics.models import Import
from capture import gmail_live, locks
from capture.models import GmailConnection
from ops.tracking import track_job_run

# Two minutes. Deliberately not "as fast as the quota allows".
#
# Quota is genuinely not the binding constraint — an empty poll costs one
# `history.list` (2 quota units) against a per-project allowance measured in
# the hundreds of millions per day, so even a thousand mailboxes at this
# cadence is a rounding error. What actually argues for spacing the polls out
# is the OAuth token endpoint: `sync_connection` refreshes an access token on
# every call, and access tokens last an hour — polling every few seconds
# would mean hundreds of refreshes per mailbox per day to re-mint a
# credential that was still valid.
#
# Two minutes is the point where the remaining cost is negligible and the
# latency is still invisible in the only place it is felt: "a recruiter
# replied" showing up on Today. Push would put it there in two seconds
# instead of up to two minutes, and for this feature nobody can tell.
DEFAULT_INTERVAL = 120

# Ceiling on the automatic gap between mailboxes. Without a cap, a long
# `--interval` on a two-mailbox deployment would sit idle for minutes
# between two calls that each take a second — spacing exists to avoid a
# burst, not to stretch a pass out to fill the interval.
MAX_SPACING = 5.0

# The per-mailbox advisory lock now lives in `capture.locks` — shared by
# every writer that can touch a mailbox (this poller, gmail_backfill's two
# selections, the Pub/Sub listener, the import-triggered scan), because a
# lock only one of them takes is a lock the others walk straight past. The
# namespace constant is re-exported here because this command's tests (and
# any operator hand-checking a stuck lock) have always addressed it by this
# name.
ADVISORY_LOCK_NAMESPACE = locks.ADVISORY_LOCK_NAMESPACE


def auto_spacing(interval: float, mailboxes: int) -> float:
    """The default gap between mailboxes in one pass: the interval spread
    across them, capped. Rule 2.

    Spreading rather than a fixed sleep means the herd control scales with
    the deployment on its own — one mailbox never waits, and a hundred
    mailboxes on a 120s interval trickle out rather than arriving as one
    burst at the top of every pass.
    """
    if mailboxes < 2:
        return 0.0
    return min(MAX_SPACING, interval / mailboxes)


class Command(BaseCommand):
    help = (
        "Poll every connected Gmail mailbox and sync it. The no-Pub/Sub, "
        "no-Google-Cloud-credentials alternative to gmail_pubsub_listen — "
        "same sync, higher latency."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval", type=float, default=None, metavar="SECONDS",
            help=(
                f"Loop forever, one pass every SECONDS, until killed "
                f"(Ctrl-C / SIGTERM). Omit for a single pass that exits — "
                f"the cron-friendly shape. Suggested value: {DEFAULT_INTERVAL}."
            ),
        )
        parser.add_argument("--email", help="Poll just this user's mailbox.")
        parser.add_argument(
            "--spacing", type=float, default=None, metavar="SECONDS",
            help=(
                "Seconds to wait between mailboxes within one pass. Default "
                "is the interval spread across the mailboxes in the pass, "
                f"capped at {MAX_SPACING}s. Pass 0 to disable."
            ),
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would sync; write nothing (see the module docstring).",
        )

    # -- entry point -------------------------------------------------------

    def handle(self, *args, **opts):
        if not gmail_live.is_configured():
            self.stdout.write("Gmail Live is not configured — nothing to poll.")
            return

        interval = opts["interval"]
        if interval is not None and interval <= 0:
            raise CommandError("--interval must be greater than zero.")
        spacing = opts["spacing"]
        if spacing is not None and spacing < 0:
            raise CommandError("--spacing cannot be negative.")

        # Created in both modes so `_run_pass` has one thing to check, and
        # so a single pass over many mailboxes is interruptible too.
        stop = threading.Event()

        if interval is None:
            if opts["dry_run"]:
                # No JobRun row: rule 4. A dry run that recorded a
                # successful run of a job it did not perform would be the
                # one lie this command tells.
                self._run_pass(stop, opts, interval=DEFAULT_INTERVAL)
                return
            with track_job_run("gmail-poll"):
                self._run_pass(stop, opts, interval=DEFAULT_INTERVAL)
            return

        self._loop(stop, opts, interval=interval)

    # -- the loop ----------------------------------------------------------

    def _loop(self, stop: threading.Event, opts: dict, *, interval: float) -> None:
        """Run a pass every `interval` seconds until signalled.

        The interval is measured from the START of each pass, not from the
        end, so a pass that takes 20s of a 120s interval is followed by a
        100s wait rather than a 120s one — the cadence stays honest instead
        of drifting later every cycle. A pass that OVERRUNS the interval
        simply starts the next one immediately; there is no queue to fall
        behind, because each pass asks Gmail for "everything since the
        cursor" rather than for a fixed slice of time.
        """
        restore = self._install_signal_handlers(stop)
        self.stdout.write(
            f"polling every {interval:g}s ... (Ctrl-C to stop)"
        )
        try:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    self._run_pass(stop, opts, interval=interval)
                except Exception as exc:  # noqa: BLE001
                    # Per-mailbox failures never reach here (rule 1) — this
                    # catches a failure of the pass ITSELF, i.e. the
                    # database query that selects the mailboxes. Killing a
                    # long-running poller because Postgres blinked once
                    # would stop syncing for everyone until a human noticed
                    # the process was gone; waiting out the interval and
                    # retrying is strictly better.
                    self.stderr.write(f"pass failed, retrying next interval: {exc}")
                if stop.is_set():
                    break
                remaining = interval - (time.monotonic() - started)
                if remaining > 0:
                    # `Event.wait` rather than `time.sleep`: the signal
                    # handler's `set()` wakes it immediately, so Ctrl-C
                    # during the idle gap exits now instead of at the end
                    # of the interval.
                    stop.wait(remaining)
        except KeyboardInterrupt:
            # Belt and braces for the case where the handler could not be
            # installed (see `_install_signal_handlers`).
            pass
        finally:
            restore()
            close_old_connections()
        self.stdout.write("stopped.")

    def _install_signal_handlers(self, stop: threading.Event):
        """Make SIGINT and SIGTERM ask the loop to finish, rather than tear
        the process down mid-sync with a traceback.

        SIGTERM matters as much as SIGINT here: a deployed worker is stopped
        with SIGTERM on every redeploy, and the default action for it is
        immediate death — mid-`apply_findings`, with no message saying the
        poller went away.

        Returns a callable that restores whatever was installed before.
        `signal.signal` only works on the main thread; when it does not
        (a test runner or an embedding harness calling `call_command` off
        the main thread), fall back to the `KeyboardInterrupt` catch in
        `_loop` rather than refusing to run.
        """
        previous: list[tuple[int, object]] = []

        def _request_stop(signum, frame):  # noqa: ARG001
            stop.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                previous.append((signum, signal.signal(signum, _request_stop)))
            except (ValueError, OSError):
                pass

        def _restore() -> None:
            for signum, handler in previous:
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):
                    pass

        return _restore

    # -- one pass ----------------------------------------------------------

    def _select(self, email: str | None):
        """The mailboxes one pass syncs.

        `status="active"` matches `renew_watches` and `gmail_backfill`: a
        `revoked` row is a known end-state waiting on the user to reconnect,
        not a failure to report every two minutes forever.

        `user__plan="pro"` matches `renew_watches` and
        `process_notification`, and it is the more important of the two.
        Real-time sync is the Pro line (docs/pricing-rebalance-plan.md §7);
        `renew_watches` enforces it by simply not renewing a Free user's
        watch and letting Google's 7-day expiry do the rest. A poller that
        ignored the plan would hand every Free and every expired-trial
        account exactly the coverage they stopped paying for, through a
        different door — and it would do it silently, because nothing
        expires a poll.
        """
        query = GmailConnection.all_objects.select_related("user").filter(
            status="active", user__plan="pro"
        )
        if email:
            query = query.filter(user__email=email)
        return list(query.order_by("id"))

    def _run_pass(
        self, stop: threading.Event, opts: dict, *, interval: float
    ) -> tuple[int, int, int]:
        dry_run = opts["dry_run"]
        prefix = "[dry-run] " if dry_run else ""

        close_old_connections()
        connections = self._select(opts["email"])
        if not connections:
            self.stdout.write(f"{prefix}No connected mailboxes to poll.")
            return (0, 0, 0)

        spacing = opts["spacing"]
        if spacing is None:
            spacing = auto_spacing(interval, len(connections))

        synced = skipped = failed = 0
        for index, connection in enumerate(connections):
            if stop.is_set():
                break
            # Between mailboxes, never before the first or after the last:
            # a one-mailbox deployment (the common case) sleeps not at all.
            if index and spacing:
                stop.wait(spacing)
                if stop.is_set():
                    break

            outcome = self._poll_one(connection, dry_run=dry_run)
            if outcome == "synced":
                synced += 1
            elif outcome == "failed":
                failed += 1
            else:
                skipped += 1

        self.stdout.write(
            f"{prefix}pass: {synced} synced, {skipped} skipped, {failed} failed"
        )
        close_old_connections()
        return (synced, skipped, failed)

    def _poll_one(self, connection: GmailConnection, *, dry_run: bool) -> str:
        """Sync one mailbox. Returns "synced" / "skipped" / "failed" and
        NEVER raises — rule 1."""
        prefix = "[dry-run] " if dry_run else ""
        address = connection.gmail_address

        # Per mailbox, not per pass: a pass over many mailboxes is itself
        # long enough for a connection to be reaped underneath it, and this
        # is the last point before the advisory lock is taken — closing
        # AFTER the lock would drop the session that holds it.
        close_old_connections()

        # Rule 4: a dry run takes no lock (it writes nothing, so it cannot
        # race anything) and calls the read-only preview instead of the
        # sync.
        if dry_run:
            try:
                preview = gmail_live.preview_sync(connection)
            except RefreshError as exc:
                self.stdout.write(
                    f"{prefix}{address}: token refresh refused — "
                    f"{self._revocation_note(exc)}"
                )
                return "failed"
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"{prefix}{address}: would fail — {exc}")
                return "failed"

            if preview["reanchor"]:
                self.stdout.write(
                    f"{prefix}{address}: stored history cursor "
                    f"{connection.history_id or '(none)'} is older than Gmail's "
                    "~7-day history window — a real run would re-anchor to now "
                    "and skip the gap (the twice-daily agent sync is the backstop)"
                )
                return "synced"

            count = len(preview["message_ids"])
            self.stdout.write(
                f"{prefix}{address}: {count} new message(s) since history "
                f"{connection.history_id or '(none)'}"
                + (
                    f" — would advance to {preview['latest_history_id']}"
                    if preview["latest_history_id"] else ""
                )
            )
            return "synced"

        # Rule 3. `skip_locked` semantics by hand: another pass already has
        # this mailbox, so leave it to them rather than racing.
        if not self._try_lock(connection.pk):
            self.stdout.write(
                f"{address}: already being synced by another run — skipped"
            )
            return "skipped"

        try:
            result = gmail_live.sync_connection(connection)
        except RefreshError as exc:
            return self._handle_refresh_error(connection, exc)
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(f"{address}: sync failed, will retry next pass: {exc}")
            self._record_error(connection, exc)
            return "failed"
        finally:
            self._unlock(connection.pk)

        # `sync_connection` writes the new cursor onto this same instance,
        # so there is nothing to re-read.
        summary = self._sync_summary(result)
        self.stdout.write(
            f"{address}: synced (history {connection.history_id})"
            + (f" — {summary}" if summary else "")
        )
        # The report's honesty valves, on the one path that runs every two
        # minutes: "application mail we could not type", "2 contacts share
        # this name", the mail-facts surfaced lines. These used to be
        # discarded with the whole SyncResult, so a message the pipeline saw
        # and could not resolve produced no row, no card, and no line.
        for line in self._sync_details(result):
            self.stdout.write(f"  {line}")
        self._record_outcome(connection, result)
        return "synced"

    # -- the per-run ledger ------------------------------------------------
    #
    # WHY THIS EXISTS. `gmail_backfill` and the "Scan Now" rescan each write
    # an `Import` row per run carrying the whole `SyncResult.as_stats()`
    # (kinds "gmail_backfill" / "gmail_rescan"), which is how anyone can ask
    # "what has capture actually done" as a QUERY. This command — the one
    # that runs every two minutes, and on a Pub/Sub-free deployment is the
    # entire live-sync feature — persisted nothing at all. Its whole output
    # was `self.stdout.write` into a worker's log stream, so a mailbox
    # failing every pass for a day was a stderr line nobody reads, and the
    # mail-facts counters this file already prints (`_sync_summary`) died
    # with the process. Two rows fix that without a new model: the ledger
    # this app already keeps, one more `kind`.
    #
    # ONLY WHEN THERE IS SOMETHING TO SAY. A row per pass would be ~720 a day
    # per mailbox of `{"findings": 0}` — the ledger would become the noise it
    # is meant to cut through, and "is it silently failing" would be no
    # easier to answer for having 720 rows saying nothing happened. So: a
    # `gmail_poll` row only when the pass actually found messages, and a
    # `gmail_poll_error` row on every failure (an error is by definition
    # something to say, and the count of them against the count of successful
    # polls is the exception-rate canary /ops/health/capture/ reads). The
    # "poll is alive at all" question is NOT answered here — that is the
    # `JobRun` row `_loop` now writes per pass, which is a different question
    # with a different retention story.
    #
    # NEVER FATAL, on the same reasoning as rule 1: a poll that found real
    # mail, logged real touches, and then could not write its own bookkeeping
    # row must not report itself as a failed sync. The work is already
    # committed; losing the receipt is strictly better than losing the work.

    def _record_outcome(self, connection: GmailConnection, result) -> None:
        """One `Import` row for a pass that found something. No row for an
        empty pass — see the block comment above."""
        try:
            stats = dict(result.as_stats())
        except Exception:  # noqa: BLE001 — no result, or a test double
            return
        if not stats.get("findings"):
            return
        self._write_import("gmail_poll", connection, stats)

    def _record_error(self, connection: GmailConnection, exc: BaseException) -> None:
        """One `Import` row per failed pass. `str(exc)` rather than a
        traceback: this row is read on a health page and in the user's own
        data export, and the useful part is which mailbox failed how often —
        the traceback lives in the worker log where a debugger wants it.
        """
        self._write_import(
            "gmail_poll_error", connection, {"error": str(exc)[:500]}
        )

    def _write_import(self, kind: str, connection: GmailConnection, stats: dict) -> None:
        try:
            Import.all_objects.create(
                user=connection.user,
                kind=kind,
                filename=connection.gmail_address,
                row_stats=stats,
            )
        except Exception as exc:  # noqa: BLE001 — see the block comment above.
            self.stderr.write(f"could not record {kind} row: {exc}")

    @staticmethod
    def _sync_summary(result) -> str:
        """Non-zero counters as one short line, or "". Defensive against
        anything that isn't a real SyncResult (tests patch the sync)."""
        try:
            stats = dict(result.as_stats())
        except Exception:  # noqa: BLE001 — no result, or a test double
            return ""
        parts = [f"{v} {k}" for k, v in stats.items() if isinstance(v, int) and v]
        return ", ".join(parts)

    @staticmethod
    def _sync_details(result) -> list[str]:
        try:
            return [str(line) for line in list(result.details)]
        except Exception:  # noqa: BLE001 — no result, or a test double
            return []

    # -- error policy ------------------------------------------------------

    @staticmethod
    def _looks_revoked(exc: BaseException) -> bool:
        """`invalid_grant` is Google's documented signal that the refresh
        token itself is dead — revoked in the user's Google Account, or the
        OAuth app dropped out of the 100-tester allowlist. Matched on the
        error payload rather than on `RefreshError` as a whole ON PURPOSE:
        google-auth raises the same exception type for a transient 5xx from
        the token endpoint, and marking a working connection `revoked`
        because Google had a bad minute puts a red "needs reconnect" banner
        in front of a user who has nothing to fix.
        """
        return "invalid_grant" in str(exc)

    def _revocation_note(self, exc: BaseException) -> str:
        if self._looks_revoked(exc):
            return "the grant is gone; this connection needs the user to reconnect"
        return f"transient, will retry next pass ({exc})"

    def _handle_refresh_error(self, connection: GmailConnection, exc: RefreshError) -> str:
        address = connection.gmail_address
        if not self._looks_revoked(exc):
            self.stderr.write(f"{address}: token refresh failed, will retry next pass: {exc}")
            return "failed"
        # Same transition, same reasoning as `register_watch`'s 401/403
        # branch — and worth doing here rather than leaving to
        # `gmail_watch_renew`, because on a Pub/Sub-free deployment this
        # command is the thing talking to Gmail most often, so it is the
        # thing that notices first.
        connection.status = "revoked"
        connection.save(update_fields=["status"])
        self.stdout.write(
            f"{address}: grant revoked — marked for reconnect, skipped"
        )
        return "skipped"

    # -- the per-mailbox advisory lock -------------------------------------

    def _try_lock(self, connection_id: int) -> bool:
        """Non-blocking. False means someone else holds it; see rule 3.
        Delegates to `capture.locks` — the shared per-mailbox lock every
        mailbox writer now takes, not just this poller."""
        return locks.try_mailbox_lock(connection_id)

    def _unlock(self, connection_id: int) -> None:
        locks.unlock_mailbox(connection_id)
