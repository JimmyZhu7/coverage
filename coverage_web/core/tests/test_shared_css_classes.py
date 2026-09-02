"""Nothing in the shared stylesheet styles something that no longer exists.

`static/css/coverage.css` is loaded on every page by every visitor, so a rule
that styles a retired component is bytes everyone pays for and nobody uses.
Worse, it is a lie about the product: a 2026-09-01 audit read six such rules
as live components and asked why they were inconsistent with the rest of the
system. They were not inconsistent. They were gone.

Six were swept when this test was written: `.net-offboard` (the off-board
escape hatches, removed from Network), `code.addr`/`.capture-addr` (the
capture pages, folded into Settings), `.pill.tag-cat` (the firm-category
tag), `.markers-facts` (the second markers row), `.net-actions` (a stale
entry in the rise-in selector list), `.pill.spon-known`/`.pill.spon-none` and
the four `.fact-chip.verdict-*` rules (both replaced by the shared fact chip
and the ledger row's own verdict).

The interesting part is the allowlist. Some classes genuinely never appear
in a template as a literal, because a VIEW builds the suffix: `warmth-{{ w }}`,
`band-{{ band }}`, `conf-{{ level }}`, `prio-{{ n }}`. Those are the
class-name contract §0 of the design spec protects, and a test that ignored
them would either fail forever or have to be switched off. So the allowlist
does not name the classes; it names the INTERPOLATION SITE each family is
generated from, and asserts that site still exists. Delete `warmth-{{` from
the templates and this test goes red on the eight warmth rules that just
became dead, which is exactly the day you want to hear about it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WEB = REPO / "coverage_web"
CSS = WEB / "static" / "css" / "coverage.css"

# family prefix -> the template fragment that generates it. Fewer than 20
# entries by design: a long allowlist is a stylesheet nobody is pruning.
DYNAMIC_FAMILIES = {
    "warmth-dot-": "warmth-dot-{{",
    "warmth-": "warmth-{{",
    "band-": "band-{{",
    "conf-": "conf-{{",
    "prio-": "prio-{{",
}

# Classes emitted by something that is not our template source at all.
FOREIGN = {
    # Django's own widget class, lowercased from CheckboxSelectMultiple, on
    # every multi-choice field the forms render.
    "checkboxselectmultiple": "django.forms.CheckboxSelectMultiple",
}

_SKIP_DIRS = {"__pycache__", "node_modules", "staticfiles", "migrations"}
_READ_EXT = (".html", ".txt", ".py", ".js", ".css")


def _declared_classes() -> set[str]:
    """Every class selector the shared stylesheet declares a rule for."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    selectors, depth, buf = [], 0, []
    for ch in css:
        if ch == "{":
            if depth <= 1:      # depth 1 is inside an @media / @supports block
                selectors.append("".join(buf))
            buf, depth = [], depth + 1
        elif ch == "}":
            depth -= 1
            buf = []
        else:
            buf.append(ch)
    found: set[str] = set()
    for sel in selectors:
        if sel.strip().startswith("@"):
            continue
        found.update(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", sel))
    return found


def _corpus() -> str:
    """Every production file that could reference a class. Tests excluded:
    a class kept alive only by its own test is dead."""
    parts = []
    for path in WEB.rglob("*"):
        if not path.is_file() or path.suffix not in _READ_EXT or path == CSS:
            continue
        if _SKIP_DIRS & set(path.parts):
            continue
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def test_allowlist_is_short_and_every_entry_names_its_setter():
    assert len(DYNAMIC_FAMILIES) + len(FOREIGN) < 20
    blob = _corpus()
    for prefix, site in DYNAMIC_FAMILIES.items():
        assert site in blob, (
            f"{prefix!r} is allowlisted because {site!r} builds it, and that "
            "no longer appears in any template. Either the setter moved or "
            "the whole family is dead."
        )


def test_no_shared_rule_styles_something_nothing_renders():
    blob = _corpus()
    dead = []
    for cls in sorted(_declared_classes()):
        if cls in FOREIGN:
            continue
        if any(cls.startswith(p) for p in DYNAMIC_FAMILIES):
            continue
        if not re.search(r"(?<![\w-])" + re.escape(cls) + r"(?![\w-])", blob):
            dead.append(cls)
    assert not dead, (
        "coverage.css styles classes nothing renders: "
        + ", ".join("." + c for c in dead)
        + ". Delete the rule, or add the family to DYNAMIC_FAMILIES with the "
          "template fragment that builds it."
    )
