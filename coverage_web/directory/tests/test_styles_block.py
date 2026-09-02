"""Guard: no Django template comment may leak into a rendered <style> block.

Django's brace-hash comment syntax is SINGLE-LINE only. Written across more
than one line inside a `<style>` block it is NOT stripped — it renders
literally into the stylesheet, the CSS parser hits the stray brace, gives up,
and discards every rule after it.

That is not hypothetical. It shipped: two such comments in
`templates/directory/_styles.html` silently killed 103 of the file's 185
rules, including `.rolecard` and `.firmcols`. The feed unwrapped into
unstyled blocks — while the top nav still looked perfectly fine, because its
styles live in `static/css/coverage.css` and had already parsed. Nothing
failed, no error was logged, and the page returned 200. The only symptom was
visual, on a page no test rendered.

These tests assert the rendered output, not the template source, so they
catch the leak however it gets in.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db

# The rendered pages that carry their own <style> block.
PAGES = ["/opportunities/"]

_STYLE_RE = re.compile(r"<style>(.*?)</style>", re.S)


def _style_blocks(path: str) -> list[str]:
    html = Client().get(path).content.decode()
    return _STYLE_RE.findall(html)


@pytest.mark.parametrize("path", PAGES)
def test_no_django_comment_leaks_into_a_style_block(path):
    """A leaked `{#` is the poison: everything after it is discarded."""
    for block in _style_blocks(path):
        assert "{#" not in block, (
            f"A Django template comment leaked into {path}'s <style> block. "
            "Use a CSS comment instead — the CSS parser stops at the stray "
            "brace and silently drops every rule that follows."
        )


@pytest.mark.parametrize("path", PAGES)
def test_style_block_braces_balance(path):
    """Balanced braces are the cheap proxy for 'a parser can read all of it'.
    An unbalanced block means some rules are unreachable even without a
    leaked template tag."""
    for block in _style_blocks(path):
        opens, closes = block.count("{"), block.count("}")
        assert opens == closes, (
            f"{path}'s <style> block has {opens} '{{' and {closes} '}}'. "
            "Unbalanced braces mean the parser cannot reach every rule."
        )


def test_the_feeds_core_layout_rules_survive_rendering():
    """The specific rules whose loss produced the incident. They live late in
    the file, so they are the first casualties of any early parse break —
    which makes them the canary worth naming explicitly.

    `.rolecard` was the canary before the 2026-08-30 row redesign retired
    it; `.rolerow` is the rule that replaced it and lives in the same late
    part of the file, so it keeps the same canary job.

    `.fuse-passed` was one of the four until 2026-09-01, when the whole fuse
    block was deleted as dead (no template has emitted `.fuse-fill` since
    the row redesign). `.rolling-dot` replaces it: same position in the file,
    a couple of lines below where the fuse used to sit, so the early-break
    canary still fires from the same place. Swapped rather than dropped —
    four canaries spread down the file is the point, and three would leave
    the top third of the block unwatched."""
    blocks = _style_blocks("/opportunities/")
    assert blocks, "the feed should render its own <style> block"
    css = "\n".join(blocks)
    for selector in (".rolerow {", ".firmcols", ".rolling-dot", ".recbar"):
        assert selector in css, f"{selector} missing from the feed's rendered CSS"


# ---------------------------------------------------------------------------
# Role-row sizing.
# ---------------------------------------------------------------------------
# The feed's cards went through three states before the 2026-08-30 redesign:
# a fixed height that CLIPPED (190px of content in a 120px box), a min-height
# that stopped the clipping but let them take SEVEN distinct heights, and a
# fixed height with every slot reserved and clamped. That third state is what
# `.rolecard` was, and the two tests it needed — "the box is fixed" and
# "every variable slot inside it is bounded" — no longer describe anything:
# `.rolerow` is a FLOOR (min-height, not height), see this file's own header
# comment and `_rolecard.html`'s. There is no fixed box left to clip against,
# so a slot can no longer overflow a box that does not exist — the whole
# category of bug these two tests existed to catch.
#
# What replaced it is below: the row still must not silently reintroduce a
# fixed box (that is how the old clipping bug came back), and the
# `content-visibility` placeholder that stands in for an unrendered row
# still must not under-guess the row's real height — that exact mistake
# shipped twice during this file's own history (see the comments on
# `.rolerow`'s `content-visibility` rule and its `(pointer: coarse)` block)
# and clipped the meta line off every offscreen row both times.


def _feed_css() -> str:
    """The FEED's own <style> block, not every block on the page.

    It used to be all three joined, and that broke the moment the base
    template grew a `@media (pointer: coarse)` rule of its own for the site
    footer: `_coarse_block` below takes the FIRST such block in the joined
    string, which is the footer's 224-character one, and every assertion
    about `.rolerow`'s touch behaviour then failed with "no rule found" —
    a true statement about the wrong block. The failure looked like the
    stylesheet had lost the rule; it had not.

    Selecting by content rather than by index because the block ORDER is the
    base template's business and this file has no business pinning it: the
    feed's block is the one that defines the row, so ask for that.
    """
    blocks = _style_blocks("/opportunities/")
    assert blocks, "the feed should render its own <style> block"
    feed = [b for b in blocks if re.search(r"^\s*\.rolerow\s*\{", b, re.M)]
    assert feed, (
        "no rendered <style> block defines `.rolerow` — the feed's own block "
        "is missing or its late rules were dropped by a parse break."
    )
    return "\n".join(feed)


def _rule(css: str, selector: str) -> str:
    """The declaration body of `selector`'s own rule.

    Anchored to the start of a line, which is what makes it the rule for that
    selector rather than for any COMPOUND selector ending in it. Splitting on
    the bare string matched `.firmcol--picked .rolecard {` — a one-declaration
    override that happens to sit earlier in the file — and cheerfully reported
    that the role card had no height. Descendant selectors are indented; the
    rules these tests are about start their own line.
    """
    m = re.search(r"^\s*" + re.escape(selector) + r"\s*\{(.*?)\}", css, re.S | re.M)
    assert m, f"no rule found for {selector}"
    return m.group(1)


def _rules(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) for every flat rule in the stylesheet. Used
    where the assertion is about how MANY rules do something rather than
    what one named rule says — counting is what makes "exactly one
    truncation point" testable instead of merely commented.

    COMMENTS ARE STRIPPED FIRST. This stylesheet explains itself at length
    directly above the rule being explained, and a naive `[^{}]+\\{` reads
    that whole comment as part of the selector — so `.rr-loc`, which carries
    a ten-line note, came back as "/* ... */ .rr-loc" and matched nothing.
    Same class of bug as `_STYLE_RE` in the honesty tests: this file's own
    prose is inside the string under test."""
    flat = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [
        (m.group(1).strip(), m.group(2))
        for m in re.finditer(r"([^{}@]+)\{([^{}]*)\}", flat)
    ]


