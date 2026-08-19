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
"""
from __future__ import annotations

import pytest
from django.test import override_settings

_CREDIT_PLANS = {
    "free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15},
    "pro": {"monthly_grant": 180, "message_cost": 3, "daily_burst": 45},
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
    assert body.count("15 messages a day") == 2, (
        "both Free and Pro should show the real 15/day ceiling with this CREDIT_PLANS config"
    )
