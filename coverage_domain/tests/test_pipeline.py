"""Regression tests for coverage_domain.pipeline — the ported warmth /
thread-state ratchet.

Ported from (semantics, not style — pytest here; the originals are plain
stdlib asserts run by a manual runner):

  - campaign/src/campaign/pipeline.py's own docstring and inline comments.
    There was no dedicated test_pipeline.py in the original suite —
    apply_touch had no direct unit tests of its own; it was only exercised
    indirectly through netdash/gmail_enrich.py's tests. The properties
    those comments describe (the ratchet, the terminal-advocate guard, the
    TOCTOU-safe atomic update) are what this file tests directly for the
    first time.

  - campaign/dashboard/tests/test_gmail_enrich.py — source of the
    stage-ratchet regression tests (a chat that actually happened being
    silently swallowed and reported as "already logged, skipped"). Those
    tests exercise netdash.gmail_enrich.apply_enrichment's own
    thread-based stage-rank dedup (_thread_stage_rank), a layer that sits
    ABOVE pipeline.apply_touch and is NOT part of this port (it's a
    separate row in build-plan.md §4's port table: "netdash/gmail_enrich.py
    apply layer"). What ported here is the primitive that layer calls once
    it has already decided a finding should log: pipeline.apply_touch.
    Each test below that has a gmail_enrich lineage says so explicitly and
    explains how the property maps down to this layer — in one case
    (test_thread_state_is_not_rank_guarded_outside_advocate) the honest
    answer is "this layer does NOT reproduce that guarantee by itself,
    and here is exactly why that's still correct."

Runs against two backends via the parametrized `conn` fixture (see
conftest.py): a real SQLite engine wrapped in a paramstyle-translating
shim (always runs, no external services), and real Postgres (skips
cleanly in this sandbox — no local Postgres available; activates
automatically under the project's CI Postgres service). Every test in
this file passed against the SQLite backend and was skipped (not
"passed") against Postgres in this environment — see the task report for
the actual pytest output.
"""

from datetime import datetime, timezone

import pytest

from coverage_domain import pipeline

USER = 1
OTHER_USER = 2


def _state(conn, contact_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT warmth, thread_state FROM contacts WHERE id = %s", (contact_id,)
    )
    row = cur.fetchone()
    cur.execute(
        "SELECT kind FROM touches WHERE contact_id = %s ORDER BY id", (contact_id,)
    )
    kinds = [r["kind"] for r in cur.fetchall()]
    return row["warmth"], row["thread_state"], kinds


_PORTED_TRANSITIONS = {
    "outreach": (None, None),
    "follow_up": (None, None),
    "reply_received": ("replied", "replied"),
    "chat": ("chatted", "chat_done"),
    "chat_scheduled": ("replied", "chat_scheduled"),
    "thank_you": (None, None),
    "maintain": (None, None),
    "reping": (None, None),
}

# Kinds Coverage added after the port. Listed explicitly, and asserted to
# be inert, so this file keeps its original job: an entry appearing here
# without a deliberate edit is still a failure, and an entry here that
# quietly starts moving warmth or thread_state is also still a failure.
_COVERAGE_NATIVE_TRANSITIONS = {
    pipeline.BULK_RECEIVED_KIND: (None, None),
}


def test_vocabularies_match_the_original_verbatim():
    """Structural pin against silent drift: the task's instruction is to
    port TOUCH_TRANSITIONS, WARMTH_RANK, and the vocabularies verbatim.

    Every PORTED entry must still read exactly as it did — that is the
    original assertion, unchanged. Additions made since the port are
    enumerated separately rather than folded into the same literal, so
    "the port is intact" and "we added a kind on purpose" stay two
    different, separately-failing claims.
    """
    assert pipeline.TOUCH_TRANSITIONS == {
        **_PORTED_TRANSITIONS,
        **_COVERAGE_NATIVE_TRANSITIONS,
    }
    for kind in _PORTED_TRANSITIONS:
        assert pipeline.TOUCH_TRANSITIONS[kind] == _PORTED_TRANSITIONS[kind]
    # Every Coverage-native kind is inert by construction: it may be
    # logged, it may never move the ladder.
    for kind in _COVERAGE_NATIVE_TRANSITIONS:
        assert pipeline.TOUCH_TRANSITIONS[kind] == (None, None)
    assert pipeline.WARMTH_RANK == {"cold": 0, "replied": 1, "chatted": 2, "advocate": 3}
    assert pipeline.CHANNELS == ("email", "linkedin", "coffee_chat", "call", "event", "other")
    assert pipeline.WARMTH == ("cold", "replied", "chatted", "advocate")
    assert pipeline.THREAD_STATES == (
        "no_reply", "replied", "chat_scheduled", "chat_done", "advocate", "quiet", "parked",
    )


