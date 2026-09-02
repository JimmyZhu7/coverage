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
        return _ORACLE_PAYLOAD  # same two reqs for every keyword, for every endpoint

    monkeypatch.setattr(oracle_mod, "fetch_json", fake_fetch_json)
    result = fetch(JPM)
    assert result.ok
    # 2 searches (one per keyword) + 2 DETAILS requests (one per unique
    # requisition discovered — J.P. Morgan is in `_EXTERNAL_DEADLINE_HOSTS`,
    # see the scope tests below). The second keyword's reqs are already in
    # `seen`, so it triggers no further DETAILS calls.
    assert len(calls) == 4
    assert len(result.opportunities) == 2        # deduped by requisition Id
    first = result.opportunities[0]
    assert first.title == "2027 Global Banking Summer Analyst Program"
    # This fixture's DETAILS response (same payload, no ExternalPostedEndDate
    # key) carries no override, so this still falls back to the old
    # PostingEndDate field — proving the fallback path is unchanged.
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


def test_oracle_fetch_flags_truncation_when_total_exceeds_returned(monkeypatch):
    """CONFIRMED DEFECT: unlike workday.py/eightfold.py/avature.py/
    icims.py/goldmansachs.py, this connector never set `truncated` at all,
    so ingest.py's truncated-pair exemption from closed-detection could
    never engage — an under-returned oracle search (capped at limit=25 per
    keyword, no pagination) ran the normal close-on-absence path unguarded.
    Reproduced live: JPM's "insight" keyword reports TotalJobsCount=1631
    and opportunity 4731's requisition was outside the top 25 returned,
    despite being genuinely live — it was falsely closed as a result."""
    payload_with_more = {
        "items": [{"TotalJobsCount": 1631, "requisitionList": [
            {"Id": 1, "Title": "Role One", "PrimaryLocation": "NYC"},
        ]}]
    }
    monkeypatch.setattr(oracle_mod, "fetch_json", lambda url, **kw: payload_with_more)
    result = fetch(JPM)
    assert result.ok
    assert result.truncated is True


def test_oracle_fetch_not_truncated_when_total_matches_returned(monkeypatch):
    monkeypatch.setattr(oracle_mod, "fetch_json", lambda url, **kw: _ORACLE_PAYLOAD)
    result = fetch(JPM)
    assert result.ok
    assert result.truncated is False


def test_oracle_fetch_not_truncated_when_envelope_carries_no_total(monkeypatch):
    """A missing/renamed TotalJobsCount must not be misread as truncation —
    same "can't tell, so don't act" posture verify() already takes on a
    malformed envelope."""
    payload_no_total = {"items": [{"requisitionList": [
        {"Id": 1, "Title": "Role One", "PrimaryLocation": "NYC"},
    ]}]}
    monkeypatch.setattr(oracle_mod, "fetch_json", lambda url, **kw: payload_no_total)
    result = fetch(JPM)
    assert result.ok
    assert result.truncated is False


def test_oracle_verify_does_not_close_on_a_malformed_envelope(monkeypatch):
    """The concrete failure mode C3 describes: Oracle renames/omits a key
    (here, `items` itself is missing) and `_search` swallows it into `[]`,
    exactly as it would for a genuinely empty result. Must not close."""
    monkeypatch.setattr(oracle_mod, "fetch_json", lambda url, **kw: {"unexpectedKey": []})
    url = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210658163"
    v = verify(url)
    assert v.result == "needs-verification"


# ---------------------------------- oracle: DETAILS-endpoint deadline scope

def _oracle_search_payload(rid: int, posting_end_date: str | None = None) -> dict:
    return {"items": [{"requisitionList": [
        {"Id": rid, "Title": "Some Role", "PrimaryLocation": "New York, NY, United States",
         "PostedDate": "2026-08-01", "PostingEndDate": posting_end_date},
    ]}]}


def _oracle_details_payload(rid: int, external_posted_end_date: str) -> dict:
    return {"items": [{"requisitionList": [
        {"Id": rid, "ExternalPostedEndDate": external_posted_end_date},
    ]}]}


