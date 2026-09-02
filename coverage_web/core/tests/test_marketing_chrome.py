"""The two marketing pages: contrast, motion, and the mobile comparison table.

Three defects from the 2026-09-01 UI audit, each measured on the rendered
page rather than argued about:

  * the landing marquee's monogram tiles ran `hsl(h 55% 31%)` on
    `hsl(h 52% 90%)`, which measures 4.3:1 at 11px and fails AA. The
    directory's copy of the same tile had already been corrected to 24%, so
    the landing page was the last surface still failing it, on a component
    that repeats about forty times across the strip.
  * `.kin-hero::after` is a 7.5s sheen declared `infinite` in coverage.css.
    Nothing about a marketing hero changes, so it pulled the eye back to the
    masthead every 7.5 seconds forever. Both pages now stop it after one
    pass, in their own style blocks, because coverage.css belongs to another
    pass.
  * the comparison table held a 480px min-width inside a 375px scroller, so
    the Pro column was off-screen with no fade, no shadow and no hint. Below
    560 the table stacks and every value carries its plan's name.
"""

from __future__ import annotations

import re

import pytest


def _style_block(body: str) -> str:
    return " ".join(re.findall(r"<style>(.*?)</style>", body, re.S))


@pytest.mark.django_db
def test_home_monogram_ink_clears_AA(client):
    """24%, matching directory/_styles.html, not the 4.3:1 31%."""
    css = _style_block(client.get("/").content.decode())

    assert "hsl(var(--hue, 210) 55% 24%)" in css
    assert "55% 31%" not in css, (
        "31% lightness on the 90% tint measures 4.3:1 at 11px, under AA"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/", "/pricing/"])
def test_hero_sheen_plays_once(client, url):
    """Both marketing pages, because both draw `.kin-hero`.

    Longhands, deliberately: coverage.css's reduced-motion block sets the
    `animation` shorthand to none, which sets the NAME to none, and nothing
    here touches the name. So the override cannot resurrect the animation for
    a reader who asked for no motion.
    """
    css = _style_block(client.get(url).content.decode())

    assert re.search(r"\.kin-hero::after\s*\{[^}]*animation-iteration-count:\s*1", css), (
        f"{url} should stop the hero sheen after one pass"
    )
    assert re.search(r"\.kin-hero::after\s*\{[^}]*animation-fill-mode:\s*both", css), (
        "without `both` the sheen snaps back to a visible resting position"
    )
    assert not re.search(r"\.kin-hero::after\s*\{[^}]*animation-name", css), (
        "the override must not set animation-name, or it would beat the "
        "prefers-reduced-motion rule that switches the animation off"
    )


@pytest.mark.django_db
def test_pricing_table_stacks_instead_of_scrolling_on_a_phone(client):
    css = _style_block(client.get("/pricing/").content.decode())

    stack = re.search(r"@media \(max-width: 560px\)\s*\{(.*)", css, re.S)
    assert stack, "the comparison table needs a phone breakpoint"
    block = stack.group(1)

    assert ".cmp { min-width: 0; }" in block, (
        "the 480px min-width is what forces the horizontal scroll; it has to "
        "go, or the stack scrolls too"
    )
    assert ".cmp-scroll { overflow-x: visible; }" in block
    assert 'content: "Free"' in block and 'content: "Pro"' in block, (
        "each stacked value must say which plan it belongs to, since the "
        "header row is no longer beside it"
    )
