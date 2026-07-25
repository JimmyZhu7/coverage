"""Deterministic, explainable role recommendations for the Opportunities feed.

Same posture as `classify.py`: pure functions over plain data, no LLM, no
randomness, no network. Two hard requirements shape everything below.

**Deterministic.** The same profile and the same candidate set must produce the
same ordered list, forever. So the score is integer arithmetic over small named
weights, and the sort key ends in the opportunity id — never a set iteration
order, never a dict ordering accident, never `random`.

**Explainable.** Every recommendation carries the reasons that produced it, in
plain language, and the card renders them. A student who cannot interrogate a
recommendation has no way to tell "this is for me" from "the machine likes
this", and an un-interrogable recommendation is worse than none at all on a
surface whose whole pitch is that the data is real.

The two things this module refuses to do, both for the same reason the rest of
the app refuses them:

- It never derives a class year and then presents it as stated. `Opportunity.
  class_year` is populated ONLY where a posting literally says "Class of 20XX"
  (3 rows in ~4,000 — see `classify.py`). `Opportunity.cohort` is the
  PROGRAMME/intake year, which is a much weaker signal about who is eligible.
  Both feed the score, at very different weights, and a match derived from a
  programme year is labelled "likely" in the UI. See `_class_fit`.
- It never guesses a region it cannot justify. An unknown school or an
  unlocated role scores zero on that axis rather than being penalised or
  optimistically matched.

Entry points: `Profile`, `Candidate`, `score_candidate`, `recommend`. None of
them touch a request, a session, or the ORM, so the whole ranking is testable
with plain objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from directory.classify import ENTRY_LEVEL, INSIGHT, INTERNSHIP, normalize_region

# ---------------------------------------------------------------------------
# Weights. All four inputs the brief names are here, and each one alone can
# move the ranking. They are integers so the arithmetic is exact and a test can
# assert on the number rather than on a float that happens to compare equal.
# ---------------------------------------------------------------------------

# 1. Class / cycle fit.
#: The posting itself states the student's graduation year. The strongest
#: signal on the board, and the rarest.
W_CLASS_STATED = 30
#: The posting states a DIFFERENT class year. Not a soft miss — the firm has
#: said out loud who this is for, and it is not this student. Large negative so
#: it cannot be outrun by a tier-1 firm the student happens to like.
W_CLASS_STATED_MISMATCH = -25
#: The programme year implies this student's graduation year (see `_GRAD_WINDOW`).
#: Deliberately well under `W_CLASS_STATED`: it is inference, not a statement.
W_CLASS_DERIVED = 18
#: The implied year is adjacent to the student's (one year early or late).
#: Worth something — students do apply a year out — but only just.
W_CLASS_DERIVED_NEAR = 6
#: The role is the exact programme the student named as their target cycle.
W_CYCLE = 15

# 2. University location / reachability.
#: The role sits in the market the student's university is in — the cheapest
#: possible recruiting geography for them (campus pipelines, no relocation).
W_REGION_SCHOOL = 20
#: The role sits in a market the student explicitly named in their profile.
W_REGION_TARGET = 16

# 3. Industry preference.
#: First overlap between the student's tracks and the firm's.
W_TRACK_FIRST = 18
#: Each further overlap, capped — a firm covering all six tracks is not three
#: times the match of one that covers the student's exact two.
W_TRACK_EXTRA = 3
W_TRACK_CAP = 24

# 4. Firm tier (the student's own `crm.UserFirm.tier`).
#: Tier 1 must outrank tier 3. It does, by construction, and by enough that
#: tier alone clears `MIN_SCORE` while tier 3 alone does not.
TIER_POINTS: Mapping[int, int] = {1: 26, 2: 16, 3: 8}
#: A firm the student targeted but never tiered. Real signal, weak.
W_TARGET_UNTIERED = 4

#: The bar a role must clear to be shown at all. Calibrated so that no single
#: weak input can put a role on the bar by itself: a track match alone (18) or
#: a region match alone (16/20) is not a recommendation, while a tier-1 target
#: firm (26), or any two inputs together, is. Below this the honest answer is
#: an empty state, not a padded list — see `recommend`.
MIN_SCORE = 25

#: How many cards the bar shows. The bar is one horizontal row; more than this
#: and it stops being a shortlist and starts being a second feed.
DEFAULT_LIMIT = 6

#: At most this many picks from any one firm.
#:
#: Found in the browser, not in a test: scoring is per-role and every axis
#: except class fit is a property of the FIRM, so a single tier-1 firm that
#: matches the student's tracks and region scores identically on all of its
#: openings — and the first live render of the bar was six Bank of America
#: roles. That is a correct ranking and a useless shortlist; a student who
#: wanted six BofA roles would have clicked BofA. The cap is applied greedily
#: over the already-sorted list, so it changes WHICH roles show without
#: touching the ordering rule or the determinism guarantee.
MAX_PER_FIRM = 2


# ---------------------------------------------------------------------------
# School -> region. Deterministic, local, and deliberately small.
#
# The problem: `User.school` is free text a student typed ("USC Marshall"), and
# nothing in it looks like a place to a location parser. Geocoding it would
# mean a network call per user for a signal worth 20 points, so instead:
#
#   1. a short table of university names/abbreviations that carry no city or
#      country token, checked on word boundaries; then
#   2. `classify.normalize_region` over the raw string, which already knows
#      city and country tokens and therefore handles the (large) majority of
#      real school names for free — "University of Hong Kong", "London
#      Business School", "Singapore Management University".
#
# Unknown schools return "" and score zero on this axis. That is the whole
# safety property: the table's job is to add signal for the names it knows, not
# to have an opinion about every string. Ambiguous abbreviations are left OUT
# on purpose — "SMU" is Singapore Management University and Southern Methodist
# University, so it is not in the table and falls through to the location
# parser, which correctly declines to answer.
# ---------------------------------------------------------------------------
SCHOOL_REGION_KEYS: Mapping[str, tuple[str, ...]] = {
    "us": (
        "usc", "marshall", "ucla", "berkeley", "haas", "wharton", "upenn",
        "nyu", "stern", "columbia", "mit", "sloan", "harvard", "hbs", "yale",
        "princeton", "stanford", "gsb", "booth", "kellogg", "northwestern",
        "ross", "mccombs", "fuqua", "cornell", "dartmouth", "tuck",
        "georgetown", "mcdonough", "emory", "goizueta", "ivey", "stevens",
    ),
    "hk": ("hku", "cuhk", "hkust", "polyu", "cityu", "hkbu", "ust"),
    "sg": ("nus", "ntu"),  # see the ambiguity note above re: bare "SMU"
    "eu": (
        "lse", "ucl", "imperial", "warwick", "oxbridge", "insead", "bocconi",
        "hec", "esade", "essec", "st gallen", "lbs", "wharton-lbs",
    ),
    "cn": ("tsinghua", "peking", "fudan", "sjtu", "清华", "北大", "复旦"),
    "jp": ("waseda", "keio", "todai", "hitotsubashi"),
}

#: Precompiled word-boundary matchers, in a fixed order so the mapping is a
#: function and not a dict-iteration coincidence.
_SCHOOL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (code, re.compile("|".join(rf"(?<![\w-]){re.escape(k)}(?![\w-])" for k in keys),
                      re.IGNORECASE))
    for code, keys in SCHOOL_REGION_KEYS.items()
)

#: Short region labels for the reason chips. The card has ~a dozen characters
#: of room per chip, so "HK" rather than "Hong Kong"; the full label goes in
#: the chip's tooltip.
REGION_SHORT = {"hk": "HK", "us": "US", "sg": "SG", "eu": "EU", "cn": "CN", "jp": "JP"}
#: Full region names for the tooltips. Written already-capitalised and used
#: verbatim — an earlier cut ran `.capitalize()` over them to start a sentence
#: and rendered "The united states".
REGION_FULL = {
    "hk": "Hong Kong", "us": "United States", "sg": "Singapore",
    "eu": "Europe", "cn": "Mainland China", "jp": "Japan",
}

#: Short track labels for the reason chips (the long ones live in views.py's
#: TRACK_LABELS and are far too wide for a chip).
TRACK_SHORT = {
    "ib": "IB", "st": "S&T", "pe": "PE", "am": "AM",
    "consulting": "Consulting", "corp-strat": "Corp Strat",
}


def school_region(school: str) -> str:
    """Map a free-text school name to one of the product's region codes, or ""
    when it cannot be determined. Never guesses — see the section comment."""
    text = (school or "").strip()
    if not text:
        return ""
    for code, pattern in _SCHOOL_PATTERNS:
        if pattern.search(text):
            return code
    return normalize_region(text)


# ---------------------------------------------------------------------------
# Programme year -> graduation year. The documented, honest mapping.
#
# A summer internship running in intake year N is, in the ordinary case, done
# by a student the summer BEFORE they graduate — so N implies graduation N+1.
# A full-time graduate programme starting in year N is joined by that year's
# graduates, so N implies N. An insight programme / spring week in year N is
# aimed at first- and second-years, who are typically two to three years from
# graduating, so N implies N+2 or N+3.
#
# All three are conventions, not rules: degree lengths differ, regions differ,
# and individual firms differ. That is exactly why this mapping only ever
# feeds the DERIVED weights and why every UI string built from it says
# "likely". The stated-class-year path never touches this table.
#
# Values are inclusive `(min_offset, max_offset)` windows added to the cohort.
# ---------------------------------------------------------------------------
_GRAD_WINDOW: Mapping[str, tuple[int, int]] = {
    INTERNSHIP: (1, 1),
    ENTRY_LEVEL: (0, 0),
    INSIGHT: (2, 3),
}

#: Target-cycle prefixes -> the bucket that cycle is actually made of. Students
#: write their cycle the way the industry says it: "SA 2028" is the Summer
#: Analyst class of summer 2028, i.e. an internship with cohort 2028.
_CYCLE_BUCKETS: Mapping[str, str] = {
    "sa": INTERNSHIP, "summer analyst": INTERNSHIP, "sum": INTERNSHIP,
    "ft": ENTRY_LEVEL, "full time": ENTRY_LEVEL, "full-time": ENTRY_LEVEL,
    "sw": INSIGHT, "spring week": INSIGHT, "insight": INSIGHT,
}
_CYCLE_RX = re.compile(r"^\s*([A-Za-z][A-Za-z\s-]*?)\s*(20\d\d)\s*$")


def parse_target_cycle(target_cycle: str) -> tuple[str, int] | None:
    """"SA 2028" -> ("internship", 2028). Unrecognised text -> None.

    Returning None rather than a partial guess matters: an unparsed cycle must
    cost the student nothing, not silently match every 2028 posting."""
    m = _CYCLE_RX.match(target_cycle or "")
    if not m:
        return None
    kind = " ".join(m.group(1).lower().split())
    bucket = _CYCLE_BUCKETS.get(kind)
    if bucket is None:
        return None
    return bucket, int(m.group(2))


def _int_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Plain-data inputs. Neither dataclass knows about Django; `from_user` /
# `from_opportunity` are thin adapters kept beside them so the view stays dumb.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Reason:
    """One plain-language justification for a recommendation.

    `text` is what the chip shows (short — the card is narrow). `detail` is the
    full sentence, rendered as the chip's tooltip, and is where any derivation
    is admitted out loud."""

    text: str
    detail: str
    #: Presentation hook, not logic: "tier" / "track" / "region" / "class".
    kind: str = ""


@dataclass(frozen=True)
class Profile:
    """The signed-in student, flattened to the five things scoring reads."""

    class_year: int | None = None
    target_cycle: str = ""
    school: str = ""
    regions: tuple[str, ...] = ()
    tracks: tuple[str, ...] = ()
    #: firm_id -> tier (1/2/3, or None for "targeted but untiered"). Comes from
    #: `crm.UserFirm.objects.for_user(user)` — the caller does the tenant
    #: scoping; this module never queries.
    firm_tiers: Mapping[int, int | None] = field(default_factory=dict)

    @property
    def school_region(self) -> str:
        return school_region(self.school)

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to personalise on.

        The bar must not claim to be tailored to a student who has told us
        nothing — a signed-in user with an empty survey gets the same honest
        "we don't know you yet" state as a signed-out one."""
        return not any((
            self.class_year, self.target_cycle.strip(), self.school.strip(),
            self.regions, self.tracks, self.firm_tiers,
        ))

    @classmethod
    def from_user(cls, user, firm_tiers: Mapping[int, int | None] | None = None) -> "Profile":
        return cls(
            class_year=_int_or_none(getattr(user, "class_year", None)),
            target_cycle=getattr(user, "target_cycle", "") or "",
            school=getattr(user, "school", "") or "",
            regions=tuple(r.lower() for r in (getattr(user, "regions", None) or [])),
            tracks=tuple(getattr(user, "tracks", None) or []),
            firm_tiers=dict(firm_tiers or {}),
        )


