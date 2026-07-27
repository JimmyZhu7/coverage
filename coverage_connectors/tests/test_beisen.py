"""Offline unit tests for the Beisen browser connector. `fetch()` drives a
real headless browser (exercised live, not here), so these cover the pure
parsing helpers against a synthetic GetJobAdPageList record: field mapping,
the year-2222 "no deadline" sentinel, the nested-array finder, and URL
classification. No browser, no network."""

from __future__ import annotations

from coverage_connectors import beisen
from coverage_connectors.models import BeisenBoard

BOARD = BeisenBoard(firm="CICC", host="cicc.zhiye.com")
JOB = {
    "JobAdId": 151207656,
    "Id": "abc",
    "JobAdName": "项目实习生-软件组(J19302)",
    "LocNames": ["北京市", "上海市"],
    "PostDate": "2026-07-24T11:33:27.47",
    "EndTimeInt": 0,
    "EndTime": "2222-02-02T00:00:00",
}


def test_normalize_maps_fields():
    o = beisen._normalize(JOB, BOARD)
    assert o.firm == "CICC"
    assert o.source == "beisen"
    assert o.title == "项目实习生-软件组(J19302)"
    assert o.location == "北京市 / 上海市"
    assert o.url == "https://cicc.zhiye.com/custom/jobDetail?jobId=151207656"
    assert o.posted_at == "2026-07-24"
    assert o.deadline is None  # year-2222 sentinel + EndTimeInt 0


def test_real_deadline_is_parsed():
    j = {**JOB, "EndTimeInt": 1790000000000, "EndTime": "2026-09-30T00:00:00"}
    assert beisen._normalize(j, BOARD).deadline == "2026-09-30"


def test_sentinel_year_is_not_a_deadline():
    j = {**JOB, "EndTimeInt": 1, "EndTime": "2222-02-02T00:00:00"}
    assert beisen._normalize(j, BOARD).deadline is None


def test_biggest_job_list_finds_nested_array():
    payload = {"Data": {"Result": {"PageList": [JOB, JOB]},
                        "Conditions": [{"Name": "x"}]}}
    lst = beisen._biggest_job_list(payload)
    assert len(lst) == 2
    assert lst[0]["JobAdName"]


def test_biggest_job_list_empty_when_no_jobs():
    assert beisen._biggest_job_list({"Conditions": [{"Name": "x"}]}) == []


def test_classify_url():
    u = "https://cicc.zhiye.com/custom/jobDetail?jobId=1"
    assert beisen.classify_url(u) == {"url": u}
    assert beisen.classify_url("https://boards.greenhouse.io/x") is None
    assert beisen.classify_url("") is None


def test_verify_is_board_level():
    assert beisen.verify("https://cicc.zhiye.com/x").result == "needs-verification"
    assert beisen.verify("https://other.com/x").result == "needs-verification"


# ---------------------------------------------------------------------------
# _capture — C10: a page can fire GetJobAdPageList more than once, and the
# old `holder["data"] = resp.json()` unconditionally overwrote whatever was
# captured before it, so a narrower follow-up silently discarded a broader
# response. These use a tiny fake response object (`.url`, `.json()`) rather
# than a real Playwright Response — `_capture` only ever touches those two
# attributes.
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, url, payload=None, raises=False):
        self.url = url
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


def test_capture_ignores_responses_that_are_not_the_job_list():
    holder: dict = {}
    beisen._capture(_FakeResp("https://cicc.zhiye.com/api/Other"), holder)
    assert holder == {}


def test_capture_keeps_the_larger_list_regardless_of_arrival_order():
    """PINS C10's fix: a narrower follow-up capture must NOT discard an
    already-captured broader one."""
    broad = {"Data": {"PageList": [JOB, JOB, JOB]}}
    narrow = {"Data": {"PageList": [JOB]}}
    holder: dict = {}
    beisen._capture(_FakeResp("https://x/api/GetJobAdPageList", broad), holder)
    beisen._capture(_FakeResp("https://x/api/GetJobAdPageList", narrow), holder)
    assert len(holder["jobs"]) == 3          # broad capture wins, not "last write"
    assert holder["captured"] is True

    # And the reverse order: narrow first, then broad — broad still wins.
    holder2: dict = {}
    beisen._capture(_FakeResp("https://x/api/GetJobAdPageList", narrow), holder2)
    beisen._capture(_FakeResp("https://x/api/GetJobAdPageList", broad), holder2)
    assert len(holder2["jobs"]) == 3


def test_capture_records_a_non_json_body_instead_of_swallowing_it():
    """PINS C10's fix: `except: pass` gave zero visibility into a malformed
    capture. It must at least be recorded, not silently discarded."""
    holder: dict = {}
    beisen._capture(_FakeResp("https://x/api/GetJobAdPageList", raises=True), holder)
    assert holder.get("jobs") is None
    assert holder.get("captured") is None    # a parse failure is not a successful capture
    assert len(holder.get("parse_errors") or []) == 1


# ---------------------------------------------------------------------------
# fetch() — the third C10 fix: a run where the browser never once captures a
# GetJobAdPageList response must be ok=False, not a silent "0 jobs, ok=True"
# (which a caller cannot distinguish from a genuinely empty board). A
# minimal fake stands in for Playwright's sync API — just enough surface
# (`chromium.launch`, `.new_page`, `.on`, `.goto`, `.wait_for_timeout`,
# `.close`) for `fetch()` to run its real control flow with no real browser
# and no network.
# ---------------------------------------------------------------------------

class _FakePage:
    def on(self, event, handler):
        pass  # no responses ever fire -> nothing is ever captured

    def goto(self, *a, **kw):
        pass

    def wait_for_timeout(self, ms):
        pass

    def close(self):
        pass


class _FakeBrowser:
    def new_page(self):
        return _FakePage()

    def close(self):
        pass


class _FakeChromium:
    def launch(self, headless=True):
        return _FakeBrowser()


class _FakeSyncPlaywright:
    chromium = _FakeChromium()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_reports_failure_when_nothing_is_ever_captured(monkeypatch):
    """PINS C10's fix directly: before it, this exact scenario (a browser
    session that runs cleanly but never sees a GetJobAdPageList response —
    e.g. the site renamed the endpoint) returned `FetchResult(ok=True,
    opportunities=[], raw_count=0)`, indistinguishable from a genuinely
    empty board."""
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _FakeSyncPlaywright())
    result = beisen.fetch(BOARD)
    assert result.ok is False
    assert "no GetJobAdPageList response captured" in result.error