def test_invalid_kind_raises_value_error(conn, make_contact):
    """Ported guard: 'this function also guards it so it can never
    silently do the wrong thing if called with a bad kind.'"""
    cid = make_contact(USER)
    with pytest.raises(ValueError):
        pipeline.apply_touch(conn, USER, cid, "not_a_real_kind", "email", None)


def test_apply_touch_always_inserts_a_touch_row_even_with_no_state_effect(conn, make_contact):
    """'outreach' maps to (None, None) in TOUCH_TRANSITIONS — it must still
    log a touch. Ported from the original docstring's 'Always inserts a
    touches row' and gmail_enrich's 'A real touches row lands for audit
    either way.'"""
    cid = make_contact(USER)
    updates = pipeline.apply_touch(conn, USER, cid, "outreach", "email", "cold email sent")
    assert updates == {}
    _, _, kinds = _state(conn, cid)
    assert kinds == ["outreach"]


def test_warmth_ratchet_only_moves_up(conn, make_contact):
    """Ported: 'Warmth only ever moves up the WARMTH_RANK ladder ... never
    down — a new touch can't demote someone.'"""
    cid = make_contact(USER, warmth="chatted", thread_state="chat_done")
    # reply_received maps to warmth="replied" (rank 1), below the contact's
    # current "chatted" (rank 2) -> must not move warmth down.
    updates = pipeline.apply_touch(conn, USER, cid, "reply_received", "email", None)
    assert "warmth" not in updates
    warmth, _, _ = _state(conn, cid)
    assert warmth == "chatted"


def test_warmth_ratchet_moves_up_across_ranks(conn, make_contact):
    cid = make_contact(USER, warmth="cold", thread_state="no_reply")
    updates = pipeline.apply_touch(conn, USER, cid, "chat", "coffee_chat", "great chat")
    assert updates == {"warmth": "chatted", "thread_state": "chat_done"}
    warmth, state, _ = _state(conn, cid)
    assert (warmth, state) == ("chatted", "chat_done")


def test_terminal_advocate_guard_blocks_thread_state_changes(conn, make_contact):
    """Ported: 'advocate is terminal; only an explicit contact set leaves
    that state.' A TOUCH_TRANSITIONS-driven thread_state change must not
    overwrite it."""
    cid = make_contact(USER, warmth="advocate", thread_state="advocate")
    updates = pipeline.apply_touch(conn, USER, cid, "chat", "email", None)
    # warmth is already at the ladder's top rank, so it can't move further
    # either -- but the touch itself must still log (see the test above).
    assert "thread_state" not in updates
    assert "warmth" not in updates
    _, state, kinds = _state(conn, cid)
    assert state == "advocate"
    assert kinds == ["chat"]


