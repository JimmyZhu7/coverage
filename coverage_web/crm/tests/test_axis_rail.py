"""Each Fit Score axis is three rows, not four.

`.axis` declares `grid-template-columns: 1fr auto`, and the `auto` track is
there for the score: `.val` is mono with tabular-nums, which only pays off in
a column. But `.val` is emitted after the full-width `.bar` in
_contact_live.html, and grid auto-placement will not walk backwards into a
row it has already passed — so the score landed on a fourth row in column 1
and the second track resolved to 0px.

Measured live on /app/contacts/484/ at 1280px before the fix, identical on
all eight axes: gridTemplateColumns "278px 0px", four row tracks
"19.19px 6px 19.19px 18.19px", axis height 68.6px, and `.val` at x=941 —
the same x as `.name`, 29px below it. A stranger read "Depth" / a grey bar /
"0.0" / "0 chats evidenced (level 0/3)".

These tests pin the placement and the row count against the real markup
order, so re-shuffling the spans or dropping the placement fails them.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "static" / "css" / "coverage.css"
TEMPLATE = (
    Path(__file__).resolve().parents[2] / "templates" / "crm" / "_contact_live.html"
)


def _rule(selector: str) -> str:
    text = CSS.read_text()
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{(.*?)\}", text, re.S)
    assert match, f"{selector} is gone from coverage.css"
    return " ".join(match.group(1).split())


def test_the_second_track_is_actually_occupied():
    """The `auto` column must hold the score, or it collapses to 0px."""
    axis = _rule(".axis")
    assert "grid-template-columns: 1fr auto" in axis, axis
    val = _rule(".axis .val")
    assert "grid-column: 2" in val, (
        "without an explicit column the score auto-places into column 1 and "
        "the auto track resolves to 0px"
    )
    assert "grid-row: 1" in val, (
        "without an explicit row the score lands under the bar instead of "
        "beside the axis name"
    )


def test_the_score_still_needs_placing_given_the_markup_order():
    """The bug is a function of source order — keep the test honest about it.

    If `.val` ever moves before `.bar` the placement becomes belt-and-braces
    rather than load-bearing, and this test should be the thing that says so.
    """
    html = TEMPLATE.read_text()
    order = re.findall(r'<span class="(name|bar|val|meta)"', html)
    assert order, "the axis spans are gone from _contact_live.html"
    # Walk each name -> ... -> val run and check the bar comes between them.
    runs = []
    current: list[str] = []
    for cls in order:
        if cls == "name" and current:
            runs.append(current)
            current = []
        current.append(cls)
    runs.append(current)
    assert len(runs) >= 8, f"expected the eight fit-score axes, found {len(runs)}"
    for run in runs:
        if "val" in run and "bar" in run:
            assert run.index("bar") < run.index("val"), run
            break
    else:  # pragma: no cover - only if the markup loses bar or val entirely
        raise AssertionError("no axis emits both a bar and a val any more")


def test_the_axis_is_three_rows_of_content():
    """name+val, bar, meta — the bar and meta span, the score does not."""
    assert "grid-column: 1 / -1" in _rule(".axis .bar")
    assert "grid-column: 1 / -1" in _rule(".axis .meta")
    assert "grid-column: 1 / -1" not in _rule(".axis .val"), (
        "the score must not span the row; that is what put it on its own line"
    )
