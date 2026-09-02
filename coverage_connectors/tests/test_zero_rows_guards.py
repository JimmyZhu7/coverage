"""Zero rows means three different things, and every connector now says which.

Before this file existed, fifteen of eighteen connectors answered a 200 that
parsed to nothing with `ok=True, 0 rows` — the same answer they give for a
firm that genuinely has no openings. Downstream that is not a shrug: `ingest`
reads a clean zero as permission to close the firm's entire open set, and
`health` files it under "plausible market fact, worth a manual look now and
then". Jefferies sat at zero for a year that way, on a board serving 51 live
vacancies.

Each test below feeds a connector the real shape its platform produces when
something has gone wrong, taken from `research-ats-lifecycle.md`'s live
probing (2026-09-01), and asserts the connector reports it unreadable. Its
partner test feeds the shape the platform produces when it genuinely has
nothing and asserts that one still comes back `ok=True` with
`empty_state=True` — the guards must not turn a quiet board into a false
alarm, which is the failure mode that trains an operator to ignore the
report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coverage_connectors import (
    avature, eightfold, goldmansachs, greenhouse, icims, lever, lumesse,
    mckinsey, oracle, phenom, sitemap, socgen, successfactors, talentgateway,
    talentsoft, workday, zero_rows_guard,
)
from coverage_connectors.models import (
    AvatureBoard, EightfoldBoard, FetchResult, GoldmanSachsBoard,
    GreenhouseBoard, IcimsBoard, LeverBoard, LumesseBoard, McKinseyBoard,
    Opportunity, OracleBoard, PhenomBoard, SitemapBoard, SocGenBoard,
    SuccessFactorsBoard, TalentGatewayBoard, TalentsoftBoard, WorkdayBoard,
)

FIXTURES = Path(__file__).parent / "fixtures"

UNREADABLE = "board unreadable, not empty"


def fixture_json(name: str):
    return json.loads((FIXTURES / name).read_text())


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def assert_unreadable(result: FetchResult, *, contains: str = ""):
    assert not result.ok, f"expected unreadable, got ok=True with {result.raw_count} rows"
    assert result.opportunities == []
    assert UNREADABLE in (result.error or ""), result.error
    if contains:
        assert contains in (result.error or ""), result.error


# --------------------------------------------------------------- greenhouse

GH_BOARD = GreenhouseBoard(firm="Sixth Street", token="sixthstreet")


def test_greenhouse_vacated_token_is_empty_on_the_wire(monkeypatch):
    """The shape the research reproduced live on `optiver` (2026-09-01): a
    board token the firm has moved off answers 200 `{"jobs":[],"meta":
    {"total":0}}`, byte-identical to a board that is genuinely quiet. The
    connector cannot tell them apart and must not pretend to — it reports a
    clean, flagged zero and leaves the call to the layer that knows the
    board's history."""
    monkeypatch.setattr(greenhouse, "fetch_json",
                        lambda url, **kw: fixture_json("greenhouse_vacated_token.json"))
    r = greenhouse.fetch(GH_BOARD)
    assert r.ok and r.opportunities == []
    assert r.empty_state, "meta.total=0 is Greenhouse stating it, not us guessing"


def test_greenhouse_envelope_without_a_jobs_key_is_unreadable(monkeypatch):
    """`boards-api` always sends `jobs`. A body without it is a WAF page or a
    renamed API, and `data.get("jobs", [])` used to read every one of those as
    a firm with nothing open."""
    monkeypatch.setattr(greenhouse, "fetch_json",
                        lambda url, **kw: {"error": "Job board not found"})
    assert_unreadable(greenhouse.fetch(GH_BOARD), contains="no 'jobs' key")


def test_greenhouse_stated_total_against_zero_jobs_is_unreadable(monkeypatch):
    monkeypatch.setattr(greenhouse, "fetch_json",
                        lambda url, **kw: {"jobs": [], "meta": {"total": 20}})
    assert_unreadable(greenhouse.fetch(GH_BOARD), contains="meta.total=20")


def test_greenhouse_zero_from_a_board_that_held_rows_fails(monkeypatch):
    """THE SIXTH STREET CASE. 20 open rows, and the token stopped resolving
    two runs ago with nothing anywhere saying so. History is the only signal
    that separates this from a quiet board, and history lives with the
    caller, so the caller passes it in."""
    monkeypatch.setattr(greenhouse, "fetch_json", lambda url, **kw: {"jobs": []})
    clean = greenhouse.fetch(GH_BOARD)
    assert clean.ok and not clean.empty_state, "no meta.total: nothing was stated"

    guarded = zero_rows_guard(clean, banked_rows=20)
    assert_unreadable(guarded, contains="zero rows from a board that held 20")
    assert "token vacated or renamed?" in guarded.error


