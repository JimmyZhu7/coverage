"""Controls take the control radius; only badges are allowed the full pill.

The shape system has two tokens with two jobs. `--r-badge` (999px) is the
pill, and its own comment reserves it for state — warmth chips, plan badges,
the things that REPORT. `--r-ctl` (10px) is the shape of a thing you press:
its comment says "buttons, inputs" outright.

Two of the loudest controls in the app ignored that and hardcoded the pill:
`.btn`, which is every button on all seven screens, and `.site-nav` with its
tabs. The cost was not only that fully-rounded buttons read consumer-fintech
rather than institutional — it was that spending the badge shape on actions
left nothing to tell an action apart from a status. On a queue row carrying a
warmth chip, a tier chip and three action buttons, every element was the same
shape.

These tests pin the two rules a future edit can quietly undo: the named
controls resolve to the control token, and the nav's inner tabs stay
concentric with the container that holds them (matching both at 10px against
4px of padding leaves a crescent of container visible at each tab corner).

Status chips are deliberately NOT covered here — they keep the pill, and a
test that forbade 999px file-wide would be asserting the opposite of the
design system.
"""

from __future__ import annotations

import re

import pytest


def _css(settings) -> str:
    return (settings.BASE_DIR / "static" / "css" / "coverage.css").read_text()


def _rule(css: str, selector: str) -> str:
    """The declaration block for one selector, as authored.

    Anchored at the line start so `.btn` cannot match `.btn-primary` or the
    `.filters .csel-btn` override further down the file.
    """
    match = re.search(
        rf"^{re.escape(selector)} \{{(.*?)\}}", css, re.S | re.M
    )
    assert match, f"{selector} is gone from coverage.css; re-check this guard"
    return match.group(1)


@pytest.mark.parametrize("selector", [".btn", ".site-nav", ".site-nav a"])
def test_the_named_controls_take_the_control_radius_not_the_pill(settings, selector):
    """A button and a nav tab are actions, and `--r-ctl` is the token that
    says so. Hardcoding 999px here spends the badge shape on a control."""
    block = _rule(_css(settings), selector)

    radius = re.search(r"border-radius:\s*([^;]+);", block)
    assert radius, f"{selector} no longer sets a border-radius at all"

    value = radius.group(1).strip()
    assert "999px" not in value, (
        f"{selector} is back to a hardcoded pill ({value}). The full round is "
        "--r-badge's, reserved for state chips; a control takes --r-ctl, so "
        '"this is an action" and "this is a status" stay distinguishable.'
    )
    assert "--r-ctl" in value, (
        f"{selector} sets border-radius: {value}, which is neither the token "
        "nor derived from it. Controls track --r-ctl so one edit moves them "
        "all together."
    )


def test_the_nav_tabs_stay_concentric_with_the_bar_around_them(settings):
    """Inner radius = outer radius - the gap between the boxes. Matched at
    10px each with 4px of padding, the filled active tab shows a crescent of
    container at every corner."""
    css = _css(settings)
    container = _rule(css, ".site-nav")
    tab = _rule(css, ".site-nav a")

    padding = re.search(r"padding:\s*(\d+)px;", container)
    assert padding, "the nav bar's padding is gone; re-check this guard"
    inset = int(padding.group(1))

    tab_radius = re.search(r"border-radius:\s*([^;]+);", tab).group(1).strip()
    assert re.fullmatch(
        rf"calc\(var\(--r-ctl\)\s*-\s*{inset}px\)", tab_radius
    ), (
        f"the nav bar insets its tabs by {inset}px but the tab radius is "
        f"{tab_radius}. It should be calc(var(--r-ctl) - {inset}px) so the "
        "tab's corners follow the container's instead of cutting inside them."
    )
