"""The Today board: firm-level facts worth acting on today.

Two kinds, one lane and one card anatomy — a confirmed dated firm event
joined to the student's own people there, and a tiered firm where they know
nobody. See `crm.today._board`.

THE MEASURED GAP. The founder's Today page rendered zero cards the week he
sent ~50 personalised coffee-chat requests — nothing was DUE, and the cadence
queue is the page's only content source. The page already knew J.P. Morgan
closes in 3 days and he has 6 people there; the ingredients never met. Every
case here pins one rule from that fix: the cap, the anti-nag dismissal (keyed
on the FACT, not the card), the sourcing case (a live date with nobody
there), the confirmed-only bar, honest counts, and tenant isolation.

`transaction=True`, matching `test_today.py` and `test_relevance.py`: some
paths this module exercises indirectly (`_cockpit_context`) go through
`crm.services`, which opens its own connection outside Django's test
transaction.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.models import Contact, PlayDismissal, Touch, UserFirm
from crm.today import (
    BOARD_COVERAGE_MAX, BOARD_COVERAGE_MAX_BUSY, COVERAGE_DISMISSAL_DATE,
    COVERAGE_EVENT_KIND, PLAYS_MAX, _cockpit_context, _coverage_cards, _plays,
)
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="plays@example.com", **kw):
    kw.setdefault("weekly_touch_goal", 14)
    return get_user_model().objects.create_user(email=email, password="pw12345!", **kw)


def _firm(slug, name=None):
    return Firm.objects.create(slug=slug, name=name or slug.title())


def _confirmed(firm, *, today, days, event_kind="app_close", cycle="SA 2028"):
    return FirmDate.objects.create(
        firm=firm, cycle=cycle, region="us", event_kind=event_kind,
        date=today + timedelta(days=days), confidence=1.0,
    )


def _contact(user, firm, *, name, warmth="cold", archived=False):
    return Contact.all_objects.create(
        user=user, firm=firm, name=name, warmth=warmth, archived=archived,
        school_affiliation=True,
    )


def _target(user, firm, *, tier):
    return UserFirm.all_objects.create(user=user, firm=firm, tier=tier)


def _login_and_get(client, user) -> str:
    client.force_login(user)
    return client.get(reverse("crm:week")).content.decode()


# ---------------------------------------------------------------------------
# The founder's own scenario, as a single end-to-end sanity check.
# ---------------------------------------------------------------------------
def test_the_founders_own_scenario():
    """J.P. Morgan closes soonest with 6 people (1 replied, 2 cold, 3
    parked); Goldman's insight programme opens next with 5 new cold
    contacts; BlackRock closes last with nobody there. Three cards, in that
    order, matching the brief's own numbers exactly."""
    user = _user()
    today = timezone.localdate()

    jpm = _firm("jpm", "J.P. Morgan")
    _confirmed(jpm, today=today, days=3, event_kind="app_close")
    _contact(user, jpm, name="Replied One", warmth="replied")
    _contact(user, jpm, name="Cold One", warmth="cold")
    _contact(user, jpm, name="Cold Two", warmth="cold")
    _contact(user, jpm, name="Parked One", warmth="cold", archived=True)
    _contact(user, jpm, name="Parked Two", warmth="replied", archived=True)
    _contact(user, jpm, name="Parked Three", warmth="cold", archived=True)

    gs = _firm("gs", "Goldman Sachs")
    _confirmed(gs, today=today, days=5, event_kind="insight_open")
    for i in range(5):
        _contact(user, gs, name=f"New Batch {i}", warmth="cold")

    blk = _firm("blk", "BlackRock")
    _confirmed(blk, today=today, days=6, event_kind="app_close")

    plays = _plays(user, today)
    assert [p["firm"].slug for p in plays] == ["jpm", "gs", "blk"]

    jpm_play = plays[0]
    assert jpm_play["total"] == 6
    breakdown = {row["key"]: row["count"] for row in jpm_play["breakdown"]}
    assert breakdown == {"replied": 1, "cold": 2, "parked": 3}
    assert jpm_play["sourcing"] is False

    gs_play = plays[1]
    assert gs_play["total"] == 5
    assert {row["key"]: row["count"] for row in gs_play["breakdown"]} == {"cold": 5}

    blk_play = plays[2]
    assert blk_play["total"] == 0
    assert blk_play["breakdown"] == []
    assert blk_play["sourcing"] is True