def test_the_history_guard_never_overrides_a_stated_empty_board(monkeypatch):
    """A board that SAID it is empty is not second-guessed, however many rows
    we hold. Closing them is then correct, and the alternative — an alarm that
    fires every run on a firm that has simply finished its season — is how a
    report stops being read."""
    monkeypatch.setattr(greenhouse, "fetch_json",
                        lambda url, **kw: fixture_json("greenhouse_vacated_token.json"))
    r = zero_rows_guard(greenhouse.fetch(GH_BOARD), banked_rows=200)
    assert r.ok and r.empty_state


def test_the_history_guard_leaves_a_board_with_rows_alone():
    board = GreenhouseBoard(firm="William Blair", token="williamblair")
    good = FetchResult(board=board, ok=True, raw_count=1,
                       opportunities=[Opportunity(firm="William Blair", title="SA",
                                                  location="Chicago", url="u",
                                                  source="greenhouse")])
    assert zero_rows_guard(good, banked_rows=48) is good


def test_the_history_guard_is_inert_without_history():
    """Every caller that does not pass row counts keeps exactly the behaviour
    it had — the guard cannot invent a failure out of a zero it knows nothing
    about."""
    board = GreenhouseBoard(firm="HPS", token="hps")
    empty = FetchResult(board=board, ok=True, opportunities=[], raw_count=0)
    assert zero_rows_guard(empty, banked_rows=0) is empty


# -------------------------------------------------------------------- lever

def test_lever_non_array_body_is_unreadable(monkeypatch):
    monkeypatch.setattr(lever, "fetch_json",
                        lambda url, **kw: {"ok": False, "error": "rate limited"})
    assert_unreadable(lever.fetch(LeverBoard(firm="Palantir", org="palantir")),
                      contains="not the documented array")


# ------------------------------------------------------------------ workday

WD_BOARD = WorkdayBoard(firm="Citi", tenant_host="citi.wd5",
                        site="Citi_Early_Careers_Events_Site")


def test_workday_missing_jobpostings_key_is_unreadable(monkeypatch):
    """Workday mostly fails loudly (400/404/422/500). The case left over is
    the CxS endpoint answering 200 with its SPA shell or an error envelope —
    JSON that parses and carries no `jobPostings` at all."""
    monkeypatch.setattr(workday, "_fetch_all",
                        lambda *a, **kw: {"errorCode": "HTTP_500"})
    assert_unreadable(workday.fetch(WD_BOARD), contains="no 'jobPostings' key")


def test_workday_stated_total_against_zero_postings_is_unreadable(monkeypatch):
    monkeypatch.setattr(workday, "_fetch_all",
                        lambda *a, **kw: {"total": 41, "jobPostings": []})
    assert_unreadable(workday.fetch(WD_BOARD), contains="total=41")


def test_workday_honest_empty_search_stays_ok(monkeypatch):
    """`{"total":0,"jobPostings":[]}` — the one 200-with-zero-rows case the
    platform research could produce deliberately, and a real answer."""
    monkeypatch.setattr(workday, "_fetch_all",
                        lambda *a, **kw: {"total": 0, "jobPostings": []})
    r = workday.fetch(WD_BOARD)
    assert r.ok and r.opportunities == [] and r.empty_state


# ------------------------------------------------------------------- oracle

OR_BOARD = OracleBoard(firm="J.P. Morgan", host="jpmc.fa.oraclecloud.com",
                       site_number="CX_1001", keywords=("internship",))


def test_oracle_missing_requisitionlist_is_unreadable(monkeypatch):
    """Drop `expand=` and Oracle answers 200 with an `items[0]` that has no
    `requisitionList` key at all (observed live 2026-09-01). Read as a length,
    that is zero jobs and no error; read as a KEY, it is a broken request."""
    monkeypatch.setattr(oracle, "fetch_json",
                        lambda url, **kw: fixture_json("oracle_no_requisitionlist.json"))
    assert_unreadable(oracle.fetch(OR_BOARD), contains="no 'requisitionList' key")