def test_oracle_fetch_extracts_jpm_deadline_from_details_endpoint(monkeypatch):
    """THE CONFIRMED FIX: `PostingEndDate` off the SEARCH endpoint
    (`recruitingCEJobRequisitions`) is always null — verified live against
    Oracle's own API for two real J.P. Morgan requisitions (210778140,
    210747420). The real "Apply Before" date lives on the DETAILS endpoint
    (`recruitingCEJobRequisitionDetails`) under `ExternalPostedEndDate`. This
    is in scope for J.P. Morgan (host `jpmc.fa.oraclecloud.com`) only — see
    `_EXTERNAL_DEADLINE_HOSTS`."""
    calls = []

    def fake_fetch_json(url, **kw):
        calls.append(url)
        if "recruitingCEJobRequisitionDetails" in url:
            return _oracle_details_payload(210778140, "2026-09-07T15:59:00+00:00")
        return _oracle_search_payload(210778140, posting_end_date=None)

    monkeypatch.setattr(oracle_mod, "fetch_json", fake_fetch_json)
    result = fetch(JPM)
    assert result.ok
    assert len(result.opportunities) == 1
    opp = result.opportunities[0]
    # Day precision, same [:10] slicing convention greenhouse.py's
    # application_deadline uses — ingest.py stamps deadline_precision="day"
    # and confidence=1.0 off nothing more than `opp.deadline` being set, so
    # matching that convention here is just: populate the field correctly.
    assert opp.deadline == "2026-09-07"
    assert any("recruitingCEJobRequisitionDetails" in c for c in calls), (
        "fetch() must make the extra DETAILS request for an in-scope firm"
    )


def test_oracle_fetch_extracts_deadline_for_lazard_and_schroders(monkeypatch):
    """FOUNDER-ACCEPTED-RISK PIN: a human check of Lazard's and Schroders'
    live posting pages (2026-08-29) found `ExternalPostedEndDate` populated
    but never shown to candidates there — it reads as internal, not the
    confirmed-candidate-facing bar J.P. Morgan cleared. The founder was told
    this plainly and chose to extract it anyway (2026-08-30): more coverage
    over the risk some of these are wrong. This pins THAT decision — both
    firms must get the DETAILS-endpoint deadline like J.P. Morgan does, not
    the JPM-only scoping that predates it."""
    calls = []

    def fake_fetch_json(url, **kw):
        calls.append(url)
        if "recruitingCEJobRequisitionDetails" in url:
            return _oracle_details_payload(999111, "2026-10-01T15:59:00+00:00")
        return _oracle_search_payload(999111, posting_end_date=None)

    monkeypatch.setattr(oracle_mod, "fetch_json", fake_fetch_json)
    lazard = OracleBoard(firm="Lazard", host="icbpjb.fa.ocs.oraclecloud.com",
                          site_number="CX_1", keywords=("intern",))
    result = fetch(lazard)
    assert result.ok
    assert len(result.opportunities) == 1
    assert result.opportunities[0].deadline == "2026-10-01"
    assert any("recruitingCEJobRequisitionDetails" in c for c in calls)

    schroders = OracleBoard(firm="Schroders", host="ekbq.fa.em2.oraclecloud.com",
                             site_number="CX_2", keywords=("intern",))
    calls.clear()
    result = fetch(schroders)
    assert result.opportunities[0].deadline == "2026-10-01"
    assert any("recruitingCEJobRequisitionDetails" in c for c in calls)


def test_oracle_verify_extracts_jpm_deadline_from_details_endpoint(monkeypatch):
    """`verify()` shares the same DETAILS-endpoint fix as `fetch()` — without
    it, a reverify pass could never self-heal a row whose deadline was
    ingested null before this fix landed."""
    def fake_fetch_json(url, **kw):
        if "recruitingCEJobRequisitionDetails" in url:
            return _oracle_details_payload(210778140, "2026-09-07T15:59:00+00:00")
        return _oracle_search_payload(210778140, posting_end_date=None)

    monkeypatch.setattr(oracle_mod, "fetch_json", fake_fetch_json)
    url = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210778140"
    v = verify(url)
    assert v.result == "verified-open"
    assert v.deadline_dates == ["2026-09-07"]
    assert "ExternalPostedEndDate=2026-09-07" in v.evidence


def test_oracle_verify_extracts_deadline_for_lazard(monkeypatch):
    """Same founder-accepted-risk proof as the fetch()-side test, through
    verify() — a reverify pass must self-heal a Lazard row the same way it
    does a J.P. Morgan one."""
    calls = []

    def fake_fetch_json(url, **kw):
        calls.append(url)
        if "recruitingCEJobRequisitionDetails" in url:
            return _oracle_details_payload(999111, "2026-10-01T15:59:00+00:00")
        return _oracle_search_payload(999111, posting_end_date=None)

    monkeypatch.setattr(oracle_mod, "fetch_json", fake_fetch_json)
    url = ("https://icbpjb.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
           "CX_1/job/999111")
    v = verify(url)
    assert v.result == "verified-open"
    assert v.deadline_dates == ["2026-10-01"]
    assert "ExternalPostedEndDate=2026-10-01" in v.evidence
    assert any("recruitingCEJobRequisitionDetails" in c for c in calls)


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