def test_thread_state_is_not_rank_guarded_outside_advocate(conn, make_contact):
    """Pins the exact boundary this port draws: apply_touch alone does NOT
    protect thread_state ordering except for the advocate terminal case. A
    'chat' touch (-> chat_done) followed directly by a 'chat_scheduled'
    touch (-> chat_scheduled) DOES move thread_state backward at this
    layer.

    In the original system this specific regression could not reach
    apply_touch in production, because netdash/gmail_enrich.py's own
    per-thread stage-rank guard (_thread_stage_rank, NOT ported here — see
    build-plan.md §4) refuses to even call apply_touch for a
    same-or-lower-ranked finding on a given Gmail thread. Calling this
    primitive directly and out of order — which is exactly what a
    differently-behaved future caller could do — does regress
    thread_state, matching the exact wording of
    campaign/dashboard/tests/test_gmail_enrich.py's
    test_completed_does_not_regress_to_scheduled docstring: 'apply_touch
    guards warmth with WARMTH_RANK but not thread_state, so a mis-read
    "scheduled" on a finished chat would knock chat_done back ... and
    cancel the thank-you.' This test exists so that boundary is an
    explicit, asserted fact about this package rather than a silent gap."""
    cid = make_contact(USER, warmth="cold", thread_state="no_reply")
    pipeline.apply_touch(conn, USER, cid, "chat", "email", None)
    warmth_after_chat, state_after_chat, _ = _state(conn, cid)
    assert (warmth_after_chat, state_after_chat) == ("chatted", "chat_done")

    pipeline.apply_touch(conn, USER, cid, "chat_scheduled", "email", None)
    warmth, state, kinds = _state(conn, cid)
    assert state == "chat_scheduled", "thread_state regresses here by design -- see docstring"
    assert warmth == "chatted", "warmth stays put: replied (rank 1) is not > chatted (rank 2)"
    assert kinds == ["chat", "chat_scheduled"]


def test_full_stage_progression_all_touches_land(conn, make_contact):
    """Analogue of test_gmail_enrich.py's test_full_ladder_one_thread at
    this layer: reply -> scheduled -> completed, each a distinct call,
    each landing once, ending at (chatted, chat_done)."""
    cid = make_contact(USER)
    pipeline.apply_touch(conn, USER, cid, "reply_received", "email", None)
    pipeline.apply_touch(conn, USER, cid, "chat_scheduled", "email", None)
    pipeline.apply_touch(conn, USER, cid, "chat", "email", None)
    warmth, state, kinds = _state(conn, cid)
    assert (warmth, state) == ("chatted", "chat_done")
    assert kinds == ["reply_received", "chat_scheduled", "chat"]


def test_repeated_same_kind_calls_both_log_and_do_not_change_state_twice(conn, make_contact):
    """apply_touch has no dedup of its own (that's the caller's job -- see
    the gmail_enrich boundary note above): calling the same kind twice logs
    two touches, but the effect on warmth/thread_state is idempotent past
    the first call, since the second call's target values already match
    what's there. This is the pipeline-level shape of what
    test_gmail_enrich.py's test_same_stage_twice_is_noop protects one layer
    up (there, the *caller* skips the second call entirely; here, the
    second call is made and still resolves to a no-op update)."""
    cid = make_contact(USER)
    first = pipeline.apply_touch(conn, USER, cid, "chat_scheduled", "email", None)
    second = pipeline.apply_touch(conn, USER, cid, "chat_scheduled", "email", None)
    assert first == {"warmth": "replied", "thread_state": "chat_scheduled"}
    assert second == {}, "no further change the second time -> empty updates dict"
    _, _, kinds = _state(conn, cid)
    assert kinds == ["chat_scheduled", "chat_scheduled"], "but both calls still logged a touch"


def test_ratchet_uses_live_db_state_not_a_python_snapshot(conn, make_contact):
    """The TOCTOU-safety property, made concrete without needing real
    concurrency: apply_touch is never given a warmth/thread_state value by
    its caller at all (only a contact_id) -- the ratchet comparison is
    entirely a live SQL CASE evaluated against the row at UPDATE-time.
    Modeled here as two 'writers' applying out-of-rank-order touches
    sequentially to the same row: whichever call lands second must still
    see and respect what the first one committed -- exactly as it would if
    the two calls came from genuinely concurrent transactions instead of
    the same thread one after another.

    This test cannot, by itself, prove the atomic update survives real
    concurrent transactions (no two transactions are ever open at once
    here) -- see test_pipeline_postgres.py for the one test in this suite
    that forces genuine interleaving. What this test does prove is that
    the ratchet decision has no Python-side input to go stale in the first
    place, which is the actual mechanism that makes concurrent use safe."""
    cid = make_contact(USER, warmth="cold", thread_state="no_reply")
    # "Writer B": a chat that already happened.
    pipeline.apply_touch(conn, USER, cid, "chat", "email", None)
    # "Writer A": a lower-ranked reply_received landing after -- must not
    # downgrade warmth from chatted(2) back to replied(1).
    updates = pipeline.apply_touch(conn, USER, cid, "reply_received", "email", None)
    assert "warmth" not in updates
    warmth, _, _ = _state(conn, cid)
    assert warmth == "chatted"


