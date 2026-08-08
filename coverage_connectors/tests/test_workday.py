"""Offline unit tests for the Workday connector: normalization/parsing
against a captured real response (tests/fixtures/workday_citi_page1.json —
saved from a live fetch of citi.wd5.myworkdayjobs.com's Citi_Early_Careers
board on 2026-07-23), plus a synthetic multi-page test for the pagination
contract itself (the real board fetched for this fixture only had 9 total
postings — one page — so pagination across >20 results needs a fabricated
second page to exercise deterministically and offline). No network access;
`coverage_connectors.http` is monkeypatched."""

from __future__ import annotations

import urllib.error

from coverage_connectors import workday
from coverage_connectors.models import WorkdayBoard


def test_fetch_normalizes_real_fixture(monkeypatch, workday_citi_page1_fixture):
    monkeypatch.setattr(workday, "post_json", lambda url, payload, **kw: workday_citi_page1_fixture)

    board = WorkdayBoard(firm="Citi", tenant_host="citi.wd5", site="Citi_Early_Careers_Events_Site")
    result = workday.fetch(board)

    assert result.ok
    assert result.error is None
    raw_jobs = workday_citi_page1_fixture["jobPostings"]
    assert result.raw_count == workday_citi_page1_fixture["total"]
    assert len(result.opportunities) == len(raw_jobs)

    opp = result.opportunities[0]
    raw = raw_jobs[0]
    assert opp.firm == "Citi"
    assert opp.source == "workday"
    assert opp.title == raw["title"]
    assert opp.location == raw["locationsText"]
    # Must include the board's site slug -- tenant_host + path alone 404s
    # (a real bug in the original, fixed here; see workday.py's module docstring).
    assert opp.url == f"https://citi.wd5.myworkdayjobs.com/{board.site}{raw['externalPath']}"
    assert opp.posted_at == raw["postedOn"]
    assert opp.deadline is None  # Workday's CxS jobs listing exposes no deadline field, ever
    assert opp.status == "open"


def test_pagination_stitches_pages_using_reported_total(monkeypatch):
    """Fabricate a 25-total board (page 1 = 20 rows, page 2 = 5 rows) to
    exercise the offset-stepping contract deterministically -- the real
    board captured in the fixture only had 9 total postings (one page)."""
    page1 = {"total": 25, "jobPostings": [{"title": f"Role {i}", "externalPath": f"/job/r{i}",
                                            "locationsText": "NYC", "postedOn": "Posted 1 Day Ago"}
                                           for i in range(20)]}
    page2 = {"total": 25, "jobPostings": [{"title": f"Role {i}", "externalPath": f"/job/r{i}",
                                            "locationsText": "NYC", "postedOn": "Posted 1 Day Ago"}
                                           for i in range(20, 25)]}

    calls = []

    def fake_post_json(url, payload, **kw):
        calls.append(payload["offset"])
        return page1 if payload["offset"] == 0 else page2

    monkeypatch.setattr(workday, "post_json", fake_post_json)

    board = WorkdayBoard(firm="Big Bank", tenant_host="bigbank.wd1", site="Careers")
    result = workday.fetch(board)

    assert calls == [0, 20]  # exactly two pages fetched, no third page beyond total
    assert result.raw_count == 25
    assert len(result.opportunities) == 25
    assert result.opportunities[-1].title == "Role 24"


def test_pagination_caps_at_max_jobs(monkeypatch):
    """A board reporting a much larger total than _MAX_JOBS must still stop
    at the cap -- one saturated tenant can't become an unbounded fetch.

    The expected page count is derived from `_MAX_JOBS`, not written out.
    This test hardcoded "3 pages, 60 jobs" and went red the day the cap was
    raised, which is a test asserting a constant's VALUE rather than the
    behaviour that depends on it."""
    monkeypatch.setattr(workday, "_MAX_JOBS", 60)
    total = workday._MAX_JOBS * 10
    page = {"total": total, "jobPostings": [{"title": "x", "externalPath": "/job/x",
                                             "locationsText": "NYC", "postedOn": ""}] * 20}
    calls = []

    def fake_post_json(url, payload, **kw):
        calls.append(payload["offset"])
        return page

    monkeypatch.setattr(workday, "post_json", fake_post_json)

    board = WorkdayBoard(firm="Huge Co", tenant_host="huge.wd1", site="Careers")
    result = workday.fetch(board)

    expected_pages = workday._MAX_JOBS // 20
    assert calls == [i * 20 for i in range(expected_pages)]
    assert result.raw_count == total  # raw_count reports the provider's own total
    assert len(result.opportunities) == workday._MAX_JOBS  # only what was fetched
    # And the board must SAY it was cut short, or ingest will read the
    # missing rows as closed.
    assert result.truncated is True


