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
TEMPLATES = REPO / "coverage_web" / "templates"

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


# ---------------------------------------------------------------------------
# §6.1: the case convention (D-15, 2026-09-02)
# ---------------------------------------------------------------------------
# The rule that stood here demanded sentence case and no page had ever used
# it. D-15 kept the code and rewrote the document. What a machine can check
# without guessing is NOT "is every label Title Case" -- a scan cannot tell a
# name from a sentence, and `smart_title` output would fail it -- but the two
# halves the rule actually turns on: that the three exceptions are real in the
# code, and that the labels the decision named still ship as it says they do.


def _spec_section_6() -> str:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("## 6. Voice & microcopy rules")
    return text[start:text.index("## 7.", start)]


def _css_block(selector: str) -> str:
    """The declaration body of one rule, keyed on its exact selector text."""
    css = CSS.read_text(encoding="utf-8")
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.DOTALL)
    assert match, f"coverage.css no longer has a `{selector}` rule"
    return match.group(1)


def test_the_case_rule_is_title_case_and_says_it_reverses_the_old_one():
    """Silence would read as drift, and the next reader would re-litigate a
    settled taste call. The paragraph has to name the reversal."""
    section = _spec_section_6()
    assert "**Title Case for names.**" in section
    assert "reverses the rule" in section
    assert "**Sentence case everywhere**" not in section


def test_the_case_rule_names_its_three_exceptions():
    section = _spec_section_6()
    assert "text-transform:" in section, "(a) uppercase comes from CSS"
    assert "`smart_title`" in section, "(b) data is cased by the filter"
    assert "sentence case" in section, "(c) prose stays prose"


def test_nav_badges_and_chips_are_uppercased_by_css_not_in_the_source():
    """Exception (a), both halves. The transform is what shouts; the source
    text stays readable, so turning the transform off leaves ordinary copy
    rather than a page of ALL CAPS."""
    assert "text-transform: uppercase" in _css_block(".site-nav a")
    assert "text-transform: uppercase" in _css_block(".pill, .chip, .prio")

    nav = re.search(
        r'<nav class="site-nav".*?</nav>',
        (TEMPLATES / "base.html").read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert nav, "base.html no longer renders a .site-nav"
    labels = re.findall(r">([A-Za-z][A-Za-z ]*)</a>", nav.group(0))
    assert len(labels) >= 5, labels
    for label in labels:
        assert label != label.upper(), f"nav types {label!r} in caps; CSS does that"


def test_the_labels_the_case_decision_named_still_ship():
    """The ones D-15 quotes, plus the three §6.5 lists. Literal strings in
    named files, so this cannot fire on data or on a heading whose shape it
    had to guess."""
    named = {
        # "Coverage Gaps" and the ledger's own "Add Contact" buttons went with
        # the gaps widget on 2026-09-02, at the founder's request: the gap
        # state is a CG chip on the firm card now. "Add Contact" still ships
        # on this page, but as `_pagehead.html`'s `action_label`, so it is no
        # longer a literal between two tags and cannot be read the way the
        # rest of this list is. "Covered Firms" replaces both: same page, same
        # Title Case rule, still a literal.
        "crm/contact_list.html": ["Covered Firms", "Log Touch"],
        "account/login.html": ["Welcome Back"],
        "core/home.html": ["Build My Queue"],
        "accounts/import.html": ["Upload &amp; Import"],
        "accounts/delete.html": ["Permanently Delete My Account"],
    }
    for path, labels in named.items():
        body = (TEMPLATES / path).read_text(encoding="utf-8")
        for label in labels:
            assert f">{label}<" in body, f"{path} no longer renders {label!r}"
