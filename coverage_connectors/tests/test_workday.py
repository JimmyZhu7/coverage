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

import pytest

from coverage_connectors import workday
from coverage_connectors.http import FetchError
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
    # The board's own run is "London  United Kingdom" — city, EMPTY state slot,
    # country. Punctuated on that slot boundary on the way in; the raw run is
    # still kept verbatim on the Opportunity.
    assert raw["locationsText"] == "London  United Kingdom"
    assert opp.location == "London, United Kingdom"
    assert opp.raw["locationsText"] == raw["locationsText"]
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


def test_classify_url_strips_a_trailing_apply_segment():
    """PINS A FIXED BUG: phenom.py stores a BMO posting's url as the feed's
    own applyUrl, which is the real job path PLUS a trailing /apply page --
    e.g. ".../job/Mississauga-ON-CAN/Associate_R260018720/apply". Left in,
    job_path captured that whole three-segment string, `verify()`'s
    job-detail fetch 404s, and `deadline_dates` (and the fresh "verified-
    open" signal itself) never reach the row at all. Confirmed live
    2026-08-14: four of five BMO rows sampled for the frozen-deadline
    defect carry exactly this /apply-suffixed url shape."""
    info = workday.classify_url(
        "https://bmo.wd3.myworkdayjobs.com/External/job/Mississauga-ON-CAN/Associate_R260018720/apply"
    )
    assert info["job_path"] == "Mississauga-ON-CAN/Associate_R260018720"


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


def test_verify_reads_deadline_stated_in_the_job_description(monkeypatch):
    """PINS A FIXED BUG: BMO's own posting descriptions (routed here via
    myworkdayjobs.com even for rows ingested with source="phenom") state a
    real, reposting-updated deadline as literal HTML text --
    "Application Deadline:</span></p>...08/30/2026" -- that `verify()` used
    to discard entirely (deadline_dates was hardcoded `[]`), freezing
    `reverify.py`'s idea of the deadline at whatever was scraped at first
    ingest. Confirmed live 2026-08-14 against four such BMO requisitions."""
    monkeypatch.setattr(
        workday, "fetch_json",
        lambda url, **kw: {"jobPostingInfo": {
            "title": "Senior Technology Officer",
            "postedOn": "Posted 30+ Days Ago",
            "jobDescription": (
                "<p>Some intro text.</p><p><span>Application "
                "Deadline:</span></p><p>08/30/2026</p><p>More body text.</p>"
            ),
        }},
    )
    result = workday.verify(
        "https://bmo.wd3.myworkdayjobs.com/External/job/Toronto-ON-CAN/Senior-Technology-Officer_R260004979"
    )
    assert result.result == "verified-open"
    assert result.deadline_dates == ["2026-08-30"]


def test_verify_deadline_extraction_ignores_unrelated_dates(monkeypatch):
    """A date elsewhere in a lengthy description -- a start date, a posted
    date -- must not be mistaken for a deadline just because it is a
    fully-specified MM/DD/YYYY date somewhere in the text."""
    monkeypatch.setattr(
        workday, "fetch_json",
        lambda url, **kw: {"jobPostingInfo": {
            "title": "Analyst",
            "postedOn": "Posted 1 Day Ago",
            "jobDescription": "<p>Anticipated start date: 09/01/2026.</p>",
        }},
    )
    result = workday.verify(
        "https://bmo.wd3.myworkdayjobs.com/External/job/Toronto-ON-CAN/Analyst_R1"
    )
    assert result.result == "verified-open"
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


def _raise_403_wrapped(url, **kw):
    """What fetch_json ACTUALLY raises in production on a persistent 403 --
    fetch_bytes retries a non-404/410 HTTPError, then wraps the last one in
    FetchError on exhaustion (see http.py). A bare `raise HTTPError(...)`
    here would test dead code: the first version of these tests did exactly
    that, passed, and shipped a fallback that never ran in production
    (Opportunity id=17403 stayed stuck at 'unreachable' — round 2's own
    recheck caught it). Raising the real wrapper type is the only way this
    test can fail if that regresses again."""
    raise FetchError(url, "GET", urllib.error.HTTPError(url, 403, "permission denied", None, None))


def test_verify_falls_back_to_posting_page_when_cxs_api_blocked(monkeypatch):
    """Regression test for the confirmed TD Securities defect (Opportunity
    id=17403): the CxS job-detail endpoint 403s (Cloudflare `S22 permission
    denied`) while the plain posting page stays reachable and its own
    `postingAvailable` bootstrap flag reads false. Without the fallback,
    verify() could only ever report 'unreachable' here -- which reverify.py
    never acts on -- so a genuinely dead posting stayed open forever."""
    monkeypatch.setattr(workday, "fetch_json", _raise_403_wrapped)
    # Real shape: a bare JS object key, no quotes -- `window.workday = {
    # postingAvailable: false, ...}`. This page is client-side JS, not
    # JSON. A quoted-key mock here is exactly what let the regex bug (fixed
    # alongside the exception-type bug above) pass its own test unnoticed.
    monkeypatch.setattr(workday, "fetch_text",
                        lambda url, **kw: 'window.workday = {postingAvailable: false, other: 1};')

    result = workday.verify(
        "https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/Greenville-South-Carolina/"
        "Contact-Center-Rep-II--TDAF--Bilingual-Spanish-Greenville--SC-MTL--NJ-Jacksonville"
        "--FL_R_1498964-1"
    )
    assert result.result == "closed"
    assert "postingAvailable flag reads false" in result.evidence


