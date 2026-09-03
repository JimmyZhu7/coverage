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
    def __init__(self, *cycles):
        self.target_cycles = [c for c in cycles if c]


@pytest.mark.django_db
def test_the_note_fires_only_when_the_board_lacks_the_cycle():
    f = Firm.objects.create(slug="gs", name="Goldman Sachs")
    Opportunity.objects.create(firm=f, url="https://x/1", status="open",
                               bucket="internship", cohort="2027",
                               title="2027 Summer Analyst")
    qs = Opportunity.objects.filter(status="open")

    note = _cycle_not_open_note(_P("2028 Summer Internship"), qs)
    assert "2028 Summer Internship" in note
    # "not open ... yet", not "haven't opened ... yet": the sentence was
    # shortened on 2026-09-02 (it wrapped to two italic lines inside a 320px
    # column header). Both claims it carries are still asserted here — the
    # cycle is not open, and what the column holds instead.
    assert "not open" in note and "yet" in note
    assert "Closest fits" in note
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


@pytest.mark.django_db
def test_the_note_names_every_closed_cycle_when_none_are_open():
    """A student recruiting for two programmes at once must hear about BOTH
    if neither has opened — not just whichever one happened to be checked
    first."""
    qs = Opportunity.objects.filter(status="open")
    note = _cycle_not_open_note(_P("2028 Summer Internship", "2027 Spring Week / Insight"), qs)
    assert "2028 Summer Internship" in note
    assert "2027 Spring Week / Insight" in note
    assert "and" in note


@pytest.mark.django_db
def test_the_note_stays_silent_once_any_one_named_cycle_is_open():
    """The moment ONE of the student's several target cycles has live
    postings, there is nothing to warn about — even if the other one is
    still closed."""
    f = Firm.objects.create(slug="gs", name="Goldman Sachs")
    Opportunity.objects.create(firm=f, url="https://x/3", status="open",
                               bucket="internship", cohort="2028",
                               title="2028 Summer Analyst")
    qs = Opportunity.objects.filter(status="open")
    note = _cycle_not_open_note(_P("2028 Summer Internship", "2027 Spring Week / Insight"), qs)
    assert note == ""


# ---------------------------------------------------------------------------
# THE ROW'S OWN "WHY" LINE, BUDGETED (2026-09-02).
#
# `.rr-why` is one line that never wraps and ellipsises at the row's edge, so
# whatever runs past the edge is cut by the browser MID-WORD. On the founder's
# board it read "Tier 2 · matches IB · US · For 2028-2029 grads (yours) · You
# know so…", which is not a shorter fact, it is a rendering fault, and it was
# the fourth line of a four-line row. `_pick_why_line` makes the cut on whole
# reasons instead, and counts what did not fit.
# ---------------------------------------------------------------------------

def test_the_why_line_cuts_on_whole_reasons_and_counts_the_rest():
    """AMENDED 2026-09-02: the count is an ITEM on the line now, not a suffix
    glued to the last reason.

    The old assertion split the line on `" +"` to find where the reasons
    ended, which only worked because the count joined with a bare space while
    every reason joined with `" · "`. That asymmetry was the defect: on the
    founder's board four of six Picked rows read "Tier 1 · HK · 2027 intake, a
    year early for you +2", and the "+2" read as the tail of the sentence it
    was stuck to rather than as a count of what follows it. The count takes
    the line's own separator now, so this splits on the separator instead.

    The budget slack moves from 4 to 5 for the same reason: the appended
    suffix grew from `" +N"` to `" · +N"`. It is still appended AFTER the
    budget rather than charged to it, which is what `_PICK_WHY_CHARS`'s own
    note says the 50-against-53 gap is reserved for.
    """
    from directory.views import _PICK_WHY_CHARS, _PICK_WHY_SEP, _pick_why_line

    long_ones = [
        Reason("Tier 1", "Alpha is a Tier 1 firm on your target list.", "tier"),
        Reason("matches IB", "Alpha covers IB, which you are recruiting for.", "track"),
        Reason("United States", "One of the regions on your profile.", "region"),
        Reason("you know someone here", "You have a warm contact at Alpha.", "network"),
    ]
    out = _pick_why_line(long_ones)

    line = out["pick_why"]
    assert len(line) <= _PICK_WHY_CHARS + 5, line
    parts = line.split(_PICK_WHY_SEP)
    # Whole reasons only: the line never ends inside one of them, and the
    # count is one of the parts rather than hiding inside the last of them.
    hidden = len(long_ones) - len([p for p in parts if not p.startswith("+")])
    for part in parts:
        assert any(r.text == part for r in long_ones) or part == f"+{hidden}", part
    # And what did not fit is COUNTED, never silently dropped.
    if hidden:
        assert parts[-1] == f"+{hidden}", line
        assert line.endswith(f"{_PICK_WHY_SEP}+{hidden}"), (
            f"the count needs the line's own separator in front of it: {line}")
    # The title is always every reason, whatever the line could hold.
    for r in long_ones:
        assert r.detail in out["pick_why_title"]


