"""crm.digest: the weekly retention email's assembly, pinned against the
three engines it reuses rather than re-derives.

Every case here exists to catch exactly one failure mode: this module quietly
growing its own copy of a rule `directory.views` (closing-soon partitioning),
`crm.today` (the cadence queue) or `directory.recommend` (fit scoring)
already owns. See crm/digest.py's module docstring for the full rationale.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from analytics.models import UserOpportunity
from crm.digest import MAX_ACTIONS, assemble_digest
from crm.models import Contact, Touch, UserFirm
from directory.deadlines import CLOSING_SOON_DAYS
from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()
TODAY = timezone.localdate()


def _user(email="digest@example.com", **kw):
    return User.objects.create_user(email=email, password="pw12345!", **kw)


def _firm(name="Evercore", slug="evercore"):
    return Firm.objects.create(name=name, slug=slug)


def _opp(firm, n=1, *, days=None, bucket="internship", status="open"):
    """`status` is the POSTING's own (`Opportunity.status`, written by the
    nightly reverify pass when a firm takes a listing down) — not the
    student's funnel stage, which is `UserOpportunity.applied_status`."""
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{firm.slug}/{n}", title=f"Summer Analyst {n}",
        bucket=bucket, status=status,
        deadline=None if days is None else TODAY + timedelta(days=days),
    )


def _touch(user, contact, kind, *, days_ago=0):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel="email",
        ts=timezone.now() - timedelta(days=days_ago),
    )


# ---------------------------------------------------------------------------
# Nothing to report.
# ---------------------------------------------------------------------------

def test_a_user_with_nothing_due_gets_no_digest():
    user = _user()
    assert assemble_digest(user, today=TODAY) is None


def test_recommended_picks_alone_never_produce_a_digest():
    """The explicit contract: picks are a bonus on a real digest, never a
    reason to send one by themselves. A student with nothing closing and
    nothing to action, but a Tier 1 firm posting something fresh, still gets
    silence this week."""
    user = _user()
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _opp(firm, days=None)  # rolling, open, scores well against the Tier 1 target

    digest = assemble_digest(user, today=TODAY)
    assert digest is None


# ---------------------------------------------------------------------------
# Closing this week — reused from directory.views, not re-derived.
# ---------------------------------------------------------------------------

def test_a_tracked_role_inside_the_window_appears_once_correctly_labeled():
    """The overlap rule digest inherits from My Applications: a Saved role
    closing this week is ONE row, carrying its own funnel stage, never
    duplicated and never silently dropped for being "just Saved"."""
    user = _user()
    firm = _firm()
    o = _opp(firm, days=3)
    UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="saved")

    digest = assemble_digest(user, today=TODAY)
    assert digest is not None
    assert len(digest["closing"]) == 1
    item = digest["closing"][0]
    assert item["firm_name"] == "Evercore"
    assert item["stage_label"] == "Saved"
    assert item["days_left"] == 3


def test_the_window_boundary_matches_closing_soon_days_not_a_hardcoded_week():
    """Pinned against directory.deadlines.CLOSING_SOON_DAYS directly, not the
    literal 10: if that product constant ever moves, this test moves with it
    rather than silently falling out of step (the exact drift this module's
    docstring exists to prevent)."""
    user = _user()
    firm = _firm()
    inside = _opp(firm, n=1, days=CLOSING_SOON_DAYS - 1)
    outside = _opp(firm, n=2, days=CLOSING_SOON_DAYS + 1)
    UserOpportunity.all_objects.create(user=user, opportunity=inside, applied_status="saved")
    UserOpportunity.all_objects.create(user=user, opportunity=outside, applied_status="saved")

    digest = assemble_digest(user, today=TODAY)
    titles = {i["title"] for i in digest["closing"]}
    assert inside.title in titles
    assert outside.title not in titles


def test_a_done_role_never_appears_as_closing():
    """Mirrors My Applications' own rule: a finished application has no
    deadline urgency left, so Done rows are excluded from the lens."""
    user = _user()
    firm = _firm()
    o = _opp(firm, days=2)
    UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="closed")

    digest = assemble_digest(user, today=TODAY)
    assert digest is None


def test_a_dismissed_role_never_appears_as_closing():
    user = _user()
    firm = _firm()
    o = _opp(firm, days=2)
    UserOpportunity.all_objects.create(
        user=user, opportunity=o, applied_status="saved", dismissed=True
    )

    digest = assemble_digest(user, today=TODAY)
    assert digest is None


def test_closing_items_sort_soonest_first():
    user = _user()
    firm = _firm()
    later = _opp(firm, n=1, days=8)
    sooner = _opp(firm, n=2, days=1)
    UserOpportunity.all_objects.create(user=user, opportunity=later, applied_status="saved")
    UserOpportunity.all_objects.create(user=user, opportunity=sooner, applied_status="saved")

    digest = assemble_digest(user, today=TODAY)
    assert [i["title"] for i in digest["closing"]] == [sooner.title, later.title]


