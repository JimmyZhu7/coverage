"""Every column header on the feed names its column on the same baseline.

`.firmcol-head` is a fixed 92px box and its comment promises the headers align
"regardless of how the name wraps or which pills show". A fixed box was only
half of that: `align-items: center` centred each id stack in it, so where a
name LANDED depended on what came after it. Picked-for-you emits its
`.firmcol-stats` row unconditionally and it holds nothing until a why-chip
exists, so that column's id block measured 42.2px against a firm's 61.4px and
its name rendered 9.6px lower than Morgan Stanley's beside it — measured live
on /opportunities/ at 1280px, with the logo tiles perfectly level in all
three columns because a 38px box centred in a 92px head lands identically
whatever its sibling does. A firm name wrapping to two lines would raise that
column's name by the same mechanism.

Anchoring the id stacks to the top of the head fixes both cases at once.
Measured after: all 13 columns report nameTop 553.2 in their row band (the
Picked column moved up 12.4px, the firm columns 2.8px), meta lines all at
573.2, logo tiles unmoved at 567.7, and a name forced to two lines still
starts at its neighbours' y.

Not the fix of dropping the empty stats div: removing that node live moved
the name DOWN a further 1.5px (567.6 -> 569.1), because its margin-top and
the flex gap it consumes are partly compensating today. `:empty` would not
have matched it either — the div holds a whitespace text node.
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


def test_the_header_anchors_its_names_to_the_top_not_to_their_own_centre():
    css = _feed_css()
    rule = _rule(css, ".firmcol-head")
    assert "align-items: flex-start" in rule, rule
    assert "align-items: center" not in rule, (
        "centring makes each name's position depend on how tall the stack "
        "under it happens to be, which is the defect"
    )
    assert "min-height: 92px" in rule, "the fixed header height is the other half"


def test_the_logo_tile_keeps_its_own_centring():
    """The tiles were already level; the fix must not move them."""
    assert "align-self: center" in _rule(_feed_css(), ".firmcol-logo")


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


def test_the_picked_column_really_does_render_a_shorter_id_stack(feed_with_both_columns):
    """The condition that made centring fail, asserted on real markup.

    Not a hypothetical: with no why-chips to show, Picked's stats row is
    present and empty while every firm column's carries at least a tier.
    """
    html = feed_with_both_columns.get("/opportunities/").content.decode()
    html = _STYLE_RE.sub("", html)

    stats = re.findall(r'<div class="firmcol-stats">(.*?)</div>', html, re.S)
    assert len(stats) >= 2, f"expected the Picked column and a firm one, got {len(stats)}"
    assert stats[0].strip() == "", (
        "the Picked column is expected to render an EMPTY stats row here — if "
        "it now carries chips, this test is no longer exercising the failure"
    )
    assert "firmcol-tier" in stats[1], "a firm column should carry its tier pill"
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
