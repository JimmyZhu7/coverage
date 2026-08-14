"""The "you are here" month must sit over its own column.

Two surfaces draw twelve or twenty-four months as bars with a label centred
under each: the cycle band (`directory/_cycle_band.html`, rendered on the feed
and under Settings' cycle picker) and the calendar's month rail
(`crm/calendar.html`). Both mark the current month with a dot rather than a
colour, so "today lives here" and "you are reading this" never have to be told
apart by hue.

Both drew that dot as `content: " •"` INSIDE the label. Both centre a
shrink-to-fit label box over the bar, so the dot could not move the box — it
could only move the letters inside it. Measured at vw=1280 before the fix:

    cycle band  Aug `.cycband-m b` box 32.0px vs 19.2px of text,
                text-centre minus bar-centre = -6.4px; Sep..Jul all 0
    month rail  Aug `.mrail-lab` box 32.5px vs 19.5px of text,
                text-centre minus bar-centre = -6.5px; the other 23 all 0

The one month the marker exists to draw the eye to was the only one whose
label did not sit over its bar. At 390px the band was worse still: the column
is ~29px wide, so " •" wrapped to a second line and AUG rode up out of the
label row entirely.

The invariant these tests hold: a now-marker pseudo-element may not
participate in the label's inline flow. Take it out of flow and the label's
box is the width of its letters again, so the letters stay centred.
"""

from __future__ import annotations

import pathlib
import re

# Every stylesheet that draws a month rail or a cycle band.
_ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCES = [
    _ROOT / "static" / "css" / "coverage.css",
    _ROOT / "templates" / "crm" / "calendar.html",
]

_RULE_RE = re.compile(r"([^{}@]+)\{([^{}]*)\}")


def _rules(path: pathlib.Path) -> list[tuple[str, str]]:
    text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in _RULE_RE.finditer(text)]


def _now_markers() -> list[tuple[pathlib.Path, str, str]]:
    """(file, selector, declarations) for every rule that injects a marker on
    the current month via a pseudo-element."""
    out = []
    for path in SOURCES:
        for sel, body in _rules(path):
            if "is-now" in sel and "::after" in sel and "content:" in body:
                out.append((path, sel, body))
    return out


def test_both_surfaces_still_mark_the_current_month():
    """Guard against the fix being 'applied' by deleting the marker: the dot
    is the colour-independent signal for where today is."""
    markers = _now_markers()
    files = {p.name for p, _, _ in markers}
    assert files == {"coverage.css", "calendar.html"}, (
        f"a surface lost its 'you are here' marker; found {files}"
    )


def test_the_marker_never_widens_the_label_it_marks():
    """Set inline, the marker is part of the label's own line box. Since both
    parents centre a shrink-to-fit label over the bar, widening the box cannot
    move the box — it can only shift the glyphs inside it by half the width
    the marker added. `position: absolute` is what takes it out of flow."""
    markers = _now_markers()
    assert markers, "no now-marker rules found at all"
    for path, sel, body in markers:
        assert re.search(r"position:\s*absolute", body), (
            f"{path.name}: `{sel}` injects the current-month marker into the "
            "label's inline flow. That widens the shrink-to-fit box the "
            "column centres, pushing the month's letters off its own bar — "
            "and at phone widths it wraps the marker onto a second line."
        )


def test_an_absolute_marker_is_positioned_against_its_own_label():
    """`position: absolute` resolves against the nearest positioned ancestor.
    Without `position: relative` on the label, the dot would escape to the
    page and land somewhere unrelated."""
    for path, sel, _ in _now_markers():
        base = sel.replace("::after", "").strip()
        positioned = [
            s for s, body in _rules(path)
            if base in s and re.search(r"position:\s*relative", body)
        ]
        assert positioned, (
            f"{path.name}: `{sel}` is absolutely positioned but nothing gives "
            f"`{base}` a `position: relative` containing block."
        )


def test_the_marker_carries_no_leading_space():
    """A leading space in `content` is a width the label would still pay for
    even with the glyph out of flow — and it was the space, not the bullet,
    that gave the phone's band a line break to take."""
    for path, sel, body in _now_markers():
        content = re.search(r"content:\s*\"([^\"]*)\"", body)
        assert content, f"{path.name}: `{sel}` has no quoted content"
        assert content.group(1) == content.group(1).strip(), (
            f"{path.name}: `{sel}` sets content {content.group(0)!r}; the "
            "padding belongs in margin, not in the string."
        )
