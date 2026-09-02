"""SuccessFactors RMK connector — the server-rendered /search/ results table."""

from __future__ import annotations

import urllib.error

import pytest

from coverage_connectors import successfactors
from coverage_connectors.models import SuccessFactorsBoard

BOARD = SuccessFactorsBoard(firm="EY", origin="https://careers.ey.com",
                            keywords=("internship",))

# Shape captured live from careers.ey.com 2026-08-19. RMK renders the SAME
# anchor twice per row (a desktop copy and a phone copy) with the href and
# class attributes in opposite orders, and puts a "+1 more…" <small> chip
# inside the location cell for multi-location reqs — both reproduced here.
_ROW_DESKTOP_FIRST = """
<tr class="data-row">
  <td class="colTitle" headers="hdrTitle">
    <span class="jobTitle hidden-phone">
      <a href="/ey/job/Katowice-Junior-AI-Developer/1416841433/" class="jobTitle-link">EY GDS Poland NextGen Talent Internship &amp; Junior AI Developer</a>
    </span>
    <div class="jobdetail-phone visible-phone">
      <span class="jobTitle visible-phone">
        <a class="jobTitle-link" href="/ey/job/Katowice-Junior-AI-Developer/1416841433/">EY GDS Poland NextGen Talent Internship &amp; Junior AI Developer</a>
      </span>
    </div>
  </td>
  <td class="colLocation hidden-phone" headers="hdrLocation">
    <span class="jobLocation">
        Katowice, &#346;l&#261;skie, PL, 40-202
    </span>
  </td>
</tr>
<tr class="data-row">
  <td class="colTitle"><span class="jobTitle hidden-phone">
    <a href="/ey/job/Lisbon-Audit-Internship/1419926533/" class="jobTitle-link">Audit Internship - From January 2027</a>
  </span></td>
  <td class="colLocation"><span class="jobLocation">
      Lisbon, PT, 1349-066 <small class="nobr">+1 more&hellip;</small>
  </span></td>
</tr>
"""


def _page(rows: str, label: str | None = "Results 1 &ndash; 25 of 88") -> str:
    tail = f'<span class="paginationLabel">{label}</span>' if label else ""
    return f"<html><body>{rows}{tail}</body></html>"


def _rows(n: int, start: int = 0) -> str:
    return "".join(
        f'<tr class="data-row"><td><span class="jobTitle hidden-phone">'
        f'<a href="/ey/job/Role-{i}/{1000 + i}/" class="jobTitle-link">Role {i}</a>'
        f'</span></td><td><span class="jobLocation">London, GB</span></td></tr>'
        for i in range(start, start + n)
    )


def test_parses_title_location_and_absolute_url(monkeypatch):
    monkeypatch.setattr(successfactors, "fetch_text",
                        lambda url, **kw: _page(_ROW_DESKTOP_FIRST, label=None))
    r = successfactors.fetch(BOARD)

    assert r.ok and r.raw_count == 2
    first, second = r.opportunities
    # Entities decoded, the duplicated phone-copy anchor not double-counted,
    # and the relative href resolved against the board's own origin.
    assert first.title == "EY GDS Poland NextGen Talent Internship & Junior AI Developer"
    assert first.url == "https://careers.ey.com/ey/job/Katowice-Junior-AI-Developer/1416841433/"
    assert first.location == "Katowice, Śląskie, PL, 40-202"
    assert first.source == "successfactors"
    # The "+1 more…" chip is pagination chrome, not part of the place.
    assert second.location == "Lisbon, PT, 1349-066"


def test_anchor_attribute_order_does_not_matter(monkeypatch):
    """RMK's phone copy writes class before href; the desktop copy writes
    href before class. A row that carries ONLY the phone shape must still
    parse — a positional regex would silently drop it."""
    phone_only = ('<tr class="data-row"><td>'
                  '<a class="jobTitle-link" href="/ey/job/Phone-Only/99/">Phone Only</a>'
                  "</td></tr>")
    monkeypatch.setattr(successfactors, "fetch_text", lambda url, **kw: _page(phone_only, label=None))
    r = successfactors.fetch(BOARD)
    assert [o.title for o in r.opportunities] == ["Phone Only"]


def test_walks_startrow_until_the_stated_total_is_reached(monkeypatch):
    calls: list[str] = []

    def fake(url, **kw):
        calls.append(url)
        start = int(url.split("startrow=")[1])
        # 30 results: a full page, then a short one.
        return _page(_rows(25 if start == 0 else 5, start),
                     label="Results 1 &ndash; 25 of 30")

    monkeypatch.setattr(successfactors, "fetch_text", fake)
    r = successfactors.fetch(BOARD)

    assert r.ok and r.raw_count == 30 and not r.truncated
    assert [c.split("startrow=")[1] for c in calls] == ["0", "25"]
    # The keyword is what goes into q=, not a hardcoded filter.
    assert "q=internship" in calls[0]


def test_one_walk_per_keyword_deduped_by_url(monkeypatch):
    board = SuccessFactorsBoard(firm="EY", origin="https://careers.ey.com",
                                keywords=("internship", "trainee"))
    seen_keywords: list[str] = []

    def fake(url, **kw):
        seen_keywords.append(url.split("q=")[1].split("&")[0])
        # Both keywords return the SAME two rows — a real overlap, since a
        # posting titled "Trainee Internship" matches both searches.
        return _page(_rows(2), label="Results 1 &ndash; 2 of 2")

    monkeypatch.setattr(successfactors, "fetch_text", fake)
    r = successfactors.fetch(board)

    assert seen_keywords == ["internship", "trainee"]
    assert r.raw_count == 2, "the same posting found by two keywords is one row"