def test_tenancy_apply_touch_wrong_user_is_not_found(conn, make_contact):
    """Required change: 'Add a user_id tenancy parameter to every function
    that touches data. Every query must be scoped by it. There is no
    unscoped path.'"""
    cid = make_contact(USER)
    with pytest.raises(pipeline.ContactNotFound):
        pipeline.apply_touch(conn, OTHER_USER, cid, "outreach", "email", None)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM touches WHERE contact_id = %s", (cid,))
    assert cur.fetchone()["n"] == 0, "no touch should have been inserted under the wrong tenant"


def test_manual_override_bypasses_ratchet_and_can_move_warmth_down(conn, make_contact):
    """Ported from the CLI's `contact set`: an explicit override is not
    rank-guarded -- unlike apply_touch, it can move warmth backward."""
    cid = make_contact(USER, warmth="chatted", thread_state="chat_done")
    updates = pipeline.set_state(conn, USER, cid, warmth="cold")
    assert updates == {"warmth": "cold"}
    warmth, state, kinds = _state(conn, cid)
    assert (warmth, state) == ("cold", "chat_done")
    assert kinds == [pipeline.MANUAL_OVERRIDE_KIND]


def test_manual_override_inserts_exactly_one_audit_touch(conn, make_contact):
    """The one intentional addition in this port (build-plan.md §2): the
    original CLI's `contact set` mutated the row directly and logged no
    touch at all for this path."""
    cid = make_contact(USER)
    pipeline.set_state(conn, USER, cid, warmth="advocate")
    cur = conn.cursor()
    cur.execute("SELECT kind, note FROM touches WHERE contact_id = %s", (cid,))
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == pipeline.MANUAL_OVERRIDE_KIND
    assert "warmth=advocate" in rows[0]["note"]


def test_manual_override_setting_advocate_warmth_also_sets_thread_state(conn, make_contact):
    """Mirrors contact_set's implicit behavior: warmth='advocate' with no
    explicit thread_state also sets thread_state='advocate'."""
    cid = make_contact(USER, warmth="cold", thread_state="no_reply")
    updates = pipeline.set_state(conn, USER, cid, warmth="advocate")
    assert updates == {"warmth": "advocate", "thread_state": "advocate"}


def test_manual_override_explicit_thread_state_wins_over_advocate_default(conn, make_contact):
    cid = make_contact(USER)
    updates = pipeline.set_state(conn, USER, cid, warmth="advocate", thread_state="quiet")
    assert updates == {"warmth": "advocate", "thread_state": "quiet"}


def test_manual_override_can_exit_advocate_state(conn, make_contact):
    """The other half of the terminal-advocate guard: apply_touch can never
    move a contact OUT of 'advocate' (see
    test_terminal_advocate_guard_blocks_thread_state_changes above) --
    only set_state can, by design ('only an explicit set operation leaves
    that state')."""
    cid = make_contact(USER, warmth="advocate", thread_state="advocate")
    updates = pipeline.set_state(conn, USER, cid, thread_state="quiet")
    assert updates == {"thread_state": "quiet"}
    _, state, _ = _state(conn, cid)
    assert state == "quiet"


def test_manual_override_noop_call_inserts_no_audit_touch(conn, make_contact):
    cid = make_contact(USER)
    updates = pipeline.set_state(conn, USER, cid)
    assert updates == {}
    _, _, kinds = _state(conn, cid)
    assert kinds == []