def test_verify_fallback_confirms_open_too(monkeypatch):
    """The control case from the same investigation: a live sibling TD
    posting (id=17024) read `postingAvailable: true` from the same fallback
    path in the same run, ruling out a template/rate-limit artifact."""
    monkeypatch.setattr(workday, "fetch_json", _raise_403_wrapped)
    monkeypatch.setattr(workday, "fetch_text",
                        lambda url, **kw: 'window.workday = {postingAvailable: true, other: 1};')

    result = workday.verify(
        "https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/Toronto-Ontario/"
        "Senior-Java-Developer--TD-Securities_R_1502226"
    )
    assert result.result == "verified-open"


def test_verify_stays_unreachable_when_fallback_page_also_fails(monkeypatch):
    """No page, no flag -- still an honest 'unreachable', never a guess."""
    monkeypatch.setattr(workday, "fetch_json", _raise_403_wrapped)
    monkeypatch.setattr(workday, "fetch_text", _raise_403_wrapped)

    result = workday.verify(
        "https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/Greenville-South-Carolina/Role_R1"
    )
    assert result.result == "unreachable"


def test_posting_available_reads_the_bare_js_key_workday_actually_sends(monkeypatch):
    """Confirmed live against Opportunity id=17403's real bootstrap script:
    `postingAvailable: false,` with no quotes around the key -- a JS object
    literal, not JSON. A quoted-only regex silently never matches this and
    returns None, which is indistinguishable from 'page unreadable' to the
    caller -- the exact way this fallback kept returning 'unreachable' even
    after the exception-type bug above was fixed."""
    monkeypatch.setattr(workday, "fetch_text",
                        lambda url, **kw: 'x = {a: 1, postingAvailable: false, b: 2};')
    assert workday._posting_available_from_page("https://x.wd1.myworkdayjobs.com/Site/job/p_R1") is False


def test_posting_available_still_reads_a_quoted_key(monkeypatch):
    """Belt and braces: if Workday ever does emit real JSON with quoted
    keys, that must keep working too."""
    monkeypatch.setattr(workday, "fetch_text",
                        lambda url, **kw: '{"a": 1, "postingAvailable": true, "b": 2}')
    assert workday._posting_available_from_page("https://x.wd1.myworkdayjobs.com/Site/job/p_R1") is True


def test_verify_falls_back_on_a_bare_unwrapped_httperror_too(monkeypatch):
    """The other real shape: fetch_bytes raises 404/410 immediately,
    unretried and unwrapped (see http.py) -- a bare HTTPError, not a
    FetchError. The fallback must handle both, since a persistent
    Cloudflare 403 and an immediate 404 arrive as different exception
    types for the same reason (only one of them gets retried)."""
    def raise_404(url, **kw):
        raise urllib.error.HTTPError(url, 404, "not found", None, None)

    monkeypatch.setattr(workday, "fetch_json", raise_404)
    monkeypatch.setattr(workday, "fetch_text",
                        lambda url, **kw: 'window.workday = {"postingAvailable": false};')

    result = workday.verify(
        "https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/Greenville-South-Carolina/Role_R2"
    )
    assert result.result == "closed"


def test_verify_via_search_text_zero_results(monkeypatch):
    monkeypatch.setattr(workday, "post_json", lambda url, payload, **kw: {"total": 0, "jobPostings": []})
    result = workday.verify("https://carlyle.wd1.myworkdayjobs.com/Carlyle?q=R456")
    assert result.result == "closed"


def test_verify_needs_verification_without_path_or_query():
    result = workday.verify("https://citi.wd5.myworkdayjobs.com/Citi_Early_Careers_Events_Site")
    assert result.result == "needs-verification"


# ---------------------------------------------------------------------------
# locationsText: Workday's City/State/Country run, punctuated on its own
# empty-slot boundary. Measured live on Citi's board (2026-08-14): 49 rows
# carried an unpunctuated run, 40 carried a leading street address.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    # The empty State slot is the 2-space gap, and it is the ONE place the run
    # can be segmented without guessing.
    ("Hong Kong  Hong Kong", "Hong Kong, Hong Kong"),
    ("London  United Kingdom", "London, United Kingdom"),
    ("Kowloon  Hong Kong", "Kowloon, Hong Kong"),
    ("Seoul, Korea,  Republic of", "Seoul, Korea, Republic of"),
    # No empty slot means no boundary information. Left exactly as it arrived
    # rather than invented into: "New York New York United States" is city ==
    # state, so a repeated-token dedupe would corrupt it.
    ("New York New York United States", "New York New York United States"),
    ("Irving Texas United States", "Irving Texas United States"),
    ("Singapore", "Singapore"),
    # A street address tells a student nothing the city does not.
    ("890 Herron Road, Montreal, Quebec", "Montreal, Quebec"),
    ("1060-1068 Stelton Road, Piscataway, New Jersey", "Piscataway, New Jersey"),
    ("115 South Jefferson Rd Campus, Whippany", "Whippany"),
    # ...but only when a PLACE NAME survives it, and only at the head. Live
    # row: trimming this one leaves "Suite 500 212", a worse string than the
    # one it replaced.
    ("2121 N Pearl St, Suite 500 212", "2121 N Pearl St, Suite 500 212"),
    ("2 Locations", "2 Locations"),
    ("Milano Bicocca Calendario 3", "Milano Bicocca Calendario 3"),
    ("New York - 499 Park", "New York - 499 Park"),
    ("", ""),
])
def test_locations_text_is_punctuated_not_guessed_at(raw, expected):
    assert workday.normalize_locations_text(raw) == expected
