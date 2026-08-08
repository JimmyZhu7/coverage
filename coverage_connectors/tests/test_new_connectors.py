"""Tests for the three ported connectors (oracle / talnet / sitemap).

Fetch tests monkeypatch each module's http entry point with canned
payloads — the tal.net ones are the radar's own captured board samples —
and the verify tests exercise the verdict logic the same way. No live
network anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coverage_connectors import (
    OracleBoard,
    SitemapBoard,
    TalnetBoard,
    fetch,
    verify,
)
from coverage_connectors import oracle as oracle_mod
from coverage_connectors import sitemap as sitemap_mod
from coverage_connectors import talnet as talnet_mod

FIXTURES = Path(__file__).parent / "fixtures"

JPM = OracleBoard(firm="J.P. Morgan", host="jpmc.fa.oraclecloud.com",
                  site_number="CX_1001", keywords=("summer analyst", "insight"))
MS_EVENTS = TalnetBoard(
    firm="Morgan Stanley", kind="events",
    board_url="https://morganstanley.tal.net/vx/lang-en-GB/mobile-0/appcentre-1/brand-2/candidate/jobboard/vacancy/2/adv/",
)
BOFA_JOBS = TalnetBoard(
    firm="Bank of America", kind="jobs",
    board_url="https://bankcampuscareers.tal.net/vx/mobile-0/brand-4/candidate/jobboard/vacancy/1/adv/",
)
HSBC = SitemapBoard(firm="HSBC", sitemap_url="https://apply.careers.hsbc.com/sitemap.xml",
                    path_filter="/emergingtalent/job/")


# --------------------------------------------------------------------- oracle

_ORACLE_PAYLOAD = {
    "items": [{"requisitionList": [
        {"Id": 210658163, "Title": "2027 Global Banking Summer Analyst Program",
         "PrimaryLocation": "New York, NY, United States",
         "PostedDate": "2026-07-01", "PostingEndDate": "2026-09-15"},
        {"Id": 210658164, "Title": "Software Engineer Program Insight Day",
         "PrimaryLocation": "Hong Kong", "PostedDate": "2026-07-10", "PostingEndDate": None},
    ]}]
}


def test_oracle_fetch_dedupes_across_keywords(monkeypatch):
    calls = []

    def fake_fetch_json(url, **kw):
        calls.append(url)
        return _ORACLE_PAYLOAD  # same two reqs for every keyword

    monkeypatch.setattr(oracle_mod, "fetch_json", fake_fetch_json)
    result = fetch(JPM)
    assert result.ok
    assert len(calls) == 2                       # one search per keyword
    assert len(result.opportunities) == 2        # deduped by requisition Id
    first = result.opportunities[0]
    assert first.title == "2027 Global Banking Summer Analyst Program"
    assert first.deadline == "2026-09-15"        # PostingEndDate is a real deadline
    assert first.posted_at == "2026-07-01"
    assert first.url == ("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
                          "CX_1001/job/210658163")
    assert result.opportunities[1].deadline is None


def test_oracle_verify_id_found(monkeypatch):
    monkeypatch.setattr(oracle_mod, "fetch_json", lambda url, **kw: _ORACLE_PAYLOAD)
    url = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210658163"
    v = verify(url)
    assert v.provider == "oracle" and v.result == "verified-open"
    assert v.deadline_dates == ["2026-09-15"]


def test_oracle_verify_does_not_close_when_id_is_missing(monkeypatch):
    """PINS A FIXED BUG (C3): this test used to be named
    `test_oracle_verify_id_found_and_missing` and asserted the "missing" half
    resolved to `result == "closed"`. `_search` returns `[]` both for a
    genuine empty result AND for a missing/renamed envelope key
    (`data.get("items", [])` / `items[0].get("requisitionList", [])`) — those
    two cases are indistinguishable at the call site, so treating "not
    found" as "closed" risked a one-shot feed deletion on nothing more than
    an API shape Oracle changed. Not found must be `needs-verification`."""
    monkeypatch.setattr(oracle_mod, "fetch_json", lambda url, **kw: _ORACLE_PAYLOAD)
    url = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210658163"
    gone = url.replace("210658163", "999999999")
    v = verify(gone)
    assert v.result == "needs-verification"


def test_oracle_verify_does_not_close_on_a_malformed_envelope(monkeypatch):
    """The concrete failure mode C3 describes: Oracle renames/omits a key
    (here, `items` itself is missing) and `_search` swallows it into `[]`,
    exactly as it would for a genuinely empty result. Must not close."""
    monkeypatch.setattr(oracle_mod, "fetch_json", lambda url, **kw: {"unexpectedKey": []})
    url = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210658163"
    v = verify(url)
    assert v.result == "needs-verification"


# --------------------------------------------------------------------- talnet

def test_talnet_jobs_board_parses_fixture(monkeypatch):
    html = (FIXTURES / "talnet_jobs_sample.html").read_text()
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: html)
    result = fetch(BOFA_JOBS)
    assert result.ok
    titles = [o.title for o in result.opportunities]
    # No region policy in the connector: the Tokyo row is reported too.
    assert "2027 Investment Banking Summer Analyst Program (Hong Kong)" in titles
    assert any("Tokyo" in t for t in titles)
    hk = result.opportunities[0]
    assert hk.location == "Hong Kong"
    assert hk.source == "talnet"
    assert "/opp/10001-" in hk.url
    # The per-session xf-<hex> segment is stripped: URLs must be stable
    # across fetches or dedup and closed-detection both break.
    assert "/xf-" not in hk.url


def test_talnet_events_board_extracts_deadline(monkeypatch):
    html = (FIXTURES / "talnet_events_sample.html").read_text()
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: html)
    result = fetch(MS_EVENTS)
    assert result.ok and result.opportunities
    with_deadline = [o for o in result.opportunities if o.deadline]
    assert with_deadline, "events fixture carries a Registration Deadline column"
    # dd/mm/yyyy -> ISO
    assert all(len(o.deadline) == 10 and o.deadline[4] == "-" for o in with_deadline)


def test_talnet_verify_reads_closed_language(monkeypatch):
    page = ('<html><head><title>Sophomore Summer Analyst</title>'
            '<meta name="description" content="Event Date: 05/08/2026 '
            'Registration Deadline: 28/07/2026"></head>'
            '<body>This vacancy has closed.</body></html>')
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: page)
    v = verify("https://morganstanley.tal.net/vx/candidate/so/pm/1/pl/1/opp/1234-Sophomore/en-GB")
    assert v.provider == "talnet" and v.result == "closed"
    assert v.deadline_dates == ["2026-08-05", "2026-07-28"]

    open_page = page.replace("This vacancy has closed.", "Apply now.")
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: open_page)
    v = verify("https://morganstanley.tal.net/vx/candidate/so/pm/1/pl/1/opp/1234-Sophomore/en-GB")
    assert v.result == "verified-open"


# A tal.net tenant can switch on Oleeo Protect at any time (Evercore did,
# observed 2026-08-08). The challenge page is served with HTTP 200, so both
# entry points have to name it rather than treat it as content.

_CHALLENGE_PAGE = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
    '<title>Quick Check Needed</title>'
    '<script async defer src="/vx/oleeoProtect/main.js" type="module"></script>'
    "</head><body><div class=\"container\"><h1>Quick Check Needed</h1>"
    "<p>We just need to confirm you're a real person.</p>"
    "<altcha-widget></altcha-widget></body></html>"
)


def test_talnet_fetch_names_bot_protection_instead_of_reporting_an_empty_board(monkeypatch):
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: _CHALLENGE_PAGE)
    result = fetch(BOFA_JOBS)
    # ok=False is the load-bearing half: an ok=True/0-row result reads
    # downstream as a shape change and sends someone to fix a fine parser,
    # and it is the ingest layer's ok flag that protects the firm's open
    # postings from a false mass auto-close.
    assert result.ok is False
    assert result.opportunities == [] and result.raw_count == 0
    assert "bot protection" in result.error and "Oleeo Protect" in result.error


def test_talnet_verify_does_not_rubber_stamp_a_challenge_page_as_open(monkeypatch):
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: _CHALLENGE_PAGE)
    v = verify("https://evercore.tal.net/vx/candidate/so/pm/1/pl/1/opp/1234-Analyst/en-GB")
    # A challenge page carries no closed-language, so the pre-check is the
    # only thing standing between it and a permanent "verified-open".
    assert v.result == "unreachable"
    assert "bot protection" in v.evidence


def test_bot_challenge_detector_leaves_real_boards_alone():
    from coverage_connectors.http import bot_challenge_reason

    assert bot_challenge_reason(_CHALLENGE_PAGE) == "Oleeo Protect"
    assert bot_challenge_reason((FIXTURES / "talnet_jobs_sample.html").read_text()) is None
    assert bot_challenge_reason("") is None


# -------------------------------------------------------------------- sitemap

_SITEMAP_XML = """<?xml version="1.0"?>
<urlset>
  <url><loc>https://apply.careers.hsbc.com/emergingtalent/job/Global-Banking-Internship-Hong/12345</loc></url>
  <url><loc>https://apply.careers.hsbc.com/emergingtalent/job/Wealth-Graduate-Programme-London/23456</loc></url>
  <url><loc>https://apply.careers.hsbc.com/careers/job/Senior-Manager-Risk/34567</loc></url>
