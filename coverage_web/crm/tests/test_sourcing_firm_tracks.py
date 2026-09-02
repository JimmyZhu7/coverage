"""`crm.sourcing` reads the FIRM's tracks and recruiting style (2026-09-01).

THE DEFECT, measured on the founder's board: the panel picked archetypes
from `user.tracks` alone, so it proposed "investment banking analyst" at
Jane Street and "sales and trading analyst" at KKR and Google — seats those
firms do not have. 21 of the 33 `st`-tagged firms on the board are quant or
prop shops, and Jane Street's own FAQ answers "Can I schedule a phone call
or coffee?" with "unfortunately, no".

Pure unit tests over stand-in objects, like test_sourcing.py.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from crm import sourcing


class FakeUser:
    def __init__(self, tracks=None, school=""):
        self.tracks = tracks
        self.school = school


class FakeFirm:
    """A model-style firm: attributes, not keys."""

    def __init__(self, name, tracks=None, recruiting_style="campus"):
        self.name = name
        self.tracks = tracks or []
        self.recruiting_style = recruiting_style


def _keywords(url: str) -> str:
    return parse_qs(urlparse(url).query)["keywords"][0]


def _tracks(rows):
    return [r["key"].split("-")[0] for r in rows]


# ---------------------------------------------------------------------------
# 1. The firm's tracks pick the seats.
# ---------------------------------------------------------------------------
def test_a_pe_shop_gets_pe_seats_whatever_the_student_runs():
    """KKR is PE. The founder runs IB and S&T. Before: "investment banking
    analyst" and "sales and trading analyst" at KKR."""
    rows = sourcing.suggestions_for({"name": "KKR", "tracks": ["pe"]}, FakeUser(["ib", "st"]))
    assert _tracks(rows) == ["pe", "pe", "pe"]
    assert all("private equity" in r["query"] or "credit analyst" in r["query"] for r in rows)
    assert not any("investment banking" in r["query"] or "sales and trading" in r["query"]
                   for r in rows)


def test_a_corp_strat_firm_gets_the_generic_trio_not_banking_seats():
    """REWRITTEN for D-3 (2026-09-02), which retired `corp-strat` from the
    picker and deleted its archetype table. This test used to assert Google
    handed back three corporate-strategy seats.

    Google keeps its tag and its panel; what it must NOT do is inherit this
    student's banking seats, which is what a plain "no tracks this module
    knows -> fall back to the student's" rule would have produced. That is
    the exact defect the firm-tracks read fixed on 2026-09-01 ("sales and
    trading analyst" at KKR and Google). A firm that stated a track we have
    no seats for is a classified firm, not an unknown one, so it degrades
    to the generic trio."""
    rows = sourcing.suggestions_for({"name": "Google", "tracks": ["corp-strat"]},
                                    FakeUser(["ib", "st"]))
    assert _tracks(rows) == ["any", "any", "any"]
    assert not any("investment banking" in r["query"] or "sales and trading" in r["query"]
                   for r in rows)


def test_an_unclassified_firm_still_falls_back_to_the_students_tracks():
    """The other half of the same rule: a firm nobody has classified is
    unknown, not empty, and the student's own tracks stay the best guess
    for it. Pinned beside the case above so the two can never be collapsed
    into one branch again."""
    rows = sourcing.suggestions_for({"name": "Some Boutique"}, FakeUser(["ib"]))
    assert _tracks(rows) == ["ib", "ib", "ib"]


def test_a_pipeline_firm_gets_the_generic_trio_too():
    """MLT and SEO Career are access programmes, not employers: `pipeline`
    is a real, stated track with no seats in this module. Same branch as
    the retired-track case, and the reason that branch is written on
    "stated something we have no table for" rather than on the retired
    slug alone."""
    rows = sourcing.suggestions_for({"name": "MLT", "tracks": ["pipeline"]},
                                    FakeUser(["ib", "st"]))
    assert _tracks(rows) == ["any", "any", "any"]


def test_a_bank_on_one_of_the_students_tracks_uses_only_that_track():
    """HSBC is IB only. Before: an S&T row at a bank that runs no markets
    programme on the board."""
    rows = sourcing.suggestions_for({"name": "HSBC", "tracks": ["ib"]},
                                    FakeUser(["ib", "st"], school="USC"))
    assert [r["key"] for r in rows] == ["ib-0", "ib-1", "alumni"]


def test_a_bank_on_both_tracks_still_alternates():
    rows = sourcing.suggestions_for({"name": "Citi", "tracks": ["ib", "st"]},
                                    FakeUser(["ib", "st"]))
    assert [r["key"] for r in rows] == ["ib-0", "st-0", "ib-1"]


def test_shared_tracks_keep_the_students_order_not_the_firms():
    rows = sourcing.suggestions_for({"name": "Citi", "tracks": ["ib", "st"]},
                                    FakeUser(["st", "ib"]))
    assert [r["key"] for r in rows] == ["st-0", "ib-0", "st-1"]


