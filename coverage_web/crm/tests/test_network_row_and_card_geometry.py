"""Two geometry defects on the Network board, both reported by eye.

"looks weird, refine design with opus" about the Coverage Gaps ledger, and
"why are card sizes not standardized, fix" about the contact grid. Both were
measured before and after with headless Playwright against the demo board
(`demo@coverage.local`, 49 contacts, 6 gap rows) at 1280x800, 375x812 and
three widths between, in both colour schemes.

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
    """A board with one tiered, uncovered firm, so the Coverage Gaps strip
    actually renders a row. `_page()` above has neither, which is fine for
    reading the stylesheet and useless for reading the markup."""
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

    Anchored to the start of a line on purpose: `.gap-act` also appears
    inside `.gap-card > .gap-act { grid-column: 5; }`, and an unanchored
    search reads that one-declaration placement rule as the whole button.
    """
    match = re.search(
        rf"^[ \t]*{re.escape(selector)} \{{(.*?)\}}", css, re.S | re.M
    )
    assert match, f"{selector} is gone from the Network page; re-check this guard"
    return match.group(1)


# ---------------------------------------------------------------------------
# Coverage Gaps: six rows, one set of columns
# ---------------------------------------------------------------------------
def test_the_gap_rows_share_one_set_of_column_tracks():
    """The ledger's own CSS comment claimed the columns "line up across all
    six rows". They did not: `.gap-row` was a flex column and every
    `.gap-card` was its own grid, so each `auto` track was sized against one
    row's content. Measured at 1280px with the six verbs the live board
    produces (four "Add Contact", two "Talk to <name>"):

        tier tag x     762.2 / 815.5 / 830.6 / 856.6
        gap state x    845.9 / 861 / 887
        "Who to find"  1005.9 / 1021 / 1047
        button width   98 / 124 / 139

    Four left edges for a column one word wide, and three button widths under
    one shared right edge, which is the ragged action end that got the strip
    flagged. After: one value for each of those four, at 1280, 1600, 900, 700
    and 375. `subgrid` is what does it, so this guard is on the `@supports`
    block rather than on any measured width.
    """
    css = _styles(_page())
    block = re.search(
        r"@supports \(grid-template-columns: subgrid\) \{(.*?)\n  \}", css, re.S
    )
    assert block, (
        "the subgrid block is gone. Without it every row sizes its own "
        "columns again and the tier tag lands at four different x positions."
    )
    body = block.group(1)
    assert ".gap-row { display: grid;" in body, (
        "the row surface no longer owns the tracks, so there is nothing for "
        "the rows to share."
    )
    assert "grid-template-columns: subgrid" in body, (
        "the rows stopped subscribing to the shared tracks."
    )
    assert "grid-column: 1 / -1" in body, (
        "a row that does not span every track cannot share them."
    )


def test_the_gap_rows_keep_a_working_fallback_without_subgrid():
    """The shared tracks are an upgrade, not a floor. Outside `@supports` the
    strip is still a flex column of rows, each laying out its own five
    columns, which is what shipped on 2026-09-01 and what
    `test_the_gap_strip_is_one_ledger_not_six_boxes` pins.
    """
    css = _styles(_page())
    assert "flex-direction: column" in _rule(css, ".gap-row")
    assert "grid-template-columns" in _rule(css, ".gap-card")