_SITEMAP_XML_WITH_POSTAL_SUFFIXES = """<?xml version="1.0"?>
<urlset>
  <url><loc>https://apply.careers.hsbc.com/emergingtalent/job/New-York-Investment-Banking-Graduate-NY-10001/1368939957/</loc></url>
  <url><loc>https://apply.careers.hsbc.com/emergingtalent/job/London-Corporate-and-Institutional-Banking-Graduate-Insight-Programme-E14-5HQ/1367637057/</loc></url>
  <url><loc>https://apply.careers.hsbc.com/emergingtalent/job/Singapore-Investment-Banking-HSBC-Infrastructure-Finance-Internship-018983/1365767157/</loc></url>
  <url><loc>https://apply.careers.hsbc.com/emergingtalent/job/Central-Investment-Banking-Graduate-Hong/1365764257/</loc></url>
</urlset>"""


def test_sitemap_splits_a_recognized_trailing_postal_code_into_location(monkeypatch):
    """Fixes the live defect on opportunity 17423: the slug's trailing
    postal code used to ride along inside the title unstripped ("New York
    Investment Banking Graduate NY 10001") while location stayed blank.
    Recognized US "<STATE> <ZIP>" and UK postcode shapes now move out of
    the title into location — copied verbatim from the same slug tokens,
    not inferred."""
    monkeypatch.setattr(sitemap_mod, "fetch_text",
                        lambda url, **kw: _SITEMAP_XML_WITH_POSTAL_SUFFIXES)
    result = fetch(SitemapBoard(firm="HSBC",
                                sitemap_url="https://apply.careers.hsbc.com/sitemap.xml",
                                path_filter="/emergingtalent/job/"))
    assert result.ok
    by_title = {o.title: o for o in result.opportunities}

    ny = by_title["New York Investment Banking Graduate"]
    assert ny.location == "NY 10001"

    london = by_title["London Corporate and Institutional Banking Graduate Insight Programme"]
    assert london.location == "E14 5HQ"


def test_sitemap_leaves_ambiguous_trailing_tokens_alone(monkeypatch):
    """Bare numeric codes with no letter component ("018983") and truncated
    slug fragments ("Hong", from "...-Hong-Kong" truncated) are NOT
    postal-code shaped — stripping either would be a guess, not a read, so
    both the title and the blank location stay exactly as the slug states
    them."""
    monkeypatch.setattr(sitemap_mod, "fetch_text",
                        lambda url, **kw: _SITEMAP_XML_WITH_POSTAL_SUFFIXES)
    result = fetch(SitemapBoard(firm="HSBC",
                                sitemap_url="https://apply.careers.hsbc.com/sitemap.xml",
                                path_filter="/emergingtalent/job/"))
    by_title = {o.title: o for o in result.opportunities}

    sg = by_title["Singapore Investment Banking HSBC Infrastructure Finance Internship 018983"]
    assert sg.location == ""

    hk = by_title["Central Investment Banking Graduate Hong"]
    assert hk.location == ""


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


def test_mckinsey_fetch_survives_a_null_docs_page(monkeypatch):
    """The gateway returns `docs` PRESENT with value `null` on a legitimate
    zero-hit page. `.get("docs", [])` only defaults on a MISSING key, so it
    handed `None` to the `for doc in batch` loop — which sits outside the
    per-page network try — and the uncaught TypeError killed the whole
    board's fetch. Must come back ok with zero rows instead."""
    monkeypatch.setattr(mck_mod, "fetch_json", lambda url, **kw: {"numFound": 0, "docs": None})
    result = fetch(McKinseyBoard(firm="McKinsey & Company", keywords=("intern",)))
    assert result.ok and result.error is None
    assert result.opportunities == [] and result.raw_count == 0