def _rule_all(css: str, selector: str) -> str:
    """Every rule literally selecting `selector` (not a compound or pseudo
    selector built on it), concatenated in source order. `.rolerow` gets two
    separate rules in the file — the layout declaration and, later, the
    `content-visibility` one — and the cascade applies both, so a single-match
    `_rule` would silently miss whichever properties live in the second."""
    bodies = [m.group(1) for m in re.finditer(
        r"^\s*" + re.escape(selector) + r"\s*\{(.*?)\}", css, re.S | re.M)]
    assert bodies, f"no rule found for {selector}"
    return "\n".join(bodies)


def _coarse_block(css: str) -> str:
    """Every `@media (pointer: coarse) { ... }` body on the page, joined.

    Brace-counted, so a nested rule inside one does not truncate the match.

    ALL of them, not the first. This returned the first until 2026-09-02,
    which was the feed's own while the feed was the only source of one. The
    UI pass then gave the shell a coarse block of its own, earlier in the
    document, and the guards below started reading a stylesheet with no
    `.rolerow` in it and reporting that the row rule was gone. What the page
    does on a touch screen is the union of its coarse rules, wherever they
    were written, so read the union."""
    bodies = []
    for m in re.finditer(r"@media\s*\(\s*pointer:\s*coarse\s*\)\s*\{", css):
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        bodies.append(css[m.end():i - 1])
    assert bodies, "no (pointer: coarse) block in the feed's rendered CSS"
    return "\n".join(bodies)


