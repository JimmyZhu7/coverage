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
