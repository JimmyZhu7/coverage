"""The Network board's contact mini-cards are a PICK-LIST, not a card gallery.

Reported directly, looking at the live board: "these are too big." Nine
contacts filled most of a screen. Measured at 1280px before this guard
existed, every card in the action lane:

    width 131px, heights 80 / 99 / 120 / 141px, average 111.6px

Four different heights in one grid, because at 131px the name came down in
two or three pieces and a flex line stretches every card to its tallest
member. After: 177px wide, heights 43 / 59px, average 52.3px — a 53% cut in
the average card and 58% off the tallest one.

At 375px it was worse and had never been looked at: the four-across basis
applied unconditionally, phone included, so each card was 67px wide and
245-536px tall — one letter-stack per contact. After: a flat 43px, every
card, name and firm on one line each.

These assertions are deliberately about the RULES rather than the rendered
pixels: a Django test client has no layout engine, so the CSS is what can be
checked here. Each one names the height it is protecting.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = pytest.mark.django_db

NETWORK = "/app/contacts/"


def _network_styles() -> str:
    user = get_user_model().objects.create_user(
        email="net-density@example.com", password="x" * 14
    )
    client = Client()
    client.force_login(user)
    html = client.get(NETWORK).content.decode()
    blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    assert blocks, "the Network page no longer renders a <style> block"
    return "\n".join(blocks)


def _rule(css: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)} \{{(.*?)\}}", css, re.S)
    assert match, f"{selector} is gone from the Network page; re-check this guard"
    return match.group(1)


def test_the_card_keeps_its_vertical_padding_tight():
    """8px of padding top and bottom on a 33px stack of text is a card
    gallery's proportion, not a pick-list's — that is what the tightened
    pass fixed, and this guard is against sliding all the way back to it.

    The floor itself moved off its tightest value on request: the first
    pass read as cramped rather than compact, and got a small deliberate
    bump back up (padding var(--s1) -> 6px, floor 38px -> 44px). This test
    pins a ceiling under `var(--s2)` (8px) / 58px — the ORIGINAL
    proportions this board moved away from — not the exact tightened
    numbers, which are allowed to breathe a little without failing a test
    that was written to catch a regression, not to freeze a constant."""
    block = _rule(_network_styles(), ".net-mini")

    padding = re.search(r"padding:\s*([^;]+);", block)
    assert padding, f"the card no longer states its padding: {block.strip()}"
    vertical = padding.group(1).split()[0]
    assert vertical != "var(--s2)", (
        f"the card's vertical padding is back at the full var(--s2) (8px) "
        "the tightened pass moved away from — 8px of air above and below "
        "a 33px stack of text is a card gallery's proportion again."
    )

    floor = re.search(r"min-height:\s*(\d+)px", block)
    assert floor, f"the card no longer states a min-height floor: {block.strip()}"
    assert int(floor.group(1)) <= 50, (
        f"the card's min-height floor is back up at {floor.group(1)}px, "
        "past the small bump this was asked for — a one-line name plus a "
        "one-line firm is 33px of text; a floor this far above it is "
        "padding the box past what it holds, not just giving it room to "
        "breathe."
    )


def test_the_firm_line_is_one_line_and_may_ellipsise():
    """The NAME wraps and must never be cut (see
    test_network_name_truncation.py). The firm is the opposite case: it is
    context for the name, not a second thing to read, and at two lines every
    "Morgan Stanley" on the board bought a whole extra row of card height to
    repeat what its first line already said."""
    block = _rule(_network_styles(), ".net-mini-firm")

    assert "nowrap" in block, (
        "the firm line wraps again. Two lines of firm is the single biggest "
        "avoidable chunk of card height on this board."
    )
    assert "line-clamp" not in block, (
        "the firm line is back on a multi-line clamp; one line is the budget."
    )
    assert "ellipsis" in block, (
        "a one-line firm with no ellipsis just clips. It must truncate "
        "honestly — and the markup carries the full name in a title= so the "
        "cut-off half is still readable."
    )


def test_the_firm_name_stays_readable_when_the_line_truncates(client):
    """A one-line firm may ellipsise, which is only honest if the whole
    string is still recoverable. The CARD's own title= carries the action
    reason, not the firm, so the firm line needs a title of its own."""
    from datetime import timedelta

    from django.utils import timezone

    from crm.models import Contact, Touch, UserFirm
    from directory.models import Firm

    user = get_user_model().objects.create_user(
        email="net-firmtitle@example.com", password="x" * 14
    )
    firm = Firm.objects.create(slug="sixth-street", name="Sixth Street Partners")
    contact = Contact.all_objects.create(
        user=user, name="Mia Garcia", firm=firm, warmth="cold"
    )
    # The relevance gate won't queue a contact at a firm the student isn't
    # chasing, and the cadence engine needs a reason to call her overdue.
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    Touch.all_objects.create(
        user=user, contact=contact, kind="outreach", channel="email",
        ts=timezone.now() - timedelta(days=30),
    )

    client.force_login(user)
    body = client.get(NETWORK).content.decode()

    assert re.search(
        r'class="net-mini-firm[^"]*"\s+title="Sixth Street Partners"', body
    ), (
        "the firm line on a contact mini-card no longer carries its own "
        "title=, so an ellipsised firm name has nowhere left to be read."
    )


def test_the_open_profile_link_keeps_a_24px_touch_target():
    """The one part of the card that OPENS rather than SELECTS. The glyph is
    small on purpose; the TARGET must not be. It used to be a 16px link grown
    to 24x24 by an `::after { inset: -4px }` that overhung the card — capped
    there because the overhang would otherwise reach into the next card's own
    open link across an 8px gap.

    It is now the card's whole right edge: 24px wide by the full height of
    the card. Strictly more target, and none of it outside the card — which
    is what let the row gap come down to 4px without two cards' targets
    meeting."""
    css = _network_styles()
    block = _rule(css, ".net-mini-open")

    width = re.search(r"width:\s*(\d+)px", block)
    assert width and int(width.group(1)) >= 24, (
        f"the open-profile link is {width and width.group(1)}px wide, under "
        "WCAG 2.5.8 AA's 24px floor."
    )
    assert "align-self: stretch" in block, (
        "the open-profile link no longer stretches to the card's height, so "
        "its target is back to the height of the glyph."
    )
    assert ".net-mini-open::after" not in css, (
        "the old overhanging ::after hit area is back. It bleeds 4px past the "
        "card on every side, and the lane's row gap is 4px now — two stacked "
        "cards' targets would overlap, so a tap could open the wrong "
        "contact's profile."
    )


def test_a_phone_gets_one_card_per_row():
    """The three-across basis is a desktop measurement. Left unconditional it
    gave a 375px phone 67px-wide cards 245-536px tall — a letter-stack per
    contact. One per row instead: name and firm get a whole line each and the
    card lands flat at 43px."""
    css = _network_styles()
    narrow = re.search(
        r"@media \(max-width: 560px\) \{\s*\.net-actions \.net-mini \{([^}]*)\}", css
    )
    assert narrow, (
        "the phone breakpoint for the contact mini-card is gone. Without it "
        "the three-across basis applies at 375px too, where a third of the "
        "lane is under 100px and the name breaks across three and four lines."
    )
    assert "flex: 0 0 100%" in narrow.group(1), (
        f"the phone card is no longer full width: {narrow.group(1).strip()}"
    )


def test_a_phone_actually_shows_the_contacts_in_a_lane():
    """Stacked, the panel and the page swap roles: the PAGE scrolls, so
    pinning the panel to a fixed height and splitting it four ways left each
    populated lane 32px. Measured at 375px: panel 266px, the two EMPTY lanes
    taking 99px each, the two real ones 32px — a header and not one contact
    visible, on every phone."""
    css = _network_styles()
    stacked = re.search(r"@media \(max-width: 900px\) \{(.*?)\n  \}", css, re.S)
    assert stacked, "the Network board's stack breakpoint is gone"
    body = stacked.group(1)

    assert ".net-panel { height: auto; }" in body, (
        "the stacked panel is back on a fixed height, which it then divides "
        "between four lanes that no longer have a scroll of their own."
    )
    assert ".net-actions .net-group { flex: 0 0 auto; }" in body, (
        "the stacked lanes are back to splitting the panel into equal "
        "quarters, so an empty lane takes as much room as a full one."
    )
