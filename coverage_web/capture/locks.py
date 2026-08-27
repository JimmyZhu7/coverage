"""The per-mailbox advisory lock — one writer per mailbox at a time.

WHY THIS IS A MODULE AND NOT THREE PRIVATE COPIES. The lock started life
inside `gmail_poll` as a poll-vs-poll guard, which left every OTHER writer
free to interleave with a live poll pass: `gmail_backfill` (both its
first-connect and "Scan Now" selections), the import-triggered
`backfill_new_contacts`, and the Pub/Sub listener all call the same
`apply_findings` machinery against the same mailbox. The warmth ratchet's
TOCTOU-safe SQL bounds the damage of a race to one duplicated same-stage
touch — narrow, but with a 2-minute poll loop running permanently against
the founder's real mailbox, "eventually" is a schedule, not a hypothesis.
One lock, taken by every writer, ends the class instead of the instance.

THE SHAPE, unchanged from the poll command that pioneered it:

  - `pg_try_advisory_lock`, never the blocking variant, and never a row
    lock: a row lock on `gmail_connections` would be held for the length
    of a sync and would block the Settings page's own UPDATEs.
  - Session-scoped, so it is released by `unlock` OR by the database
    connection going away — including the process being killed mid-sync,
    which is the case a lock table or a `locked_at` column would leave
    stuck forever with no way to tell a live run from a dead one.
  - Keyed (namespace, connection_id). The namespace is an arbitrary but
    fixed int4 — ASCII "gmlp" — so these locks can never collide with
    another feature's.
  - A caller that cannot take the lock SKIPS the mailbox and leaves it to
    whoever holds it: the poll loop retries next interval, the backfill
    cron retries next tick (its status stays `pending`), and the
    best-effort import scan simply doesn't run. Nobody waits, nobody
    races.

Non-Postgres (the SQLite shims some pure tests use) degrades to "always
acquired" — the production database is Postgres, and a lock that only
exists there guards the only place two processes can actually meet.
"""

from __future__ import annotations

from contextlib import contextmanager

from django.db import connection as db_connection

# 0x676D6C70 is ASCII "gmlp" — kept byte-identical to the constant
# `gmail_poll` shipped with, so a redeploy never changes the lock space
# under a still-running poller.
ADVISORY_LOCK_NAMESPACE = 0x676D6C70


def try_mailbox_lock(connection_id: int) -> bool:
    """Non-blocking. False means another writer holds this mailbox."""
    if db_connection.vendor != "postgresql":
        return True
    with db_connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            [ADVISORY_LOCK_NAMESPACE, connection_id],
        )
        return bool(cursor.fetchone()[0])


def unlock_mailbox(connection_id: int) -> None:
    if db_connection.vendor != "postgresql":
        return
    with db_connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_unlock(%s, %s)",
            [ADVISORY_LOCK_NAMESPACE, connection_id],
        )


@contextmanager
def mailbox_lock(connection_id: int):
    """`with mailbox_lock(pk) as acquired:` — yields whether the lock was
    taken, and releases it on exit only if it was. The caller decides what
    "not acquired" means for its job (skip, retry next tick); this context
    only guarantees a taken lock is never leaked on an exception."""
    acquired = try_mailbox_lock(connection_id)
    try:
        yield acquired
    finally:
        if acquired:
            unlock_mailbox(connection_id)