</urlset>"""


def test_sitemap_fetch_filters_path_and_derives_titles(monkeypatch):
    """PINS A FIXED BUG (C7): this test used to assert `hk.location ==
    "Hong Kong"`, inferred from a bare "Hong" token surviving HSBC's slug
    truncation ("…-Hong-Kong" -> "…-Hong"). That directly violated
    `models.py`'s "nothing here is inferred, guessed, or filled in" — a
    sitemap carries no location field at all, so every row's location must
    be blank, the same honest answer as any other field this source lacks."""
    monkeypatch.setattr(sitemap_mod, "fetch_text", lambda url, **kw: _SITEMAP_XML)
    result = fetch(HSBC)
    assert result.ok
    assert len(result.opportunities) == 2        # the /careers/ row is filtered out
    hk = result.opportunities[0]
    assert hk.title == "Global Banking Internship Hong"
    assert hk.location == ""                     # never inferred from the slug
    assert result.opportunities[1].location == ""


def test_sitemap_verify_rereads_the_sitemap(monkeypatch):
    monkeypatch.setattr(sitemap_mod, "fetch_text", lambda url, **kw: _SITEMAP_XML)
    fetch(HSBC)  # registers the host -> sitemap mapping
    listed = "https://apply.careers.hsbc.com/emergingtalent/job/Global-Banking-Internship-Hong/12345"
    assert verify(listed).result == "verified-open"
    gone = "https://apply.careers.hsbc.com/emergingtalent/job/Something-Else/99999"
    assert verify(gone).result == "closed"


def test_sitemap_verify_does_not_false_open_on_a_shared_id_prefix(monkeypatch):
    """PINS C6's fix: a raw `url in xml` substring test false-matches a
    REMOVED posting whose id is a string-prefix of a still-listed one —
    ".../job/X/123" is literally a substring of ".../job/X/1234". Exact
    `<loc>` membership must tell these apart."""
    xml = ('<urlset><url><loc>https://apply.careers.hsbc.com/emergingtalent/'
           'job/Global-Banking-Internship-Hong/1234</loc></url></urlset>')
    monkeypatch.setattr(sitemap_mod, "fetch_text", lambda url, **kw: xml)
    fetch(SitemapBoard(firm="HSBC", sitemap_url="https://apply.careers.hsbc.com/sitemap.xml",
                        path_filter="/emergingtalent/job/"))
    removed = "https://apply.careers.hsbc.com/emergingtalent/job/Global-Banking-Internship-Hong/123"
    assert verify(removed).result == "closed"


def test_sitemap_verify_does_not_false_close_on_an_escaped_ampersand(monkeypatch):
    """PINS C6's fix: sitemaps escape `&` as `&amp;` in `<loc>`, so a stored
    URL with a real `&` (a query string) never matches raw against the
    escaped text — a false "closed" on a posting that is genuinely still
    listed. Unescaping before the exact-membership test fixes it."""
    listed_url = "https://apply.careers.hsbc.com/emergingtalent/job/Role-Hong/555?a=1&b=2"
    xml = f'<urlset><url><loc>{listed_url.replace("&", "&amp;")}</loc></url></urlset>'
    monkeypatch.setattr(sitemap_mod, "fetch_text", lambda url, **kw: xml)
    fetch(SitemapBoard(firm="HSBC", sitemap_url="https://apply.careers.hsbc.com/sitemap.xml",
                        path_filter="/emergingtalent/job/"))
    assert verify(listed_url).result == "verified-open"


# ---------------------------------------------------- mckinsey / phenom / gs

from coverage_connectors import GoldmanSachsBoard, McKinseyBoard, PhenomBoard
from coverage_connectors import goldmansachs as gs_mod
from coverage_connectors import mckinsey as mck_mod
from coverage_connectors import phenom as phenom_mod


def test_mckinsey_paginates_and_dedupes(monkeypatch):
    payload = {"numFound": 2, "docs": [
        # Cities deliberately NOT in alphabetical order in the source payload
        # — `_location` must sort before formatting (C9) so a reordered-but-
        # unchanged `cities` array on a later fetch can never flip which city
        # is reported "first" and, with it, `content_hash`.
        {"jobID": "1", "title": "Business Analyst Intern", "cities": ["New York", "Boston"],
         "friendlyURL": "business-analyst-intern"},
        {"jobID": "2", "title": "Campus Recruiter", "cities": ["Chicago"],
         "friendlyURL": "campus-recruiter"},
    ]}
    monkeypatch.setattr(mck_mod, "fetch_json", lambda url, **kw: payload)
    result = fetch(McKinseyBoard(firm="McKinsey & Company", keywords=("intern",)))
    assert result.ok and len(result.opportunities) == 2
    ba = result.opportunities[0]
    assert ba.location == "Boston +1 more"       # multi-city honesty, sorted
    assert ba.url.endswith("/business-analyst-intern")


def test_mckinsey_verify_open_when_slug_matches(monkeypatch):
    payload = {"numFound": 1, "docs": [
        {"jobID": "1", "title": "Business Analyst Intern", "cities": ["Boston"],
         "friendlyURL": "business-analyst-intern"},
    ]}
    monkeypatch.setattr(mck_mod, "fetch_json", lambda url, **kw: payload)
    v = verify("https://www.mckinsey.com/careers/search-jobs/jobs/business-analyst-intern")
    assert v.result == "verified-open"


def test_mckinsey_verify_does_not_close_when_slug_is_absent(monkeypatch):
    """PINS C4's fix: `verify` only checks ONE 50-row page with a keyword
    derived by lopping the slug's first token off — nowhere near the full
    board `fetch` walks. A slug not on that single page is not a confirmed
    closure, just a coverage gap in this connector's own re-check."""
    monkeypatch.setattr(mck_mod, "fetch_json", lambda url, **kw: {"numFound": 0, "docs": []})
    v = verify("https://www.mckinsey.com/careers/search-jobs/jobs/business-analyst-intern")
    assert v.result == "needs-verification"