def test_mckinsey_verify_does_not_crash_on_a_null_docs_page(monkeypatch):
    """Same null-`docs` envelope through `verify`'s single-page read: no
    TypeError, and a null page is not a confirmed closure."""
    monkeypatch.setattr(mck_mod, "fetch_json", lambda url, **kw: {"numFound": 0, "docs": None})
    v = verify("https://www.mckinsey.com/careers/search-jobs/jobs/business-analyst-intern")
    assert v.result == "needs-verification"


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
    `[]` via `.get("docs") or []`, indistinguishable from a genuine zero
    matches. Must not close."""
    monkeypatch.setattr(mck_mod, "fetch_json", lambda url, **kw: {"unexpectedKey": []})
    v = verify("https://www.mckinsey.com/careers/search-jobs/jobs/business-analyst-intern")
    assert v.result == "needs-verification"


def test_mckinsey_verify_does_not_crash_when_docs_is_null(monkeypatch):
    """Reproduces the live-confirmed crash: McKinsey's gateway search API
    returns the JSON key "docs" PRESENT with value `null` (not absent, not
    `[]`) on a legitimate zero-hit single-keyword search. A bare
    `data.get("docs", [])` does NOT catch this — the dict.get default only
    fires when the key is missing, so `for doc in None` raised an uncaught
    TypeError that reverify.py's except-block then misclassified as
    'unreachable' instead of the correct 'needs-verification'. Must return
    needs-verification, not raise."""
    monkeypatch.setattr(mck_mod, "fetch_json", lambda url, **kw: {"numFound": 0, "docs": None})
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


# ------------------------------------------------- talnet: the card layout
#
# Oleeo renders the same board two ways and the choice is per tenant, not a
# platform migration. Jefferies serves a card grid — `<li class="col-md-6
# opp-container">` tiles, not a single `<tr>` on the page — so the table
# regex matched nothing, the loop body never ran, and `fetch()` returned a
# clean, successful, EMPTY board. Jefferies ingested zero rows for its whole
# history while serving 50+ live vacancies, and nothing downstream could
# tell that apart from a firm that posts nothing.

JEFFERIES_CARDS = TalnetBoard(
    firm="Jefferies", kind="jobs",
    board_url="https://jefferies.tal.net/vx/lang-en-GB/mobile-0/appcentre-ext/brand-4/candidate/jobboard/vacancy/2/adv/",
)


def _card_pages(monkeypatch, first, second=None):
    """Serve the card fixture, and the page-2 fixture for the `?start=` URL
    its own pagination nav points at."""
    def fake(url, **kw):
        if "start=" in url:
            if second is None:
                raise AssertionError(f"unexpected pagination fetch: {url}")
            return second
        return first
    monkeypatch.setattr(talnet_mod, "fetch_text", fake)


def test_talnet_card_layout_parses_a_board_with_no_table_at_all(monkeypatch):
    page1 = (FIXTURES / "talnet_cards_sample.html").read_text()
    page2 = (FIXTURES / "talnet_cards_sample_page2.html").read_text()
    assert "<tr" not in page1, "the point of this fixture is that there is no table"
    _card_pages(monkeypatch, page1, page2)

    result = fetch(JEFFERIES_CARDS)
    assert result.ok and result.error is None

    titles = [o.title for o in result.opportunities]
    assert titles == [
        "2027 Summer Analyst Program - Investment Banking - Hong Kong",
        # HTML-escaped ampersand and a real en-dash survive intact.
        "2027 Investment Banking Internship – Frankfurt, M&A (ALL INTAKES)",
        "2026 Investment Banking Off-Cycle Internship - Stockholm (Q3/Q4 Start)",
        # page 2, reached through the board's own next_links nav
        "2027 Quant Masters Summer Programme - London",
    ]
    hk = result.opportunities[0]
    assert hk.source == "talnet"
    assert hk.firm == "Jefferies"
    assert hk.url.endswith(
        "/candidate/so/pm/1/pl/2/opp/"
        "1814-2027-Summer-Analyst-Program-Investment-Banking-Hong-Kong/en-GB")
    # Same per-session xf-<hex> strip the table path does: without it every
    # fetch mints new URLs and closed-detection kills the previous set.
    assert "/xf-" not in hk.url
    assert all("/xf-" not in o.url for o in result.opportunities)
    # The full list was read, so absence from it IS evidence of absence.
    assert result.truncated is False


def test_talnet_card_layout_reads_fields_by_label_not_by_position(monkeypatch):
    """Label order is not guaranteed stable across Oleeo tenants, so the
    card parser keys `cols` off each tile's own field-label text. A tenant
    that labels City and Registration Deadline therefore fills `location`
    and `deadline` through the very same `_normalize` the table path uses —
    and the order the two fields appear in must not matter."""
    def card(oppid, title, fields):
        return (
            f'<li class="col-md-6 opp-container" id="oppid-{oppid}" data-oppid="{oppid}">'
            f'<div class="opp_{oppid} search_res details_row candidate-opp-tile" '
            f'data-oppid="{oppid}" data-title="{title}">'
            + "".join(
                f'<div class="candidate-opp-field-{i}">'
                f'<span class="candidate-opp-field-label">{label}:</span> {value}</div>'
                for i, (label, value) in enumerate(fields, start=1))
            + f'<h3 class="candidate-opp-field-{len(fields) + 1}">'
              f'<a class="subject" href="https://acme.tal.net/vx/candidate/so/pm/1/pl/2/'
              f'opp/{oppid}-{title.replace(" ", "-")}/en-GB">{title}</a></h3>'
            "</div></li>")

    page = ('<html><body><ul id="tile-results-list">'
            + card(11, "Deadline First", [("Registration Deadline", "28/07/2026"),
                                          ("City", "Hong Kong")])
            + card(12, "City First", [("City", "London"),
                                      ("Registration Deadline", "1/9/2026")])
            + "</ul></body></html>")
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: page)

    first, second = fetch(JEFFERIES_CARDS).opportunities
    assert (first.location, first.deadline) == ("Hong Kong", "2026-07-28")
    assert (second.location, second.deadline) == ("London", "2026-09-01")


def test_talnet_card_pagination_says_truncated_rather_than_looping(monkeypatch):
    """A card board pages at 50 tiles. If the nav keeps pointing back at a
    page already read, stop — but report the list as partial, because
    ingest reads "absent from the fetch" as "closed"."""
    page1 = (FIXTURES / "talnet_cards_sample.html").read_text()
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: page1)

    result = fetch(JEFFERIES_CARDS)
    assert result.ok
    assert len(result.opportunities) == 3
    assert result.truncated is True


# ---------------------------------------------- talnet: the zero-rows guard

def test_talnet_table_layout_is_untouched_by_card_support(monkeypatch):
    """Regression guard for the four tenants that parse today (BofA, Morgan
    Stanley, Nomura, Evercore). The table path must stay the first and
    unconditional branch, and the card parser must find nothing in a table
    board — otherwise the two layouts could double-count."""
    html = (FIXTURES / "talnet_jobs_sample.html").read_text()
    assert talnet_mod._parse_table(html), "table parser must still read a table board"
    assert talnet_mod._parse_cards(html) == [], "card parser must not claim table rows"

    calls = []

    def fake(url, **kw):
        calls.append(url)
        return html

    monkeypatch.setattr(talnet_mod, "fetch_text", fake)
    result = fetch(BOFA_JOBS)
    assert result.ok and len(result.opportunities) == 2
    assert result.opportunities[0].location == "Hong Kong"
    assert result.truncated is False
    # This fixture carries no next_links nav, so the walk ends on page one
    # and the board issues exactly the one request it always did. Table
    # boards that DO advertise a next page are walked — see
    # test_talnet_pagination.py, which is where that behaviour is pinned.
    assert calls == [BOFA_JOBS.board_url]


def test_talnet_genuinely_empty_board_still_reports_zero_cleanly(monkeypatch):
    """Jefferies' events board really is empty right now, and says so with
    `no_results_message` and zero vacancy markup. That must stay a clean
    ok=True/0-row result — the guard below is worthless if it cries wolf on
    a board that is honestly quiet."""
    html = (FIXTURES / "talnet_cards_empty_sample.html").read_text()
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: html)

    result = fetch(JEFFERIES_CARDS)
    assert result.ok is True
    assert result.opportunities == [] and result.raw_count == 0
    assert result.error is None
    assert result.truncated is False


def test_talnet_zero_rows_off_a_page_full_of_vacancies_is_an_error(monkeypatch):
    """The failure this whole change exists to surface: the page is plainly
    listing vacancies and the parser read none of them. ok=True/0 rows is
    the dangerous shape — it reads downstream as "this firm posts nothing"
    and lets closed-detection auto-close the firm's entire open set."""
    page = (FIXTURES / "talnet_cards_sample.html").read_text().replace(
        '<a class="subject"', '<a class="subject-link"')  # tomorrow's rename
    assert "opp-container" in page and "candidate-opp-field-label" in page
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: page)

    result = fetch(JEFFERIES_CARDS)
    assert result.ok is False
    assert result.opportunities == [] and result.raw_count == 0
    assert "not empty" in result.error


