"""The Django -> coverage_domain adapter (docs/build-plan.md §2, "the
domain adapter").

Thin wiring only — no business logic lives here. `coverage_domain.pipeline`
is the reviewed, tested, ported artifact (see its module docstring); this
module's entire job is handing it a connection shaped the way it expects
and forwarding arguments.

Connection strategy: a dedicated psycopg (v3) connection, opened fresh
per call, rather than reusing `django.db.connection.connection` (Django's
own wrapped DBAPI connection). Reasoning:

- pipeline.py's module docstring is explicit: the handed-in connection
  "MUST NOT be in autocommit mode" and pipeline.py alone calls its
  `commit()`/`rollback()`. Django's connection toggles autocommit around
  its own transaction management (autocommit-by-default outside
  `transaction.atomic()`, wrapped during it) — reusing it would either
  fight Django's transaction state or depend on the caller happening to be
  inside/outside an `atomic()` block at the right moment. A dedicated
  connection sidesteps that coupling entirely: its transaction lifecycle
  belongs to pipeline.py alone, for exactly the duration of one call.
- pipeline.py also requires dict-like row access by column name (it reads
  `before["warmth"]`, `after["thread_state"]`, etc.). A dedicated
  connection gets its own `psycopg.rows.dict_row` row factory with zero
  risk of interfering with whatever cursor/row shape Django's ORM is using
  concurrently on its own connection.
- Cost: one extra physical connection per adapter call (no pooler yet at
  v1's scale — see docs/build-plan.md "Background work"). Acceptable now;
  worth revisiting if/when pgbouncer or a connection pool is introduced.

Connection parameters are read from `settings.DATABASES["default"]`
(already parsed by django-environ from `DATABASE_URL`) rather than a
second copy of the DSN, so this can never drift from Django's own DB
config.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from django.conf import settings
from psycopg.rows import dict_row

from coverage_domain import pipeline


@contextmanager
def _pipeline_connection() -> Iterator[psycopg.Connection]:
    db = settings.DATABASES["default"]
    conn = psycopg.connect(
        dbname=db["NAME"],
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"] or None,
        port=db["PORT"] or None,
        row_factory=dict_row,
        autocommit=False,  # pipeline.py owns commit()/rollback() itself.
    )
    try:
        yield conn
    finally:
        conn.close()


def log_touch(
    user_id: int,
    contact_id: int,
    kind: str,
    channel: str,
    note: str | None = None,
) -> dict[str, str]:
    """Log one interaction against `contact_id` and let the ported
    ratchet advance warmth/thread_state per `TOUCH_TRANSITIONS`. Returns
    the dict of changed `contacts` columns (empty if nothing moved).

    Thin wrapper over `coverage_domain.pipeline.apply_touch` — see that
    function's docstring for the full behavior contract (ratchet,
    terminal-advocate guard, TOCTOU-safe atomic update).
    """
    with _pipeline_connection() as conn:
        return pipeline.apply_touch(conn, user_id, contact_id, kind, channel, note)


def set_contact_state(
    user_id: int,
    contact_id: int,
    *,
    warmth: str | None = None,
    thread_state: str | None = None,
    note: str | None = None,
) -> dict[str, str]:
    """Manual override: set warmth and/or thread_state directly, bypassing
    the ratchet (and, uniquely, able to move `thread_state` out of the
    terminal `advocate` state). Also inserts an audit `touches` row itself
    — see `coverage_domain.pipeline.set_state`.
    """
    with _pipeline_connection() as conn:
        return pipeline.set_state(
            conn,
            user_id,
            contact_id,
            warmth=warmth,
            thread_state=thread_state,
            note=note,
        )
