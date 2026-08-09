"""`_group_picks` — the presentation-layer regroup behind the "Picked for you" bar.

Three of the scorer's four axes (tier, track, region) read FIRM properties, so
two roles at the same firm cannot differ on them. Rendered one reason-set per
card that produced a row in which every card's "why" string was byte-identical,
which told the student nothing and buried the fields that *did* vary. The fix
is a display regroup, not a scoring change, so the properties worth pinning are
about faithfulness rather than about ranking quality:

  * nothing is lost — every reason the scorer produced still prints, once;
  * nothing is invented — a reason only rises to a broader level when its long
    form is byte-identical there, because "Tier 1" carries a different sentence
    for each firm and printing one firm's sentence over another's role would be
    a lie the rest of this codebase works hard to avoid;
  * nothing is reordered — the scorer keeps sole authority over ranking.
"""

from __future__ import annotations

from directory.recommend import Reason
from directory.views import _group_picks


def _pick(pk, firm, *reasons, title=None):
    """A `_pick_card`-shaped dict, trimmed to the keys the regroup reads."""
    return {
        "id": pk,
        "firm_name": firm.title(),
        "firm_slug": firm,
        "monogram": firm[:2].upper(),
        "title": title or f"Role {pk}",
        "reasons": list(reasons),
    }


TIER_A = Reason("Tier 1", "Alpha is a Tier 1 firm on your target list.", "tier")
TIER_B = Reason("Tier 1", "Beta is a Tier 1 firm on your target list.", "tier")
TRACK_A = Reason("matches IB", "Alpha covers IB, which you're recruiting for.", "track")
# Region and class details name the student, never the firm, so they are the
# reasons that can legitimately be identical across firms.
REGION = Reason("US", "United States — the market your university sits in.", "region")
INTAKE = Reason("2027 intake", "A 2027 programme usually targets 2028 graduates.", "class")


def test_empty_input_is_not_a_special_case_for_the_template():
    assert _group_picks([]) == ([], [])


def test_a_reason_on_every_pick_rises_to_the_bar():
    """The exact defect: the same chip on all six cards. It belongs in the head,
    stated once."""
    cards = [
        _pick(1, "alpha", TIER_A, REGION),
        _pick(2, "alpha", TIER_A, REGION),
        _pick(3, "beta", TIER_B, REGION),
    ]
    shared, blocks = _group_picks(cards)

    assert [r.text for r in shared] == ["US"]
    # ...and having risen, it is not also repeated underneath.
    assert all("US" not in [r.text for r in b["reasons"]] for b in blocks)
    assert all(
        "US" not in [r.text for r in role["reasons"]]
        for b in blocks for role in b["roles"]
    )


def test_a_firm_level_reason_stops_at_its_own_firm():
    """"Tier 1" reads the same on both firms but *says* something different on
    each. It may be stated once per firm and no higher."""
    cards = [
        _pick(1, "alpha", TIER_A, REGION),
        _pick(2, "alpha", TIER_A, REGION),
        _pick(3, "beta", TIER_B, REGION),
    ]
    shared, blocks = _group_picks(cards)

    assert [r.text for r in shared] == ["US"]
    assert [b["firm_slug"] for b in blocks] == ["alpha", "beta"]
    assert [r.detail for r in blocks[0]["reasons"]] == [TIER_A.detail]
    assert [r.detail for r in blocks[1]["reasons"]] == [TIER_B.detail]


def test_identical_chip_text_with_different_detail_never_merges():
    """The guard against the tempting shortcut of de-duplicating on chip text.
    Both firms show "Tier 1"; neither sentence is allowed to speak for the
    other, so the reason must NOT reach the bar."""
    cards = [_pick(1, "alpha", TIER_A), _pick(2, "beta", TIER_B)]
    shared, blocks = _group_picks(cards)

    assert shared == []
    assert [r.detail for r in blocks[0]["reasons"]] == [TIER_A.detail]
    assert [r.detail for r in blocks[1]["reasons"]] == [TIER_B.detail]