def test_talnet_zero_rows_guard_also_covers_a_broken_table_board(monkeypatch):
    """Same guard, other layout: the four table tenants get the identical
    protection, so an Oleeo change to the `<tr>` markup surfaces as a
    failure instead of as a quiet mass close."""
    page = (FIXTURES / "talnet_jobs_sample.html").read_text().replace(
        '<tr class="opp_', '<tr class="vacancy_')  # tomorrow's rename
    assert '<a class="subject"' in page, "the page still visibly lists vacancies"
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: page)

    result = fetch(BOFA_JOBS)
    assert result.ok is False
    assert "not empty" in result.error


# ---------------------------------------------------------------------------
# Oracle's details endpoint is shaped differently from its search endpoint,
# and `_fetch_details` was reading the search shape. Two independent defects,
# both silent, both fixed 2026-09-01:
#
#   1. The URL asked for `expand=requisitionList` — the SEARCH endpoint's
#      expand value. The details endpoint rejects it and the request FAILS,
#      and `_fetch_details` swallows its own exceptions, so every call
#      returned None and it read as "this firm publishes no deadlines".
#   2. Even on a successful response the parse looked for
#      `items[0].requisitionList[0]`, which the details payload does not have
#      — its fields sit directly on `items[0]`.
#
# Measured against the live boards afterwards: 25 of 25 open campus rows at
# J.P. Morgan and 6 of 16 at Lazard gained a real `ExternalPostedEndDate`,
# from zero. J.P. Morgan's board is 7,136 requisitions, and this connector
# was paying one extra HTTP request for each of them to get nothing back.
# ---------------------------------------------------------------------------