def test_a_board_read_to_the_end_is_not_truncated(monkeypatch):
    """The flag means "there is more than I returned", not "I paginated"."""
    page = {"total": 2, "jobPostings": [{"title": "x", "externalPath": "/job/x",
                                         "locationsText": "NYC", "postedOn": ""}] * 2}
    monkeypatch.setattr(workday, "post_json", lambda url, payload, **kw: page)
    board = WorkdayBoard(firm="Small Co", tenant_host="small.wd1", site="Careers")
    assert workday.fetch(board).truncated is False


def test_search_text_is_forwarded(monkeypatch):
    captured = {}

    def fake_post_json(url, payload, **kw):
        captured["url"] = url
        captured["searchText"] = payload["searchText"]
        return {"total": 0, "jobPostings": []}

    monkeypatch.setattr(workday, "post_json", fake_post_json)
    board = WorkdayBoard(firm="Carlyle", tenant_host="carlyle.wd1", site="Carlyle", search_text="intern")
    workday.fetch(board)

    assert captured["searchText"] == "intern"
    assert captured["url"] == "https://carlyle.wd1.myworkdayjobs.com/wday/cxs/carlyle/Carlyle/jobs"


def test_fetch_reports_board_level_failure(monkeypatch):
    def raise_error(url, payload, **kw):
        raise urllib.error.HTTPError(url, 500, "boom", None, None)

    monkeypatch.setattr(workday, "post_json", raise_error)
    board = WorkdayBoard(firm="Some Firm", tenant_host="somefirm.wd1", site="Careers")
    result = workday.fetch(board)

    assert not result.ok
    assert result.opportunities == []


def test_normalized_url_includes_site_segment_bug_regression(monkeypatch):
    """Regression test for a real bug found live in the original codebase
    (2026-07-23): building the clickable URL as tenant_host + externalPath
    alone produces a 404. citi.wd5.myworkdayjobs.com/job/London--United-
    Kingdom/Citi-London-Military-Insight-Day_26979549 confirmed 404 live;
    the same URL with /Citi_Early_Careers_Events_Site/ inserted before
    /job/ confirmed 200 live. See workday.py's module docstring, bug (1)."""
    fixture = {"total": 1, "jobPostings": [{
        "title": "Citi London Military Insight Day",
        "externalPath": "/job/London--United-Kingdom/Citi-London-Military-Insight-Day_26979549",
        "locationsText": "London United Kingdom",
        "postedOn": "Posted 2 Days Ago",
    }]}
    monkeypatch.setattr(workday, "post_json", lambda url, payload, **kw: fixture)
    board = WorkdayBoard(firm="Citi", tenant_host="citi.wd5", site="Citi_Early_Careers_Events_Site")
    result = workday.fetch(board)

    assert result.opportunities[0].url == (
        "https://citi.wd5.myworkdayjobs.com/Citi_Early_Careers_Events_Site"
        "/job/London--United-Kingdom/Citi-London-Military-Insight-Day_26979549"
    )


def test_classify_url_does_not_truncate_two_segment_job_path_bug_regression():
    """Regression test for a real bug found live in the original codebase
    (2026-07-23): verify_rows.py's _WORKDAY_RE captured only the first path
    segment after /job/, silently dropping the title segment. Re-querying
    the CxS job-detail endpoint with the truncated path returns a
    not-found body live, which the original's verifier reads as "closed"
    for a posting that is actually live. See workday.py's module docstring,
    bug (2)."""
    info = workday.classify_url(
        "https://citi.wd5.myworkdayjobs.com/Citi_Early_Careers_Events_Site"
        "/job/London--United-Kingdom/Citi-London-Military-Insight-Day_26979549"
    )
    assert info["job_path"] == "London--United-Kingdom/Citi-London-Military-Insight-Day_26979549"


