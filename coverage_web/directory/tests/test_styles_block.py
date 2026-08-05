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