def test_manual_override_invalid_warmth_raises(conn, make_contact):
    cid = make_contact(USER)
    with pytest.raises(ValueError):
        pipeline.set_state(conn, USER, cid, warmth="super-warm")


def test_manual_override_invalid_thread_state_raises(conn, make_contact):
    cid = make_contact(USER)
    with pytest.raises(ValueError):
        pipeline.set_state(conn, USER, cid, thread_state="ghosted")


def test_tenancy_set_state_wrong_user_is_not_found(conn, make_contact):
    cid = make_contact(USER)
    with pytest.raises(pipeline.ContactNotFound):
        pipeline.set_state(conn, OTHER_USER, cid, warmth="advocate")
    warmth, _, kinds = _state(conn, cid)
    assert warmth == "cold"
    assert kinds == []


# --------------------------------------------------------------------------- #
# The event-order guard (pipeline.py's module docstring, fourth change).
#
# Every case below is a shape measured on the founder's live account on
# 2026-09-01, where the Gmail backfill stamps `now=occurred_at` (p90 35 days
# back) and the write-order ratchet let an older event overturn a newer
# decision: 27 touches on 17 contacts written out of `ts` order, 4 park
# overrides overturned, 2 contacts un-parked with no un-park row, 1 regressed
# from `chat_scheduled` to `replied`.
# --------------------------------------------------------------------------- #

_JULY = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
_AUGUST = datetime(2026, 8, 10, 6, 43, tzinfo=timezone.utc)


def test_state_moving_kinds_is_derived_from_the_transition_table():
    """The guard's "what counts as a newer decision" set is derived, never
    hand-listed: a kind added to TOUCH_TRANSITIONS with a real transition
    joins it automatically, and an inert kind stays out."""
    assert set(pipeline.STATE_MOVING_KINDS) == {
        "reply_received", "chat", "chat_scheduled",
    }
    assert pipeline.MANUAL_OVERRIDE_KIND not in pipeline.STATE_MOVING_KINDS


def test_backdated_reply_after_a_park_leaves_the_contact_parked(conn, make_contact):
    """Nicole Park (366) and Myra Fernandez (367): parked by the Aug 10
    board bulk park, then un-parked with no un-park row on the ledger by a
    July reply the backfill wrote in late August. The reply is real and is
    still recorded; the park is newer and still stands."""
    cid = make_contact(USER)
    pipeline.set_state(
        conn, USER, cid, thread_state="parked",
        note="Parked from the Network board (bulk)", now=_AUGUST,
    )
    result = pipeline.apply_touch(
        conn, USER, cid, "reply_received", "email", "[gmail:t1] They replied",
        now=_JULY, source="capture",
    )
    assert result == {}
    assert result.stale is True
    warmth, state, kinds = _state(conn, cid)
    assert (warmth, state) == ("cold", "parked")
    # The ledger keeps the row — the guard suppresses the state move, never
    # the record of what happened.
    assert kinds == [pipeline.MANUAL_OVERRIDE_KIND, "reply_received"]


def test_backdated_reply_after_a_newer_state_moving_touch_leaves_state(conn, make_contact):
    """Lily Liu (765): `chat_scheduled` logged Aug 25, then two replies dated
    Aug 24 and Aug 25 written afterwards regressed her to `replied`. The
    older replies are recorded; `chat_scheduled` survives."""
    cid = make_contact(USER)
    pipeline.apply_touch(
        conn, USER, cid, "chat_scheduled", "email", None,
        now=datetime(2026, 8, 25, 15, 10, tzinfo=timezone.utc),
    )
    result = pipeline.apply_touch(
        conn, USER, cid, "reply_received", "email", None,
        now=datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc), source="capture",
    )
    assert result == {}
    assert result.stale is True
    warmth, state, _ = _state(conn, cid)
    assert (warmth, state) == ("replied", "chat_scheduled")


