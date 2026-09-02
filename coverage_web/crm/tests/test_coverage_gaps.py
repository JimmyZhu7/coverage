"""Tests for the Coverage-Gaps ranking, the CG tag it now drives, and the
advocate arithmetic on the Network board (crm/coverage.py +
crm.views.contact_list).

`crm.coverage` is pure — no DB, no clock — so the ranking tests below are
plain unit tests over constructed dicts with an explicit `today`. That is
the point of keeping the formula out of the view: the ordering claims the
product makes ("a Tier 1 firm with nobody is worse than a Tier 3 with
someone") are assertable rather than eyeballed.

THE STRIP THIS FILE WAS NAMED FOR IS GONE (2026-09-02). It drew the worst
six ranked firms in a ledger above the board; the founder asked for it
deleted and its status routed onto the firm cards. So every test here that
sliced the page down to that section is rewritten against the surface that
replaced it: a "CG" pill on the card of any firm whose exposure clears
`coverage.CG_EXPOSURE_MIN`. The RANKING claims are unchanged and mostly did
not need touching, which is the argument for having kept the formula pure.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from crm import coverage
from crm.models import Contact, UserFirm
from directory.models import Firm, FirmDate

User = get_user_model()

TODAY = date(2026, 7, 25)

_HEAD = re.compile(
    r'<div class="firm-card-head">\s*'
    r'<div class="firm-card-name">(?P<name>.*?)</div>(?P<marks>.*?)</div>',
    re.S,
)


def _cards(body: str) -> str:
    """Just the tier lanes, not the board around them.

    Replaces `_gap_strip`, which sliced the page down to the deleted
    Coverage Gaps `<h2>`. Ends at `.net-legend-mini`, not the panel's
    `</section>`: the key inside that panel draws a "CG" swatch of its own,
    and leaving it in scope would make "no card on this board is tagged"
    indistinguishable from "the key always shows one anyway" — the same
    trap `test_firm_card_badges._tier_board` documents for "SP".
    """
    start = body.index('<div class="tier-section"')
    return body[start : body.index('<p class="net-legend-mini"', start)]


def _tagged(body: str) -> list[str]:
    """The firm names whose CARD carries the CG pill, in board order."""
    return [
        m.group("name").strip()
        for m in _HEAD.finditer(_cards(body))
        if "pill fc-cg" in m.group("marks")
    ]


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


def test_limit_returns_only_the_worst_handful_when_it_is_asked_for():
    """`limit` still caps, but it no longer caps BY DEFAULT.

    The default was 6 while the Coverage Gaps strip drew exactly six rows.
    That strip is gone and the tag that replaced it has no cap, so the
    default is None and a caller that wants a handful says so. Renamed from
    `test_limit_returns_only_the_worst_handful` because the old name read as
    a claim about the function's default, and that claim is now false.
    """
    firms = [_firm(f"Firm {i:02d}", 1, []) for i in range(20)]
    assert len(coverage.rank_gaps(firms, today=TODAY, limit=6)) == 6
    assert len(coverage.rank_gaps(firms, today=TODAY)) == 20
    assert len(coverage.rank_gaps(firms, today=TODAY, limit=None)) == 20


# ---------------------------------------------------------------------------
# 1b. The CG bar: which firms earn the tag, now that nothing caps the list.
# ---------------------------------------------------------------------------
def test_the_cg_bar_is_a_position_on_the_scale_not_a_tuned_number():
    """8 is `TIER_WEIGHT[2] × GAP_POINTS[NO_CONTACTS]` and is written that
    way in the module, so it moves if the scale under it ever moves."""
    assert coverage.CG_EXPOSURE_MIN == 8
    assert coverage.CG_EXPOSURE_MIN == (
        coverage.TIER_WEIGHT[2] * coverage.GAP_POINTS[coverage.NO_CONTACTS]
    )


def test_which_firms_earn_the_tag_across_the_whole_ladder():
    """The bar, stated as the board it produces. One firm per reachable
    (tier × rung) combination on track, plus the off-track and deadline
    cases, and exactly which of them come back tagged.

    Every number here is the formula's own arithmetic, so a change to
    TIER_WEIGHT, GAP_POINTS, track_fit or the bar breaks this test with the
    firm names that changed rather than with a count.
    """
    warm = ["chatted", "replied"]        # no_advocate
    cold = ["cold", "cold"]              # all_cold
    below = ["advocate", "cold"]         # below_target, one of two
    firms = [
        _firm("T1 nobody", 1, []),                      # 3 x 4  = 12  TAG
        _firm("T1 no replies", 1, cold),                # 3 x 3  =  9  TAG
        _firm("T1 no advocate", 1, warm),               # 3 x 2  =  6
        _firm("T1 one advocate", 1, below),             # 3 x 1  =  3
        _firm("T2 nobody", 2, []),                      # 2 x 4  =  8  TAG
        _firm("T2 no replies", 2, cold),                # 2 x 3  =  6
        _firm("T2 no advocate", 2, warm),               # 2 x 2  =  4
        _firm("T3 nobody", 3, []),                      # 1 x 4  =  4
        _firm("T3 no replies", 3, cold),                # 1 x 3  =  3
    ]
    for f in firms:                      # both sides on track
        f["firm_tracks"], f["user_tracks"] = ["ib"], ["ib"]
    # Off track: the ladder rung is halved, so a tier-1 firm with nobody
    # lands where a tier-1 firm with warm contacts does and stays untagged.
    off = _firm("T1 nobody, off track", 1, [], firm_id="off")
    off["firm_tracks"], off["user_tracks"] = ["pe"], ["ib"]      # 3 x 2 = 6
    # ...until a confirmed close inside 30 days adds the 2 that clears it.
    off_soon = _firm("T1 nobody, off track, closing", 1, [],
                     app_close=TODAY + timedelta(days=20), firm_id="offsoon")
    off_soon["firm_tracks"], off_soon["user_tracks"] = ["pe"], ["ib"]  # 6+2=8
    # A firm at target is not a gap at all, so it cannot be tagged.
    done = _firm("Covered", 1, ["advocate", "advocate"], firm_id="done")

    gaps = coverage.rank_gaps(firms + [off, off_soon, done], today=TODAY, target=2)
    tagged = {g["name"] for g in gaps if g["exposure"] >= coverage.CG_EXPOSURE_MIN}

    assert tagged == {
        "T1 nobody", "T1 no replies", "T2 nobody", "T1 nobody, off track, closing",
    }
    assert coverage.flagged_firm_ids(gaps) == {
        g["firm_id"] for g in gaps if g["name"] in tagged
    }
    # The three cases the brief names, in order, and where the bar leaves them.
    by_name = {g["name"]: g for g in gaps}
    assert by_name["T1 nobody"]["exposure"] == 12          # no contacts: worst
    assert by_name["T1 no replies"]["exposure"] == 9       # contacts, no replies
    assert by_name["T1 one advocate"]["exposure"] == 3     # an advocate: near the floor
    assert "Covered" not in by_name                        # at target: not a gap


def test_a_firm_with_an_advocate_can_never_be_tagged():
    """BELOW_TARGET is 1 point, so the ceiling for a firm that HAS an
    advocate is 3 x 1 + 3 = 6 — under the bar even at tier 1 with a deadline
    inside two weeks. Progress is not a thing to nag about, the same posture
    the ladder already holds by scoring COVERED at zero."""
    best = _firm("Tier 1, one advocate, closing tomorrow", 1, ["advocate"],
                 app_close=TODAY + timedelta(days=1))
    (gap,) = coverage.rank_gaps([best], today=TODAY, target=2)
    assert gap["ladder_state"] == coverage.BELOW_TARGET
    assert gap["exposure"] == 6 < coverage.CG_EXPOSURE_MIN
    assert coverage.flagged_firm_ids([gap]) == set()


def test_flagged_firm_ids_ignores_a_row_with_no_firm_id():
    """`rank_gaps` takes plain dicts and does not require `firm_id`; the tag
    is looked up BY id, so a row without one must be skipped rather than
    putting `None` into the set and marking every card whose firm_id is
    None."""
    anon = _firm("Anonymous", 1, [], firm_id=None)
    anon["firm_id"] = None
    gaps = coverage.rank_gaps([anon], today=TODAY, target=2)
    assert gaps and gaps[0]["exposure"] == 12
    assert coverage.flagged_firm_ids(gaps) == set()


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
def test_the_widget_is_gone_and_left_nothing_behind(client):
    """Delete, not hide. The founder asked for the Coverage Gaps widget off
    the page, so no part of it may render on any account: not the heading,
    not the ledger, not a row, not the "Who to find" dropdown that lived
    inside it, and not the CSS that drew any of them.

    Asserted on a board that WOULD have drawn a full strip — a tiered firm
    with nobody at it is the top of the old ranking — because "nothing
    renders" is only evidence when something was owed.
    """
    user = User.objects.create_user(email="nostrip@example.com", password="x" * 14)
    firm = Firm.objects.create(slug="exposed-co", name="Exposed Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()

    assert _tagged(body) == ["Exposed Co"], "the firm is not on the board at all"

    # Markup and stylesheet are checked separately, and the split matters.
    # CSS `/* */` comments SURVIVE into the response, and several of the
    # rules that stayed name the deleted classes in their own comments to
    # explain what went; a bare `"gap-card" not in body` would fail on prose
    # about the deletion. `<style>` is also plural on this page, so every
    # block is stripped, not the first one.
    styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", body, re.S))
    markup = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.S)

    for gone in ('class="gap-strip"', 'class="gap-row', 'class="gap-card',
                 'class="gap-name"', 'class="gap-head"', 'class="gap-tier-tag"',
                 'class="gap-state"', 'class="btn gap-act"', 'class="src"',
                 'class="src-toggle"', 'class="src-panel'):
        assert gone not in markup, f"{gone} still renders"
    for gone in ("Coverage Gaps", "Who to find", "Ranked by exposure",
                 "to close", "aria-label=\"Add a contact at "):
        assert gone not in markup, f"{gone!r} is still on the page"

    # The rules, which were asked for by name: "delete the rules too, not
    # just the markup". `.gap-due-tag` is deliberately NOT in this list — the
    # firm card's own red countdown adopted it and still draws it.
    for rule in (".gap-strip {", ".gap-row {", ".gap-card {", ".gap-name {",
                 ".gap-head {", ".gap-tier-tag {", ".gap-state {", ".gap-act {",
                 ".src-toggle {", ".src-panel {", ".src-link {", ".src-row {",
                 "@keyframes src-drop"):
        assert rule not in styles, f"{rule} is still in the stylesheet"
    assert ".gap-due-tag {" in styles, (
        "the firm card's countdown lost its rule when the strip's block went"
    )
    assert ".pill.fc-cg {" in styles, "the tag that replaced the strip has no rule"
    # And the endpoint the panel POSTed to has no caller left on the page.
    assert reverse("crm:sourcing_event") not in markup


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

    # The empty Tier 1 firm is the gap and wears the tag; the covered one is
    # not a gap at all, so it cannot. Read off the CARDS now rather than off
    # the deleted strip: both firms have always appeared on this board, and
    # what distinguishes them is the mark, not membership of a second list.
    assert "Exposed Co" in body and "Covered Co" in body
    assert _tagged(body) == ["Exposed Co"]
    # The covered card's advocate progress. The fraction became advocate
    # SOCKETS — dots that fill — with the words kept in the accessible name.
    assert "2 of 2 advocates" in body
    assert 'adv-socket is-filled' in body
    # Exposed Co has zero contacts, so its card renders no bar and no
    # sockets at all — an empty bar next to two empty dots said nothing its
    # own "＋ Add a contact" line (already asserted via the gap strip above)
    # didn't already say. See crm/tests/test_firm_card_badges.py::
    # test_a_firm_with_nobody_added_shows_no_bar_or_sockets and
    # ::test_sockets_hide_until_a_firm_has_an_advocate for that contract in
    # full, including where the "0 of N advocates" number moved to for a
    # firm that HAS contacts but no advocate yet.
    # The tier cost line ("2 firms × 2 = 4 advocates · ... in place · ... to
    # go") was pulled from Firm Coverage per direct feedback that it read as
    # clutter under every tier label. coverage.tier_cost() is still exercised
    # directly by test_tier_cost_makes_the_commitment_visible above; only the
    # render was removed.
    assert "firms × 2" not in body


@pytest.mark.django_db
def test_network_gaps_weight_a_confirmed_deadline_only(client):
    """A rumored close date must not add exposure — the same
    `confirmed_official` bar cadence._closing_soon holds.

    Asserted on `resp.context["gaps"]` rather than on the strip's rendered
    "7d to close" tag, which is gone with the strip. The claim is about the
    FORMULA'S INPUT, and the context is where that is legible: the rumored
    firm must reach `rank_gaps` with no `app_close` at all, not merely fail
    to print a countdown. Two tier-3 firms with nobody, so the only thing
    that can separate them is the deadline term.
    """
    user = User.objects.create_user(email="dl@example.com", password="x")
    soon = Firm.objects.create(slug="soon-co", name="Soon Co")
    rumor = Firm.objects.create(slug="rumor-co", name="Rumor Co")
    UserFirm.all_objects.create(user=user, firm=soon, tier=3)
    UserFirm.all_objects.create(user=user, firm=rumor, tier=3)
    today = date.today()
    # 1.0 is the stored float for "confirmed_official"; 0.3 is "rumor".
    FirmDate.objects.create(
        firm=soon, cycle="ft2027", region="us", event_kind="app_close",
        date=today + timedelta(days=7), confidence=1.0,
    )
    FirmDate.objects.create(
        firm=rumor, cycle="ft2027", region="us", event_kind="app_close",
        date=today + timedelta(days=3), confidence=0.3,
    )

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"))
    gaps = {g["name"]: g for g in resp.context["gaps"]}

    assert [g["name"] for g in resp.context["gaps"]] == ["Soon Co", "Rumor Co"]
    assert gaps["Soon Co"]["days_out"] == 7
    assert gaps["Soon Co"]["deadline_bonus"] == 3
    assert gaps["Rumor Co"]["days_out"] is None, (
        "a rumored close reached the formula as a real deadline"
    )
    assert gaps["Rumor Co"]["deadline_bonus"] == 0
    # 1 x 4 + 3 = 7 against 1 x 4 = 4. Both still under the CG bar: a tier-3
    # firm is the student's own statement that it matters least, and a close
    # date does not overturn that on its own.
    assert (gaps["Soon Co"]["exposure"], gaps["Rumor Co"]["exposure"]) == (7, 4)
    assert _tagged(resp.content.decode()) == []


# ---------------------------------------------------------------------------
# 4. Every under-covered firm card carries a one-click action, and the CG
#    tag is a SEPARATE mark from it. `_pick_lever` (crm/views.py) answers
#    "who to work next" for every card; `flagged_firm_ids` answers "is this
#    one of the worst". A card can carry either, both or neither.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_an_untagged_firm_still_gets_its_own_lever(client):
    """Rewritten from `test_a_firm_ranked_outside_the_strip_still_gets_its
    _own_lever`, whose premise was the strip's `limit=6`: it seeded seven
    firms so the seventh fell off a list that no longer exists.

    The claim underneath it is not retired, it is now the ONLY claim — the
    lever is the card's only verb and the strip is not there to carry one
    for anybody. So the shape stays and the cut moves to the CG bar: a firm
    with a warm contact scores 6 at tier 1, under the bar, so it is untagged
    and must still offer the person to talk to.
    """
    user = User.objects.create_user(email="lever@example.com", password="x")
    empties = [Firm.objects.create(slug=f"empty-{i}", name=f"Empty {i}") for i in range(6)]
    for f in empties:
        UserFirm.all_objects.create(user=user, firm=f, tier=1)
    seventh = Firm.objects.create(slug="seventh-co", name="Seventh Co")
    UserFirm.all_objects.create(user=user, firm=seventh, tier=1)
    Contact.all_objects.create(user=user, name="Warm One", firm=seventh, warmth="chatted")

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()

    # All six empties are tagged; the seventh is not. Nothing is capped at
    # six any more, which is the whole point of the tag.
    assert sorted(_tagged(body)) == sorted(f"Empty {i}" for i in range(6))
    assert "Seventh Co" not in _tagged(body)
    # And its card still carries the lever.
    assert "Talk to Warm One" in body
    assert reverse("crm:contact_new") + "?firm=seventh-co" not in body  # has a lever, not the empty-firm CTA


@pytest.mark.django_db
def test_an_unranked_firm_is_never_tagged_but_still_gets_the_cta(client):
    """`rank_gaps` skips untiered firms outright, so they can never earn CG:
    the student has not claimed to care about them yet, and a tag is a
    statement about a claim they made. The card-level action is a genuinely
    separate mechanism and is unaffected — an Unranked firm with nobody
    still gets the add-a-contact CTA."""
    user = User.objects.create_user(email="unranked@example.com", password="x")
    firm = Firm.objects.create(slug="wildcard-co", name="Wildcard Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=None)

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"))
    body = resp.content.decode()
    assert "Wildcard Co" in body
    assert resp.context["gaps"] == []
    assert _tagged(body) == []
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
def test_the_tag_shows_no_number_and_explains_itself_in_words(client):
    """Rewritten from `test_the_gap_strip_shows_no_number_on_its_face`,
    which pinned the deleted strip's cards. The RULE it defended survives
    the widget and is inherited by the mark that replaced it, so the test
    moves rather than going.

    The founder asked what "exposure" meant, heard the answer, and said drop
    the number; a first pass swapped the score for the card's rank ("ranked
    1 of 6") and that was rejected within minutes too. So no measurement may
    reach a card's face — not the score, not a rank, not a position.

    The tag inherits one more constraint the strip's own arguments earned:
    `title=` is unreachable on a touchscreen, so a mark may not depend on a
    hover to be readable. "CG" is two letters, which alone say nothing to a
    first-time reader, so the board owes it a plain-words explanation that
    is not a tooltip — the key at the foot of the grid
    (`test_the_key_explains_the_tag_without_a_hover` below). The `title=`
    remains, in full words and with no arithmetic in it, for the pointer
    reader who asks the card directly.
    """
    user = User.objects.create_user(email="math@example.com", password="x")
    firm = Firm.objects.create(slug="exposed-co", name="Exposed Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"))
    body = resp.content.decode()
    cards = _cards(body)

    # Tier 1, nobody: 3 x 4 = 12, the top of the scale. The card is tagged.
    assert resp.context["gaps"][0]["exposure"] == 12
    assert _tagged(body) == ["Exposed Co"]
    # And says none of that arithmetic anywhere, tooltip included. The score
    # itself was allowed in the strip's `title=`; on a card it is not, and
    # nothing on this board asks a reader to compare a number across cards.
    #
    # "ranked N of" and not the bare word: the tag's own tooltip says "You
    # ranked this firm high", which is the student's tier described in their
    # own words and is exactly the kind of plain-language reason this test
    # exists to protect. What is banned is a POSITION, not the verb.
    for number in ("exposure", "ranked 1 of", "rank ", "= 12", "x 4", "×"):
        assert number not in cards, f"a number leaked back onto a card: {number!r}"
    assert not re.search(r">\s*1 of \d", cards), "a rank is back on a card"
    # The tag's own tooltip is a sentence, not a formula.
    assert ('title="Coverage gap. You ranked this firm high and nobody here '
            'is warm yet."') in cards


@pytest.mark.django_db
def test_tied_gaps_are_ordered_by_who_is_actually_hiring(client):
    """Two Tier 1 firms with no contacts are genuinely TIED on exposure (both
    score 12) — that is the formula being honest, and also the point at which
    it stops helping. The open-role count breaks the tie: the firm with seats
    open right now is the one worth a contact today, in a way the exposure
    formula has no term for.

    Asserted on `resp.context["gaps"]`, not on a rendered order. The Network
    page no longer paints this ordering at all: the strip that did is gone,
    the tag it left behind is a boolean, and the firm cards sort inside their
    own tier by act-now and open roles, which is a different question. The
    ordering is still LIVE and still rendered, one surface over — the weekly
    digest calls `rank_gaps` and names `no_contact[:3]` (crm/digest.py) — so
    the claim is kept where it can be checked rather than deleted with the
    markup that used to display it.

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
    resp = client.get(reverse("crm:contact_list"))
    gaps = resp.context["gaps"]

    # Both tied at exposure 12 (Tier 1 x no_contacts = 3 x 4)...
    assert [g["exposure"] for g in gaps] == [12, 12]
    # ...and the tie is broken by who is hiring, not by the alphabet.
    assert [g["name"] for g in gaps] == ["Zeta Co", "Alpha Co"], (
        "the firm with three seats open sorts below a firm with none, on the "
        "strength of its first letter."
    )
    assert [g["open"] for g in gaps] == [3, 0]
    # Both clear the bar, so both cards are tagged: a tag is not a rank and
    # does not pretend to separate what the formula says is tied.
    assert sorted(_tagged(resp.content.decode())) == ["Alpha Co", "Zeta Co"]
    # The count is not printed anywhere on the board, here or on a card.
    assert "pill fc-open" not in resp.content.decode()
    assert "open roles right now" not in resp.content.decode()


