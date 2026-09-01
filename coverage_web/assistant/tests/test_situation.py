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
    nothing to do with the firm actually opening anything. Same fix the
    retired crm.today._new_at_your_firms used to make for the identical
    trap."""
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

    # The new role belongs to a SECOND known firm, and the fixture is
    # poorer for it having ever been otherwise. `_new_role_events` keeps
    # one role per firm and picks the firm's newest, so when every role
    # here shared a firm that slot was won by `moved` — and this test
    # passed while asserting a three-card list whose second and third
    # cards were the same role, said two different ways. That is the
    # duplicate the flat list now refuses, so the fixture has to stop
    # depending on it to produce a third kind.
    other = _firm(name="Morgan Stanley", slug="morgan-stanley")
    Contact(user=user, firm=other, name="Another Banker").save()
    _opp(other, url="https://example.com/other-old",
         first_seen=timezone.now() - timedelta(days=60))
    _opp(other, url="https://example.com/new", first_seen=timezone.now())

    result = situation.build_situation(user)

    assert [e["kind"] for e in result["events"]] == [
        "role_closed", "deadline_moved", "new_role_at_known_firm",
    ]
    ids = [e["opportunity_id"] for e in result["events"]]
    assert len(ids) == len(set(ids)), "the ordering fixture is reporting one role twice"


# ---------------------------------------------------------------------------
# One card per role. Reported directly, with a screenshot of two cards side by
# side for the same Bank of America forum: one saying it had closed and would
# not accept applications, the other saying its deadline had moved to a date
# nine days out. Each helper deduped inside its own kind and none could see
# the others, so a role changing in two ways surfaced twice, contradicting
# itself.
# ---------------------------------------------------------------------------

def test_a_role_that_closed_and_moved_its_deadline_reports_once_as_closed():
    """The exact shipped bug. Both rows were real; together they were
    nonsense. Closed is the terminal fact and wins."""
    user = _user()
    firm = _firm(name="Bank of America", slug="bank-of-america")
    opp = _opp(
        firm,
        title="Campus Insight Forum: The Power to Lead - Fall 2026",
        status="closed",
    )
    _track(user, opp)
    _change(opp, "status", "open", "closed")
    _change(opp, "deadline", "2026-08-21", "2026-08-31")

    result = situation.build_situation(user)

    kinds = [e["kind"] for e in result["events"]]
    assert kinds == ["role_closed"], (
        f"one role produced {len(kinds)} cards ({kinds}). A student saw the "
        "same forum reported as closed and as having moved its deadline into "
        "the future, side by side."
    )
    ids = [e["opportunity_id"] for e in result["events"]]
    assert len(ids) == len(set(ids)), "the flat events list repeats a role"


def test_a_moved_deadline_on_a_closed_posting_is_not_reported_at_all():
    """Fixed at the source too, not only at the merge: a deadline is a
    promise about a window still open, and on a dead posting it is a stale
    row the scraper has already overtaken. So it is absent from the
    per-kind list as well, which the merge alone would not have done."""
    user = _user()
    opp = _opp(_firm(), status="closed")
    _track(user, opp)
    _change(opp, "deadline", "2026-08-21", "2026-08-31")

    result = situation.build_situation(user)

    assert result["deadline_moved"] == [], (
        "a closed posting is still advertising a moved deadline."
    )


def test_a_moved_deadline_on_a_still_open_posting_is_untouched():
    """The guard is about DEAD postings only. An open role that moved its
    deadline is the whole point of the event type and must still report."""
    user = _user()
    opp = _opp(_firm(), status="open")
    _track(user, opp)
    _change(opp, "deadline", "2026-08-21", "2026-08-31")

    result = situation.build_situation(user)

    assert len(result["deadline_moved"]) == 1
    assert result["deadline_moved"][0]["opportunity_id"] == opp.id


def test_the_per_kind_lists_are_not_pruned_by_the_merge():
    """Only the flat `events` list is deduplicated. A caller asking for
    `role_closed` wants every close, not the ones that survived a merge."""
    user = _user()
    firm = _firm()
    closed_and_moved = _opp(firm, url="https://example.com/both", status="closed")
    _track(user, closed_and_moved)
    _change(closed_and_moved, "status", "open", "closed")

    other_closed = _opp(firm, url="https://example.com/other", status="closed")
    _track(user, other_closed)
    _change(other_closed, "status", "open", "closed")

    result = situation.build_situation(user)

    assert len(result["role_closed"]) == 2
    assert len(result["events"]) == 2


# ---------------------------------------------------------------------------
# _display_location: a "new role" card must never repeat itself.
# ---------------------------------------------------------------------------
LOCATION_CASES = [
    # Title already names the city -- suppress the whole clause rather
    # than repeat it (a real posting on the founder's own board: "...
    # Summer Associate - Houston" followed by "Houston, Texas, ...").
    (
        "2027 Capital Markets, Global Investment Banking Summer Associate - Houston",
        "Houston, Texas, United States of America",
        "",
    ),
    # The location field repeats a segment against itself under two
    # spellings (also real: Hong Kong postings emit "HONG KONG, Hong
    # Kong"). Collapsed by casefolded comparison, not a hardcoded name.
    (
        "Global Markets Trainee - FX & Rates Macro Financial Institution Sales",
        "HONG KONG, Hong Kong",
        "HONG KONG",
    ),
    # No location at all: stays empty, never invented.
    ("Private Capital Markets - Internship - New York", "", ""),
    # Neither duplication applies: passes through untouched.
    ("Some Title", "Singapore, Singapore", "Singapore"),
    ("Some Title", "London, United Kingdom", "London, United Kingdom"),
]


@pytest.mark.parametrize("title,location,expected", LOCATION_CASES)
def test_display_location_never_repeats_itself(title, location, expected):
    assert situation._display_location(title, location) == expected


# ---------------------------------------------------------------------------
# The claim in the module docstring, pinned in code.
#
# "pure code, no model" is the load-bearing promise of this whole module —
# a moved deadline is the one thing on the Today page that must never be
# allowed to hallucinate urgency, and the strip's cards are built straight
# from these typed fields (templates/crm/week.html). A docstring cannot
# enforce that; this can. If a future change genuinely needs a model here,
# the fix is a different module, not an exemption.
# ---------------------------------------------------------------------------
def test_the_situation_module_never_reaches_for_a_model():
    import re
    from pathlib import Path

    source = (Path(situation.__file__)).read_text()
    banned = re.compile(r"\banthropic\b|get_client|is_configured|messages\.(create|stream)")
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(source.splitlines(), start=1)
        if banned.search(line)
    ]
    assert not offenders, "situation.py must stay model-free:\n" + "\n".join(offenders)


def test_every_field_a_situation_card_renders_comes_from_a_typed_row():
    """The three card shapes in templates/crm/week.html read exactly these
    keys. Each one is a column or a derived value, never a sentence — so
    there is nothing on the strip for a model to have written."""
    user = _user()
    firm = _firm()
    tracked = _opp(firm, title="2028 Summer Analyst", deadline=timezone.localdate())
    UserOpportunity(user=user, opportunity=tracked).save()
    OpportunityChange.objects.create(
        opportunity=tracked, field="deadline",
        old_value="2026-09-01", new_value="2026-09-20",
        observed_at=timezone.now(),
    )

    event = situation.build_situation(user)["deadline_moved"][0]

    assert event["firm"] == firm.name
    assert event["title"] == tracked.title
    assert event["old_value"] == "2026-09-01"
    assert event["new_value"] == "2026-09-20"
    # Dates as real dates, so the template formats them rather than a
    # sentence being written about them anywhere.
    assert event["old_date"].isoformat() == "2026-09-01"
    assert event["new_date"].isoformat() == "2026-09-20"


# ---------------------------------------------------------------------------
# A moved deadline says what KIND of date it now is.
#
# Measured 2026-09-01: 354 of 394 `deadline_moved` change rows in the last 30
# days were on prose-read deadlines (confidence 0.6), 36 on stated ones. The
# event carried old/new and nothing else, so the advisor and the daily brief
# were reporting our own regex re-reading a page as the firm moving a date.
# ---------------------------------------------------------------------------
def _moved(user, *, confidence):
    firm = _firm(name=f"Bank {confidence}", slug=f"bank-{confidence}")
    opp = _opp(firm, deadline=timezone.localdate() + timedelta(days=10))
    Opportunity.objects.filter(pk=opp.pk).update(confidence=confidence)
    opp.refresh_from_db()
    _track(user, opp)
    _change(opp, "deadline", "2026-09-01", "2026-09-20")
    return opp


def test_a_moved_deadline_on_a_prose_read_date_is_marked_reported():
    user = _user()
    _moved(user, confidence=0.6)

    event = situation.build_situation(user)["deadline_moved"][0]

    assert event["deadline_source"] == "reported"


def test_a_moved_deadline_on_a_board_published_date_is_marked_stated():
    user = _user()
    _moved(user, confidence=1.0)

    event = situation.build_situation(user)["deadline_moved"][0]

    assert event["deadline_source"] == "stated"


def test_the_situation_and_the_tools_agree_on_a_deadlines_provenance():
    """`situation._deadline_source` restates `tools._deadline_source` (the
    import would be circular). Pinned against each other so the advisor's
    search rows and its situation rows can never call the same date two
    different things."""
    from assistant import tools

    firm = _firm()
    for confidence, expected in ((0.6, "reported"), (1.0, "stated")):
        opp = _opp(firm, title=f"Role {confidence}", deadline=timezone.localdate())
        Opportunity.objects.filter(pk=opp.pk).update(confidence=confidence)
        opp.refresh_from_db()
        assert situation._deadline_source(opp) == tools._deadline_source(opp) == expected
    undated = _opp(firm, title="Undated", deadline=None)
    assert situation._deadline_source(undated) is None
    assert tools._deadline_source(undated) is None


# ---------------------------------------------------------------------------
# The level a posting's TITLE names, for a student who is (or has not said
# they are not) an undergraduate.
#
# `role_matches_level` reads bucket and derived class year; a "PhD Summer
# Intern" and an "IB Summer Associate" clear both. Two of the founder's four
# new-role events on 2026-09-01 were exactly those. `directory.recommend.
# role_level(title)` is being added alongside this and is imported guarded,
# so both branches are pinned: with it, those rows are not news for an
# undergrad; without it, nothing changes.
# ---------------------------------------------------------------------------
def _fake_role_level(title: str) -> str:
    t = title.lower()
    if "phd" in t:
        return "phd"
    if "mba" in t:
        return "mba"
    if "associate" in t:
        return "experienced"
    return "undergrad"


def _known_firm_with_three_new_roles(user):
    """One tiered firm (past its board debut) that posted, newest first, a
    PhD internship, an Associate internship, and an Analyst internship this
    week. Newest-first matters: `_new_role_events` keeps ONE role per firm,
    the most recent, so without the level filter the PhD row is the one the
    student is told about."""
    firm = _firm(name="Quant Capital", slug="quant-capital")
    UserFirm(user=user, firm=firm, tier=1).save()
    _opp(firm, title="Old Posting", url="https://example.com/quant/old",
         first_seen=timezone.now() - timedelta(days=60))
    now = timezone.now()
    analyst = _opp(firm, title="Summer Analyst, Trading",
                   url="https://example.com/quant/analyst",
                   first_seen=now - timedelta(hours=3))
    associate = _opp(firm, title="IB Summer Associate",
                     url="https://example.com/quant/associate",
                     first_seen=now - timedelta(hours=2))
    phd = _opp(firm, title="PhD Summer Intern – Quantitative Research",
               url="https://example.com/quant/phd",
               first_seen=now - timedelta(hours=1))
    return analyst, associate, phd


def test_with_a_role_level_reader_an_undergrad_is_not_told_about_phd_or_associate_roles(monkeypatch):
    monkeypatch.setattr(situation, "_role_level", _fake_role_level)
    user = _user()
    analyst, associate, phd = _known_firm_with_three_new_roles(user)

    ids = [e["opportunity_id"] for e in situation.build_situation(user)["new_role_at_known_firm"]]

    assert phd.id not in ids, "a PhD internship is not news for an undergraduate"
    assert associate.id not in ids, "an Associate programme is not news for an undergraduate"
    assert ids == [analyst.id], "the filter must not zero out the real match"


def test_a_student_who_stated_a_higher_level_keeps_those_postings(monkeypatch):
    """`study_level` is a column being added alongside this. A student who
    has SAID they are a PhD gets PhD postings — the filter only ever acts
    on blank-or-undergraduate, never on a stated higher level."""
    monkeypatch.setattr(situation, "_role_level", _fake_role_level)
    user = _user()
    user.study_level = "phd"
    _analyst, _associate, phd = _known_firm_with_three_new_roles(user)

    ids = [e["opportunity_id"] for e in situation.build_situation(user)["new_role_at_known_firm"]]

    assert ids == [phd.id]


def test_without_a_role_level_reader_behaviour_is_unchanged(monkeypatch):
    """The import is guarded; until `role_level` lands, the newest posting
    at the firm is reported exactly as before, PhD or not."""
    monkeypatch.setattr(situation, "_role_level", None)
    user = _user()
    _analyst, _associate, phd = _known_firm_with_three_new_roles(user)

    ids = [e["opportunity_id"] for e in situation.build_situation(user)["new_role_at_known_firm"]]

    assert ids == [phd.id]


def test_a_misbehaving_role_level_reader_costs_the_filter_never_the_strip(monkeypatch):
    """A signature change in the sibling module must degrade to "no level
    filter", not to `build_situation`'s empty-everything fallback."""
    def boom(*_a, **_k):
        raise TypeError("role_level() takes 2 positional arguments")

    monkeypatch.setattr(situation, "_role_level", boom)
    user = _user()
    _analyst, _associate, phd = _known_firm_with_three_new_roles(user)

    result = situation.build_situation(user)

    assert [e["opportunity_id"] for e in result["new_role_at_known_firm"]] == [phd.id]


def test_drop_advanced_levels_reads_the_level_case_insensitively(monkeypatch):
    monkeypatch.setattr(situation, "_role_level", lambda title: "PhD" if "PhD" in title else "Undergrad")
    user = _user()

    class Row:
        def __init__(self, title):
            self.title = title

    rows = [Row("PhD Intern"), Row("Summer Analyst")]
    kept = situation._drop_advanced_levels(rows, user)

    assert [r.title for r in kept] == ["Summer Analyst"]
