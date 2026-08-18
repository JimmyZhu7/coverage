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
