"""Fit-score engine — the product's core scoring IP (docs/build-plan.md §6).
NEW code, nothing ported: the existing `confidence.py` scores *source-data
trustworthiness*, never a person's odds at a firm (docs/existing-system.md
§3) — do not confuse the two.

Two scores come out of one engine, both PURE functions of
(immutable touches + contacts, an as-of `datetime`, a params version):

  - `score_contact()` -> Contact Warmth Score. Four independent axes, each
    0–100 (Depth, Responsiveness, Recency, Leverage), a weighted composite,
    a band, a deterministic reasoning line, and an inputs hash.

  - `score_firm()` -> Firm Fit Score (user × firm). Four axes (Network
    strength, Momentum, Timeline readiness, Structural fit), composite,
    band, reasoning, hash. Network strength reuses `score_contact` for each
    contact at the firm, so the two scores share one definition of warmth.

One input helper comes with the engine:

  - `needs_sponsorship()` collapses a user's PER-REGION work authorization
    into the single tri-state `score_firm` takes, for one user × firm. The
    collapse rule is a product decision, so it lives here with the scoring
    contract rather than in whichever caller happens to need it.

Design guarantees (all four are load-bearing, per the brief):

  1. ZERO LLM, ZERO randomness. Every number here is a closed-form function
     of the inputs. No model call, no `random`, no set iteration order
     leaking into a result. The reasoning line is TEMPLATE-generated from
     the axis values, never written by a model.

  2. DETERMINISM. `inputs_hash` is a SHA-256 over the exact canonicalized
     inputs each score depends on (the used contact/touch fields, the as-of
     instant, and the params version). Identical inputs therefore provably
     reproduce a byte-identical result — the property the golden-fixture
     tests assert on a serialized snapshot.

  3. DECAY LIVES ONLY IN RECENCY. The stored warmth ratchet (pipeline.py)
     deliberately never decays — stage is a historical fact. Temperature is
     a computation, and the ONLY axis that applies time decay is Recency
     (exponential, ~45-day half-life). No other axis, and no part of the
     firm score, reintroduces decay.

  4. DEPTH READS THE LOG, NOT THE STORED WARMTH COLUMN. The Depth axis is
     the highest stage *evidenced in the touch log* (a reply < a chat <
     an advocate-marking), independent of `contacts.warmth`. This is the
     concrete meaning of "compute warmth on read" coexisting with "the
     stored ratchet never decays".

The web layer fetches rows (scoped to one user_id) and passes them in; this
module issues no query, reads no wall clock beyond the caller-supplied
`as_of`, and imports nothing from Django or a DB driver.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Parameters. One versioned bundle of every weight and constant the engine
# uses. `version` is echoed into each score row as `params_version` and mixed
# into `inputs_hash`, so a weight change is a visible, reproducible event
# rather than silent drift. Callers may pass a full override bundle (must
# carry its own `version`) to A/B a weight set without a code change.
# ---------------------------------------------------------------------------
DEFAULT_PARAMS: dict[str, Any] = {
    "version": "scoring-v1",

    # --- Contact Warmth Score ---
    # Depth: score per evidenced stage (0=no reply, 1=replied, 2=chatted,
    # 3=advocate-marked). Convex on purpose — a real conversation is most of
    # the way to the ceiling; the advocate step is the last stretch.
    "depth_level_scores": {0: 0.0, 1: 40.0, 2: 75.0, 3: 100.0},

    # Responsiveness: reply-ratio vs latency split, and the latency ramp.
    # A reply within a day scores near-max; latency >= latency_zero_days
    # scores 0.
    "resp_ratio_weight": 0.6,
    "resp_latency_weight": 0.4,
    "resp_latency_zero_days": 7.0,

    # Recency: exponential decay half-life, in days. A reply 45d ago carries
    # half weight; 8 months (~240d) ago ~2.5%.
    "recency_half_life_days": 45.0,

    # Leverage: role-seniority keyword tier (0–100) + a school-affiliation
    # bonus (additive, capped at 100). Unknown role -> neutral-low baseline.
    "leverage_unknown_role": 30.0,
    "leverage_school_bonus": 20.0,

    # Contact composite weights (sum to 1.0).
    "contact_weights": {
        "depth": 0.30,
        "responsiveness": 0.20,
        "recency": 0.30,
        "leverage": 0.20,
    },

    # --- Firm Fit Score ---
    # Network strength: top-k contacts, advocates overweighted; the target is
    # `advocate_target` full-strength advocates (the existing advocate_target:2
    # doctrine as the 100-point yardstick).
    "network_top_k": 3,
    "advocate_weight": 1.5,
    "advocate_target": 2,

    # Momentum: interaction-volume trend, recent vs base trailing windows.
    "momentum_recent_days": 14,
    "momentum_base_days": 45,

    # Timeline readiness: `runway_days` is the comfortable lead time to build
    # a firm network. Inside that runway, readiness increasingly reflects how
    # many warm contacts / advocates are actually in place. `warm_threshold`
    # is the contact-composite at/above which a contact counts as "warm".
    "timeline_runway_days": 90,
    "warm_threshold": 50.0,

    # Structural fit (rules v1) component weights (sum to 1.0).
    "structural_weights": {
        "region": 0.4,
        "track": 0.4,
        "sponsorship": 0.2,
    },

    # Firm composite weights (sum to 1.0).
    "firm_weights": {
        "network": 0.35,
        "momentum": 0.15,
        "timeline": 0.25,
        "structural": 0.25,
    },

    # Shared band thresholds (composite -> label). Checked high to low.
    "bands": [(75.0, "hot"), (50.0, "warm"), (25.0, "cool"), (0.0, "cold")],
}

# Touch-kind sets, matching pipeline.TOUCH_TRANSITIONS' vocabulary.
_REPLY_KINDS = ("reply_received", "chat_scheduled")   # evidence they engaged, pre-chat
_CHAT_KINDS = ("chat",)
_MEANINGFUL_KINDS = ("reply_received", "chat_scheduled", "chat")  # their engagement -> recency
_OUTBOUND_KINDS = ("outreach", "follow_up", "reping", "maintain", "thank_you")
_MANUAL_OVERRIDE_KIND = "manual_override"  # pipeline.set_state's audit touch


# Role-seniority keyword tiers, checked in order; first hit wins. Multiword
# phrases match as substrings of the lowercased role; the abbreviations in
# `_ROLE_TOKENS` match whole word tokens so "md" can't fire inside "admin".
_ROLE_PHRASES: list[tuple[str, float]] = [
    ("managing director", 100.0),
    ("global head", 100.0),
    ("group head", 100.0),
    ("head of", 95.0),
    ("executive director", 90.0),
    ("senior vice president", 65.0),
    ("vice president", 65.0),
    ("director", 80.0),   # after the two multiword *director* phrases above
    ("principal", 65.0),
    ("associate", 40.0),
    ("analyst", 30.0),
    ("intern", 20.0),
]
_ROLE_TOKENS: dict[str, float] = {
    "md": 100.0, "partner": 100.0, "ceo": 100.0, "cfo": 100.0, "coo": 100.0,
    "chief": 100.0, "president": 100.0, "ed": 90.0, "svp": 70.0, "vp": 65.0,
}


def _merged_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    if params is None:
        return DEFAULT_PARAMS
    if "version" not in params:
        raise ValueError("a custom params bundle must carry its own 'version' string")
    return dict(params)


# ---------------------------------------------------------------------------
# Small numeric / time helpers.
# ---------------------------------------------------------------------------
def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _round1(x: float) -> float:
    """Round to 1 dp. Every axis/composite is rounded through this before it
    leaves the engine, so a snapshot is stable across platforms' float
    formatting and small representation differences never make two 'equal'
    scores serialize differently."""
    return round(float(x), 1)


def _as_dt(value: Any) -> datetime | None:
    """Coerce a timestamp to a tz-aware UTC datetime (naive assumed UTC).
    Accepts datetime / date / ISO-8601 string. None on unparseable input."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[: len(fmt) + 2], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _days_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 86400.0


