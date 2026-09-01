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

# ---------------------------------------------------------------------------
# TWO MORE FIRM-LEVEL FACTS (2026-09-01): track fit, which HALVES the gap
# points, and recruiting style, which ZEROES them.
# ---------------------------------------------------------------------------
#
#     exposure = TIER_WEIGHT[tier] × GAP_POINTS[state] × track_fit + deadline_bonus
#
# 4. track_fit — does this firm hire for the student's own tracks at all?
#    Measured on the founder's account 2026-09-01: 18 of his 25 zero-contact
#    tiered firms are OFF his tracks (7 PE, 3 AM, 2 consulting, 6 corp-strat
#    tech), and eleven of them — Apollo, Ares, Bain & Co, Bain Capital,
#    BlackRock, Blue Owl, Carlyle, Fidelity International, KKR, McKinsey,
#    Oaktree, PIMCO — outranked HSBC, a tier-1 bank on his track with 8
#    contacts and a confirmed close on 2026-10-30. The strip was telling an
#    IB/S&T student that his worst exposure was at private-equity shops he
#    had tiered aspirationally, and the formula could not see it because it
#    never read `firm.tracks` or `user.tracks`.
#
#    A MULTIPLIER, not a filter: the firm is still his tier 2 and still a
#    gap, it is just half the gap a same-tier on-track firm is, because the
#    seat it would open is not the one he is recruiting for. Half lands
#    where a student would put it by hand: a tier-2 off-track firm with
#    nobody (2 × 4 × 0.5 = 4) now sits below a tier-1 on-track firm with no
#    advocate (3 × 2 = 6) and above a tier-3 on-track firm one advocate
#    short (1 × 1 = 1). The card says why it sank: its state becomes
#    OFF_TRACK ("Not on your tracks") and the ladder rung it would otherwise
#    show rides along as `ladder_state`.
#
#    DEGRADES TO TODAY'S ORDER EXACTLY when either side has no tracks. A firm
#    with no verticals on file, or a student who skipped that onboarding
#    step, gets 1.0 — "we do not know" is not "off track". ANY overlap is a
#    full fit: the founder runs two tracks (ib, st), and a firm that hires
#    for one of them is on track, so a multi-track student is never
#    penalised for the track they are not using at this firm.
#
# 5. recruiting_style — can networking move this firm's process at all?
#    `Firm.recruiting_style == "assessment"` (see that field's comment for
#    the evidence: Jane Street's FAQ answers "Can I schedule a phone call or
#    coffee?" with "unfortunately, no"; Citadel Securities' campus funnel is
#    Datathons and Invitationals; "if you can't pass their tests it doesn't
#    matter who you know") sets GAP_POINTS to 0 outright. There is no
#    networking gap to close at a firm that hires off a test, and a strip
#    that ranked Jane Street "No contacts · exp 12" was asking the student
#    to do the one thing the firm says does nothing. What survives is the
#    deadline term: a confirmed close still puts the firm on the strip, for
#    the deadline alone, and the card's verb is "Apply" rather than "Add"
#    with the reason beside it. No confirmed close, no card — the strip has
#    nothing honest to ask.
OFF_TRACK = "off_track"
GAP_LABELS[OFF_TRACK] = "Not on your tracks"

ON_TRACK_FIT = 1.0
OFF_TRACK_FIT = 0.5

# `Firm.recruiting_style` values, spelled here so this module stays free of
# Django imports (see the module docstring: pure, like the cadence engine).
CAMPUS = "campus"
ASSESSMENT = "assessment"

# The verb on an empty firm's card, and the one line that explains "Apply".
VERB_ADD = "Add"
VERB_APPLY = "Apply"
ASSESSMENT_REASON = "They hire off their test, not off a chat."

# The outcome-predicting yardstick from the networking research: students
# who land offers carry somewhere between 2 and 20 people who would actively
# vouch for them, across all their target firms. Per-firm, `advocate_target`
# is the student's own dial; this pair is the product-wide range the Network
# summary prints beside the count, so a student with 0 of 2 at every one of
# 54 firms reads one number and one range instead of 54 identical tooltips.
ADVOCATE_RANGE: tuple[int, int] = (2, 20)


