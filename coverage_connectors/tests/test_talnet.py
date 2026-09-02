"""tal.net: the board's own location column, under whichever label it uses.

The rest of the tal.net suite lives in `test_new_connectors.py` alongside the
other ported connectors. This file is separate because it pins one specific
defect and its measurement: WS-OPP-09 in the 2026-09-01 product plan, whose
acceptance criteria name this path.

The defect. `_normalize` read `cols["City"]`. Bank of America, Morgan Stanley
and Evercore label that column "City"; nomuracampus labels it "Location".
Every Nomura row therefore arrived with a blank `location`, and since
`ingest` derives `region` from `location` and nothing else, they arrived
with a blank region too and were charged `W_REGION_UNKNOWN` for the
product's own ignorance. Measured on the founder's live data 2026-09-01:
56 talnet rows carried a non-empty "Location" cell and an empty `location`
field, one of them the founder's number one pick.
"""

from __future__ import annotations

from pathlib import Path

from coverage_connectors import talnet as talnet_mod
from coverage_connectors.models import TalnetBoard

FIXTURES = Path(__file__).parent / "fixtures"

NOMURA_BOARD = TalnetBoard(
    firm="Nomura", kind="jobs",
    board_url="https://nomuracampus.tal.net/vx/mobile-0/candidate/jobboard/vacancy/1/adv/",
)


def test_nomura_location_column_becomes_the_row_location(monkeypatch):
    """The Discover Nomura row yields `location == "London"`.

    This is the row WS-OPP-09 names: the founder's number one pick, which
    carried "Location London" in its own board row while the product showed
    it with no region at all."""
    html = (FIXTURES / "talnet_nomura_location_sample.html").read_text()
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: html)

    result = talnet_mod.fetch(NOMURA_BOARD)

    assert result.ok
    by_title = {o.title: o for o in result.opportunities}
    discover = by_title["2027 - Discover Nomura Programme - Insight Programme"]
    assert discover.location == "London"
    assert by_title["2027 Investment Banking Graduate Program, Singapore"].location == "Singapore"


def test_a_board_with_no_location_column_still_yields_a_blank_location(monkeypatch):
    """P1: silence beats a guess.

    The events boards ship Title / Event Date / Registration Deadline and no
    location column of any kind. Nothing in this connector may invent one
    from the title or from prose, so `location` stays empty and
    `normalize_region` is left with nothing to work from — which is the
    honest answer."""
    html = (FIXTURES / "talnet_events_sample.html").read_text()
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: html)

    result = talnet_mod.fetch(NOMURA_BOARD)

    assert result.ok and result.opportunities
    assert all(o.location == "" for o in result.opportunities)


def test_the_city_label_still_wins_for_the_boards_that_ship_it(monkeypatch):
    """Bank of America, Morgan Stanley and Evercore are bit-for-bit unchanged.

    "City" is first in `_LOCATION_COL_LABELS` precisely so that adding
    "Location" cannot reorder anything for the tenants that were already
    working."""
    html = (FIXTURES / "talnet_jobs_sample.html").read_text()
    monkeypatch.setattr(talnet_mod, "fetch_text", lambda url, **kw: html)

    result = talnet_mod.fetch(
        TalnetBoard(firm="Bank of America", kind="jobs",
                    board_url="https://bankcampuscareers.tal.net/vx/mobile-0/brand-4/candidate/jobboard/vacancy/1/adv/")
    )

    assert result.ok
    assert {o.location for o in result.opportunities} == {"Hong Kong", "Tokyo"}
    assert talnet_mod._location({"City": "Hong Kong", "Location": "London"}) == "Hong Kong"


def test_an_empty_location_cell_reads_as_no_location_not_as_whitespace():
    """A board that renders the column but leaves the cell blank says
    nothing, and must not be stored as a location made of spaces — Morgan
    Stanley ships exactly this on 7 rows today."""
    assert talnet_mod._location({"City": "   "}) == ""
    assert talnet_mod._location({"City": "", "Location": "Paris"}) == "Paris"
    assert talnet_mod._location({}) == ""
