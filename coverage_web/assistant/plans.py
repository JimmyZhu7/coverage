"""Which model answers, and what a message costs — per plan.

`accounts.User.plan` is the only input; `settings.ASSISTANT_PLANS` (model
selection) and `settings.CREDIT_PLANS` (what a message costs — see
settings/base.py for the reasoning behind each tier) are the only sources of
values. Nothing else in the assistant app should read either directly, so
that a third tier, a per-user override, or a Stripe-driven plan change is
a change HERE and nowhere else.

An unknown or blank plan resolves to Free rather than raising: this field
is written by admin today and by a billing webhook tomorrow, and a typo in
either must degrade a student to the cheap tier, not 500 their advisor.

`daily_cap` stays on `PlanLimits` for one reason only: `core/views.py`'s
pricing-page context still reads it (`templates/core/pricing.html`'s own
copy names "15 messages a day" / "60 messages a day"). It no longer gates
anything in `agent.py` — `message_cost`, resolved from
`billing.credits.plan_config` via `settings.CREDIT_PLANS`, is what the
credit system (docs/credit-system-plan.md) actually enforces. See that
module for `monthly_grant` and `daily_burst`, the other two numbers a plan
carries; they aren't duplicated here because nothing in this app reads them
directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from billing import credits as billing_credits

FREE = "free"
PRO = "pro"

# Fallbacks if settings are somehow missing a tier — mirrors the settings
# defaults so a bare test environment behaves the same as a configured one.
_DEFAULTS = {
    FREE: {"model": "claude-haiku-4-5-20251001", "daily_cap": 15},
    PRO: {"model": "claude-sonnet-5", "daily_cap": 60},
}


@dataclass(frozen=True)
class PlanLimits:
    plan: str
    model: str
    daily_cap: int
    message_cost: int

    @property
    def label(self) -> str:
        return "Pro" if self.plan == PRO else "Free"


def plan_of(user) -> str:
    plan = (getattr(user, "plan", "") or "").strip().lower()
    return plan if plan in _DEFAULTS else FREE


def limits_for(user) -> PlanLimits:
    plan = plan_of(user)
    configured = (getattr(settings, "ASSISTANT_PLANS", None) or {}).get(plan) or {}
    defaults = _DEFAULTS[plan]
    return PlanLimits(
        plan=plan,
        model=str(configured.get("model") or defaults["model"]),
        daily_cap=int(configured.get("daily_cap") or defaults["daily_cap"]),
        message_cost=billing_credits.plan_config(user)["message_cost"],
    )