# ---------------------------------------------------------------------------
# 5. (removed) "Contacts Needing Action" sorted each lane longest-silent-
#    first. The panel — and the per-lane sort that only ever served its
#    rendering — is gone along with it: it duplicated Today's own queue
#    under different lane labels (crm/views.py::contact_list). The
#    longest-silent-first ordering claim itself is still exercised where the
#    queue actually lives now: see crm/tests/test_today.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# `deadline_bonus` over its whole domain, not three examples.
# ---------------------------------------------------------------------------
def test_deadline_bonus_is_defined_and_bounded_across_the_domain():
    """Generated inputs rather than another example. Three invariants:

    1. Every answer is one of the declared bonuses. A band table read in the
       wrong order, or a missing final `return 0`, breaks this before it
       breaks any single hand-picked case.
    2. Urgency never RISES as a deadline moves further away. This is the
       property the bands exist to express; `DEADLINE_BONUS` being a
       first-match-wins tuple means a re-ordered or overlapping entry would
       violate it silently while every existing example still passed.
    3. The far edges are finite answers, not exceptions — the closes this
       reads come from `FirmDate.date`, an unbounded DateField, so a row
       dated in 2099 or in 2015 reaches here as a five-figure day count.
    """
    allowed = {b for _limit, b in coverage.DEADLINE_BONUS} | {0}
    domain = list(range(-400, 400)) + [-100000, -1, 100000, 10**9]
    for days in domain:
        assert coverage.deadline_bonus(days) in allowed, days

    ordered = sorted(domain)
    scores = [coverage.deadline_bonus(d) for d in ordered]
    assert scores == sorted(scores, reverse=True), (
        "a deadline further out must never score MORE urgent"
    )


