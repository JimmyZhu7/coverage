"""No control may be invisible and tappable at the same time.

`opacity: 0` hides a control from the eye. It does not hide it from a finger:
the box still occupies layout, still hit-tests, and still fires its handler.
On a device with a mouse that is fine, because the pointer reveals the control
before it can reach it. On a phone there is no hover — the reveal never
happens, and the first thing a tap does is fire the action.

That shipped. Every untracked role card on /opportunities/ drew a "Not for me"
button at `opacity: 0` with `pointer-events: auto`, 61.6x19, flush against the
right edge of the Save pill, with no `@media (hover: hover)` guard anywhere in
the repo. Measured in a touch context at 390x844, `elementFromPoint` at the
region's centre returned `.track-hide`, and a blind `touchscreen.tap()` there
fired `POST /opportunities/<id>/track/` with `status=dismiss` — the label only
faded in AFTER the role had been dismissed. 490 of 493 feed cards carried one.

These tests assert the rendered stylesheet, because that is the artifact the
browser actually gets.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db

# Controls that are deliberately hidden until a pointer reveals them. Add a
# class here only alongside the guard the tests below require.
REVEAL_ON_HOVER = ["track-hide"]

_STYLE_RE = re.compile(r"<style>(.*?)</style>", re.S)
# The body of an `@media (hover: hover)` block, brace-counted so a nested rule
# inside it does not truncate the match.
_HOVER_BLOCK_RE = re.compile(r"@media\s*\(\s*hover:\s*hover\s*\)\s*\{")


def _feed_css() -> str:
    html = Client().get("/opportunities/").content.decode()
    css = "\n".join(_STYLE_RE.findall(html))
    # Comments carry braces-free prose that would otherwise be swept into the
    # next rule's selector.
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _hover_guarded_blocks(css: str) -> list[str]:
    """Every `@media (hover: hover) { ... }` body in the stylesheet."""
    out = []
    for m in _HOVER_BLOCK_RE.finditer(css):
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        out.append(css[m.end():i - 1])
    return out


def _rules(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) for every flat rule in the stylesheet."""
    return [
        (m.group(1).strip(), m.group(2))
        for m in re.finditer(r"([^{}@]+)\{([^{}]*)\}", css)
    ]


@pytest.mark.parametrize("cls", REVEAL_ON_HOVER)
def test_a_hidden_control_is_only_hidden_where_a_pointer_can_hover(cls):
    """`opacity: 0` on a control is a promise that something will reveal it.
    Only a hover-capable pointer can keep that promise, so the hidden state
    has to live behind `@media (hover: hover)`. Unguarded, a phone gets the
    hidden half and never the reveal."""
    css = _feed_css()
    guarded = "\n".join(_hover_guarded_blocks(css))
    unguarded = css
    for block in _hover_guarded_blocks(css):
        unguarded = unguarded.replace(block, "")

    hides_outside = [
        sel for sel, body in _rules(unguarded)
        if cls in sel and re.search(r"(?<!-)opacity:\s*0\s*[;}]", body)
    ]
    assert hides_outside == [], (
        f".{cls} is hidden with opacity: 0 outside any `@media (hover: hover)` "
        f"guard ({hides_outside}). A coarse pointer never hovers, so on a phone "
        "that control is permanently invisible and permanently tappable."
    )
    assert re.search(rf"\.{cls}[^{{}}]*\{{[^{{}}]*opacity:\s*0", guarded), (
        f".{cls} declares no hidden state inside `@media (hover: hover)`. If "
        "the reveal-on-hover behaviour was dropped on purpose, drop it from "
        "REVEAL_ON_HOVER too."
    )


@pytest.mark.parametrize("cls", REVEAL_ON_HOVER)
def test_a_hidden_control_refuses_the_pointer_and_the_reveal_hands_it_back(cls):
    """Opacity is paint, not hit-testing: an `opacity: 0` box still swallows
    the tap that lands on it. `pointer-events: none` is what actually takes it
    out of reach — and the reveal rule MUST set it back to `auto`, or the
    desktop button that works today silently stops working."""
    css = _feed_css()
    hidden = [
        (sel, body) for block in _hover_guarded_blocks(css)
        for sel, body in _rules(block)
        if cls in sel and re.search(r"(?<!-)opacity:\s*0\s*[;}]", body)
    ]
    assert hidden, f"no hidden state found for .{cls}"
    for sel, body in hidden:
        assert re.search(r"pointer-events:\s*none", body), (
            f"`{sel}` hides .{cls} with opacity alone. The box still "
            "hit-tests, so a blind tap fires the action it is hiding."
        )

    revealed = [
        (sel, body) for block in _hover_guarded_blocks(css)
        for sel, body in _rules(block)
        if cls in sel and re.search(r"(?<!-)opacity:\s*1\s*[;}]", body)
    ]
    assert revealed, (
        f".{cls} is hidden inside the hover guard with nothing revealing it."
    )
    for sel, body in revealed:
        assert re.search(r"pointer-events:\s*auto", body), (
            f"`{sel}` reveals .{cls} but leaves `pointer-events: none` in "
            "force, so the control is visible and dead to the mouse."
        )


def test_the_reveal_covers_the_keyboard_as_well_as_the_mouse():
    """A control revealed only by `:hover` is unreachable by keyboard: you can
    tab to it, but it stays invisible while focused."""
    css = _feed_css()
    revealing = [
        sel for block in _hover_guarded_blocks(css)
        for sel, body in _rules(block)
        if "track-hide" in sel and re.search(r"(?<!-)opacity:\s*1\s*[;}]", body)
    ]
    assert any(":focus-visible" in sel for sel in revealing), (
        "nothing reveals .track-hide on keyboard focus; the reveal selectors "
        f"are {revealing}"
    )


def test_save_and_its_opposite_are_not_zero_gap_siblings():
    """Save and "Not for me" do opposite things and sat edge to edge: the
    dismiss box began at the exact pixel the Save pill ended (measured
    x282.4 for both at 390px). One slipped thumb was the whole margin."""
    css = _feed_css()
    track = [body for sel, body in _rules(css) if sel == ".track"]
    assert track, "the .track container rule is gone"
    gap = re.search(r"(?<!-)gap:\s*(\d+(?:\.\d+)?)px", track[0])
    assert gap and float(gap.group(1)) >= 4, (
        "`.track` needs a gap so the dismiss control cannot begin where the "
        f"Save pill ends; found {track[0].strip()!r}"
    )