def _human_ago(days: float) -> str:
    """Compact age string for reasoning lines: '3d', '2w', '5mo', '1y'."""
    d = int(round(days))
    if d <= 0:
        return "today"
    if d < 14:
        return f"{d}d"
    if d < 60:
        return f"{d // 7}w"
    if d < 365:
        return f"{d // 30}mo"
    return f"{d // 365}y"


def _human_until(days: int) -> str:
    """Compact future-distance string: 'in ~5 weeks', 'in ~3 days'."""
    if days <= 0:
        return "now"
    if days < 14:
        return f"in ~{days} days"
    if days < 75:
        return f"in ~{max(1, round(days / 7))} weeks"
    return f"in ~{max(1, round(days / 30))} months"


def _inputs_hash(payload: Any) -> str:
    """SHA-256 over the canonicalized inputs a score depends on. sort_keys +
    default=str makes the serialization order-independent and stable, so the
    hash is identical iff the inputs are."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _band(composite: float, params: Mapping[str, Any]) -> str:
    for threshold, label in params["bands"]:
        if composite >= threshold:
            return label
    return params["bands"][-1][1]


# ---------------------------------------------------------------------------
# Contact Warmth Score.
# ---------------------------------------------------------------------------
def _depth_level(touches: list[Mapping[str, Any]]) -> int:
    """Highest stage EVIDENCED IN THE LOG (not the stored warmth column):
      3 advocate  — a manual_override audit touch that set warmth=advocate
      2 chatted   — a 'chat' touch (the conversation happened)
      1 replied   — a 'reply_received' or 'chat_scheduled' touch
      0 none      — only outreach/follow-up, or nothing
    Advocate is read from pipeline.set_state's audit touch, whose note is
    exactly 'manual override: warmth=advocate...' — the one place advocacy is
    recorded in the append-only log."""
    level = 0
    for t in touches:
        kind = t.get("kind")
        if kind in _CHAT_KINDS:
            level = max(level, 2)
        elif kind in _REPLY_KINDS:
            level = max(level, 1)
        elif kind == _MANUAL_OVERRIDE_KIND and "warmth=advocate" in (t.get("note") or ""):
            level = max(level, 3)
    return level


def _score_depth(level: int, params: Mapping[str, Any]) -> float:
    return _clamp(params["depth_level_scores"].get(level, 0.0))


def _score_responsiveness(
    touches: list[Mapping[str, Any]], params: Mapping[str, Any]
) -> tuple[float, dict[str, Any]]:
    """Reply ratio (how often they answer) blended with median reply latency
    (how fast). Latency is measured per reply as the gap to the immediately
    preceding outbound touch. No outbound at all -> 0 (no signal yet)."""
    sends = [t for t in touches if t.get("kind") in _OUTBOUND_KINDS]
    replies = [t for t in touches if t.get("kind") == "reply_received"]
    meta: dict[str, Any] = {"sends": len(sends), "replies": len(replies), "median_latency_days": None}
    if not sends and not replies:
        return 0.0, meta

    ratio = min(1.0, len(replies) / max(1, len(sends)))

    # Median latency: for each reply, the gap to the last outbound before it.
    ordered = sorted(touches, key=lambda t: (_as_dt(t.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)))
    latencies: list[float] = []
    last_out: datetime | None = None
    for t in ordered:
        dt = _as_dt(t.get("ts"))
        if dt is None:
            continue
        if t.get("kind") in _OUTBOUND_KINDS:
            last_out = dt
        elif t.get("kind") == "reply_received" and last_out is not None:
            latencies.append(max(0.0, _days_between(dt, last_out)))
    if latencies:
        latencies.sort()
        n = len(latencies)
        median = latencies[n // 2] if n % 2 else (latencies[n // 2 - 1] + latencies[n // 2]) / 2
        meta["median_latency_days"] = _round1(median)
        latency_score = _clamp(1.0 - median / params["resp_latency_zero_days"], 0.0, 1.0)
    else:
        latency_score = 0.0

    score = 100.0 * (params["resp_ratio_weight"] * ratio + params["resp_latency_weight"] * latency_score)
    meta["reply_ratio"] = _round1(ratio * 100) / 100
    return _clamp(score), meta


def _score_recency(
    touches: list[Mapping[str, Any]], as_of: datetime, params: Mapping[str, Any]
) -> tuple[float, dict[str, Any]]:
    """Exponential decay on the last MEANINGFUL interaction (their
    engagement: a reply or a chat). Half-life = recency_half_life_days. This
    is the ONLY axis in the whole engine that decays."""
    meaningful = [
        _as_dt(t.get("ts")) for t in touches if t.get("kind") in _MEANINGFUL_KINDS
    ]
    meaningful = [dt for dt in meaningful if dt is not None]
    if not meaningful:
        return 0.0, {"days_since_meaningful": None}
    last = max(meaningful)
    days = max(0.0, _days_between(as_of, last))
    weight = 0.5 ** (days / params["recency_half_life_days"])
    return _clamp(100.0 * weight), {"days_since_meaningful": _round1(days)}


def _seniority_from_role(role: str | None) -> float | None:
    """Coarse role-seniority tier (0–100) from keywords, or None if the role
    is empty/unrecognized. v1 is deliberately keyword-based."""
    if not role:
        return None
    text = role.lower()
    for phrase, score in _ROLE_PHRASES:
        if phrase in text:
            return score
    tokens = {tok for tok in "".join(ch if ch.isalnum() else " " for ch in text).split()}
    best: float | None = None
    for tok, score in _ROLE_TOKENS.items():
        if tok in tokens and (best is None or score > best):
            best = score
    return best


def _score_leverage(
    contact: Mapping[str, Any], params: Mapping[str, Any]
) -> tuple[float, dict[str, Any]]:
    seniority = _seniority_from_role(contact.get("role"))
    base = seniority if seniority is not None else params["leverage_unknown_role"]
    alum = bool(contact.get("school_affiliation"))
    score = base + (params["leverage_school_bonus"] if alum else 0.0)
    return _clamp(score), {
        "seniority": None if seniority is None else _round1(seniority),
        "school_affiliation": alum,
    }


def _contact_reason(
    depth_level: int, chat_count: int, recency_meta: dict, resp_meta: dict,
    lev_meta: dict, band: str,
) -> str:
    """Deterministic reasoning line from the axis facts — template, never a
    model. Fixed clause order reads naturally and reproduces exactly."""
    clauses: list[str] = []

    if depth_level >= 3:
        clauses.append("advocate")
    elif depth_level == 2:
        clauses.append(f"{chat_count} chat{'s' if chat_count != 1 else ''}")
    elif depth_level == 1:
        clauses.append("replied, no chat yet")
    else:
        clauses.append("no reply yet")

    ds = recency_meta.get("days_since_meaningful")
    if ds is not None:
        ago = _human_ago(ds)
        clauses.append("last today" if ago == "today" else f"last {ago} ago")

    ml = resp_meta.get("median_latency_days")
    if resp_meta.get("replies"):
        if ml is not None and ml <= 1.5:
            clauses.append("replies within a day")
        elif ml is not None and ml <= 4:
            clauses.append(f"replies in ~{int(round(ml))}d")
        elif ml is not None:
            clauses.append("slow to reply")
    elif resp_meta.get("sends"):
        n = resp_meta["sends"]
        clauses.append(f"no reply to {n} note{'s' if n != 1 else ''}")

    lev_bits = []
    if lev_meta.get("seniority") is not None and lev_meta["seniority"] >= 80:
        lev_bits.append("senior")
    if lev_meta.get("school_affiliation"):
        lev_bits.append("alum")
    if lev_bits:
        clauses.append("/".join(lev_bits))

    # Surface the ceiling for a strong-but-not-advocate contact — the
    # actionable next step, matching the brief's "no advocate yet" example.
    if depth_level == 2 and band in ("hot", "warm"):
        clauses.append("no advocate yet")

    return "; ".join(clauses)


def score_contact(
    contact: Mapping[str, Any],
    touches: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Contact Warmth Score for one contact.

    Args:
        contact: contact dict. Keys used: `id` (echoed), `role` (leverage),
            `school_affiliation` (leverage). The stored `warmth`/`thread_state`
            columns are DELIBERATELY NOT read — Depth comes from the log.
        touches: this contact's touch dicts. Keys used: `kind`, `ts`, `note`
            (the last only for the advocate-marking audit touch).
        as_of: as-of instant driving Recency decay (and the inputs hash).
        params: optional override bundle (must carry `version`).

    Returns:
        {"subject_type": "contact", "subject_id", "axes": {axis: {...}},
         "composite", "band", "reasoning", "params_version", "inputs_hash",
         "as_of"}. Every number is rounded to 1 dp so a serialized snapshot is
        byte-stable.
    """
    p = _merged_params(params)
    as_of = _as_dt(as_of)  # naive -> UTC, so the serialized as_of never depends on machine tz
    tlist = list(touches)

    depth_level = _depth_level(tlist)
    chat_count = sum(1 for t in tlist if t.get("kind") in _CHAT_KINDS)
    depth = _score_depth(depth_level, p)
    responsiveness, resp_meta = _score_responsiveness(tlist, p)
    recency, rec_meta = _score_recency(tlist, as_of, p)
    leverage, lev_meta = _score_leverage(contact, p)

    w = p["contact_weights"]
    composite = (
        w["depth"] * depth
        + w["responsiveness"] * responsiveness
        + w["recency"] * recency
        + w["leverage"] * leverage
    )
    composite = _round1(composite)
    band = _band(composite, p)

    axes = {
        "depth": {"score": _round1(depth), "level": depth_level, "chats": chat_count},
        "responsiveness": {"score": _round1(responsiveness), **resp_meta},
        "recency": {"score": _round1(recency), **rec_meta},
        "leverage": {"score": _round1(leverage), **lev_meta},
    }
    reasoning = _contact_reason(depth_level, chat_count, rec_meta, resp_meta, lev_meta, band)

    hash_payload = {
        "kind": "contact",
        "version": p["version"],
        "as_of": as_of.astimezone(timezone.utc).isoformat(),
        "contact": {
            "role": contact.get("role"),
            "school_affiliation": bool(contact.get("school_affiliation")),
        },
        "touches": sorted(
            (
                {
                    "kind": t.get("kind"),
                    "ts": (_as_dt(t.get("ts")).isoformat() if _as_dt(t.get("ts")) else None),
                    "note": t.get("note") if t.get("kind") == _MANUAL_OVERRIDE_KIND else None,
                }
                for t in tlist
            ),
            key=lambda d: (d["ts"] or "", d["kind"] or ""),
        ),
    }

    return {
        "subject_type": "contact",
        "subject_id": contact.get("id"),
        "axes": axes,
        "composite": composite,
        "band": band,
        "reasoning": reasoning,
        "params_version": p["version"],
        "inputs_hash": _inputs_hash(hash_payload),
        "as_of": as_of.astimezone(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Firm Fit Score (user × firm).
# ---------------------------------------------------------------------------
def _score_network(
    contact_scores: list[dict], params: Mapping[str, Any]
) -> tuple[float, dict[str, Any]]:
    """Top-k contact composites, advocates overweighted. Full marks =
    `advocate_target` full-strength advocates (the advocate_target:2 doctrine
    as the 100-point yardstick)."""
    if not contact_scores:
        return 0.0, {"contacts": 0, "advocates": 0, "top_k": []}
    weighted = []
    advocates = 0
    for cs in contact_scores:
        is_adv = cs["axes"]["depth"]["level"] >= 3
        if is_adv:
            advocates += 1
        eff = cs["composite"] * (params["advocate_weight"] if is_adv else 1.0)
        weighted.append((eff, cs["composite"], is_adv, cs.get("subject_id")))
    weighted.sort(key=lambda x: (-x[0], str(x[3])))
    top = weighted[: params["network_top_k"]]
    raw = sum(e for e, *_ in top)
    target_raw = params["advocate_target"] * params["advocate_weight"] * 100.0
    score = _clamp(100.0 * raw / target_raw) if target_raw else 0.0
    return score, {
        "contacts": len(contact_scores),
        "advocates": advocates,
        "top_k": [{"id": sid, "composite": comp, "advocate": adv} for _, comp, adv, sid in top],
    }


def _score_momentum(
    touches: list[Mapping[str, Any]], as_of: datetime, params: Mapping[str, Any]
) -> tuple[float, dict[str, Any]]:
    """Interaction-volume trend: per-day rate in the recent window vs the
    base window. Accelerating -> 100, steady -> 50, decelerating/stalled ->
    below 50 / 0. Overlapping windows (recent inside base) are intentional —
    a fresh burst reads as momentum."""
    recent_days = params["momentum_recent_days"]
    base_days = params["momentum_base_days"]
    recent = base = 0
    for t in touches:
        dt = _as_dt(t.get("ts"))
        if dt is None:
            continue
        age = _days_between(as_of, dt)
        if 0 <= age <= base_days:
            base += 1
            if age <= recent_days:
                recent += 1
    meta = {"recent_count": recent, "base_count": base}
    if base == 0:
        return 0.0, meta
    recent_rate = recent / recent_days
    base_rate = base / base_days
    ratio = recent_rate / base_rate if base_rate else 0.0
    score = _clamp(50.0 + 50.0 * (ratio - 1.0))
    meta["trend_ratio"] = _round1(ratio)
    return score, meta


def _next_firm_date(
    firm_dates: Iterable[Mapping[str, Any]], user: Mapping[str, Any], today: date
) -> dict[str, Any] | None:
    """Soonest upcoming CONFIRMED firm_date within the user's regions.
    app_open / app_close / insight_deadline all qualify; the nearest one
    drives timeline readiness."""
    user_regions = {r.lower() for r in (user.get("regions") or [])}
    best: dict[str, Any] | None = None
    best_date: date | None = None
    for e in firm_dates:
        if e.get("confidence") != "confirmed_official":
            continue
        if e.get("event_kind") not in ("app_open", "app_close", "insight_deadline"):
            continue
        region = (e.get("region") or "").lower()
        if user_regions and region and region not in user_regions:
            continue
        d = _as_dt(e.get("date"))
        if d is None:
            continue
        d = d.date()
        if d < today:
            continue
        if best_date is None or d < best_date:
            best, best_date = dict(e), d
    return best


def _score_timeline(
    firm_dates: Iterable[Mapping[str, Any]],
    user: Mapping[str, Any],
    contact_scores: list[dict],
    as_of: datetime,
    params: Mapping[str, Any],
) -> tuple[float, dict[str, Any]]:
    """Readiness = are you prepared for the firm's next binding date? HIGH
    with plenty of runway OR with warm contacts in place; LOW when a date is
    imminent AND no warm contacts exist ("opens in 30 days, zero warm
    contacts" — and the reasoning says so)."""
    today = as_of.date()
    nxt = _next_firm_date(firm_dates, user, today)
    warm = sum(1 for cs in contact_scores if cs["composite"] >= params["warm_threshold"])
    advocates = sum(1 for cs in contact_scores if cs["axes"]["depth"]["level"] >= 3)
    meta: dict[str, Any] = {
        "warm_contacts": warm, "advocates": advocates,
        "next_event": None, "days_until": None,
    }
    if nxt is None:
        # No confirmed date -> not measurably behind; neutral, and say so.
        return 60.0, meta

    days_until = (_as_dt(nxt.get("date")).date() - today).days
    meta["next_event"] = nxt.get("event_kind")
    meta["days_until"] = days_until

    # network readiness: advocates count full, other warm contacts half,
    # against the advocate_target yardstick.
    target = params["advocate_target"]
    network_readiness = min(1.0, (advocates + 0.5 * max(0, warm - advocates)) / target) if target else 1.0

    runway = params["timeline_runway_days"]
    urgency = _clamp((runway - days_until) / runway, 0.0, 1.0)  # 0 far out, 1 at/after the date
    readiness_fraction = 1.0 - urgency * (1.0 - network_readiness)
    meta["network_readiness"] = _round1(network_readiness * 100) / 100
    return _clamp(100.0 * readiness_fraction), meta


def _overlap(a: Iterable[str] | None, b: Iterable[str] | None) -> bool | None:
    """True/False if both sides are known; None if either is missing (=>
    treated as neutral, not penalized)."""
    sa = {x.lower() for x in (a or [])}
    sb = {x.lower() for x in (b or [])}
    if not sa or not sb:
        return None
    return bool(sa & sb)


# Work-authorization vocabulary (mirrors accounts.models.WORK_AUTH). Anything
# else — including a missing entry — is UNKNOWN, never a guess.
WORK_AUTH_CITIZEN = "citizen"
WORK_AUTH_SPONSORSHIP = "sponsorship"


def needs_sponsorship(
    work_authorization: Mapping[str, Any] | None,
    user_regions: Iterable[str] | None,
    firm_regions: Iterable[str] | None,
) -> bool | None:
    """Derive `score_firm`'s `needs_sponsorship` input for ONE user × firm.

    Work authorization is per-region (`{"us": "citizen", "hk": "sponsorship"}`)
    because a student can be free to work in one target region and need a visa
    in the other. A firm, though, gets a single structural-fit answer — so the
    per-region facts have to collapse, and how they collapse is a real product
    decision:

      RULE: BEST CASE ACROSS THE REGIONS IN PLAY. If there is even one relevant
      region where the student needs no sponsorship, the answer is False (no
      sponsorship needed) — a student only has to be employable in ONE of a
      firm's regions to work there, and a firm that can hire them in New York
      is not a worse fit because they'd also need a visa in Hong Kong. Only
      when EVERY relevant region needs sponsorship does this return True. If
      no region says "citizen" and any relevant region has no entry at all,
      the answer is None — unknown, which `_score_structural` scores as
      neutral rather than penalizing.

    "Relevant regions" are the overlap between the user's target regions and
    the firm's — the regions where this pairing could actually happen. When
    that overlap is empty (an unstated preference on either side, or a firm the
    user is browsing outside their regions) it falls back to whichever side is
    known, and to None when neither is.

    Args:
        work_authorization: {region: "citizen" | "sponsorship"}; unknown
            regions may be absent. Unrecognized values count as unknown.
        user_regions: the user's target regions.
        firm_regions: the firm's regions.

    Returns:
        False (no sponsorship needed), True (needs it everywhere relevant), or
        None (not enough information — scored neutral).
    """
    user_set = {(r or "").strip().lower() for r in (user_regions or [])} - {""}
    firm_set = {(r or "").strip().lower() for r in (firm_regions or [])} - {""}
    relevant = user_set & firm_set or firm_set or user_set
    if not relevant:
        return None

    auth = {
        (k or "").strip().lower(): (v or "").strip().lower()
        for k, v in (work_authorization or {}).items()
        if isinstance(v, str)
    }
    statuses = [auth.get(region) for region in sorted(relevant)]
    if any(s == WORK_AUTH_CITIZEN for s in statuses):
        return False
    if statuses and all(s == WORK_AUTH_SPONSORSHIP for s in statuses):
        return True
    return None


def _score_structural(
    user: Mapping[str, Any], firm: Mapping[str, Any], params: Mapping[str, Any]
) -> tuple[float, dict[str, Any]]:
    """Rule-based v1 (labeled honestly as rules): region overlap, track
    overlap, and a work-authorization/sponsorship check. Unknown inputs are
    neutral (0.5), never a hard penalty."""
    def comp(v: bool | None) -> float:
        return 1.0 if v else (0.5 if v is None else 0.0)

    region = _overlap(user.get("regions"), firm.get("regions"))
    track = _overlap(user.get("tracks"), firm.get("tracks"))

    needs = user.get("needs_sponsorship")
    if needs is None:
        spon: bool | None = None
    elif not needs:
        spon = True   # no sponsorship needed -> no conflict possible
    else:
        firm_sponsors = firm.get("sponsors")
        spon = None if firm_sponsors is None else bool(firm_sponsors)

    w = params["structural_weights"]
    score = 100.0 * (w["region"] * comp(region) + w["track"] * comp(track) + w["sponsorship"] * comp(spon))
    return _clamp(score), {
        "method": "rules_v1",
        "region_overlap": region,
        "track_overlap": track,
        "sponsorship_ok": spon,
    }


def _firm_reason(
    net_meta: dict, tl_meta: dict, struct_meta: dict, mom_meta: dict,
) -> str:
    """Deterministic firm reasoning line — template from the axis facts."""
    clauses: list[str] = []

    n = net_meta.get("contacts", 0)
    if n == 0:
        clauses.append("no contacts yet")
    else:
        adv = net_meta.get("advocates", 0)
        bit = f"{n} contact{'s' if n != 1 else ''}"
        if adv:
            bit += f" ({adv} advocate{'s' if adv != 1 else ''})"
        else:
            bit += ", no advocate yet"
        clauses.append(bit)

    ev = tl_meta.get("next_event")
    du = tl_meta.get("days_until")
    if ev is not None and du is not None:
        verb = {"app_open": "opens", "app_close": "closes", "insight_deadline": "insight due"}.get(ev, ev)
        warm = tl_meta.get("warm_contacts", 0)
        tail = "" if warm else ", no warm contacts"
        clauses.append(f"{verb} {_human_until(du)}{tail}")

    parts = []
    if struct_meta.get("region_overlap"):
        parts.append("region")
    if struct_meta.get("track_overlap"):
        parts.append("track")
    if parts:
        clauses.append("+".join(parts) + " match")
    if struct_meta.get("sponsorship_ok") is False:
        clauses.append("no sponsorship")

    ratio = mom_meta.get("trend_ratio")
    if mom_meta.get("base_count"):
        if ratio is not None and ratio >= 1.25:
            clauses.append("activity picking up")
        elif ratio is not None and ratio <= 0.5:
            clauses.append("activity cooling")

    return "; ".join(clauses)


def score_firm(
    user: Mapping[str, Any],
    firm: Mapping[str, Any],
    contacts: Iterable[Mapping[str, Any]],
    touches: Iterable[Mapping[str, Any]],
    firm_dates: Iterable[Mapping[str, Any]] | None = None,
    *,
    as_of: datetime,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Firm Fit Score for one (user, firm) pair.

    Args:
        user: user dict. Keys used: `id` (echoed), `regions`, `tracks`,
            `needs_sponsorship` (structural fit + timeline region scope).
        firm: firm dict. Keys used: `id` (echoed), `regions`, `tracks`,
            `sponsors` (structural fit).
        contacts: the user's contacts AT THIS FIRM. Each is scored via
            `score_contact` (so network strength shares one warmth
            definition).
        touches: touch dicts across those contacts (interaction volume ->
            momentum; per-contact warmth via `score_contact`).
        firm_dates: shared firm_date dicts for this firm (timeline
            readiness). Keys used: `event_kind`, `region`, `date`,
            `confidence`.
        as_of: as-of instant (recency/momentum/timeline + inputs hash).
        params: optional override bundle (must carry `version`).

    Returns:
        {"subject_type": "firm", "subject_id", "axes", "composite", "band",
         "reasoning", "params_version", "inputs_hash", "as_of"}.
    """
    p = _merged_params(params)
    as_of = _as_dt(as_of)  # naive -> UTC, so the serialized as_of never depends on machine tz
    clist = list(contacts)
    tlist = list(touches)
    fdates = list(firm_dates or ())

    # Per-contact warmth, reusing the contact engine.
    touches_by_contact: dict[Any, list[Mapping[str, Any]]] = {}
    for t in tlist:
        touches_by_contact.setdefault(t.get("contact_id"), []).append(t)
    contact_scores = [
        score_contact(c, touches_by_contact.get(c.get("id"), []), as_of=as_of, params=p)
        for c in clist
    ]

    network, net_meta = _score_network(contact_scores, p)
    momentum, mom_meta = _score_momentum(tlist, as_of, p)
    timeline, tl_meta = _score_timeline(fdates, user, contact_scores, as_of, p)
    structural, struct_meta = _score_structural(user, firm, p)

    w = p["firm_weights"]
    composite = (
        w["network"] * network
        + w["momentum"] * momentum
        + w["timeline"] * timeline
        + w["structural"] * structural
    )
    composite = _round1(composite)
    band = _band(composite, p)

    axes = {
        "network": {"score": _round1(network), **net_meta},
        "momentum": {"score": _round1(momentum), **mom_meta},
        "timeline": {"score": _round1(timeline), **tl_meta},
        "structural": {"score": _round1(structural), **struct_meta},
    }
    reasoning = _firm_reason(net_meta, tl_meta, struct_meta, mom_meta)

    hash_payload = {
        "kind": "firm",
        "version": p["version"],
        "as_of": as_of.astimezone(timezone.utc).isoformat(),
        "user": {
            "id": user.get("id"),
            "regions": sorted(r.lower() for r in (user.get("regions") or [])),
            "tracks": sorted(t.lower() for t in (user.get("tracks") or [])),
            "needs_sponsorship": user.get("needs_sponsorship"),
        },
        "firm": {
            "id": firm.get("id"),
            "regions": sorted(r.lower() for r in (firm.get("regions") or [])),
            "tracks": sorted(t.lower() for t in (firm.get("tracks") or [])),
            "sponsors": firm.get("sponsors"),
        },
        "contact_hashes": sorted(cs["inputs_hash"] for cs in contact_scores),
        "firm_dates": sorted(
            (
                {
                    "event_kind": e.get("event_kind"),
                    "region": (e.get("region") or "").lower(),
                    "date": (_as_dt(e.get("date")).date().isoformat() if _as_dt(e.get("date")) else None),
                    "confidence": e.get("confidence"),
                }
                for e in fdates
            ),
            key=lambda d: (d["date"] or "", d["event_kind"] or "", d["region"]),
        ),
    }

    return {
        "subject_type": "firm",
        "subject_id": firm.get("id"),
        "axes": axes,
        "composite": composite,
        "band": band,
        "reasoning": reasoning,
        "params_version": p["version"],
        "inputs_hash": _inputs_hash(hash_payload),
        "as_of": as_of.astimezone(timezone.utc).isoformat(),
    }
