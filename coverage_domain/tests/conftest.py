"""Shared fixtures for coverage_domain's test suite.

Two backends can back the `conn` fixture used by test_pipeline.py:

- "sqlite": a real sqlite3 in-memory connection wrapped by a thin cursor
  shim that rewrites psycopg's pyformat placeholders (bare %s and named
  %(name)s) to sqlite3's (? and :name) before delegating to a real SQL
  engine. This means the CASE-expression ratchet logic in pipeline.py is
  genuinely parsed and executed by a real SQL engine, not re-implemented
  in Python for the test — but a single in-process SQLite connection does
  NOT exercise Postgres's actual MVCC/row-locking behavior, and this suite
  never runs two real concurrent transactions against it. Always runs; no
  external services required.

- "postgres": a real psycopg (v3) connection against a live Postgres
  instance. Skips cleanly (pytest.skip) at fixture setup if psycopg isn't
  importable, or if no reachable Postgres is configured via
  COVERAGE_DOMAIN_TEST_DATABASE_URL (default "postgresql:///postgres").
  Running the same functional suite against a real engine is a genuine
  (if narrower) check that the %s/%(name)s SQL text is valid and behaves
  identically outside the shim — but it's still one connection, one
  thread, no real interleaving. The one test that deliberately forces two
  real transactions to race lives in test_pipeline_postgres.py, gated on
  the same availability check, and is the only place this suite attempts
  to exercise the atomic update's actual concurrency-safety property.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

import pytest

_PARAM_RE = re.compile(r"%\((\w+)\)s|%s")

_SCHEMA_SQLITE = """
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    warmth TEXT NOT NULL DEFAULT 'cold',
    thread_state TEXT NOT NULL DEFAULT 'no_reply'
);
CREATE TABLE touches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    ts TEXT NOT NULL,
    channel TEXT,
    kind TEXT,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'manual'
);
"""

_SCHEMA_POSTGRES = """
CREATE TABLE contacts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    warmth TEXT NOT NULL DEFAULT 'cold',
    thread_state TEXT NOT NULL DEFAULT 'no_reply'
);
CREATE TABLE touches (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    ts TIMESTAMPTZ NOT NULL,
    channel TEXT,
    kind TEXT,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'manual'
);
"""


def _to_sqlite_params(sql: str, params):
    """Rewrite psycopg pyformat placeholders to sqlite3's, and adapt
    datetime values to ISO-8601 strings (sqlite3's built-in datetime
    adapter is deprecated as of 3.12 and shouldn't be relied on) — this
    conversion is entirely the test shim's business; production code in
    pipeline.py never has to know SQLite exists."""
    if isinstance(params, dict):
        new_sql = _PARAM_RE.sub(lambda m: f":{m.group(1)}", sql)
        new_params = {
            k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in params.items()
        }
        return new_sql, new_params
    new_sql = _PARAM_RE.sub("?", sql)
    new_params = tuple(v.isoformat() if isinstance(v, datetime) else v for v in (params or ()))
    return new_sql, new_params


class _ShimCursor:
    """Wraps a real sqlite3.Cursor; translates paramstyle only."""

    def __init__(self, real_cursor):
        self._cur = real_cursor

    def execute(self, sql, params=None):
        sql2, params2 = _to_sqlite_params(sql, params)
        return self._cur.execute(sql2, params2)

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class SqliteShimConnection:
    """Wraps a real sqlite3.Connection so pipeline.py's psycopg-flavored
    (%s / %(name)s) SQL runs against a genuine SQL engine without a
    Postgres server. `with conn:` and `.cursor()` behave the same way a
    psycopg2/psycopg3 connection's do."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def cursor(self):
        return _ShimCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)

    def close(self):
        self._conn.close()


@pytest.fixture
def sqlite_conn():
    conn = SqliteShimConnection()
    conn._conn.executescript(_SCHEMA_SQLITE)
    yield conn
    conn.close()


def make_postgres_conn(dsn: str):
    """Returns (connection, None) on success, or (None, reason) — never
    raises, so callers can turn any failure into a clean pytest.skip."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        return None, "psycopg is not installed — skipping real-Postgres test"
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=2)
    except Exception as e:  # noqa: BLE001 - any connection failure means "skip"
        return None, f"could not connect to Postgres ({dsn}): {e}"
    return conn, None


def reset_postgres_schema(conn) -> None:
    # Explicit commit — NOT `with conn:`. Under psycopg v3 a `with conn:`
    # block closes the connection on exit, which would hand a dead
    # connection to the test. See pipeline.py's module docstring.
    try:
        conn.execute("DROP TABLE IF EXISTS touches")
        conn.execute("DROP TABLE IF EXISTS contacts")
        conn.execute(_SCHEMA_POSTGRES)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


@pytest.fixture(params=["sqlite", "postgres"])
def conn(request, sqlite_conn):
    """The main parametrized backend fixture used by test_pipeline.py —
    every test using it runs once against the SQLite shim (always) and
    once against real Postgres (skipped when unavailable, e.g. in this
    sandbox; runs for real under the project's CI Postgres service)."""
    if request.param == "sqlite":
        yield sqlite_conn
        return

    import os

    dsn = os.environ.get("COVERAGE_DOMAIN_TEST_DATABASE_URL", "postgresql:///postgres")
    pg, skip_reason = make_postgres_conn(dsn)
    if pg is None:
        pytest.skip(skip_reason)
    try:
        reset_postgres_schema(pg)
        yield pg
    finally:
        pg.close()


@pytest.fixture
def make_contact(conn):
    """Insert a bare contact row (user_id, warmth, thread_state) and return
    its id. Uses `%s` placeholders and `RETURNING id` — supported by both
    backends (SQLite 3.35+ here is 3.53; Postgres always), so this fixture
    needs no per-backend branch."""

    def _make(user_id: int, warmth: str = "cold", thread_state: str = "no_reply") -> int:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO contacts (user_id, warmth, thread_state) "
                "VALUES (%s, %s, %s) RETURNING id",
                (user_id, warmth, thread_state),
            )
            row = cur.fetchone()
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        return row["id"]

    return _make