# ---------------------------------------------------------------------------
# Who to ping — crm.today's own queue, not a second cadence formula.
#
# `school_affiliation=True` on the fixtures below is exactly that reuse showing
# through: the queue gained a relevance gate (crm.relevance) and the digest
# inherits it, which is the point — an email that lists people the student does
# not target is the same failure as a page that does. The school tie is the
# cheapest way to be relevant, and none of these tests are about the gate.
# ---------------------------------------------------------------------------

def test_a_due_follow_up_shows_up_as_something_to_ping():
    user = _user(weekly_touch_goal=14)
    c = Contact.all_objects.create(user=user, name="Ada Lovelace", school_affiliation=True)
    _touch(user, c, "outreach", days_ago=20)  # long enough to clear follow_up's threshold

    digest = assemble_digest(user, today=TODAY)
    assert digest is not None
    names = [a["contact"]["name"] for a in digest["actions"]]
    assert "Ada Lovelace" in names


def test_park_never_shows_up_as_something_to_ping():
    """'park' is a bulk strip of contacts to stop chasing, never a thing to
    DO — crm/today.py's own `_today_class` puts it in its own class
    (`CLASS_PARK`) for exactly this reason, and the digest must honor that,
    not just Today's lane rendering."""
    user = _user(weekly_touch_goal=14)
    stale = Contact.all_objects.create(user=user, name="Gone Quiet", school_affiliation=True)
    _touch(user, stale, "outreach", days_ago=200)
    _touch(user, stale, "follow_up", days_ago=150)

    digest = assemble_digest(user, today=TODAY)
    if digest is not None:
        assert "Gone Quiet" not in [a["contact"]["name"] for a in digest["actions"]]


def test_actions_are_capped_with_an_honest_overflow_count():
    user = _user(weekly_touch_goal=14)
    for i in range(MAX_ACTIONS + 3):
        c = Contact.all_objects.create(user=user, name=f"Contact {i:02d}", school_affiliation=True)
        _touch(user, c, "outreach", days_ago=20)

    digest = assemble_digest(user, today=TODAY)
    assert len(digest["actions"]) == MAX_ACTIONS
    assert digest["actions_overflow"] == 3


def test_actions_carry_a_ready_compose_link():
    user = _user(weekly_touch_goal=14)
    c = Contact.all_objects.create(
        user=user, name="Ada Lovelace", email="ada@example.com",
        school_affiliation=True,
    )
    _touch(user, c, "outreach", days_ago=20)

    digest = assemble_digest(user, today=TODAY)
    action = next(a for a in digest["actions"] if a["contact"]["name"] == "Ada Lovelace")
    assert action["mailto"].startswith("https://mail.google.com/mail/?")


# ---------------------------------------------------------------------------
# New for you — directory.recommend, gated the same way its own bar is.
# ---------------------------------------------------------------------------

def test_an_empty_survey_profile_gets_no_picks_even_with_a_real_digest():
    user = _user()
    firm = _firm()
    o = _opp(firm, days=3)
    UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="saved")

    digest = assemble_digest(user, today=TODAY)
    assert digest is not None
    assert digest["picks"] == []


def test_a_tier_one_target_firm_surfaces_a_pick():
    user = _user()
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    pick_opp = _opp(firm, n=1)
    # Something due, so the digest itself is real (picks alone don't qualify).
    urgent_firm = _firm(name="Jefferies", slug="jefferies")
    urgent_opp = _opp(urgent_firm, n=2, days=2)
    UserOpportunity.all_objects.create(user=user, opportunity=urgent_opp, applied_status="saved")

    digest = assemble_digest(user, today=TODAY)
    assert digest is not None
    assert any(p["firm_name"] == "Evercore" and p["title"] == pick_opp.title
               for p in digest["picks"])


def test_an_already_tracked_role_is_never_recommended_as_new():
    """A role the student already tracks (or dismissed) is not news — the
    digest must not point at what it already knows the student has acted
    on. Pinned against a firm with TWO roles so the assertion can't pass by
    the tracked role simply having no candidates left at all."""
    user = _user()
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    tracked = _opp(firm, n=1)
    untracked = _opp(firm, n=2)
    UserOpportunity.all_objects.create(user=user, opportunity=tracked, applied_status="saved")
    urgent_firm = _firm(name="Jefferies", slug="jefferies")
    urgent_opp = _opp(urgent_firm, n=3, days=2)
    UserOpportunity.all_objects.create(user=user, opportunity=urgent_opp, applied_status="saved")

    digest = assemble_digest(user, today=TODAY)
    pick_titles = [p["title"] for p in digest["picks"] if p["firm_name"] == "Evercore"]
    assert untracked.title in pick_titles
    assert tracked.title not in pick_titles


