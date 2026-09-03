"""Every column header on the feed names its column on the same baseline.

THE HEADER IS A TWO-ROW GRID (2026-09-02) and that is what keeps the promise
now. Row one is the logo tile and the firm name, centred on each other. Row
two is one line carrying the category, the open count and the tier. A name
cannot move, whatever follows it, because it is in a row of its own.

WHAT CAME BEFORE, and why the grid is the fix rather than a restyle. The head
was a flex row: a 38px tile, then an id COLUMN of three short lines. Two
defects came out of that shape. `align-items: center` centred each id stack,
so where a name LANDED depended on what came after it — Picked-for-you emits
its `.firmcol-stats` row unconditionally and it held nothing until a why-chip
existed, so that column's id block measured 42.2px against a firm's 61.4px and
its name rendered 9.6px lower than Morgan Stanley's beside it (measured live
at 1280px). `align-items: flex-start` fixed that one by declaration. The
second defect survived it: the tile kept its own centring against a three-row
stack, so the logo sat level with the CATEGORY rather than with the firm it
stands for, and the founder's review of the shipped page said so — "the top
part of these widgets look weird ... make it more visually harmonious".

A tile anchoring three lines has nothing to align to. Two rows give it one.
Measured after: the firm head went 126px -> 86px, the Picked head 194px ->
139px, row one computes to exactly the 38px tile, and every column's name
starts at the same y whether its neighbour's name wraps to two lines or not.

The old note about the empty stats div is retired with the flex column that
made it true: `.firmcol-stats` now always carries at least the category and
count, and it is a grid row rather than a flex sibling, so nothing about the
name's position depends on it at all.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db

_STYLE_RE = re.compile(r"<style>(.*?)</style>", re.S)


def _feed_css() -> str:
    html = Client().get("/opportunities/").content.decode()
    blocks = _STYLE_RE.findall(html)
    assert blocks, "the feed should render its own <style> block"
    return "\n".join(blocks)


def _rule(css: str, selector: str) -> str:
    match = re.search(r"^\s*" + re.escape(selector) + r"\s*\{(.*?)\}", css, re.S | re.M)
    assert match, f"no rule found for {selector}"
    return " ".join(match.group(1).split())


@pytest.fixture
def feed_with_both_columns(db):
    """A profiled student, and picks that share no reason.

    Every piece is load-bearing. The Picked column only renders for a signed-in
    user with a profile; the firm columns it must line up with only exist if
    there are roles; and its stats row is only EMPTY — the state that made the
    centring fail — when no reason is byte-identical across every pick, which
    is why the two firms differ in tier, region and cohort.
    """
    from django.contrib.auth import get_user_model

    from crm.models import UserFirm
    from directory.models import Firm, Opportunity

    alpha = Firm.objects.create(slug="alpha", name="Alpha Partners", tracks=["ib"])
    beta = Firm.objects.create(slug="beta", name="Beta Securities", tracks=["ib"])
    Opportunity.objects.create(
        firm=alpha, url="https://x.test/1", title="2027 Summer Analyst Programme",
        bucket="internship", cohort="2027", status="open", region="us",
        location="New York",
    )
    Opportunity.objects.create(
        firm=beta, url="https://x.test/2", title="2028 Summer Analyst Programme",
        bucket="internship", cohort="2028", status="open", region="hk",
        location="Hong Kong",
    )
    user = get_user_model().objects.create_user(
        email="head@example.com", password="x" * 14
    )
    user.class_year = 2029
    user.target_cycles = ["SA 2028"]
    user.school = "USC Marshall"
    user.regions = ["us", "hk"]
    user.tracks = ["ib"]
    user.save()
    UserFirm.all_objects.create(user=user, firm=alpha, tier=1)
    UserFirm.all_objects.create(user=user, firm=beta, tier=2)

    client = Client()
    client.force_login(user)
    return client


def test_the_header_puts_the_name_in_a_row_nothing_below_it_can_move():
    """REWRITTEN 2026-09-02. The old assertions were `align-items:
    flex-start`, no `align-items: center`, and `min-height: 92px` — three
    declarations that made a FLEX row keep the name's y independent of the
    stack under it. The header is a grid now, so the same promise is kept by
    structure instead: the name is in row one and everything else is in row
    two, and a grid row's position cannot depend on a later row's content.

    `align-items: center` is not only allowed now, it is required — it is
    what levels the 38px tile with the name beside it, which is the alignment
    complaint that prompted the change. Under the old flex shape that same
    declaration was the bug; under the grid it applies per row, and row one
    holds nothing but the tile and the name."""
    css = _feed_css()
    rule = _rule(css, ".firmcol-head")
    assert "display: grid" in rule, rule
    # THE FLOOR MOVED, THE INVARIANT DID NOT (2026-09-03). Row one was
    # floored at 38px, the tile's own height, so the header could never be
    # shorter than the mark. But the tile spans BOTH rows, so it was already
    # holding the header open by itself; all the floor did was inflate the row
    # the NAME sits in — an 18px title in a 38px row, putting 14px between the
    # name and the stats line that belongs to it against a declared 4px
    # row-gap. The equal-height guarantee now rests on `min-height` alone.
    # Measured after: all 13 headers 76px, name-to-stats 8.9px.
    assert "grid-template-rows: auto auto" in rule, rule
    assert "min-height: 76px" in rule, "the fixed header height is still half of it"
    name = _rule(css, ".firmcol-h")
    assert "grid-row: 1" in name, name
    stats = _rule(css, ".firmcol-stats")
    assert "grid-row: 2" in stats, stats


def test_the_logo_spans_the_header_and_stays_in_its_own_column():
    """REWRITTEN 2026-09-02, because its premise was retired by the thing it
    was guarding against.

    It was `test_the_logo_sits_in_the_name_row_and_nowhere_else`, and row one
    was the right answer to the question it was asked: the header had been a
    tile beside a THREE-row text block, so a tile centred on the block landed
    beside the middle row, which was the category line rather than the firm
    name. Pinning the tile to row one fixed that.

    The header lost its third row in the same pass that wrote this test, and
    with two rows the original reasoning inverts. A tile centred on row one
    now sits 13.6px above the centre of the block beside it — measured on all
    13 columns of the founder's board at 1280px and 375px, and his own reading
    of it was that the logo needed to come down. So the tile spans the header
    and centres on the whole block, which is what the retired test's own
    sentence was reaching for when only one row was worth centring against.

    `grid-column: 1` is the half of the old assertion that never depended on
    the row count, and it is kept verbatim."""
    css = _feed_css()
    rule = _rule(css, ".firmcol-logo")
    assert "grid-row: 1 / -1" in rule, rule
    assert "grid-column: 1" in rule, rule
    # Two rows, so "both of them" and "all of them" are the same span. A third
    # row would silently change what `-1` means, and the header must not grow
    # one — see `test_the_picked_columns_header_spends_the_same_two_rows_a_firms_does`.
    # `auto auto` since 2026-09-03. The 38px floor was the logo's own height
    # and the logo spans both rows, so it held the header open without help —
    # all the floor did was inflate the title's row, opening 14px between the
    # name and the stats line under it against a declared 4px row-gap. The
    # header now rests on its `min-height: 76px` instead, which is the number
    # the Picked column's own comment calls the one that lands it level with
    # its neighbours; measured, all 13 headers are 76px and the title/stats
    # pair closed to 8.9px.
    assert "grid-template-rows: auto auto" in _rule(css, ".firmcol-head")


def test_the_logo_tile_keeps_its_own_centring():
    """The tiles were already level; the fix must not move them. Under the
    grid this centres the tile inside the ROWS IT SPANS, which is both of
    them — see the test above for why that stopped being row one alone."""
    assert "align-self: center" in _rule(_feed_css(), ".firmcol-logo")


def test_the_heading_carries_no_margin_of_its_own():
    """`.firmcol-h` is an h2 and the shared stylesheet gives an h2 20px of
    vertical margin. Inside the old flex column that collapsed away; inside
    the grid it does not. Measured before the reset: row one computed 58px
    against a 38px tile, so every header on the board ran 20px taller than
    the design and the tile floated in an empty band."""
    assert "margin: 0" in _rule(_feed_css(), ".firmcol-h")


def test_the_picked_columns_shared_reasons_are_one_nowrap_line_not_wrapping_pills():
    """Measured live at 1440px: the Picked header rendered its two shared
    reasons as `.why-chip` pills (89px + 169px in a 236px stats row), which
    wrapped to a second line and made that header 122px against every firm
    column's 92px — pushing all of Picked's cards 30px below the row they
    sit in. The reasons now render as one `.firmcol-why` text line in the
    same voice as a firm's "TIER 1 · 56 CLOSING", which must be forbidden
    from wrapping and must ellipsise instead, with the full sentences kept
    in the tooltip."""
    css = _feed_css()
    rule = _rule(css, ".firmcol-why")
    assert "white-space: nowrap" in rule, rule
    assert "text-overflow: ellipsis" in rule, rule
    assert "overflow: hidden" in rule, rule
    assert "min-width: 0" in rule, "a flex child can't shrink below its content without this"
    assert ".why-chip" not in css, "the wrapping pills are gone from the header for good"


def test_the_picked_column_renders_shared_reasons_in_the_firmcol_why_line(db):
    """Two picks that share a cohort AND a bucket AND a tier: the shared
    reasons must land inside `.firmcol-why` (with each full sentence in its
    `title`), never as `.why-chip` pills."""
    from django.contrib.auth import get_user_model

    from crm.models import UserFirm
    from directory.models import Firm, Opportunity

    alpha = Firm.objects.create(slug="alpha", name="Alpha Partners", tracks=["ib"])
    beta = Firm.objects.create(slug="beta", name="Beta Securities", tracks=["ib"])
    for firm, n in ((alpha, 1), (beta, 2)):
        Opportunity.objects.create(
            firm=firm, url=f"https://x.test/{n}", title="2028 Summer Analyst Programme",
            bucket="internship", cohort="2028", status="open", region="us",
            location="New York",
        )
    user = get_user_model().objects.create_user(email="why@example.com", password="x" * 14)
    user.class_year = 2029
    user.target_cycles = ["2028 Summer Internship"]
    user.school = "USC Marshall"
    user.regions = ["us"]
    user.tracks = ["ib"]
    user.save()
    UserFirm.all_objects.create(user=user, firm=alpha, tier=1)
    UserFirm.all_objects.create(user=user, firm=beta, tier=1)

    client = Client()
    client.force_login(user)
    html = _STYLE_RE.sub("", client.get("/opportunities/").content.decode())
    picked = re.search(r'<article class="firmcol firmcol--picked.*?</header>', html, re.S)
    assert picked, "the Picked column should render for a profiled student with picks"
    head = picked.group(0)
    assert 'class="firmcol-why"' in head, head
    assert "why-chip" not in head
    assert "2028 Summer Internship" in head
    assert 'title="' in head, "the full sentences must survive in the tooltip"


def test_both_columns_spend_the_same_two_rows_on_their_identity(feed_with_both_columns):
    """REWRITTEN 2026-09-02, twice over, and the history is the argument.

    It was first written to prove the Picked column's id STACK is shorter
    than a firm's — the condition that made `align-items: center` misalign
    the two — and then rewritten to assert the `fc-eyebrow` span, because
    that word had moved into the stats slot and made the "empty row" premise
    unreachable. Both premises are gone now. The header is a two-row grid, so
    stack height cannot move a name at all; and the eyebrow is deleted,
    because it read "PICKED" directly under a heading reading "Picked for
    you" and the founder's review called that what it was.

    What survives, and is the thing worth pinning: both columns say who they
    are in exactly two rows, and row two is one line in both. The Picked
    column has no tier, so its second row carries its count and whatever the
    picks share; a firm's carries its category, its open count and its tier.
    Different words, same two rows, which is why the headers line up.
    """
    html = feed_with_both_columns.get("/opportunities/").content.decode()
    html = _STYLE_RE.sub("", html)

    stats = re.findall(r'<div class="firmcol-stats">(.*?)</div>', html, re.S)
    assert len(stats) >= 2, f"expected the Picked column and a firm one, got {len(stats)}"
    assert "fc-eyebrow" not in html, (
        "the 'PICKED' eyebrow is back. It repeats the heading directly above "
        "it; the accent star tile and the accent heading are what say this "
        "column is not a firm (see the identity tests below)")
    # Row two, both columns: one `.firmcol-meta` line, and the tier only
    # where a tier exists.
    assert "firmcol-meta" in stats[0], (
        "the Picked column's second row should carry its own count line — "
        "if it moved back out to a row of its own the header grew a third row")
    assert "firmcol-tier" not in stats[0], "the Picked column has no tier"
    assert "firmcol-meta" in stats[1] and "firmcol-tier" in stats[1], (
        "a firm's category, open count and tier belong on ONE line; they were "
        "two stacked rows beside the logo tile until 2026-09-02, which is the "
        "layout the founder called cluttered")
    assert ":empty" not in _feed_css(), (
        "a :empty selector cannot match that row — it holds a whitespace text "
        "node — so a fix resting on one would be silently dead"
    )


# ---------------------------------------------------------------------------
# THE PICKED COLUMN'S IDENTITY (2026-08-31). It used to carry an accent WASH
# (`background: var(--accent-soft)`), and the founder's own dark-mode screen-
# shot is why it does not any more. Measured on the rendered page:
#
#     --accent-soft #232a35 vs --surface #1c201a   1.144:1
#     --accent-soft #232a35 vs --paper   #141712   1.252:1
#
# So the wash separated the column by HUE, not luminance — a blue-grey slab
# on a green-black page. Worse, `.firmcol-scroll` paints `--surface` over the
# whole body, so the wash only ever reached the header: the column rendered
# as a blue lid on a green-black box with that 1.144:1 jump at the seam
# between them, which was the harshest edge in the column and internal to it.
#
# What replaces it must be structural, and these tests pin that it IS
# structural — because with the wash gone there is very little left, and on
# the founder's board there is even less than the stylesheet assumes: all 12
# of his firm columns are `.is-mine`, so the `--accent-line` border the
# Picked column wears is not distinguishing it from anything.
# ---------------------------------------------------------------------------


def test_the_picked_column_shares_its_neighbours_surface():
    """No wash, and specifically no `--accent-soft`: the column that is meant
    to look calm must not be the one column painted a different hue from the
    page it sits in, and it must not seam against its own scroll body."""
    rule = _rule(_feed_css(), ".firmcol--picked")
    assert "background: var(--surface)" in rule, rule
    assert "accent-soft" not in rule, (
        "the accent wash is back. It measures 1.144:1 against the --surface "
        "its own scroll window paints, so it reads as a hue change rather "
        "than a level, and the seam between the two is inside the column."
    )


def test_the_picked_column_still_says_it_is_not_a_firm_without_the_wash():
    """Three structural signals, none of them a fill: the accent top edge,
    the accent hairline border, and the star tile. Removing the wash removed
    a fourth, so the survivors are load-bearing and pinned here."""
    css = _feed_css()
    rule = _rule(css, ".firmcol--picked")
    assert "inset 0 3px 0 0 var(--accent)" in rule, (
        "the heavier accent top edge is the strongest remaining signal that "
        "this column is a view and not a company")
    assert "border-color: var(--accent-line)" in rule, rule
    assert "var(--accent-ink)" in _rule(css, ".firmcol--picked .firmcol-name")


def test_the_star_tile_actually_wins_the_cascade():
    """It did not, for as long as the wash was there to hide it.

    `.firmcol-logo--picked` is ONE class and `.firmcol-logo` is one class,
    and the generic rule sits ~46 lines later in the file, so source order
    handed the star the default monogram tile: measured live in light, the
    tile rendered rgb(216, 230, 243) — `hsl(210 52% 90%)` — instead of
    `--accent`, and the inset hairline this rule asks to drop came back too.
    All three declarations were dead.

    Pinned by SPECIFICITY rather than by source order, so re-sorting this
    stylesheet cannot silently kill the star again. That matters more now
    than it did: the tile is the one signal on this founder's board that no
    firm column shares."""
    css = _feed_css()
    generic = css.index("\n  .firmcol-logo {")
    picked = css.index(".firmcol-logo.firmcol-logo--picked {")
    assert picked < generic, (
        "if the picked rule ever moves BELOW the generic one this test stops "
        "proving anything — it is the compound selector that must win, not "
        "the position"
    )
    rule = _rule(css, ".firmcol-logo.firmcol-logo--picked")
    assert "background: var(--accent)" in rule, rule
    assert "color: var(--on-accent)" in rule, (
        "the star sits ON the accent fill, so it takes the token measured "
        "against it — dark mode's accent is a LIGHT blue")
    assert "box-shadow: none" in rule, (
        "the generic tile's inset hairline reads as a monogram chip, which "
        "is the thing this tile exists not to look like")