# ---------------------------------------------------------------------------
# Rule 1: at most 3, ever.
# ---------------------------------------------------------------------------
def test_at_most_three_plays_render_even_with_more_candidates():
    user = _user()
    today = timezone.localdate()
    firms = [_firm(f"f{i}") for i in range(5)]
    for i, firm in enumerate(firms):
        _confirmed(firm, today=today, days=i + 1)
        _contact(user, firm, name=f"Person {i}")

    plays = _plays(user, today)
    assert len(plays) == PLAYS_MAX == 3
    # The three SOONEST, not an arbitrary three.
    assert [p["firm"].slug for p in plays] == ["f0", "f1", "f2"]


# ---------------------------------------------------------------------------
# Rule 3: dismissal is remembered against the FACT, not the card.
# ---------------------------------------------------------------------------
def test_a_dismissed_fact_never_returns():
    user = _user()
    today = timezone.localdate()
    firm = _firm("nomura")
    fd = _confirmed(firm, today=today, days=3)
    _contact(user, firm, name="Someone There")

    assert [p["firm"].slug for p in _plays(user, today)] == ["nomura"]

    PlayDismissal.all_objects.create(
        user=user, firm=firm, event_kind=fd.event_kind, date=fd.date,
    )
    assert _plays(user, today) == []


def test_the_same_fact_with_a_new_date_plays_again():
    """A re-scrape moves the close date IN PLACE (see FirmDate.history) —
    the dismissal must not survive the tuple changing."""
    user = _user()
    today = timezone.localdate()
    firm = _firm("nomura")
    fd = _confirmed(firm, today=today, days=3)
    _contact(user, firm, name="Someone There")

    PlayDismissal.all_objects.create(
        user=user, firm=firm, event_kind=fd.event_kind, date=fd.date,
    )
    assert _plays(user, today) == []

    fd.date = today + timedelta(days=10)
    fd.save(update_fields=["date"])

    plays = _plays(user, today)
    assert [p["firm"].slug for p in plays] == ["nomura"]
    assert plays[0]["date"] == fd.date


def test_dismissing_one_fact_leaves_room_for_the_next_candidate():
    """Filtered before the cap: a dismissed fact must not cost the day one of
    the at-most-3 slots."""
    user = _user()
    today = timezone.localdate()
    firms = [_firm(f"f{i}") for i in range(4)]
    facts = []
    for i, firm in enumerate(firms):
        fd = _confirmed(firm, today=today, days=i + 1)
        facts.append(fd)
        _contact(user, firm, name=f"Person {i}")

    PlayDismissal.all_objects.create(
        user=user, firm=firms[0], event_kind=facts[0].event_kind, date=facts[0].date,
    )
    plays = _plays(user, today)
    assert len(plays) == 3
    assert [p["firm"].slug for p in plays] == ["f1", "f2", "f3"]


# ---------------------------------------------------------------------------
# Rule 2 / 4: counts must equal what renders.
# ---------------------------------------------------------------------------
def test_breakdown_counts_sum_to_the_rendered_total():
    user = _user()
    today = timezone.localdate()
    firm = _firm("citi")
    _confirmed(firm, today=today, days=2)
    _contact(user, firm, name="A", warmth="advocate")
    _contact(user, firm, name="B", warmth="chatted")
    _contact(user, firm, name="C", warmth="replied")
    _contact(user, firm, name="D", warmth="cold")
    _contact(user, firm, name="E", warmth="cold", archived=True)

    play = _plays(user, today)[0]
    assert play["total"] == 5
    assert sum(row["count"] for row in play["breakdown"]) == play["total"]
    assert {row["key"]: row["count"] for row in play["breakdown"]} == {
        "advocate": 1, "chatted": 1, "replied": 1, "cold": 1, "parked": 1,
    }