# ---------------------------------------------------------------------------
# "Closing this week" must mean a role that can still be applied to.
#
# The incident: _closing_this_week partitioned on the student's own Done
# marking and never on `Opportunity.status`, so a posting the firm had already
# taken down was advertised in the retention email as closing this week — the
# digest telling a student to hurry toward something already gone.
# ---------------------------------------------------------------------------

def test_a_posting_the_firm_closed_is_not_advertised_as_closing_this_week():
    user = _user()
    o = _opp(_firm(), days=3, status="closed")
    UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="saved")

    assert assemble_digest(user, today=TODAY) is None


def test_a_closed_posting_is_dropped_from_the_digest_at_every_funnel_stage():
    """A student who submitted still cares about the role, but "closing this
    week" is a claim about the deadline, and the deadline stopped meaning
    anything when the firm pulled the posting."""
    user = _user()
    firm = _firm()
    for n, stage in enumerate(("submitted", "interview", "offer"), start=1):
        o = _opp(firm, n=n, days=3, status="closed")
        UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status=stage)

    assert assemble_digest(user, today=TODAY) is None


def test_a_posting_the_scraper_never_rechecked_still_closes_this_week():
    """The over-filtering guard. `Opportunity.status` defaults to "" and most
    rows have never been reverified, so the rule has to be `== "closed"`, not
    `!= "open"` — otherwise the digest empties itself for nearly everyone."""
    user = _user()
    o = _opp(_firm(), days=3, status="")
    UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="saved")

    digest = assemble_digest(user, today=TODAY)
    assert digest is not None
    assert len(digest["closing"]) == 1


def test_an_open_role_beside_a_closed_one_still_reaches_the_digest():
    """One dead row must not suppress the live rows next to it."""
    user = _user()
    firm = _firm()
    dead = _opp(firm, n=1, days=3, status="closed")
    live = _opp(firm, n=2, days=4, status="open")
    for o in (dead, live):
        UserOpportunity.all_objects.create(user=user, opportunity=o, applied_status="saved")

    digest = assemble_digest(user, today=TODAY)
    assert digest is not None
    assert [i["title"] for i in digest["closing"]] == [live.title]


# ---------------------------------------------------------------------------
# The daily email must not lead with the cards the queue has just deferred.
#
# `_gate_and_rank` marks an action `firm_paced` once its firm has had its
# day's sends, and rewrites the reason to "... already has 2 today, so this
# one is better tomorrow". `_who_to_ping` sorted purely by `_today_sort_key`,
# which is blind to that flag — measured on the founder's live queue, 6 of the
# 8 cards the digest chose were paced while sendable ones sat in a 36-card
# overflow. An email whose top half says "not today" is not a worse ordering
# of the same information; it is the wrong information.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_digest_prefers_contacts_that_are_actually_sendable_today():
    from crm.digest import _who_to_ping

    user = get_user_model().objects.create_user(
        email="paced@x.test", password="pw12345!")
    crowded = Firm.objects.create(slug="crowded-bank", name="Crowded Bank")
    quiet = Firm.objects.create(slug="quiet-bank", name="Quiet Bank")
    UserFirm.all_objects.create(user=user, firm=crowded, tier=1, status="target")
    UserFirm.all_objects.create(user=user, firm=quiet, tier=1, status="target")

    long_ago = timezone.now() - timedelta(days=40)
    # Four at one firm: the cap lets two through and paces the rest.
    for i in range(4):
        c = Contact.all_objects.create(user=user, name=f"Crowded {i}",
                                       firm=crowded, email=f"c{i}@crowded.test")
        Touch.all_objects.create(user=user, contact=c, kind="email", ts=long_ago)
    # One at a firm with room. It is NEWER, so a flag-blind sort ranks it last.
    c = Contact.all_objects.create(user=user, name="Quiet One", firm=quiet,
                                   email="q@quiet.test")
    Touch.all_objects.create(user=user, contact=c, kind="email",
                             ts=timezone.now() - timedelta(days=30))

    shown, _overflow = _who_to_ping(user)
    assert shown, "fixture should produce a queue"
    paced = [a for a in shown if a.get("firm_paced")]
    unpaced = [a for a in shown if not a.get("firm_paced")]
    # Every sendable card outranks every deferred one.
    assert shown[:len(unpaced)] == unpaced, (
        "a firm-paced action was ordered above one the student can still send "
        "today; the digest would lead with 'better tomorrow'"
    )
    if paced:
        assert shown.index(paced[0]) >= len(unpaced)
