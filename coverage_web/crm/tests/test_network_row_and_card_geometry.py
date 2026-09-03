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
from crm.views import _since_label
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

    Re-measured again after the spacing pass later the same day (see
    `test_the_cards_gaps_group_the_identity_block` below), which took 23px
    out of the card without touching any of that: heights 179.4 x49 at 1280
    and 156.2 x49 at 375, both colour schemes, `.cc-firm` at 77px on all 49
    every time. One height and one firm-line offset are the two properties
    that pass bought and they are unchanged.
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


def test_the_cards_gaps_group_the_identity_block():
    """"Cards still look a little weird ... ensure space is adequately taken
    up, visually harmonious", reported looking at the board in DARK, after
    the skeleton above had already made every card one height.

    The skeleton was not the defect and none of it is undone here. What it
    was SPENDING is. Every slot and every gap, measured on all 49 demo cards
    at 1280 with `content-visibility` forced visible:

        slot/gap        before   after
        padding-top       16       12
        .cc-head          40       40    the avatar; untouched
        gap                8        4
        .cc-tags          16       16    reserved; untouched
        gap                8        4
        .cc-firm        20.8     20.8    one line; untouched
        gap                8        4
        .cc-foot        67.6     64.6
        padding-bottom    16       12
        CARD           202.4    179.4    at 1280   (-11.4%)
                       171.2    156.2    at 375    (-8.8%)

    The first finding retires an assumption worth stating: `margin-top: auto`
    was NOT hoarding the slack. It collected 0.0px on all 49 cards at both
    widths, because everything above the foot is already a fixed height.
    There was no pooled gap to redistribute — the air was spread evenly
    through four identical 8px gaps and 16px of vertical padding.

    Four slots separated by an identical gap say nothing about which of them
    belong together, which is what made the card read as five loose rows. The
    name, its pills and its firm line are one identity block at 4px, and the
    only real division on the card is the seam above the foot (see the test
    below). That is the hierarchy this pins.

    The horizontal padding stays at `var(--s4)`: `.cc-firm`'s truncation
    budget (253.5px at 1280, 283px at 375) and the 3.85em role cap were
    measured against 16px sides, and narrowing them would re-open the
    mid-word cut that rule exists to prevent.
    """
    css = _styles(_page())
    card = _rule(css, ".contact-card")

    assert "padding: var(--s3) var(--s4)" in card, (
        "the card is back on 16px of vertical padding. That is 32px of the "
        "179px card spent above the avatar and below the buttons, on a card "
        "whose own content is four short lines."
    )
    assert "gap: var(--s1)" in card, (
        "the card's slots are back on a flat 8px gap, so the name, its "
        "pills and its firm line read as three unrelated rows instead of "
        "one identity block — and the seam above the foot reads as just "
        "another gap rather than the card's one division."
    )


def test_the_foot_is_the_cards_one_seam_and_it_is_drawn():
    """The dark half of the same report, and the reason it is a rule rather
    than more whitespace.

    Above the foot the card has two bands of reserved-and-possibly-empty
    space within 20px of each other: the pill row, which renders empty on 29
    of the 49 demo cards, and the seam. Bounded only by the card's own edge
    they read as the same thing, and on a dark ground that edge is `--line`
    against `--bg-2` — the contrast that makes a gap read as breathing room
    on paper is not there to make it read as anything but a hole.

    A hairline gives the footer zone an edge of its own. It is `--line`, the
    card's own border token, so it needs no separate dark-mode treatment and
    it was checked in both: identical geometry (179.4 x49 at 1280, 156.2 x49
    at 375) and the same reading in each. It costs the card 1px.

    `padding-top: var(--s3)` is the other half: 4px of card gap, the rule,
    then 12px, so the seam is a deliberate 17px against the 4px gaps above
    it. Before the pass it was 8 + 8 against 8s — the same total, spent
    where nothing could see it.
    """
    foot = _rule(_styles(_page()), ".cc-foot")

    assert "border-top: 1px solid var(--line)" in foot, (
        "the seam above the foot is drawn in whitespace alone again. In dark "
        "mode that is indistinguishable from the empty pill band 20px above "
        "it, which is what got the card reported as having a hole in it."
    )
    # `--s2` since 2026-09-03, not `--s3`. The seam's job is to be BIGGER
    # than the gaps above it, and it still is — those are 4px (name to pills)
    # and 8px (pills to firm), against this 8px plus a drawn hairline, which
    # no gap above it has. The 4px it gave up is exactly what paid for the
    # pills-to-firm gap, so the card kept its height while gaining a
    # hierarchy; see `.cc-foot`'s own comment for the founder's two reports.
    assert "padding-top: var(--s2)" in foot, (
        "the foot's own padding is back level with the gaps above it, so the "
        "card's one real division reads as just another gap."
    )