def test_a_deadline_already_passed_scores_the_maximum():
    """The docstring's own claim, held at the boundary rather than at -4: a
    deadline you missed at a firm you have no coverage at is the most exposed
    a firm can be, not the least."""
    top = max(b for _limit, b in coverage.DEADLINE_BONUS)
    assert coverage.deadline_bonus(-1) == top
    assert coverage.deadline_bonus(0) == top


@pytest.mark.django_db
def test_an_estimated_close_never_reaches_the_exposure_formula(client):
    """`rank_gaps` does no confidence filtering of its own by design — its
    caller does, and the caller's bar is both halves of "confirmed". An
    estimated month-level guess is worth up to 3 exposure points, which is
    now the difference between a card wearing the CG tag and not, so it must
    not arrive as an `app_close` at all.

    Read off the context rather than off the deleted strip's "5d to close"
    tag: the claim was always about what reaches the formula, and the tag
    was only the nearest visible proxy for it.
    """
    user = User.objects.create_user(email="est@example.com", password="pw12345!")
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    UserFirm.all_objects.create(user=user, firm=firm, tier=3)
    today = date.today()
    fd = FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="us", event_kind="app_close",
        date=today + timedelta(days=5), confidence=1.0,
    )
    FirmDate.objects.filter(pk=fd.pk).update(precision="estimated")

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"))
    (gap,) = resp.context["gaps"]
    assert gap["name"] == "Goldman Sachs", "the firm is still a gap, it has nobody"
    assert gap["app_close"] is None and gap["days_out"] is None, (
        "a month-level guess reached the formula as a real deadline"
    )
    assert gap["deadline_bonus"] == 0 and gap["exposure"] == 4
    # And the card says nothing about a day it does not know.
    assert "5d" not in _cards(resp.content.decode())