# ---------------------------------------------------------------------------
# A live date with nobody there is still a play.
# ---------------------------------------------------------------------------
def test_a_firm_with_no_contacts_still_plays_as_a_sourcing_card():
    user = _user()
    today = timezone.localdate()
    firm = _firm("blackrock")
    _confirmed(firm, today=today, days=5)

    plays = _plays(user, today)
    assert len(plays) == 1
    assert plays[0]["total"] == 0
    assert plays[0]["sourcing"] is True
    assert plays[0]["breakdown"] == []


# ---------------------------------------------------------------------------
# Confirmed dates only.
# ---------------------------------------------------------------------------
def test_an_unconfirmed_date_never_plays():
    user = _user()
    today = timezone.localdate()
    firm = _firm("rumor-bank")
    FirmDate.objects.create(
        firm=firm, cycle="SA 2028", region="us", event_kind="app_close",
        date=today + timedelta(days=3), confidence=0.3,
    )
    _contact(user, firm, name="Someone")
    assert _plays(user, today) == []


def test_a_past_confirmed_date_never_plays():
    user = _user()
    today = timezone.localdate()
    firm = _firm("past-bank")
    _confirmed(firm, today=today, days=-1)
    _contact(user, firm, name="Someone")
    assert _plays(user, today) == []


# ---------------------------------------------------------------------------
# Tenant isolation.
# ---------------------------------------------------------------------------
def test_contacts_and_dismissals_never_cross_tenants():
    a = _user(email="a@example.com")
    b = _user(email="b@example.com")
    today = timezone.localdate()
    firm = _firm("shared-bank")
    fd = _confirmed(firm, today=today, days=3)
    _contact(a, firm, name="A's Contact")
    _contact(b, firm, name="B's Contact One")
    _contact(b, firm, name="B's Contact Two")

    play_a = _plays(a, today)[0]
    play_b = _plays(b, today)[0]
    assert play_a["total"] == 1
    assert play_b["total"] == 2

    # A dismisses; B's identical fact must still play.
    PlayDismissal.all_objects.create(
        user=a, firm=firm, event_kind=fd.event_kind, date=fd.date,
    )
    assert _plays(a, today) == []
    assert len(_plays(b, today)) == 1


# ---------------------------------------------------------------------------
# Wired into the cockpit context and the dismiss endpoint.
# ---------------------------------------------------------------------------
def test_plays_key_is_present_on_the_cockpit_context():
    user = _user()
    today = timezone.localdate()
    firm = _firm("wells")
    _confirmed(firm, today=today, days=2)
    _contact(user, firm, name="Someone")

    ctx = _cockpit_context(user)
    assert len(ctx["plays"]) == 1
    assert ctx["plays"][0]["firm"].slug == "wells"


def test_the_dismiss_endpoint_writes_the_fact_and_the_card_disappears(client):
    user = _user()
    today = timezone.localdate()
    firm = _firm("dismiss-bank")
    fd = _confirmed(firm, today=today, days=2)
    _contact(user, firm, name="Someone")

    client.force_login(user)
    assert len(_cockpit_context(user)["plays"]) == 1

    resp = client.post(
        reverse("crm:play_dismiss"),
        {"firm": firm.id, "event_kind": fd.event_kind, "date": fd.date.isoformat()},
    )
    assert resp.status_code == 200
    assert PlayDismissal.objects.for_user(user).count() == 1
    assert _cockpit_context(user)["plays"] == []


