"""Tests for the Coverage-Gaps ranking and the advocate arithmetic on the
Network board (crm/coverage.py + crm.views.contact_list).

`crm.coverage` is pure — no DB, no clock — so the ranking tests below are
plain unit tests over constructed dicts with an explicit `today`. That is
the point of keeping the formula out of the view: the ordering claims the
product makes ("a Tier 1 firm with nobody is worse than a Tier 3 with
someone") are assertable rather than eyeballed.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from crm import coverage
from crm.models import Contact, UserFirm
from directory.models import Firm, FirmDate

User = get_user_model()

TODAY = date(2026, 7, 25)


def _gap_strip(body: str) -> str:
    """Just the Coverage Gaps section — the rest of the Network page also
    names every firm, so a whole-body search proves nothing about ranking."""
    start = body.index("Coverage Gaps")
    return body[start : body.index("Contacts Needing Action", start)]


def _firm(name, tier, warmths, app_close=None, firm_id=None):
    return {
        "firm_id": firm_id if firm_id is not None else name,
        "name": name,
        "tier": tier,
        "warmths": warmths,
        "app_close": app_close,
    }


# ---------------------------------------------------------------------------
# 1. The formula itself.
# ---------------------------------------------------------------------------
def test_tier_one_with_no_contacts_outranks_tier_three_with_one():
    """The headline ordering claim in the brief."""
    gaps = coverage.rank_gaps(
        [_firm("Empty Tier 1", 1, []), _firm("Tier 3 With One", 3, ["cold"])],
        today=TODAY,
        target=2,
    )
    assert [g["name"] for g in gaps] == ["Empty Tier 1", "Tier 3 With One"]
    # 3 × 4 = 12 vs 1 × 3 = 3 — the arithmetic is shown, not asserted.
    assert [g["exposure"] for g in gaps] == [12, 3]


def test_gap_ladder_orders_states_by_how_much_work_is_left():
    """Within one tier: no contacts > all cold > no advocate > below target,
    and a firm at target drops out of the strip entirely."""
    gaps = coverage.rank_gaps(
        [
            _firm("D Below Target", 2, ["advocate", "cold"]),
            _firm("C No Advocate", 2, ["chatted", "replied"]),
            _firm("B All Cold", 2, ["cold", "cold"]),
            _firm("A No Contacts", 2, []),
            _firm("E Covered", 2, ["advocate", "advocate"]),
        ],
        today=TODAY,
        target=2,
    )
    assert [g["state"] for g in gaps] == [
        coverage.NO_CONTACTS,
        coverage.ALL_COLD,
        coverage.NO_ADVOCATE,
        coverage.BELOW_TARGET,
    ]
    assert "E Covered" not in [g["name"] for g in gaps]


def test_confirmed_deadline_adds_urgency_without_outweighing_tier():
    """A close date lifts a gap, but additively: it never lets a Tier 3
    firm jump a Tier 1 firm in the same state."""
    gaps = coverage.rank_gaps(
        [
            _firm("Tier 1 No Date", 1, []),
            _firm("Tier 3 Closing", 3, [], app_close=TODAY + timedelta(days=5)),
        ],
        today=TODAY,
        target=2,
    )
    assert [g["name"] for g in gaps] == ["Tier 1 No Date", "Tier 3 Closing"]
    assert [g["exposure"] for g in gaps] == [12, 4 + 3]


@pytest.mark.parametrize(
    "days_out,bonus",
    [(0, 3), (14, 3), (15, 2), (30, 2), (31, 1), (60, 1), (61, 0), (None, 0), (-4, 3)],
)
def test_deadline_bonus_bands_are_exact(days_out, bonus):
    assert coverage.deadline_bonus(days_out) == bonus


def test_untiered_firms_are_never_ranked():
    """The user hasn't claimed to care about an untiered firm, so the strip
    doesn't tell them they're exposed at it."""
    assert coverage.rank_gaps([_firm("Unranked", None, [])], today=TODAY) == []


def test_ranking_is_deterministic_on_ties():
    """Equal exposure breaks on (tier, name) — the same order every render."""
    firms = [
        _firm("Zeta", 2, []),
        _firm("Alpha", 2, []),
        _firm("Mid", 2, []),
    ]
    names = [g["name"] for g in coverage.rank_gaps(firms, today=TODAY)]
    assert names == ["Alpha", "Mid", "Zeta"]
    # Input order must not matter.
    assert names == [
        g["name"] for g in coverage.rank_gaps(list(reversed(firms)), today=TODAY)
    ]


def test_limit_returns_only_the_worst_handful():
    firms = [_firm(f"Firm {i:02d}", 1, []) for i in range(20)]
    assert len(coverage.rank_gaps(firms, today=TODAY, limit=6)) == 6


# ---------------------------------------------------------------------------
# 2. The advocate target and the tier-cost arithmetic.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_advocate_target_reads_assets_and_falls_back():
    user = User.objects.create_user(email="t@example.com", password="x")
    assert coverage.advocate_target(user) == 2          # empty assets
    user.assets = {"advocate_target": 3}
    assert coverage.advocate_target(user) == 3
    # Junk never propagates into the arithmetic: a target of 0 would make
    # every firm permanently "covered".
    for junk in (0, -1, "two", True, None):
        user.assets = {"advocate_target": junk}
        assert coverage.advocate_target(user) == 2


def test_tier_cost_makes_the_commitment_visible():
    cost = coverage.tier_cost(
        [
            {"advocates": 2, "contact_count": 4},
            {"advocates": 1, "contact_count": 2},
            {"advocates": 0, "contact_count": 0},
        ],
        target=2,
    )
    assert cost == {
        "firms": 3,
        "target": 2,
        "needed": 6,
        "have": 3,
        "remaining": 3,
        "uncovered": 1,
    }


# ---------------------------------------------------------------------------
# 3. The Network page renders both, against real rows.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_network_page_shows_gaps_advocate_fractions_and_tier_cost(client):
    user = User.objects.create_user(email="net@example.com", password="x")
    user.assets = {"advocate_target": 2}
    user.save(update_fields=["assets"])

    covered = Firm.objects.create(slug="covered-co", name="Covered Co")
    exposed = Firm.objects.create(slug="exposed-co", name="Exposed Co")
    UserFirm.all_objects.create(user=user, firm=covered, tier=1)
    UserFirm.all_objects.create(user=user, firm=exposed, tier=1)
    for i in range(2):
        Contact.all_objects.create(
            user=user, name=f"Advocate {i}", firm=covered, warmth="advocate"
        )

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()

    # The empty Tier 1 firm is the gap; the covered one is not listed.
    assert "Exposed Co" in body
    gap_block = _gap_strip(body)
    assert "Exposed Co" in gap_block and "Covered Co" not in gap_block
    # Advocate fractions on the cards, both sides of the target.
    # The fraction became advocate SOCKETS — dots that fill — with the words
    # kept in the accessible name. Assert both halves of that contract: the
    # spoken fraction, and a filled and an empty socket actually drawn.
    assert "2 of 2 advocates" in body and "0 of 2 advocates" in body
    assert 'adv-socket is-filled' in body
    assert '<i class="adv-socket"></i>' in body
    # And the tier's cost: 2 firms × 2 = 4 advocates, 2 in place.
    assert "2 firms × 2" in body
    assert "= 4 advocates" in body


@pytest.mark.django_db
def test_network_gaps_weight_a_confirmed_deadline_only(client):
    """A rumored close date must not move a firm up the strip — the same
    `confirmed_official` bar cadence._closing_soon holds."""
    user = User.objects.create_user(email="dl@example.com", password="x")
    soon = Firm.objects.create(slug="soon-co", name="Soon Co")
    rumor = Firm.objects.create(slug="rumor-co", name="Rumor Co")
    UserFirm.all_objects.create(user=user, firm=soon, tier=3)
    UserFirm.all_objects.create(user=user, firm=rumor, tier=3)
    today = date.today()
    # 1.0 is the stored float for "confirmed_official"; 0.3 is "rumor".
    FirmDate.objects.create(
        firm=soon, cycle="2027", region="us", event_kind="app_close",
        date=today + timedelta(days=7), confidence=1.0,
    )
    FirmDate.objects.create(
        firm=rumor, cycle="2027", region="us", event_kind="app_close",
        date=today + timedelta(days=3), confidence=0.3,
    )

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()
    gap_block = _gap_strip(body)
    # Both are gaps (neither has contacts), but only the confirmed one
    # carries the deadline pill, and it sorts first.
    assert "7d to close" in gap_block
    assert "3d to close" not in gap_block
    assert gap_block.index("Soon Co") < gap_block.index("Rumor Co")
