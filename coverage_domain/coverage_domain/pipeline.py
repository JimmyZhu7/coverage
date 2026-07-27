"""Warmth / thread-state machine — ported from the `campaign` project's
`pipeline.py` (single source of truth for the touch-log write + warmth/
thread-state advance logic there; both its CLI and its web dashboard called
`apply_touch` so the warmth ladder could never drift between front ends).

This module is pure Python: it knows nothing about Django, HTTP, or any
particular Postgres driver. Every function takes a DB-API 2.0 connection
(anything exposing `.cursor()`, `.commit()`, and `.rollback()` — psycopg
(v3), psycopg2, and sqlite3 connections all qualify) and returns plain
dicts.

Transaction handling: each function wraps its statements in an explicit
`try: ... conn.commit() except: conn.rollback(); raise`. It deliberately
does NOT use `with conn:` as a transaction boundary — that is not a PEP 249
primitive and the drivers disagree on it: psycopg2 and sqlite3 treat
`with conn:` as commit/rollback-only (connection stays open), but psycopg
v3 ALSO CLOSES the connection on block exit, which would leave the handed-in
connection dead after a single call. Explicit commit/rollback is the only
idiom every DB-API 2.0 driver implements identically. The connection MUST
NOT be in autocommit mode, or the multi-statement ratchet below loses its
atomicity.

Callers own two responsibilities this module deliberately does not:

  - Configuring that connection so `cursor.fetchone()` / `.fetchall()`
    return rows addressable by column name (e.g. psycopg's
    `psycopg.rows.dict_row` row factory, psycopg2's
    `psycopg2.extras.RealDictCursor`, or sqlite3's `Row` row_factory) —
    every row access below is by column name, matching the original's
    reliance on `sqlite3.Row`.
  - Schema/migrations. This package has no CREATE TABLE of its own (unlike
    the original `campaign.db`, which self-applied its schema on connect)
    — schema ownership moved to Django migrations. See
    coverage_domain/tests/conftest.py for the minimal test-only DDL this
    module's own tests run against.

Tenancy: every function here takes an explicit `user_id`, and every query
is scoped by it (WHERE ... AND user_id = %s, or the equivalent parameter in
a named CASE). A contact_id that exists but belongs to a different user_id
is indistinguishable from one that doesn't exist at all — there is no
unscoped path.

What ported verbatim (behavior unchanged from the SQLite original, only the
storage adapter and the tenancy parameter changed):
  - TOUCH_TRANSITIONS, WARMTH_RANK, CHANNELS.
  - apply_touch()'s single-statement atomic `UPDATE ... CASE` — TOCTOU-safe
    because the ratchet comparison is embedded entirely in SQL, evaluated
    against the row's live state at UPDATE-time. Neither this module nor
    its caller ever reads a warmth/thread_state value in Python and feeds
    it back into the ratchet decision — apply_touch is given only a
    contact_id, never a cached snapshot of the row, so there is nothing
    for a second, near-simultaneous writer to race against except the row
    itself. Do not refactor this into a read-a-value-then-decide-in-Python
    shape; that reintroduces exactly the gap this design closes.
  - `advocate` is a terminal thread_state: only `set_state()` (the manual
    override path, see below) can move a contact out of it. A touch whose
    TOUCH_TRANSITIONS entry would otherwise advance thread_state is a
    no-op on thread_state while it's already 'advocate'.
  - Warmth only ever moves up WARMTH_RANK's ladder. thread_state itself is
    NOT rank-guarded the same way — only the advocate terminal case is
    protected. A kind whose TOUCH_TRANSITIONS thread_state value is
    "behind" the contact's current thread_state (e.g. a 'chat_scheduled'
    touch landing after a 'chat' touch already advanced thread_state to
    'chat_done') WILL move thread_state backward at this layer, exactly as
    it did in the original. In the shipped `campaign` system this couldn't
    surface in production because the caller
    (`netdash/gmail_enrich.py`'s `_thread_stage_rank`) ratchets by Gmail
    thread before ever calling apply_touch, refusing to call it at all for
    a same-or-lower-ranked finding on that thread. That caller-side ratchet
    is a separate port (build-plan.md §4's "netdash/gmail_enrich.py apply
    layer" row) and is intentionally NOT part of this package — see this
    module's test suite (test_thread_state_is_not_rank_guarded_outside_advocate)
    for the regression test that pins this exact boundary rather than
    silently reproducing or silently "fixing" it here.

New in this port (the one intentional addition, per build-plan.md §2 and
the task brief): `set_state()`, the manual-override path ported from the
CLI's `campaign contact set` warmth/thread_state behavior, now ALSO inserts
an audit `touches` row (kind=MANUAL_OVERRIDE_KIND). The original
`contact_set` mutated `contacts` directly with no touches insert at all —
without this addition the touches log would have a silent gap at the one
place state can change outside apply_touch's ratchet.

A second deliberate additive change (audit batch, safe-fixes pass):
`apply_touch()` gained a `source` keyword (default "manual", so every
existing caller is byte-for-byte unaffected) and now writes it into the
`touches.source` column instead of hardcoding the literal `'manual'` in the
INSERT. Before this, `Touch.SOURCE_CHOICES`'s `"capture"` value could never
be written by anything, which made "captured vs typed touches" — the
product's own risk metric — unanswerable from the DB. `apply_touch` also
returns its result as a `TouchResult` (a `dict` subclass carrying one extra
attribute, `touch_id`) instead of a plain `dict`. This is deliberately NOT a
new key inside the dict: every existing caller and test that compares the
result with `== {...}` or checks `bool(...)` sees exactly the same mapping
as before (dict equality and truthiness only ever look at the mapping
contents), while the one caller that needs the id of the row just inserted
(`capture.services._apply_touch_for_event`, to populate `Touch.capture_event`
after the fact) reads it off `.touch_id`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Duck-typed: anything exposing .cursor() and the `with conn:` protocol
# (psycopg v3, psycopg2, sqlite3 connections, or a test double standing in
# for one of those).
DBConnection = Any

TOUCH_TRANSITIONS: dict[str, tuple[str | None, str | None]] = {
    # touch kind -> (new warmth if "higher", new thread_state)
    "outreach": (None, None),
    "follow_up": (None, None),
    "reply_received": ("replied", "replied"),
    # "chat" means the conversation ALREADY HAPPENED — it fires the
    # thank-you cadence (due_actions checks thread_state=='chat_done' and
    # computes hours since the touch's own timestamp as "hours since the
    # chat"). A reply that only confirms/schedules a FUTURE chat must use
    # "chat_scheduled" instead, or due_actions will wrongly report a
    # not-yet-happened chat as overdue for a thank-you.
    "chat": ("chatted", "chat_done"),
    "chat_scheduled": ("replied", "chat_scheduled"),
    "thank_you": (None, None),
    "maintain": (None, None),
    "reping": (None, None),
}
WARMTH_RANK: dict[str, int] = {"cold": 0, "replied": 1, "chatted": 2, "advocate": 3}
CHANNELS: tuple[str, ...] = ("email", "linkedin", "coffee_chat", "call", "event", "other")

# Not present in the original pipeline.py (they lived in campaign/db.py as
# WARMTH / THREAD_STATES) but needed here for set_state()'s validation —
# ported verbatim from db.py:65-66, not reconstructed.
WARMTH: tuple[str, ...] = ("cold", "replied", "chatted", "advocate")
THREAD_STATES: tuple[str, ...] = (
    "no_reply", "replied", "chat_scheduled", "chat_done", "advocate", "quiet", "parked",
)

# Kind recorded for set_state()'s audit touch. Deliberately kept OUT of
# TOUCH_TRANSITIONS (ported verbatim above): it never itself drives a
# ratchet move through apply_touch's machinery — by the time this row is
# written, set_state() has already applied the state change directly. The
# row exists purely so the touches log has no gap at this path.
MANUAL_OVERRIDE_KIND = "manual_override"


class TouchResult(dict):
    """What `apply_touch` returns: the same `{"warmth": ..., "thread_state":
    ...}` (or empty) mapping it always has, plus one additive attribute —
    `touch_id`, the id of the `touches` row this call just inserted.

    Subclassing `dict` rather than returning a tuple or a dataclass is the
    whole point: `TouchResult({...}) == {...}` and `bool(TouchResult({}))`
    behave exactly like the plain dict this function used to return, because
    dict equality and truthiness only ever look at the mapping's contents —
    never at extra instance attributes. Every existing caller/test that
    pattern-matches the old return value keeps working unchanged; the one
    caller that needs the inserted row's id reads `.touch_id` off the same
    object.
    """

    def __init__(self, mapping: dict[str, str], *, touch_id: int):
        super().__init__(mapping)
        self.touch_id = touch_id


class ContactNotFound(LookupError):
    """Raised when contact_id doesn't exist, or doesn't belong to user_id.

    The two cases are indistinguishable to the caller by design: there is
    no unscoped path, so "wrong tenant" and "doesn't exist" must carry the
    same observable behavior, or a contact_id could be used to probe
    whether a given id belongs to some other user."""

    def __init__(self, contact_id: int, user_id: int):
        super().__init__(f"no contact id={contact_id} for user_id={user_id}")
        self.contact_id = contact_id
        self.user_id = user_id


def _rank_case_sql(test_expression: str) -> str:
    """Builds `CASE <test_expression> WHEN 'cold' THEN 0 ... END` from
    WARMTH_RANK. `test_expression` is always either the literal column name
    `warmth` or a bind-parameter placeholder — never caller-supplied text —
    so this stays injection-safe the same way the original was: the WHEN
    values come from this module's own hardcoded WARMTH_RANK dict, not from
    any request/user input."""
    cases = " ".join(f"WHEN '{warmth}' THEN {rank}" for warmth, rank in WARMTH_RANK.items())
    return f"CASE {test_expression} {cases} END"


def apply_touch(
    conn: DBConnection,
    user_id: int,
    contact_id: int,
    kind: str,
    channel: str,
    note: str | None,
    *,
    now: datetime | None = None,
    source: str = "manual",
) -> TouchResult:
    """Log one interaction against contact_id and auto-advance warmth /
    thread_state where TOUCH_TRANSITIONS says to. See the module docstring
    for the full behavior contract (ratchet, terminal-advocate guard,
    TOCTOU-safe atomic update).

    - Always inserts a `touches` row, for every kind, whether or not
      warmth/thread_state end up changing.
    - Warmth only ever moves *up* the WARMTH_RANK ladder (cold -> replied
      -> chatted -> advocate), never down — a new touch can't demote
      someone.
    - thread_state advances per TOUCH_TRANSITIONS unless it's already
      'advocate' (terminal — only set_state() changes it once there).
    - Returns a TouchResult: the dict of `contacts` columns that were
      changed (empty if nothing moved), e.g. {"warmth": "chatted",
      "thread_state": "chat_done"}, plus a `.touch_id` attribute carrying
      the id of the row just inserted (see TouchResult's docstring for why
      this is additive and not a behavior change for existing callers).
    - `source` (default "manual", one of Touch.SOURCE_CHOICES on the Django
      side — this module has no FK to validate against, so an unrecognized
      value is written as-is rather than rejected) is stored verbatim on the
      new row instead of the hardcoded literal 'manual' this used to write
      regardless of who called it. That hardcoding meant "capture" (a
      captured touch, vs. one a student typed in) could never be recorded —
      the one metric the product most needs to answer was unanswerable from
      the DB.

    `kind` is validated against TOUCH_TRANSITIONS (raises ValueError) so
    this can never silently do the wrong thing if called with a bad kind.
    `channel` is NOT validated here, matching the original — validating
    it against CHANNELS is a caller concern (the original CLI did this
    itself before calling apply_touch, to surface a friendly error instead
    of a constraint violation).

    Raises:
        ValueError: kind not in TOUCH_TRANSITIONS.
        ContactNotFound: no contact_id row scoped to user_id.
    """
    if kind not in TOUCH_TRANSITIONS:
        raise ValueError(f"kind must be one of {', '.join(TOUCH_TRANSITIONS)}, got {kind!r}")

    new_warmth, new_state = TOUCH_TRANSITIONS[kind]
    ts = now or datetime.now(timezone.utc)

    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT warmth, thread_state FROM contacts WHERE id = %s AND user_id = %s",
            (contact_id, user_id),
        )
        before = cur.fetchone()
        if before is None:
            raise ContactNotFound(contact_id, user_id)

        cur.execute(
            "INSERT INTO touches (user_id, contact_id, ts, channel, kind, note, source) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, contact_id, ts, channel, kind, note, source),
        )
        touch_id = cur.fetchone()["id"]

        # The ratchet: both CASE expressions read live column values at
        # the moment THIS statement executes (contacts.warmth here, plus
        # whatever the row's warmth/thread_state are at lock-acquisition
        # time) — never a value computed earlier in Python. That is what
        # makes this safe against a second writer landing between our
        # SELECT above and this UPDATE: whichever transaction's UPDATE
        # commits second still re-evaluates against whatever the first one
        # just committed, because the comparison happens inside the
        # database, not in this function's Python.
        # CAST(... AS text) on every bind-parameter occurrence: Postgres
        # cannot infer a bare parameter's type inside `WHEN $1 IS NOT NULL`
        # or a CASE comparison and raises AmbiguousParameter (it may also be
        # a typeless NULL, e.g. for a touch kind whose transition is
        # (None, None)). SQLite tolerates untyped params, so this masked
        # cleanly there. `CAST(x AS text)` is valid in both engines (unlike
        # Postgres-only `::text`), keeping the SQLite test shim working.
        cur.execute(
            "UPDATE contacts SET "
            "warmth = CASE "
            "WHEN CAST(%(new_warmth)s AS text) IS NOT NULL "
            f"AND ({_rank_case_sql('CAST(%(new_warmth)s AS text)')}) > ({_rank_case_sql('warmth')}) "
            "THEN CAST(%(new_warmth)s AS text) ELSE warmth END, "
            "thread_state = CASE "
            "WHEN CAST(%(new_state)s AS text) IS NOT NULL AND thread_state != 'advocate' "
            "THEN CAST(%(new_state)s AS text) ELSE thread_state END "
            "WHERE id = %(contact_id)s AND user_id = %(user_id)s",
            {
                "new_warmth": new_warmth,
                "new_state": new_state,
                "contact_id": contact_id,
                "user_id": user_id,
            },
        )

        cur.execute(
            "SELECT warmth, thread_state FROM contacts WHERE id = %s AND user_id = %s",
            (contact_id, user_id),
        )
        after = cur.fetchone()
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()

    updates: dict[str, str] = {}
    if after["warmth"] != before["warmth"]:
        updates["warmth"] = after["warmth"]
    if after["thread_state"] != before["thread_state"]:
        updates["thread_state"] = after["thread_state"]
    return TouchResult(updates, touch_id=touch_id)


def set_state(
    conn: DBConnection,
    user_id: int,
    contact_id: int,
    *,
    warmth: str | None = None,
    thread_state: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Manual override: set warmth and/or thread_state directly, bypassing
    apply_touch's ratchet and its terminal-advocate guard entirely. Ported
    from the CLI's `campaign contact set` warmth/state behavior — this is
    the ONLY path in this package that can move warmth down, or move
    thread_state out of 'advocate'.

    New in this port: always inserts an audit `touches` row (kind=
    MANUAL_OVERRIDE_KIND) recording what changed, so the touches log has no
    gap here. The original `contact_set` CLI command did not log a touch
    for this path at all.

    Mirrors contact_set's one bit of implicit behavior: setting
    warmth='advocate' with no explicit thread_state also sets
    thread_state='advocate' (an explicit thread_state always wins over
    that default, whatever order the two keyword arguments are given in).

    A no-op call (both warmth and thread_state omitted) makes no database
    change and inserts no audit touch — matching the original CLI's
    "Nothing to update" short-circuit.

    Raises:
        ValueError: warmth not in WARMTH, or thread_state not in
            THREAD_STATES.
        ContactNotFound: no contact_id row scoped to user_id.
    """
    if warmth is not None and warmth not in WARMTH:
        raise ValueError(f"warmth must be one of {WARMTH}, got {warmth!r}")
    if thread_state is not None and thread_state not in THREAD_STATES:
        raise ValueError(f"thread_state must be one of {THREAD_STATES}, got {thread_state!r}")

    updates: dict[str, str] = {}
    if warmth is not None:
        updates["warmth"] = warmth
        if warmth == "advocate" and thread_state is None:
            updates["thread_state"] = "advocate"
    if thread_state is not None:
        updates["thread_state"] = thread_state

    if not updates:
        return {}

    ts = now or datetime.now(timezone.utc)

    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM contacts WHERE id = %s AND user_id = %s",
            (contact_id, user_id),
        )
        if cur.fetchone() is None:
            raise ContactNotFound(contact_id, user_id)

        set_clause = ", ".join(f"{column} = %s" for column in updates)
        cur.execute(
            f"UPDATE contacts SET {set_clause} WHERE id = %s AND user_id = %s",
            (*updates.values(), contact_id, user_id),
        )

        audit_note = "manual override: " + ", ".join(f"{k}={v}" for k, v in updates.items())
        if note:
            audit_note = f"{audit_note} — {note}"

        cur.execute(
            "INSERT INTO touches (user_id, contact_id, ts, channel, kind, note, source) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'manual')",
            (user_id, contact_id, ts, None, MANUAL_OVERRIDE_KIND, audit_note),
        )
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()

    return updates
