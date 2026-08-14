"""The warmth meter's colour must say the same thing its length says.

`.meter-fill` carries the cold→advocate gradient. A CSS gradient's positioning
area defaults to the painted element's own box, and that box used to be
`width: var(--to)` — so the entire four-stop ramp was squeezed into whatever
slice was filled and the unpositioned last stop landed on the fill's right
edge at EVERY level. Measured live at 1280x900 before the fix: contact 484
(cold) filled 204px of an 818px track and contact 480 (chatted) filled 612px
of the same track, and both bars terminated in the identical
rgb(143, 58, 69) — `--w-advocate-bar`. A cold contact got an advocate-red tip
printed two lines under "Cold · No reply yet".

The fix keeps the fill full width and reveals it with a clip, so the ramp's
positioning area is the whole track at every level and through the whole
900ms growth, and puts the stops on the same quartiles the fill uses
(`_warmth_pct`) so a bar ends in exactly its own warmth colour.

These tests read the shipped stylesheet and re-derive that invariant from
`WARMTH_ORDER` and `_warmth_pct` rather than restating the numbers: put the
ramp back on the fill's own box, or slide a stop off the quartile grid, and
they fail.
"""

from __future__ import annotations

import re
from pathlib import Path

from crm.utils import WARMTH_ORDER, _warmth_pct

CSS = Path(__file__).resolve().parents[2] / "static" / "css" / "coverage.css"


def _rule(selector: str) -> str:
    """The declaration block of the first top-level rule for `selector`."""
    text = CSS.read_text()
    match = re.search(
        r"(?m)^" + re.escape(selector) + r"\s*\{(.*?)\}", text, re.S
    )
    assert match, f"{selector} is gone from coverage.css"
    return match.group(1)


def _keyframes(name: str) -> str:
    text = CSS.read_text()
    match = re.search(r"@keyframes\s+" + re.escape(name) + r"\s*\{(.*?\})\s*\}", text, re.S)
    assert match, f"@keyframes {name} is gone from coverage.css"
    return match.group(1)


def _stops(block: str) -> list[tuple[str, float]]:
    """The ramp as [(colour var, position %)] in source order."""
    gradient = re.search(r"linear-gradient\((.*?)\);", block, re.S)
    assert gradient, ".meter-fill lost its gradient"
    body = " ".join(gradient.group(1).split())
    out: list[tuple[str, float]] = []
    for part in body.split(",")[1:]:  # [0] is the 90deg angle
        found = re.search(r"var\((--[\w-]+)\)\s*(?:([\d.]+)%)?", part)
        assert found, f"unreadable gradient stop: {part!r}"
        out.append((found.group(1), float(found.group(2)) if found.group(2) else -1.0))
    return out


def test_ramp_is_measured_against_the_track_not_the_fill():
    """The gradient's positioning area must be the whole track.

    A `width: var(--to)` fill makes the ramp's 100% land on the fill's tip,
    which is what painted advocate red at the end of a cold bar.
    """
    block = _rule(".meter-fill")
    width = re.search(r"width:\s*([^;]+);", block)
    assert width and width.group(1).strip() == "100%", (
        "the gradient element must span the whole track; found "
        f"width: {width.group(1).strip() if width else 'none'}"
    )
    assert "clip-path: inset(0 calc(100% - var(--to)) 0 0)" in " ".join(block.split()), (
        "the fill level must be revealed by a clip, so the ramp keeps the "
        "track as its positioning area"
    )


def test_growth_animates_the_clip_so_the_ramp_holds_mid_flight():
    """--from/--to stay the contract, but they must drive the clip.

    Animating width instead would re-squeeze the ramp on every frame of the
    900ms growth even if the resting state were correct.
    """
    frames = " ".join(_keyframes("warmthgrow").split())
    assert "width" not in frames, "warmthgrow must not animate width any more"
    assert "clip-path: inset(0 calc(100% - var(--from)) 0 0)" in frames
    assert "clip-path: inset(0 calc(100% - var(--to)) 0 0)" in frames


def test_every_level_ends_in_its_own_warmth_colour():
    """Walk the real ramp at the real fill percentages.

    For each warmth level the bar's right edge sits at `_warmth_pct(level)`
    of the track, so the colour sampled there must be that level's bar
    colour — not the next rung's, and not advocate red for all four.
    """
    stops = _stops(_rule(".meter-fill"))
    assert all(pos >= 0 for _, pos in stops), (
        "every stop needs an explicit position, or the last one floats to "
        "the fill's edge again"
    )
    by_pos = dict((pos, var) for var, pos in stops)
    for level in WARMTH_ORDER:
        tip = float(_warmth_pct(level))
        assert tip in by_pos, (
            f"{level} fills to {tip}% but no gradient stop sits there, so its "
            "bar ends mid-interpolation on someone else's colour"
        )
        assert by_pos[tip] == f"--w-{level}-bar", (
            f"a {level} bar ends at {tip}% of the ramp, where the colour is "
            f"{by_pos[tip]} instead of --w-{level}-bar"
        )


def test_reduced_motion_still_lands_on_the_full_level():
    """The reduced-motion path skips the animation, not the fill level."""
    text = " ".join(CSS.read_text().split())
    reduced = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\*, \*::before", text, re.S
    )
    assert reduced, "the reduced-motion block moved"
    assert "clip-path: inset(0 calc(100% - var(--to)) 0 0)" in reduced.group(1), (
        "reduced motion must pin the clip to --to; leaving the old width "
        "override there would collapse the bar to full width"
    )
