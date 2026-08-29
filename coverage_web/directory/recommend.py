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
#: Ceiling for a match INFERRED from the firm's coverage.
W_TRACK_CAP = 20
#: A match the ROLE ITSELF states ("Sales & Trading Summer Analyst"). Above
#: the inferred ceiling on purpose: evidence must outrank inference, and
#: without the gap a generic "Intern" at a firm covering three of the
#: student's tracks (18 + 3 + 3 = 24) outscored a posting that named their
#: track outright (21) — the scorer preferring the role it had guessed about
#: to the one that had told it.
W_TRACK_STATED = 26

# 4. The student's own NETWORK at the firm — the CRM data this product calls
#    its moat, which until 2026-08-09 contributed nothing to ranking. A warm
#    relationship changes what a listing is worth: the same posting with an
#    advocate behind it is a referral conversation, not a cold application.
#: Someone at this firm has actually talked with the student (warmth
#: `chatted` or `advocate`). Sits between tier-2 and tier-1 on purpose:
#: a real conversation at an untargeted firm should be able to outrank a
#: targeted-but-cold tier-2, while never outrunning the class axis — who a
#: programme is FOR still beats who you know there.
W_NETWORK_WARM = 14
#: Someone there has replied but not yet talked. Real, weaker.
W_NETWORK_REPLIED = 7