def test_mckinsey_verify_does_not_close_on_a_malformed_envelope(monkeypatch):
    """The concrete C4 failure mode: `docs` renamed/missing silently becomes
    `[]` via `.get("docs", [])`, indistinguishable from a genuine zero
    matches. Must not close."""
    monkeypatch.setattr(mck_mod, "fetch_json", lambda url, **kw: {"unexpectedKey": []})
    v = verify("https://www.mckinsey.com/careers/search-jobs/jobs/business-analyst-intern")
    assert v.result == "needs-verification"


def test_mckinsey_location_is_stable_regardless_of_city_order(monkeypatch):
    """PINS C9's fix directly: the same two cities in the opposite order must
    format identically, or a later fetch that gets them back in a different
    order (same posting, same cities) would flip `content_hash` for a role
    nothing actually changed about."""
    forward = {"numFound": 1, "docs": [
        {"jobID": "1", "title": "Role", "cities": ["Zurich", "Amsterdam"], "friendlyURL": "role"},
    ]}
    backward = {"numFound": 1, "docs": [
        {"jobID": "1", "title": "Role", "cities": ["Amsterdam", "Zurich"], "friendlyURL": "role"},
    ]}
    monkeypatch.setattr(mck_mod, "fetch_json", lambda url, **kw: forward)
    a = fetch(McKinseyBoard(firm="McKinsey & Company")).opportunities[0]
    monkeypatch.setattr(mck_mod, "fetch_json", lambda url, **kw: backward)
    b = fetch(McKinseyBoard(firm="McKinsey & Company")).opportunities[0]
    assert a.location == b.location == "Amsterdam +1 more"


