"""Pricing page: every number must match what the app actually enforces.

`core.views.pricing`'s advisor-cap figures used to read
`assistant.plans.limits_for(...).daily_cap` — a number that stopped gating
anything the moment the credit system (billing/credits.py) took over
turn-by-turn enforcement. `assistant/plans.py`'s own docstring says so in
plain words: "It no longer gates anything in agent.py". The real per-day
ceiling a student hits is `billing_credits.can_spend`'s daily burst guard,
which blocks once `daily_spent(user) >= plan['daily_burst']` — i.e. a plan
can send at most `daily_burst // message_cost` messages before being cut
off for the day. Free's message_cost is 1, so both numbers happen to agree
at 15. Pro's message_cost is 3, so its real ceiling is 45 // 3 = 15 — not
the disconnected ASSISTANT_PLANS.daily_cap of 60 the page used to show,
which overstated Pro's real daily allowance by 4x.

That collision is also why the rebalanced page (docs/pricing-rebalance-plan.md)
sells Pro on the monthly credit grant instead: 60 vs 180 is the only 3x that
is true and enforced today. The tests below pin both halves — the number the
page may print, and the sameness it may NOT dress up as a difference.
"""
from __future__ import annotations

import re

import pytest
from django.test import override_settings

_CREDIT_PLANS = {
    "free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15},
    "pro": {"monthly_grant": 180, "message_cost": 3, "daily_burst": 45},
}

# Deliberately unlike the defaults in every digit, so a test that passes here
# is proving the page READS the credit system rather than that two hardcoded
# numbers happen to agree with it.
_ODD_CREDIT_PLANS = {
    "free": {"monthly_grant": 42, "message_cost": 2, "daily_burst": 18},
    "pro": {"monthly_grant": 210, "message_cost": 3, "daily_burst": 90},
}


@pytest.mark.django_db
@override_settings(CREDIT_PLANS=_CREDIT_PLANS)
def test_pricing_page_advisor_caps_match_the_credit_systems_real_daily_ceiling(client):
    body = client.get("/pricing/").content.decode()

    assert "15 messages a day" in body, "Free's real ceiling (15 // 1 = 15) should render"
    assert "60 messages a day" not in body, (
        "Pro's real daily ceiling is daily_burst // message_cost = 45 // 3 = 15, "
        "not the stale ASSISTANT_PLANS.daily_cap of 60"
    )
    assert body.count("messages a day") == 1, (
        "only Free may quote a daily message ceiling. Pro computes to the same 15 "
        "today, so printing it on both cards sells sameness as an upgrade"
    )


@pytest.mark.django_db
@override_settings(CREDIT_PLANS=_CREDIT_PLANS)
def test_pricing_page_renders_both_monthly_grants_from_the_credit_system(client):
    """The 3x Pro claim has to be the grant, because the grant is the only
    axis where Pro is actually three times Free."""
    body = client.get("/pricing/").content.decode()

    assert "15 messages a day, 60 a month." in body, (
        "Free's advisor bullet states both ceilings, the daily one and the monthly grant"
    )
    assert "Three times the credits: 180 a month." in body, (
        "Pro's advisor bullet sells the credit pool, not a message count"
    )


@pytest.mark.django_db
@override_settings(CREDIT_PLANS=_ODD_CREDIT_PLANS)
def test_pricing_grants_and_caps_track_a_changed_credit_plan_config(client):
    """Same page against a different CREDIT_PLANS: every figure moves with it.

    This is the whole point of measuring instead of typing. `advisor_free_cap`
    is 18 // 2 = 9 here, and the grants are 42 / 210 — none of which appear
    anywhere in the template.
    """
    response = client.get("/pricing/")
    body = response.content.decode()

    assert response.context["advisor_free_cap"] == 9
    assert response.context["advisor_pro_cap"] == 30
    assert response.context["advisor_free_grant"] == 42
    assert response.context["advisor_pro_grant"] == 210

    assert "9 messages a day, 42 a month." in body
    assert "Three times the credits: 210 a month." in body
    assert "60 a month" not in body, "no default grant may leak through as a literal"


