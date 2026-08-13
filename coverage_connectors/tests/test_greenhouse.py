"""Offline unit tests for the Greenhouse connector: normalization/parsing
against a captured real response (tests/fixtures/greenhouse_jobs.json,
tests/fixtures/greenhouse_job_detail.json — saved from a live fetch of
boards-api.greenhouse.io/v1/boards/williamblair/jobs on 2026-07-23). No
network access; `coverage_connectors.http` is monkeypatched."""

from __future__ import annotations

import urllib.error

from coverage_connectors import greenhouse
from coverage_connectors.models import GreenhouseBoard


def test_fetch_normalizes_real_fixture(monkeypatch, greenhouse_jobs_fixture):
    monkeypatch.setattr(greenhouse, "fetch_json", lambda url, **kw: greenhouse_jobs_fixture)

    board = GreenhouseBoard(firm="William Blair", token="williamblair")
    result = greenhouse.fetch(board)

    assert result.ok
    assert result.error is None
    assert result.raw_count == len(greenhouse_jobs_fixture["jobs"])
    assert len(result.opportunities) == result.raw_count

    opp = result.opportunities[0]
    raw = greenhouse_jobs_fixture["jobs"][0]
    assert opp.firm == "William Blair"
    assert opp.source == "greenhouse"
    assert opp.title == raw["title"]
    assert opp.url == raw["absolute_url"]
    assert opp.location == raw["location"]["name"]
    # This board's application_deadline is null for every job in the fixture
    # (verified live) -- deadline must come through as None, not "" or "None".
    assert opp.deadline is None
    assert opp.posted_at == raw["updated_at"][:10]
    assert opp.status == "open"
    assert opp.region is None
    assert opp.cohort is None
    assert opp.sponsorship is None


def test_fetch_reports_board_level_failure(monkeypatch):
    def raise_error(url, **kw):
        raise urllib.error.HTTPError(url, 500, "boom", None, None)

    monkeypatch.setattr(greenhouse, "fetch_json", raise_error)
    board = GreenhouseBoard(firm="Some Firm", token="somefirm")
    result = greenhouse.fetch(board)

    assert not result.ok
    assert result.opportunities == []
    assert result.error is not None


def test_deadline_is_populated_when_present(monkeypatch, greenhouse_jobs_fixture):
    job_with_deadline = dict(greenhouse_jobs_fixture["jobs"][0])
    job_with_deadline["application_deadline"] = "2026-09-01T00:00:00-04:00"
    monkeypatch.setattr(greenhouse, "fetch_json", lambda url, **kw: {"jobs": [job_with_deadline]})

    board = GreenhouseBoard(firm="William Blair", token="williamblair")
    result = greenhouse.fetch(board)

    assert result.opportunities[0].deadline == "2026-09-01"


def test_classify_url_board_only():
    info = greenhouse.classify_url("https://job-boards.greenhouse.io/williamblair/jobs")
    assert info == {"token": "williamblair", "job_id": None}


def test_classify_url_job_boards_host():
    info = greenhouse.classify_url("https://job-boards.greenhouse.io/williamblair/jobs/5181697007")
    assert info == {"token": "williamblair", "job_id": "5181697007"}


def test_classify_url_unrecognized():
    assert greenhouse.classify_url("https://example.com/careers") is None


def test_classify_url_does_not_match_boards_api_host():
    # boards-api.greenhouse.io is the internal JSON endpoint fetch() calls,
    # not a candidate-facing URL -- Opportunity.url (and therefore what
    # verify() is actually called with) is job.get("absolute_url"), which
    # Greenhouse serves as either a job-boards.greenhouse.io URL or the
    # firm's own custom domain (see the "known limitation" test below), NEVER
    # a boards-api.greenhouse.io URL. classify_url must not match it.
    assert greenhouse.classify_url("https://boards-api.greenhouse.io/v1/boards/williamblair/jobs") is None


def test_verify_open_from_fixture(monkeypatch, greenhouse_job_detail_fixture):
    monkeypatch.setattr(greenhouse, "fetch_json", lambda url, **kw: greenhouse_job_detail_fixture)

    result = greenhouse.verify("https://job-boards.greenhouse.io/williamblair/jobs/5181697007")

    assert result.provider == "greenhouse"
    assert result.result == "verified-open"
    assert greenhouse_job_detail_fixture["title"] in result.evidence
    assert result.deadline_dates == []  # this fixture's application_deadline is null
    assert result.posted_date == greenhouse_job_detail_fixture["updated_at"][:10]


def test_verify_closed_on_404(monkeypatch):
    def raise_404(url, **kw):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(greenhouse, "fetch_json", raise_404)
    result = greenhouse.verify("https://job-boards.greenhouse.io/williamblair/jobs/999999999999")

    assert result.result == "closed"
    assert "404" in result.evidence


def test_verify_needs_verification_without_job_id():
    result = greenhouse.verify("https://job-boards.greenhouse.io/williamblair/jobs")
    assert result.result == "needs-verification"


def test_verify_unreachable_on_non_404_http_error(monkeypatch):
    def raise_500(url, **kw):
        raise urllib.error.HTTPError(url, 500, "boom", None, None)

    monkeypatch.setattr(greenhouse, "fetch_json", raise_500)
    result = greenhouse.verify("https://job-boards.greenhouse.io/williamblair/jobs/123")
    assert result.result == "unreachable"


def test_classify_url_known_custom_domain():
    """Regression test for the confirmed William Blair defect: all 48 open
    William Blair rows use `https://www.williamblair.com/Careers/job-
    description?gh_jid=<id>`, a shape `_BOARD_URL_RE` never matches, which
    left every one of them stuck at provider='unknown' -- meaning
    `reverify`'s single-URL liveness backstop could never usefully check a
    single William Blair row. See module docstring and
    `_CUSTOM_DOMAIN_TOKENS`."""
    info = greenhouse.classify_url(
        "https://www.williamblair.com/Careers/job-description?gh_jid=5186505007")
    assert info == {"token": "williamblair", "job_id": "5186505007"}


def test_verify_open_on_known_custom_domain_url(monkeypatch, greenhouse_job_detail_fixture):
    monkeypatch.setattr(greenhouse, "fetch_json", lambda url, **kw: greenhouse_job_detail_fixture)

    result = greenhouse.verify(
        "https://www.williamblair.com/Careers/job-description?gh_jid=5181697007")

    assert result.result == "verified-open"


def test_verify_needs_verification_on_unlisted_custom_domain_url():
    """An UNLISTED third-party domain must still fall through honestly --
    this package has no reliable way to recover a board token from an
    arbitrary domain it has never been told about, so it must not guess
    one. Only domains in `_CUSTOM_DOMAIN_TOKENS`, each backed by a real
    board registration in `directory/boards.py`, are ever resolved."""
    result = greenhouse.verify("https://careers.example-bank.com/job?gh_jid=5181697007")
    assert result.result == "needs-verification"


def test_wafd_scalar_json_degrades_to_board_failure(monkeypatch):
    """A WAF returning valid-but-scalar JSON (a bare string) must surface as
    ok=False, not escape as an AttributeError that aborts the whole run.
    Uses the real fetch_json guard against a mocked fetch_text."""
    from coverage_connectors import http

    monkeypatch.setattr(http, "fetch_text", lambda url, **kw: '"Access Denied"')
    monkeypatch.setattr(greenhouse, "fetch_json", http.fetch_json)
    result = greenhouse.fetch(GreenhouseBoard(firm="X", token="x"))
    assert result.ok is False
    assert "expected JSON object" in (result.error or "")