@dataclass(frozen=True)
class Candidate:
    """One open role, flattened. Mirrors `Opportunity` plus the two firm fields
    scoring needs, so nothing here has to follow a FK mid-scoring."""

    id: int
    firm_id: int
    firm_name: str
    firm_slug: str
    title: str
    url: str
    bucket: str = ""
    cohort: str = ""
    class_year: str = ""
    region: str = ""
    location: str = ""
    firm_tracks: tuple[str, ...] = ()
    deadline: date | None = None

    @classmethod
    def from_opportunity(cls, o) -> "Candidate":
        return cls(
            id=o.id, firm_id=o.firm_id, firm_name=o.firm.name,
            firm_slug=o.firm.slug, title=o.title, url=o.url,
            bucket=o.bucket or "", cohort=o.cohort or "",
            class_year=o.class_year or "", region=o.region or "",
            location=o.location or "",
            firm_tracks=tuple(o.firm.tracks or []),
            deadline=o.deadline,
        )


@dataclass(frozen=True)
class Recommendation:
    candidate: Candidate
    score: int
    reasons: tuple[Reason, ...]

    @property
    def why(self) -> str:
        """The one-line "why" a card renders: "Tier 1 · matches IB · HK"."""
        return " · ".join(r.text for r in self.reasons)