def test_the_role_row_never_pins_a_fixed_height():
    """The opposite contract from the card's. A fixed `height` is exactly
    what clipped the card's content twice; the row's whole design is to
    stay a natural, `min-height`-at-most box so nothing it holds can ever
    be cut off by its own container. `.rolerow` itself declares neither —
    row height falls out of its content — but this also guards against the
    fixed-height bug being reintroduced by a future edit."""
    css = _feed_css()
    rule = _rule_all(css, ".rolerow") + "\n" + _rule(_coarse_block(css), ".rolerow")
    assert not re.search(r"(?<!min-)(?<!-)height:\s*\d", rule), (
        f"`.rolerow` declares a fixed height somewhere ({rule.strip()!r}), "
        "which is the exact defect this row replaced the card to fix — a "
        "taller control or a two-line title now has nothing to overflow into."
    )
    # Load-bearing per the rule's own comment: `.firmcol-scroll` is a fixed-
    # height flex column, and a row left at the default `flex-shrink: 1` is
    # compressed below its own content — clipping the meta line (location,
    # the visa verdict) off every row. Dropping this in a first pass did
    # exactly that.
    assert "flex: none" in rule, (
        "`.rolerow` no longer declares `flex: none`, so its fixed-height "
        "flex-column parent can compress it below its own content again."
    )


def test_the_offscreen_placeholder_never_guesses_a_bare_number():
    """The row's `content-visibility: auto` placeholder — `contain-intrinsic-
    size` — stands in for every unrendered row until it has painted once. A
    BARE guessed number here is the bug that shipped twice on this exact
    rule: 56px against ~88px of real content in the base case, then 81px
    against 100px once touch wrapped the controls onto their own line — both
    times it silently clipped the meta line off the page. `auto` is what
    stops that: it tells the browser to substitute the row's own last real
    size once one has rendered, rather than trusting the guess forever."""
    css = _feed_css()
    base_rule = _rule_all(css, ".rolerow")
    coarse_rule = _rule(_coarse_block(css), ".rolerow")
    for rule, label in ((base_rule, ".rolerow"),
                        (coarse_rule, "the (pointer: coarse) block")):
        m = re.search(r"contain-intrinsic-size:\s*([^;]+);", rule)
        assert m, f"{label} declares no contain-intrinsic-size"
        assert m.group(1).strip().startswith("auto "), (
            f"contain-intrinsic-size is {m.group(1).strip()!r}, a bare "
            "number with no `auto` fallback — this is the exact regression "
            "that clipped every offscreen row's meta line."
        )
    # The touch placeholder must be at least as tall as the base one: the
    # controls wrap onto their own line under `(pointer: coarse)` (see
    # `.rr-act`'s comment), which costs the row a whole extra line, so a
    # placeholder that did not grow to match would clip that line again.
    base_px = float(re.search(r"auto\s+(\d+(?:\.\d+)?)px",
                              re.search(r"contain-intrinsic-size:\s*([^;]+);",
                                        base_rule).group(1)).group(1))
    coarse_px = float(re.search(r"auto\s+(\d+(?:\.\d+)?)px",
                                re.search(r"contain-intrinsic-size:\s*([^;]+);",
                                          coarse_rule).group(1)).group(1))
    assert coarse_px >= base_px, (
        f"the touch placeholder ({coarse_px}px) is shorter than the base "
        f"one ({base_px}px), but touch controls take an extra line, not less."
    )


def test_the_title_is_clamped_to_two_lines():
    """A scraped job title is unbounded. The row has no fixed box left for
    it to overflow, but an unclamped title could still grow to any length
    and dominate the row — the clamp is what keeps title height predictable
    regardless of how long the scraped text is."""
    css = _feed_css()
    rule = _rule(css, ".rr-title a")
    assert "-webkit-line-clamp: 2" in rule
    assert "overflow: hidden" in rule