def test_in_order_touches_still_ratchet(conn, make_contact):
    """Degradation clause: a touch that is not older than anything on file
    moves the ladder exactly as it always did, `.stale` False. This is the
    hand-logged case (`now=None`) and the in-order captured case both."""
    cid = make_contact(USER)
    early = pipeline.apply_touch(
        conn, USER, cid, "reply_received", "email", None, now=_JULY,
    )
    assert early == {"warmth": "replied", "thread_state": "replied"}
    assert early.stale is False
    later = pipeline.apply_touch(
        conn, USER, cid, "chat", "coffee_chat", None, now=_AUGUST,
    )
    assert later == {"warmth": "chatted", "thread_state": "chat_done"}
    assert later.stale is False
    hand_logged = pipeline.apply_touch(conn, USER, cid, "reply_received", "email", None)
    assert hand_logged.stale is False


def test_guard_ignores_touches_that_never_moved_anything(conn, make_contact):
    """An `outreach` row dated later than the reply is not a decision — it
    moves nothing, so it must not make a later-written reply stale. Only
    STATE_MOVING_KINDS and overrides order the ladder."""
    cid = make_contact(USER)
    pipeline.apply_touch(conn, USER, cid, "outreach", "email", None, now=_AUGUST)
    result = pipeline.apply_touch(
        conn, USER, cid, "reply_received", "email", None, now=_JULY, source="capture",
    )
    assert result == {"warmth": "replied", "thread_state": "replied"}
    assert result.stale is False


def test_an_inert_kind_is_never_stale(conn, make_contact):
    """A `bulk_received` row moves nothing whatever its date, so the guard
    has nothing to decide and the row lands unremarkably."""
    cid = make_contact(USER)
    pipeline.set_state(conn, USER, cid, thread_state="parked", now=_AUGUST)
    result = pipeline.apply_touch(
        conn, USER, cid, pipeline.BULK_RECEIVED_KIND, "email", None,
        now=_JULY, source="capture",
    )
    assert result == {}
    assert result.stale is False
    _, state, kinds = _state(conn, cid)
    assert state == "parked"
    assert kinds == [pipeline.MANUAL_OVERRIDE_KIND, pipeline.BULK_RECEIVED_KIND]


def test_guard_is_scoped_to_the_contact(conn, make_contact):
    """A newer override on somebody ELSE'S row must not freeze this contact.
    The guard's query carries the contact and tenant scopes for the same
    reason every other query in this module does."""
    mine = make_contact(USER)
    other_contact = make_contact(USER)
    pipeline.set_state(conn, USER, other_contact, thread_state="parked", now=_AUGUST)
    result = pipeline.apply_touch(
        conn, USER, mine, "reply_received", "email", None, now=_JULY,
    )
    assert result == {"warmth": "replied", "thread_state": "replied"}
    assert result.stale is False


def test_equal_timestamps_are_not_stale(conn, make_contact):
    """"Predates", strictly. A touch stamped at the same instant as the
    override is not older than it, and the ratchet still applies — the guard
    exists to stop a July event overturning an August one, not to make
    same-instant writes order-dependent in a second way."""
    cid = make_contact(USER)
    pipeline.set_state(conn, USER, cid, thread_state="parked", now=_AUGUST)
    result = pipeline.apply_touch(
        conn, USER, cid, "reply_received", "email", None, now=_AUGUST,
    )
    assert result == {"warmth": "replied", "thread_state": "replied"}
    assert result.stale is False


def test_set_state_records_the_door_that_made_the_override(conn, make_contact):
    """179 of the founder's 179 override rows said `source='manual'`
    whichever door wrote them, so "which tap parked these 44 people" was
    answerable only by regex over prose. The default is unchanged."""
    cid = make_contact(USER)
    pipeline.set_state(
        conn, USER, cid, thread_state="parked",
        note="Parked from the Today queue (bulk)", source="park_all",
    )
    other = make_contact(USER)
    pipeline.set_state(conn, USER, other, thread_state="parked")
    cur = conn.cursor()
    cur.execute("SELECT contact_id, source FROM touches ORDER BY id")
    by_contact = {r["contact_id"]: r["source"] for r in cur.fetchall()}
    assert by_contact[cid] == "park_all"
    assert by_contact[other] == "manual"