# ---------------------------------------------------------------------------
# The four scoring axes. Each returns (points, reasons) and is independent of
# the others, so a test can move exactly one input and watch the ranking move.
# ---------------------------------------------------------------------------

def _class_fit(profile: Profile, c: Candidate) -> tuple[int, list[Reason]]:
    """Class / cycle fit — the axis where the cohort-vs-class-year distinction
    is load-bearing.

    A stated class year is authoritative: it settles who the posting is for,
    including settling it AGAINST the student, in which case nothing else on
    this axis is allowed to argue. Only when the posting states nothing do we
    infer from the programme year, at a lower weight and labelled "likely".

    The target-cycle bonus is a separate question ("is this the programme I'm
    recruiting for") and applies on top of either path."""
    points, reasons = 0, []
    cohort = _int_or_none(c.cohort)

    stated = _int_or_none(c.class_year)
    if stated is not None and profile.class_year:
        if stated != profile.class_year:
            # The posting named a different class. This is a veto, not a
            # subtraction: no other class/cycle evidence gets to argue with the
            # firm's own words, so nothing below runs.
            return W_CLASS_STATED_MISMATCH, [Reason(
                f"Class of {stated}",
                f"The posting states Class of {stated}, not your {profile.class_year}.",
                "class",
            )]
        points += W_CLASS_STATED
        reasons.append(Reason(
            f"Class of {stated}",
            f"The posting states Class of {stated}, which is your class year.",
            "class",
        ))
        # Deliberately falls through to the cycle bonus below but NOT to the
        # derived-from-cohort branch. A stated class year is the authoritative
        # answer to "which class is this for", so inferring a second, weaker
        # answer from the intake year would be noise at best — while the target
        # cycle is a different question ("is this the programme I'm recruiting
        # for") and still applies. An earlier cut returned here outright, which
        # made a posting that *stated* the student's class score lower than one
        # that merely implied it. That was backwards.
    elif cohort is not None and profile.class_year:
        window = _GRAD_WINDOW.get(c.bucket)
        if window is not None:
            lo, hi = (cohort + window[0], cohort + window[1])
            gap = 0 if lo <= profile.class_year <= hi else min(
                abs(profile.class_year - lo), abs(profile.class_year - hi)
            )
            if gap == 0:
                points += W_CLASS_DERIVED
                reasons.append(Reason(
                    f"likely Class of {profile.class_year}",
                    f"A {cohort} programme is usually done by students "
                    f"graduating {lo}{'' if lo == hi else f'–{hi}'}. The posting "
                    f"does not state a class year — this is inferred from the "
                    f"{cohort} intake year, not something the firm said.",
                    "class",
                ))
            elif gap == 1:
                points += W_CLASS_DERIVED_NEAR
                reasons.append(Reason(
                    f"{cohort} intake",
                    f"A {cohort} programme usually targets {lo}"
                    f"{'' if lo == hi else f'–{hi}'} graduates — a year off "
                    f"from your {profile.class_year}, so worth a look but not a fit.",
                    "class",
                ))

    cycle = parse_target_cycle(profile.target_cycle)
    if cycle is not None and cohort is not None:
        bucket, year = cycle
        if c.bucket == bucket and cohort == year:
            points += W_CYCLE
            reasons.append(Reason(
                profile.target_cycle.strip(),
                f"This is a {year} {c.bucket.replace('_', ' ')} — the "
                f"{profile.target_cycle.strip()} cycle you're recruiting for.",
                "class",
            ))
    return points, reasons