@pytest.fixture
def two_roles(db):
    """One rolling role and one with a real deadline — the two card shapes.

    Both are needed: the whole point of the redesign is that a dated card and
    a rolling one measure identically, which is only testable with one of each.
    """
    from datetime import timedelta

    from django.utils import timezone

    from directory.models import Firm, Opportunity

    firm = Firm.objects.create(slug="evercore", name="Evercore", tracks=["ib"])
    rolling = Opportunity.objects.create(
        firm=firm, url="https://x.test/1", title="2027 Summer Analyst Programme",
        bucket="internship", cohort="2027", status="open", region="us",
        location="New York",
    )
    dated = Opportunity.objects.create(
        firm=firm, url="https://x.test/2", title="Insight Evening",
        bucket="insight", status="open", region="us", location="London",
        deadline=timezone.localdate() + timedelta(days=6),
        confidence=1.0,
    )
    return rolling, dated


def _markup(client) -> str:
    """The feed with its `<style>` block removed.

    Counting class names across the whole response counts the stylesheet's own
    selectors too — `.rolecard-meta {` is not a card. Strip the CSS and what
    is left is markup, so a count means what it says.
    """
    html = client.get("/opportunities/").content.decode()
    return _STYLE_RE.sub("", html)


def test_first_seen_is_stated_once_and_only_where_it_is_the_answer(
        client, two_roles):
    """It used to render as a top-row badge AND in the tag below — the same
    fact twice on 857 of 879 cards. Asserted against the card MARKUP, with
    real rows on the page: an empty feed would pass this vacuously.

    It is now also stated only on the UNDATED card. On a dated one the
    countdown answers "how long has this been here" better and about the role
    rather than about our scraper, and 288 of 2,599 live feed cards were
    printing "Closes in 3 days · first seen 35d ago" — two time facts in one
    grey, competing."""
    html = _markup(client)

    rows = html.count('class="rolerow ')
    assert rows == 2, f"fixture should render two rows, got {rows}"

    # Retired duplicates, gone from markup AND stylesheet.
    assert "fresh-badge" not in html, "the retired duplicate badge is back"
    assert "rolling-tag" not in html, "the retired duplicate tag is back"

    # One meta line per row; the age on the undated one only, and once.
    assert html.count('class="rr-meta"') == rows
    assert html.count("first seen") == 1
    # The dated row's due column carries a real countdown figure instead of
    # the retired fuse bar — see `_rolecard.html`'s header comment on why
    # urgency moved into that figure's colour.
    assert html.count("rr-due-n meta-") == 1, "one dated card in the fixture"


def test_the_card_carries_every_fact_the_feed_promises(client, two_roles):
    """Programme year, location, first-seen, and a countdown on the dated role
    but not the rolling one."""
    html = _markup(client)

    # Role type + programme year: no longer a pill (`.role-mini` retired with
    # the card), now plain text in `.rr-kind` — see `_rolecard.html`'s header
    # comment on the pills being gone.
    assert re.search(r'<span class="rr-kind"[^>]*>[^<]*2027', html), (
        "the programme year should ride in the row's kind label"
    )
    assert "New York" in html and "London" in html, "location"
    assert "first seen" in html, "provenance"
    # NOT "Rolling": that word is now reserved for postings whose own text
    # states rolling review. A role with no deadline in the data says the
    # true thing instead, because nobody posted a date.
    assert "No date posted" in html, "the undated role says what is known"
    # The countdown figure belongs to the dated role only.
    assert html.count("rr-due-n meta-") == 1


# ---------------------------------------------------------------------------
# The Companies panel must stay inside the viewport.
#
# It is a fixed 520px anchored to its button's LEFT edge, and that button is
# the last control in the filter bar — so at every width that does not happen
# to leave 520px to its right, the panel ran off the screen. Reported from a
# real window; reproduced at 820px, where it overflowed by 43px.
#
# The clamp is JS (no CSS can know how far a button sits from the right edge),
# so these are static guards: they cannot prove the arithmetic, but they do
# stop the mechanism being removed or quietly disconnected.
# ---------------------------------------------------------------------------

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BASE = _ROOT / "templates" / "base.html"
_OPPS = _ROOT / "templates" / "directory" / "opportunities.html"
_CSS = _ROOT / "static" / "css" / "coverage.css"