def test_the_dismiss_endpoint_rejects_a_malformed_fact(client):
    user = _user()
    client.force_login(user)
    resp = client.post(reverse("crm:play_dismiss"), {"firm": "not-a-number"})
    assert resp.status_code == 400
    assert PlayDismissal.objects.for_user(user).count() == 0


# ---------------------------------------------------------------------------
# A play card must not count people its own link cannot show.
#
# `directory:firm_detail`'s "My Network here" section excludes archived
# contacts (directory.views._my_network_at), same exclusion as the Network
# board. A card whose breakdown says "1 parked" and whose only button points
# at that page was promising a person the destination hides — this is that
# same "counts must equal what renders" bug, instance six.
# ---------------------------------------------------------------------------
def test_live_total_excludes_parked_contacts_the_firm_page_cannot_show(client):
    """`live_total` is what `directory:firm_detail` will actually render —
    it must match that page's own count, not the card's headline `total`
    (which still counts parked people in, on purpose, as a fact worth
    knowing)."""
    user = _user()
    today = timezone.localdate()
    firm = _firm("citi")
    _confirmed(firm, today=today, days=2)
    _contact(user, firm, name="Live One", warmth="cold")
    _contact(user, firm, name="Live Two", warmth="chatted")
    _contact(user, firm, name="Parked One", warmth="cold", archived=True)

    play = _plays(user, today)[0]
    assert play["total"] == 3
    assert play["live_total"] == 2

    client.force_login(user)
    resp = client.get(reverse("directory:firm_detail", args=[firm.slug]))
    assert resp.context["my_total"] == play["live_total"]
    shown_names = {p["c"].name for p in resp.context["my_contacts"]}
    assert shown_names == {"Live One", "Live Two"}
    assert "Parked One" not in shown_names


def test_parked_link_shows_the_people_the_firm_page_hides(client):
    """The fix: the breakdown's "parked" count and the card's fallback CTA
    both point at `crm:contact_archived` scoped to this firm, and THAT page
    shows exactly the people `firm_detail` will not."""
    user = _user()
    today = timezone.localdate()
    firm = _firm("nomura-parked")
    _confirmed(firm, today=today, days=2)
    _contact(user, firm, name="Parked Here", warmth="cold", archived=True)

    play = _plays(user, today)[0]
    assert play["live_total"] == 0
    breakdown = {row["key"]: row["count"] for row in play["breakdown"]}
    assert breakdown == {"parked": 1}

    client.force_login(user)
    firm_page = client.get(reverse("directory:firm_detail", args=[firm.slug]))
    assert firm_page.context["my_total"] == 0
    assert list(firm_page.context["my_contacts"]) == []

    archived_page = client.get(reverse("crm:contact_archived"), {"firm": firm.id})
    assert archived_page.status_code == 200
    assert [c.name for c in archived_page.context["contacts"]] == ["Parked Here"]
    assert archived_page.context["firm"] == firm

    # The Today template itself renders the fixed link, not the old
    # firm_detail-only one, for a play whose only people are parked.
    cockpit_html = client.get(reverse("crm:week")).content.decode()
    assert f"contacts/archived/?firm={firm.id}" in cockpit_html


# ---------------------------------------------------------------------------
# The board's OTHER half: coverage holes.
# ---------------------------------------------------------------------------
# THE MEASURED GAP, part two. Dated facts are as bursty as the cadence queue,
# because they come off the same calendar the whole market shares: the founder
# has four confirmed future firm dates in total, and after the last one passes
# the dated half of this lane is empty for the rest of the cycle. Meanwhile 25
# of his 54 tiered firms have nobody at them at all — a standing backlog the
# cadence engine can never surface, because there is nobody there to schedule.
#
# Every case below pins one of the three rules that keep it from being filler:
# no new judgment (it is `crm.coverage.rank_gaps`, the Network board's own
# ranking), NO_CONTACTS only, and tiered firms only.
# ---------------------------------------------------------------------------
def test_a_tier_1_firm_with_nobody_at_it_is_todays_work():
    user = _user()
    today = timezone.localdate()
    _target(user, _firm("centerview", "Centerview"), tier=1)

    cards = _cockpit_context(user)["plays"]
    assert [(c["firm"].name, c["kind"]) for c in cards] == [
        ("Centerview", "coverage")]
    card = cards[0]
    assert card["label"] == "Tier 1 target"
    assert card["cta_label"] == "Add someone"
    assert f"firm={card['firm'].slug}" in card["cta_href"]
    # No clock it did not earn.
    assert card["date"] is None
    assert card["when"] == ""
    assert card["urgent"] is False


