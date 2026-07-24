"""Genuine concurrent-transaction test for apply_touch's atomic ratchet.

Every test in test_pipeline.py runs against a real SQL engine (SQLite,
always; Postgres too, when reachable) but never opens two transactions at
once — so none of them can actually prove the TOCTOU-safety property the
task brief calls out, only the ratchet logic itself. This file is the one
place in the suite that deliberately forces two real transactions to
overlap on the same row, so the only thing standing between "correct" and
"silently wrong" is Postgres's own row-level locking plus apply_touch's
SQL, not test-harness luck.

Design: connection B opens a transaction and updates the row to a
HIGHER-ranked warmth directly (simulating "the other writer already
landed"), then holds that transaction open for a short, explicit
`pg_sleep()` before committing -- deterministically, not by hoping the OS
schedules a real race. Connection A's apply_touch("reply_received") call
-- for a LOWER rank -- runs on a background thread and is given time to
reach its own UPDATE and block on B's row lock before B commits. If
apply_touch's ratchet were a read-then-write-in-Python (the exact
anti-pattern the atomic UPDATE ... CASE exists to prevent), A would have
read 'cold' before B's write ever happened, decided in Python to upgrade to
'replied', and overwritten B's 'chatted' the instant its blocked UPDATE
finally ran. Because the ratchet comparison lives entirely inside the SQL
CASE, A's UPDATE instead blocks until B's commit, re-reads the
now-'chatted' row, and correctly refuses to downgrade it.

Skips cleanly (module-level pytest.importorskip, plus a live-connection
check in the fixture) if psycopg isn't installed or no Postgres is
reachable -- expected in most local dev environments, including the one
this port was written in (no Postgres server or psycopg install
available). This file's correctness was NOT observed by running it: it is
included as a designed, self-contained test that will execute for real
under the project's CI Postgres service (docs/build-plan.md §9), not as a
claim that it has been seen to pass. Treat it as a draft to validate the
first time it actually runs against Postgres.
"""

import os
import threading
import time

import pytest

psycopg = pytest.importorskip(
    "psycopg", reason="psycopg not installed -- skipping real-Postgres concurrency test"
)
from psycopg.rows import dict_row  # noqa: E402

from coverage_domain import pipeline  # noqa: E402

DSN = os.environ.get("COVERAGE_DOMAIN_TEST_DATABASE_URL", "postgresql:///postgres")


def _connect():
    return psycopg.connect(DSN, row_factory=dict_row, connect_timeout=2)


@pytest.fixture
def two_connections():
    try:
        conn_a = _connect()
        conn_b = _connect()
    except Exception as e:  # noqa: BLE001 - any connection failure means "skip"
        pytest.skip(f"Postgres not reachable at {DSN}: {e}")
        return
    # Explicit commit, not `with conn_a:` — psycopg v3 closes the connection
    # on `with` exit, which would kill conn_a before the test uses it.
    conn_a.execute("DROP TABLE IF EXISTS touches")
    conn_a.execute("DROP TABLE IF EXISTS contacts")
    conn_a.execute(
        "CREATE TABLE contacts (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL, "
        "warmth TEXT NOT NULL DEFAULT 'cold', "
        "thread_state TEXT NOT NULL DEFAULT 'no_reply')"
    )
    conn_a.execute(
        "CREATE TABLE touches (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL, "
        "contact_id INTEGER NOT NULL REFERENCES contacts(id), "
        "ts TIMESTAMPTZ NOT NULL, channel TEXT, kind TEXT, note TEXT, "
        "source TEXT NOT NULL DEFAULT 'manual')"
    )
    conn_a.commit()
    yield conn_a, conn_b
    conn_a.close()
    conn_b.close()


def test_two_concurrent_writers_do_not_undo_each_others_ratchet_advance(two_connections):
    conn_a, conn_b = two_connections
    cur = conn_a.execute(
        "INSERT INTO contacts (user_id, warmth, thread_state) "
        "VALUES (1, 'cold', 'no_reply') RETURNING id"
    )
    contact_id = cur.fetchone()["id"]
    conn_a.commit()

    result = {}

    def writer_a_reply_received():
        # By the time this runs, B (below) already holds the row lock.
        # apply_touch's own UPDATE blocks here until B commits.
        result["updates"] = pipeline.apply_touch(
            conn_a, 1, contact_id, "reply_received", "email", "writer A"
        )

    # Writer B: the higher-ranked event lands first and holds its
    # transaction open (via pg_sleep, deterministically -- not a guess).
    conn_b.execute(
        "UPDATE contacts SET warmth = 'chatted', thread_state = 'chat_done' WHERE id = %s",
        (contact_id,),
    )  # row lock taken now; transaction still open (psycopg default: no autocommit)

    t = threading.Thread(target=writer_a_reply_received)
    t.start()
    time.sleep(0.3)  # give A's thread time to reach its UPDATE and block on B's lock
    conn_b.execute("SELECT pg_sleep(0.2)")  # hold the lock a bit longer, deterministically
    conn_b.commit()  # release the lock; A's blocked UPDATE can now proceed
    t.join(timeout=10)

    assert not t.is_alive(), "writer A's apply_touch call never returned -- deadlock?"
    # The safety property is "A must not undo B's advance." A genuine
    # downgrade surfaces as warmth == 'replied' (A's lower-ranked value
    # winning). The atomic CASE instead re-reads B's committed 'chatted' and
    # keeps it, so the one value that must never appear is a downgrade to
    # 'replied'. NOTE: the returned dict is a before/after snapshot diff, and
    # A snapshotted 'cold' before B committed -- so it legitimately reports
    # warmth='chatted' here (B's advance surfacing, not a downgrade by A).
    # That is why the assertion checks "!= 'replied'" rather than "absent":
    # a read-then-write-in-Python bug would report 'replied' and fail here;
    # the correct atomic implementation reports 'chatted' and passes. The
    # authoritative check remains the final persisted row state below.
    assert result["updates"].get("warmth") != "replied", (
        "apply_touch downgraded warmth to 'replied' -- the ratchet used a "
        f"stale pre-block value instead of re-reading live state; got {result['updates']!r}"
    )

    conn_a.rollback()  # end any open txn so this read sees B's committed write
    row = conn_a.execute(
        "SELECT warmth FROM contacts WHERE id = %s", (contact_id,)
    ).fetchone()
    conn_a.commit()
    assert row["warmth"] == "chatted", (
        f"concurrent writers produced warmth={row['warmth']!r}; the ratchet must "
        f"resolve to the higher rank regardless of which transaction commits last"
    )
