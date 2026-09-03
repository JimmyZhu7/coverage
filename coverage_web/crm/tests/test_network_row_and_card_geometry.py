"""Two geometry defects on the Network board, both reported by eye.

"looks weird, refine design with opus" about the Coverage Gaps ledger, and
"why are card sizes not standardized, fix" about the contact grid. Both were
measured before and after with headless Playwright against the demo board
(`demo@coverage.local`, 49 contacts, 6 gap rows) at 1280x800, 375x812 and
three widths between, in both colour schemes.

The ledger half of that work was deleted on 2026-09-02 along with the widget
it laid out. Its eight tests are replaced by two: one that the rules went
with the markup, and one for the "CG" chip that carries the widget's meaning
on the firm card now. Same method, one surface fewer.

A Django test client has no layout engine, so the same rule this repository's
other geometry guards follow applies here: the assertions are about the CSS
declarations that PRODUCE the measurement, and each docstring carries the
pixels it is standing in for. Every number below was read off a real render.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from crm.models import Contact, UserFirm
from directory.models import Firm

pytestmark = pytest.mark.django_db

NETWORK = "/app/contacts/"


def _page() -> str:
    user = get_user_model().objects.create_user(
        email="net-geometry@example.com", password="x" * 14
    )
    client = Client()
    client.force_login(user)
    return client.get(NETWORK).content.decode()


def _page_with_a_contact() -> str:
    """The warmth ledger only renders rows that hold somebody, so a board
    with no contacts renders no grid and no wrapper around it."""
    user = get_user_model().objects.create_user(
        email="net-geometry-cards@example.com", password="x" * 14
    )
    Contact.all_objects.create(user=user, name="Sarah Goldberg")
    client = Client()
    client.force_login(user)
    return client.get(NETWORK).content.decode()


def _page_with_a_gap() -> str:
    """A board with one tiered firm nobody is at, so a firm card actually
    renders the CG tag. `_page()` above has no firms at all, which is fine
    for reading the stylesheet and useless for reading the markup."""
    user = get_user_model().objects.create_user(
        email="net-geometry-gap@example.com", password="x" * 14
    )
    firm = Firm.objects.create(slug="exposed-co", name="Exposed Co")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    client = Client()
    client.force_login(user)
    return client.get(NETWORK).content.decode()


def _styles(html: str) -> str:
    blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    assert blocks, "the Network page no longer renders a <style> block"
    return "\n".join(blocks)


def _rule(css: str, selector: str) -> str:
    """The first rule whose selector list IS this selector.

    Anchored to the start of a line on purpose. The deleted gap ledger is
    where this bit: `.gap-act` also appeared inside `.gap-card > .gap-act {
    grid-column: 6; }`, and an unanchored search read that one-declaration
    placement rule as the whole button. `.firm-card-head .gap-due-tag` is the
    same trap on the surface that survived, so the anchor stays.
    """
    match = re.search(
        rf"^[ \t]*{re.escape(selector)} \{{(.*?)\}}", css, re.S | re.M
    )
    assert match, f"{selector} is gone from the Network page; re-check this guard"
    return match.group(1)


# ---------------------------------------------------------------------------
# Coverage Gaps: the ledger this section measured is deleted
# ---------------------------------------------------------------------------
def test_the_gap_ledgers_geometry_is_gone_with_the_ledger():
    """The eight tests that stood here are retired, not weakened.

    They pinned the Coverage Gaps ledger's layout: one set of subgrid column
    tracks across six rows, the flexible gutter that belonged to no zone, one
    button width, the tier tag's two channels after the rail came off, the
    shared baseline, the state phrase outranking the tier tag, and the "Who
    to find" toggle's resting rule. Every one of them was a claim about
    markup the founder asked to have deleted on 2026-09-02, and a claim about
    deleted markup cannot be true or false, only unreachable.

    What replaces them is the guarantee that made them worth writing: this
    board carries one row shape and one card shape, and a rule left behind
    for markup that cannot render is exactly how a second shape creeps back
    in. So this asserts the whole family is gone from the stylesheet, and
    that the firm card's countdown, which shared one class with the strip,
    kept its own rule when the block around it went.

    The geometry that REPLACED the ledger is on the firm card and is pinned
    where firm cards are pinned: `test_the_cg_tag_is_built_like_the_chip_
    beside_it` below, and crm/tests/test_coverage_gaps.py for the bar that
    decides which cards wear it.
    """
    css = _styles(_page())
    for selector in (
        ".gap-strip", ".gap-row", ".gap-card", ".gap-name", ".gap-head",
        ".gap-tier-tag", ".gap-state", ".gap-act", ".gap-t1 .gap-tier-tag",
        ".gap-card .src", ".src-toggle", ".src-panel", ".src-note",
        ".src-list", ".src-row", ".src-link", ".src-why",
    ):
        assert not re.search(rf"^[ \t]*{re.escape(selector)} \{{", css, re.M), (
            f"{selector} still has a rule, with no markup that can use it"
        )
    assert "@keyframes src-drop" not in css
    assert "grid-template-columns: subgrid" not in css, (
        "the ledger's shared column tracks outlived the ledger"
    )
    # The one class the firm card adopted, which must NOT have gone with it.
    assert "color: var(--danger)" in _rule(css, ".gap-due-tag")


def test_the_cg_tag_is_built_like_the_chip_beside_it():
    """The mark that replaced the ledger, and the one rule it has to hold: it
    may not become a second red shouting at the countdown it shares a 22px
    row with.

    Both are `--danger`, which is the token the founder asked for and the one
    the palette already carries. They are separated by SHAPE instead: the
    countdown is bare text on the card's own ground, and the tag is a
    soft-filled chip built exactly like the green "SP" beside it. Measured at
    1280x800 on the demo board, a head row carrying the name, CG, SP and a
    countdown all at once is 22px on one line and its card is 96px, the same
    as a card with none of them. Nothing wraps; the name is the only thing
    that gives, which is this row's existing contract.
    """
    css = _styles(_page())
    shared = _rule(css, ".pill.fc-spon, .pill.fc-cg")
    assert "font-size: 10px" in shared and "padding: 2px var(--s2)" in shared, (
        "the two chips stopped sharing a box, so one of them is a new shape"
    )
    cg = _rule(css, ".pill.fc-cg")
    assert "background: var(--danger-soft)" in cg, (
        "CG lost its fill, which leaves two bare red marks on one row"
    )
    assert "color: var(--danger)" in cg and "border-color: var(--danger-line)" in cg
    assert "background" not in _rule(css, ".gap-due-tag"), (
        "the countdown grew a fill, so the two red marks are one shape again"
    )
    # Both chips are `flex: none`, so the firm NAME is the only thing on the
    # head row a mark can squeeze.
    assert "flex: none" in _rule(
        css, ".firm-card-head .fc-spon, .firm-card-head .fc-cg"
    )
    assert "flex: 1 1 auto" in _rule(css, ".firm-card-name")
    # And the chip actually reaches the head row, inside it rather than on a
    # line of its own: a mark on its own row is the four-lines-for-two-facts
    # shape "SP" was folded up here to escape.
    # Bounded on the NEXT row of the card, not on the first `</div>`: the
    # firm name is its own div inside the head, so a lazy `.*?</div>` closes
    # on the name and never sees the chips after it.
    page = _page_with_a_gap()
    start = page.index('<div class="firm-card-head">')
    head = page[start : page.index('<div class="firm-card-foot">', start)]
    assert 'class="pill fc-cg"' in head, "the CG chip is not in the card's head row"
    assert head.index("firm-card-name") < head.index("pill fc-cg"), (
        "the chip leads the row; the firm the card is about has to"
    )


# ---------------------------------------------------------------------------
# The contact grid: one card height
# ---------------------------------------------------------------------------
def test_every_contact_card_in_the_grid_is_one_height():
    """`1fr` rows are what makes all ROWS equal rather than just the cards
    within one row, `stretch` fills each row with its card, and the foot's
    `margin-top: auto` puts the controls at the same offset from the bottom
    of every card.

    Re-measured 2026-09-02 across all five warmth grids at once, 49 cards,
    with `content-visibility` disabled for the measurement (leave it on and
    every card reports its `contain-intrinsic-size` placeholder instead of
    its box, which reads as a flat "no change" whatever you do):

        1280x800   before  heights 156 x16, 178 x9, 202 x24   foot 72/94/118
                   after   heights 202 x49                    foot 118 x49
        375x812    before  heights 128 x16, 146 x9, 171 x24   foot 75/93/118
                   after   heights 171 x49                    foot 118 x49

    The rules below were already doing their job WITHIN a grid before this
    pass; what changed is that the card's own slots are fixed heights now
    (see the two tests below), so the tallest card in every warmth section is
    the same card, and the whole page settles on one height instead of one
    height per section.
    """
    css = _styles(_page())
    grid = _rule(css, ".contact-grid")
    assert "grid-auto-rows: 1fr" in grid, (
        "the rows size themselves again, so a card with a wrapped name makes "
        "its row 22px taller than the row above it."
    )
    assert "align-items: stretch" in grid, (
        "the cards no longer fill their row, so an equal row does not mean "
        "an equal card."
    )
    assert "margin-top: auto" in _rule(css, ".cc-foot"), (
        "the controls row floats after the firm line again instead of "
        "sitting a fixed distance off the bottom of the card."
    )


def test_the_grid_height_cap_is_not_on_the_grid():
    """`1fr` divides the space it is given. With `max-height: 416px` still on
    the grid the browser had a definite box and handed each row 416/3 =
    128px, which clipped the foot off every card whose firm ran to two
    lines — measured, footOff 92 inside a 128px card. The cap and the
    scrollbar live on `.contact-scroll` now and the grid is content-sized,
    which is what lets `1fr` mean "as tall as the tallest row".
    """
    html = _page()
    css = _styles(html)
    assert "max-height" not in _rule(css, ".contact-grid"), (
        "the cap is back on the grid, which collapses every row to a third "
        "of it and clips the cards."
    )
    scroll = _rule(css, ".contact-scroll")
    assert "max-height: 416px" in scroll and "overflow-y: scroll" in scroll, (
        "the wrapper stopped capping the group, so a 93-card warmth row "
        "pushes Covered Firms a full screen down again."
    )
    assert '<div class="contact-scroll">' in _page_with_a_contact(), (
        "the wrapper is not rendered, so the cap and the grid are the same "
        "element again."
    )


def test_the_foot_wraps_on_the_card_width_not_on_the_day_count():
    """`.cc-foot` wraps when its two zones do not fit, and the zones measured
    105 to 108px depending on how many digits the day count carried. At
    700px the foot has 267px and the two zones plus their gap wanted 266 to
    269, so three pixels of text decided the layout: two cards in one row
    came out with feet of 36px and 68px and their controls 31px apart. A
    fixed floor on the zone makes the answer a property of the card's width,
    identical for every card in the grid.
    """
    since = _rule(_styles(_page()), ".cc-since")
    assert "min-width: 7.5rem" in since, (
        "the days-since zone sizes itself to its text again, so two cards in "
        "one row can disagree about whether the foot wraps."
    )
    assert "white-space: nowrap" in since, (
        "the fact can wrap inside its own zone now, which is the same "
        "variance by another route."
    )


def test_the_firm_line_is_one_line_and_the_role_is_what_survives_it():
    """THIS TEST REPLACES `test_the_firm_line_reads_before_it_cuts`, whose
    premise was retired hours after it was written.

    That test pinned `-webkit-line-clamp: 2` on `.cc-firm`, and it was right
    about its own problem: at 173px of room "Goldman Sachs · Campus
    Recruiting Manager" rendered as "Goldman Sachs · Campus R…", an ellipsis
    one letter into the word carrying the meaning, and two lines fitted every
    string on the board. What it could not see is that a clamp which is two
    lines on some cards and one on others is a variable-height fact, and the
    card is a fixed skeleton now: "the description of their job cannot spill
    over into the second line."

    So the mid-word cut it was protecting against is answered a different
    way, and this test pins THAT instead of weakening what it asked for:

      ROOM. The line is a full-width row of the card, not a column beside
      the 40px avatar. 253px at 1280 and 283px at 375, against 173px.
      Two-line firm lines on the demo board: 2 before, 0 after, both widths.

      SPLIT. Firm and role truncate independently and the firm is the half
      that gives way, because two people at the same firm are told apart by
      the role. Rendered against all 384 real firm/role pairs on the demo
      and the founder's boards, at every line width this grid produces:
      roleCut 0 everywhere; firmCut 2 of 323 and 1 of 61 at the narrowest,
      0 at 321px.

      SHRINK ZERO, not a ratio. At 100-to-1 the role still gave up 0.34px of
      Rachel Lin's 18.2px deficit, and `text-overflow: ellipsis` fires on any
      overflow at all and then eats three more characters to fit its own
      glyph: a third of a pixel cost four characters. Only 0 is safe.
    """
    css = _styles(_page())
    firm = _rule(css, ".cc-firm")
    assert "line-clamp" not in firm, (
        "the firm line is back on a multi-line clamp. One card in the grid "
        "then spends two lines on it and the next spends one, which is the "
        "spill this was reported for."
    )
    assert "white-space: nowrap" in firm, (
        "the firm line can wrap again, so its height is a property of the "
        "contact's job title rather than of the card."
    )
    assert "text-overflow: ellipsis" in _rule(css, ".cc-firm-name"), (
        "the firm name clips instead of ellipsising, so a reader cannot see "
        "that anything was cut."
    )

    role = _rule(css, ".cc-firm-role")
    assert "flex: 0 0 auto" in role, (
        "the role can shrink again. Any shrink at all, even a third of a "
        "pixel, triggers its ellipsis and costs four characters — the role "
        "is the half that distinguishes two people at the same firm and it "
        "must give up nothing while the firm still has room to give."
    )
    assert "max-width: calc(100% - 3.85em)" in role, (
        "the cap that guarantees the firm 3.5em is gone. It is what makes "
        "`flex: 0 0 auto` on the role safe: without it a very long role "
        "leaves the firm nothing to shrink into and overflows the card."
    )
    assert "min-width" not in _rule(css, ".cc-firm-name") or (
        "min-width: 0" in _rule(css, ".cc-firm-name")
    ), (
        "the firm has a min-width floor again. A min-width is also a WIDTH: "
        "it pads 'BCG' and 'USC' out to the floor and hands the reserved "
        "pixels to nobody. Measured at a 5.5em floor, that cut the role on 4 "
        "of 323 rows on the founder's board and 1 of 61 on the demo one; the "
        "cap on the role cuts none at any width."
    )


def test_the_pill_row_is_reserved_whether_or_not_it_has_pills():
    """The pills used to ride the name's line, and there are four of them —
    Parked, gender, tier, region — each independently conditional. Measured
    at 1280x800 on the demo board, that put the firm line under them at three
    different heights in one grid:

        firmTop  before  43 x41, 65 x5, 89 x3    (3 distinct)
                 after   89 x49                  (1)

    and the founder's own 323-contact board has the same shape: "Parked HK"
    on 81 rows, a bare "US" on 59, nothing at all on 58.

    Reserved beats omitted, and both were measured. Reserved, `.cc-firm` sits
    at 89px on all 49 cards and a pill-less card shows an empty band where
    its pills would be; it costs 24px of card height only when NO card in the
    grid has a pill, because `grid-auto-rows: 1fr` was already stretching
    every card to the tallest one. Omitted, the 29 pill-less demo cards pull
    their firm line up 24px and the grid has two firm-line offsets again.

    `nowrap` is safe rather than hopeful: the widest combination the data can
    produce is Parked + gender + tier + region = 59.6 + 25.5 + 29.2 + 46.8
    plus three 8px gaps = 185px, against 248px in the narrowest card the grid
    can build (the 280px `minmax` floor less 32px of padding).
    """
    css = _styles(_page())
    tags = _rule(css, ".cc-tags")

    assert "min-height" in tags, (
        "the pill row no longer reserves its line, so a contact with no "
        "region and no tier pulls the whole card below it up one row."
    )
    assert "flex-wrap: nowrap" in tags, (
        "the pill row can wrap to a second line again, which is the original "
        "defect moved one row down: one card in the grid is then 24px taller "
        "inside than its neighbour."
    )

    html = _page_with_a_contact()
    assert re.search(r'class="[^"]*\bcc-tags\b', html), (
        "the pill row is not rendered at all. It is rendered UNCONDITIONALLY "
        "on purpose — the contact seeded by this test has no pills, and the "
        "empty row is what holds the skeleton open."
    )
    assert not re.search(r'class="[^"]*\bcc-pill\b', html), (
        "this fixture's contact has no region, tier, gender or parked state, "
        "so it should render an EMPTY pill row. A pill here means the "
        "reserved-row measurement above was taken against different markup."
    )


def test_the_identity_row_is_the_avatars_own_height():
    """The other source of drift, and the one that survives a name nobody
    predicted: the name wrapped. "Mariela Jimenez-Sanchez" on the founder's
    board measures 181px in this face and "Bartholomew Vanderhoeven" on the
    demo one 204px, against 173px of room, so both take two lines and used to
    push every fact under them down 24px.

    The name is still never truncated (see the test below). The head absorbs
    the second line instead: it is pinned at the avatar's own 40px, and the
    name's line-height is 20px so that TWO lines land on exactly that 40px.
    Three of the 49 demo cards wrap their name at 1280 and one at 375, and
    `.cc-firm` sits at 89px on all 49 either way.

    `align-items: center` is what makes a one-line name sit against the
    middle of the avatar rather than its cap-height, and it also replaced the
    hand-computed 12px nudge on the checkbox — (40 - 16) / 2, arithmetic
    standing in for a centring the row could not do while it was on
    `flex-start`.
    """
    css = _styles(_page())
    head = _rule(css, ".cc-head")
    assert "min-height: 40px" in head, (
        "the identity row can be shorter or taller than the avatar again, so "
        "a wrapped name moves the pill row and the firm line down with it."
    )
    assert "align-items: center" in head, (
        "the identity row is back on top alignment, which leaves a one-line "
        "name floating above the middle of a 40px avatar."
    )
    assert "line-height: 20px" in _rule(css, ".cc-name"), (
        "the name's line-height is not the half of the 40px head it has to "
        "divide into, so a two-line name overflows the row that reserves it."
    )
    assert "margin-top: 0" in _rule(css, "input.cc-check"), (
        "the checkbox is back on a hand-computed top margin. The row centres "
        "it now; a fixed nudge goes wrong the moment the avatar or the box "
        "is resized."
    )


def test_the_contact_name_is_still_never_truncated():
    """The card grew a fixed frame, and the obvious way to hold a frame is to
    cut what does not fit. The name is the one thing on this card a reader is
    scanning for, and the same rule `.net-mini-name` states applies here: it
    wraps, it is never ellipsised. The frame absorbs the second line instead.
    """
    name = _rule(_styles(_page()), ".cc-name")
    assert "nowrap" not in name, "the contact name is back on one clipped line"
    assert "ellipsis" not in name, "the contact name truncates with an ellipsis"
    assert "line-clamp" not in name, "the contact name is clamped"