def test_the_gap_rows_slack_belongs_to_no_zone():
    """UPDATED 2026-09-02; the same defect was reported twice and the second
    report is what this now pins.

    The `1fr` started on column 1, so at 1280px the firm name sat at the left
    edge and the four zones that explain it were shoved against the right
    one: "HSBC" ended at x=90 and its tier tag started at x=762. Moving it to
    the state column fixed the reading order and kept the hole — `.gap-state`
    became a 739px box holding two words, so "No contacts" ended at x=380 and
    the next thing on the row began at x=1047. Reported again as "the empty
    stretch between the status phrase and the controls".

    No zone carries it now. Five `auto` columns hold the four facts and the
    disclosure at their own widths, and an empty fifth track takes the slack:
    measured at 1280px, name x=41, tier x=164.4, state x=289.2, "Who to find"
    x=413.5, and the button alone at the far end, x=1141.2.
    """
    css = _styles(_page())
    template = _rule(css, ".gap-card")
    columns = re.search(r"grid-template-columns:\s*([^;]+);", template)
    assert columns, f"the row no longer states its columns: {template.strip()}"
    tracks = columns.group(1)
    assert not tracks.strip().startswith("minmax(0, 1fr)"), (
        "the flexible track is back on the firm name, which pushes every "
        "other zone to the right edge of the row."
    )
    assert "1fr" in tracks, (
        "no track absorbs the row's slack, so the action end no longer sits "
        "at the right edge of the ledger."
    )
    # Six tracks for five zones: the flexible one is track 5 and nothing is
    # placed into it. A `1fr` that any zone sits in is the stranded-box bug
    # again, wherever it moves to.
    placed = re.findall(r"\.gap-card > \.[\w-]+ \{ grid-column: (\d+); \}", css)
    assert "5" not in placed, (
        f"a zone was placed into the gutter track: {placed}"
    )
    assert "6" in placed, "nothing sits past the gutter, so there is no action end"


def test_every_gap_row_button_is_one_width():
    """Six verbs, three widths: "Add Contact" measured 98px, "Talk to Nick
    Tehle" 124px and "Talk to Grace Huang" 139px, all right-aligned to the
    same edge, so their left edges were 41px apart. Sharing the track sizes
    the column to the widest verb; stretching into it makes all six 139px.
    Nothing is truncated to get there, which is why the ellipsis and the
    14rem cap are still the last resort and not the mechanism.
    """
    act = _rule(_styles(_page()), ".gap-act")
    assert "justify-self: stretch" in act, (
        "the buttons size themselves again, so the action column has six "
        "different left edges under one right edge."
    )
    assert "max-width: 14rem" in act, (
        "the last-resort cap on a very long lever name is gone."
    )


def test_the_tier_rail_is_gone_and_tier_still_has_two_channels():
    """REWRITTEN 2026-09-02; the rail it defended is the thing that was cut.

    It read `test_the_tier_rail_is_not_cut_between_rows` and pinned a
    continuous 3px tier-coloured left edge, after a `border-top` separator
    was found mitring against it and cutting the rail into one slab per row.
    That fix was correct and the founder rejected the result anyway: shown
    the continuous version he said six identical coral edges read as a wall,
    not a signal. The measurement agrees — `rank_gaps` weights tier heaviest,
    so the top of this strip is T1 on any board that has T1 firms, and all
    six rows measured `3px rgb(157, 43, 35)` on the demo board.

    Tier keeps two channels, which is the rule that was actually load-bearing
    and is what this test now pins: the text tag says "T1" and `.gap-t1`
    raises that tag's weight. Neither is a rail, and red is left meaning one
    thing on this strip — a deadline is close.

    The hairline between rows stays a pseudo-element. There is no edge left
    for a `border-top` to mitre against, but an inset rule is what every
    other ledger surface on this page draws.
    """
    css = _styles(_page())
    card = _rule(css, ".gap-card")
    assert "border-left" not in card, (
        "the tier rail is back on the row; six rows of one tier draw it as a "
        "single coloured bar down the strip."
    )
    assert "border-top" not in card, (
        "the row separator is a border again, and a border-top on a row is "
        "how the mitre bug got in the first time."
    )
    assert ".gap-card + .gap-card::after {" in css, (
        "nothing draws the hairline between rows any more."
    )
    assert ".gap-t1 .gap-tier-tag {" in css, (
        "tier lost its second channel; the tag is the only place tier "
        "appears now, so it is the only place a contrast step can live."
    )
    row = _page_with_a_gap()
    assert '<span class="gap-tier-tag">T1</span>' in row, (
        "the tier tag stopped printing the tier in words, which is the "
        "channel that has to survive however quiet the other one gets."
    )
    assert 'gap-card kin-reveal gap-t1' in row, (
        "the row lost the tier class, so the contrast step has nothing to "
        "hang off."
    )


