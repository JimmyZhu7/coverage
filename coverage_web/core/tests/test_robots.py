"""core.robots — the robots.txt gate both scraping commands go through.

Nothing in this repo consulted robots.txt before 2026-09-01, and the two
commands that read firms' own websites (`enrich_postings`,
`fetch_firm_logos`) sent a spoofed Chrome user-agent while doing it. These
tests pin both halves of the fix: the rules are read and obeyed, and the
name we send is our own.

The fixture below is a real robots.txt shape — a `*` group with a couple of
disallowed prefixes, then a named group that is stricter for us. Every test
that exercises the fetch path carries `@pytest.mark.robots_live` and fakes
`urlopen`; the suite-wide default (coverage_web/conftest.py) keeps every
other test offline.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

from core import robots

UA = "coverage-enrich/0.1 (+https://coverage.app; job posting detail fetcher)"

ROBOTS_TXT = b"""
User-agent: *
Disallow: /private/
Disallow: /careers/apply
Allow: /careers/

User-agent: coverage-logos
Disallow: /
"""


class _FakeResponse:
    """The two things `_fetch_parser` uses off `urlopen`: a context manager
    and `.read()`."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return io.BytesIO(self._body)

    def __exit__(self, *exc):
        return False


@pytest.fixture
def served(monkeypatch):
    """Serve ROBOTS_TXT for every host, and count the fetches."""
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url if hasattr(req, "full_url") else str(req))
        return _FakeResponse(ROBOTS_TXT)

    monkeypatch.setattr(robots.urllib.request, "urlopen", fake_urlopen)
    robots.reset_cache()
    return calls


# ---------------------------------------------------------------------------
# The rules are read and obeyed.
# ---------------------------------------------------------------------------
@pytest.mark.robots_live
def test_a_disallowed_path_is_refused(served):
    assert robots.is_allowed("https://firm.example/private/posting-1", UA) is False


@pytest.mark.robots_live
def test_an_allowed_path_is_permitted(served):
    assert robots.is_allowed("https://firm.example/careers/posting-1", UA) is True


@pytest.mark.robots_live
def test_a_more_specific_disallow_beats_the_group_allow(served):
    assert robots.is_allowed("https://firm.example/careers/apply?id=1", UA) is False


@pytest.mark.robots_live
def test_a_group_naming_our_agent_wins_over_the_star_group(served):
    """`User-agent:` lines match the leading product token of the header,
    which is why every UA in this project starts with a bare name."""
    logos_ua = "coverage-logos/0.1 (+https://coverage.app; firm logo fetcher)"
    assert robots.is_allowed("https://firm.example/careers/x", logos_ua) is False
    assert robots.is_allowed("https://firm.example/careers/x", UA) is True


@pytest.mark.robots_live
def test_robots_txt_is_read_once_per_host_not_once_per_url(served):
    for i in range(5):
        robots.is_allowed(f"https://firm.example/careers/{i}", UA)
    robots.is_allowed("https://other.example/careers/1", UA)

    assert served == [
        "https://firm.example/robots.txt",
        "https://other.example/robots.txt",
    ]


@pytest.mark.robots_live
def test_the_robots_fetch_sends_our_own_user_agent(monkeypatch):
    """The one request this makes about identity must not be the one that
    lies about it — `RobotFileParser.read()` would have sent Python's."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        return _FakeResponse(ROBOTS_TXT)

    monkeypatch.setattr(robots.urllib.request, "urlopen", fake_urlopen)
    robots.reset_cache()
    robots.is_allowed("https://firm.example/careers/x", UA)

    assert seen["ua"] == UA
    assert seen["timeout"] == robots.TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Failure modes. Absence permits; a refusal to state the rules does not.
# ---------------------------------------------------------------------------
@pytest.mark.robots_live
def test_a_missing_robots_txt_allows_everything(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(robots.urllib.request, "urlopen", fake_urlopen)
    robots.reset_cache()
    assert robots.is_allowed("https://firm.example/private/x", UA) is True


@pytest.mark.robots_live
def test_an_unreachable_host_allows_rather_than_silently_emptying_a_run(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(robots.urllib.request, "urlopen", fake_urlopen)
    robots.reset_cache()
    assert robots.is_allowed("https://firm.example/private/x", UA) is True


@pytest.mark.robots_live
@pytest.mark.parametrize("code", [401, 403])
def test_a_host_that_refuses_to_state_its_rules_disallows_everything(monkeypatch, code):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "No", {}, None)

    monkeypatch.setattr(robots.urllib.request, "urlopen", fake_urlopen)
    robots.reset_cache()
    assert robots.is_allowed("https://firm.example/careers/x", UA) is False


@pytest.mark.robots_live
def test_a_failed_fetch_is_cached_too(monkeypatch):
    """One unreadable robots.txt must not mean one wasted request per URL."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(robots.urllib.request, "urlopen", fake_urlopen)
    robots.reset_cache()
    for i in range(4):
        robots.is_allowed(f"https://firm.example/careers/{i}", UA)
    assert len(calls) == 1


@pytest.mark.parametrize("url", ["", "not-a-url", "mailto:x@y.z", "file:///etc/passwd"])
def test_a_url_with_no_http_host_is_left_alone(url):
    """Nothing to ask, so nothing to refuse — and no attempt to fetch."""
    assert robots.is_allowed(url, UA) is True
