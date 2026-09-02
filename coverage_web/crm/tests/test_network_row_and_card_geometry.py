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
    """Measured at 1280x800 on the demo board, the nine cards in the open
    warmth row:

        before   heights 156 x8, 178 x1   foot offsets 71 x8, 93 x1
        after    heights 178 x9           foot offsets 93 x9

    and at every other width tested, one height and one foot offset per
    grid: 1600 -> 146, 900 -> 128, 700 -> 178, 375 -> 146, nothing clipped.

    `1fr` rows are what makes all ROWS equal rather than just the cards
    within one row, `stretch` fills each row with its card, and the foot's
    `margin-top: auto` puts the controls at the same offset from the bottom
    of every card.
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


def test_the_firm_line_reads_before_it_cuts():
    """The column is 174px and "Goldman Sachs · Campus Recruiting Manager"
    wants 270px, so the board rendered "Goldman Sachs · Campus R…" — the
    ellipsis one letter into the word carrying the meaning. Two lines is
    348px, which fits that title, "Bain & Company · Bain Campus Recruiter"
    and every firm · role string on the demo board outright. Wrapping stays
    `normal`, so a line that still overruns breaks at a space rather than
    inside a word, and the clamp is the cap that stops a 124-character title
    becoming three lines.
    """
    firm = _rule(_styles(_page()), ".cc-firm")
    assert "-webkit-line-clamp: 2" in firm, (
        "the firm line is capped somewhere other than two lines; one cuts "
        "mid-word and three is no longer a bounded card."
    )
    assert "nowrap" not in firm, (
        "the firm line is back on one line, which is the truncation that was "
        "reported: an ellipsis four letters into the role."
    )
    assert "anywhere" not in firm and "break-all" not in firm, (
        "the line breaks inside words again, which is the mid-word cut this "
        "was fixed to avoid."
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