def test_the_foots_two_axes_are_spaced_apart_from_each_other():
    """`gap: var(--s3)` set BOTH axes of a row that wraps, and the two axes
    are not the same problem.

    ACROSS, 12px is the separation between two zones that belong to
    different owners: a fact on the left, controls on the right.

    DOWN, those same two zones are one footer. The row wraps on every card
    at four across — `.cc-since`'s 7.5rem floor plus 12px plus the 149.1px
    button pair wants 281.1px against 253.5px of card — and at 12px the
    stacked fact and controls read as two loose things rather than one
    block. 4px is what says they are one.

    Forcing the row not to wrap at all was costed and rejected. It needs
    27.6px, and the only places it can come from are the floor that keeps
    the wrap decision uniform across the grid (the widest real string is
    109px, so the floor cannot go below about 7rem) and the buttons' own
    horizontal padding (14px -> 8px). That leaves 2.4px of headroom at 1280
    and still wraps at the grid's 280px `minmax` floor: a layout one glyph
    from breaking, in exchange for squeezed controls.
    """
    foot = _rule(_styles(_page()), ".cc-foot")

    assert "column-gap: var(--s3)" in foot, (
        "the days-since zone and the controls have lost the separation that "
        "keeps them from reading as one run of text across the row."
    )
    assert "row-gap: var(--s1)" in foot, (
        "the wrapped foot is back on a 12px row gap, so at four across the "
        "fact and the controls stack as two loose rows instead of one "
        "footer block — and the card carries 8px more air for it."
    )
    assert not re.search(r"(?<![a-z-])gap: var\(--s3\)", foot), (
        "the foot is back on the `gap` shorthand, which sets both axes at "
        "once: whatever is right for the row is then also forced on the "
        "wrap, and this row wraps on every card at four across."
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
    assert "overflow-y: scroll" in scroll, "the wrapper stopped scrolling"
    # The literal 416 became `var(--band-cap, 416px)` on 2026-09-03, when
    # `capOneBand()` started writing a row-aligned cap the way the tier grids
    # already did: a flat 416 cut 76px into a 154px card. What this test is
    # really protecting is unchanged and still asserted — the cap lives here
    # rather than on the grid, and it FALLS BACK TO A NUMBER. `none` is what
    # the tier grids fall back to, and it is wrong here: a tier holds a
    # couple of dozen firms, a warmth band holds 93, and uncapped that is
    # exactly the "pushes Covered Firms a full screen down" this guards.
    cap = re.search(r"max-height:\s*var\(--band-cap,\s*(\d+)px\)", scroll)
    assert cap, (
        "the wrapper stopped capping the group, so a 93-card warmth row "
        f"pushes Covered Firms a full screen down again. Got: {scroll}"
    )
    assert 200 <= int(cap.group(1)) <= 600, (
        f"the no-JS fallback is {cap.group(1)}px, which does not bound the "
        "band to roughly two rows of cards"
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

    THE FLOOR IS HALF THE MECHANISM. `min-width` is a floor and not a cap,
    so it only fixes the wrap decision for a string that fits inside it, and
    a raw day count does not stay inside it forever. Re-measured 2026-09-03
    on the demo board (49 cards, `content-visibility` forced off, both the
    375 and 1280 layouts), the strings at `--fs-xs`:

        Never contacted           91.4
        43d since last touch     109.0   the oldest touch the board holds
        364d since last touch    116.4   the widest `_since_label` can make
        999d since last touch    117.2   inside the 120px floor
        1234d since last touch   120.2   outgrows it
        3650d since last touch   124.1   outgrows it

    and the defect that follows, with a four-digit count planted on every
    fifth card at 375, where the phone's one-column card gives the foot
    283px against the 120 + 12 + 149.1 = 281.1px its two zones want:

        floor only     footTop 101.8 x11, 125 x38  cardH 156.2 x1, 179.4 x48
        floor + label  footTop 101.8 x49           cardH 156.2 x49

    Two foot offsets in one grid, 23.2px apart, which is the same defect the
    floor was written to prevent arriving by the one route a floor cannot
    block. So this test pins BOTH halves, and the second half is the reason
    the first one is still allowed to say 7.5rem: widening the floor to 8rem
    fixes the same defect and costs 23.2px on all 49 phone cards forever
    (measured, card height 156.2 -> 179.4 at 375, the foot wrapping on every
    card to hold headroom for a number no card is showing), while bounding
    the string costs nothing measurable at any of the five widths.

    The vertical numbers move whenever the card's skeleton moves, and they
    did between this file's last pass and this one (foot 117.8 -> 101.8,
    card 171.2/202.4 -> 156.2/179.4, a spacing change from elsewhere). The
    horizontal ones did not, and they are the ones this test is about: the
    string widths, the 149.1px control zone, the 12px gap, and the 283px of
    foot a phone card has to fit all three into.
    """
    since = _rule(_styles(_page()), ".cc-since")
    # THE FLOOR IS GONE WITH THE WRAP IT REGULATED (2026-09-03). It made the
    # wrap decision a property of the card's width rather than this string's
    # length, so a grid answered it uniformly. `_since_label` now renders
    # "43d ago" (46px) instead of "43d since last touch" (109px), the footer
    # wants ~211px against 246px at the grid's own `minmax(280px)` floor, and
    # there is no wrap left for a floor to keep uniform.
    #
    # What the wrap was costing is why it went rather than being tuned again:
    # a grid row sizes from the card's intrinsic height, resolved at its
    # max-content WIDTH where the footer fits on one line, so the row came out
    # 154px while the wrapped footer needed 177px and `overflow: hidden` cut
    # the difference — measured at 1280, scrollHeight 177 vs clientHeight 152
    # on every card in the band, with Log Touch and Edit inside the 25px being
    # clipped.
    assert "min-width: 0" in since, (
        "the days-since zone has a floor again, which pushes the footer back "
        "toward the wrap that was clipping 25px off every card."
    )
    assert "white-space: nowrap" in since, "the short label must not itself wrap"
    assert "white-space: nowrap" in since, (
        "the fact can wrap inside its own zone now, which is the same "
        "variance by another route."
    )

    # The other half: the string the floor has to hold is bounded, so the
    # floor is a cap by construction rather than by how young the board is.
    # Shortened 2026-09-03. The words that went ("since last touch") were on
    # all 49 cards and are what made the footer want 281.1px inside a 246px
    # card; they now live on the span's `title`. The COUNTING is untouched —
    # the day/year switch, the flooring, and the never-touched case all still
    # answer exactly as they did.
    assert _since_label(None) == "No touches"
    assert _since_label(0) == "0d ago"
    assert _since_label(41) == "41d ago"
    assert _since_label(364) == "364d ago", (
        "the last day before the switch is still counted in days"
    )
    assert _since_label(365) == "1y ago"
    assert _since_label(729) == "1y ago", (
        "the year count floors rather than rounds: 729 days is one full year "
        "of silence and change, not two."
    )
    assert _since_label(730) == "2y ago"
    assert _since_label(3650) == "10y ago"

    # And no count in a century of them produces a wider string than that.
    # A character count, not a pixel one, because a character count is what a
    # test with no layout engine can actually check, and because the two
    # measured strings either side of the floor differ by exactly one
    # character: "364d since last touch" is 21 and fits at 116.4px, "1234d
    # since last touch" is 22 and breaches at 120.2px. So 21 is the number
    # this function is allowed to reach, and 22 is the one that has to come
    # back here and be re-measured. Several strings tie at 21 ("100d" and
    # "364d" both), and the pixel spread inside the tie is under a point:
    # 115.8 against 116.4. It is the character count that moves the answer.
    longest = max(
        (_since_label(d) for d in [None] + list(range(0, 36525))), key=len
    )
    # The bound is what keeps the footer on ONE line, which is now the thing
    # being protected rather than a shared floor. At the grid's own 280px
    # `minmax` the card has 246px of content; the button pair is 149.1px and
    # the column gap 12px, leaving about 85px for this string — roughly 12
    # characters at `--fs-xs`. Anything longer re-opens the wrap, and the wrap
    # is what `overflow: hidden` was silently clipping 25px of.
    assert len(longest) <= 12, (
        f"the longest label this function can produce is now {longest!r} "
        f"({len(longest)} chars). Past ~12 the footer wraps again at the "
        "grid's narrowest column, and a wrapped footer does not grow the "
        "card — it gets cut off by `.contact-card`'s own `overflow: hidden`."
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
    can build (the 280px `minmax` floor less 32px of padding). Re-checked by
    forcing all four pills onto all 49 demo cards at both widths: nothing
    clipped, `.cc-tags` scrollWidth never past its client width.

    RE-MEASURED 2026-09-02 against the current skeleton, when the empty band
    came back as "a hole" in dark mode. Reserve still wins, and the collapse
    was measured rather than argued: `min-height: 0` puts `.cc-firm` at 75px
    on the 20 demo cards that have a pill and 61px on the 29 that do not,
    and the cards at 177.4px and 163.4px — two firm-line offsets AND two
    card heights in one grid, which is both defects this row exists to
    prevent. The band is answered by being smaller instead: the gaps around
    it are 4px now, so a pill-less card shows 24px of empty band where it
    used to show 32px, and the seam below it is drawn (see
    `test_the_foot_is_the_cards_one_seam_and_it_is_drawn`) so the two
    empties can no longer be mistaken for one hole.

    The 16px reserve stays 16px and is deliberately 2px more than the 14px a
    pill actually renders. Trimming it to the measured height leaves no
    headroom, and any face or browser that draws the pill one pixel taller
    then makes a card WITH pills taller than a card without — the same two
    offsets, bought back for two pixels.
    """
    css = _styles(_page())
    tags = _rule(css, ".cc-tags")

    assert "min-height: 16px" in tags, (
        "the pill row no longer reserves its 16px line, so a contact with no "
        "region and no tier pulls the whole card below it up one row. "
        "Measured with the reserve removed: firm line at 61px on 29 cards "
        "and 75px on 20, cards at 163.4px and 177.4px, in one grid."
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
