"""Guards the core invariant of this extraction: the package owns no
filesystem paths and no state/side-store files. See the task brief's
"Critical: strip ALL single-user assumptions" section and
docs/build-plan.md's shared-cache design -- a fetch→normalize→return
function has no business reading or writing any file, ever.

This is a mechanical, greppable check, not a style opinion: the original
`sources.py` hardcoded `_ROOT = Path("/Users/zhujimmy/Claude/Projects/...")`
and read/wrote a dozen `*_state.json` side-stores through `util.py`'s
`load_json_state`/`save_json_state`/`atomic_write_text`. None of that may
resurface here.
"""

from __future__ import annotations

import re
from pathlib import Path

import coverage_connectors

PACKAGE_DIR = Path(coverage_connectors.__file__).parent
SOURCE_FILES = sorted(PACKAGE_DIR.glob("*.py"))

# Absolute-path literals: a quoted string starting with a POSIX root segment
# ("/Users/...", "/home/...", "/etc/...", "/var/...", "/tmp/...") or a Windows
# drive path. Deliberately broad -- the whole point is that this package
# should contain zero filesystem-path constants of any kind.
_ABS_PATH_RE = re.compile(
    r"""["'](?:/(?:Users|home|etc|var|tmp|opt|root)/|[A-Za-z]:\\)"""
)

# Single-user state/config file names the original read or wrote. A hit on
# any of these inside the package (not the tests, which legitimately
# reference them in docstrings/comments explaining what was NOT ported)
# means a state dependency leaked back in.
_STATE_FILE_TOKENS = (
    "tracker.md",
    "targets.yaml",
    "firms.yaml",
    "profile.yaml",
    "cadence.yaml",
    "_state.json",
    "seen.json",
    "ats_state",
    "newsletter_state",
    "scan_state",
)

# Filesystem-write call shapes. Any of these appearing in the package
# (outside of a docstring/comment, which the AST-based check below already
# excludes by only scanning non-comment source) would mean the package is
# persisting something to disk on its own -- exactly what "no state files"
# forbids.
_FILE_WRITE_RE = re.compile(
    r"""\bopen\([^)]*["']w["']\)|\.write_text\(|\.write_bytes\(|json\.dump\("""
)


def _non_comment_lines(path: Path) -> list[tuple[int, str]]:
    """Source lines with '#'-comments and docstring bodies stripped enough
    that a path/filename mentioned only in prose doesn't trip the checks
    below. Not a full tokenizer -- deliberately conservative: it strips
    whole-line comments and lines that are clearly inside a triple-quoted
    docstring block, which is exactly where this module's own
    self-documentation mentions the forbidden tokens by name."""
    lines = path.read_text().splitlines()
    out = []
    in_docstring = False
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            # crude toggle -- good enough given this codebase's style of
            # always opening/closing docstrings on their own delimiter line
            # or with content on the same line as a single set of triple
            # quotes appearing an even number of times overall.
            quote_count = stripped.count('"""') + stripped.count("'''")
            if quote_count % 2 == 1:
                in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        if stripped.startswith("#"):
            continue
        out.append((i, line))
    return out


def test_no_hardcoded_absolute_filesystem_paths():
    offenders = []
    for path in SOURCE_FILES:
        for lineno, line in _non_comment_lines(path):
            if _ABS_PATH_RE.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "Hardcoded absolute filesystem path(s) found:\n" + "\n".join(offenders)


def test_no_single_user_state_file_references():
    offenders = []
    for path in SOURCE_FILES:
        for lineno, line in _non_comment_lines(path):
            lowered = line.lower()
            for token in _STATE_FILE_TOKENS:
                if token in lowered:
                    offenders.append(f"{path.name}:{lineno}: ({token!r}) {line.strip()}")
    assert not offenders, "Reference(s) to single-user state/config file(s) found:\n" + "\n".join(offenders)


def test_no_filesystem_write_calls():
    offenders = []
    for path in SOURCE_FILES:
        for lineno, line in _non_comment_lines(path):
            if _FILE_WRITE_RE.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "Filesystem-write call(s) found in the package:\n" + "\n".join(offenders)


def test_package_has_no_module_level_board_list():
    """The package must not own its own board directory (no
    `_ATS_BOARDS`-equivalent constant). Every board is a caller-supplied
    argument -- see coverage_connectors/__init__.py's module docstring."""
    forbidden_names = ("_ATS_BOARDS", "ATS_BOARDS", "_WATCH_PAGES", "DEFAULT_BOARDS")
    offenders = []
    for path in SOURCE_FILES:
        text = path.read_text()
        for name in forbidden_names:
            if re.search(rf"^\s*{name}\s*=", text, re.MULTILINE):
                offenders.append(f"{path.name} defines {name}")
    assert not offenders, "Package defines its own board list:\n" + "\n".join(offenders)