def test_oracle_genuine_zero_result_search_stays_ok(monkeypatch):
    """The key present, the list empty, the count zero: a real answer."""
    monkeypatch.setattr(oracle, "fetch_json", lambda url, **kw: {
        "items": [{"requisitionList": [], "TotalJobsCount": 0}]})
    r = oracle.fetch(OR_BOARD)
    assert r.ok and r.opportunities == [] and r.empty_state


# ------------------------------------------------------------------- phenom

PH_BOARD = PhenomBoard(firm="BCG", host="careers.bcg.com", keywords="intern")


def test_phenom_reports_failure_in_the_body_with_a_200(monkeypatch):
    """Phenom does not use status codes for this. A rejected payload, an
    unknown ddoKey or a widget the tenant turned off all arrive as
    `{"status": "failure"}` behind a 200."""
    monkeypatch.setattr(phenom, "post_json",
                        lambda url, payload, **kw: fixture_json("phenom_status_failure.json"))
    r = phenom.fetch(PH_BOARD)
    assert_unreadable(r, contains="status='failure'")
    assert "Invalid ddoKey" in r.error, "the platform's own message, not ours"


def test_phenom_missing_refinesearch_block_is_unreadable(monkeypatch):
    monkeypatch.setattr(phenom, "post_json",
                        lambda url, payload, **kw: {"status": "success", "eventName": "x"})
    assert_unreadable(phenom.fetch(PH_BOARD), contains="no 'refineSearch' block")


def test_phenom_zero_hits_stays_ok(monkeypatch):
    monkeypatch.setattr(phenom, "post_json", lambda url, payload, **kw: {
        "status": "success",
        "refineSearch": {"totalHits": 0, "data": {"jobs": []}},
    })
    r = phenom.fetch(PH_BOARD)
    assert r.ok and r.opportunities == [] and r.empty_state


# ----------------------------------------------------------------- avature

AV_BOARD = AvatureBoard(firm="Bain & Company",
                        feed_url="https://careers.bain.com/SearchJobs/feed/")


def test_avature_login_stub_is_unreadable(monkeypatch):
    """A tenant that puts its careers site behind SSO serves a small sign-in
    page at the feed URL. It parses to zero `<item>`s exactly like an empty
    feed does."""
    monkeypatch.setattr(avature, "fetch_text",
                        lambda url, **kw: fixture_text("avature_login_stub.html"))
    assert_unreadable(avature.fetch(AV_BOARD), contains="login stub")


def test_avature_202_empty_body_is_unreadable(monkeypatch):
    """urllib does not raise on a 2xx, so Avature's 202-with-no-body reached
    the parser as an empty string and came back as an empty board."""
    monkeypatch.setattr(avature, "fetch_text", lambda url, **kw: "")
    assert_unreadable(avature.fetch(AV_BOARD), contains="empty body")