def test_the_count_never_costs_a_reason_its_place_on_the_line():
    """A first pass at the separator charged the "+N" to `_PICK_WHY_CHARS`
    instead of appending it, which reads as the more careful choice and is
    not. Measured on the founder's board it evicted "2027 intake, a year early
    for you" to make room for the count of what it had just evicted, turning a
    47-character line carrying three real reasons into "Tier 1 · HK · +3
    more": 21 characters, 29 of the budget unspent, and one fewer thing the
    student is told.

    So the budget belongs to the reasons. The constant's own note explains
    that it is set to 50 rather than the ~53 the row fits precisely so the
    count has room outside it."""
    from directory.views import _PICK_WHY_CHARS, _pick_why_line

    # The founder's own row: three reasons that fit, two that do not.
    reasons = [
        Reason("Tier 1", "Nomura is a Tier 1 firm on your target list.", "tier"),
        Reason("HK", "Hong Kong is one of the regions on your profile.", "region"),
        Reason("2027 intake, a year early for you",
               "This is a 2027 programme and you graduate in 2028.", "class"),
        Reason("You know someone here", "You have a warm contact at Nomura.", "network"),
        Reason("IB role", "Nomura covers IB, which you are recruiting for.", "track"),
    ]
    line = _pick_why_line(reasons)["pick_why"]
    assert "2027 intake, a year early for you" in line, (
        f"a reason that fits the budget was dropped to make room for the "
        f"count of the ones that did not: {line}")
    # Which is only meaningful while that reason is genuinely near the limit.
    assert len("Tier 1 · HK · 2027 intake, a year early for you") <= _PICK_WHY_CHARS


def test_a_year_verdict_takes_the_class_reason_off_the_visible_line():
    """The founder's example row said the same thing three times: "Your year"
    in the verdict, "Grad 2028-2029" in the fact chips, and "For 2028-2029
    grads (yours)" here. The first two are different facts (the reader, and
    the posting's own window); this one is a third rendering of them, so it
    comes off the LINE and stays in the title.

    A row with no year verdict keeps it — there it is the only place the year
    is answered at all."""
    from directory.views import _pick_why_line

    cls = Reason("For 2028-2029 grads (yours)",
                 "Your class year is inside the posting's stated window.", "class")
    tier = Reason("Tier 2", "Alpha is a Tier 2 firm on your target list.", "tier")

    with_verdict = _pick_why_line([tier, cls], {"kind": "year_ok"})
    assert "For 2028-2029 grads" not in with_verdict["pick_why"]
    assert "Tier 2" in with_verdict["pick_why"]
    assert "inside the posting" in with_verdict["pick_why_title"], (
        "the reason is suppressed on the line, never from the record")

    without = _pick_why_line([tier, cls], None)
    assert "For 2028-2029 grads (yours)" in without["pick_why"]

    # A row whose ONLY reason is the suppressed one prints nothing rather than
    # a bare "+1" — the template's own `{% if r.pick_why %}` then hides the
    # line, and the year is on the meta line above it either way.
    alone = _pick_why_line([cls], {"kind": "year_ok"})
    assert alone["pick_why"] == ""
