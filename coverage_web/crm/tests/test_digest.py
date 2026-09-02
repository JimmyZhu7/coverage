"""crm.digest: the weekly retention email's assembly, pinned against the
three engines it reuses rather than re-derives.

Every case here exists to catch exactly one failure mode: this module quietly
growing its own copy of a rule `directory.views` (closing-soon partitioning),
`crm.today` (the cadence queue) or `directory.recommend` (fit scoring)
already owns. See crm/digest.py's module docstring for the full rationale.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from analytics.models import UserOpportunity
from crm.digest import (
    MAX_ACTIONS, MIN_NEW_PICKS, MODE_BEST, MODE_LINES, MODE_NEW,
    NEW_WINDOW_DAYS, assemble_digest,
)
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
# A weekly email does not apply a daily cap.
#
# REWRITTEN 2026-09-02, and the reason is worth stating because the old test
# was not wrong so much as aimed one step short. It pinned "firm-paced cards
# sort last" (`3c9227f`), which was the right fix for the ordering and left
# the real defect standing: a paced card still PRINTS, and what it prints is
# "Crowded Bank already has 2 today, so this one is better tomorrow" — a
# sentence about a single day, in an email read across a week, with
# `sent_today` behind it counted against digest morning
# (`audit-personalization-networking.md` D5). On the founder's queue it
# stayed out of the email only because 8 unpaced cards happened to exist.
#
# So the digest now runs the queue with `pace=False` and applies its own
# weekly ceiling instead. The old assertion is subsumed: nothing is paced, so
# no paced card can lead.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_weekly_digest_never_paces_a_card_by_the_day():
    from crm.digest import _who_to_ping

    user = get_user_model().objects.create_user(
        email="paced@x.test", password="pw12345!")
    crowded = Firm.objects.create(slug="crowded-bank", name="Crowded Bank")
    quiet = Firm.objects.create(slug="quiet-bank", name="Quiet Bank")
    UserFirm.all_objects.create(user=user, firm=crowded, tier=1, status="target")
    UserFirm.all_objects.create(user=user, firm=quiet, tier=1, status="target")

    long_ago = timezone.now() - timedelta(days=40)
    # Four at one firm — enough that the DAILY cap of 2 would have paced two
    # of them on Today's page. It must not pace them here.
    for i in range(4):
        c = Contact.all_objects.create(user=user, name=f"Crowded {i}",
                                       firm=crowded, email=f"c{i}@crowded.test")
        Touch.all_objects.create(user=user, contact=c, kind="email", ts=long_ago)
    c = Contact.all_objects.create(user=user, name="Quiet One", firm=quiet,
                                   email="q@quiet.test")
    Touch.all_objects.create(user=user, contact=c, kind="email",
                             ts=timezone.now() - timedelta(days=30))

    shown, _overflow = _who_to_ping(user)
    assert shown, "fixture should produce a queue"
    assert not [a for a in shown if a.get("firm_paced")]
    for a in shown:
        assert "better tomorrow" not in (a.get("reason") or "")

    # And Today itself is untouched: the same account, the same queue, the
    # daily cap still applied.
    from crm.today import _build_actions

    today_actions, _ = _build_actions(user)
    assert [a for a in today_actions if a.get("firm_paced")], (
        "the daily cap must still fire on Today's own queue; only the weekly "
        "email opts out"
    )


@pytest.mark.django_db
def test_no_rendered_reason_in_the_digest_talks_about_today():
    """The acceptance criterion, read literally: a weekly email may not say
    "today" or "better tomorrow" in the sentence under a name."""
    from crm.digest import _who_to_ping

    user = get_user_model().objects.create_user(
        email="words@x.test", password="pw12345!")
    firm = Firm.objects.create(slug="wordy-bank", name="Wordy Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1, status="target")
    long_ago = timezone.now() - timedelta(days=40)
    for i in range(6):
        c = Contact.all_objects.create(user=user, name=f"W {i}", firm=firm,
                                       email=f"w{i}@wordy.test")
        Touch.all_objects.create(user=user, contact=c, kind="email", ts=long_ago)

    shown, _ = _who_to_ping(user)
    assert shown
    for a in shown:
        reason = (a.get("reason") or "").lower()
        assert "today" not in reason
        assert "better tomorrow" not in reason


@pytest.mark.django_db
def test_the_weekly_list_caps_one_firm_at_the_weekly_budget(monkeypatch):
    """Twelve cards at one firm, and the email may name at most ten of them.

    `MAX_ACTIONS` is raised for the duration: at its shipped value of 8 the
    ceiling of 10 cannot bind, and the two numbers are independent — "how
    long may this email be" and "how many people at one bank may it name".
    This test is what stops the second one going missing the day the first
    one moves.
    """
    from crm import digest as digest_mod
    from crm.today import FIRM_DAILY_CONTACT_CAP

    monkeypatch.setattr(digest_mod, "MAX_ACTIONS", 20)

    user = get_user_model().objects.create_user(
        email="twelve@x.test", password="pw12345!")
    firm = Firm.objects.create(slug="one-bank", name="One Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1, status="target")
    long_ago = timezone.now() - timedelta(days=40)
    for i in range(12):
        c = Contact.all_objects.create(user=user, name=f"One {i}", firm=firm,
                                       email=f"o{i}@one.test")
        Touch.all_objects.create(user=user, contact=c, kind="email", ts=long_ago)

    shown, overflow = digest_mod._who_to_ping(user)
    at_firm = [a for a in shown if a.get("firm_name") == "One Bank"]
    assert len(at_firm) <= FIRM_DAILY_CONTACT_CAP * 5 == 10
    assert len(at_firm) == 10
    # Trimmed, never silently dropped: the rest are counted.
    assert overflow == 2


@pytest.mark.django_db
def test_the_weekly_budget_changes_nothing_below_its_ceiling():
    """P3. Nine cards at one firm is the common case and the list is exactly
    what `_today_sort_key` orders, unabridged."""
    from crm.digest import _who_to_ping
    from crm.today import _build_actions, _today_class, _today_sort_key, CLASS_PARK

    user = get_user_model().objects.create_user(
        email="below@x.test", password="pw12345!")
    firm = Firm.objects.create(slug="small-bank", name="Small Bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1, status="target")
    long_ago = timezone.now() - timedelta(days=40)
    for i in range(5):
        c = Contact.all_objects.create(user=user, name=f"S {i}", firm=firm,
                                       email=f"s{i}@small.test")
        Touch.all_objects.create(user=user, contact=c, kind="email", ts=long_ago)

    raw, _ = _build_actions(user, pace=False)
    expected = sorted([a for a in raw if _today_class(a) != CLASS_PARK],
                      key=_today_sort_key)[:MAX_ACTIONS]
    shown, _overflow = _who_to_ping(user)
    assert [a["contact"]["id"] for a in shown] == [
        a["contact"]["id"] for a in expected
    ]


# ---------------------------------------------------------------------------
# The weekly email does not go out inside the December blackout.
#
# `crm.today.outreach_blackout` holds Today to confirmed deadlines from Dec 20
# to Jan 2 (two practitioners, Dec 2025: "Anyone sending emails right now is
# on my shit list ... Wait until first week of January"). An email headed "who
# to ping" landing on Dec 24 is the product doing the one thing the page is
# telling the student not to, so `assemble_digest` returns None, the existing
# "skip this user" signal, on every day of the window. Runs on the real
# calendar (`outreach_blackout` marker, see coverage_web/conftest.py).
# ---------------------------------------------------------------------------

@pytest.mark.outreach_blackout
def test_the_weekly_digest_stays_silent_inside_the_holiday_window():
    user = _user(weekly_touch_goal=14)
    c = Contact.all_objects.create(user=user, name="Ada Lovelace", school_affiliation=True)
    _touch(user, c, "outreach", days_ago=20)  # a real thing to ping, on any day

    assert assemble_digest(user, today=date(2026, 12, 19)) is not None, (
        "the Saturday before the window: weekends do not gate the email"
    )
    assert assemble_digest(user, today=date(2026, 12, 20)) is None
    assert assemble_digest(user, today=date(2026, 12, 24)) is None
    assert assemble_digest(user, today=date(2027, 1, 2)) is None
    assert assemble_digest(user, today=date(2027, 1, 4)) is not None
# The line the digest leads with: advocates in place, firms with nobody.
#
# Computed through `crm.coverage.rank_gaps` from the same counts the Network
# board's Coverage Gaps strip ranks, so the firms named are the ones that
# strip would put first: tier 1 before tier 2, more open roles before fewer,
# then name.
# ---------------------------------------------------------------------------
def _digest_with_something_due(user):
    urgent_firm = _firm(name="Urgent Bank", slug="urgent-bank")
    urgent = _opp(urgent_firm, n=99, days=2)
    UserOpportunity.all_objects.create(user=user, opportunity=urgent, applied_status="saved")


def test_the_digest_leads_with_advocates_and_zero_contact_firms():
    user = _user()
    alpha = _firm("Alpha Bank", "alpha")
    beta = _firm("Beta Bank", "beta")
    gamma = _firm("Gamma Bank", "gamma")
    delta = _firm("Delta Bank", "delta")
    for firm, tier in ((alpha, 1), (beta, 1), (gamma, 2), (delta, 1)):
        UserFirm.all_objects.create(user=user, firm=firm, tier=tier)
    # Two open roles at Beta: among equally-exposed tier-1 firms with nobody,
    # the strip ranks the one with more seats first.
    _opp(beta, n=1)
    _opp(beta, n=2)
    Contact.all_objects.create(user=user, name="Ada Advocate", firm=delta, warmth="advocate")
    Contact.all_objects.create(user=user, name="Cold Call", firm=delta, warmth="cold")
    _digest_with_something_due(user)

    digest = assemble_digest(user, today=TODAY)

    assert digest["coverage"] == {
        "advocates": 1,
        "advocates_elsewhere": 0,
        "firms": 4,
        "no_contact": 3,
        "named": ["Beta Bank", "Alpha Bank", "Gamma Bank"],
        "line": ("1 advocate across 4 target firms · 3 firms with no contact "
                 "yet: Beta Bank, Alpha Bank and Gamma Bank"),
    }


def test_advocates_outside_the_target_firms_are_counted_aside_not_as_zero():
    """The founder's own case: both advocates carry `firm_text="usc"` and no
    linked firm, so the target-firm count is honestly 0 — and a student who
    knows they have two advocates reads a bare "0 advocates" as a bug."""
    user = _user()
    UserFirm.all_objects.create(user=user, firm=_firm(), tier=1)
    Contact.all_objects.create(user=user, name="Yumna", firm_text="usc", warmth="advocate")
    Contact.all_objects.create(user=user, name="Jeffrey", firm_text="usc", warmth="advocate")
    _digest_with_something_due(user)

    coverage = assemble_digest(user, today=TODAY)["coverage"]

    assert coverage["advocates"] == 0
    assert coverage["advocates_elsewhere"] == 2
    assert coverage["line"].startswith("0 advocates across 1 target firm (2 elsewhere) · ")


def test_more_than_three_zero_contact_firms_are_counted_and_the_worst_three_named():
    user = _user()
    for i in range(5):
        UserFirm.all_objects.create(
            user=user, firm=_firm(f"Bank {i}", f"bank-{i}"), tier=1)
    _digest_with_something_due(user)

    coverage = assemble_digest(user, today=TODAY)["coverage"]

    assert coverage["no_contact"] == 5
    assert coverage["named"] == ["Bank 0", "Bank 1", "Bank 2"]
    assert coverage["line"] == (
        "0 advocates across 5 target firms · 5 firms with no contact yet, "
        "starting with Bank 0, Bank 1 and Bank 2"
    )


def test_a_tier_one_firm_with_nobody_is_named_before_a_tier_two_one():
    user = _user()
    UserFirm.all_objects.create(user=user, firm=_firm("Second Tier", "second"), tier=2)
    UserFirm.all_objects.create(user=user, firm=_firm("Top Tier", "top"), tier=1)
    _digest_with_something_due(user)

    assert assemble_digest(user, today=TODAY)["coverage"]["named"] == ["Top Tier", "Second Tier"]


def test_a_fully_covered_portfolio_says_so_instead_of_counting_zero():
    user = _user()
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    Contact.all_objects.create(user=user, name="Someone", firm=firm, warmth="cold")
    _digest_with_something_due(user)

    line = assemble_digest(user, today=TODAY)["coverage"]["line"]

    assert line == "0 advocates across 1 target firm · a contact at every one"


def test_a_student_with_no_tiered_firms_gets_no_coverage_line():
    user = _user()
    _digest_with_something_due(user)

    assert assemble_digest(user, today=TODAY)["coverage"] == {}


def test_an_archived_contact_does_not_cover_a_firm():
    user = _user()
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    Contact.all_objects.create(user=user, name="Gone", firm=firm, warmth="advocate", archived=True)
    _digest_with_something_due(user)

    coverage = assemble_digest(user, today=TODAY)["coverage"]

    assert coverage["advocates"] == 0
    assert coverage["named"] == ["Evercore"]


# ---------------------------------------------------------------------------
# "New for you" says when every pick is for a cycle the student is NOT in.
#
# The founder's four picks all carried a "2027 intake" chip whose tooltip
# said "not a fit", printed in the digest as if it were a reason to apply. The
# note is derived from each pick's own bucket + intake year against
# `User.target_cycles`, never from the chip text another surface owns.
# ---------------------------------------------------------------------------
def _pick_opp(firm, *, cohort, n=1, days=None, confidence=0.0):
    return Opportunity.objects.create(
        firm=firm, url=f"https://x/{firm.slug}/pick-{n}", title=f"Summer Analyst {n}",
        bucket="internship", status="open", cohort=cohort, confidence=confidence,
        deadline=None if days is None else TODAY + timedelta(days=days),
    )


def test_picks_all_a_year_early_get_one_honest_line_about_the_cycle():
    user = _user(target_cycles=["2028 Summer Internship"])
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _pick_opp(firm, cohort="2027")
    _digest_with_something_due(user)

    digest = assemble_digest(user, today=TODAY)

    assert digest["picks"], "fixture should have produced a pick"
    assert digest["picks_note"] == (
        "Nothing yet for your 2028 Summer Internship cycle; these are a year early"
    )


def test_a_pick_for_the_declared_cycle_silences_the_note():
    user = _user(target_cycles=["2028 Summer Internship"])
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _pick_opp(firm, cohort="2028", n=1)
    _pick_opp(firm, cohort="2027", n=2)
    _digest_with_something_due(user)

    digest = assemble_digest(user, today=TODAY)

    assert len(digest["picks"]) == 2
    assert digest["picks_note"] == ""


def test_a_pick_with_no_intake_year_gets_the_bare_sentence():
    """Nothing about its timing is known, so nothing about its timing is
    claimed — not "a year early", not "other intakes"."""
    user = _user(target_cycles=["2028 Summer Internship"])
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _pick_opp(firm, cohort="")
    _digest_with_something_due(user)

    assert assemble_digest(user, today=TODAY)["picks_note"] == (
        "Nothing yet for your 2028 Summer Internship cycle"
    )


def test_picks_from_intakes_further_back_are_called_earlier_not_a_year_early():
    user = _user(target_cycles=["2028 Summer Internship"])
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _pick_opp(firm, cohort="2026")
    _digest_with_something_due(user)

    assert assemble_digest(user, today=TODAY)["picks_note"].endswith("; these are earlier intakes")


def test_an_insight_week_beside_a_year_early_internship_is_not_called_early():
    """The founder's live picks: three 2027 summer internships and one 2027
    insight programme against a 2028 Summer Internship cycle. The insight
    week is a different programme, not an early one, so the suffix names the
    roles it is actually about."""
    user = _user(target_cycles=["2028 Summer Internship"])
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _pick_opp(firm, cohort="2027", n=1)
    Opportunity.objects.create(
        firm=firm, url=f"https://x/{firm.slug}/insight", title="Discover Programme",
        bucket="insight", status="open", cohort="2027",
    )
    _digest_with_something_due(user)

    digest = assemble_digest(user, today=TODAY)

    assert len(digest["picks"]) == 2
    assert digest["picks_note"] == (
        "Nothing yet for your 2028 Summer Internship cycle; "
        "the summer internship roles here are a year early"
    )


def test_picks_in_a_bucket_the_student_did_not_declare_get_the_bare_sentence():
    user = _user(target_cycles=["2028 Summer Internship"])
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    Opportunity.objects.create(
        firm=firm, url=f"https://x/{firm.slug}/insight", title="Discover Programme",
        bucket="insight", status="open", cohort="2027",
    )
    _digest_with_something_due(user)

    assert assemble_digest(user, today=TODAY)["picks_note"] == (
        "Nothing yet for your 2028 Summer Internship cycle"
    )


def test_a_student_with_no_declared_cycle_gets_no_note():
    user = _user()
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _pick_opp(firm, cohort="2027")
    _digest_with_something_due(user)

    digest = assemble_digest(user, today=TODAY)

    assert digest["picks"]
    assert digest["picks_note"] == ""


# ---------------------------------------------------------------------------
# A pick's deadline travels with its provenance or not at all.
# ---------------------------------------------------------------------------
def _only_pick(user):
    picks = assemble_digest(user, today=TODAY)["picks"]
    assert len(picks) == 1, picks
    return picks[0]


def test_a_picks_prose_read_deadline_is_marked_reported():
    user = _user()
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _pick_opp(firm, cohort="2028", days=12, confidence=0.6)
    _digest_with_something_due(user)

    pick = _only_pick(user)

    assert pick["deadline_marker"]["countdown"] == "closes in 12 days"
    assert pick["reported"]["label"] == "reported"


def test_a_picks_board_published_deadline_carries_no_marker():
    user = _user()
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _pick_opp(firm, cohort="2028", days=12, confidence=1.0)
    _digest_with_something_due(user)

    pick = _only_pick(user)

    assert pick["deadline_marker"]["countdown"] == "closes in 12 days"
    assert pick["reported"] is None


def test_an_undated_pick_prints_no_date():
    user = _user()
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _pick_opp(firm, cohort="2028")
    _digest_with_something_due(user)

    pick = _only_pick(user)

    assert pick["deadline_marker"]["posted"] is False
    assert pick["reported"] is None


# ---------------------------------------------------------------------------
# D-11: "New for you" means new.
#
# The section used to run the page's scorer over the page's board, and the
# founder's four picks were picks one to four on the Opportunities page: 100%
# overlap, an email that repeated the page it was meant to extend. The picks
# now qualify on `first_seen` inside `NEW_WINDOW_DAYS` and fall back to the
# scorer only when fewer than `MIN_NEW_PICKS` rows clear it, saying which of
# the two it did.
# ---------------------------------------------------------------------------
def _age(opp, days):
    """`Opportunity.first_seen` is `auto_now_add`, so a fixture row is always
    born today. Writing the column afterwards is the only way to build an old
    row, and every case below needs one."""
    Opportunity.objects.filter(pk=opp.pk).update(
        first_seen=timezone.now() - timedelta(days=days)
    )
    opp.refresh_from_db()
    return opp


def test_a_fresh_row_is_picked_over_an_old_one_that_scores_the_same():
    """Two tier-1 firms, identical rows, and the only difference between
    them is age. Before D-11 the four picks would have come off the top of
    the same ranking the page shows; now the old firm's rows are not
    eligible at all."""
    user = _user()
    stale_firm = _firm("Stale Bank", "stale")
    fresh_firm = _firm("Fresh Bank", "fresh")
    UserFirm.all_objects.create(user=user, firm=stale_firm, tier=1)
    UserFirm.all_objects.create(user=user, firm=fresh_firm, tier=1)
    for n in (1, 2, 3):
        _age(_pick_opp(stale_firm, cohort="2028", n=n), NEW_WINDOW_DAYS + 1)
    for n in (4, 5):
        _pick_opp(fresh_firm, cohort="2028", n=n)
    _digest_with_something_due(user)

    digest = assemble_digest(user, today=TODAY)

    assert digest["picks_mode"] == MODE_NEW
    assert digest["picks"], "the fresh rows should have scored"
    assert {p["firm_name"] for p in digest["picks"]} == {"Fresh Bank"}
    assert all(p["first_seen_days"] <= NEW_WINDOW_DAYS for p in digest["picks"])


def test_one_qualifying_row_is_not_enough_and_the_email_says_so():
    """The fallback, at its exact boundary: `MIN_NEW_PICKS` is 2, so a week
    with one new row is a week the section stops claiming novelty. The old
    rows come back, and the sentence above them changes."""
    user = _user()
    stale_firm = _firm("Stale Bank", "stale")
    fresh_firm = _firm("Fresh Bank", "fresh")
    UserFirm.all_objects.create(user=user, firm=stale_firm, tier=1)
    UserFirm.all_objects.create(user=user, firm=fresh_firm, tier=1)
    for n in (1, 2, 3):
        _age(_pick_opp(stale_firm, cohort="2028", n=n), NEW_WINDOW_DAYS + 1)
    _pick_opp(fresh_firm, cohort="2028", n=4)
    _digest_with_something_due(user)

    digest = assemble_digest(user, today=TODAY)

    assert digest["picks_mode"] == MODE_BEST
    assert len(digest["picks"]) > MIN_NEW_PICKS - 1
    assert "Stale Bank" in {p["firm_name"] for p in digest["picks"]}
    assert digest["picks_mode_line"] == (
        "Nothing new enough this week, so these are your best open roles."
    )


def test_the_boundary_day_still_counts_as_new():
    """Seven days is the gap between two sends, so a row first seen exactly
    `NEW_WINDOW_DAYS` ago is one the last email could not have carried. The
    window is inclusive on purpose."""
    user = _user()
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for n in (1, 2):
        _age(_pick_opp(firm, cohort="2028", n=n), NEW_WINDOW_DAYS)
    _digest_with_something_due(user)

    digest = assemble_digest(user, today=TODAY)

    assert digest["picks_mode"] == MODE_NEW
    assert [p["first_seen_days"] for p in digest["picks"]] == [NEW_WINDOW_DAYS] * 2


def test_every_pick_carries_the_age_the_heading_rests_on():
    """The claim has to be checkable from the inbox, in both modes: a reader
    who is told these are new can count the days on every row."""
    user = _user()
    firm = _firm()
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    _pick_opp(firm, cohort="2028", n=1)
    _age(_pick_opp(firm, cohort="2028", n=2), 3)
    _digest_with_something_due(user)

    digest = assemble_digest(user, today=TODAY)

    assert sorted(p["first_seen_days"] for p in digest["picks"]) == [0, 3]
    assert digest["picks_mode_line"] == MODE_LINES[digest["picks_mode"]]


def test_the_mode_line_is_one_sentence_in_the_house_voice():
    for line in MODE_LINES.values():
        assert line.endswith(".")
        assert line.count(".") == 1
        assert "—" not in line