def test_a_firm_with_no_tracks_falls_back_to_the_students():
    """The degrade rule: exactly what the panel did before it read the firm."""
    with_key = sourcing.suggestions_for({"name": "Lazard", "tracks": []}, FakeUser(["ib"]))
    without_key = sourcing.suggestions_for({"name": "Lazard"}, FakeUser(["ib"]))
    assert with_key == without_key
    assert [r["key"] for r in with_key] == ["ib-0", "ib-1", "ib-2"]


def test_a_student_with_no_tracks_gets_the_firms_own():
    rows = sourcing.suggestions_for({"name": "KKR", "tracks": ["pe"]}, FakeUser(None))
    assert _tracks(rows) == ["pe", "pe", "pe"]


def test_no_tracks_on_either_side_is_still_the_generic_trio():
    rows = sourcing.suggestions_for({"name": "Barclays"}, FakeUser(None))
    assert [r["key"] for r in rows] == ["any-0", "any-1", "any-2"]


def test_unknown_firm_tracks_are_data_not_a_crash():
    rows = sourcing.suggestions_for({"name": "X", "tracks": ["quant-research", "", "pe"]},
                                    FakeUser(["ib"]))
    assert _tracks(rows) == ["pe", "pe", "pe"]


def test_a_model_style_firm_is_read_the_same_way():
    rows = sourcing.suggestions_for(FakeFirm("KKR", ["pe"]), FakeUser(["ib"]))
    assert _tracks(rows) == ["pe", "pe", "pe"]


# ---------------------------------------------------------------------------
# 2. Assessment firms get the two honest rows.
# ---------------------------------------------------------------------------
def test_jane_street_gets_a_recruiter_and_a_referral_not_an_analyst_to_chat_with():
    jane = {"name": "Jane Street", "tracks": ["st"], "recruiting_style": "assessment"}
    rows = sourcing.suggestions_for(jane, FakeUser(["ib", "st"], school="USC"))
    assert [r["key"] for r in rows] == ["assess-0", "alumni"]
    assert rows[0]["label"] == "Campus recruiter (they run the process; a chat is not part of it)"
    assert rows[0]["why"] == (
        "Ask what the assessment covers and when it runs. That is the whole conversation."
    )
    assert _keywords(rows[0]["linkedin_url"]) == '"Jane Street" campus recruiting'
    assert rows[1]["label"] == "Alumnus at the firm, for a resume referral only"
    assert rows[1]["why"] == "A referral gets your resume read. It does not skip the test."
    assert rows[1]["query"] == '"Jane Street" "USC"'
    assert not any("analyst" in r["query"] for r in rows)
    assert not any("chat" in r["why"].lower() and "not" not in r["why"].lower() for r in rows)


def test_the_alumni_row_stays_without_a_school_as_a_plain_alumni_search():
    jane = {"name": "Jane Street", "tracks": ["st"], "recruiting_style": "assessment"}
    rows = sourcing.suggestions_for(jane, FakeUser(["st"]))
    assert [r["key"] for r in rows] == ["assess-0", "assess-1"]
    assert rows[1]["label"] == "Alumnus at the firm, for a resume referral only"
    assert _keywords(rows[1]["linkedin_url"]) == '"Jane Street" alumni'


def test_the_panel_note_says_apply_for_an_assessment_firm_and_the_disclosure_otherwise():
    jane = FakeFirm("Jane Street", ["st"], recruiting_style="assessment")
    assert sourcing.panel_note(jane) == sourcing.ASSESSMENT_NOTE
    assert "coffee chat" in sourcing.ASSESSMENT_NOTE and "Apply" in sourcing.ASSESSMENT_NOTE
    assert sourcing.panel_note(FakeFirm("Citi", ["ib"])) == sourcing.DISCLOSURE
    assert sourcing.panel_note({"name": "Citi"}) == sourcing.DISCLOSURE


def test_a_campus_firm_on_the_same_track_still_gets_the_desk_rows():
    """The style decides, not the track: Goldman's markets desk is `st` too
    and it does do coffee chats."""
    rows = sourcing.suggestions_for({"name": "Goldman Sachs", "tracks": ["st"]},
                                    FakeUser(["st"]))
    assert [r["key"] for r in rows] == ["st-0", "st-1", "st-2"]


def test_an_assessment_firms_rows_honour_the_limit():
    jane = {"name": "Jane Street", "recruiting_style": "assessment"}
    assert len(sourcing.suggestions_for(jane, FakeUser(["st"]), limit=1)) == 1
    assert sourcing.suggestions_for(jane, FakeUser(["st"]), limit=0) == []


@pytest.mark.parametrize("style", ["campus", "", None])
def test_anything_but_assessment_is_campus(style):
    firm = {"name": "Citi", "tracks": ["ib"], "recruiting_style": style}
    assert [r["key"] for r in sourcing.suggestions_for(firm, FakeUser(["ib"]))] == \
        ["ib-0", "ib-1", "ib-2"]


def test_assessment_rows_are_still_plain_linkedin_searches_about_this_firm():
    jane = {"name": "Jane Street", "recruiting_style": "assessment"}
    for r in sourcing.suggestions_for(jane, FakeUser(["st"], school="USC")):
        assert r["linkedin_url"].startswith(sourcing.LINKEDIN_PEOPLE_SEARCH + "?")
        assert '"Jane Street"' in _keywords(r["linkedin_url"])