def test_phenom_reads_refinesearch(monkeypatch):
    payload = {"refineSearch": {"totalHits": 1, "data": {"jobs": [
        {"jobId": "9", "title": "Consulting Intern", "city": "Boston", "country": "United States",
         "applyUrl": "https://studenttalent.bcg.com/careerhub/explore/jobs/123?x=1",
         "dateCreated": "2026-07-01T00:00:00"},
    ]}}}
    monkeypatch.setattr(phenom_mod, "post_json", lambda url, payload, **kw: payload if False else payload)
    monkeypatch.setattr(phenom_mod, "post_json", lambda url, p, **kw: payload)
    result = fetch(PhenomBoard(firm="BCG", host="careers.bcg.com", keywords="intern"))
    assert result.ok and len(result.opportunities) == 1
    j = result.opportunities[0]
    assert j.location == "Boston, United States"
    assert "?" not in j.url                          # query string stripped
    assert j.posted_at == "2026-07-01"


def test_goldman_campus_query(monkeypatch):
    rs = {"data": {"roleSearch": {"totalCount": 1, "items": [
        {"roleId": "180086_GS_CAMPUS",
         "jobTitle": "2027 | APEJ | Hong Kong | Investment Banking | Summer Analyst",
         "corporateTitle": "Analyst",
         "locations": [{"city": None, "state": "Hong Kong", "country": "Hong Kong", "primary": True}]},
    ]}}}
    monkeypatch.setattr(gs_mod, "fetch_json", lambda url, **kw: rs)
    result = fetch(GoldmanSachsBoard())
    assert result.ok and len(result.opportunities) == 1
    o = result.opportunities[0]
    assert o.title == "Investment Banking — Summer Analyst"   # year/region/city prefix dropped
    assert o.location == "Hong Kong"
    assert o.url == "https://higher.gs.com/roles/180086_GS_CAMPUS"


