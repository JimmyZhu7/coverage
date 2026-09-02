"""The contact lifecycle fixes from the 2026-09-01 CRM audit: what the two
bulk park doors now ask and offer, what every override row now records, what
unarchive says about a still-parked contact, and what a board card carries.

Each test names the measurement it exists for. `transaction=True` throughout:
every park and un-park goes through `crm.services`, which opens its own
psycopg connection outside Django's test transaction and cannot see
uncommitted rows.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from crm.models import Contact, Touch
from crm.views import PARK_UNDO_SESSION_KEY, _contact_card
from directory.models import Firm

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="lifecycle@example.com", **kw):
    return get_user_model().objects.create_user(email=email, password="pw12345!", **kw)


def _contact(user, name, **kw):
    kw.setdefault("school_affiliation", True)
    return Contact.all_objects.create(user=user, name=name, **kw)


def _touch(user, contact, kind, *, days_ago=0):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel="email",
        ts=timezone.now() - timedelta(days=days_ago),
    )


def _quiet(user, n):
    """`n` contacts the engine will offer to park: written to, followed up,
    silent for weeks."""
    made = []
    for i in range(n):
        c = _contact(user, f"Quiet {i:02d}")
        _touch(user, c, "outreach", days_ago=40)
        _touch(user, c, "follow_up", days_ago=30)
        made.append(c)
    return made


def _bulk(client, ids, verb, scope=""):
    return client.post(
        reverse("crm:contacts_bulk"),
        {"ids": [str(i) for i in ids], "verb": verb, "scope": scope},
        follow=True,
    )


# ---------------------------------------------------------------------------
# L5. Every override says which door made it.
#
# 179 of 179 overrides on the founder's account said `source='manual'`, so
# "which tap parked these 44 people" was answerable only by regex over the
# human half of the note.
# ---------------------------------------------------------------------------
def _override_sources(user, contact):
    return set(
        Touch.all_objects.filter(
            user=user, contact=contact, kind="manual_override"
        ).values_list("source", flat=True)
    )


def test_the_network_boards_bulk_park_records_its_own_door(client):
    user = _user()
    a = _contact(user, "Ada")
    client.force_login(user)
    _bulk(client, [a.id], "park")
    assert _override_sources(user, a) == {"bulk"}


def test_todays_park_all_records_its_own_door(client):
    user = _user(weekly_touch_goal=14)
    made = _quiet(user, 8)
    client.force_login(user)
    client.post(reverse("crm:today_park_all"))
    for c in made:
        assert _override_sources(user, c) == {"park_all"}


def test_an_undo_and_a_cohort_unpark_are_distinguishable_afterwards(client):
    """Park then undo is two named acts on the ledger, not two anonymous
    "manual" ones — which is the whole difference between an audit trail you
    can query and one you have to read."""
    user = _user()
    a = _contact(user, "Ada")
    client.force_login(user)
    _bulk(client, [a.id], "park")
    client.post(
        reverse("crm:contacts_park_undo"),
        {"ids": str(a.id), "where": "network"}, follow=True,
    )
    assert _override_sources(user, a) == {"bulk", "undo"}


def test_a_single_hand_park_still_says_manual(client):
    """The degradation clause: "manual" keeps meaning exactly what it meant,
    a person with the door unrecorded. Nothing is retro-labelled."""
    from crm import services

    user = _user()
    a = _contact(user, "Ada")
    services.set_contact_state(
        user.id, a.id, thread_state="parked", note="Parked from the Today queue",
    )
    assert _override_sources(user, a) == {"manual"}


# ---------------------------------------------------------------------------
# L4. The confirm and the undo, on both doors.
#
# 44 contacts parked in one tap on 2026-09-01, 98 on 2026-08-10, neither door
# asking and neither offering a way back in place.
# ---------------------------------------------------------------------------
def test_park_all_names_its_count_before_the_tap(client):
    user = _user(weekly_touch_goal=14)
    _quiet(user, 8)
    client.force_login(user)
    page = client.get(reverse("crm:week")).content.decode()
    assert "Park all 8" in page
    # The number is in the dialog, not only in the flash afterwards.
    assert "hx-confirm=\"Park all 8?" in page


def test_the_board_asks_before_parking_more_than_one(client):
    """The bar's confirm used to guard Archive alone; park and the three
    region verbs submitted at once."""
    user = _user()
    _contact(user, "Ada")
    client.force_login(user)
    page = client.get(reverse("crm:contact_list")).content.decode()
    assert 'verb === "park"' in page
    assert "Stop following up with" in page


def test_park_all_hands_back_an_undo_offer_naming_the_ids_it_parked(client):
    user = _user(weekly_touch_goal=14)
    made = _quiet(user, 8)
    client.force_login(user)
    resp = client.post(reverse("crm:today_park_all"))
    body = resp.content.decode()
    assert "Parked 8 contacts." in body
    assert reverse("crm:contacts_park_undo") in body
    # EXACT ids, not "the newest cohort": undo is for the tap that just
    # happened, and must not sweep up a deliberate park from the same minute.
    for c in made:
        assert str(c.id) in body


def test_the_board_carries_its_undo_offer_across_the_redirect(client):
    """The Network board redirects, so there is no response body for the ids
    to ride in — they go through the session, and are POPPED on read so a
    refresh is not still offering to reverse a tap from ten minutes ago."""
    user = _user()
    a = _contact(user, "Ada")
    b = _contact(user, "Bo")
    client.force_login(user)

    page = _bulk(client, [a.id, b.id], "park").content.decode()
    assert "Stopped following up with 2 contacts." in page
    assert reverse("crm:contacts_park_undo") in page
    assert PARK_UNDO_SESSION_KEY not in client.session

    again = client.get(reverse("crm:contact_list")).content.decode()
    assert "Stopped following up with" not in again


def test_undo_restores_each_contact_to_the_state_their_warmth_implies(client):
    """Never one value applied to everyone: a park that swept up an advocate
    must not hand them back as a cold no-reply."""
    user = _user()
    cold = _contact(user, "Cold", warmth="cold")
    chatted = _contact(user, "Chatted", warmth="chatted")
    advocate = _contact(user, "Advocate", warmth="advocate")
    client.force_login(user)
    _bulk(client, [cold.id, chatted.id, advocate.id], "park")

    client.post(
        reverse("crm:contacts_park_undo"),
        {"ids": ",".join(str(c.id) for c in (cold, chatted, advocate)),
         "where": "network"},
        follow=True,
    )
    cold.refresh_from_db(); chatted.refresh_from_db(); advocate.refresh_from_db()
    assert cold.thread_state == "no_reply"
    assert chatted.thread_state == "chat_done"
    assert advocate.thread_state == "advocate"


def test_undo_writes_one_audit_row_per_contact_and_never_a_bulk_update(client):
    user = _user()
    rows = [_contact(user, f"Person {i}") for i in range(3)]
    client.force_login(user)
    _bulk(client, [c.id for c in rows], "park")
    client.post(
        reverse("crm:contacts_park_undo"),
        {"ids": ",".join(str(c.id) for c in rows), "where": "network"},
        follow=True,
    )
    for c in rows:
        assert Touch.all_objects.filter(
            user=user, contact=c, kind="manual_override", source="undo"
        ).count() == 1


def test_undo_cannot_reach_another_tenants_rows(client):
    mine = _user("mine@example.com")
    theirs = _user("theirs@example.com")
    not_ours = _contact(theirs, "Not Ours", thread_state="parked")
    client.force_login(mine)
    client.post(
        reverse("crm:contacts_park_undo"),
        {"ids": str(not_ours.id), "where": "network"}, follow=True,
    )
    not_ours.refresh_from_db()
    assert not_ours.thread_state == "parked"


def test_undo_leaves_a_contact_who_is_no_longer_parked_alone(client):
    """Somebody who has replied since is already back, and undo must not
    re-write state on a contact it did not park."""
    user = _user()
    a = _contact(user, "Ada", warmth="replied", thread_state="replied")
    client.force_login(user)
    client.post(
        reverse("crm:contacts_park_undo"),
        {"ids": str(a.id), "where": "network"}, follow=True,
    )
    a.refresh_from_db()
    assert a.thread_state == "replied"
    assert not Touch.all_objects.filter(user=user, contact=a).exists()


def test_undo_with_no_ids_is_rejected_outright(client):
    user = _user()
    client.force_login(user)
    assert client.post(reverse("crm:contacts_park_undo"), {"ids": ""}).status_code == 400


# ---------------------------------------------------------------------------
# L3. Accept-all shows its count and asks.
#
# 39 contacts created in one tap on 2026-09-01, 38 with no role, 23 with no
# region — and the number first appeared afterwards, in the emptied lane.
# ---------------------------------------------------------------------------
def test_accept_all_names_the_count_and_confirms(client):
    from capture.models import ContactProposal

    user = _user()
    firm = Firm.objects.create(slug="nb", name="North Bank", domains=["nb.example"])
    for i in range(3):
        ContactProposal.all_objects.create(
            user=user, name=f"Person {i}", email=f"p{i}@nb.example", firm=firm,
            evidence="Replied to your email", evidence_kind="reply_received",
        )
    client.force_login(user)
    page = client.get(reverse("crm:week")).content.decode()
    assert "Add all 3" in page
    assert "hx-confirm=\"Accept 3 contacts?" in page


def test_the_proposal_card_offers_a_role_and_a_region(client):
    from capture.models import ContactProposal

    user = _user()
    firm = Firm.objects.create(slug="nb", name="North Bank", domains=["nb.example"])
    p = ContactProposal.all_objects.create(
        user=user, name="Alex Banker", email="alex@nb.example", firm=firm,
        role_hint="Analyst",
        evidence="Replied to your email", evidence_kind="reply_received",
    )
    client.force_login(user)
    page = client.get(reverse("crm:week")).content.decode()
    assert f'id="prop-role-{p.id}"' in page
    # Pre-filled where the signature parser found anything — 1 of 137 accepted
    # proposals on live data, which is the measure of how little the From
    # header carries.
    assert 'value="Analyst"' in page
    assert f'id="prop-region-{p.id}"' in page


# ---------------------------------------------------------------------------
# L7. Unarchive says what it actually did.
#
# 9 of the founder's 41 archived rows are also parked, and the flash said
# "back on your board" for every one of them.
# ---------------------------------------------------------------------------
def test_unarchiving_a_parked_contact_says_they_are_still_parked(client):
    user = _user()
    a = _contact(user, "Ada", archived=True, thread_state="parked")
    client.force_login(user)
    page = client.post(
        reverse("crm:contact_unarchive", args=[a.id]), follow=True
    ).content.decode()
    assert "still parked" in page
    # And it does NOT reverse the park. `archived` and `thread_state` are two
    # decisions; un-parking here would silently undo the second one to make a
    # sentence about the first one true.
    a.refresh_from_db()
    assert a.thread_state == "parked"
    assert not Touch.all_objects.filter(
        user=user, contact=a, kind="manual_override"
    ).exists()


def test_unarchiving_an_unparked_contact_says_what_it_always_said(client):
    user = _user()
    a = _contact(user, "Ada", archived=True)
    client.force_login(user)
    page = client.post(
        reverse("crm:contact_unarchive", args=[a.id]), follow=True
    ).content.decode()
    assert "is back on your board." in page
    assert "still parked" not in page


# ---------------------------------------------------------------------------
# L8. The board card carries the state.
#
# 158 of 265 cards are parked and none of them said so: "Emailed, No Reply"
# mixed 92 active with 129 parked, "Advocate" showed 2 parked people as the
# whole advocate bench.
# ---------------------------------------------------------------------------
def test_the_card_context_carries_thread_state_and_a_parked_flag():
    user = _user()
    parked = _contact(user, "Parked", thread_state="parked")
    active = _contact(user, "Active", thread_state="no_reply")
    parked.last_touch_ts = None
    active.last_touch_ts = None

    parked_card = _contact_card(parked, tier=None, today=timezone.localdate())
    active_card = _contact_card(active, tier=None, today=timezone.localdate())

    assert parked_card["thread_state"] == "parked"
    assert parked_card["parked"] is True
    assert active_card["thread_state"] == "no_reply"
    assert active_card["parked"] is False


def test_every_card_the_board_renders_carries_it(client):
    """Through the real view, not just the helper: the value has to survive
    into the template's own context or the chip has nothing to read."""
    user = _user()
    _contact(user, "Parked", thread_state="parked")
    _contact(user, "Active")
    client.force_login(user)
    sections = client.get(reverse("crm:contact_list")).context["sections"]
    cards = [card for s in sections for card in s["cards"]]
    assert len(cards) == 2
    assert {c["parked"] for c in cards} == {True, False}
    assert all("thread_state" in c for c in cards)