def _region_fit(profile: Profile, c: Candidate) -> tuple[int, list[Reason]]:
    """University location. Two ways a market can be right for a student: it is
    where their university is (campus pipeline, no relocation, no visa change),
    or they named it as a target. The first scores higher; if both are true the
    student gets the higher score once, not both.

    A role with no resolved region scores zero rather than being penalised —
    board data leaves `region` blank often, and "we couldn't tell" must not
    read as "wrong place"."""
    if not c.region:
        return 0, []
    home = profile.school_region
    targets = set(profile.regions)
    short = REGION_SHORT.get(c.region, c.region.upper())
    full = REGION_FULL.get(c.region, c.region)

    if home and c.region == home:
        detail = f"{full} — the market your university ({profile.school}) sits in."
        if c.region in targets:
            detail += " You also named it as a target region."
        return W_REGION_SCHOOL, [Reason(short, detail, "region")]
    if c.region in targets:
        return W_REGION_TARGET, [Reason(
            short, f"{full} — one of the regions on your profile.", "region",
        )]
    return 0, []


def _track_fit(profile: Profile, c: Candidate) -> tuple[int, list[Reason]]:
    """Industry preference: the student's tracks against the firm's. Sorted by
    the student's own stated order so the chip text is stable and reflects
    their priority, not the firm's field order."""
    firm = set(c.firm_tracks)
    overlap = [t for t in profile.tracks if t in firm]
    if not overlap:
        return 0, []
    points = min(W_TRACK_CAP, W_TRACK_FIRST + W_TRACK_EXTRA * (len(overlap) - 1))
    names = [TRACK_SHORT.get(t, t.upper()) for t in overlap]
    return points, [Reason(
        "matches " + " + ".join(names[:2]),
        f"{c.firm_name} covers {', '.join(names)}, which you're recruiting for.",
        "track",
    )]