def test_the_viewport_clamp_helper_exists_and_both_dropdowns_call_it():
    base = _BASE.read_text()
    assert "window.covKeepInViewport = function" in base
    # The single-select dropdowns (Year / Region / Track / Sponsorship) share
    # the helper — they are narrower and overflow less often, not never.
    # Called on the floating PANEL, not the `<ul>` menu inside it — a large
    # select's optional search input lives in that same panel, above the
    # menu, and only clamping the menu would leave the search box (and the
    # panel's own border) hanging off the edge of the screen it was
    # supposed to be clamped inside of.
    assert base.count("covKeepInViewport(panel)") >= 2, "csel must call the clamp"
    assert "covKeepInViewport(menu)" in _OPPS.read_text(), "Companies must call it"


def _clamp_code() -> str:
    """The clamp's body with `//` comments stripped.

    Stripping matters: the comments deliberately NAME the wrong APIs to
    explain why they are wrong, so a naive substring check on the raw text
    fails against correct code. An assertion that reads prose is testing the
    documentation, not the behaviour."""
    body = _BASE.read_text()
    fn = body[body.index("window.covKeepInViewport"):]
    fn = fn[:fn.index("};")]
    return "\n".join(line.split("//")[0] for line in fn.splitlines())


def test_the_clamp_measures_layout_metrics_not_client_rects():
    """The open animation starts at `scale(0.98)` and the clamp runs on the
    frame the panel is unhidden, so a client rect reads the panel ~2% small —
    about 10px on a 520px menu, exactly enough to conclude it fits when it
    does not. offsetLeft/offsetWidth ignore transforms."""
    code = _clamp_code()
    assert "offsetWidth" in code and "offsetLeft" in code
    assert "getBoundingClientRect().left" in code  # only to locate the anchor
    # The failure this replaced: measuring the panel's own transformed box.
    assert "menu.getBoundingClientRect()" not in code


def test_the_clamp_uses_the_layout_viewport_not_innerwidth():
    """`window.innerWidth` includes the vertical scrollbar, so clamping to it
    parks the panel's last ~15px underneath the scrollbar."""
    code = _clamp_code()
    assert "document.documentElement.clientWidth" in code
    assert "innerWidth" not in code


def test_the_panel_can_never_be_wider_than_the_screen():
    css = _CSS.read_text()
    block = css[css.index(".cmulti-menu {"):]
    block = block[:block.index("}")]
    # Viewport-relative, and matching the clamp's 12px gutter on both sides.
    assert "max-width: calc(100dvw - 24px)" in block
    assert "92vw" not in block


# ---------------------------------------------------------------------------
# The fact-chip ellipsis (2026-08-10). `.fact-chip` caps long labels at 18ch
# with `overflow: hidden; text-overflow: ellipsis`, but it was ALSO
# `display: inline-flex` — and `text-overflow` is defined only for block
# containers; a flex container ignores it with no warning. Longer labels
# were hard-clipped mid-character with no "…" to mark the cut: "Won't
# sponsor you here" silently lost "here", read by the reader as the text
# running straight into the chip's own border.
#
# A second, compounding bug: the rule had no `white-space: nowrap`, so a
# multi-word label ("Won't sponsor you here") could WRAP onto a second line
# inside the fixed-height box instead of truncating on one — which is what
# the reported screenshot actually showed, text overrunning the chip
# vertically rather than a clean single-line ellipsis.
# ---------------------------------------------------------------------------


def _fact_chip_rule() -> str:
    css = _CSS.read_text()
    i = css.index(".fact-chip {")
    return css[i:css.index("}", i) + 1]


def test_fact_chip_is_not_a_flex_container():
    """`text-overflow: ellipsis` has no effect on flex/inline-flex containers
    per spec. Whatever `.fact-chip` is, it must not be one."""
    rule = _fact_chip_rule()
    assert "display: inline-flex" not in rule
    assert "display: flex" not in rule


def test_fact_chip_forces_single_line_before_it_ellipsizes():
    """All three preconditions for a working single-line ellipsis, in one
    place — losing any one silently reintroduces the bug."""
    rule = _fact_chip_rule()
    assert "overflow: hidden" in rule
    assert "text-overflow: ellipsis" in rule
    assert "white-space: nowrap" in rule


