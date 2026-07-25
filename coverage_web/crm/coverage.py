"""Coverage exposure: which of the user's tiered firms they are least
covered at, ranked.

The product is called Coverage; the Network board should answer "where am I
actually covered?" without the user having to read 69 firm cards and do the
arithmetic in their head. That is what `rank_gaps` below is for.

This module is PURE, in the same sense as `coverage_domain.cadence`: it
reads no database and no wall clock. The web layer fetches the rows (already
tenant-scoped), flattens them into the plain dicts described on `rank_gaps`,
and passes an explicit `today`. Same inputs, same output, every time — which
is what makes the ranking testable against constructed fixtures rather than
eyeballed against production data.

Deliberately NOT a learned or tuned score. Every number below is a small
integer a user can be shown and can argue with; the rank is reproducible by
hand from the card itself.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping

# The product default when the user hasn't set `assets["advocate_target"]`.
# Matches coverage_domain.cadence.CADENCE_DEFAULTS["advocate_target"], which
# is the same yardstick the backward planner uses for its
# advocates-in-place task.
DEFAULT_ADVOCATE_TARGET = 2

# ---------------------------------------------------------------------------
# THE FORMULA (deterministic, explainable, no magic score)
# ---------------------------------------------------------------------------
#
#     exposure = TIER_WEIGHT[tier] × GAP_POINTS[state] + deadline_bonus(days)
#
# Three independent, separately-defensible terms:
#
# 1. TIER_WEIGHT — how much this firm matters TO THE USER. The user's own
#    tiering is the only statement of priority the product has, so it
#    multiplies rather than adds: a Tier 1 firm with no contacts at all
#    (3 × 4 = 12) outranks a Tier 3 firm with no contacts (1 × 4 = 4) and
#    every lesser Tier 1 state. Untiered firms are not ranked at all — the
#    user hasn't claimed to care about them yet.
#
# 2. GAP_POINTS — how far from covered this firm is, on the warmth ladder
#    the rest of the app already uses. The steps are ordered by what it
#    would take to close them: finding a name at a firm where you know
#    nobody is a bigger lift than warming a name you already have, which is
#    a bigger lift than converting a chat into an advocate. A firm at or
#    above the advocate target scores 0 and is not a gap — the strip is not
#    allowed to nag about firms that are done.
#
# 3. deadline_bonus — urgency, ADDED not multiplied, and only for a
#    CONFIRMED official close date (the same `confirmed_official` bar
#    coverage_domain.cadence._closing_soon and the backward planner hold:
#    a rumored date never moves anything). Additive on purpose: a close
#    date makes a gap more urgent, it does not make a Tier 3 firm matter
#    like a Tier 1. A Tier 3 with nobody and a deadline inside two weeks
#    (4 + 3 = 7) still ranks below a Tier 1 with nobody and no known date
#    (12), and above a Tier 1 that is one advocate short (3) — which is the
#    intended reading: you cannot fix a firm you have no way into once its
#    deadline lands.
#
# Ties break on (tier, firm name) so the strip is stable render to render.
TIER_WEIGHT: dict[int, int] = {1: 3, 2: 2, 3: 1}

NO_CONTACTS = "no_contacts"
ALL_COLD = "all_cold"
NO_ADVOCATE = "no_advocate"
BELOW_TARGET = "below_target"
COVERED = "covered"

GAP_POINTS: dict[str, int] = {
    NO_CONTACTS: 4,   # nobody at the firm — no way in at all
    ALL_COLD: 3,      # names, but not one of them has ever replied
    NO_ADVOCATE: 2,   # warm contacts, nobody who'd go to bat for you
    BELOW_TARGET: 1,  # advocates, but fewer than the target
    COVERED: 0,       # at/above target — not a gap
}

GAP_LABELS: dict[str, str] = {
    NO_CONTACTS: "No contacts",
    ALL_COLD: "No replies yet",
    NO_ADVOCATE: "No advocate",
    BELOW_TARGET: "Short of target",
    COVERED: "Covered",
}

# (days until the confirmed close, bonus). First match wins; anything
# further out (or unknown) scores 0.
DEADLINE_BONUS: tuple[tuple[int, int], ...] = ((14, 3), (30, 2), (60, 1))

# Warmth values that mean the contact has engaged back at least once.
WARM = frozenset({"replied", "chatted", "advocate"})


def advocate_target(user) -> int:
    """The user's advocates-per-firm yardstick from
    `User.assets["advocate_target"]`, falling back to the product default.

    Defensive because `assets` is a free-form JSONField: a missing key, a
    string, a bool (an int subclass in Python), or a nonsense value like 0
    or -1 all fall back rather than propagating into the arithmetic below,
    where a target of 0 would make every firm permanently "covered" and a
    negative one would make every firm permanently short."""
    raw = getattr(user, "assets", None)
    value = raw.get("advocate_target") if isinstance(raw, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return DEFAULT_ADVOCATE_TARGET
    return value


def gap_state(warmths: Iterable[str], advocates: int, target: int) -> str:
    """Which rung of the gap ladder this firm sits on. `warmths` is every
    non-archived contact's warmth at the firm; `advocates` is how many of
    them are advocates (passed in rather than recounted so callers that
    already have the count don't pay for it twice)."""
    warmths = list(warmths)
    if advocates >= target:
        return COVERED
    if not warmths:
        return NO_CONTACTS
    if advocates:
        return BELOW_TARGET
    if any(w in WARM for w in warmths):
        return NO_ADVOCATE
    return ALL_COLD


def deadline_bonus(days_out: int | None) -> int:
    """Urgency points for a CONFIRMED close date `days_out` days away.
    Already-passed dates (negative) score the maximum: a deadline you
    missed at a firm you have no coverage at is the most exposed a firm
    can be, not the least."""
    if days_out is None:
        return 0
    for limit, bonus in DEADLINE_BONUS:
        if days_out <= limit:
            return bonus
    return 0


def rank_gaps(
    firms: Iterable[Mapping[str, Any]],
    *,
    today: date,
    target: int = DEFAULT_ADVOCATE_TARGET,
    limit: int = 6,
) -> list[dict]:
    """The worst `limit` coverage gaps among the user's tiered firms.

    Args:
        firms: one dict per UserFirm row. Keys used:
            `firm_id`, `name`, `tier` (1/2/3; anything else is skipped),
            `warmths` (list of warmth strings for the user's non-archived
            contacts at that firm), `app_close` (a `date` for the soonest
            CONFIRMED official close, or None — the caller does the
            confidence filtering, exactly as the cadence engine's caller
            does).
        today: the as-of date. Drives deadline proximity only.
        target: advocates-per-firm yardstick (see `advocate_target`).
        limit: how many gaps to return. "The worst handful", not a report.

    Returns:
        Gap dicts sorted worst-first, each carrying the full arithmetic
        (`tier_weight`, `gap_points`, `deadline_bonus`, `exposure`) so the
        UI can show its work instead of asserting a number.
    """
    ranked: list[dict] = []
    for f in firms:
        tier = f.get("tier")
        if tier not in TIER_WEIGHT:
            continue
        warmths = list(f.get("warmths") or [])
        advocates = sum(1 for w in warmths if w == "advocate")
        state = gap_state(warmths, advocates, target)
        points = GAP_POINTS[state]
        if not points:
            continue
        close = f.get("app_close")
        days_out = (close - today).days if close else None
        bonus = deadline_bonus(days_out)
        ranked.append(
            {
                "firm_id": f.get("firm_id"),
                "name": f.get("name") or "",
                "tier": tier,
                "state": state,
                "label": GAP_LABELS[state],
                "contact_count": len(warmths),
                "advocates": advocates,
                "target": target,
                "app_close": close,
                "days_out": days_out,
                "tier_weight": TIER_WEIGHT[tier],
                "gap_points": points,
                "deadline_bonus": bonus,
                "exposure": TIER_WEIGHT[tier] * points + bonus,
            }
        )
    ranked.sort(key=lambda g: (-g["exposure"], g["tier"], g["name"]))
    return ranked[:limit]


def tier_cost(cards: Iterable[Mapping[str, Any]], target: int) -> dict:
    """What one tier is actually committing the user to.

    `cards` is the tier's firm cards (each with `advocates`). A tier of 14
    firms at a target of 2 is a promise to find 28 people willing to
    advocate — a number worth seeing before deciding the tiering is right,
    because the alternative is discovering it one unanswered email at a
    time. Re-tiering deliberately is the intended response.
    """
    cards = list(cards)
    firms = len(cards)
    have = sum(c.get("advocates", 0) for c in cards)
    needed = firms * target
    return {
        "firms": firms,
        "target": target,
        "needed": needed,
        "have": have,
        "remaining": max(0, needed - have),
        # Firms where the user knows literally nobody: the part of the
        # commitment that hasn't even started.
        "uncovered": sum(1 for c in cards if not c.get("contact_count")),
    }