def test_goldman_verify_open_when_role_present(monkeypatch):
    rs = {"data": {"roleSearch": {"totalCount": 1, "items": [
        {"roleId": "180086_GS_CAMPUS", "jobTitle": "x", "locations": []},
    ]}}}
    monkeypatch.setattr(gs_mod, "fetch_json", lambda url, **kw: rs)
    v = verify("https://higher.gs.com/roles/180086_GS_CAMPUS")
    assert v.result == "verified-open"


def test_goldman_verify_does_not_close_on_an_empty_role_search_envelope(monkeypatch):
    """PINS C5's fix directly: `data.roleSearch` null-but-error-free makes
    `_post` return `{}`, and `{}.get("totalCount") or 0` reads as `total=0` —
    which ends the loop after page 0 having found nothing, for EVERY role in
    one sweep. This must never read as a confirmed closure."""
    monkeypatch.setattr(gs_mod, "fetch_json", lambda url, **kw: {"data": {"roleSearch": None}})
    v = verify("https://higher.gs.com/roles/180086_GS_CAMPUS")
    assert v.result == "needs-verification"


def test_goldman_verify_does_not_close_when_role_is_genuinely_absent(monkeypatch):
    rs = {"data": {"roleSearch": {"totalCount": 1, "items": [
        {"roleId": "OTHER_ROLE", "jobTitle": "x", "locations": []},
    ]}}}
    monkeypatch.setattr(gs_mod, "fetch_json", lambda url, **kw: rs)
    v = verify("https://higher.gs.com/roles/180086_GS_CAMPUS")
    assert v.result == "needs-verification"