# 4b. Firm tier (the student's own `crm.UserFirm.tier`).
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
#
# EVERY NAME NEEDS BOTH OF ITS SPELLINGS. The table started as abbreviations
# and brand names only, which quietly assumed students type "USC" rather than
# what is on their diploma. The founder's own account says "University of
# Southern California": `usc` does not appear in it as a token, and
# `normalize_region` knows cities and countries but not US states, so his
# school resolved to "" and the highest-weighted region signal on the board
# (W_REGION_SCHOOL = 20) scored zero for the only real user of the product.
# Checked across 52 spelled-out names, 14 missed the same way.
#
# The additions below are spellings of institutions the table already meant to
# cover, never new guesses, and a name that is only unambiguous in its long
# form is stored in its long form: bare "cambridge" is also Cambridge,
# Massachusetts (Harvard and MIT both sit in it) and bare "oxford" is also
# Oxford, Georgia (Emory) and Oxford, Ohio (Miami University), so both are
# keyed as "university of oxford"/"university of cambridge" and a student who
# types the bare city still gets the honest "" rather than a coin flip.
# ---------------------------------------------------------------------------
SCHOOL_REGION_KEYS: Mapping[str, tuple[str, ...]] = {
    "us": (
        "usc", "marshall", "ucla", "berkeley", "haas", "wharton", "upenn",
        "nyu", "stern", "columbia", "mit", "sloan", "harvard", "hbs", "yale",
        "princeton", "stanford", "gsb", "booth", "kellogg", "northwestern",
        "ross", "mccombs", "fuqua", "cornell", "dartmouth", "tuck",
        "georgetown", "mcdonough", "emory", "goizueta", "ivey", "stevens",
        # Spelled-out forms of the same institutions, plus the US schools
        # whose full names carry no token above at all.
        "southern california", "massachusetts institute of technology",
        "pennsylvania", "duke", "vanderbilt", "notre dame", "carnegie mellon",
        "johns hopkins", "rice university", "virginia", "michigan",
        "new york university", "university of chicago", "washington university",
    ),
    "hk": ("hku", "cuhk", "hkust", "polyu", "cityu", "hkbu", "ust"),
    "sg": (
        "nus", "ntu",  # see the ambiguity note above re: bare "SMU"
        "nanyang technological",
    ),
    "eu": (
        "lse", "ucl", "imperial", "warwick", "oxbridge", "insead", "bocconi",
        "hec", "esade", "essec", "st gallen", "lbs", "wharton-lbs",
        "university of oxford", "university of cambridge",
        # The other word order — "Oxford University", "Cambridge University"
        # — is at least as common as "University of X" for these two
        # (it's the everyday British short form and how most international
        # applicants write it), and was missing: the section comment above
        # says "EVERY NAME NEEDS BOTH OF ITS SPELLINGS," and only one order
        # was added. Unlike the bare city names, this pair is safe: nothing
        # else calls itself "Oxford University" or "Cambridge University" —
        # Oxford, Georgia's Emory campus is "Oxford College of Emory
        # University," never "Oxford University" on its own, and Cambridge,
        # Massachusetts answers to Harvard or MIT, neither of which uses
        # this name either. So this order needs no city-collision guard the
        # bare word did.
        "oxford university", "cambridge university",
        "london school of economics", "university college london",
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

#: Target-cycle prefixes -> the bucket that cycle is actually made of. Kept
#: for any stored value already in this shape ("SA 2028" — kind, then year).
#: Students who typed it this way, or a legacy row from before the dropdown
#: below existed, must keep parsing.
_CYCLE_BUCKETS: Mapping[str, str] = {
    "sa": INTERNSHIP, "summer analyst": INTERNSHIP, "sum": INTERNSHIP,
    "ft": ENTRY_LEVEL, "full time": ENTRY_LEVEL, "full-time": ENTRY_LEVEL,
    "sw": INSIGHT, "spring week": INSIGHT, "insight": INSIGHT,
}
_CYCLE_RX = re.compile(r"^\s*([A-Za-z][A-Za-z\s-]*?)\s*(20\d\d)\s*$")

# ---------------------------------------------------------------------------
# The OTHER cycle shape: year, then a label ("2028 Summer Internship") — what
# `accounts.forms.ProfileForm`'s `target_cycle` dropdown actually stores. The
# bug this section fixes: the dropdown has always produced this year-first
# shape and `parse_target_cycle` only ever recognised the kind-first "SA
# 2028" shape above, so all eight of the dropdown's own choices scored zero
# on the 15-point cycle axis for every single user.
#
# `CYCLE_LABELS` is now the one place these label strings live.
# `accounts.forms.cycle_choices()` builds its dropdown text from it instead
# of restating the words, so the producer (the settings page) and this
# consumer (the scorer) cannot drift apart the way they did before.
# ---------------------------------------------------------------------------
CYCLE_LABELS: Mapping[str, str] = {
    INTERNSHIP: "Summer Internship",
    ENTRY_LEVEL: "Full-Time / Graduate",
    INSIGHT: "Spring Week / Insight",
}
#: How many years past `base_year` the dropdown offers a choice for, per
#: bucket. Also read by `accounts.forms.cycle_choices` so the two stay in
#: lockstep; order here is display order.
CYCLE_YEAR_SPAN: Mapping[str, tuple[int, ...]] = {
    INTERNSHIP: (0, 1, 2),
    ENTRY_LEVEL: (0, 1),
    INSIGHT: (0, 1),
}
#: The one choice with no attached year: off-cycle recruiting has no fixed
#: intake. Rather than leaving it unparseable (the same silent-zero bug as
#: above, just for a ninth value), it scores against "as soon as possible" —
#: the current year, read at PARSE time so this never goes stale.
OFF_CYCLE_LABEL = "Off-Cycle / Immediate"
OFF_CYCLE_BUCKET = ENTRY_LEVEL

_CYCLE_LABEL_TO_BUCKET: Mapping[str, str] = {v: k for k, v in CYCLE_LABELS.items()}
_YEAR_FIRST_CYCLE_RX = re.compile(r"^\s*(20\d\d)\s+(.+?)\s*$")


def cycle_choices(*, base_year: int | None = None) -> list[tuple[str, str]]:
    """The full `target_cycle` dropdown vocabulary, anchored to `base_year`
    (today's year by default) — the single source `accounts.forms.
    ProfileForm` builds its Select choices from, so this parser's vocabulary
    and the form's can never restate the same strings twice and drift.

    Deliberately a function, not a module-level constant computed once: a
    module-level `_YEAR = date.today().year` goes stale in a long-lived
    worker process (a Django dev/prod server started in December still
    serving last year's choices in July). Call this at the point you need
    the choices, not at import time."""
    base_year = base_year or date.today().year
    choices: list[tuple[str, str]] = [("", "Select a cycle")]
    for bucket, offsets in CYCLE_YEAR_SPAN.items():
        label = CYCLE_LABELS[bucket]
        for offset in offsets:
            value = f"{base_year + offset} {label}"
            choices.append((value, value))
    choices.append((OFF_CYCLE_LABEL, OFF_CYCLE_LABEL))
    return choices


def parse_target_cycle(target_cycle: str) -> tuple[str, int] | None:
    """"2028 Summer Internship" -> ("internship", 2028); the legacy "SA 2028"
    shape still parses too. Unrecognised text -> None.

    Returning None rather than a partial guess matters: an unparsed cycle must
    cost the student nothing, not silently match every 2028 posting."""
    text = (target_cycle or "").strip()
    if not text:
        return None
    if text == OFF_CYCLE_LABEL:
        return OFF_CYCLE_BUCKET, date.today().year
    m = _YEAR_FIRST_CYCLE_RX.match(text)
    if m:
        bucket = _CYCLE_LABEL_TO_BUCKET.get(m.group(2).strip())
        return (bucket, int(m.group(1))) if bucket is not None else None
    m = _CYCLE_RX.match(text)
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
    #: One or more `cycle_choices()` labels — a student can be recruiting for
    #: more than one programme at once (e.g. an Insight week this year AND
    #: next year's SA cycle), so this is plural, not a single string.
    target_cycles: tuple[str, ...] = ()
    school: str = ""
    regions: tuple[str, ...] = ()
    tracks: tuple[str, ...] = ()
    #: firm_id -> tier (1/2/3, or None for "targeted but untiered"). Comes from
    #: `crm.UserFirm.objects.for_user(user)` — the caller does the tenant
    #: scoping; this module never queries.
    firm_tiers: Mapping[int, int | None] = field(default_factory=dict)
    #: firm_id -> the WARMEST live relationship the student has there:
    #: "warm" (chatted/advocate) or "replied". Unarchived contacts only —
    #: the caller queries and collapses; this module never touches the ORM.
    warm_firms: Mapping[int, str] = field(default_factory=dict)

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
            self.class_year, self.target_cycles, self.school.strip(),
            self.regions, self.tracks, self.firm_tiers, self.warm_firms,
        ))

    @classmethod
    def from_user(cls, user, firm_tiers: Mapping[int, int | None] | None = None,
                  warm_firms: Mapping[int, str] | None = None) -> "Profile":
        return cls(
            class_year=_int_or_none(getattr(user, "class_year", None)),
            target_cycles=tuple(
                v.strip() for v in (getattr(user, "target_cycles", None) or []) if v and v.strip()
            ),
            school=getattr(user, "school", "") or "",
            regions=tuple(r.lower() for r in (getattr(user, "regions", None) or [])),
            tracks=tuple(getattr(user, "tracks", None) or []),
            firm_tiers=dict(firm_tiers or {}),
            warm_firms=dict(warm_firms or {}),
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
    #: The eligibility lens' blocking verdict for THIS student — the posting
    #: states a graduation window that excludes them, or refuses the visa they
    #: need in the market it names. Set by the caller, which owns the profile
    #: pairing (directory.views._eligibility); this module stays pure.
    #:
    #: A blocked role can never be a pick. The feed's "Eligible only" filter
    #: already hides these, and the bar recommending what the filter hides is the
    #: product contradicting itself: the first live audit of Jimmy's picks had
    #: FIVE of six carrying a blocking verdict — three wrong-year, two
    #: won't-sponsor — every one of them a role he cannot get, ranked top of
    #: the page as the best thing on the board for him.
    blocked: bool = False
    #: The graduation window the posting states in its own BODY — the facts
    #: extractor's `grad.years` (evidence-phrase-backed), flattened to year
    #: strings. The second live audit found the scorer blind to this: the
    #: eligibility lens read it and issued verdicts on it, while ranking saw
    #: only the title-derived `class_year` column — so SIG's Discovery
    #: Program, whose text states "graduate in the winter of 2028 or the
    #: spring of 2029" and which carried a real November deadline, scored 26
    #: for the 2029 student it names and ranked below fifteen prior-cycle
    #: internships that merely failed to exclude him. Two features reading
    #: the same fact and disagreeing about it is the exact inconsistency the
    #: year facet was rebuilt to prevent; this field closes the same gap for
    #: ranking.
    grad_years: tuple[str, ...] = ()

    @classmethod
    def from_opportunity(cls, o, *, blocked: bool = False) -> "Candidate":
        return cls(
            id=o.id, firm_id=o.firm_id, firm_name=o.firm.name,
            firm_slug=o.firm.slug, title=o.title, url=o.url,
            bucket=o.bucket or "", cohort=o.cohort or "",
            class_year=o.class_year or "", region=o.region or "",
            location=o.location or "",
            firm_tracks=tuple(o.firm.tracks or []),
            deadline=o.deadline,
            blocked=bool(blocked),
            grad_years=tuple(
                y for y in (((o.raw or {}).get("facts") or {})
                            .get("grad", {}).get("years") or ())
                if isinstance(y, str) and y.isdigit()
            ),
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

def _stated_grad_window(profile: Profile, c: Candidate) -> tuple[int, int] | None:
    """The graduation window this posting STATES in its own words, as an
    inclusive `(lo, hi)`, or None when it states none — or when the student
    has no class year for it to be a statement ABOUT.

    Both places a posting can say it: the title ("Class of 2028" ->
    `Opportunity.class_year`) and the body ("graduate in the winter of 2028 or
    the spring of 2029" -> the facts extractor's `grad.years`). A single year
    is returned as the degenerate window `[y, y]` so one code path serves both.

    Factored out of `_class_fit` so `stated_class_mismatch` below cannot drift
    from the scoring that produced it — the two ARE the same question, and two
    features reading the same fact and disagreeing about it is precisely what
    the `grad_years` field was added to stop."""
    if not profile.class_year:
        return None
    stated = _int_or_none(c.class_year)
    if stated is not None:
        return (stated, stated)
    ys = sorted(int(y) for y in c.grad_years if str(y).isdigit())
    return (ys[0], ys[-1]) if ys else None


def stated_class_mismatch(profile: Profile, c: Candidate) -> bool:
    """True when the posting's OWN WORDS name a graduating class and this
    student is not in it. Never inference: a programme year that merely
    IMPLIES a class (`cohort`) is not a statement and never reaches here."""
    window = _stated_grad_window(profile, c)
    return window is not None and not (window[0] <= profile.class_year <= window[1])


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

    # The posting's own words about who it is for, from either place it can
    # say them: the title ("Class of 2028" -> the `class_year` column) or the
    # body ("graduating between December 2027 and June 2028" -> the grad
    # fact). Both are statements; both bind. A window is checked by
    # containment, a single year by equality — `_stated_grad_window` serves
    # both by returning the single year as a [y, y] window.
    window = _stated_grad_window(profile, c)

    if window is not None and profile.class_year:
        lo, hi = window
        label = str(lo) if lo == hi else f"{lo}–{hi}"
        if not (lo <= profile.class_year <= hi):
            # The posting named a different class. This is a veto, not a
            # subtraction: no other class/cycle evidence gets to argue with the
            # firm's own words, so nothing below runs.
            # The chip text must not be byte-identical to the MATCH branch six
            # lines below: `Recommendation.why` joins on `.text` alone, and a
            # mismatch chip reading "Class of {stated}" typeset a reason
            # AGAINST the role as if it were a reason FOR it. Only the
            # tooltip differed before this fix.
            return W_CLASS_STATED_MISMATCH, [Reason(
                f"Not Class of {label}",
                f"The posting states it is for {label} graduates, "
                f"not your {profile.class_year}.",
                "class",
            )]
        points += W_CLASS_STATED
        reasons.append(Reason(
            f"Class of {label}" if lo == hi else f"For {label} grads — you",
            f"The posting itself states it is for {label} graduates, "
            f"which includes your {profile.class_year}.",
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

    # Bonus applies ONCE even when several of the student's selected cycles
    # would match (recruiting for both an Insight week and next year's SA
    # cycle doesn't make one particular SA posting twice as relevant) — the
    # first matching label, in the student's own selection order, is what
    # the reason chip names.
    if cohort is not None:
        for raw in profile.target_cycles:
            cycle = parse_target_cycle(raw)
            if cycle is None:
                continue
            bucket, year = cycle
            if c.bucket == bucket and cohort == year:
                points += W_CYCLE
                reasons.append(Reason(
                    raw.strip(),
                    f"This is a {year} {c.bucket.replace('_', ' ')} — the "
                    f"{raw.strip()} cycle you're recruiting for.",
                    "class",
                ))
                break
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


#: A role whose TITLE names its own function, mapped to the track vocabulary.
#: Checked before the firm's tracks, because a bank that covers IB also runs
#: an audit department, a technology division and a compliance team, and
#: scoring every one of its roles as an IB match is how "2027 Internal Audit
#: Analyst Program" arrived as the top pick for a student recruiting for IB,
#: S&T and PE. Longest patterns first so "investment banking" wins over
#: "banking" and "quantitative research" over "research".
_ROLE_FUNCTION: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rx, re.I), track) for rx, track in (
        (r"\binvestment bank(ing)?\b|\bm&a\b|\bmergers? (and|&) acquisitions?\b"
         r"|\bleveraged finance\b|\bcapital markets?\b|\bcoverage bank(er|ing)\b", "ib"),
        (r"\bsales (and|&) trading\b|\btrading\b|\btrader\b|\bmarkets? division\b"
         r"|\bequities?\b|\bfixed income\b|\bfx\b|\bcommodities\b|\bstructuring\b"
         r"|\bquantitative (analysis|research|trading|strateg)", "st"),
        (r"\bprivate equity\b|\bbuyout\b|\bgrowth equity\b|\bprivate capital\b"
         r"|\bprivate markets?\b|\binfrastructure investing\b", "pe"),
        (r"\basset management\b|\bwealth management\b|\bportfolio management\b"
         r"|\binvestment management\b|\bmulti-?asset\b", "am"),
        (r"\bconsult(ing|ant)\b|\bstrategy (and|&) operations\b|\badvisory\b", "consulting"),
        (r"\bcorporate strateg(y|ic)\b|\bbusiness development\b", "corp-strat"),
    )
)

#: Functions that are NOT any of the tracks Coverage lets a student pick.
#: A role naming one of these is a support or control function: real work,
#: and not what someone recruiting for IB/S&T/PE is looking at. Detecting it
#: is what lets the scorer decline to claim a track match rather than
#: inheriting one from the firm.
_NON_TRACK_FUNCTION = re.compile(
    r"\b(internal )?audit(or|ing)?\b|\bcompliance\b|\btax\b|\blegal\b"
    r"|\bhuman resources\b|\bpeople (team|operations)\b|\brecruit(ing|ment)\b"
    r"|\bmarketing\b|\bcommunications?\b|\bfacilit(y|ies)\b"
    r"|\bsoftware engineer(ing)?\b|\bdeveloper\b|\bcyber ?security\b"
    r"|\bnetwork engineer\b|\bhelp ?desk\b|\bit support\b"
    r"|\boperations?\b|\bback ?office\b|\bmiddle ?office\b"
    # Retail branch network: "Branch Manager", "Relationship Banker |
    # Meadowbrook Branch", "Branch Relationship Manager, Consumer Banking".
    # 229 open rows carried this and NONE of them named a track, so every one
    # inherited its bank's firm-level coverage and scored as an IB match —
    # which is how a Jackson, Tennessee branch role reached the top of a
    # US/IB student's day-one brief. Deliberately NOT `\bretail\b`: that is
    # also an IB COVERAGE GROUP, and banning it would misread real
    # "Investment Banking — Consumer & Retail" analyst roles as non-track.
    r"|\bbranch\b"
    r"|\brisk management\b|\bcredit risk\b|\bmodel validation\b"
    r"|\baccounting\b|\bfinancial report(ing)?\b|\bprocurement\b"
    # Internal technology department, named the way the department names
    # itself, never a bare `\btechnology\b`: that word is ALSO an IB coverage
    # sector ("Investment Banking Associate - Technology" at Solomon
    # Partners, "M&A intern - ... Technology team" at Lazard — 6 live rows,
    # all with the track stated elsewhere in the same title). The five
    # phrases below are the department's own job-title and org-name
    # vocabulary, never a sector suffix: on the live board every one of them
    # is an internal engineering/infra function with no track word anywhere
    # in the title — "2027 Technology Summer Analyst Program" (Morgan
    # Stanley, six cities), "Global Technology Summer Analyst" (Bank of
    # America, four rows), "Group Technology Office" (UBS, four rows),
    # "Technology Process Analysis" / "Global Technology Governance Intern"
    # (Deutsche Bank), "Global Technology & Engineering Analyst" (SocGen) —
    # 55 silent rows in total, every one of them at a firm whose `Firm.
    # tracks` includes ib and/or st, so every one scored as a track match by
    # inheritance. Checked against the whole open+closed board (25,294
    # rows): the only titles this phrasing touches that also state a track
    # elsewhere are three ambiguous "embedded tech within the business line"
    # roles (e.g. "Investment Banking Technology Analyst") and two internal
    # "Technology Solutions Consultant" titles at Vanguard/Baird, none of
    # them a clean front-office match to begin with — the same "co-occurring
    # non-track word, decline rather than guess" call this file already
    # makes for "Trading Operations Analyst".
    r"|\btechnology\s+(?:summer\s+analyst|analyst|associate|internship|intern)\b"
    r"|\btechnology\s+(?:office|infrastructure|process|governance|solutions?)\b"
    r"|\bgroup\s+technology\s+office\b"
    r"|\btechnology\s+(?:&|and)\s+engineering\b"
    # Corporate Treasury / Corporate Planning: the bank's own balance-sheet
    # and internal-planning functions, never a track. "Corporate Treasury"
    # is unambiguous on the live board — 7 rows, all at Goldman, Morgan
    # Stanley or Ares, none with a track word elsewhere in the title — unlike
    # bare `\bcorporate\b`, which is also how boutiques name their IB
    # division ("Corporate Finance" at Houlihan Lokey, "Corporate Advisory"
    # at Goldman and Citi, "Corporate Banking" at RBC and JPMorgan) and so
    # stays deliberately OUT of this list. "Corporate Infrastructure" is the
    # same call for Nomura's "2026 Insight Day: Corporate Infrastructure" —
    # ranked the #1 pick on the founder's own live profile ahead of every
    # dated Morgan Stanley and HSBC internship on the board, for a division
    # whose own event description says "these are the teams that power and
    # support our business every day."
    r"|\bcorporate treasury\b|\bcorporate planning\b|\bcorporate infrastructure\b",
    re.I)


#: Phrases that name the HIRING PROCESS, not the job. "2027 Campus Recruiting
#: - Investment Banking Summer Associate - Houston" is an investment banking
#: posting whose title happens to carry the name of the programme that posts
#: it; `\brecruit(ing|ment)\b` above read that as "this is an HR job" and
#: returned "none" for all five of Piper Sandler's open IB summer associate
#: requisitions — the single most on-track thing on the board for a student
#: recruiting IB, scored as a support function.
#:
#: Deliberately only the ATTRIBUTIVE forms ("campus recruiting", "recruitment
#: event"), never a bare "recruiting": "Campus Recruiting Coordinator" and
#: "Recruiter Intern" really are HR jobs and must keep answering "none". The
#: exemption in `role_function` is gated on the title separately naming a
#: track outright, so a title that is ONLY hiring-process words never reaches
#: it.
_HIRING_PROCESS = re.compile(
    r"\b(campus|graduate|early[\s-]careers?)\s+recruit(ing|ment)\b"
    r"|\brecruit(ing|ment)\s+(event|day)s?\b",
    re.I)


def _track_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Every span in `text` covered by a phrase that names a track — all
    patterns, all matches, not just the first. `_stated_track` answers WHICH
    track; this answers WHERE the evidence for any of them sits."""
    return tuple(m.span() for rx, _ in _ROLE_FUNCTION for m in rx.finditer(text))


def _stated_track(text: str) -> str:
    """The first track `text` names, "" if none. Pattern order is
    `_ROLE_FUNCTION`'s, longest-phrase-first by construction."""
    for rx, track in _ROLE_FUNCTION:
        if rx.search(text):
            return track
    return ""


def _names_non_track(text: str) -> bool:
    """Whether `text` names a function OUTSIDE the track vocabulary.

    A blocklist word lying INSIDE a phrase that names a track is not a second,
    competing claim about the function — it is one word of a longer and more
    specific one. `\\boperations?\\b` sits inside the consulting pattern's own
    `\\bstrategy (and|&) operations\\b`, so that clause could never fire: the
    author wrote the rule, the blocklist ran first, and every "Consulting -
    Associate - Strategy & Operations" on the board came back "none" (8 open
    campus rows at PwC and Deloitte, plus "Consulting - Finance Strategy &
    Operations Internship"). Strategy & Operations is a flagship consulting
    practice, not an ops job.

    This is the same reasoning the `\\bbranch\\b` note above already applies to
    `\\bretail\\b` — a word can be a support function in one title and part of
    a front-office phrase in another — generalised so it does not have to be
    re-litigated per word. It can only ever IGNORE a blocklist hit that the
    track vocabulary itself already spans, so it cannot admit a function no
    pattern here names: "Trading Operations Analyst" still answers "none"
    (`\\btrading\\b` spans "Trading", not "Operations"), and so does "2027
    Commercial & Investment Bank Risk Management Summer Analyst".
    """
    spans = _track_spans(text)
    return any(
        not any(lo <= m.start() and m.end() <= hi for lo, hi in spans)
        for m in _NON_TRACK_FUNCTION.finditer(text)
    )


def role_function(title: str) -> str:
    """The track this role's own title names, "" if it names none, or "none"
    if it names a function outside the track vocabulary entirely.

    The FUNCTION is checked before the DIVISION, because a title routinely
    carries both and the function is the job: "2027 Commercial & Investment
    Bank Risk Management Summer Analyst" sits in the investment bank and is a
    risk role, and reading the division first ranked it as an IB match for a
    student recruiting IB. Where you sit is not what you do.

    Two things are NOT a competing claim about the function, and both were
    costing real front-office roles their track: a blocklist word that is part
    of a longer track phrase (`_names_non_track`), and a blocklist word that
    only names the hiring process (`_HIRING_PROCESS`). The second is exempted
    only when the title separately names a track outright — a title made of
    hiring-process words alone ("Campus Recruiting Coordinator") still answers
    "none", because there is nothing else in it to be the job."""
    text = title or ""
    track = _stated_track(text)
    if not _names_non_track(text):
        return track
    if track and _HIRING_PROCESS.search(text):
        rest = _HIRING_PROCESS.sub(" ", text)
        if _stated_track(rest) == track and not _names_non_track(rest):
            return track
    return "none"


def role_matches_tracks(title: str, tracks) -> bool:
    """Whether a role's OWN TITLE **states** one of the tracks a student is
    recruiting for. An ALLOWLIST, not a blocklist — the role has to say it.

    Exists because the two "what's new at your firms" surfaces — Today's card
    (`crm.today._new_at_your_firms`) and the advisor's situation snapshot
    (`assistant.situation._new_role_events`) — select purely on the FIRM.
    That is right for the firm axis and wrong for everything on top of it: a
    student who tiers a universal bank is tiering its investment bank, and
    the same firm also posts branch, audit and helpdesk reqs. So the day-one
    brief could open by telling a US/IB student to apply to a retail branch
    role in Jackson, Tennessee — confidently, in the first thing they ever
    read from this product.

    WHY AN ALLOWLIST, AND NOT THE THREE-CASE RULE `_track_fit` RANKS WITH.
    The first cut of this function let a silent title through ("the firm is
    already theirs; the title makes no claim to contradict") and leaned on
    `_NON_TRACK_FUNCTION` to catch the rest. That rule shipped, and the same
    Jackson, Tennessee requisition (opportunity 22872) came straight back as
    the hero card for the same US/IB/2028 profile — because its title is the
    single word "Intern", so there was never a phrase for a blocklist to
    match. Measured on the live board for that profile the week after:
    **33 rows survived every filter and 2 of them named investment banking.**
    The other 31 were Engineering, Risk, Controllers, Corporate Treasury,
    Human Capital Management, Media Relations, Conflicts Resolution — every
    one of them silent on the track axis and every one of them inheriting a
    firm's coverage it had nothing to do with. A blocklist of function words
    will always lag the market's title vocabulary; the market invents a new
    department name faster than this regex gets edited.

    WHY THE FIRM'S OWN COVERAGE CANNOT RESCUE THE SILENT CASE. The obvious
    escape hatch is "let a silent title through when the FIRM is narrowly
    tracked" — a one-line IB boutique's unlabelled "Summer Analyst Program"
    really is an IB role. It does not survive contact with the data:
    `Firm.tracks` tops out at TWO entries across the whole board, and Morgan
    Stanley — a universal bank running a retail branch network in Jackson,
    Tennessee — carries exactly `["ib", "st"]`, the same shape a two-desk
    boutique would. There is no field on `Firm` that distinguishes "does
    only this" from "does this among forty other things", so any
    firm-narrowness test would readmit requisition 22872 for any student
    recruiting IB and S&T. Until the model can tell those two firms apart,
    the honest answer is to require the ROLE to speak for itself.

    THIS IS NOT THE RANKING RULE, ON PURPOSE. `_track_fit` still scores a
    silent title off `Firm.tracks` (capped at `W_TRACK_CAP`, and its reason
    chip says "matches IB" with a tooltip naming the FIRM as the source).
    That is fine for a feed row, where track is one of five axes, the score
    is visible, and the student is browsing. It is not fine here: these two
    surfaces announce "this is new AND you should look at it right now",
    which is a much stronger claim than a ranked row, so it carries a much
    higher bar of evidence. Callers that want ranking parity should score
    with `score_candidate`, not filter with this.

    A student who has stated NO tracks gets no filtering: there is nothing to
    be relevant to, and everything is equally (ir)relevant.

      title names a track they want   -> True
      title names a track they don't  -> False
      title names a non-track         -> False  (audit, ops, branch, ...)
        function
      title names nothing at all      -> False  (the role has not earned
                                                 "relevant"; the caller says
                                                 so out loud — see
                                                 `_new_at_your_firms`)
      student stated no tracks        -> True   (nothing to filter to)
    """
    wanted = set(tracks or ())
    if not wanted:
        return True
    fn = role_function(title)
    return bool(fn) and fn != "none" and fn in wanted


def role_matches_regions(region: str, regions) -> bool:
    """Whether a role's OWN `Opportunity.region` is compatible with the
    regions a student stated. Same posture as `role_matches_tracks`: a
    student who has named NO regions gets no filtering.

    Deliberately reads the ROLE's region, not the firm's `Firm.regions`
    list — a firm can run desks in five markets and post a role in only
    one of them, and the firm-level list is what let a Pune, India ops
    role read as relevant to a Hong Kong/US student who had merely tiered
    the bank. `directory.views._apply_region_filter` and
    `accounts.onboarding_preview._matching` are the two places the product
    already filters listings by region, and both filter on this same
    field for the same reason.

    BLANK REGION — the honest case, not the rare one: 2,249 of ~21,700 rows
    board-wide resolve to no region at all, because the location string
    didn't parse. The instinct is to let it pass unfiltered ("the firm is
    already theirs; an unknown location makes no claim"), mirroring how
    `role_matches_tracks` treats a silent title. But that is NOT what the
    product already does with a stated region preference: both
    `_apply_region_filter` (`region__iexact=region`) and `_matching`
    (`region__in=answers["regions"]`) exclude blank rows the moment a
    specific region is asked for — a blank only ever surfaces again under
    the *explicit* "Unstated" facet, never for free inside an ordinary
    region filter. Diverging here would make this the one surface on the
    board where "you said Hong Kong" quietly includes rows nobody can
    place. So a blank region FAILS once the student has stated any region,
    same as the feed's own filter would drop it. `region_matches_tracks`'s
    silent-title case is not a counterexample to this: a title's silence
    still leaves the FIRM's own stated coverage to speak for the role,
    while a blank region has no fallback source of truth to defer to at
    all — nothing else on the row claims a place.

      role names a region they want     -> True
      role names a region they don't    -> False  (including "other" and
                                                    "global": stated, just
                                                    not one of theirs)
      role names no region (blank)      -> False  (see above)
      student named no regions          -> True   (nothing to filter to)
    """
    wanted = set(regions or ())
    if not wanted:
        return True
    return bool(region) and region in wanted


def role_matches_level(
    bucket: str,
    class_year_derived: str,
    target_cycles,
    profile_class_year: int | None,
) -> bool:
    """Whether a role's own LEVEL — its programme bucket, and the class year
    its shape implies — is compatible with the level a student is actually
    recruiting at. Same posture as the other two filters: nothing stated
    (by either side) means nothing is filtered.

    Exists for the failure the track and region filters do not cover: a
    role can name the student's exact track, sit in one of their regions,
    and still be the wrong RUNG of the ladder — a full-time "New Associate"
    role, or a programme whose intake year implies a graduating class years
    off from the student's, surfacing as day-one news for an undergrad
    sophomore who is nowhere near either. Two independent checks, either of
    which can fail a role:

    1. BUCKET vs. the buckets the student's `target_cycles` name
       (`parse_target_cycle`, same parser `_class_fit`'s cycle bonus uses).
       A sophomore who has only ever picked Summer Internship cycles has
       told the product it is not recruiting for `entry_level` roles yet —
       an honest reading of their own stated plan, not a guess about their
       year. Cycles that fail to parse are ignored rather than treated as
       "nothing stated": a student who typed something is still a student
       who said SOMETHING, just not something this parses, and a role
       should not be excluded on the strength of a parse failure.

    2. The DERIVED class year (`Opportunity.class_year_derived`, from
       `classify.derive_class_year` — a convention, never the posting's own
       words) against the student's stated `class_year`, using the exact
       gap `_class_fit` already scores: 0 is a match, 1 is "worth a look"
       (near, not excluded), 2+ is excluded here — `_class_fit` already
       scores a gap that wide as zero, so nothing ranks it up today; this
       makes the same judgement into "and don't call it news" for a card
       that exists to say "you should look at this right now."

    Deliberately does NOT duplicate `directory.views._eligibility`'s
    BLOCKING verdict (a stated class year or extracted grad-window that
    excludes this student outright) — that is a harder, title/body-STATED
    fact and the caller applies it separately, the same verdict
    `Candidate.blocked` already reads elsewhere. This function only ever
    acts on the softer, INFERRED signals a role's shape carries, and never
    on nothing: a role with no derivable year and a student with cycles
    this can't place both pass.
    """
    if bucket and target_cycles:
        wanted_buckets = set()
        for raw in target_cycles:
            parsed = parse_target_cycle(raw)
            if parsed is not None:
                wanted_buckets.add(parsed[0])
        if wanted_buckets and bucket not in wanted_buckets:
            return False
    if class_year_derived and profile_class_year:
        derived = _int_or_none(class_year_derived)
        if derived is not None and abs(derived - profile_class_year) >= 2:
            return False
    return True


def _track_fit(profile: Profile, c: Candidate) -> tuple[int, list[Reason]]:
    """Industry preference — the ROLE's function first, the firm's coverage
    only where the role is silent.

    Track used to be read purely off `Firm.tracks`, which made it a property
    of the employer rather than the job: every opening at a bank that covers
    IB scored as an IB match, so an Internal Audit programme and an M&A
    programme were indistinguishable on this axis. Three cases now:

      the title names one of the student's tracks  -> full points, named
      the title names a function outside the       -> nothing, and no claim
        track vocabulary (audit, ops, tech, HR)
      the title names nothing                      -> the firm's coverage,
        ("Summer Analyst Program")                    as before
    """
    fn = role_function(c.title)
    if fn == "none":
        # The role said what it is and it is not one of these tracks. Claiming
        # "matches IB" here would be the card lying about the job.
        return 0, []
    if fn:
        if fn not in profile.tracks:
            return 0, []
        name = TRACK_SHORT.get(fn, fn.upper())
        return W_TRACK_STATED, [Reason(
            f"{name} role",
            f"The posting itself is a {name} role, which you're recruiting for.",
            "track",
        )]
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
def _network_fit(profile: Profile, c: Candidate) -> tuple[int, list[Reason]]:
    """The student's own relationships at the firm — the CRM half of the
    product, finally allowed to argue with the listings half.

    Chip text says what the relationship IS, never who it is with: the
    reasons render on a shared board surface, and a contact's name does not
    belong in a scoring chip. The tooltip points at the firm page, where
    "Your Network Here" already names names behind the login."""
    warmth = profile.warm_firms.get(c.firm_id)
    if warmth == "warm":
        return W_NETWORK_WARM, [Reason(
            "You know someone here",
            "You've already talked with someone at this firm — see Your "
            "Network Here on the firm page. A warm intro beats a cold "
            "application.",
            "network",
        )]
    if warmth == "replied":
        return W_NETWORK_REPLIED, [Reason(
            "Warm-ish contact here",
            "Someone at this firm has replied to you. Worth building on "
            "before the deadline gets close.",
            "network",
        )]
    return 0, []


_AXES = (_tier_fit, _track_fit, _region_fit, _class_fit, _network_fit)


def score_candidate(profile: Profile, candidate: Candidate) -> tuple[int, tuple[Reason, ...]]:
    """Score one role for one student. Pure: same inputs, same output."""
    total = 0
    reasons: list[Reason] = []
    for axis in _AXES:
        points, why = axis(profile, candidate)
        total += points
        reasons.extend(why)
    return total, tuple(reasons)


def _sort_key(rec: Recommendation, today: date | None = None):
    """Total order, with no ties left to chance.

    Score first; then a PASSED deadline last of all, then the soonest real
    deadline (rolling roles after dated ones, because a dated role at the same
    score is the one that can actually be missed); then firm name and id,
    which are stable and unique.

    The passed-deadline term is not decoration. Sorting on `d or date.max`
    alone is ascending by date, so an expired role — having the earliest date
    of all — sorted FIRST among equal scores. `recommend` excludes expired
    candidates outright, and this keeps the order right anyway for any caller
    that scores without that exclusion."""
    d = rec.candidate.deadline
    today = today or date.today()
    return (
        -rec.score,
        bool(d and d < today),
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
    today: date | None = None,
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
    today = today or date.today()
    out = []
    for c in candidates:
        # Never rank what the student cannot have. This is a hard exclusion,
        # not a penalty: no combination of tier, track and region should be
        # able to outweigh the posting saying out loud that this person is
        # not who it is for.
        if c.blocked:
            continue
        # Nor anything whose own words name a graduating class this student is
        # not in. `W_CLASS_STATED_MISMATCH` claims in its own comment to be
        # "large negative so it cannot be outrun by a tier-1 firm the student
        # happens to like", and arithmetic says otherwise: tier 1 (26) + a
        # stated track (26) + the school's own market (20) + a warm contact
        # (14) - 25 = 61, well clear of MIN_SCORE, so a posting that says out
        # loud "this is for the Class of 2027" could be recommended to a 2029
        # student on the strength of knowing somebody there. Today the view
        # layer filters those rows out before scoring (`Candidate.blocked`,
        # from `views._eligibility`), which is why no live pick has ever shown
        # it — a guarantee that lives entirely in the CALLER, for a promise
        # this module makes about itself. Measured over all 2,599 open campus
        # rows against the founder's profile, this exclusion and that filter
        # disagree on zero rows; it is defence in depth for the next caller
        # that scores without one, not a change of behaviour.
        #
        # A veto, deliberately, and only ever on the posting's STATED words —
        # a programme year that merely implies a class still scores, still
        # says "likely", and still shows. Silence never hides.
        if stated_class_mismatch(profile, c):
            continue
        # Nor anything whose deadline has already passed. A listing may
        # honestly stay on the board after its date — the firm still lists it,
        # and Coverage does not close what the source has not — but a PICK is
        # the product pointing at something and saying "do this one", and
        # there is nothing to do about a closed application. Two HSBC roles
        # reached ranks 3 and 4 this way, boosted by the network axis, on
        # dates that turned out to be wrong; the fix for the dates does not
        # make it right to recommend a real one.
        if c.deadline and c.deadline < today:
            continue
        score, reasons = score_candidate(profile, c)
        if score >= min_score:
            out.append(Recommendation(candidate=c, score=score, reasons=reasons))
    out.sort(key=lambda r: _sort_key(r, today))

    picked: list[Recommendation] = []
    seen: dict[int, int] = {}
    for rec in out:
        # Checked BEFORE the append, not after it. `if len(picked) >= limit:
        # break` at the foot of the loop tests a list that has already grown,
        # so `limit=0` returned one recommendation and `limit=-1` returned one
        # too — a caller asking for nothing got something. `max_per_firm` is
        # already floored correctly (its check precedes its own increment),
        # which is why only this one was wrong.
        if len(picked) >= limit:
            break
        n = seen.get(rec.candidate.firm_id, 0)
        if n >= max_per_firm:
            continue
        seen[rec.candidate.firm_id] = n + 1
        picked.append(rec)
    return picked
