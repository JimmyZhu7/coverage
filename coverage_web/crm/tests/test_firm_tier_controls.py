"""Re-tiering a firm must not require a pointer.

The Network board's tier lanes were drag-and-drop only: cards carried
`draggable="true"` and the lanes listened for `dragstart`/`dragover`/`drop`.
HTML5 drag-and-drop does not fire on a touchscreen and has no keyboard
equivalent, so on a phone or tablet the whole board was read-only — a student
could see which tier a firm was on and had no way to change it. The page's own
hover text said "Drag a firm card here to set its tier", which is an
instruction a touch device cannot follow.

Every card carries a tier `<select>` now. The drag is still bound underneath
as the pointer shortcut, and both gestures write through the same
`crm:set_firm_tier` endpoint, which is also the endpoint Settings' Target
Firms board posts to — see `accounts/tests/test_settings_page.py` for that
board's half of the same guarantee.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from crm.models import UserFirm
from directory.models import Firm

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def student(db):
    return User.objects.create_user(email="tiers@example.com", password="x" * 14)


def _board(client, user) -> str:
    client.force_login(user)
    body = client.get(reverse("crm:contact_list")).content.decode()
    start = body.index('<div class="tier-section"')
    return body[start : body.index("</section>", start)]


def _cards(board: str) -> list[str]:
    """One string per firm card, split on the card's own opening tag."""
    return re.split(r'(?=<div class="firm-card )', board)[1:]


def test_every_card_carries_a_tier_picker(client, student):
    """Not "most" and not "the ones with a gap": the picker rides the verb
    row, which used to render only for firms still short of their advocate
    target. A covered firm is exactly as much in need of a way to change its
    tier as an uncovered one."""
    for n, tier in enumerate((1, 2, 3)):
        firm = Firm.objects.create(slug=f"f{n}", name=f"Firm {n}", regions=["us"])
        UserFirm.all_objects.create(user=student, firm=firm, tier=tier)
    cards = _cards(_board(client, student))
    assert len(cards) == 3
    for card in cards:
        assert 'class="fc-tier"' in card, card


def test_the_picker_offers_exactly_the_lanes_that_exist(client, student):
    """A picker that can name a tier the board doesn't render would move a
    card into a lane with no grid to append it to. Options come off
    `tier_sections`, the same list that builds the lanes, so the two cannot
    drift. Unranked is the case that matters: the view only builds that lane
    when something is already sitting in it."""
    firm = Firm.objects.create(slug="only", name="Only Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=firm, tier=2)

    board = _board(client, student)
    lanes = set(re.findall(r'<div class="tier-section" data-tier="([^"]*)"', board))
    options = set(re.findall(r'<option value="([^"]*)"', board))
    assert lanes == {"1", "2", "3"}
    assert options == lanes

    # Park a second firm off the tiers and the Unranked lane appears — and
    # with it, the option that moves a card back into it.
    parked = Firm.objects.create(slug="parked", name="Parked Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=parked, tier=None)
    board = _board(client, student)
    lanes = set(re.findall(r'<div class="tier-section" data-tier="([^"]*)"', board))
    options = set(re.findall(r'<option value="([^"]*)"', board))
    assert lanes == {"1", "2", "3", ""}
    assert options == lanes


def test_the_picker_shows_the_tier_the_card_is_actually_on(client, student):
    """A control that always reads "Tier 1" is a control that lies about two
    thirds of the board."""
    firm = Firm.objects.create(slug="third", name="Third Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=firm, tier=3)
    card = _cards(_board(client, student))[0]
    assert '<option value="3" selected>Tier 3</option>' in card
    assert "selected" not in card.replace('<option value="3" selected>', "")


def test_the_picker_is_labelled_by_firm_and_spells_the_tier_out(client, student):
    """Two facts a screen reader needs and can only get from two places: the
    box's `aria-label` names WHICH firm, and the option text names the tier,
    because the option is what gets read back when the value changes. An
    abbreviated "T3" would have saved pixels and dropped half the sentence."""
    firm = Firm.objects.create(slug="named", name="Named Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=firm, tier=1)
    card = _cards(_board(client, student))[0]
    assert 'aria-label="Tier for Named Co"' in card
    assert ">Tier 1</option>" in card


def test_the_drag_still_works(client, student):
    """The picker is the route that always works, not a replacement. Anyone
    holding a mouse keeps the faster gesture."""
    firm = Firm.objects.create(slug="drag", name="Drag Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=firm, tier=1)
    client.force_login(student)
    body = client.get(reverse("crm:contact_list")).content.decode()
    assert '<div class="firm-card kin-reveal" draggable="true"' in body
    for handler in ("dragstart", "dragover", "drop"):
        assert f'"{handler}"' in body


def test_the_hints_no_longer_prescribe_a_gesture_touch_cannot_send(client, student):
    """The board used to tell a phone to drag. Whatever the hover text says
    now, it must not name dragging as THE way to change a tier."""
    firm = Firm.objects.create(slug="hint", name="Hint Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=firm, tier=1)
    client.force_login(student)
    body = client.get(reverse("crm:contact_list")).content.decode()
    for banned in (
        "Drag a firm card here to set its tier",
        "Drag a card between lanes to change a firm's tier",
        "Drag to another tier.",
    ):
        assert banned not in body


def test_the_endpoint_takes_what_the_picker_sends(client, student):
    """Every option value the picker can send, posted straight at
    `crm:set_firm_tier` — including the empty string the Unranked option
    carries, which the view reads as tier=None."""
    firm = Firm.objects.create(slug="post", name="Post Co", regions=["us"])
    UserFirm.all_objects.create(user=student, firm=firm, tier=1)
    client.force_login(student)
    url = reverse("crm:set_firm_tier")
    for value, expected in (("2", 2), ("3", 3), ("", None), ("1", 1)):
        resp = client.post(url, {"firm": firm.id, "tier": value})
        assert resp.status_code == 204, value
        assert UserFirm.all_objects.get(user=student, firm=firm).tier == expected