@pytest.mark.django_db
@override_settings(CREDIT_PLANS=_CREDIT_PLANS)
def test_comparison_table_renders_every_row_in_both_columns(client):
    """Sixteen feature rows in four groups, three cells each.

    Counted rather than spot-checked: a comparison table that silently loses
    a row is the failure mode that matters here, and it is invisible to a
    test that only asserts a handful of labels exist.
    """
    body = client.get("/pricing/").content.decode()

    table = re.search(r'<table class="cmp">.*?</table>', body, re.S)
    assert table, "the side-by-side table should render inside the Individual panel"
    table = table.group(0)

    for group in ("The board", "The CRM", "The advisor", "Your data"):
        assert f'<td class="cmp-group" colspan="3">{group}</td>' in table

    rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)
    feature_rows = [r for r in rows if "cmp-group" not in r and "<th" not in r]
    assert len(feature_rows) == 16, (
        f"the spec calls for exactly 16 feature rows, found {len(feature_rows)}"
    )
    for row in feature_rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        assert len(cells) == 3, f"every feature row needs label + Free + Pro: {row!r}"
        for state in cells[1:]:
            assert (
                "cmp-yes" in state or "cmp-no" in state or "cmp-val" in state
            ), f"a plan cell must be a check, a dash, or a value: {state!r}"

    # The two states the page uses to say "this is where Pro differs".
    assert table.count('class="cmp-no"') == 3, (
        "exactly three rows are Pro-only: Gmail Live, Calendar sync, LinkedIn import"
    )
    assert "Gmail scan on demand" in table, "the free on-demand scan stays on the board"
    assert "Every 6 hours" in table and "Hourly on Tier 1" in table
    assert "This cycle" in table and "Every cycle" in table
    assert "Fast model" in table and "Stronger model" in table
    assert 'class="cmp-badge">In the works</span>' in table, (
        "one In-the-works marker on the Pro column header, and only one"
    )
    assert table.count("cmp-badge") == 1


@pytest.mark.django_db
@override_settings(CREDIT_PLANS=_CREDIT_PLANS)
def test_comparison_table_credit_row_is_measured_not_typed(client):
    """Row 13's two numbers are the context vars, not literals."""
    body = client.get("/pricing/").content.decode()
    table = re.search(r'<table class="cmp">.*?</table>', body, re.S).group(0)

    row = re.search(
        r"<tr>\s*<td>Credits a month, chat and Gmail AI scans</td>(.*?)</tr>",
        table,
        re.S,
    )
    assert row, "the credits row should exist under The advisor"
    assert '<span class="cmp-val">60</span>' in row.group(1)
    assert '<span class="cmp-val">180</span>' in row.group(1)


@pytest.mark.django_db
def test_free_card_no_longer_claims_gmail_live(client):
    """Move 1: real-time sync is the paid anchor; Free keeps the on-demand scan.

    Guards against the old copy creeping back in — the page spent a cycle
    listing Gmail Live on Free while Pro listed "Optional Gmail and Calendar
    sync" as an addition, which left a visitor unable to tell what the paid
    tier actually added.
    """
    body = client.get("/pricing/").content.decode()

    assert "Gmail Live: connect Gmail and it logs itself" not in body
    assert "Optional Gmail and Calendar sync" not in body
    assert "Gmail Live: real-time sync that logs itself." in body

    free_card = re.search(
        r'<article class="price-card featured.*?</article>', body, re.S
    ).group(0)
    assert "Gmail Live" not in free_card, "the Free card must not claim real-time sync"


@pytest.mark.django_db
def test_supporting_copy_agrees_with_the_rebalanced_story(client):
    """Hero and FAQ are part of the pitch and must not contradict the cards."""
    body = client.get("/pricing/").content.decode()

    assert "The board is free. The engine is Pro." in body
    assert "Free while we earn the right to charge." not in body
    assert "The whole board. No card, no trial clock." in body

    # FAQ 1 dropped its Gmail Live mention; FAQ 2's promise resets to the new
    # list, which is only honest because the page has never shipped to a user.
    assert "the Today queue and the Network board." in body
    assert "Everything listed in Free today stays in Free" not in body
    assert "This list is the floor. Free may grow; it will not shrink." in body