@pytest.mark.django_db
def test_a_long_verdict_label_renders_as_one_unwrapped_line(client):
    """Regression, end to end: the exact case reported — a visa-refusal chip
    long enough to hit the 18ch cap must render as a single text run the
    browser can ellipsize, not a wrapped multi-line block."""
    from accounts.models import User
    from directory.models import Firm, Opportunity

    u = User.objects.create_user(
        email="stu@example.com", password="x", class_year=2029,
        work_authorization={"hk": "sponsorship"})
    f = Firm.objects.create(slug="test-hk-bank", name="Test HK Bank")
    Opportunity.objects.create(
        firm=f, url="https://x/1", title="Analyst", bucket="internship",
        status="open", region="hk", sponsorship="no")
    client.force_login(u)
    resp = client.get("/opportunities/")
    assert b"Won&#x27;t sponsor you here" in resp.content or \
           b"Won't sponsor you here" in resp.content


# ---------------------------------------------------------------------------
# The chip cap is a LABEL budget, and it has to be able to pay for the box it
# is written on (2026-08-14).
#
# `box-sizing: border-box` is set file-wide, so `max-width: 18ch` was never
# 18 characters of label: 6px of padding and 1px of border on each side came
# out of the same 108px, leaving 94px — 15.7ch. Measured on the live feed at
# 1280px, 138 of 375 chips ellipsized under it, including "Portuguese needed",
# which is SEVENTEEN characters. Two of the misses were sub-pixel — "Your year
# (2029)" and "Penultimate year" need 94.41px against 94.00px — so they were
# invisible to any `scrollWidth > clientWidth` check while still rendering as
# "Your year (202…", a graduation year without its last digit.
#
# These tests do the arithmetic the comment used to assert by eye: they read
# the padding, border and cap straight out of the shipped rule, work out how
# much LABEL that leaves, and check it against the longest label the product's
# own code can produce — built by calling that code, not by quoting it.
# ---------------------------------------------------------------------------

# 1ch is the advance of "0", which in this face at font-size 10px is 6px —
# measured in Chrome on the live page (max-width 107.998px for the old 18ch).
_CH_PX = 6.0


def _chip_label_budget_ch() -> float:
    """How many characters of label `.fact-chip` actually delivers.

    Deliberately derived from the rule as written, so that a cap that forgets
    the border-box tax reports the budget it really has (15.7ch for the old
    `max-width: 18ch`) rather than the one it claims.
    """
    rule = _fact_chip_rule()
    pad = float(re.search(r"padding:\s*[\d.]+px\s+([\d.]+)px", rule).group(1))
    border = float(re.search(r"border:\s*([\d.]+)px\s+solid", rule).group(1))
    chrome_px = 2 * (pad + border)

    decl = re.search(r"max-width:\s*([^;]+);", rule).group(1)
    ch = float(re.search(r"([\d.]+)ch", decl).group(1))
    # Everything the declaration adds back on top of the ch figure.
    added_px = sum(float(x) for x in re.findall(r"([\d.]+)px", decl))
    if "var(--chip-chrome)" in decl:
        added_px += float(re.search(r"--chip-chrome:\s*([\d.]+)px", rule).group(1))
    return (ch * _CH_PX + added_px - chrome_px) / _CH_PX


def test_the_chip_cap_declares_the_padding_and_border_it_has_to_pay_for():
    """If the box's chrome changes, the constant the cap adds back must change
    with it — otherwise the cap silently starts under-delivering again, which
    is exactly how it under-delivered for two revisions."""
    rule = _fact_chip_rule()
    pad = float(re.search(r"padding:\s*[\d.]+px\s+([\d.]+)px", rule).group(1))
    border = float(re.search(r"border:\s*([\d.]+)px\s+solid", rule).group(1))
    chrome = re.search(r"--chip-chrome:\s*([\d.]+)px", rule)
    assert chrome, "the cap must name the chrome it is paying for"
    assert float(chrome.group(1)) == 2 * (pad + border), (
        f"--chip-chrome says {chrome.group(1)}px but the box spends "
        f"{2 * (pad + border)}px on padding and border")


def test_the_chip_never_exceeds_the_row_it_sits_in():
    """The cap is now wider than a card column on a small phone, so it needs
    the second bound. Without it the widest chip decides the card's width."""
    decl = re.search(r"max-width:\s*([^;]+);", _fact_chip_rule()).group(1)
    assert "100%" in decl and "min(" in decl, decl


