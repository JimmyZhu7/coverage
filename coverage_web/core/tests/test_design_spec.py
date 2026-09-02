"""The design spec has to describe the product that ships.

`docs/design-spec.md` §0 was marked "committed" and was wrong in five binding
places: it forbade `@font-face` while three families are vendored, forbade dark
mode while a full second palette ships, forbade rounded pills while the pill IS
the badge shape, forbade gradients while four surfaces use them, and put `.page`
at 960px while it is 1440. Every "spec drift" finding filed against those five
was the document being stale, not the CSS being wrong, and the cost was real:
agents were sent to "fix" a shipping system to match a July draft.

A prose document cannot be kept honest by good intentions, so the one statement
in §0 that a machine can check is checked here. If someone adds a fourth family
to the stylesheet, or drops one, this test fails until §0 says so.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "docs" / "design-spec.md"
CSS = REPO / "coverage_web" / "static" / "css" / "coverage.css"

# The fenced block in §0 that carries the typeface list, keyed off its own
# marker comment so a later edit cannot move it without noticing this test.
_SPEC_BLOCK = re.compile(
    r"/\* §0 typefaces.*?\*/(?P<body>.*?)```", re.DOTALL
)
_FAMILY = re.compile(r'font-family:\s*"([^"]+)"')


def _spec_section_0() -> str:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("## 0. Design direction")
    end = text.index("## 1", start)
    return text[start:end]


def _spec_families() -> set[str]:
    block = _SPEC_BLOCK.search(_spec_section_0())
    assert block, "§0 no longer carries the marked typeface block"
    return set(_FAMILY.findall(block.group("body")))


def _css_families() -> set[str]:
    """Every family declared by an `@font-face` rule in the shared stylesheet."""
    css = CSS.read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*\{(.*?)\}", css, re.DOTALL)
    assert faces, "coverage.css declares no @font-face rules"
    return {m for face in faces for m in _FAMILY.findall(face)}


def test_spec_names_exactly_the_shipped_font_families():
    assert _spec_families() == _css_families()


def test_spec_does_not_carry_the_five_reversed_prohibitions():
    """The literal strings the 2026-09-01 audit greps for.

    Kept as strings rather than a summary because the audit's acceptance
    criterion is a `git grep`, and a test that checks something looser would
    let the exact wording creep back.
    """
    text = SPEC.read_text(encoding="utf-8").lower()
    for banned in ("system fonts only", "light mode only", "no gradients"):
        assert banned not in text, f"design-spec.md still says {banned!r}"


def test_every_page_in_section_5_is_specced_or_says_it_is_not():
    """No silent gaps: a page either points at a spec or says it has none.

    "No spec" is a statement of fact in this document. The alternative — an
    index row that just trails off — is how the July per-page blocks survived
    two months after their pages were rewritten.
    """
    text = SPEC.read_text(encoding="utf-8")
    section = text[text.index("## 5. Per-page layout specs"):text.index("## 6.")]
    rows = [
        line for line in section.splitlines()
        if line.startswith("|") and not line.startswith("|---") and "| Spec |" not in line
    ]
    assert len(rows) >= 14, "the §5 index lost rows"
    for row in rows:
        spec_cell = row.split("|")[3].strip()
        assert spec_cell, f"empty spec cell: {row}"

    # The seven pages the audit named as genuinely unspecced.
    assert section.count("No spec") >= 7
