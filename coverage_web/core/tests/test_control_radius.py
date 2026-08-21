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

Squaring those two exposed the next seam, in the Opportunities filter bar. All
six of its controls were pills, each for its own reason: four `.csel-btn`s
re-rounded by a `.filters` override, the `.cmulti-btn` drawn to match them, and
the search box re-rounded again in a template `<style>` block. None of those
overrides argued for the shape; every one of them was carrying forward the look
of the native `<select>`s the custom dropdown replaced. So the bar disagreed
with `.btn` everywhere the two met, and once the stylesheet alone was swept it
would have disagreed with ITSELF — squared dropdowns beside a rounded search
box. The `.skip-link` was the same hardcode in a place almost nobody sees,
which is not a reason to shape it differently: for a keyboard user it is the
first control on the page.

These tests pin the rules a future edit can quietly undo: the named controls
resolve to the control token, the filter bar stops overriding it, and the
nav's inner tabs stay concentric with the container that holds them (matching
both at 10px against 4px of padding leaves a crescent of container visible at
each tab corner).

Two things are deliberately NOT swept, and the last test pins them as
exclusions rather than oversights. Status chips keep the pill — a test that
forbade 999px file-wide would be asserting the opposite of the design system.
And `.subnav a` keeps it too: a breadcrumb is a text link whose round is the
shape of the wash behind the words, not a button's edge, and those same rules
are what the `.subnav.scope-tabs` segmented capsule inherits its segments'
shape from.
"""

from __future__ import annotations

import re

import pytest


def _css(settings) -> str:
    return (settings.BASE_DIR / "static" / "css" / "coverage.css").read_text()


def _rule(css: str, selector: str, indent: str = "") -> str:
    """The declaration block for one selector, as authored.

    Anchored at the line start so `.btn` cannot match `.btn-primary` or the
    `.filters .csel-btn` override further down the file. `indent` is how the
    rule is laid out in ITS file, and is part of that anchor: coverage.css
    writes top-level rules at column 0 and indents the media-query overrides
    beneath them, so requiring `""` here is what keeps `.site-nav` from
    resolving to its own narrow-screen override further down.
    """
    match = re.search(
        rf"^{indent}{re.escape(selector)} \{{(.*?)\}}", css, re.S | re.M
    )
    assert match, f"{selector} is gone; re-check this guard"
    return match.group(1)


@pytest.mark.parametrize(
    "selector",
    [".btn", ".site-nav", ".site-nav a", ".skip-link", ".csel-btn", ".cmulti-btn"],
)
def test_the_named_controls_take_the_control_radius_not_the_pill(settings, selector):
    """A button, a nav tab, a skip link and a dropdown trigger are all things
    you press, and `--r-ctl` is the token that says so. Hardcoding 999px here
    spends the badge shape on a control."""
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


def test_the_filter_bar_no_longer_re_rounds_the_dropdown_it_restyles(settings):
    """`.filters .csel-btn` restyles the base control — softer border, a lift,
    a wider tap target — and used to re-round it to 999px as well, carrying
    forward the shape of the native selects the component replaced. The
    override should now say nothing about radius and let the base token
    through, so the filter bar and the `.btn` row above it are one family."""
    block = _rule(_css(settings), ".filters .csel-btn")

    radius = re.search(r"border-radius:\s*([^;]+);", block)
    assert radius is None, (
        f"the filter bar sets border-radius: {radius.group(1).strip()} on "
        ".csel-btn again. The base rule already gives it --r-ctl; an override "
        "here can only take the shape AWAY from the token, which is how the "
        "pill survived the first sweep."
    )


def test_the_filter_bars_own_stylesheet_does_not_re_pill_its_controls(settings):
    """The filter bar keeps some of its styling in a template `<style>` block,
    and that is where the last pill hid: `.filters select, .filters
    input[type="search"]` and the mobile `Filters` disclosure each re-rounded
    to 999px where a grep of coverage.css could not see them. The search box
    and the disclosure are the two controls the custom dropdown does NOT
    replace, so on Opportunities they were the ones left rounded after the
    stylesheet was swept — one on desktop, one on the phone."""
    styles = (
        settings.BASE_DIR / "templates" / "directory" / "_styles.html"
    ).read_text()

    for selector in ('.filters select, .filters input[type="search"]',
                     ".filters-more > summary"):
        block = _rule(styles, selector, indent="  ")
        radius = re.search(r"border-radius:\s*([^;]+);", block)
        if radius is None:
            continue  # inherits --r-ctl from the base input/select rule
        assert "999px" not in radius.group(1), (
            f"{selector} hardcodes a pill again. It is drawn as one of the "
            "filter bar's controls — same border, surface and lift — so it "
            "takes --r-ctl with the rest of them; leaving it round puts two "
            "shape families back in one row."
        )


def test_the_breadcrumb_and_its_segmented_tabs_keep_the_pill_on_purpose(settings):
    """The sweep stops at `.subnav`. Its links are text on the page
    background, where the round is the shape of the wash behind the words
    rather than a button's edge — and the `.subnav.scope-tabs` capsule, which
    does real segmented-control work, inherits its segments' shape from those
    same rules. Squaring here would square the capsule with it."""
    css = _css(settings)

    for selector in ('.subnav a', '.subnav a[aria-current="page"], .subnav strong'):
        radius = re.search(r"border-radius:\s*([^;]+);", _rule(css, selector))
        assert radius, f"{selector} no longer sets a border-radius at all"
        assert radius.group(1).strip() == "999px", (
            f"{selector} was squared to {radius.group(1).strip()}. This one is "
            "an exclusion, not an oversight: it shapes a text link's wash, and "
            "the scope-tabs segments read their radius from it."
        )

    segments = _rule(css, ".subnav.scope-tabs a")
    assert "border-radius" not in segments, (
        "the scope-tab segments now set their own radius. They are meant to "
        "inherit the pill from .subnav a — if that is changing, the capsule "
        "and the breadcrumb have become two components and should say so."
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
