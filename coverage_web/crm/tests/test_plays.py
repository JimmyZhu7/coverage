"""Today "plays": a dated world fact joined to the student's own people.

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

from crm.models import Contact, PlayDismissal
from crm.today import PLAYS_MAX, _cockpit_context, _plays
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