def _tier_fit(profile: Profile, c: Candidate) -> tuple[int, list[Reason]]:
    """Firm tier, straight off the student's own target list."""
    if c.firm_id not in profile.firm_tiers:
        return 0, []
    tier = profile.firm_tiers.get(c.firm_id)
    if tier in TIER_POINTS:
        return TIER_POINTS[tier], [Reason(
            f"Tier {tier}",
            f"{c.firm_name} is a Tier {tier} firm on your target list.", "tier",
        )]
    return W_TARGET_UNTIERED, [Reason(
        "On your list",
        f"{c.firm_name} is on your target list but has no tier yet.", "tier",
    )]


#: Fixed axis order — this is what fixes the order of the reason chips, so
#: "Tier 1 · matches IB · HK" reads the same way on every card and in every
#: test run.
_AXES = (_tier_fit, _track_fit, _region_fit, _class_fit)


def score_candidate(profile: Profile, candidate: Candidate) -> tuple[int, tuple[Reason, ...]]:
    """Score one role for one student. Pure: same inputs, same output."""
    total = 0
    reasons: list[Reason] = []
    for axis in _AXES:
        points, why = axis(profile, candidate)
        total += points
        reasons.extend(why)
    return total, tuple(reasons)


def _sort_key(rec: Recommendation):
    """Total order, with no ties left to chance.

    Score first; then the soonest real deadline (rolling roles last, because a
    dated role at the same score is the one that can actually be missed); then
    firm name and id, which are stable and unique."""
    d = rec.candidate.deadline
    return (
        -rec.score,
        d or date.max,
        rec.candidate.firm_name.lower(),
        rec.candidate.id,
    )


def recommend(
    profile: Profile,
    candidates: Sequence[Candidate],
    *,
    limit: int = DEFAULT_LIMIT,
    min_score: int = MIN_SCORE,
    max_per_firm: int = MAX_PER_FIRM,
) -> list[Recommendation]:
    """Rank `candidates` for `profile` and return the ones worth showing.

    Returns [] — not a padded list of the least-bad options — when nothing
    clears `min_score`. The caller renders that as an honest empty state. A
    recommendation bar that always has six cards in it is a bar that means
    nothing when it has six cards in it.

    An empty profile also returns []: with nothing to personalise on, every
    score would be zero anyway, but returning early makes the intent explicit
    and skips the work.

    `max_per_firm` keeps one firm from owning the whole bar — see MAX_PER_FIRM.
    It is applied AFTER the sort, greedily, so the ranking rule is untouched:
    the roles that survive are still the highest-scoring ones, just spread."""
    if profile.is_empty:
        return []
    out = []
    for c in candidates:
        score, reasons = score_candidate(profile, c)
        if score >= min_score:
            out.append(Recommendation(candidate=c, score=score, reasons=reasons))
    out.sort(key=_sort_key)

    picked: list[Recommendation] = []
    seen: dict[int, int] = {}
    for rec in out:
        n = seen.get(rec.candidate.firm_id, 0)
        if n >= max_per_firm:
            continue
        seen[rec.candidate.firm_id] = n + 1
        picked.append(rec)
        if len(picked) >= limit:
            break
    return picked
