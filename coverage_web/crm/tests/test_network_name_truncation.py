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


    # The four-across action lane this test's siblings once guarded
    # (`.net-actions .net-minis` laid out with flex so a wrapped name's
    # second line was never clipped; `.net-actions .net-mini` fixed at a
    # three-across basis) is gone along with the panel it belonged to — its
    # queue moved to Today, see crm/views.py::contact_list. The one claim
    # that survives it, and that this file still guards, is the more
    # general one above: a name wraps rather than losing characters to an
    # ellipsis, wherever `.net-mini-name` is used (today, the Unplaced
    # panel's per-firm cards).
