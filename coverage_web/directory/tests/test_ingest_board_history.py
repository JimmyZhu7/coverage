"""The two things ingest now hands the connector layer, and the one it records.

`coverage_connectors` keeps no state on purpose — every call is a fresh fetch,
nothing remembers what a board returned last night. That is the right shape
for a library, and it is also why Greenhouse's vacated token was undetectable:
`200 {"jobs":[]}` from a board a firm has moved off is byte-identical to the
same response from a board that is genuinely quiet, and only row history
separates them.

So ingest passes the history down (`_banked_open_rows`) and writes the result
back up, one line per board (`stats["boards"]`). Both directions are pinned
here, because both were silent failures: a guard that never receives a count
cannot fire, and a per-board line nobody records cannot be reported.
"""

from __future__ import annotations

import pytest

from coverage_connectors import FetchResult, GreenhouseBoard, TalnetBoard
from coverage_connectors.models import Opportunity as ConnOpp

from directory import ingest
from directory.boards import board_key
from directory.models import Firm, Opportunity

pytestmark = pytest.mark.django_db

BOARD = GreenhouseBoard(firm="Sixth Street", token="sixthstreet")
U1 = "https://boards.greenhouse.io/sixthstreet/jobs/1"


def _opp(url, firm="Sixth Street"):
    return ConnOpp(firm=firm, title="2027 Summer Analyst", location="New York",
                   url=url, source="greenhouse")


def _result(board, opps, *, ok=True, error=None, empty_state=False):
    return FetchResult(board=board, ok=ok, opportunities=list(opps),
                       raw_count=len(list(opps)), error=error,
                       empty_state=empty_state)


def test_the_open_row_count_reaches_the_fetch_layer(monkeypatch):
    """`fetch_many` is where the guard lives, and it can only fire on a
    number the caller gives it."""
    firm = Firm.objects.create(slug="sixthstreet", name="Sixth Street")
    for i in range(20):
        Opportunity.objects.create(firm=firm, url=f"{U1}{i}", title="SA",
                                   bucket="internship", status="open",
                                   source="greenhouse")
    Opportunity.objects.create(firm=firm, url=f"{U1}closed", title="SA",
                               bucket="internship", status="closed",
                               source="greenhouse")
    seen = {}

    def fake_fetch_many(boards, **kwargs):
        seen.update(kwargs.get("banked_rows") or {})
        return [_result(BOARD, [_opp(U1)])]

    monkeypatch.setattr(ingest, "fetch_many", fake_fetch_many)
    ingest.ingest_boards([BOARD], label="greenhouse")

    # OPEN rows only. A board whose season has legitimately closed out has no
    # live rows to protect, and failing its fetch every night forever would
    # be an alarm nobody reads.
    assert seen[("Sixth Street", "greenhouse")] == 20


def test_a_board_whose_firm_is_not_seeded_yet_contributes_no_history(monkeypatch):
    """An unknown firm must leave the guard inert rather than passing a zero
    that means something else."""
    counts = ingest._banked_open_rows([BOARD])
    assert counts == {}


def test_row_history_is_counted_per_provider_not_per_firm():
    """A firm with boards on two providers must not lend one board's rows to
    the other: closed-detection is per (firm, provider) and so is this."""
    firm = Firm.objects.create(slug="moelis", name="Moelis")
    Opportunity.objects.create(firm=firm, url="https://moelis.wd/1", title="VP",
                               bucket="other", status="open", source="workday")
    talnet = TalnetBoard(
        firm="Moelis",
        board_url="https://moelis-careers.tal.net/vx/mobile-0/candidate/jobboard/vacancy/2/adv/")

    counts = ingest._banked_open_rows([talnet])
    assert counts == {("Moelis", "talnet"): 0}


