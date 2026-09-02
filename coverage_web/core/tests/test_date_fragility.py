"""A test that goes red on a date is a test nobody trusts on that date.

`audit-perf-tests.md §2` looked for one and found none — but the search was a
human reading 256 date literals across 40 test files, and in a single night
three tests were written that hardcoded today's date (`todo-mined.md §1`,
`37ba641`). A manual pass does not repeat. This is the pass that does.

WHAT IS FRAGILE, precisely. Not a date literal: this suite is full of them and
most are fine, because a test that asks `outreach_blackout(date(2026, 12, 25))`
is asking a question about a specific day and will still be asking it in 2031.
What is fragile is a literal COMPARED AGAINST THE REAL CLOCK — `assert
timezone.localdate() == date(2026, 9, 1)` is true for one day and is a
scheduled failure for every other. The check below is that shape and only that
shape: a clock read, a bare date literal, and a comparison, in one statement.

THE ALLOWLIST is content-keyed, not line-keyed, because unrelated edits move
line numbers and a line-keyed allowlist rots into permission for whatever
happens to be on that line next month. Each entry names why it is deliberate.

The companion to this file is the simulated-clock run documented in
`coverage_web/conftest.py` and `docs/see-it-locally.md`:

    COVERAGE_FAKE_TODAY=2026-12-24 pytest ...
    COVERAGE_FAKE_TODAY=saturday   pytest ...

This check is the cheap half that runs every time; those two commands are the
expensive half that runs before a release.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# A read of the real clock.
_CLOCK = re.compile(
    r"timezone\.(localdate|now|localtime)\(\)"
    r"|(?<![\w.])date\.today\(\)"
    r"|(?<![\w.])datetime\.now\(\)"
    r"|dt\.date\.today\(\)"
)
# A bare calendar literal: `date(2026, 9, 1)`, `datetime(2026, 9, 1, 8, 0)`.
_LITERAL = re.compile(r"(?<![\w.])(date|datetime)\(\s*(19|20)\d\d\s*,")
_COMPARISON = re.compile(r"==|!=|<=|>=|<|>")

# Deliberate, and each one says why.
_ALLOWED = {
    # A self-check INSIDE `_frozen(date(2026, 12, 24))`: it asserts the freeze
    # actually took before the rest of the test leans on it. The clock read is
    # not the real clock at that point, which is the whole assertion.
    "assert timezone.localdate() == date(2026, 12, 24)",
}


def _test_files():
    """Every test module the suite collects, walked from the configured
    testpaths rather than the repo root — the root also holds
    `.claude/worktrees/`, a full checkout per in-flight agent, and reading a
    sibling's half-written test file would fail this build for it. Same
    reasoning as `core/tests/test_suite_hygiene.py::_suite_files`.
    """
    here = Path(__file__).resolve()
    for name in ("coverage_web", "coverage_domain", "coverage_connectors"):
        for path in (REPO_ROOT / name).rglob("test_*.py"):
            # Not this file. It carries the fragile shape on purpose, in its
            # docstring and in the parametrised proofs below, and a checker
            # that flags its own examples flags nothing else ever again.
            if path.resolve() != here:
                yield path


def fragile_lines(source: str, label: str = "<source>") -> list[str]:
    """Lines in `source` that compare a real-clock read to a date literal.

    Exposed rather than inlined so the check can be tested against a string
    (below) instead of only against the tree, which is what makes it possible
    to prove it still fires.
    """
    found = []
    for number, line in enumerate(source.splitlines(), start=1):
        code = line.split("#", 1)[0].strip()
        if not code or code in _ALLOWED:
            continue
        if _CLOCK.search(code) and _LITERAL.search(code) and _COMPARISON.search(code):
            found.append(f"{label}:{number}: {code}")
    return found


def test_no_test_compares_the_real_clock_to_a_date_literal():
    offenders = []
    for path in _test_files():
        offenders += fragile_lines(
            path.read_text(encoding="utf-8"), str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        "a test compares a real-clock read to a hardcoded date. It passes "
        "today and is a scheduled failure on every other day. Either pin the "
        "clock (`mock.patch(\"django.utils.timezone.now\", ...)`, the pattern "
        "crm/tests/test_today_timezone.py established) or write the literal as "
        "an offset from the clock:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("source", [
    "assert timezone.localdate() == date(2026, 9, 1)",
    "    assert date.today() > date(2026, 1, 1)",
    "assert timezone.now() < datetime(2027, 3, 4, 9, 0)",
    "assert dt.date.today() != date(2030, 1, 1)",
])
def test_the_check_fires_on_the_shape_it_is_for(source):
    """The proof that it still works. Written against strings rather than a
    temporary file on disk, because a temporary file is a thing that can be
    left behind and a string cannot."""
    assert fragile_lines(source), f"the check missed: {source}"


@pytest.mark.parametrize("source", [
    # A question about a specific day, asked of a pure function. Fine forever.
    'assert outreach_blackout(date(2026, 12, 25)) == "holiday"',
    # A literal on both sides: no clock involved.
    "assert date(2026, 1, 1) < date(2026, 2, 1)",
    # An offset from the clock, which is the fix this check asks for.
    "assert row.deadline == timezone.localdate() + timedelta(days=30)",
    # A fixture pinning its own `as_of`, deliberately.
    "ctx = _cockpit_context(user, as_of=date(2026, 12, 24))",
    # Commented out.
    "# assert timezone.localdate() == date(2026, 9, 1)",
])
def test_the_check_leaves_the_legitimate_shapes_alone(source):
    """The other half. A check that flags every date literal in a suite with
    256 of them would be turned off within a week."""
    assert not fragile_lines(source), f"false positive on: {source}"
