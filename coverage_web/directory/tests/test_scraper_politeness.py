"""The two commands that read firms' own websites say who they are and ask
before they knock.

Both `enrich_postings` and `fetch_firm_logos` sent
`Mozilla/5.0 ... Chrome/126` and never looked at a robots.txt until
2026-09-01 — `grep robotparser` over the whole repo returned nothing. That
is one command reading 800+ pages across ~50 hosts and another walking
firms' homepages, both claiming to be a person at a browser.

These tests pin the two properties that fix is: the user-agent names
Coverage, and no outbound request happens against a path the host has
disallowed. `core/tests/test_robots.py` covers the parser itself; this file
covers the seam between it and the fetchers.
"""

from __future__ import annotations

import logging
from urllib.robotparser import RobotFileParser

import pytest

from core import robots
from directory.management.commands import enrich_postings as enrich_mod
from directory.management.commands import fetch_firm_logos as logos_mod

ROBOTS_TXT = """
User-agent: *
Disallow: /
"""


def _wall(origin: str) -> None:
    """Seed the robots cache so `origin` disallows everything.

    Seeded rather than served: the suite-wide `_no_live_robots_fetches`
    fixture (coverage_web/conftest.py) keeps the fetch path off the network
    and clears this cache around every test, so putting a parser in it is
    how a test says "this host said no" without a request.
    """
    parser = RobotFileParser()
    parser.parse(ROBOTS_TXT.splitlines())
    robots._CACHE[origin] = parser


# ---------------------------------------------------------------------------
# The name we send.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ua", [
    enrich_mod.UA["User-Agent"],
    logos_mod.UA["User-Agent"],
])
def test_neither_command_pretends_to_be_a_browser(ua):
    lowered = ua.lower()
    for tell in ("mozilla", "chrome", "safari", "applewebkit", "gecko"):
        assert tell not in lowered, f"{ua!r} still spoofs a browser"


@pytest.mark.parametrize("ua", [
    enrich_mod.UA["User-Agent"],
    logos_mod.UA["User-Agent"],
])
def test_both_follow_the_connectors_user_agent_convention(ua):
    """`coverage_connectors/http.py`'s shape: a bare product token, then a
    contact URL, then what the fetcher is for. The leading token matters
    beyond tidiness — it is the part a robots.txt `User-agent:` line
    matches on."""
    assert ua.startswith("coverage-")
    assert "+https://coverage.app" in ua
    assert ua.split("/")[0].strip()


# ---------------------------------------------------------------------------
# enrich_postings
# ---------------------------------------------------------------------------
def test_enrich_skips_a_disallowed_posting_without_fetching_it(monkeypatch, caplog):
    _wall("https://walled.example")

    def fail(*a, **k):
        raise AssertionError("robots.txt said no; nothing should be requested")

    monkeypatch.setattr(enrich_mod.requests, "get", fail)
    monkeypatch.setattr(enrich_mod.requests, "post", fail)

    with caplog.at_level(logging.INFO, logger=enrich_mod.__name__):
        result = enrich_mod.fetch_posting("https://walled.example/jobs/1")

    assert result == (None, "", "")
    assert "robots.txt disallows https://walled.example/jobs/1" in caplog.text


def test_enrich_still_fetches_a_host_that_allows_it(monkeypatch):
    class _Resp:
        status_code = 200
        text = "<html><body>Applications close 30 November 2026.</body></html>"

    monkeypatch.setattr(enrich_mod.requests, "get", lambda *a, **k: _Resp())
    text, _location, _title = enrich_mod.fetch_posting("https://open.example/jobs/1")
    assert text and "30 November 2026" in text


def test_a_disallowed_posting_is_not_recorded_as_answered(monkeypatch):
    """`(None, "", "")` is the same shape an unreachable page returns, and
    that is the point: the command retries it next run rather than writing
    down that the page said nothing about a deadline."""
    _wall("https://walled.example")
    monkeypatch.setattr(
        enrich_mod.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")),
    )
    assert enrich_mod.fetch_posting("https://walled.example/jobs/1")[0] is None


# ---------------------------------------------------------------------------
# fetch_firm_logos — three separate places it goes out.
# ---------------------------------------------------------------------------
def test_logos_skips_a_disallowed_homepage_for_declared_icons(monkeypatch, caplog):
    _wall("https://walled.example")
    monkeypatch.setattr(
        logos_mod.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")),
    )
    with caplog.at_level(logging.INFO, logger=logos_mod.__name__):
        assert logos_mod.site_icons("walled.example") == []
    assert "robots.txt disallows" in caplog.text


def test_logos_skips_a_disallowed_homepage_for_page_wordmarks(monkeypatch):
    _wall("https://walled.example")
    _wall("https://www.walled.example")
    monkeypatch.setattr(
        logos_mod.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")),
    )
    assert logos_mod.page_logos("walled.example") == []


def test_logos_skips_a_disallowed_image_url(monkeypatch):
    _wall("https://walled.example")
    monkeypatch.setattr(
        logos_mod.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")),
    )
    assert logos_mod._fetch("https://walled.example/logo.png") is None
