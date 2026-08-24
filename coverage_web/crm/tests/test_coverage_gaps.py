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
    names every firm, so a whole-body search proves nothing about ranking.

    Anchored on the rendered `<h2>`, not the bare words "Coverage Gaps":
    the page's own inlined `<style>` block (`crm/_styles.html`) has a CSS
    comment naming the same section ("Coverage Gaps strip: where you are
    least covered..."), earlier in the document than the real heading. A
    bare `body.index("Coverage Gaps")` anchored there instead, which cost
    every membership check (`"x" in gap_strip`) nothing — the CSS block was
    just extra haystack — but silently broke the first assertion to COUNT
    occurrences, since CSS text landed inside the slice too."""
    start = body.index('<h2 class="strip-title" title="Ranked by exposure')
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
def test_network_page_shows_gaps_and_advocate_fractions(client):
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
    # The tier cost line ("2 firms × 2 = 4 advocates · ... in place · ... to
    # go") was pulled from Firm Coverage per direct feedback that it read as
    # clutter under every tier label. coverage.tier_cost() is still exercised
    # directly by test_tier_cost_makes_the_commitment_visible above; only the
    # render was removed.
    assert "firms × 2" not in body


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


# ---------------------------------------------------------------------------
# 4. Every under-covered firm card carries the SAME one-click action the
#    Coverage Gaps strip computes for its worst 6 — not just those 6.
#    `_pick_lever` (crm/views.py) is the single function both surfaces call,
#    so a firm's "who to work next" answer can never disagree with itself
#    between the strip and its own card.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_firm_ranked_outside_the_strip_still_gets_its_own_lever(client):
    """The strip only ever shows the worst 6. Before this, the one-click
    action existed nowhere else — a firm ranked 7th or worse had a status
    but no path from reading it to doing something about it."""
    user = User.objects.create_user(email="lever@example.com", password="x")
    # Six firms with NOTHING (no_contacts, 4 points) always outrank a
    # seventh that has one warm-but-unconverted contact (no_advocate, 2
    # points) at the same tier, so the seventh is guaranteed to fall outside
    # `rank_gaps`'s default `limit=6`.
    empties = [Firm.objects.create(slug=f"empty-{i}", name=f"Empty {i}") for i in range(6)]
    for f in empties:
        UserFirm.all_objects.create(user=user, firm=f, tier=1)
    seventh = Firm.objects.create(slug="seventh-co", name="Seventh Co")
    UserFirm.all_objects.create(user=user, firm=seventh, tier=1)
    Contact.all_objects.create(user=user, name="Warm One", firm=seventh, warmth="chatted")

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()

    gap_block = _gap_strip(body)
    assert "Seventh Co" not in gap_block  # confirms it's outside the strip
    # And yet its OWN card, further down the page, still carries the lever.
    assert "Work Warm One" in body
    assert reverse("crm:contact_new") + "?firm=seventh-co" not in body  # has a lever, not the empty-firm CTA


@pytest.mark.django_db
def test_an_unranked_firm_still_gets_the_add_contact_cta(client):
    """`rank_gaps` skips untiered firms outright — they never reach the
    strip at all, tiered or not. Proves the card-level action is a
    genuinely separate mechanism, not a reflection of strip membership:
    an Unranked firm with nobody still gets a CTA, and it is the
    add-a-contact form (there is no lever — nobody exists to work)."""
    user = User.objects.create_user(email="unranked@example.com", password="x")
    firm = Firm.objects.create(slug="wildcard-co", name="Wildcard Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=None)

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()
    assert "Wildcard Co" in body
    # Not the bare phrase: the embedded stylesheet's own section comment
    # ("Coverage Gaps strip: where...") contains it too, in <head>, on
    # every page regardless of whether the section renders. The rendered
    # heading is a different, more specific string.
    assert '<h2 class="strip-title" title="Ranked by exposure' not in body
    assert reverse("crm:contact_new") + "?firm=wildcard-co" in body


def test_pick_lever_returns_none_with_no_candidates():
    from crm.views import _pick_lever

    assert _pick_lever([]) is None


@pytest.mark.django_db
def test_a_covered_firm_shows_no_action_at_all(client):
    """`adv_met` firms are done, not a task — the same posture the gap
    ladder already holds (COVERED scores 0 and is never a gap)."""
    user = User.objects.create_user(email="met@example.com", password="x")
    user.assets = {"advocate_target": 2}
    user.save(update_fields=["assets"])
    firm = Firm.objects.create(slug="settled-co", name="Settled Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for i in range(2):
        Contact.all_objects.create(
            user=user, name=f"Advocate {i}", firm=firm, warmth="advocate"
        )

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()
    assert "Settled Co" in body
    # Not the bare class name: the embedded stylesheet defines
    # `.fc-act-link { ... }` in <head> on every page regardless of whether
    # any card renders one. The rendered markup always carries the
    # `class="..."` attribute form, which the CSS selector text does not.
    assert 'class="fc-act-link"' not in body


@pytest.mark.django_db
def test_the_gap_strip_shows_its_score_without_a_hover(client):
    """The full formula lives in a `title=` tooltip, unreachable on any
    touch device — but the SCORE it resolves to, the thing a card is
    actually ranked by, must be plain text, not hover-only. Spelled out as
    "exposure", not the "exp" abbreviation nobody could read without the
    tooltip (which touch devices can never reach in the first place)."""
    user = User.objects.create_user(email="math@example.com", password="x")
    firm = Firm.objects.create(slug="exposed-co", name="Exposed Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()
    gap_block = _gap_strip(body)
    # Tier 1, no contacts: 3 × 4 = exposure 12. "· exposure 12" (not the bare
    # substring) is the plain-text reading specifically — the hover tooltip
    # below also contains "exposure 12" via its own "= exposure 12", and the
    # two must not be conflated.
    assert 'class="gap-exp"' in gap_block
    assert "· exposure 12" in gap_block
    # The full breakdown still rides along in the hover tooltip.
    assert "= exposure 12" in gap_block


@pytest.mark.django_db
def test_tied_gap_cards_are_ordered_by_who_is_actually_hiring(client):
    """Two Tier 1 firms with no contacts are genuinely TIED on exposure (both
    score 12) — that is the formula being honest, and also the point at which
    it stops helping. Tied cards read as identical text, so something real
    has to tell them apart.

    That something used to be an "N Open" badge printed on the card. The
    badge was asked for and removed (see contact_list.html), so the count
    does the same job by ORDERING the tied cards instead: the firm with seats
    open right now is the one worth a contact today, in a way the exposure
    formula has no term for.

    Named so that the OLD tie-break would get this wrong: alphabetically
    "Zeta" comes last, and it is the one that has to come first.
    """
    from directory.models import Opportunity

    user = User.objects.create_user(email="open@example.com", password="x")
    hiring = Firm.objects.create(slug="zeta-co", name="Zeta Co")
    quiet = Firm.objects.create(slug="alpha-co", name="Alpha Co")
    UserFirm.all_objects.create(user=user, firm=hiring, tier=1)
    UserFirm.all_objects.create(user=user, firm=quiet, tier=1)
    for n in range(3):
        Opportunity.objects.create(
            firm=hiring, url=f"https://x/{n}", title=f"Summer Analyst {n}",
            bucket="internship", status="open",
        )

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()
    gap_block = _gap_strip(body)

    # Both tied at exposure 12 (Tier 1 × no_contacts = 3 × 4)...
    assert gap_block.count("· exposure 12") == 2
    # ...and the tie is broken by who is hiring, not by the alphabet.
    assert gap_block.index("Zeta Co") < gap_block.index("Alpha Co"), (
        "the firm with three seats open sorts below a firm with none, on the "
        "strength of its first letter. With the open-count badge gone from "
        "the card, this ordering is the only thing left telling two "
        "identically-scored cards apart."
    )
    # The count itself is not printed on any card, but it is still legible
    # in the card's own arithmetic tooltip.
    assert "pill fc-open" not in gap_block, (
        "the open-role badge is back on a Coverage Gaps card."
    )
    assert "3 open roles right now" in gap_block


# ---------------------------------------------------------------------------
# 5. "Contacts Needing Action" sorts each lane longest-silent-first.
#    Before this, `cadence.due_actions` only ever sorted by
#    (priority, tier, firm name) — real for choosing which LANE an action
#    lands in, meaningless for ordering 80+ people inside the SAME lane, all
#    the same priority. The lane fell back to alphabetical-by-firm, which is
#    what this test proves is no longer true.
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_follow_up_lane_sorts_longest_silent_first(client):
    from django.utils import timezone as tz

    user = User.objects.create_user(email="stale@example.com", password="x")
    # Named so alphabetical order would put Aardvark BEFORE Zebra — the
    # opposite of the order this test expects once idle time decides it.
    aardvark = Firm.objects.create(slug="aardvark-co", name="Aardvark Co")
    zebra = Firm.objects.create(slug="zebra-co", name="Zebra Co")
    UserFirm.all_objects.create(user=user, firm=aardvark, tier=2)
    UserFirm.all_objects.create(user=user, firm=zebra, tier=2)

    barely = Contact.all_objects.create(
        user=user, name="Barely Overdue", firm=aardvark, warmth="cold",
        thread_state="no_reply",
    )
    long_overdue = Contact.all_objects.create(
        user=user, name="Long Overdue", firm=zebra, warmth="cold",
        thread_state="no_reply",
    )
    from crm.models import Touch
    # Both offsets are CALENDAR days; the follow-up threshold
    # (`followup_after_business_days`, 6) is in BUSINESS days, and the two
    # diverge by weekday. 8 calendar days is 6 business days Mon-Fri but only
    # 5 on Sat/Sun — below the window — so an 8 here made this test pass five
    # days a week and fail on the weekend (seen on Sat 2026-08-15, with the
    # whole Follow Up lane empty rather than merely misordered). 10 is the
    # smallest offset that clears 6 business days on EVERY weekday, and still
    # sits under the 10-business-day park window, so this test measures the
    # ordering it is named for rather than the day it runs on. Same fix, same
    # reason as test_today.test_within_a_class_the_longest_silent_goes_first;
    # the arithmetic itself is pinned by
    # test_cadence.test_ten_calendar_days_is_the_weekday_proof_followup_offset.
    Touch.all_objects.create(user=user, contact=barely, kind="outreach",
                             channel="email", ts=tz.now() - timedelta(days=10))
    Touch.all_objects.create(user=user, contact=long_overdue, kind="outreach",
                             channel="email", ts=tz.now() - timedelta(days=40))

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()
    follow_up_start = body.index("Follow Up")
    lane = body[follow_up_start:follow_up_start + 3000]
    assert "Long Overdue" in lane and "Barely Overdue" in lane
    # Longest-silent (Zebra Co's contact) renders FIRST despite sorting
    # alphabetically last — proof the idle clock, not the firm name, now
    # decides the order.
    assert lane.index("Long Overdue") < lane.index("Barely Overdue")