def test_the_tier_the_student_set_orders_the_cards():
    """The user's own tiering is the only statement of priority the product
    has. `rank_gaps` multiplies by it, so tier 1 leads tier 2 leads tier 3
    with no tie-break of this module's own invention."""
    user = _user()
    _target(user, _firm("t3", "Cee Firm"), tier=3)
    _target(user, _firm("t1", "Aay Firm"), tier=1)
    _target(user, _firm("t2", "Bee Firm"), tier=2)

    cards = _coverage_cards(user, timezone.localdate(),
                            skip_firm_ids=set(), limit=9)
    assert [c["firm"].name for c in cards] == [
        "Aay Firm", "Bee Firm", "Cee Firm"]


def test_an_untiered_firm_is_never_carded():
    """No tier is no claim to care. `rank_gaps` skips these outright and this
    lane is not allowed to invent a priority the student never stated."""
    user = _user()
    _target(user, _firm("unranked", "Unranked Bank"), tier=None)
    assert _coverage_cards(user, timezone.localdate(),
                           skip_firm_ids=set(), limit=9) == []


def test_a_firm_with_contacts_is_the_queues_problem_not_the_boards():
    """RULE 2, and the one that keeps this from becoming the cold-contact
    flood. `rank_gaps` also ranks all_cold / no_advocate firms — 22 of the
    founder's 40 — and every one of those is a firm where somebody already
    exists for the cadence engine to schedule. Carding them here as well
    would be the page asking twice about the same person."""
    user = _user()
    today = timezone.localdate()
    firm = _firm("has-people", "Has People")
    _target(user, firm, tier=1)
    _contact(user, firm, name="Only Cold One", warmth="cold")

    assert _coverage_cards(user, today, skip_firm_ids=set(), limit=9) == []


def test_one_firm_one_card_when_it_has_both_a_date_and_a_hole():
    """BlackRock on the founder's board: a confirmed close in three days AND
    nobody there. It gets the dated card, because the date is the stronger
    fact, and the coverage half must not card it again."""
    user = _user()
    today = timezone.localdate()
    firm = _firm("blackrock", "BlackRock")
    _target(user, firm, tier=2)
    _confirmed(firm, today=today, days=3)

    cards = _cockpit_context(user)["plays"]
    assert [(c["firm"].name, c["kind"]) for c in cards] == [
        ("BlackRock", "date")]
    assert cards[0]["sourcing"] is True


def test_a_loud_page_gets_one_coverage_card_and_a_quiet_one_gets_two():
    """The standing backlog is equally true either way; what changes is how
    much of it belongs in front of somebody who already has a morning's work
    queued. Nothing here decides whether a firm is empty."""
    user = _user()
    for i, name in enumerate(["Aay", "Bee", "Cee", "Dee"]):
        _target(user, _firm(f"gap{i}", name), tier=1)

    quiet = _cockpit_context(user)
    assert not quiet["lanes"], "precondition: no cadence work"
    assert len(quiet["plays"]) == BOARD_COVERAGE_MAX == 2

    # Now give the cadence engine something to plan.
    other = _firm("otherfirm", "Other Firm")
    for i in range(6):
        c = _contact(user, other, name=f"Due {i:02d}")
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=timezone.now() - timedelta(days=20),
        )
    busy = _cockpit_context(user)
    assert busy["lanes"], "precondition: this queue has planned work"
    assert len(busy["plays"]) == BOARD_COVERAGE_MAX_BUSY == 1