def test_the_row_sits_its_text_on_one_baseline():
    """Three type sizes on one line — `.gap-name` bold at --fs-s, the mono
    `.gap-tier-tag` at --fs-xs and `.gap-state` at --fs-s — were each
    centred inside their own box, which left their baselines a pixel or two
    apart on every row. The two controls opt back out: a button centres
    against the line rather than sitting on it.

    The state's size changed 2026-09-02 (--fs-xs to --fs-s, see
    `test_the_state_phrase_outranks_the_tier_tag`), which makes the baseline
    rule matter more than it did, not less: two sizes on a line is what the
    rule is for and there are now three genuinely different ones.
    """
    css = _styles(_page())
    assert "align-items: baseline" in _rule(css, ".gap-card")
    assert "align-self: center" in _rule(css, ".gap-act")
    assert "align-self: center" in _rule(css, ".gap-card .src")


def test_the_state_phrase_outranks_the_tier_tag():
    """Reported by eye with the rest of this strip: the tier tag and the gap
    state read as equals. Measured before, they were: both `--fs-xs`, both
    `--ink-3`, and the TAG the heavier of the two at `--w-med` against
    regular. So a row's rank looked more important than its reason, and "No
    contacts" — the one phrase that differs row to row — read as a second
    label rather than as the answer to "why is this firm here".

    Three separations now, all the same way round and all on every row: size
    (13px against 12px), colour (`--ink-2` against `--ink-3`) and face (the UI
    face against the tag's mono). The state's weight stays regular, which is
    what keeps it a step behind the bold firm name.

    The tag's colour is flat across tiers on purpose. A first attempt ranked
    tier by ink and put T1 on `--ink-2` — the state's own colour — so on a T1
    row, which is most rows on this strip, the separation this test is about
    disappeared. Tier ranks by weight instead
    (`test_the_tier_rail_is_gone_and_tier_still_has_two_channels`), which is
    the one channel the state phrase is not using.
    """
    css = _styles(_page())
    state = _rule(css, ".gap-state")
    tag = _rule(css, ".gap-tier-tag")
    assert "font-size: var(--fs-s)" in state and "color: var(--ink-2)" in state
    assert "font-size: var(--fs-xs)" in tag and "color: var(--ink-3)" in tag
    assert "font-family: var(--font-mono)" in tag
    assert "font-weight" not in state, (
        "the state took a weight, which puts it back in a shouting match "
        "with the firm name above it"
    )
    assert "color" not in _rule(css, ".gap-t1 .gap-tier-tag"), (
        "tier is ranking by ink again, which lands T1 on the state phrase's "
        "own colour and erases the separation above"
    )


def test_the_who_to_find_rule_stops_competing_with_the_button():
    """REWRITTEN 2026-09-02; quieting the rule was not enough and the same
    thing was reported twice.

    The rule was dashed at `--line-strong`, which gave the row two control
    shapes one 16px gap apart. It was quieted to dotted `--line` and flagged
    again: any permanent horizontal rule under text sitting beside a bordered
    box is still a box next to a box.

    Two fixes replace it and this pins both. The row's flexible gutter now
    falls BETWEEN the disclosure and the verb (measured at 1280px: the
    toggle at x=413.5, the button at x=1141.2, 649.5px of gutter between
    them, against 16px before), so they are not neighbours at all. And the
    resting rule is transparent, leaving the "this opens" job to the ↓ glyph,
    which says more than an underline did and is not a colour. Hover,
    keyboard focus and the open state bring a solid underline back in the
    text's own colour, which is when it should be the loudest thing there.
    """
    css = _styles(_page())
    toggle = _rule(css, ".src-toggle")
    assert "border-bottom: 1px solid transparent" in toggle, (
        "the resting rule under 'Who to find' is back; it is the second "
        "control shape this strip was flagged for twice."
    )
    assert "border-bottom-color: currentColor" in css, (
        "the underline no longer comes back on hover, focus or open, which "
        "is the one moment it earns its place."
    )
    assert ".src-toggle:focus-visible" in css, (
        "the underline no longer answers the keyboard, only the mouse."
    )
    assert 'content: " ↓"' in css, (
        "the caret is the whole resting affordance now, and it is the "
        "non-colour half of the signal."
    )
    assert 'details[open] > .src-toggle::after { content: " ↑"; }' in css, (
        "the caret stopped reporting the open state."
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
