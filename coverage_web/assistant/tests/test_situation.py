"""Tests for `assistant.situation.build_situation`: the three event types,
the tenant-scoped join that decides which opportunities are even in play,
the never-raises posture, and the caps.

Cross-tenant isolation (another student's tracked-opportunity changes never
leaking into this student's snapshot) is pinned in
`assistant/tests/test_isolation.py`, following that file's alice/bob
pattern, not here — this file is about whether each event type fires on
the right SHAPE of scenario for one student at a time.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from analytics.models import UserOpportunity
from assistant import situation
from crm.models import Contact, UserFirm
from directory.models import Firm, Opportunity, OpportunityChange

User = get_user_model()

pytestmark = pytest.mark.django_db


def _user(email="student@example.com"):
    return User.objects.create_user(email=email, password="pw12345!")


def _firm(name="Goldman Sachs", slug="goldman-sachs"):
    return Firm.objects.create(name=name, slug=slug)


def _opp(firm, *, title="Summer Analyst", status="open", deadline=None,
         first_seen=None, url=None):
    opp = Opportunity.objects.create(
        firm=firm, title=title, bucket="internship", status=status,
        deadline=deadline,
        url=url or f"https://example.com/{firm.slug}/{title.lower().replace(' ', '-')}-{Opportunity.objects.count()}",
    )
    if first_seen is not None:
        # `first_seen` is `auto_now_add`, so backdating it needs an UPDATE —
        # passing a value to the constructor is silently ignored.
        Opportunity.objects.filter(pk=opp.pk).update(first_seen=first_seen)
        opp.refresh_from_db()
    return opp


def _track(user, opp, **kw):
    tracked = UserOpportunity(user=user, opportunity=opp, **kw)
    tracked.save()
    return tracked


def _change(opp, field, old, new, *, stage="reverify", observed_at=None):
    return OpportunityChange.objects.create(
        opportunity=opp, field=field, old_value=old, new_value=new,
        stage=stage, observed_at=observed_at or timezone.now(),
    )


def _empty():
    return {"deadline_moved": [], "role_closed": [], "new_role_at_known_firm": [], "events": []}


# ---------------------------------------------------------------------------
# The baseline: nothing to report is not an error.
# ---------------------------------------------------------------------------
def test_a_student_with_no_changes_gets_an_empty_snapshot_not_an_error():
    result = situation.build_situation(_user())
    assert result == _empty()


def test_a_failure_degrades_to_the_same_empty_shape_never_raises(monkeypatch):
    """Same posture as assistant.brief: a bug in one of the three event
    queries must never turn into a broken Today page."""
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(situation, "_role_closed_events", boom)

    result = situation.build_situation(_user())

    assert result == _empty()


# ---------------------------------------------------------------------------
# deadline_moved
# ---------------------------------------------------------------------------
def test_a_tracked_roles_moved_deadline_is_reported():
    user = _user()
    firm = _firm()
    opp = _opp(firm, deadline=timezone.localdate() + timedelta(days=10))
    _track(user, opp)
    _change(opp, "deadline", "2026-08-01", "2026-08-15")

    result = situation.build_situation(user)

    assert len(result["deadline_moved"]) == 1
    event = result["deadline_moved"][0]
    assert event["opportunity_id"] == opp.id
    assert event["firm"] == "Goldman Sachs"
    assert event["old_value"] == "2026-08-01"
    assert event["new_value"] == "2026-08-15"
    assert event["old_date"] == timezone.datetime(2026, 8, 1).date()
    assert event["new_date"] == timezone.datetime(2026, 8, 15).date()
    assert event in result["events"]


def test_a_deadline_move_on_an_untracked_role_is_not_reported():
    """Only opportunities the student TRACKS — a deadline moving on some
    other role on the shared board is not this student's business."""
    user = _user()
    opp = _opp(_firm())
    # Deliberately never tracked.
    _change(opp, "deadline", "2026-08-01", "2026-08-15")

    result = situation.build_situation(user)

    assert result["deadline_moved"] == []