def test_dismissing_a_coverage_card_is_permanent_for_that_firm(client):
    """The anti-nag rule, in the shape a gap with no clock needs. A dated
    play un-dismisses when the date moves because that is a new fact; a
    coverage hole has no date, so one Dismiss means "stop asking about this
    firm" and the escape hatch is putting somebody there."""
    user = _user()
    keep = _firm("keepme", "Keep Me")
    drop = _firm("dropme", "Drop Me")
    _target(user, drop, tier=1)
    _target(user, keep, tier=1)

    assert len(_cockpit_context(user)["plays"]) == 2

    client.force_login(user)
    resp = client.post(reverse("crm:play_dismiss"), {
        "firm": drop.id,
        "event_kind": COVERAGE_EVENT_KIND,
        "date": COVERAGE_DISMISSAL_DATE.isoformat(),
    })
    assert resp.status_code == 200
    assert PlayDismissal.all_objects.filter(
        user=user, firm=drop, event_kind=COVERAGE_EVENT_KIND).count() == 1

    assert [c["firm"].name for c in _cockpit_context(user)["plays"]] == [
        "Keep Me"]


def test_a_dismissed_coverage_card_does_not_eat_a_slot():
    """Filtered BEFORE the cap, same rule as the dated half. Dismissing the
    worst gap must promote the next one, not leave a hole where it was."""
    user = _user()
    firms = [_firm(f"cap{i}", f"Firm {i}") for i in range(4)]
    for f in firms:
        _target(user, f, tier=1)
    assert len(_cockpit_context(user)["plays"]) == 2

    PlayDismissal.all_objects.create(
        user=user, firm=firms[0], event_kind=COVERAGE_EVENT_KIND,
        date=COVERAGE_DISMISSAL_DATE,
    )
    after = _cockpit_context(user)
    assert len(after["plays"]) == 2, (
        "a dismissed gap must not occupy one of the two slots")
    assert firms[0].name not in [c["firm"].name for c in after["plays"]]


def test_a_covered_board_gets_no_coverage_cards_and_says_so():
    """THE WHOLE TEST OF THIS WORK. A genuinely empty day is still allowed to
    be empty: with somebody at every tiered firm the lane renders nothing and
    the quiet header comes back. The page never manufactures a card."""
    user = _user()
    firm = _firm("covered", "Covered Bank")
    _target(user, firm, tier=1)
    # Five, so the setup seeds are gated off too (SEED_NETWORK_FLOOR): this
    # student is running an account, not still building one.
    for i in range(5):
        c = _contact(user, firm, name=f"Somebody {i}")
        Touch.all_objects.create(
            user=user, contact=c, kind="outreach", channel="email",
            ts=timezone.now() - timedelta(days=1),
        )

    ctx = _cockpit_context(user)
    assert ctx["seeds"] == []
    assert ctx["plays"] == []
    assert not ctx["lanes"]
    assert ctx["quiet"] is True
    assert ctx["quiet_line"].startswith("Quiet on the cadence.")


def test_a_page_with_board_cards_never_also_says_youre_all_caught_up(client):
    """The founder's own page, this morning: three board cards above a
    full-width "You're all caught up" panel, because the quiet header's copy
    of the rule knew about `plays` and the empty states below it did not."""
    user = _user()
    _target(user, _firm("contradiction", "Contradiction Bank"), tier=1)

    body = _login_and_get(client, user)
    assert "Your board" in body
    assert "You're all caught up." not in body
    assert "Done for today." not in body


def test_coverage_cards_are_scoped_to_their_tenant():
    mine = _user("mine@example.com")
    theirs = _user("theirs@example.com")
    _target(theirs, _firm("theirbank", "Their Bank"), tier=1)
    assert _cockpit_context(mine)["plays"] == []