def test_a_pair_with_two_boards_lends_neither_of_them_a_count():
    """Opportunity rows record the PROVIDER, not which of a firm's boards on
    it produced them. Solomon Partners runs a Greenhouse "studentsgraduates"
    board and a "professionals" one; lending the students' rows to the
    professionals board would fail it every night for being seasonally empty.
    Unattributable means unused."""
    firm = Firm.objects.create(slug="solomonpartners", name="Solomon Partners")
    Opportunity.objects.create(firm=firm, url="https://gh/sp/1", title="SA",
                               bucket="internship", status="open",
                               source="greenhouse")
    students = GreenhouseBoard(firm="Solomon Partners",
                               token="solomonpartnersstudentsgraduates")
    professionals = GreenhouseBoard(firm="Solomon Partners",
                                    token="solomonpartnersprofessionals")

    assert ingest._banked_open_rows([students, professionals]) == {}
    # Alone, the same firm's board does get its history — the ambiguity is
    # the pair's, not the firm's.
    assert ingest._banked_open_rows([students]) == {
        ("Solomon Partners", "greenhouse"): 1}


def test_every_board_gets_its_own_line_in_the_run(monkeypatch):
    """THE MOELIS CASE, from the recording side. Two boards, one firm, one
    provider: the run has to carry both or `health.board_health()` has
    nothing to read."""
    Firm.objects.create(slug="moelis", name="Moelis")
    jobs = TalnetBoard(
        firm="Moelis",
        board_url="https://moelis-careers.tal.net/vx/mobile-0/candidate/jobboard/vacancy/1/adv/")
    events = TalnetBoard(
        firm="Moelis",
        board_url="https://moelis-careers.tal.net/vx/mobile-0/candidate/jobboard/vacancy/2/adv/")
    monkeypatch.setattr(ingest, "fetch_many", lambda boards, **kw: [
        _result(jobs, [ConnOpp(firm="Moelis", title="2027 London Summer Analyst",
                               location="London", url="https://moelis-careers.tal.net/x/1",
                               source="talnet")]),
        _result(events, [], empty_state=True),
    ])
    run = ingest.ingest_boards([jobs, events], label="talnet")

    lines = {b["board"]: b for b in run.stats["boards"]}
    assert set(lines) == {board_key(jobs), board_key(events)}
    assert lines[board_key(jobs)]["rows"] == 1 and lines[board_key(jobs)]["ok"]
    assert lines[board_key(events)]["rows"] == 0
    assert lines[board_key(events)]["empty_state"] is True
    assert lines[board_key(jobs)]["slug"] == "moelis"


def test_a_failed_board_keeps_its_error_on_its_own_line(monkeypatch):
    """`stats["errors"]` keys on (firm, provider) and always will — it is what
    closed-detection reads. The per-board line is what says WHICH board."""
    Firm.objects.create(slug="sixthstreet", name="Sixth Street")
    monkeypatch.setattr(ingest, "fetch_many", lambda boards, **kw: [
        _result(BOARD, [], ok=False, error="HTTP Error 404: Not Found")])
    run = ingest.ingest_boards([BOARD], label="greenhouse")

    line = run.stats["boards"][0]
    assert line["ok"] is False
    assert line["board"] == "sixthstreet"
    assert "404" in line["error"]
    assert run.status == "error"


def test_a_failed_board_still_closes_nothing(monkeypatch):
    """The contract this whole change protects, restated as a test: an
    unreadable fetch must never be read as an empty board."""
    firm = Firm.objects.create(slug="sixthstreet", name="Sixth Street")
    Opportunity.objects.create(firm=firm, url=U1, title="SA", bucket="internship",
                               status="open", source="greenhouse")
    monkeypatch.setattr(ingest, "fetch_many", lambda boards, **kw: [
        _result(BOARD, [], ok=False,
                error="zero rows from a board that held 20 — token vacated or "
                      "renamed? — board unreadable, not empty")])
    ingest.ingest_boards([BOARD], label="greenhouse")

    assert Opportunity.objects.get(url=U1).status == "open"