def test_a_deadline_move_outside_the_recent_window_is_not_reported():
    user = _user()
    opp = _opp(_firm())
    _track(user, opp)
    stale = timezone.now() - timedelta(days=situation.RECENT_DAYS + 5)
    _change(opp, "deadline", "2026-08-01", "2026-08-15", observed_at=stale)

    result = situation.build_situation(user)

    assert result["deadline_moved"] == []


def test_a_dismissed_tracked_roles_deadline_move_is_not_reported():
    """A dismissed role is one the student already said "not for me" — its
    deadline moving is noise, not news."""
    user = _user()
    opp = _opp(_firm())
    _track(user, opp, dismissed=True)
    _change(opp, "deadline", "2026-08-01", "2026-08-15")

    result = situation.build_situation(user)

    assert result["deadline_moved"] == []


def test_a_role_that_moved_twice_in_the_window_reports_once():
    """Two deadline-change rows on the same tracked opportunity collapse to
    one event — the most recent — not two disagreeing cards."""
    user = _user()
    opp = _opp(_firm())
    _track(user, opp)
    earlier = timezone.now() - timedelta(days=2)
    _change(opp, "deadline", "2026-08-01", "2026-08-10", observed_at=earlier)
    _change(opp, "deadline", "2026-08-10", "2026-08-20", observed_at=timezone.now())

    result = situation.build_situation(user)

    assert len(result["deadline_moved"]) == 1
    assert result["deadline_moved"][0]["new_value"] == "2026-08-20"


def test_deadline_moved_is_capped_at_max_per_type():
    user = _user()
    firm = _firm()
    for i in range(situation.MAX_PER_TYPE + 3):
        opp = _opp(firm, url=f"https://example.com/role-{i}")
        _track(user, opp)
        _change(opp, "deadline", "2026-08-01", "2026-08-15")

    result = situation.build_situation(user)

    assert len(result["deadline_moved"]) == situation.MAX_PER_TYPE


# ---------------------------------------------------------------------------
# role_closed
# ---------------------------------------------------------------------------
def test_a_tracked_role_that_closed_is_reported():
    user = _user()
    opp = _opp(_firm(), status="closed")
    _track(user, opp)
    _change(opp, "status", "open", "closed")

    result = situation.build_situation(user)

    assert len(result["role_closed"]) == 1
    assert result["role_closed"][0]["opportunity_id"] == opp.id
    assert result["role_closed"][0]["firm"] == "Goldman Sachs"


def test_a_role_that_closed_and_reopened_is_not_reported():
    """`directory.deadlines.is_posting_closed` gates the row against the
    posting's LIVE status: a role that closed and then reopened inside the
    same window is not news the student needs to act on — the scraper
    already resolved it on its own, and reporting it as closed would be
    stale by the time the student reads the card."""
    user = _user()
    opp = _opp(_firm(), status="open")  # reopened: live status is open again
    _track(user, opp)
    _change(opp, "status", "open", "closed")

    result = situation.build_situation(user)

    assert result["role_closed"] == []


def test_a_close_on_an_untracked_role_is_not_reported():
    user = _user()
    opp = _opp(_firm(), status="closed")
    _change(opp, "status", "open", "closed")

    result = situation.build_situation(user)

    assert result["role_closed"] == []


# ---------------------------------------------------------------------------
# new_role_at_known_firm
# ---------------------------------------------------------------------------
def test_a_new_role_at_a_firm_with_a_contact_is_reported():
    user = _user()
    firm = _firm()
    # Give the firm an older posting so it isn't itself a board debut (see
    # the debut test below) — a single fresh posting at a firm otherwise new
    # to Coverage should not fire this event.
    _opp(firm, url="https://example.com/old", first_seen=timezone.now() - timedelta(days=60))
    Contact(user=user, firm=firm, name="A Banker").save()
    opp = _opp(firm, url="https://example.com/new", first_seen=timezone.now())

    result = situation.build_situation(user)

    assert [e["opportunity_id"] for e in result["new_role_at_known_firm"]] == [opp.id]


