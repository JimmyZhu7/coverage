"""A contact's name on the Network board must not be cut off mid-word.

The four-across action lane gives each card 131px, and 67px of that is fixed
chrome — 24px of padding, 2px of border, a 13px checkbox, two 8px gaps and the
12px open-profile glyph. The name got the 64px left over, on one nowrap line
with an ellipsis, and on the live board every single visible card was cut:
"Lucas Silva" wanted 68px, "Sofia Reyes" 70px, "Sophia Kim" 71px, "Mia Garcia"
65px. The board read "Lucas Si…", "Sofia Re…", "Ethan P…" — an ellipsis
costing more characters than it saved, and the plainest "this is unfinished"
signal a page can send.

The fix is the doctrine this board already states on `.firm-card-name`: the
box gives way, not the text. The name wraps and the card grows.

Two things have to hold for that, and a later edit could quietly undo either:

1. The name must be allowed to wrap. Putting `white-space: nowrap` back is
   the obvious regression.
2. The lane must lay its cards out with FLEX. This one is not obvious and is
   the reason the first attempt at this fix shipped a worse bug. As a grid,
   the lane sized its rows from the card's intrinsic height contribution —
   which it measured at 23px against the 67-76px the card really occupies at
   131px wide — so the track resolved to the card's `min-height` floor of
   58px, and the wrapped second line was cut off by the card's own
   `overflow: hidden`. The horizontal truncation came straight back as a
   vertical one. `grid-auto-rows: minmax(58px, auto)`, `align-self: start`
   and dropping `overflow` were each tried against the live board; none of
   them moved the track. A flex line's cross size is measured after the
   item's main size is resolved, which is the order this needs.

Asserted against the rendered <style> block rather than the template source,
so the guard holds however the CSS gets in.
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
        email="net-truncation@example.com", password="x" * 14
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


def test_a_contact_name_may_wrap_rather_than_being_cut_mid_word():
    """64px of room and a 68px name is not a case for an ellipsis — every
    name on the board hit it, most of them by a handful of pixels."""
    block = _rule(_network_styles(), ".net-mini-name")

    assert "nowrap" not in block, (
        "the contact name is back on one nowrap line. In the four-across "
        "lane the name has 64px and the names on the live board need 65-71px, "
        'so this renders "Lucas Si…", "Sofia Re…", "Ethan P…". Let it wrap; '
        "the card grows to fit."
    )
    assert "ellipsis" not in block, (
        "the contact name truncates with an ellipsis again. A person's name "
        "is the one thing on this card a reader is scanning for."
    )


def test_the_action_lane_lets_a_card_grow_to_fit_its_name():
    """A grid pinned every card to the 58px min-height floor and clipped the
    wrapped line, turning the truncation vertical instead of fixing it."""
    block = _rule(_network_styles(), ".net-actions .net-minis")

    assert "display: flex" in block, (
        "the action lane is laid out with something other than flex. As a "
        "grid it measured the card's height contribution at 23px, sized its "
        "rows to the 58px min-height floor, and the card's overflow:hidden "
        "cut off the wrapped second line of the name — the same truncation, "
        "rotated 90 degrees. A flex line sizes its cross axis after the "
        "card's width is known, which is what makes the wrap safe."
    )
    assert "flex-wrap: wrap" in block, (
        "the action lane no longer wraps, so its cards run off in one row "
        "instead of forming the four-across grid the lane is sized for."
    )


def test_the_lane_lays_three_cards_across_and_the_basis_counts_its_gaps():
    """The column count is a density decision, and it moved five → four →
    three. Four was chosen to stop names ELLIPSISING; once the name was
    allowed to wrap instead, four became the thing that made wrapping
    expensive — 131px of card, 64px of name, and the ordinary names on this
    board measure 65-71px, so nearly every card wrapped and many came down in
    three pieces. Every card in a flex line stretches to the tallest, so one
    three-line name set the height of its whole row: 80/99/120/141px, 111.6px
    a contact on average. Reported directly: "these are too big."

    Three gives 177px of card and ~118px of name — one line is the normal
    case, two the exception — and the lane measures 43-59px a card. Density
    is height per contact, not cards per row.
    """
    block = _rule(_network_styles(), ".net-actions .net-mini")

    basis = re.search(r"flex:\s*0 0 calc\(\(100% - (\d+) \* var\(--s2\)\) / (\d+)\)", block)
    assert basis, (
        "the card's flex basis is no longer the explicit three-across "
        f"third: {block.strip()}"
    )
    gaps, columns = int(basis.group(1)), int(basis.group(2))
    assert columns == 3, (
        f"the lane now lays out {columns} across, not three. At four the card "
        "is 131px and the name gets 64px, which is less than the names on "
        "this board measure — every card wraps, some to three lines, and the "
        "row stretches to the tallest."
    )
    assert gaps == columns - 1, (
        f"{columns} cards have {columns - 1} gaps between them, but the basis "
        f"subtracts {gaps}; the last card in each row will not fit."
    )