def test_a_reason_only_one_role_has_stays_on_that_role():
    """The distinguishing bits are the whole point of the exercise; they must
    survive at row level rather than being averaged away."""
    # Beta is present so that TIER_A cannot rise past the alpha block — this
    # test is about the row level, and a single-firm fixture would let every
    # reason float to the bar and prove nothing.
    cards = [
        _pick(1, "alpha", TIER_A, TRACK_A),
        _pick(2, "alpha", TIER_A),
        _pick(3, "beta", TIER_B),
    ]
    shared, blocks = _group_picks(cards)
    roles = blocks[0]["roles"]

    assert shared == []
    assert [r.text for r in blocks[0]["reasons"]] == ["Tier 1"]
    assert [r.text for r in roles[0]["reasons"]] == ["matches IB"]
    assert roles[1]["reasons"] == []


def test_every_reason_is_printed_exactly_once_somewhere():
    """The invariant that makes this safe to ship: the regroup moves reasons,
    it never drops or duplicates them."""
    cards = [
        _pick(1, "alpha", TIER_A, TRACK_A, REGION, INTAKE),
        _pick(2, "alpha", TIER_A, REGION, INTAKE),
        _pick(3, "beta", TIER_B, REGION, INTAKE),
    ]
    before = sorted(
        (r.kind, r.text, r.detail) for c in cards for r in c["reasons"]
    )
    shared, blocks = _group_picks(cards)

    printed = [(r.kind, r.text, r.detail) for r in shared]
    for b in blocks:
        printed += [(r.kind, r.text, r.detail) for r in b["reasons"]]
        for role in b["roles"]:
            printed += [(r.kind, r.text, r.detail) for r in role["reasons"]]

    assert len(printed) == len(set(printed)), "a reason was printed twice"
    assert set(printed) == set(before), "a reason was lost or invented"


def test_the_scorer_keeps_sole_authority_over_order():
    """Blocks follow the rank of their best role, and roles keep their rank
    inside a block. The regroup is not allowed to promote anything."""
    cards = [
        _pick(1, "beta", TIER_B),
        _pick(2, "alpha", TIER_A),
        _pick(3, "beta", TIER_B),
        _pick(4, "alpha", TIER_A),
    ]
    _, blocks = _group_picks(cards)

    assert [b["firm_slug"] for b in blocks] == ["beta", "alpha"]
    assert [r["id"] for r in blocks[0]["roles"]] == [1, 3]
    assert [r["id"] for r in blocks[1]["roles"]] == [2, 4]


def test_a_single_pick_still_produces_one_block():
    shared, blocks = _group_picks([_pick(1, "alpha", TIER_A, REGION)])

    # One row means every reason is trivially universal, so all of them rise —
    # and with one block there is nowhere for the bar and the block to disagree.
    assert [r.text for r in shared] == ["Tier 1", "US"]
    assert len(blocks) == 1 and blocks[0]["roles"][0]["reasons"] == []


# ---------------------------------------------------------------------------
# The cycle-not-open note (2026-08-09). A 2029 student targeting the 2028
# Summer Internship cycle sees a picked column full of 2027-summer roles,
# because cohort-2028 internships do not exist yet — that recruiting has not
# opened. Without the note, the column presents prior-cycle near-misses as
# if they were the thing the student asked for.
# ---------------------------------------------------------------------------


import pytest

from directory.models import Firm, Opportunity
from directory.views import _cycle_not_open_note


class _P:
    def __init__(self, cycle):
        self.target_cycle = cycle


@pytest.mark.django_db
def test_the_note_fires_only_when_the_board_lacks_the_cycle():
    f = Firm.objects.create(slug="gs", name="Goldman Sachs")
    Opportunity.objects.create(firm=f, url="https://x/1", status="open",
                               bucket="internship", cohort="2027",
                               title="2027 Summer Analyst")
    qs = Opportunity.objects.filter(status="open")

    note = _cycle_not_open_note(_P("2028 Summer Internship"), qs)
    assert "2028 Summer Internship" in note
    assert "haven't opened" in note
    # House copy: no em dash in product prose.
    assert "—" not in note

    # The moment ONE cohort-2028 internship exists, the note must vanish —
    # "not open yet" may only ever mean the BOARD lacks it.
    Opportunity.objects.create(firm=f, url="https://x/2", status="open",
                               bucket="internship", cohort="2028",
                               title="2028 Summer Analyst")
    assert _cycle_not_open_note(_P("2028 Summer Internship"), qs) == ""


@pytest.mark.django_db
def test_an_unparseable_cycle_yields_no_note():
    qs = Opportunity.objects.filter(status="open")
    assert _cycle_not_open_note(_P(""), qs) == ""
    assert _cycle_not_open_note(_P("whenever works"), qs) == ""