def test_a_new_role_at_a_tiered_firm_with_no_contact_is_also_reported():
    """Judgement call, stated in the module docstring: UserFirm targets
    count too, not only firms with a contact — a firm ranked as a target
    but not yet met anyone at is just as "known" to the student."""
    user = _user()
    firm = _firm()
    _opp(firm, url="https://example.com/old", first_seen=timezone.now() - timedelta(days=60))
    UserFirm(user=user, firm=firm, tier=1).save()
    opp = _opp(firm, url="https://example.com/new", first_seen=timezone.now())

    result = situation.build_situation(user)

    assert [e["opportunity_id"] for e in result["new_role_at_known_firm"]] == [opp.id]


def test_a_new_role_at_an_unknown_firm_is_not_reported():
    user = _user()
    firm = _firm()
    _opp(firm, url="https://example.com/old", first_seen=timezone.now() - timedelta(days=60))
    _opp(firm, url="https://example.com/new", first_seen=timezone.now())
    # No contact, no tier: this student has never heard of this firm.

    result = situation.build_situation(user)

    assert result["new_role_at_known_firm"] == []


def test_one_firms_posting_batch_does_not_crowd_out_every_other_firm():
    """Measured live: a single firm (CICC) posted three campus roles in one
    scrape, and all three of the Today page's card slots filled with the
    SAME firm — three cards that said nothing about the breadth of what
    actually moved. This event type exists to name WHICH firms have news,
    not to enumerate one firm's whole batch, so the result must cap at one
    posting per firm regardless of how many any single firm opened."""
    user = _user()
    busy_firm = _firm(name="CICC", slug="cicc")
    quiet_firm_a = _firm(name="Bank Alpha", slug="bank-alpha")
    quiet_firm_b = _firm(name="Bank Beta", slug="bank-beta")
    old = timezone.now() - timedelta(days=60)
    now = timezone.now()

    for firm in (busy_firm, quiet_firm_a, quiet_firm_b):
        _opp(firm, url=f"https://example.com/{firm.slug}/old", first_seen=old)
        UserFirm(user=user, firm=firm, tier=1).save()

    # The busy firm alone posts three roles in the window.
    busy_opps = [
        _opp(busy_firm, title=f"CICC Role {i}", url=f"https://example.com/cicc/new-{i}", first_seen=now)
        for i in range(3)
    ]
    quiet_a = _opp(quiet_firm_a, title="Alpha Role", url="https://example.com/bank-alpha/new", first_seen=now)
    quiet_b = _opp(quiet_firm_b, title="Beta Role", url="https://example.com/bank-beta/new", first_seen=now)

    result = situation.build_situation(user)

    firms_reported = [e["firm"] for e in result["new_role_at_known_firm"]]
    assert firms_reported.count("CICC") == 1, "one firm's batch must not eat every slot"
    assert "Bank Alpha" in firms_reported
    assert "Bank Beta" in firms_reported
    assert len(firms_reported) == len(set(firms_reported)), "every firm reported at most once"