# ---------------------------------------------------------------------------
# The persistence -> domain adapter, at its one dangerous boundary.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "stored,label",
    [
        (1.0, "confirmed_official"),
        (0.6, "reported"),
        (0.3, "rumor"),
        (0.0, "reported"),
        # The band boundary. `round(0.99, 1)` is 1.0, so everything from 0.96
        # up used to come back "confirmed_official" — the label
        # `cadence._closing_soon` and the re-ping branch act on. The column's
        # CheckConstraint bounds the RANGE, not the band, so 0.99 is a legal
        # stored value; nothing else stopped an unconfirmed date from firing
        # a pre-deadline re-ping and printing a countdown.
        (0.99, "reported"),
        (0.96, "reported"),
        (0.95, "reported"),
        (0.8, "reported"),
        (None, "reported"),
        ("confirmed_official", "confirmed_official"),
    ],
)
def test_a_confidence_below_the_band_is_never_laundered_into_confirmed(stored, label):
    from crm.utils import _confidence_label

    assert _confidence_label(stored) == label


# ---------------------------------------------------------------------------
# 6. The CG tag on a real board: the key that explains it, and the shape of
#    the founder's own account, which is the board the bar was measured on.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_key_explains_the_tag_without_a_hover(client):
    """"CG" is two letters and says nothing on its own, so the board owes it
    an explanation a touchscreen can reach. `.net-legend-mini` at the foot of
    the Covered Firms panel is that explanation, the same contract "SP"
    already had, and it renders whether or not any card is tagged: a key that
    appears and disappears with the data is a key a reader cannot learn.
    """
    user = User.objects.create_user(email="key@example.com", password="x" * 14)
    # Deliberately a board with NO tagged card: the key still has to be there.
    firm = Firm.objects.create(slug="done-co", name="Done Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    for i in range(2):
        Contact.all_objects.create(
            user=user, name=f"Advocate {i}", firm=firm, warmth="advocate"
        )

    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()
    assert _tagged(body) == []

    legend = body[body.index('class="net-legend-mini"'):]
    legend = legend[: legend.index("</p>")]
    assert 'class="pill fc-cg"' in legend, "the key lost its CG swatch"
    assert "Coverage gap, nobody warm yet" in legend, (
        "the key shows a CG swatch with no words beside it, which explains "
        "nothing a reader did not already see on a card"
    )
    # Beside the entry it was built to match, not instead of it.
    assert "Sponsors visas" in legend
    assert legend.index("Sponsors visas") < legend.index("Coverage gap")


@pytest.mark.django_db
def test_the_tag_marks_a_minority_of_a_realistic_board(client):
    """The number behind the bar, as a test rather than as a comment.

    A tag every card wears says nothing, and that is not hypothetical: on
    the founder's live board every one of his 54 tiered firms carries SOME
    gap, because he has zero advocates anywhere. This is that board in
    miniature, at the same proportions the measurement found: banks he is on
    track for and cannot reach, banks he has emailed with no reply, off-track
    shops he tiered aspirationally, and tier-3 names.

    Nine firms, eight with a gap, three tagged. The exact set matters more
    than the fraction: the tagged three are the on-track firms he ranked and
    has no way into, and the untagged include an off-track firm with NOBODY
    at it, which is `track_fit` doing the job it was added for.
    """
    user = User.objects.create_user(
        email="board@example.com", password="x" * 14, tracks=["ib"]
    )
    spec = [
        # (name, tier, firm tracks, warmths)              exposure   tagged
        ("Empty Bank", 1, ["ib"], []),                       # 12      yes
        ("Silent Bank", 1, ["ib"], ["cold", "cold"]),        #  9      yes
        ("Second Empty", 2, ["ib"], []),                     #  8      yes
        ("Chatty Bank", 1, ["ib"], ["chatted"]),             #  6      no
        ("Empty Buyside", 1, ["pe"], []),                    #  6      no
        ("Second Silent", 2, ["ib"], ["cold"]),              #  6      no
        ("Third Empty", 3, ["ib"], []),                      #  4      no
        ("Nearly There", 1, ["ib"], ["advocate", "cold"]),   #  3      no
        ("Done Bank", 1, ["ib"], ["advocate", "advocate"]),  # not a gap
    ]
    for i, (name, tier, tracks, warmths) in enumerate(spec):
        firm = Firm.objects.create(slug=f"b{i}", name=name, tracks=tracks)
        UserFirm.all_objects.create(user=user, firm=firm, tier=tier)
        for n, w in enumerate(warmths):
            Contact.all_objects.create(
                user=user, name=f"{name} {n}", firm=firm, warmth=w
            )

    client.force_login(user)
    resp = client.get(reverse("crm:contact_list"))
    body = resp.content.decode()

    # Eight of nine firms carry a gap; a tag on all of them would be furniture.
    assert len(resp.context["gaps"]) == 8
    assert len(_HEAD.findall(_cards(body))) == 9
    assert sorted(_tagged(body)) == ["Empty Bank", "Second Empty", "Silent Bank"]
    # The off-track firm with nobody at it is NOT tagged, and the on-track
    # firm with nobody at it is. That difference is the whole `track_fit`
    # term, and it is what stopped eleven PE and consulting shops outranking
    # a tier-1 bank with a confirmed close on the founder's own board.
    by_name = {g["name"]: g for g in resp.context["gaps"]}
    assert by_name["Empty Buyside"]["state"] == coverage.OFF_TRACK
    assert by_name["Empty Buyside"]["exposure"] == 6
    assert by_name["Empty Bank"]["exposure"] == 12
