"""Talentsoft list-page connector — card parsing and the loud-empty rule."""
from __future__ import annotations

from coverage_connectors import talentsoft
from coverage_connectors.models import TalentsoftBoard

BOARD = TalentsoftBoard(firm="Crédit Agricole CIB",
                        origin="https://jobs.ca-cib.com",
                        list_url="https://jobs.ca-cib.com/job/list-of-all-jobs.aspx?LCID=2057&all=1")

_CARD = '''
<div class="ts-offer-card Layer">
  <h3 class="ts-offer-card__title">
    <a class="ts-offer-card__title-link " href="/job/job-12-month-internship-global-markets_114000.aspx"
       title="2026-114000"> 12-month Internship &amp; Trainee — Global Markets </a>
  </h3>
  <div class="ts-offer-card-content offerContent">
    <ul class="ts-offer-card-content__list ">
      <li>Internship / Trainee</li><li>United Kingdom</li><li class="noBorder">London</li>
    </ul>
  </div>
</div>'''


def test_parses_a_card_with_entities_city_and_country(monkeypatch):
    monkeypatch.setattr(talentsoft, "fetch_text", lambda url, **kw: _CARD)
    r = talentsoft.fetch(BOARD)
    assert r.ok and len(r.opportunities) == 1
    o = r.opportunities[0]
    assert o.title == "12-month Internship & Trainee — Global Markets"
    assert o.location == "London, United Kingdom"
    assert o.url == "https://jobs.ca-cib.com/job/job-12-month-internship-global-markets_114000.aspx"
    assert o.raw["contract_type"] == "Internship / Trainee"


def test_zero_cards_is_a_failure_not_an_empty_market(monkeypatch):
    """CA-CIB always has open roles; a page that parses to nothing means the
    markup changed, and that must land in the failing-board report rather
    than closing a hundred live rows."""
    monkeypatch.setattr(talentsoft, "fetch_text", lambda url, **kw: "<html>redesigned</html>")
    r = talentsoft.fetch(BOARD)
    assert r.ok is False and "markup" in (r.error or "")


def test_duplicate_hrefs_collapse(monkeypatch):
    monkeypatch.setattr(talentsoft, "fetch_text", lambda url, **kw: _CARD + _CARD)
    r = talentsoft.fetch(BOARD)
    assert len(r.opportunities) == 1