@pytest.mark.django_db
def test_every_label_the_product_can_build_fits_the_chip_cap():
    """The real failure mode: a label the code can produce, in a box that
    cannot hold it. The labels come from `_eligibility` and `_fact_chips`
    themselves — the same call the card template makes — so a new label that
    outgrows the cap fails here rather than on someone's screen.

    A monospace label of N characters is at most N ch wide (letter-spacing is
    negative), so comparing character counts against the ch budget is the
    conservative form of the comparison.
    """
    from directory.models import Firm, Opportunity
    from directory.views import _eligibility, _fact_chips

    f = Firm.objects.create(slug="budget-bank", name="Budget Bank")
    grad = {"value": "2027–2028", "years": [2027, 2028],
            "phrase": "Open to 2027 and 2028 graduates."}

    year_out = Opportunity(firm=f, url="https://x/1", title="Analyst",
                           bucket="internship", status="open",
                           raw={"facts": {"grad": grad}})
    year_ok = Opportunity(firm=f, url="https://x/2", title="Analyst",
                          bucket="internship", status="open",
                          raw={"facts": {"grad": grad}})
    visa_out = Opportunity(firm=f, url="https://x/3", title="Analyst",
                           bucket="internship", status="open",
                           region="hk", sponsorship="no")
    # The convention-derived verdict — the longest label of the four, because
    # it prefixes the year match with "Likely".
    likely = Opportunity(firm=f, url="https://x/4", title="Analyst",
                         bucket="internship", status="open",
                         class_year_derived=2029)

    labels = []
    for o, profile in (
        (year_out, {"class_year": 2026, "work_auth": {}}),
        (year_ok, {"class_year": 2027, "work_auth": {}}),
        (visa_out, {"class_year": 2029, "work_auth": {"hk": "sponsorship"}}),
        (likely, {"class_year": 2029, "work_auth": {}}),
    ):
        verdict = _eligibility(o, profile)
        assert verdict, "each fixture should produce the verdict it was built for"
        labels.append(verdict["label"])

    # …and the posting-fact chips whose labels are fixed strings.
    facts = Opportunity(firm=f, url="https://x/5", title="Analyst",
                        bucket="internship", status="open", sponsorship="no",
                        raw={"facts": {"grad": grad,
                                       "gpa": {"value": "3.5", "hedge": True,
                                               "phrase": "GPA 3.5 preferred."}}})
    labels += [c["label"] for c in _fact_chips(facts)]

    budget = _chip_label_budget_ch()
    worst = max(labels, key=len)
    assert len(worst) <= budget, (
        f"{worst!r} is {len(worst)} characters and the chip delivers "
        f"{budget:.2f}ch of label. Labels: {labels}")


