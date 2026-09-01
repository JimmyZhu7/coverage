"""Branch 6's follow-up shelf life: `followup_expires_after_business_days`
(the 2026-09-01 divergence in cadence.py's module docstring).

THE DEFECT, measured on the founder's live queue that day: 44 of 44 cards
were "follow up", every one on a note sent 27 business days earlier. Branch
6 offered the one follow-up forever — a contact with a single outbound
touch and no reply read `follow_up` at 27 business days exactly as at 6 —
and the only expiry anywhere in the tree was the thank-you's. A stranger
re-appearing five weeks after one unanswered email is a second cold open,
not a follow-up, and the research says stop well before that.

Pure engine tests, like test_cadence.py: plain dicts, an explicit as-of, no
DB, no clock. The web-side tunable (crm.today.TUNABLE_CADENCE_PARAMS) and
its Settings label are covered in crm/tests/test_followup_expiry_tunable.py.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

from coverage_domain import cadence

UTC = timezone.utc
# A Tuesday, so business-day arithmetic below is unambiguous.
TODAY = date(2026, 9, 1)
AS_OF = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
FIRMS = {"usfirm": {"name": "US Firm", "tier": 1}}


def contact(cid, **kw):
    base = dict(id=cid, firm_id="usfirm", warmth="cold", thread_state="no_reply", source=None)
    base.update(kw)
    return base


def touch(cid, kind, ts, **kw):
    """`ts` is a date (10:00 UTC that day), a datetime, or None for the
    undated-touch case the engine has to cope with."""
    if ts is None or isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.combine(ts, datetime.min.time().replace(hour=10), tzinfo=UTC)
    base = dict(contact_id=cid, kind=kind, ts=dt, channel="email", note=None)
    base.update(kw)
    return base


def business_days_ago(n: int) -> date:
    """The date exactly `n` business days before TODAY, by the engine's own
    counter — so a test says "27 business days" and the calendar does the
    arithmetic, not the author."""
    d = TODAY
    while cadence.business_days_since(d, TODAY) < n:
        d -= timedelta(days=1)
    assert cadence.business_days_since(d, TODAY) == n
    return d


def only(actions, cid=1):
    mine = [a for a in actions if a["contact"]["id"] == cid]
    assert len(mine) == 1, mine
    return mine[0]


def run(touches, **params):
    return cadence.due_actions([contact(1)], touches, [], as_of=AS_OF, firms=FIRMS,
                               params=params or None)


# ---------------------------------------------------------------------------
# 1. The default and its shape.
# ---------------------------------------------------------------------------
def test_default_is_fifteen_business_days_and_sits_past_the_followup_window():
    d = cadence.CADENCE_DEFAULTS
    assert d["followup_expires_after_business_days"] == 15
    # Three working weeks, and clear of the follow-up window by enough that
    # a student who missed the card for a fortnight still sees it.
    assert d["followup_expires_after_business_days"] > d["followup_after_business_days"] + 5


def test_the_engine_stays_django_free():
    """`coverage_domain` is a PURE package. Importing the engine in a fresh
    interpreter must pull in nothing from Django — the whole reason the
    expiry lives here as an int and not as a model field."""
    code = (
        "import sys, coverage_domain.cadence; "
        "bad = sorted(m for m in sys.modules if m == 'django' or m.startswith('django.')); "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# ---------------------------------------------------------------------------
# 2. Inside the window: nothing changes.
# ---------------------------------------------------------------------------
def test_a_note_inside_the_window_still_gets_its_follow_up():
    a = only(run([touch(1, "outreach", business_days_ago(8))]))
    assert a["action"] == "follow_up"
    assert a["ctx"]["business_days"] == 8
    assert "expired" not in a["ctx"]


def test_day_fifteen_exactly_still_gets_the_follow_up():
    """Strict `>`, mirroring `thank_you_expires_after_days` (`hrs > days *
    24`): the window is a shelf life, and the last day of it is still on
    the shelf."""
    a = only(run([touch(1, "outreach", business_days_ago(15))]))
    assert a["action"] == "follow_up"


def test_a_brand_new_contact_is_still_first_outreach():
    """Zero outbound touches never reach the expiry test at all."""
    a = only(run([]))
    assert a["action"] == "first_outreach"


# ---------------------------------------------------------------------------
# 3. Past the window: park, with the reason naming the weeks.
# ---------------------------------------------------------------------------
def test_day_sixteen_parks_and_names_three_weeks():
    a = only(run([touch(1, "outreach", business_days_ago(16))]))
    assert a["action"] == "park"
    assert a["priority"] == 3
    assert a["reason"] == (
        "First note went unanswered 3 weeks ago. Park it, or re-open with a new reason."
    )
    assert a["ctx"]["expired"] is True
    assert a["ctx"]["business_days"] == 16
    assert a["ctx"]["weeks_silent"] == 3
    assert a["ctx"]["expiry_business_days"] == 15
    assert a["ctx"]["outbound"] == 1


def test_the_founders_twenty_seven_business_day_note_parks_at_five_weeks():
    """The measured case: 27 business days of silence after one note is
    five weeks, and the card says so instead of "follow up"."""
    a = only(run([touch(1, "outreach", business_days_ago(27))]))
    assert a["action"] == "park"
    assert a["reason"].startswith("First note went unanswered 5 weeks ago.")
    assert a["ctx"]["weeks_silent"] == 5


def test_weeks_never_read_zero_or_singular_wrongly():
    """`round(bd / 5)` floors to one week at the smallest expiry a user can
    set, and the noun agrees with the number."""
    a = only(run([touch(1, "outreach", business_days_ago(6))],
                 followup_expires_after_business_days=5))
    assert a["action"] == "park"
    assert "1 week ago" in a["reason"]
    assert "1 weeks" not in a["reason"]


def test_an_undated_first_note_is_expired_too():
    """Outbound touches with no dateable ts: branch 1 reads an undated chat
    as expired and the park test reads an undated note as "definitely stale
    enough". The follow-up's expiry reads it the same way, so an undated
    note cannot sit in the queue forever — which is the defect this whole
    file is about, in a different coat."""
    a = only(run([touch(1, "outreach", None)]))
    assert a["action"] == "park"
    assert a["reason"] == (
        "First note went unanswered, no dateable touch on record. "
        "Park it, or re-open with a new reason."
    )
    assert a["ctx"]["weeks_silent"] is None
    assert a["ctx"]["business_days"] is None
    assert "999" not in a["reason"]


# ---------------------------------------------------------------------------
# 4. The knob.
# ---------------------------------------------------------------------------
def test_a_longer_window_brings_the_follow_up_back():
    a = only(run([touch(1, "outreach", business_days_ago(27))],
                 followup_expires_after_business_days=60))
    assert a["action"] == "follow_up"


def test_a_shorter_window_parks_sooner():
    a = only(run([touch(1, "outreach", business_days_ago(8))],
                 followup_expires_after_business_days=5))
    assert a["action"] == "park"
    assert a["ctx"]["expiry_business_days"] == 5


# ---------------------------------------------------------------------------
# 5. What the expiry must NOT touch.
# ---------------------------------------------------------------------------
def test_the_existing_two_touch_park_keeps_its_own_reason():
    """A note plus its one follow-up, silent past the park window, was
    already a park before this change and still says why in the old
    words — the expiry is a THIRD way into park, not a rewrite of the other
    two."""
    touches = [
        touch(1, "outreach", business_days_ago(30)),
        touch(1, "follow_up", business_days_ago(20)),
    ]
    a = only(run(touches))
    assert a["action"] == "park"
    assert a["reason"] == "2 touches, no reply, 20 business days silent — park it"
    assert "expired" not in a["ctx"]


def test_a_contact_who_replied_is_branch_sevens_however_old_the_thread():
    c = contact(1, warmth="replied", thread_state="replied")
    touches = [
        touch(1, "outreach", business_days_ago(40)),
        touch(1, "reply_received", business_days_ago(30)),
    ]
    a = only(cadence.due_actions([c], touches, [], as_of=AS_OF, firms=FIRMS))
    assert a["action"] == "advance"


def test_a_parked_contact_stays_skipped():
    c = contact(1, thread_state="parked")
    actions = cadence.due_actions(
        [c], [touch(1, "outreach", business_days_ago(40))], [], as_of=AS_OF, firms=FIRMS
    )
    assert [a for a in actions if a["contact"]["id"] == 1] == []


@pytest.mark.parametrize("bd", [1, 5, 15])
def test_the_followup_window_still_gates_below_the_expiry(bd):
    """A note younger than the follow-up window produces nothing — the expiry
    is only ever consulted once the follow-up would otherwise fire."""
    actions = run([touch(1, "outreach", business_days_ago(bd))],
                  followup_after_business_days=16,
                  followup_expires_after_business_days=20)
    assert [a for a in actions if a["contact"]["id"] == 1] == []