def track_fit(firm_tracks: Iterable[str] | None, user_tracks: Iterable[str] | None) -> float:
    """1.0 when the firm hires for at least one of the student's tracks, or
    when either side has no tracks on file; 0.5 when both sides are known
    and share nothing. See the paragraph above for why half and why the
    unknown case is a full fit."""
    firm = {t for t in (firm_tracks or []) if t}
    user = {t for t in (user_tracks or []) if t}
    if not firm or not user:
        return ON_TRACK_FIT
    return ON_TRACK_FIT if firm & user else OFF_TRACK_FIT


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
            does), and `open` (count of open campus roles, optional).
            Three more, all optional and all degrading to today's ranking
            when absent: `firm_tracks` and `user_tracks` (lists of track
            slugs; see `track_fit`), and `recruiting_style` (a
            `Firm.recruiting_style` value; "assessment" zeroes the gap
            points and turns the card's verb into "Apply").
            `open` does NOT enter the exposure formula and never will —
            hiring volume is not a coverage gap. It breaks TIES, and only
            after exposure and tier have both said nothing. Same-tier,
            same-gap-state firms score identically on purpose, which is the
            formula being honest and also the point at which it stops
            helping; before this the tie fell through to alphabetical, so
            "Apollo" outranked a firm with 64 seats open on the strength of
            its first letter. The strip used to print the count on the card
            to break the tie by eye. It was asked to stop, so the ranking
            does the comparing instead.
        today: the as-of date. Drives deadline proximity only.
        target: advocates-per-firm yardstick (see `advocate_target`).
        limit: how many gaps to return. "The worst handful", not a report.

    Returns:
        Gap dicts sorted worst-first, each carrying the full arithmetic
        (`tier_weight`, `gap_points`, `deadline_bonus`, `exposure`) so the
        UI can show its work instead of asserting a number. `gap_points` is
        the ladder's points AFTER `track_fit`, so `tier_weight × gap_points
        + deadline_bonus` is always exactly `exposure` — the card's hover
        arithmetic stays true without knowing about tracks; the unfitted
        rung is `ladder_points`. Also on each: `state` (OFF_TRACK when the
        fit halved it, else the ladder rung), `ladder_state` / `ladder_label`
        (the rung either way), `track_fit`, `off_track`, `recruiting_style`,
        `verb` ("Add", or "Apply" at an assessment firm) and `verb_reason`
        (the one line beside "Apply"; "" otherwise).
    """
    ranked: list[dict] = []
    for f in firms:
        tier = f.get("tier")
        if tier not in TIER_WEIGHT:
            continue
        warmths = list(f.get("warmths") or [])
        advocates = sum(1 for w in warmths if w == "advocate")
        state = gap_state(warmths, advocates, target)
        if state == COVERED:
            continue
        fit = track_fit(f.get("firm_tracks"), f.get("user_tracks"))
        style = f.get("recruiting_style") or CAMPUS
        assessment = style == ASSESSMENT
        ladder_points = GAP_POINTS[state]
        # Fitted points: the rung halved for an off-track firm, zeroed for a
        # firm that hires off a test. Kept as a number the hover can print —
        # 2, 1.5, 1, 0.5 — rather than hidden inside `exposure`.
        points = 0 if assessment else ladder_points * fit
        close = f.get("app_close")
        days_out = (close - today).days if close else None
        bonus = deadline_bonus(days_out)
        exposure = TIER_WEIGHT[tier] * points + bonus
        if exposure <= 0:
            # Only an assessment firm with no confirmed close lands here
            # (a covered firm left above): nothing on this strip can help
            # it, so it is not drawn rather than drawn at zero.
            continue
        off_track = fit < ON_TRACK_FIT
        ranked.append(
            {
                "firm_id": f.get("firm_id"),
                "name": f.get("name") or "",
                "tier": tier,
                "state": OFF_TRACK if off_track else state,
                "label": GAP_LABELS[OFF_TRACK] if off_track else GAP_LABELS[state],
                "ladder_state": state,
                "ladder_label": GAP_LABELS[state],
                "contact_count": len(warmths),
                "advocates": advocates,
                "target": target,
                "app_close": close,
                "days_out": days_out,
                "tier_weight": TIER_WEIGHT[tier],
                "gap_points": _tidy(points),
                "ladder_points": ladder_points,
                "track_fit": fit,
                "off_track": off_track,
                "recruiting_style": style,
                "verb": VERB_APPLY if assessment else VERB_ADD,
                "verb_reason": ASSESSMENT_REASON if assessment else "",
                "deadline_bonus": bonus,
                "exposure": _tidy(exposure),
                "open": int(f.get("open") or 0),
            }
        )
    # Four keys, plus a fifth the strip's own promise needs.
    #
    # `rank_gaps`' docstring says ties break "so the strip is stable render to
    # render", and until this line that was only true down to the fourth key.
    # Two firms at the same tier, in the same gap state, with the same open
    # count and the same name tie on all four and fall through to `list.sort`'s
    # stability — i.e. to the order the CALLER built `firms` in, which is a
    # queryset with no ORDER BY. Measured: six identical firms shuffled 50
    # times produced 47 distinct strips. Same firms, same data, different
    # order on the next page load.
    #
    # `str(...)` on the name for the same reason `_coerce_tier` exists in the
    # cadence engine: `f.get("name") or ""` leaves a non-string through
    # unchanged, and one integer name in the list crashes the whole sort with
    # `'<' not supported between instances of 'str' and 'int'` — taking down
    # the Network board rather than mislabelling one card. `firm_id` last is
    # the actual tiebreak; it is unique per row, so the order is total.
    ranked.sort(
        key=lambda g: (
            -g["exposure"], g["tier"], -g["open"], str(g["name"]), str(g["firm_id"]),
        )
    )
    return ranked[:limit]


def _tidy(value: float) -> int | float:
    """`4.0` prints as `4`, `4.5` stays `4.5`. The strip's numbers were all
    integers before `track_fit` existed and most still are; a card that
    reads "exposure 12.0" would be the multiplier leaking into the copy."""
    return int(value) if float(value).is_integer() else round(value, 1)


def advocate_summary(
    firms: Iterable[Mapping[str, Any]], *, target: int = DEFAULT_ADVOCATE_TARGET
) -> dict:
    """One honest aggregate for the top of the Network page: how many
    advocates the student has across their tiered firms, against the range
    the outcome research says matters (`ADVOCATE_RANGE`, 2-20).

    Takes the same row dicts `rank_gaps` takes (only `tier` and `warmths`
    are read). Counts ADVOCATES AT TIERED FIRMS ONLY, because that is what
    the number is for: the founder's two advocates are both USC peers at a
    free-text firm, and a summary that counted them would print "2" over a
    board where every one of his 54 target firms is at zero. Untiered
    `UserFirm` rows are left out for the same reason `rank_gaps` skips
    them — the student has not claimed to care about them yet.

    Returns a dict rather than the bare line so a template can print the
    parts it wants: `advocates`, `firms` (tiered firm count), `covered`
    (firms at or above `target`), `target`, `low`/`high` (the range), and
    `line` — "Advocates: 0 across 54 target firms · aim for 2-20". The
    template is not touched here; `crm.views.contact_list` puts this in
    the context as `advocate_summary`.
    """
    tiered = [f for f in firms if f.get("tier") in TIER_WEIGHT]
    per_firm = [
        sum(1 for w in (f.get("warmths") or []) if w == "advocate") for f in tiered
    ]
    advocates = sum(per_firm)
    covered = sum(1 for n in per_firm if n >= target)
    low, high = ADVOCATE_RANGE
    firms_n = len(tiered)
    line = (
        f"Advocates: {advocates} across {firms_n} target "
        f"firm{'' if firms_n == 1 else 's'} · aim for {low}-{high}"
    )
    return {
        "advocates": advocates,
        "firms": firms_n,
        "covered": covered,
        "target": target,
        "low": low,
        "high": high,
        "line": line,
    }


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
    # `or 0`, not just the `.get` default: a card carrying an explicit
    # `advocates=None` (the shape an annotated queryset produces for a firm
    # with no matching rows, before COALESCE) satisfies `.get("advocates", 0)`
    # and then raises `TypeError: unsupported operand type(s) for +: 'int' and
    # 'NoneType'` inside `sum` — the same missing-value trap `_firm_meta`'s
    # tier coercion documents one module over, in the same shape.
    have = sum(c.get("advocates") or 0 for c in cards)
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