def test_stated_total_beyond_the_cap_reports_truncated(monkeypatch):
    """`truncated` is load-bearing: ingest infers 'closed' from 'absent from
    the fetch', which is only valid when the fetch was the whole list."""
    monkeypatch.setattr(successfactors, "_MAX_JOBS_PER_KEYWORD", 50)
    monkeypatch.setattr(
        successfactors, "fetch_text",
        lambda url, **kw: _page(_rows(25, int(url.split("startrow=")[1])),
                                label="Results 1 &ndash; 25 of 4000"))
    r = successfactors.fetch(BOARD)

    assert r.ok and r.truncated
    assert r.raw_count == 50


def test_an_empty_board_is_ok_and_empty_not_an_error(monkeypatch):
    """REWRITTEN 2026-09-01. This used to pin "no rows on the page -> ok=True,
    0 rows" for ANY 200, which is the silent-empty failure itself: RMK is a
    catch-all 200 (a wrong q=, a moved tenant, a maintenance page and a real
    zero-result search all look identical bar the empty-state panel), and a
    clean zero is what lets ingest close the firm's whole open set. The
    behaviour it now pins is that a page SAYING it found nothing is still
    ok=True and 0 rows — and additionally flags `empty_state`, so the caller's
    own history guard knows not to second-guess it. The
    no-panel-and-no-rows case has its own test below."""
    monkeypatch.setattr(
        successfactors, "fetch_text",
        lambda url, **kw: _page('<div class="noSearchResults">No jobs found.</div>',
                                label=None))
    r = successfactors.fetch(BOARD)
    assert r.ok and r.raw_count == 0 and r.opportunities == [] and not r.truncated
    assert r.empty_state, "the page said so in its own words"


def test_zero_rows_with_no_empty_state_panel_is_unreadable(monkeypatch):
    """An empty result set renders no paginationLabel AND no data-row, so
    without the empty-state panel there is nothing on the page to tell a
    genuinely quiet board from one whose markup moved. Reported as unreadable
    rather than empty — the direction that costs a health line instead of a
    firm's whole open set."""
    monkeypatch.setattr(successfactors, "fetch_text", lambda url, **kw: _page("", label=None))
    r = successfactors.fetch(BOARD)
    assert not r.ok and r.opportunities == []
    assert "board unreadable, not empty" in (r.error or "")
    assert "no empty-state panel" in (r.error or "")


def test_fetch_failure_is_reported_not_raised(monkeypatch):
    def boom(url, **kw):
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(successfactors, "fetch_text", boom)
    r = successfactors.fetch(BOARD)
    assert not r.ok and r.opportunities == [] and "503" in (r.error or "")


def test_bot_challenge_is_reported_as_unreadable_not_empty(monkeypatch):
    monkeypatch.setattr(successfactors, "bot_challenge_reason", lambda html: "quick check needed")
    monkeypatch.setattr(successfactors, "fetch_text", lambda url, **kw: "<html>challenge</html>")
    r = successfactors.fetch(BOARD)
    assert not r.ok and "unreadable, not empty" in (r.error or "")


@pytest.mark.parametrize("url", [
    "https://careers.ey.com/ey/job/Katowice-Junior-AI-Developer/1416841433/",
    "https://careers.ey.com/ey/job/Some-Role/12345",
])
def test_classify_url_claims_rmk_posting_urls(url):
    assert successfactors.classify_url(url) is not None


@pytest.mark.parametrize("url", [
    # Workday: same /job/ segment, but the last path segment is never numeric.
    "https://citi.wd5.myworkdayjobs.com/Citi_Careers/job/London/Analyst_26979549",
    # iCIMS / Avature use /jobs/ (plural).
    "https://careers-sig.icims.com/jobs/10966/accounting-internship/job",
    "https://apply.deloitte.com/careers/JobDetail/Manager-Transfer-Pricing/360763",
    "",
])
def test_classify_url_does_not_claim_other_providers(url):
    assert successfactors.classify_url(url) is None


def test_verify_reads_the_job_title_property(monkeypatch):
    page = ('<h1><span itemprop="title" data-careersite-propertyid="title" '
            'class="x">Audit Internship &amp; Trainee</span></h1>')
    monkeypatch.setattr(successfactors, "fetch_text", lambda url, **kw: page)
    v = successfactors.verify("https://careers.ey.com/ey/job/Audit/123/")
    assert v.result == "verified-open" and "Audit Internship & Trainee" in v.evidence


def test_verify_reports_closed_when_rmk_serves_the_search_page(monkeypatch):
    """A removed requisition answers 200 with the search page, not a 404 —
    confirmed live 2026-08-19. Without this branch every gone posting would
    read as unrecognized forever."""
    monkeypatch.setattr(successfactors, "fetch_text",
                        lambda url, **kw: '<section id="search-results"></section>')
    v = successfactors.verify("https://careers.ey.com/ey/job/Gone/999999999999/")
    assert v.result == "closed"


def test_verify_will_not_close_a_row_on_an_unrecognized_200(monkeypatch):
    # A WAF page, a maintenance page or a template change all look like this.
    # Closing on it would delete a live posting from the feed for the wrong
    # reason — the same trap workday.py's verify() documents.
    monkeypatch.setattr(successfactors, "fetch_text", lambda url, **kw: "<html>maintenance</html>")
    v = successfactors.verify("https://careers.ey.com/ey/job/Whatever/123/")
    assert v.result == "needs-verification"


def test_verify_declines_urls_it_does_not_own():
    v = successfactors.verify("https://boards-api.greenhouse.io/v1/boards/tpg/jobs/123")
    assert v.result == "needs-verification"