def test_the_feed_row_has_exactly_one_truncation_point():
    """A first pass gave `.rr-fact` AND `.rr-loc` their own independent
    `flex-shrink: 1`, which meant a packed meta line — verdict, two facts,
    a market qualifier, the role type — showed as many as four separate
    mid-word ellipses, each fact reduced to a handful of characters. Live
    on the founder's own feed this read as scrambled, not truncated.

    ONE point is still the contract. WHICH point changed on 2026-08-31.

    It used to be `.rr-meta > *:last-child`, and "last" turned out to mean
    "whatever `_rolecard.html` happened to render last for this row" rather
    than "the least decisive fact". On an undated row that is `.rr-undated`
    — "No date posted, first seen 39d ago", the words the deliberately mute
    dash in `.rr-due` does not say. Measured live in dark on the founder's
    own board at 1512px, over all 790 rendered rows: 441 (55.8%) cut their
    last meta item mid-word, 150 (19.0%) cut it under 24px, and 129 (16.3%)
    overflowed with NO ellipsis at all because the one shrinkable item had
    already reached zero. At 375px it was 732 / 397 / 363 of 790.

    So the line wraps now, and the single cut is a NAMED one: `.rr-loc`,
    capped at a stated width. That is the part this stylesheet already
    designated as the compressible one ("a half-rendered year reads as a
    rendering fault, a truncated city does not"), and it is the only rule
    in the block allowed to say `text-overflow`. This test pins that
    uniqueness, which is the invariant the old assertions were reaching for
    through the `:last-child` implementation."""
    css = _feed_css()
    for cls in (".rr-firm", ".rr-vd", ".rr-fact", ".rr-loc", ".rr-kind", ".rr-cls"):
        rule = _rule(css, cls)
        assert "flex: none" in rule, (
            f"{cls} must hold its own width — a fixed-width part that "
            f"can still shrink reopens the multi-ellipsis bug")

    # The wrap is what makes a single cut point enough: without it, the
    # parts that cannot shrink simply overflow and get hard-clipped with no
    # ellipsis, which is the 129-row / 363-row defect above.
    meta = _rule(css, ".rr-meta")
    assert "flex-wrap: wrap" in meta, meta
    assert "white-space: nowrap" not in meta, (
        "nowrap on the CONTAINER is the one-line contract that clipped; each "
        "part keeps its own nowrap, the line of parts does not")

    # Exactly one rule in the row's meta line may truncate, and it is the
    # location. Asserted by counting, so adding a second one anywhere in the
    # block fails here rather than shipping four ellipses again.
    #
    # REWRITTEN 2026-09-01 to admit ONE more, by name: `.rr-why`, the
    # Picked column's per-card "why" line. It is not a part of the meta
    # line — it is a whole line of its own beneath it, `white-space:
    # nowrap`, cut once at the row's edge with the full sentences in its
    # `title` — so it cannot produce the four-ellipsis scramble this test
    # exists to prevent: a single nowrap line has exactly one place to
    # cut. The meta line's own contract (one part, the location) is
    # unchanged and still pinned below.
    truncators = sorted(
        sel for sel, body in _rules(css)
        if sel.startswith(".rr-") and "text-overflow" in body)
    assert truncators == [".rr-loc", ".rr-why"], (
        f"the meta line must have exactly one truncation point (the "
        f"location) and the row exactly one more (the whole why line); "
        f"found {truncators}")
    why = _rule(css, ".rr-why")
    assert "white-space: nowrap" in why, (
        ".rr-why is a whole line, not a part of one: it truncates as a "
        "single line or not at all")

    loc = _rule(css, ".rr-loc")
    assert "max-width" in loc, (
        "the location truncates at a STATED width — that is what keeps the "
        "cut in the same place down a column instead of at five different "
        "widths, which is how the old contract failed")
    assert "overflow: hidden" in loc and "text-overflow: ellipsis" in loc, loc

    # `:last-child` must not quietly come back as a second cut point.
    # Asserted against the comment-stripped source, because the rule that
    # replaced it NAMES the retired selector while explaining why it went —
    # the same trap `_STYLE_RE` exists for in the honesty tests, and this
    # assertion walked straight into it on the first run.
    assert not any(sel == ".rr-meta > *:last-child" for sel, _ in _rules(css)), (
        "positional truncation is what cut 'No date posted' to 5px; the cut "
        "point is named now")


# ---------------------------------------------------------------------------
# The abbreviated countdown must keep a full accessible name (2026-09-01).
# `directory/views.py` shortens "Closes in 12 days" to "12d" because the
# column is 44px, and its own comment promised "the full sentence stays the
# accessible name". It did not: the span shipped bare, so a screen reader
# read "22 d". A cross-surface consistency audit found it.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_abbreviated_countdown_still_reads_the_full_sentence_aloud(client):
    from directory.models import Firm, Opportunity
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    import datetime as dt

    User = get_user_model()
    user = User.objects.create_user(email="due-a11y@example.com", password="x")
    firm = Firm.objects.create(slug="due-a11y-co", name="Due Co", regions=["us"])
    Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship", status="open",
        url="https://due-a11y.example/1",
        deadline=timezone.localdate() + dt.timedelta(days=5),
    )
    client.force_login(user)
    body = client.get("/opportunities/").content.decode()

    # The eye gets the abbreviation, hidden from assistive tech.
    assert '<span aria-hidden="true">5d</span>' in body
    # The ear gets the sentence the comment promised.
    assert '<span class="vh">Closes in 5 days</span>' in body
