"""The CVE scan is declared, documented, and the anthropic pin is held.

`audit-security.md §9`: `pip-audit` was not installed and no CVE scan had ever
been run against this tree. The fix is a dependency plus a documented command,
and both halves rot in different ways — a tool nobody documented is a tool
nobody runs, and a documented command with no tool behind it fails on the day
somebody finally tries it. This asserts both exist and stay together.

It does NOT run the scan. That needs the network, which the suite never
touches; the scan is a step in `docs/deploy.md`, run by a human before a
deploy.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _dev_group() -> list[str]:
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return cfg["dependency-groups"]["dev"]


def test_a_cve_scanner_is_a_declared_dev_dependency():
    assert any(dep.startswith(("pip-audit", "uv-secure")) for dep in _dev_group()), (
        "no CVE scanner in the dev group. audit-security.md §9 found that no "
        "scan had ever been run; a scanner nobody declared is a scan nobody "
        "runs."
    )


def test_the_command_is_written_down_where_a_deploy_would_find_it():
    """A tool with no documented invocation is a tool that gets installed once
    and never used. `docs/deploy.md` is where a human is already reading on the
    day this matters."""
    deploy = (REPO_ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
    assert "pip-audit" in deploy
    assert "Dependency scanning" in deploy


def test_the_anthropic_pin_is_still_the_major_version_the_agent_loop_expects():
    """0.x against the 1.x that exists upstream, and held on purpose: a major
    release is exactly where the streaming and tool APIs `assistant/`'s agent
    loop is written against would change. Moving it is its own commit behind
    its own read of the migration notes, never a line in a batch of point
    releases. If this fails, that read had better have happened."""
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "anthropic"' in lock
    block = lock.split('name = "anthropic"', 1)[1][:200]
    assert 'version = "0.' in block, (
        "the anthropic pin moved to a new major version. docs/deploy.md's "
        "dependency-scanning section says why that is its own commit: the "
        "agent loop depends on the streaming and tool APIs."
    )
