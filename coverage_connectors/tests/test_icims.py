"""iCIMS connector — server-rendered career-portal search pages (SIG, Stifel)."""

from __future__ import annotations

import urllib.error

import pytest

from coverage_connectors import icims
from coverage_connectors.models import IcimsBoard

BOARD = IcimsBoard(firm="SIG", tenant="careers-sig")

# Shape captured live from careers-sig.icims.com 2026-08-08: JobCardItem
# <li>s, title anchor carrying "<id> - <title>" and the canonical
# /jobs/<id>/<slug>/job URL with the widget's ?in_iframe=1 appended.
_PAGE = """
<li class="iCIMS_JobCardItem">
<div class="col-xs-12 title">
<a href="https://careers-sig.icims.com/jobs/10966/accounting-internship%3a-summer-2027/job?in_iframe=1"
   class="iCIMS_Anchor" title="10966 - Accounting Internship: Summer 2027">
<h3>Accounting Internship: Summer 2027</h3></a>
</div></li>
<li class="iCIMS_JobCardItem">
<div class="col-xs-12 title">
<a href="https://careers-sig.icims.com/jobs/11098/quant-trading-intern/job?in_iframe=1"
   class="iCIMS_Anchor" title="11098 - Quant Trading Intern &amp; Co-op">
<h3>Quant Trading Intern &amp; Co-op</h3></a>
</div></li>
"""
_EMPTY_PAGE = "<html><body><ul></ul></body></html>"


def test_parses_rows_and_strips_the_req_id_prefix(monkeypatch):
    pages = iter([_PAGE, _EMPTY_PAGE])
    monkeypatch.setattr(icims, "fetch_text", lambda url, **kw: next(pages))
    r = icims.fetch(BOARD)
    assert r.ok and r.raw_count == 2
    titles = [o.title for o in r.opportunities]
    # The anchor's title attr renders "10966 - <title>"; rows must carry the
    # human title, with entities decoded.
    assert titles == ["Accounting Internship: Summer 2027", "Quant Trading Intern & Co-op"]
    assert all(o.source == "icims" for o in r.opportunities)
    # The widget's ?in_iframe=1 is stripped — /jobs/<id>/<slug>/job is the
    # stable canonical posting URL rows must carry.
    assert r.opportunities[0].url.endswith("/job")


def test_pagination_stops_when_a_page_repeats(monkeypatch):
    # iCIMS repeats the last page for out-of-range pr= values instead of
    # 404ing — the sweep must stop on the first page with no NEW ids, or it
    # would run to the page cap on every fetch.
    calls = []
    def fake(url, **kw):
        calls.append(url)
        return _PAGE
    monkeypatch.setattr(icims, "fetch_text", fake)
    r = icims.fetch(BOARD)
    assert r.ok and r.raw_count == 2
    assert len(calls) == 2  # page 0 (new rows), page 1 (all repeats) — stop.
    assert "pr=0" in calls[0] and "pr=1" in calls[1]


def test_a_bot_challenge_is_a_failure_not_an_empty_board(monkeypatch):
    challenge = "<html><title>Quick Check Needed</title><altcha-widget/></html>"
    monkeypatch.setattr(icims, "fetch_text", lambda url, **kw: challenge)
    r = icims.fetch(BOARD)
    assert r.ok is False and r.raw_count == 0
    assert "bot protection" in r.error


def test_verify_reads_json_ld_posted_date(monkeypatch):
    page = ('<html><head><title>Accounting Internship: Summer 2027 in Bala Cynwyd</title>'
            '<script type="application/ld+json">{"@type": "JobPosting",'
            '"datePosted": "2026-06-29T04:00:00.000Z"}</script></head></html>')
    seen = {}
    def fake(url, **kw):
        seen["url"] = url
        return page
    monkeypatch.setattr(icims, "fetch_text", fake)
    v = icims.verify("https://careers-sig.icims.com/jobs/10966/accounting-internship/job")
    assert v.result == "verified-open"
    assert v.posted_date == "2026-06-29"
    # The widget variant is the one carrying JSON-LD — verify must ask for it.
    assert "in_iframe=1" in seen["url"]


@pytest.mark.parametrize("code", [404, 410])
def test_verify_reads_gone_reqs_as_closed(monkeypatch, code):
    # iCIMS answers 410 Gone (observed live) or 404 for filled reqs.
    def fake(url, **kw):
        raise urllib.error.HTTPError(url, code, "gone", {}, None)
    monkeypatch.setattr(icims, "fetch_text", fake)
    v = icims.verify("https://careers-sig.icims.com/jobs/99999/gone/job")
    assert v.result == "closed"


def test_verify_rejects_a_foreign_url():
    v = icims.verify("https://example.com/jobs/1/x/job")
    assert v.result == "needs-verification"