def test_talentgateway_parses_embedded_results(monkeypatch):
    from coverage_connectors import TalentGatewayBoard
    from coverage_connectors import talentgateway as tg
    import html as _h, json as _j
    payload = {"HotJobs": {"Job": [
        {"Link": "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25008&siteid=5131&PageType=JobDetails&jobid=349091",
         "NoOfDaysToExpire": 0,
         "Questions": [
            {"QuestionName": "reqid", "Value": "349091"},
            {"QuestionName": "jobtitle", "Value": "2026 Off-cycle Internship \u2013 IB Marketing &amp; Events"},
            {"QuestionName": "formtext23", "Value": "Switzerland - Z\u00fcrich"},
         ]},
    ]}}
    page = '<html><body><input id="searchResults" type="hidden" value="' + \
        _h.escape(_j.dumps(payload), quote=True) + '"></body></html>'
    monkeypatch.setattr(tg, "fetch_text", lambda url, **kw: page)
    result = fetch(TalentGatewayBoard(firm="UBS", partner_id=25008, site_id=5131))
    assert result.ok and len(result.opportunities) == 1
    o = result.opportunities[0]
    assert o.title == "2026 Off-cycle Internship \u2013 IB Marketing & Events"   # entity decoded, en-dash intact
    assert o.location == "Switzerland - Z\u00fcrich"
    assert "jobid=349091" in o.url


def _tg_page(reqid: str) -> str:
    import html as _h
    import json as _j
    payload = {"HotJobs": {"Job": [{
        "Link": f"https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad?jobid={reqid}",
        "Questions": [{"QuestionName": "reqid", "Value": reqid},
                      {"QuestionName": "jobtitle", "Value": "Some Role"},
                      {"QuestionName": "formtext23", "Value": "Zurich"}],
    }]}}
    return '<html><body><input id="searchResults" value="' + \
        _h.escape(_j.dumps(payload), quote=True) + '"></body></html>'


def test_talentgateway_verify_open_when_reqid_present(monkeypatch):
    from coverage_connectors import talentgateway as tg
    monkeypatch.setattr(tg, "fetch_text", lambda url, **kw: _tg_page("349091"))
    result = verify("https://jobs.ubs.com/TGnewUI/Search/home?jobid=349091")
    assert result.result == "verified-open"


def test_talentgateway_verify_does_not_close_on_absence(monkeypatch):
    """PINS C2's fix: this board's own docstring says pagination is IGNORED
    server-side, so every `&page=N` request in `verify()` re-fetches the same
    ~10-job featured slice \u2014 a reqid among the other ~79 rows the fetch never
    sees is indistinguishable from one that's still open. Absence here must
    be `needs-verification`, never `closed` \u2014 the previous behaviour turned
    "not one of the 10 featured jobs today" into a one-shot feed deletion."""
    from coverage_connectors import talentgateway as tg
    # Every page (including the different `&page=N` variants) returns the
    # SAME featured job \u2014 reqid 111, never the target 999999 \u2014 matching the
    # documented "pagination is ignored" behaviour exactly.
    monkeypatch.setattr(tg, "fetch_text", lambda url, **kw: _tg_page("111"))
    result = verify("https://jobs.ubs.com/TGnewUI/Search/home?jobid=999999")
    assert result.result == "needs-verification"


def test_talentgateway_verify_unreachable_on_error(monkeypatch):
    from coverage_connectors import talentgateway as tg

    def boom(url, **kw):
        raise ConnectionError("timed out")

    monkeypatch.setattr(tg, "fetch_text", boom)
    result = verify("https://jobs.ubs.com/TGnewUI/Search/home?jobid=349091")
    assert result.result == "unreachable"