def test_new_role_drops_the_wrong_market_and_the_wrong_rung():
    """The other two-thirds of the customer walk `role_matches_tracks` alone
    didn't fix: a Pune, India ops role and a full-time "New Associate"
    programme both reached a US/HK IB-track sophomore's advisor snapshot
    alongside the retail-branch case — right firm, wrong market, wrong rung
    of the ladder. A genuinely relevant IB summer analyst role at the same
    firm must still show. Same fixtures and assertions as
    `crm.tests.test_today.test_new_at_firms_drops_the_wrong_market_and_the_wrong_rung`,
    for the sibling surface."""
    user = _user()
    user.class_year = 2028
    user.regions = ["us", "hk"]
    user.tracks = ["ib"]
    user.target_cycles = ["2028 Summer Internship"]
    user.save()
    firm = _firm(name="Universal Bank", slug="universal-bank")
    UserFirm(user=user, firm=firm, tier=1).save()
    _opp(firm, url="https://example.com/universal-bank/old",
         first_seen=timezone.now() - timedelta(days=60))

    pune = Opportunity.objects.create(
        firm=firm, title="Investment Banking Off-Cycle Analyst",
        bucket="internship", status="open", region="other",
        url="https://example.com/universal-bank/pune")
    Opportunity.objects.filter(pk=pune.pk).update(first_seen=timezone.now())

    full_time = Opportunity.objects.create(
        firm=firm, title="Investment Banking Full-Time Analyst Program",
        bucket="entry_level", status="open", region="us",
        url="https://example.com/universal-bank/full-time")
    Opportunity.objects.filter(pk=full_time.pk).update(first_seen=timezone.now())

    relevant = Opportunity.objects.create(
        firm=firm, title="Investment Banking Summer Analyst Program",
        bucket="internship", cohort="2027", class_year_derived="2028",
        status="open", region="us",
        url="https://example.com/universal-bank/relevant")
    Opportunity.objects.filter(pk=relevant.pk).update(first_seen=timezone.now())

    result = situation.build_situation(user)

    reported_ids = {e["opportunity_id"] for e in result["new_role_at_known_firm"]}
    assert pune.id not in reported_ids, "wrong market must not read as news"
    assert full_time.id not in reported_ids, "wrong rung must not read as news"
    assert relevant.id in reported_ids, "the fix must not zero out a real match"


def test_a_boards_debut_week_does_not_flood_the_new_role_event():
    """A firm whose FIRST posting is itself inside the window just joined
    Coverage — every role it has would read as "new" for a reason that has
    nothing to do with the firm actually opening anything. Same fix
    crm.today._new_at_your_firms already made for the identical trap."""
    user = _user()
    firm = _firm()
    Contact(user=user, firm=firm, name="A Banker").save()
    _opp(firm, first_seen=timezone.now())  # the firm's ONLY posting, brand new

    result = situation.build_situation(user)

    assert result["new_role_at_known_firm"] == []


def test_a_dismissed_role_is_not_reported_as_new():
    user = _user()
    firm = _firm()
    _opp(firm, url="https://example.com/old", first_seen=timezone.now() - timedelta(days=60))
    Contact(user=user, firm=firm, name="A Banker").save()
    new_opp = _opp(firm, url="https://example.com/new", first_seen=timezone.now())
    UserOpportunity(user=user, opportunity=new_opp, dismissed=True).save()

    result = situation.build_situation(user)

    assert result["new_role_at_known_firm"] == []


def test_a_closed_new_role_is_not_reported():
    """`new_role_at_known_firm` is scoped to `status="open"` — a role that
    was posted and closed inside the same window is not upside."""
    user = _user()
    firm = _firm()
    _opp(firm, url="https://example.com/old", first_seen=timezone.now() - timedelta(days=60))
    Contact(user=user, firm=firm, name="A Banker").save()
    _opp(firm, url="https://example.com/new", status="closed", first_seen=timezone.now())

    result = situation.build_situation(user)

    assert result["new_role_at_known_firm"] == []


# ---------------------------------------------------------------------------
# The flat `events` list: priority order and the overall cap.
# ---------------------------------------------------------------------------
def test_events_are_ordered_role_closed_then_deadline_moved_then_new_role():
    """Stated once in the module docstring: a closed role wastes ongoing
    effort, a moved deadline risks a missed window, a new role is upside —
    in decreasing order of how much it costs the student to miss it."""
    user = _user()
    firm = _firm()
    Contact(user=user, firm=firm, name="A Banker").save()
    _opp(firm, url="https://example.com/old", first_seen=timezone.now() - timedelta(days=60))

    closed = _opp(firm, url="https://example.com/closed", status="closed")
    _track(user, closed)
    _change(closed, "status", "open", "closed")

    moved = _opp(firm, url="https://example.com/moved")
    _track(user, moved)
    _change(moved, "deadline", "2026-08-01", "2026-08-20")

    _opp(firm, url="https://example.com/new", first_seen=timezone.now())

    result = situation.build_situation(user)

    assert [e["kind"] for e in result["events"]] == [
        "role_closed", "deadline_moved", "new_role_at_known_firm",
    ]