def test_jobs_url_explicit_tenant_and_alt_domain():
    """Golub Capital: hosted on myworkdaysite.com with a cxs tenant
    ("golubcapital") that is NOT the first label of the host ("wd501")."""
    assert workday._jobs_url("wd501", "Golub_Capital_Careers",
                             tenant="golubcapital", domain="myworkdaysite.com") == (
        "https://wd501.myworkdaysite.com/wday/cxs/golubcapital/Golub_Capital_Careers/jobs"
    )
    # Defaults are unchanged: tenant derived from host, classic domain.
    assert workday._jobs_url("citi.wd5", "Site") == (
        "https://citi.wd5.myworkdayjobs.com/wday/cxs/citi/Site/jobs"
    )


def test_alt_domain_board_posts_to_right_url_and_builds_url(monkeypatch):
    captured = {}

    def fake_post_json(url, payload, **kw):
        captured["url"] = url
        return {"total": 1, "jobPostings": [{"title": "Associate", "externalPath": "/job/NYC/Associate_R1",
                                             "locationsText": "New York", "postedOn": "Posted Today"}]}

    monkeypatch.setattr(workday, "post_json", fake_post_json)
    board = WorkdayBoard(firm="Golub Capital", tenant_host="wd501", tenant="golubcapital",
                         site="Golub_Capital_Careers", domain="myworkdaysite.com")
    opp = workday.fetch(board).opportunities[0]

    assert captured["url"] == "https://wd501.myworkdaysite.com/wday/cxs/golubcapital/Golub_Capital_Careers/jobs"
    assert opp.url == "https://wd501.myworkdaysite.com/Golub_Capital_Careers/job/NYC/Associate_R1"


def test_classify_url_with_job_path():
    info = workday.classify_url(
        "https://citi.wd5.myworkdayjobs.com/Citi_Early_Careers_Events_Site/job/New-York/Some-Role_R123"
    )
    assert info == {"tenant_host": "citi.wd5", "site": "Citi_Early_Careers_Events_Site",
                     "job_path": "New-York/Some-Role_R123", "search_text": None}


def test_classify_url_with_search_query():
    info = workday.classify_url(
        "https://carlyle.wd1.myworkdayjobs.com/Carlyle?q=R456"
    )
    assert info == {"tenant_host": "carlyle.wd1", "site": "Carlyle", "job_path": None, "search_text": "R456"}


def test_classify_url_unrecognized():
    assert workday.classify_url("https://example.com/careers") is None


def test_verify_open_via_job_path(monkeypatch):
    monkeypatch.setattr(
        workday, "fetch_json",
        lambda url, **kw: {"jobPostingInfo": {"title": "2027 Summer Analyst", "postedOn": "Posted 3 Days Ago"}},
    )
    result = workday.verify(
        "https://citi.wd5.myworkdayjobs.com/Citi_Early_Careers_Events_Site/job/New-York/Role_R123"
    )
    assert result.result == "verified-open"
    assert "2027 Summer Analyst" in result.evidence
    assert result.deadline_dates == []


def test_verify_does_not_close_on_a_malformed_200(monkeypatch):
    """PINS A FIXED BUG (C1): this test used to be named
    `test_verify_closed_via_job_path_missing_posting` and asserted
    `result.result == "closed"` — i.e. it pinned "empty jobPostingInfo means
    closed" as correct behaviour. An HTTP 200 with no `jobPostingInfo.title`
    is NOT a positive gone-signal: it is exactly as consistent with a WAF
    page, a rate-limit envelope, a maintenance page, or a Workday key rename
    as with a real removal, and `reverify.py` acts on "closed" with zero
    corroboration — a one-shot deletion from a student's feed for the wrong
    reason. An unrecognised-but-200 response must ask again later
    (`needs-verification`), never close outright."""
    monkeypatch.setattr(workday, "fetch_json", lambda url, **kw: {"jobPostingInfo": {}})
    result = workday.verify(
        "https://citi.wd5.myworkdayjobs.com/Citi_Early_Careers_Events_Site/job/New-York/Role_R123"
    )
    assert result.result == "needs-verification"


def test_verify_via_search_text_zero_results(monkeypatch):
    monkeypatch.setattr(workday, "post_json", lambda url, payload, **kw: {"total": 0, "jobPostings": []})
    result = workday.verify("https://carlyle.wd1.myworkdayjobs.com/Carlyle?q=R456")
    assert result.result == "closed"


def test_verify_needs_verification_without_path_or_query():
    result = workday.verify("https://citi.wd5.myworkdayjobs.com/Citi_Early_Careers_Events_Site")
    assert result.result == "needs-verification"
