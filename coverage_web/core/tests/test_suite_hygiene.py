"""The suite's own hygiene: markers, and the file handles a run leaks.

Both of these came out of a read-only audit on 2026-09-01 that ran the whole
suite under `-W default::ResourceWarning` and read what it printed. Neither is
about product behaviour, and both are the kind of thing that decays silently
because a warning is not a failure:

  - 12 `PytestUnknownMarkWarning: pytest.mark.live` per run, because the
    connectors' live-smoke marker was used and never registered. A suite that
    prints warnings every time trains its readers to stop reading them, and
    the next warning is the one that matters.
  - 3 `ResourceWarning: unclosed file` per run, all from one
    `open(...).read()` in a management command.

The markers are also the mechanism behind the documented fast run
(`-m "not slow and not stress"`, see README.md), so what they select is worth
pinning too: they are applied by SHAPE in the root conftest, and a marker
nobody has to remember to write is a marker that stays true.
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _ini() -> dict:
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return cfg["tool"]["pytest"]["ini_options"]


def _ini_markers() -> dict[str, str]:
    return {line.split(":", 1)[0].strip(): line
            for line in _ini().get("markers", [])}


def _suite_files():
    """Every test module the suite actually collects.

    Walked from `testpaths` rather than from the repo root, and that is not a
    speed choice: on a developer's machine the root also holds `.claude/
    worktrees/`, a full checkout per in-flight agent. A root walk would read
    somebody else's half-written test file and fail this build for it."""
    for name in _ini()["testpaths"]:
        yield from (REPO_ROOT / name).rglob("test_*.py")


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

def test_every_marker_the_suite_writes_by_hand_is_registered():
    """The ratchet. `pytest.mark.<name>` for a name pytest does not know is a
    warning, not an error, so it survives review — this is what makes it
    visible. `outreach_blackout` is registered in `coverage_web/conftest.py`
    rather than here because it applies to the web suite only."""
    from_ini = set(_ini_markers())
    registered = from_ini | {"outreach_blackout"} | {
        # pytest's and pytest-django's own.
        "parametrize", "skip", "skipif", "xfail", "usefixtures", "filterwarnings",
        "django_db", "urls", "ignore_template_errors", "no_django_db",
    }

    used = set()
    for path in _suite_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            marker = line.strip()
            if not marker.startswith("@pytest.mark."):
                continue
            name = re.match(r"[A-Za-z_][A-Za-z0-9_]*",
                            marker[len("@pytest.mark."):])
            if name:
                used.add(name.group(0))

    assert used <= registered, (
        f"unregistered marker(s) {sorted(used - registered)} — each one is a "
        f"PytestUnknownMarkWarning on every run. Register them under "
        f"[tool.pytest.ini_options] markers in pyproject.toml."
    )


@pytest.mark.parametrize("name", ["slow", "stress", "live"])
def test_the_three_markers_are_registered_with_a_description(name):
    """A marker without a description is a marker whose meaning lives in
    somebody's head. `-m` selection is a decision about what NOT to run, so
    the line has to say what is being skipped."""
    line = _ini_markers().get(name)
    assert line, f"{name} is not registered in pyproject.toml"
    assert len(line.split(":", 1)[1].strip()) > 20, line


class _StubItem:
    """The two attributes the root conftest's hook reads, and a record of what
    it applied. Testing the hook directly rather than by re-running pytest:
    the rule is `filename` and `fixturenames`, and a subprocess would test the
    runner as much as the rule."""

    def __init__(self, filename, fixturenames=()):
        self.path = Path(filename)
        self.fixturenames = tuple(fixturenames)
        self.marks = []

    def add_marker(self, mark):
        self.marks.append(mark.name)


def _root_conftest():
    """The REPO ROOT's conftest, loaded by path.

    `sys.modules["conftest"]` is ambiguous here — this repo has one conftest
    at the root and another under `coverage_web/`, both with that basename,
    and which one answers depends on collection order."""
    spec = importlib.util.spec_from_file_location(
        "coverage_root_conftest", REPO_ROOT / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _modify(items):
    _root_conftest().pytest_collection_modifyitems(items)
    return items


def test_stress_modules_are_marked_stress_without_being_told():
    """Auto-applied by file name, so a new `test_stress_*` module joins the
    set the day it lands rather than the day somebody remembers. 4,006 of the
    suite's 9,296 cases live in these files."""
    stress = _StubItem("directory/tests/test_stress_classify.py")
    ordinary = _StubItem("directory/tests/test_classify.py")
    _modify([stress, ordinary])
    assert "stress" in stress.marks
    assert "stress" not in ordinary.marks


def test_a_page_render_is_marked_slow_without_being_told():
    """The `client` fixture is the shape: a test that renders a page through
    the Django test client, with a fixture world built for it. That bucket is
    1,369 tests and 251 s of a 421 s run, which is why the documented fast
    invocation excludes it. `fixturenames` is the RESOLVED closure, so a test
    reaching the client through a wrapper fixture is caught too."""
    renders = _StubItem("crm/tests/test_today.py", ["client", "db"])
    through_a_wrapper = _StubItem("crm/tests/test_today.py", ["logged_in", "client"])
    pure = _StubItem("directory/tests/test_place.py", ["monkeypatch"])
    _modify([renders, through_a_wrapper, pure])
    assert "slow" in renders.marks
    assert "slow" in through_a_wrapper.marks
    assert "slow" not in pure.marks


def test_this_very_test_run_applied_the_slow_marker(request, client):
    """The end-to-end half: the stubs above prove the rule, this proves the
    hook actually ran in this session. It takes `client`, so it must be
    carrying the marker it was collected with."""
    assert request.node.get_closest_marker("slow") is not None
    assert client is not None


# ---------------------------------------------------------------------------
# File handles
# ---------------------------------------------------------------------------

def test_no_command_reads_a_file_without_closing_it():
    """The three ResourceWarnings a full run used to print, pinned at the
    source rather than at the warning.

    `open(path).read()` closes on CPython refcounting and on nothing else, and
    under `-W default::ResourceWarning` it is the difference between a clean
    run and three warnings that mean "this code assumes a garbage collector".
    Management commands are where it hides, because they are the code a test
    calls once and never profiles."""
    offenders = []
    for path in (REPO_ROOT / "coverage_web").rglob("management/commands/*.py"):
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if "open(" not in stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("with ") or " as " in stripped:
                continue
            if ".read()" in stripped or ".write(" in stripped:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {stripped}")
    assert offenders == [], (
        "unclosed file handle(s) — use a `with` block:\n" + "\n".join(offenders)
    )