_ORACLE_DETAILS_FLAT = {
    "items": [{
        "Id": "210775238",
        "Title": "2027 Corporate Analyst Development Program",
        "ExternalPostedStartDate": "2026-08-27T18:30:48+00:00",
        "ExternalPostedEndDate": "2026-11-01T23:55:00+00:00",
    }]
}


def test_oracle_details_url_asks_for_expand_all_not_requisitionlist():
    """`expand=requisitionList` is the SEARCH endpoint's value. Sending it to
    the details endpoint fails the request outright, which this helper hides
    behind its own except — so the wrong expand is indistinguishable from a
    firm that states no deadlines."""
    assert "expand=all" in oracle_mod._DETAILS_URL
    assert "expand=requisitionList" not in oracle_mod._DETAILS_URL


def test_oracle_fetch_details_reads_the_flat_details_shape(monkeypatch):
    monkeypatch.setattr(oracle_mod, "fetch_json", lambda url, **kw: _ORACLE_DETAILS_FLAT)
    got = oracle_mod._fetch_details("jpmc.fa.oraclecloud.com", "CX_1001", "210775238")
    assert got is not None, "details payload has no requisitionList; reading one returns None"
    assert got["ExternalPostedEndDate"] == "2026-11-01T23:55:00+00:00"


def test_oracle_fetch_details_still_reads_the_nested_search_shape(monkeypatch):
    """Both shapes are accepted on purpose. This helper takes a bare
    host/site/id, so a caller pointing it at a `findReqs` response should keep
    working rather than silently going quiet the way the flat shape did."""
    nested = {"items": [{"requisitionList": [{"ExternalPostedEndDate": "2026-10-12T03:59:00+00:00"}]}]}
    monkeypatch.setattr(oracle_mod, "fetch_json", lambda url, **kw: nested)
    got = oracle_mod._fetch_details("h", "s", "1")
    assert got["ExternalPostedEndDate"] == "2026-10-12T03:59:00+00:00"


def test_oracle_fetch_details_returns_none_when_there_is_no_end_date(monkeypatch):
    """A requisition that genuinely states no end date must come back None,
    not an empty dict — callers treat a truthy return as "we learned
    something". Lazard's board is ~34% populated, so this is the common case."""
    monkeypatch.setattr(oracle_mod, "fetch_json",
                        lambda url, **kw: {"items": [{"Id": "6494", "Title": "x"}]})
    assert oracle_mod._fetch_details("h", "s", "6494") is None


def test_oracle_fetch_details_swallows_a_failed_request(monkeypatch):
    """Unchanged behaviour, pinned: a transient failure costs one row its
    deadline and never the whole board fetch."""
    def boom(url, **kw):
        raise RuntimeError("503")
    monkeypatch.setattr(oracle_mod, "fetch_json", boom)
    assert oracle_mod._fetch_details("h", "s", "1") is None
