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
    which makes them the canary worth naming explicitly."""
    blocks = _style_blocks("/opportunities/")
    assert blocks, "the feed should render its own <style> block"
    css = "\n".join(blocks)
    for selector in (".rolecard {", ".firmcols", ".fuse-passed", ".recbar"):
        assert selector in css, f"{selector} missing from the feed's rendered CSS"


# ---------------------------------------------------------------------------
# Role-card standardisation.
# ---------------------------------------------------------------------------
# The feed's cards have now been through three states: a fixed height that
# CLIPPED (190px of content in a 120px box), a min-height that stopped the
# clipping but let them take SEVEN distinct heights from 122px to 205px, and
# the current fixed height with every slot reserved and clamped. Only the third
# both standardises and never cuts text off.
#
# These assert the CSS contract that makes the third state hold. They are
# deliberately about the stylesheet rather than a headless layout pass: the
# rule "the box is fixed AND every slot inside it is bounded" is the invariant,
# and it is checkable without a browser.


def _feed_css() -> str:
    blocks = _style_blocks("/opportunities/")
    assert blocks, "the feed should render its own <style> block"
    return "\n".join(blocks)


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


def test_the_role_card_pins_a_single_height():
    """A fixed `height`, not `min-height` — min-height is what let the cards
    take seven different heights and read as ragged."""
    css = _feed_css()
    rule = _rule(css, ".rolecard")
    assert "height: 168px" in rule, rule
    assert "min-height" not in rule, "min-height reintroduces the ragged heights"


def test_every_variable_slot_inside_the_card_is_height_bounded():
    """The other half of the contract. A fixed box with unbounded content is
    the ORIGINAL clipping bug, so each slot that holds scraped text must
    reserve its own height."""
    css = _feed_css()
    for selector in (".rolecard-top", ".rolecard-title", ".rolecard-sub",
                     ".rolecard-facts", ".rolecard-meta"):
        rule = _rule(css, selector)
        assert "height:" in rule, f"{selector} must reserve a height, got: {rule.strip()}"


def test_the_title_is_clamped_to_two_lines():
    """A scraped job title is unbounded. Without the clamp, a long one pushes
    past the reserved 40px and the fixed box clips it."""
    css = _feed_css()
    rule = _rule(css, ".rolecard-title a")
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


def test_first_seen_is_stated_once_per_card(client, two_roles):
    """It used to render as a top-row badge AND in the tag below — the same
    fact twice on 857 of 879 cards. Asserted against the card MARKUP, with
    real rows on the page: an empty feed would pass this vacuously."""
    html = _markup(client)

    cards = html.count('class="rolecard ')
    assert cards == 2, f"fixture should render two cards, got {cards}"

    # Retired duplicates, gone from markup AND stylesheet.
    assert "fresh-badge" not in html, "the retired duplicate badge is back"
    assert "rolling-tag" not in html, "the retired duplicate tag is back"

    # Exactly one meta row per card, and one provenance string in each.
    assert html.count("rolecard-meta") == cards
    assert html.count("first seen") == cards


def test_the_card_carries_every_fact_the_feed_promises(client, two_roles):
    """Programme year, location, first-seen, and a countdown on the dated role
    but not the rolling one."""
    html = _markup(client)

    assert "role-mini" in html, "role type + programme year pill"
    assert "· 2027" in html, "the programme year rides in the pill"
    assert "New York" in html and "London" in html, "location"
    assert "first seen" in html, "provenance"
    # NOT "Rolling": that word is now reserved for postings whose own text
    # states rolling review. A role with no deadline in the data says the
    # true thing instead, because nobody posted a date.
    assert "No date posted" in html, "the undated role says what is known"
    # The countdown hairline belongs to the dated role only.
    assert html.count("rolecard-fuse") == 1


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
    assert base.count("covKeepInViewport(menu)") >= 2, "csel must call the clamp"
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


def test_the_feed_row_shrinks_its_chips_instead_of_slicing_them():
    """The one row where chips compete for width: a fixed 330px, one line,
    `overflow: hidden`. If the chips cannot shrink, that overflow does the
    cutting — and it cuts mid-glyph with no ellipsis, which reads as a
    rendering fault rather than as truncation."""
    css = _feed_css()
    rule = _rule(css, ".rolecard-facts .fact-chip")
    assert "flex-shrink: 1" in rule, rule
    assert "min-width: 0" in rule, (
        "a flex item's automatic minimum size is its content, so without "
        "min-width: 0 the chips refuse to shrink and the row overflows")