def test_avature_real_feed_with_no_items_stays_ok(monkeypatch):
    monkeypatch.setattr(avature, "fetch_text", lambda url, **kw: (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<title>Search Jobs</title></channel></rss>"))
    r = avature.fetch(AV_BOARD)
    assert r.ok and r.opportunities == [] and r.empty_state


# -------------------------------------------------------------------- icims

IC_BOARD = IcimsBoard(firm="SIG", tenant="careers-sig")


def test_icims_job_cards_the_parser_cannot_read_are_unreadable(monkeypatch):
    """The Jefferies failure on another platform: a page listing real
    postings under markup the anchor regex no longer matches."""
    monkeypatch.setattr(icims, "fetch_text",
                        lambda url, **kw: fixture_text("icims_cards_unparsed.html"))
    assert_unreadable(icims.fetch(IC_BOARD), contains="card layout changed")


def test_icims_no_results_panel_stays_ok(monkeypatch):
    monkeypatch.setattr(icims, "fetch_text", lambda url, **kw: (
        '<div class="iCIMS_NoResults">No jobs were found matching your criteria.</div>'))
    r = icims.fetch(IC_BOARD)
    assert r.ok and r.opportunities == [] and r.empty_state


def test_icims_page_that_is_not_a_portal_at_all_is_unreadable(monkeypatch):
    monkeypatch.setattr(icims, "fetch_text",
                        lambda url, **kw: "<html><body>We'll be right back.</body></html>")
    assert_unreadable(icims.fetch(IC_BOARD), contains="neither job cards nor a no-results panel")


# ------------------------------------------------------------------ sitemap

SM_BOARD = SitemapBoard(firm="HSBC",
                        sitemap_url="https://apply.careers.hsbc.com/sitemap.xml",
                        path_filter="/emergingtalent/job/")


def test_sitemap_with_no_loc_entries_is_unreadable(monkeypatch):
    monkeypatch.setattr(sitemap, "fetch_text",
                        lambda url, **kw: "<html><body>403 Forbidden</body></html>")
    assert_unreadable(sitemap.fetch(SM_BOARD), contains="no <loc> entries")


def test_sitemap_that_lists_nothing_under_the_campus_path_stays_ok(monkeypatch):
    """A readable sitemap whose entries all sit outside this board's path is
    the site answering "nothing on the campus path today" — which is exactly
    what HSBC's board is for, and a real seasonal answer."""
    monkeypatch.setattr(sitemap, "fetch_text", lambda url, **kw: (
        "<urlset><url><loc>https://apply.careers.hsbc.com/experienced/job/"
        "London-Analyst/12345/</loc></url></urlset>"))
    r = sitemap.fetch(SM_BOARD)
    assert r.ok and r.opportunities == [] and r.empty_state


# ------------------------------------------------------------------- socgen

def test_socgen_proxy_without_a_result_block_is_unreadable(monkeypatch):
    monkeypatch.setattr(socgen, "_token", lambda: "tok")
    monkeypatch.setattr(socgen, "post_json",
                        lambda url, payload, **kw: {"Error": "invalid token"})
    assert_unreadable(socgen.fetch(SocGenBoard(firm="Société Générale")),
                      contains="no 'Result' block")


def test_socgen_stated_total_against_zero_docs_is_unreadable(monkeypatch):
    monkeypatch.setattr(socgen, "_token", lambda: "tok")
    monkeypatch.setattr(socgen, "post_json", lambda url, payload, **kw: {
        "TotalCount": 640, "Result": {"Docs": []}})
    assert_unreadable(socgen.fetch(SocGenBoard(firm="Société Générale")),
                      contains="TotalCount=640")


# ------------------------------------------------------------------ lumesse

LU_BOARD = LumesseBoard(firm="BOCI", host="au01-foc.lumessetalentlink.com",
                        tech_id="Q7WFK026203F3VBQBLOV7F624")


def test_lumesse_without_the_jobs_envelope_is_unreadable(monkeypatch):
    monkeypatch.setattr(lumesse, "fetch_json",
                        lambda url, **kw: {"message": "guest auth refused"})
    assert_unreadable(lumesse.fetch(LU_BOARD), contains="jobs/globals envelope")


def test_lumesse_stated_count_against_zero_jobs_is_unreadable(monkeypatch):
    monkeypatch.setattr(lumesse, "fetch_json", lambda url, **kw: {
        "globals": {"jobsCount": 12}, "jobs": []})
    assert_unreadable(lumesse.fetch(LU_BOARD), contains="jobsCount=12")


# ----------------------------------------------------------------- eightfold

EF_BOARD = EightfoldBoard(firm="Millennium", host="career.mlp.com", domain="mlp.com")


def test_eightfold_without_a_positions_key_is_unreadable(monkeypatch):
    monkeypatch.setattr(eightfold, "fetch_json",
                        lambda url, **kw: {"error": "domain not found"})
    assert_unreadable(eightfold.fetch(EF_BOARD), contains="no 'positions' key")


def test_eightfold_stated_count_against_zero_positions_is_unreadable(monkeypatch):
    monkeypatch.setattr(eightfold, "fetch_json",
                        lambda url, **kw: {"count": 214, "positions": []})
    assert_unreadable(eightfold.fetch(EF_BOARD), contains="count=214")


# ------------------------------------------------------------------ mckinsey

def test_mckinsey_without_a_docs_key_is_unreadable(monkeypatch):
    """`docs: null` is a documented zero-hit page and stays fine; `docs`
    ABSENT is a different envelope and must not read as the same thing."""
    monkeypatch.setattr(mckinsey, "_page", lambda kw, start: {"numFound": 0})
    assert_unreadable(mckinsey.fetch(McKinseyBoard(firm="McKinsey & Company")),
                      contains="no 'docs' key")


def test_mckinsey_null_docs_on_a_zero_hit_page_stays_ok(monkeypatch):
    monkeypatch.setattr(mckinsey, "_page", lambda kw, start: {"docs": None, "numFound": 0})
    r = mckinsey.fetch(McKinseyBoard(firm="McKinsey & Company"))
    assert r.ok and r.opportunities == [] and r.empty_state


# -------------------------------------------------------------- goldmansachs

def test_goldman_without_an_items_key_is_unreadable(monkeypatch):
    monkeypatch.setattr(goldmansachs, "_post",
                        lambda page: {"errors": [{"message": "Cannot query field"}]})
    assert_unreadable(goldmansachs.fetch(GoldmanSachsBoard()), contains="no 'items' key")


def test_goldman_stated_total_against_zero_items_is_unreadable(monkeypatch):
    monkeypatch.setattr(goldmansachs, "_post",
                        lambda page: {"items": [], "totalCount": 130})
    assert_unreadable(goldmansachs.fetch(GoldmanSachsBoard()), contains="totalCount=130")


# ------------------------------------------------------------- talentgateway

TG_BOARD = TalentGatewayBoard(firm="UBS", partner_id=25008, site_id=5131)


def test_talentgateway_page_without_the_payload_is_unreadable(monkeypatch):
    """The embedded `searchResults` input IS the board. Every missing-payload
    path used to `return []`, which is indistinguishable from a featured
    block with nothing in it."""
    monkeypatch.setattr(talentgateway, "fetch_text",
                        lambda url, **kw: "<html><body>Session expired</body></html>")
    assert_unreadable(talentgateway.fetch(TG_BOARD), contains='no id="searchResults"')


def test_talentgateway_unparseable_payload_is_unreadable(monkeypatch):
    monkeypatch.setattr(talentgateway, "fetch_text",
                        lambda url, **kw: '<input id="searchResults" value="{not json">')
    assert_unreadable(talentgateway.fetch(TG_BOARD), contains="not JSON")


def test_talentgateway_empty_hotjobs_block_stays_ok(monkeypatch):
    monkeypatch.setattr(
        talentgateway, "fetch_text",
        lambda url, **kw: '<input id="searchResults" value="{&quot;HotJobs&quot;: {}}">')
    r = talentgateway.fetch(TG_BOARD)
    assert r.ok and r.opportunities == [] and r.empty_state


# ---------------------------------------------------------------- talentsoft

TS_BOARD = TalentsoftBoard(firm="Crédit Agricole CIB",
                           origin="https://jobs.ca-cib.com",
                           list_url="https://jobs.ca-cib.com/job/list-of-all-jobs.aspx?all=1")


def test_talentsoft_zero_cards_with_no_message_is_unreadable(monkeypatch):
    monkeypatch.setattr(talentsoft, "fetch_text",
                        lambda url, **kw: "<html><body><div id='shell'></div></body></html>")
    assert_unreadable(talentsoft.fetch(TS_BOARD), contains="no empty-list message")


def test_talentsoft_empty_list_message_is_an_answer_not_a_failure(monkeypatch):
    """This connector used to raise on ANY zero, so a tenant that had
    genuinely emptied its list was reported as a broken parser — the same
    conflation as everywhere else in this file, pointing the other way."""
    monkeypatch.setattr(talentsoft, "fetch_text",
                        lambda url, **kw: "<div class='ts-no-result'>Aucune offre</div>")
    r = talentsoft.fetch(TS_BOARD)
    assert r.ok and r.opportunities == [] and r.empty_state


# ------------------------------------------------------------ successfactors

SF_BOARD = SuccessFactorsBoard(firm="EY", origin="https://careers.ey.com",
                               keywords=("internship",))


def test_successfactors_catch_all_200_is_unreadable(monkeypatch):
    """RMK answers 200 for everything — a wrong `q=`, a moved tenant, a
    maintenance page. Zero `data-row`s alone therefore says nothing; only the
    empty-state panel does."""
    monkeypatch.setattr(successfactors, "fetch_text",
                        lambda url, **kw: "<html><body>Site under maintenance</body></html>")
    assert_unreadable(successfactors.fetch(SF_BOARD), contains="no empty-state panel")


@pytest.mark.parametrize("panel", [
    '<div class="noSearchResults">No jobs found.</div>',
    "<p>No matching jobs were found.</p>",
    "<p>There are currently no open positions.</p>",
])
def test_successfactors_empty_state_phrasings_stay_ok(monkeypatch, panel):
    monkeypatch.setattr(successfactors, "fetch_text",
                        lambda url, **kw: f"<html><body>{panel}</body></html>")
    r = successfactors.fetch(SF_BOARD)
    assert r.ok and r.opportunities == [] and r.empty_state
