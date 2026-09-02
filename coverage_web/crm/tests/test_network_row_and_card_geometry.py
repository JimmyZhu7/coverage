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

from crm.models import Contact

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


def test_the_gap_rows_slack_is_not_in_front_of_the_tier_tag():
    """The `1fr` was on column 1, so at 1280px the firm name sat at the left
    edge and the four zones that explain it were shoved against the right
    one: "HSBC" ended at x=90 and its tier tag started at x=762. A reader
    crossed 670px of nothing to find out why the firm was on the list. The
    flexible track is the state column now, so the facts read off the name
    (name x=32, tier x=167, state x=292 on every row) and the gutter falls
    between the facts and the two controls.
    """
    template = _rule(_styles(_page()), ".gap-card")
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


def test_the_tier_rail_is_not_cut_between_rows():
    """A `border-top` meets a `border-left` in a mitre, so each row's 3px
    tier edge was clipped at 45 degrees top and bottom and a strip of six T1
    firms drew six separate red slabs. Reported as six equally urgent alarms
    rather than one graded signal. The separator is a pseudo-element inset
    past the edge now, so the rail runs unbroken and changes colour only
    where the tier changes.
    """
    css = _styles(_page())
    card = _rule(css, ".gap-card")
    assert "border-top" not in card, (
        "the row separator is a border again, which mitres the tier edge and "
        "cuts the rail into one slab per row."
    )
    assert "border-left: var(--edge-w)" in card, "the tier edge itself is gone"
    assert ".gap-card + .gap-card::after {" in css, (
        "nothing draws the hairline between rows any more."
    )


def test_the_row_sits_its_text_on_one_baseline():
    """Three type sizes on one line — `.gap-name` bold at --fs-s, the mono
    `.gap-tier-tag` at --fs-xs and `.gap-state` at --fs-xs — were each
    centred inside their own box, which left their baselines a pixel or two
    apart on every row. The two controls opt back out: a button centres
    against the line rather than sitting on it.
    """
    css = _styles(_page())
    assert "align-items: baseline" in _rule(css, ".gap-card")
    assert "align-self: center" in _rule(css, ".gap-act")
    assert "align-self: center" in _rule(css, ".gap-card .src")


def test_the_who_to_find_rule_stops_competing_with_the_button():
    """A permanent dashed underline at `--line-strong` gave the row two
    control shapes one 16px gap apart, and the disclosure read as loudly as
    the button beside it. Dotted at `--line` keeps the affordance a
    disclosure needs at rest and gives the glance back to the verb; hover,
    keyboard focus and the open state all take it to `--ink-3`.
    """
    css = _styles(_page())
    toggle = _rule(css, ".src-toggle")
    assert "border-bottom: 1px dotted var(--line)" in toggle, (
        "the resting rule under 'Who to find' is loud again."
    )
    assert ".src-toggle:focus-visible" in css, (
        "the underline no longer answers the keyboard, only the mouse."
    )
    assert 'content: " ↓"' in css, (
        "the caret is the non-colour half of this affordance and has to stay."
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
