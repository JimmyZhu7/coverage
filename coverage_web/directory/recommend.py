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
- It never guesses a region it cannot justify. An unknown school scores zero
  on that axis rather than being optimistically matched; an unlocated role
  scores zero for a student who named no regions and a small, labelled
  penalty for one who did — a charge against the product's own ignorance,
  never a claim that the role is in the wrong place (see `W_REGION_UNKNOWN`).

Entry points: `Profile`, `Candidate`, `score_candidate`, `recommend`. None of
them touch a request, a session, or the ORM, so the whole ranking is testable
with plain objects.

---------------------------------------------------------------------------
WHAT THE WEIGHTS ARE ACTUALLY WORTH — first measurement, 2026-09-02
---------------------------------------------------------------------------

Until this section every one of the sixteen weights below was justified by
argument alone, four of them by arithmetic about the other weights and one
(`MAX_PER_FIRM`) by a single browser observation
(`audit-personalization-opportunities.md §Q8`). P6 requires each to say what
would change it. `manage.py calibrate_recommender --email <account>` is that
question made runnable: it scores every role the student saved, applied to or
dismissed, prints the per-axis contribution and the rank the role would have
held on the whole open board, and ends with a per-axis verdict.

THE SAMPLE. The founder's account, n=18 `UserOpportunity` rows: 4 applied,
1 saved, 13 dismissed, scored against 2,737 open campus roles. That is the
entire labelled dataset this product has. Read everything below against it.

MEASURED, per axis (acted-on mean vs dismissed mean):

  track_fit    +16.4 vs  +4.0   (n=5 vs 13)  the only axis that separates
  tier_fit      +8.4 vs  +7.2   (n=5 vs 13)  no separation
  region_fit    +1.6 vs  +3.7   (n=5 vs 13)  points the WRONG way
  network_fit   +1.4 vs  +3.2   (n=5 vs 13)  points the WRONG way
  class_fit    constant at +30 across all 18 rows; unmeasurable here

WHAT THAT DOES AND DOES NOT LICENCE.

- `W_TRACK_*` is the one family with any measured support: the roles he acted
  on scored four times higher on track than the ones he threw away, and it is
  the only axis whose sign matches its intent. Five positives is not a
  calibration, but it is evidence, and it is more than any other axis has.
- `W_CLASS_*` has NO measured justification from this sample and cannot get
  one from it: every row scored +30, because his class year is stated and
  every candidate he looked at was silent about theirs. The axis is
  constant, so it can be neither confirmed nor refuted here. It stays at its
  argued value.
- `TIER_POINTS`, `W_REGION_*` and `W_NETWORK_*` have no measured
  justification either, and two of them point the wrong way on this sample.
  Do NOT read that as "lower them": the sample is 18 rows and the dismissals
  are dominated by one behaviour (thirteen quant and non-track roles he
  cleared off the board in a batch), which is exactly the shape that would
  produce a spurious sign. It is a reason to keep measuring, not a reason to
  retune.
- The two live consequences named in the audit are unaffected by this
  measurement and still stand as arguments: tier 1 plus warm (40) outweighs
  the entire positive range of the class axis (0 to 30), and
  `TIER_POINTS[1] >= MIN_SCORE`, so a row scoring on tier, region and network
  alone clears the bar with `role_function` "none".

WHAT WOULD CHANGE ANY OF THIS: more labelled rows. The command is read-only
and re-runnable, so the honest cadence is to run it again at n=50 and n=200
and see which of these lines survive. Until one does, a weight below whose
comment does not cite a measurement is still unjustified, and its comment
should say so rather than sound confident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Mapping, Sequence

from directory.classify import (
    ENTRY_LEVEL, INSIGHT, INTERNSHIP, TRACKED_REGIONS, derive_class_year,
    normalize_region, selectable_tracks,
)

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
#:
#: ZERO, NOT 6, WHEN THE STUDENT HAS NAMED THE SAME PROGRAMME IN A DIFFERENT
#: YEAR. Measured 2026-09-01 on the founder's live rail (class 2029, target
#: "2028 Summer Internship"): four of his six picks were 2027 summer
#: internships, each collecting these 6 points and a chip reading "2027
#: intake" as if it were a reason FOR the role. A student who has typed "2028
#: Summer Internship" into Settings has said which intake he is recruiting
#: for, and a 2027 intake of the same programme is by his own words the one
#: he is NOT recruiting for — it is a year early, and he cannot hold a 2027
#: summer seat while graduating in 2029. The chip still prints (as a caveat,
#: see `_class_fit`), because "a year off" is worth knowing; it just no longer
#: scores. The 6 points survive only for a student who named NO cycle for
#: this bucket, where "adjacent" is genuinely the closest thing we know.
#: What would restore the points: evidence that students who named a cycle
#: apply to the prior year's intake of it at any real rate.
W_CLASS_DERIVED_NEAR = 6
#: The role is the exact programme the student named as their target cycle.
W_CYCLE = 15

# 2. University location / reachability.
#: The role sits in the market the student's university is in — the cheapest
#: possible recruiting geography for them (campus pipelines, no relocation).
W_REGION_SCHOOL = 20
#: The role sits in a market the student explicitly named in their profile.
W_REGION_TARGET = 16
#: THE ROW'S REGION IS BLANK and the student HAS named regions. A penalty for
#: the product's own ignorance, not for the role: the location string did not
#: parse (126 of 2,723 open campus rows, 4.6%, on 2026-09-01), so the product
#: cannot say whether this role is in one of the student's markets or in
#: none of them. Until this weight existed an unlocated row scored ZERO on
#: this axis while its located neighbours scored 16-20, which sounds like a
#: penalty already and is not: the founder's #1 pick was Nomura's "2027
#: Discover Nomura Programme" with `region=""` and the words "Location:
#: London" sitting in its own detail text, ranked above every Hong Kong and
#: US role on his board because tier, track and a stated class year made
#: up the 16 it never had to earn. Half of `W_REGION_TARGET` on purpose:
#: enough that an unlocated row can never tie an otherwise-identical row
#: the student's own regions vouch for, small enough that a role with two
#: statements behind it (tier 1 + a stated track = 52) still clears
#: `MIN_SCORE` by a wide margin — "we could not place it" must not hide it,
#: only stop it winning. Applied ONLY when `profile.regions` is non-empty: a
#: student who named no regions has no market for the blank to be wrong
#: about. What would change it: parsing locations out of `detail_text` at
#: ingest (deliberately out of scope here) driving the blank rate under ~1%,
#: at which point this should go back to 0; or blank rows still reaching #1
#: at -8, at which point it should grow.
W_REGION_UNKNOWN = -8
#: THE ROW STATES A MARKET and it is NOT one of the student's. The posting's
#: own words put this job in London, and the student's own profile says Hong
#: Kong and the United States: two statements, and they disagree. Until this
#: weight existed that scored ZERO — the same as a row whose location we never
#: managed to read — so a stated wrong market was literally cheaper than our
#: own ignorance about a right one (`W_REGION_UNKNOWN` is -8). Measured
#: 2026-09-01 on the founder's live rail (hk/us, class 2029): THREE of his six
#: rendered picks were European — Morgan Stanley's Glasgow insight day at 90,
#: and two Bank of America London off-cycles at 89 — each carrying tier 1, a
#: track and a warm contact and paying nothing at all for being in a market he
#: did not name. 908 of his 2,710 open campus rows are `eu` and another 709 are
#: `other`; a free pass on that axis is a free pass on 60% of the board.
#:
#: MINUS `W_REGION_TARGET`, exactly. A market the student named is worth +16,
#: so a market they did not is worth -16 and the swing between them is 32 —
#: wider than any single inferred axis (a firm-coverage track match is at most
#: 20), which is the point: geography is not a tiebreak for a sophomore who
#: cannot fly to London for a first-round. TWICE `W_REGION_UNKNOWN` on purpose,
#: because a stated fact outranks an absent one — "we could not place this
#: role" is a charge against the product and stays small, while "this role is
#: in Europe" is the posting talking about itself and binds.
#:
#: Applied ONLY when `profile.regions` is non-empty (a student who named no
#: markets has no market for the row to be outside of, and scores 0 as before),
#: and only when the row's region is a market a student could have named or the
#: "other" bucket that means a stated location outside all six. NEVER for
#: `global`: a posting that says it has no single place has not stated a market
#: to be wrong about, and penalising it would re-run the `W_REGION_UNKNOWN`
#: mistake in the other direction.
#:
#: What would change it: a measured rate of students applying out-of-market
#: (relocation is real for a US student targeting London), or the mismatch
#: chip turning out to be the only thing standing between a thin-board student
#: and an empty rail — at which point the honest fix is the empty rail, not a
#: smaller number.
W_REGION_MISMATCH = -16

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
#: The title STATES a function that is none of the six tracks — Operations,
#: Technology, Internal Audit, Compliance, a retail branch. Not silence: the
#: posting said what the job is, and it is not the job this student is
#: recruiting for.
#:
#: Zero, until now. `_track_fit` returned "nothing, and no claim" for these,
#: which reads as neutrality and is not: every OTHER axis on a `none` row is a
#: property of the FIRM, so a tier-1 bank the student has a contact at carries
#: its helpdesk req to 62 points on tier + region + warmth alone, with nothing
#: on the track axis to say the job is a helpdesk job. Measured 2026-09-01 on
#: the founder's live board: 676 of 2,710 open campus rows answer `none`, 160
#: of them cleared `MIN_SCORE` for him, and two sat in his top ten — Nomura's
#: "2027 Operations Summer Analyst Program" at 90, second on the rail.
#:
#: MINUS `W_TRACK_CAP`, exactly. 20 is the most an INFERRED track match can
#: earn (the firm covers your tracks, the title is silent), so a title that
#: says Operations must not merely forfeit that 20, it must cost the same
#: again: the swing from "silent at a bank that covers IB" to "states
#: Operations at the same bank" is 40, the whole width of the inherited-track
#: axis, twice. Under `W_TRACK_STATED` (26) in magnitude on purpose — declining
#: to claim a match is a weaker statement than naming one, and a `none` from
#: this blocklist is our reading of a title, not the firm's own taxonomy.
#:
#: A PENALTY AND NOT A SKIP, deliberately, because the blocklist has been wrong
#: before and will be again: `\brecruit(ing|ment)\b` once answered "none" for
#: five Piper Sandler investment banking summer associate reqs, and
#: `\boperations?\b` for every "Consulting - Strategy & Operations" on the
#: board. A false `none` on a row that is tier 1, in the student's market and
#: states their class still scores 26 + 20 + 30 - 20 = 56 and stays visible; a
#: skip would have deleted it with no way for any other evidence to argue.
#: What would change it: `none` rows still reaching a rendered pick at -20, at
#: which point it should grow toward `W_CLASS_STATED_MISMATCH`'s -25.
W_TRACK_NONE = -20

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

#: THE NETWORK AXIS AT A TEST-GATED FIRM. Zero, not a reduced multiplier, and
#: the zero is the whole point.
#:
#: WHAT IT ENCODES. Both weights above encode the same claim: a relationship
#: at the firm changes the odds of the application. That claim is a statement
#: about a MECHANISM — a warm intro reaching a banker who forwards a resume —
#: and at a firm whose process is a test the mechanism is not merely weaker,
#: it is absent from the firm's own description of how it hires. Jane
#: Street's FAQ declines one-to-one coffee chats by policy; Citadel
#: Securities' campus funnel is entirely competitions and events
#: (`research-st-quant.md` Q3, Grade A). Scoring a warm contact there is the
#: product asserting a route the firm says it does not run.
#:
#: WHY ZERO AND NOT, SAY, HALF. A fraction would encode "the mechanism works
#: less well here", which is a quantitative claim no source supports. What
#: the sources support is a claim about presence: no documented path. Zero is
#: the honest magnitude for an axis with nothing under it, and it is also the
#: value the axis already returns for a student with no contacts at the firm
#: — so nothing new is invented, an existing branch is simply reached.
#:
#: WHAT THIS MUST NOT BE READ AS. Not a penalty. `_network_fit` may not
#: return a negative here, and the chip may not say networking hurts: the
#: same file is explicit that no source shows networking is counterproductive
#: at these firms, only that no mechanism is documented. A negative weight
#: would be the product inventing the stronger claim.
#:
#: WHAT WOULD CHANGE IT. A quant or proprietary firm publishing a referral or
#: campus-ambassador route, or an observed conversion through one. Then this
#: firm stops being `assessment` in `Firm.recruiting_style` and the axis
#: scores normally with no code change at all — which is why the switch is a
#: per-firm column rather than a constant in here.
#:
#: BLAST RADIUS, measured 2026-09-02: 15 firms carry `recruiting_style =
#: "assessment"` and hold 302 open campus rows between them. On the founder's
#: own account the effect is zero — every firm in play on his board is
#: `campus`, he has warm contacts at 13 firms and none of them is an
#: assessment firm, and his six picks are unchanged.
W_NETWORK_ASSESSMENT = 0

# 4b. Firm tier (the student's own `crm.UserFirm.tier`).
#: Tier 1 must outrank tier 3. It does, by construction, and by enough that
#: tier alone clears `MIN_SCORE` while tier 3 alone does not.
TIER_POINTS: Mapping[int, int] = {1: 26, 2: 16, 3: 8}
#: A firm the student targeted but never tiered. Real signal, weak.
W_TARGET_UNTIERED = 4

#: The bar a role must clear to be shown at all. Calibrated so that no INFERRED
#: input can put a role on the bar by itself, while any input that is somebody's
#: own statement can.
#:
#: Exactly three inputs clear 25 alone, and all three are statements rather than
#: guesses: the posting's own stated class year (30), the student's own tier-1
#: ranking of the firm (26), and a title that names the student's track outright
#: (26). Everything inferred falls short by itself and needs a second signal —
#: a firm-coverage track match (18/20), either region match (16/20), a
#: convention-derived class year (18), the target-cycle bonus (15), a warm
#: contact (14), tier 2 (16), tier 3 (8), an untiered target (4), an adjacent
#: derived year (6).
#:
#: An earlier version of this comment claimed "any two inputs together" clears
#: the bar. That was never true and the arithmetic says so: the two weakest
#: inputs are an adjacent derived year (6) and an untiered target firm (4),
#: which sum to 10. Left corrected rather than deleted, because a comment that
#: mis-describes its own calibration is how a threshold stops being audited.
#:
#: Below this the honest answer is an empty state, not a padded list — see
#: `recommend`.
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
        "nyu", "stern", "mit", "sloan", "harvard", "hbs", "yale",
        "princeton", "stanford", "gsb", "booth", "kellogg", "northwestern",
        "ross", "mccombs", "fuqua", "cornell", "dartmouth", "tuck",
        "georgetown", "mcdonough", "emory", "goizueta", "stevens",
        # "columbia" is stored in its long forms only, for the same reason
        # bare "cambridge" and bare "oxford" are below: the bare word is also
        # the University of BRITISH Columbia (Vancouver), which this table
        # answered "us" for on a word-boundary match, and Columbia, Missouri /
        # Columbia, South Carolina. A Canadian student was told, in a tooltip
        # rendered as fact, that the United States is "the market your
        # university sits in" — and collected the highest region weight on the
        # board (W_REGION_SCHOOL = 20) for every American role. Canada is not
        # a tracked market, so the honest answer for UBC is "" and no points.
        "columbia university", "columbia business school",
        # "ivey" was in this list and is not American: the Ivey Business
        # School is Western University's, in London, Ontario. Same false
        # "your university's market" sentence as UBC above, on a name that
        # was simply filed under the wrong country. Removed rather than
        # re-keyed — Canada has no code here to move it to.
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

#: The region values that are the POSTING STATING A PLACE, and so the only ones
#: `W_REGION_MISMATCH` may charge for. The six tracked markets a student can
#: name in Settings, plus "other" — which `classify.normalize_region` gives to
#: a location it read successfully and that sits outside all six. Deliberately
#: excludes "global" (the posting saying it has no single place) and "" (the
#: location did not parse, which is `W_REGION_UNKNOWN`'s case, not this one).
_STATED_MARKETS = frozenset(TRACKED_REGIONS) | {"other"}

#: Short track labels for the reason chips (the long ones live in views.py's
#: TRACK_LABELS and are far too wide for a chip).
TRACK_SHORT = {
    "ib": "IB", "st": "S&T", "pe": "PE", "am": "AM",
    "consulting": "Consulting", "corp-strat": "Corp Strat",
}

#: The indefinite article each short label takes, spelled out rather than
#: derived. The tooltip said "The posting itself is a IB role" on every live
#: IB pick, because the rule is the initial SOUND and not the initial letter:
#: "IB", "S&T" and "AM" are read as letter names beginning with a vowel sound
#: ("an eye-bee", "an ess-and-tee", "an ay-em") while "PE" is not ("a pee-ee").
#: A vowel-letter test gets three of these six wrong in both directions, so
#: the answer is written down per label instead of computed.
TRACK_ARTICLE = {
    "ib": "an", "st": "an", "pe": "a", "am": "an",
    "consulting": "a", "corp-strat": "a",
}


def school_region(school: str) -> str:
    """Map a free-text school name to one of the six TRACKED market codes, or
    "" when it cannot be determined. Never guesses — see the section comment.

    ONLY a tracked market, never "other" or "global". `normalize_region` has
    three answers this function must not pass on:

      "other"   a stated place outside the six markets Coverage tracks. It is
                a BUCKET, not a market: Toronto, Sydney, Mumbai and Dubai all
                resolve to it. `_region_fit` compares the student's home code
                against the ROLE's code for equality and, on a match, renders
                "{market} — the market your university sits in" as a fact.
                With "other" on both sides that sentence is a false statement
                about two different countries, and it collects the highest
                region weight on the board. Measured on the live open campus
                set: 713 of 2,662 rows carry region="other", so a student at
                any untracked-market university was being told the market of
                27% of the board matched their own.
      "global"  the placeless tier ("Remote", "Worldwide"). A university is
                not placeless; a school name that resolves here has been
                misread, not located.
      ""        already the honest abstention.

    A student at an untracked-market university simply scores zero on this
    axis, which is the same answer the table already gives for a school it
    does not recognise."""
    text = (school or "").strip()
    if not text:
        return ""
    for code, pattern in _SCHOOL_PATTERNS:
        if pattern.search(text):
            return code
    code = normalize_region(text)
    return code if code in TRACKED_REGIONS else ""


# ---------------------------------------------------------------------------
# Programme year -> graduation year. THERE IS ONE ANSWER TO THIS QUESTION AND
# IT LIVES IN `classify.derive_class_year`.
#
# This module used to keep a second one: a `_GRAD_WINDOW` table mapping the
# bucket alone to an offset window (internship +1, entry_level +0, insight
# +2..+3). It read only the BUCKET, while `derive_class_year` reads the bucket
# AND the title, and refuses outright for every shape whose convention has more
# than one answer — off-cycle and seasonal placements, co-ops, internships that
# name no season, apprenticeships and alternance contracts, talent communities,
# PhD internships, sophomore/freshman/first-year programmes, and the whole
# insight bucket (a first-year in year N graduates N+2 or N+3 depending on the
# degree, which is exactly the variance that makes it unanswerable).
#
# Measured on the live open campus set, 2026-09-01: 2,662 rows, of which
# **737 (28%) were rows this module derived a graduation year for and
# `derive_class_year` refuses to**. Every one of them rendered a chip reading
# "likely Class of 20XX" with a tooltip asserting "A 20XX programme is usually
# done by students graduating 20YY" — for winter co-ops, off-cycle Paris
# internships, a "Women in Quantitative Finance" event and a Boston career
# forum, which was the #1 pick on the founder's own live rail. On the 642 rows
# where both derived, the two agreed on every single one; the disagreement was
# never about the arithmetic, only about when there is an answer at all.
#
# Two features reading the same fact and disagreeing about it is the
# inconsistency the `grad_years` field on `Candidate` was added to stop, and
# `role_matches_level` and `views._eligibility` already read
# `derive_class_year`'s answer (through `Opportunity.class_year_derived`).
# Ranking now reads the same one, computed live rather than off the stored
# column: that column was stale on 247 of the same 2,662 rows, all in the
# direction of an empty value, and a ranker that abstains because a backfill
# has not run is abstaining for the wrong reason.
#
# The conventions themselves, and the argument for admitting only two of them,
# are documented once at `classify.derive_class_year`. This module keeps only
# the WEIGHTS, and the rule that a derived answer is labelled "likely".
# ---------------------------------------------------------------------------

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
    #: The rung of the ladder the student is on, as THEY stated it:
    #: "undergrad", "mba" or "phd" — or "" when the account has not said
    #: (the `User.study_level` column is being added alongside this; until
    #: it lands every profile reads ""). Read through `level`, never
    #: directly: `level` is where the default for a blank value lives.
    study_level: str = ""

    @property
    def school_region(self) -> str:
        return school_region(self.school)

    @property
    def level(self) -> str:
        """The level scoring compares a role's rung against — see
        `student_level` for the one rule that turns a blank `study_level`
        into "undergrad" and when it refuses to."""
        return student_level(self.study_level, self.target_cycles)

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
            # Retired slugs dropped on the way in (D-3): a track the picker
            # no longer offers cannot score a firm up, on this page or in
            # the digest, and a profile still holding one reads as the
            # one-track profile it now is. `classify.selectable_tracks` is
            # the single definition of that read.
            tracks=tuple(selectable_tracks(getattr(user, "tracks", None))),
            firm_tiers=dict(firm_tiers or {}),
            warm_firms=dict(warm_firms or {}),
            # `getattr` with a default on purpose: the column is being added
            # concurrently and may not exist on this User yet. A missing
            # attribute must read as "not stated", never raise.
            study_level=str(getattr(user, "study_level", "") or ""),
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
    #: How the firm hires, from `Firm.recruiting_style` — "campus" (the
    #: default: coffee chats and referrals move the process) or "assessment"
    #: (the process is a test or competition). Flattened here for the same
    #: reason `firm_tracks` is: this module stays free of Django and does not
    #: follow a foreign key mid-scoring.
    #:
    #: `_network_fit` is its only reader, and reads it to return ZERO rather
    #: than to penalise anything. See that function for the evidence and the
    #: limit on what may be said about it.
    recruiting_style: str = ""

    @classmethod
    def from_opportunity(cls, o, *, blocked: bool = False) -> "Candidate":
        return cls(
            id=o.id, firm_id=o.firm_id, firm_name=o.firm.name,
            firm_slug=o.firm.slug, title=o.title, url=o.url,
            bucket=o.bucket or "", cohort=o.cohort or "",
            class_year=o.class_year or "", region=o.region or "",
            location=o.location or "",
            firm_tracks=tuple(o.firm.tracks or []),
            recruiting_style=o.firm.recruiting_style or "",
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
    the `grad_years` field was added to stop.

    An OPEN-ended window — "graduating in 2028 or later", "December 2028
    onwards" — names a floor and no ceiling. It arrives here as its years
    enumerated up to the extractor's own horizon (`directory.facts.
    GRAD_YEAR_MAX`), because `Candidate` carries the years alone and not the
    `open_high` flag the extractor stores beside them. A window that reaches
    that horizon is the posting saying "and everyone after": the ceiling is
    the product's, not the firm's, so `hi` is lifted to wherever this student
    is rather than trusted. Before the extractor knew an open bound existed,
    the same sentence was stored as [2028, 2028] and this function vetoed a
    2029 student on a sentence that includes them."""
    if not profile.class_year:
        return None
    stated = _int_or_none(c.class_year)
    if stated is not None:
        return (stated, stated)
    ys = sorted(int(y) for y in c.grad_years if str(y).isdigit())
    if not ys:
        return None
    from directory.facts import GRAD_YEAR_MAX  # the extractor's own ceiling
    lo, hi = ys[0], ys[-1]
    if hi >= GRAD_YEAR_MAX:
        hi = max(hi, profile.class_year)
    return (lo, hi)


def stated_class_mismatch(profile: Profile, c: Candidate) -> bool:
    """True when the posting's OWN WORDS name a graduating class and this
    student is not in it. Never inference: a programme year that merely
    IMPLIES a class (`cohort`) is not a statement and never reaches here."""
    window = _stated_grad_window(profile, c)
    return window is not None and not (window[0] <= profile.class_year <= window[1])


def _names_same_bucket_other_year(profile: Profile, bucket: str, cohort: int) -> bool:
    """True when one of the student's own target cycles names THIS programme
    bucket in a year other than `cohort` — "2028 Summer Internship" against a
    2027 internship. That is the student saying, in their own words, which
    intake they are in, so the adjacent one is not a reason for them (see
    `W_CLASS_DERIVED_NEAR`). A student who named no cycle for this bucket
    has said nothing about it and the adjacent-year points still apply."""
    for raw in profile.target_cycles:
        cycle = parse_target_cycle(raw)
        if cycle is not None and cycle[0] == bucket and cycle[1] != cohort:
            return True
    return False


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
        # An OPEN window ("2028 or later", "December 2028 onwards") is stored
        # by the extractor as every year from the floor to its horizon, because
        # `Candidate` carries the years alone and not the `open_high` flag. So
        # a ceiling at the horizon IS the openness signal, and the chip must
        # read "For 2028+ grads", not "For 2028–2035 grads" - the posting never
        # said 2035, and printing it would typeset the product's own bookkeeping
        # as the firm's words.
        from directory.facts import GRAD_YEAR_MAX
        open_high = hi >= GRAD_YEAR_MAX and lo != hi
        if lo == hi:
            label = str(lo)
        elif open_high:
            label = f"{lo}+"
        else:
            # A PLAIN HYPHEN, and the chip below carries no em dash either.
            # These strings are not only chip text: `Recommendation.why` joins
            # them and `crm.digest` prints that join into an email, where "For
            # 2027–2028 grads — you" wrapped at the em dash and left "— you"
            # dangling on its own line. The founder's copy rule is no em
            # dashes anywhere a student reads.
            label = f"{lo}-{hi}"
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
        # AN OPEN WINDOW WHOSE FLOOR IS YEARS BELOW THIS STUDENT IS A NEAR
        # MISS, NOT A MATCH. "Graduation date must be after January 2026" is a
        # sentence about who is NOT excluded, not a sentence about who the
        # programme is for, and containment alone cannot tell the two apart:
        # the floor enumerates to the extractor's horizon, 2029 falls inside
        # it, and `W_CLASS_STATED` (30) paid out as if the firm had written
        # "Class of 2029". Measured 2026-09-01 on the founder's live rail
        # (class 2029): THREE of his top five rode this — two Bank of America
        # London off-cycles ("after January 2026", floor three years back) and
        # Baird's year-round securities processing internship — each scoring
        # the same 30 points as a posting that names his class outright, which
        # is the rarest and strongest statement on the whole board. An
        # off-cycle analyst seat open to everyone who graduated in the last
        # four years is a real opportunity and a terrible pick.
        #
        # ONE YEAR is the line, and it is the line `_class_fit` already draws
        # everywhere else: a gap of 1 is `W_CLASS_DERIVED_NEAR`'s "students do
        # apply a year out, but only just", a gap of 2 is what
        # `role_matches_level` refuses outright. So a floor at 2028 or 2029 for
        # a 2029 student is the firm describing this year's cohort with a
        # tolerance, and keeps the full stated bonus; a floor at 2026 is the
        # firm describing four cohorts at once, and pays `W_CLASS_DERIVED_NEAR`
        # — the same 6 points the product gives any other "adjacent, worth a
        # look, not a fit" signal. The chip still prints the floor, because
        # "For 2026+ grads" is true and is exactly the fact that tells the
        # student why this is not their year's programme.
        #
        # Only the SCORE moves. `_eligibility` reads the same window through
        # its own path and still refuses to block a student the sentence
        # includes, so nothing here re-hides a row; and a CLOSED window
        # ("2027–2028"), however wide, is untouched, because a firm that named
        # both ends of it named the cohorts it meant.
        near_open = open_high and (profile.class_year - lo) > 1
        points += W_CLASS_DERIVED_NEAR if near_open else W_CLASS_STATED
        if near_open:
            reasons.append(Reason(
                f"For {label} grads",
                f"The posting states it is for {label} graduates, so your "
                f"{profile.class_year} is not excluded. But it names no "
                f"class of its own, and its floor is "
                f"{profile.class_year - lo} years before you.",
                "class",
            ))
        else:
            reasons.append(Reason(
                f"Class of {label}" if lo == hi else f"For {label} grads (yours)",
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
        # The ONE derivation, `classify.derive_class_year` — which also hands
        # back the sentence that justifies it, the same sentence
        # `views._eligibility` renders on its "Likely your year" chip. Reusing
        # it rather than composing a second wording is the point: a student
        # who sees this role in the feed and in the rail is now given one
        # explanation of the inference, not two that have to be kept in step.
        derived, note = derive_class_year(c.bucket, c.title, c.cohort)
        implied = _int_or_none(derived)
        if implied is not None:
            gap = abs(profile.class_year - implied)
            if gap == 0:
                points += W_CLASS_DERIVED
                reasons.append(Reason(
                    f"likely Class of {implied}", note, "class",
                ))
            elif gap == 1:
                # A NEAR MISS, and the chip must say so in its own text —
                # not only in the tooltip. `Recommendation.why` joins chip
                # texts alone, and `crm.digest` prints that string verbatim
                # in an email with no tooltip to hover, so "2027 intake" on
                # its own read as a reason FOR the role. "A year early/late
                # for you" is the caveat, in the chip, everywhere it prints.
                early = implied < profile.class_year
                reasons.append(Reason(
                    f"{cohort} intake, a year {'early' if early else 'late'} for you",
                    f"{note} That is a year off from your "
                    f"{profile.class_year}, so worth a look but not a fit.",
                    "class",
                ))
                # The 6 points survive only when the student has NOT named
                # this programme in another year — see `W_CLASS_DERIVED_NEAR`.
                # "2027 Summer Internship" is not a reason for a student who
                # typed "2028 Summer Internship" into Settings; it is the
                # intake he told us he is not in.
                if not _names_same_bucket_other_year(profile, c.bucket, cohort):
                    points += W_CLASS_DERIVED_NEAR

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

    A role with no resolved region is not scored as a wrong place — but it
    is no longer scored as a free pass either. Zero here used to mean the
    blank row sat 16-20 points behind its located neighbours on THIS axis
    and then made the whole gap back on the others, and an unlocated Nomura
    programme ranked #1 for a HK/US student. So when the student has named
    regions, a blank costs `W_REGION_UNKNOWN`, with a chip that says the
    product could not place it — a penalty for our own ignorance, never a
    claim about the role. A student who named no regions has no market for
    the blank to be wrong about, and it still scores zero for them.

    A role that DOES resolve to a market, and to one the student never named,
    is the opposite case and costs `W_REGION_MISMATCH`. Zero there meant a
    London posting and a Hong Kong posting scored identically on the geography
    axis for a Hong Kong student — not a hedge, but the axis switched off for
    exactly the rows it exists to separate."""
    if not c.region:
        if profile.regions:
            return W_REGION_UNKNOWN, [Reason(
                "Location not read",
                "Coverage could not tell which market this role is in — "
                "the posting's location did not parse — so it cannot say "
                "whether it is in one of your regions. Check the posting.",
                "region",
            )]
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
    if profile.regions and c.region in _STATED_MARKETS:
        named = ", ".join(REGION_SHORT.get(r, r.upper()) for r in profile.regions)
        if c.region == "other":
            # `normalize_region` files a location it DID read but that sits
            # outside all six tracked markets under "other" (Toronto, Sydney,
            # Mumbai). It is a stated place, so it binds, but there is no label
            # for it that would mean anything in a chip.
            chip, where = "Not in your regions", "a market Coverage does not track"
        else:
            chip, where = f"Not in your regions ({short})", full
        return W_REGION_MISMATCH, [Reason(
            chip, f"This role is in {where}. You named {named}.", "region",
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
         r"|\bquantitative (analysis|research|trading|strateg)"
         # The S&T division under the name the banks give it: "Global
         # Markets" (BofA, HSBC, Barclays, Deutsche, SocGen), and a bare
         # "Markets" when it is attached to a programme word ("Markets
         # Summer Analyst Program" at J.P. Morgan and Citi, "APAC Markets
         # Summer Analyst"). Measured on the whole open board, 2026-09-01:
         # 36 rows carried one of these and stated no track at all, so they
         # inherited their bank's ib+st coverage instead of saying st; 12 of
         # J.P. Morgan's were reading as IB off the division prefix (see
         # `_DIVISION_PREFIX`). The lookbehinds keep the phrases that belong
         # to OTHER tracks out — "Capital Markets" is IB (and checked first
         # anyway), "Private Markets" is PE, "Public Markets" and "Growth
         # Markets" are asset management — and "markets analyst" is left out
         # entirely because "Growth Markets Analyst" is a wealth role.
         r"|\bglobal markets\b"
         r"|(?<!capital\s)(?<!private\s)(?<!public\s)(?<!growth\s)"
         r"\bmarkets\s+(?:summer|program(?:me)?|intern(?:ship)?|placement)s?\b", "st"),
        (r"\bprivate equity\b|\bbuyout\b|\bgrowth equity\b|\bprivate capital\b"
         r"|\bprivate markets?\b|\binfrastructure investing\b", "pe"),
        # `\bwealth management\b` USED to sit in this list and now sits in
        # `_NON_TRACK_FUNCTION` instead: retail wealth advisory is not asset
        # management, and filing it here put "AMP Financial Advisor Trainee"
        # and "Wealth Management Full-time Branch Analyst" under the AM label
        # for every student who picked AM (72 open campus rows on 2026-09-01).
        # The DIVISION name "Asset & Wealth Management" is the exception and
        # is spelled out here: it is the GS/JPM umbrella over both businesses,
        # it genuinely covers asset management, and without this clause the
        # blocklist's `\bwealth management\b` would swallow the eight open
        # "Asset and Wealth Management Quantitative Strats" rows that answer
        # `am` today. Listed first so `_track_spans` covers the whole phrase
        # and `_names_non_track`'s span rule protects it.
        (r"\basset\s*(?:&|and)\s*wealth management\b"
         r"|\basset management\b|\bportfolio management\b"
         r"|\binvestment management\b|\bmulti-?asset\b", "am"),
        # Bare `\badvisory\b` was here and came out. It is the Big 4's name
        # for the whole non-audit half of the firm ("Advisory - Associate -
        # Milano - Deals Valuation") and the boutiques' name for investment
        # banking ("Strategic Advisory & Restructuring" at PJT, "Sovereign
        # Advisory, Financial Restructuring Group" at Rothschild, "Global
        # Banking and Advisory (Coverage)" at SocGen), so it named no
        # function at all: 56 of the 83 open campus rows carrying the word
        # answered "consulting" on the strength of that word alone, including
        # nine PJT restructuring analyst seats in Hong Kong and London that
        # are the most on-track thing on the board for a student recruiting
        # IB. Those rows are now SILENT and inherit their firm's coverage,
        # which is the honest answer: the title did not say. "Advisory" as
        # part of a real consulting phrase is unaffected, because
        # `\bconsult(ing|ant)\b` is still here and PwC's own "Advisory
        # (Consulting)" carries it.
        (r"\bconsult(ing|ant)\b|\bstrategy (and|&) operations\b", "consulting"),
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
    # The risk department's own names, 2026-09-01 census. Bare `\brisk\b`
    # stays OUT for the same reason bare `\bretail\b` and bare `\btechnology\b`
    # do: it is also a front-office consulting practice ("Financial Services
    # Risk Consulting", "Business Consulting Risk"), and 5 open campus rows
    # state a real track alongside it. These four phrases collide with nothing
    # on the whole 26,163-row board (0 rows state a track), and between them
    # they stop 10 open campus rows inheriting their bank's ib/st coverage.
    r"|\bcorporate risk\b|\benterprise risk\b|\brisk assurance\b"
    r"|\bliquidity risk\b"
    # ...and the case the phrase list cannot reach: "Risk" standing alone as
    # its own delimited segment of a routing-style title. Nomura files every
    # posting as "YYYY - Division - Programme - City", so its risk placement
    # is "2027 - Risk - Industrial Placement - London" — a title with no
    # phrase in it for any blocklist to match, which therefore inherited
    # Nomura's ["ib", "st"] and ranked SECOND on the founder's own live
    # Picked-for-you rail. A word standing alone between delimiters is the
    # division's name; a risk word attached to something else ("Risk
    # Consulting") is the shape the phrase list deliberately declines to
    # touch. 14 rows board-wide carry this shape, 7 of them open campus; the
    # single row that also states a track is EY's "Industrial Trainee -
    # Business Consulting Risk - AMI - CNS - Risk - Process & Controls -
    # Kolkata", which now answers "none" — the same "co-occurring non-track
    # word, decline rather than guess" outcome this file already gives
    # "Trading Operations Analyst".
    r"|(?:^|[|\-–—]\s*)risk\s*(?:$|[|\-–—])"
    r"|\baccounting\b|\bfinancial report(ing)?\b|\bprocurement\b"
    # Product control / financial control, under the name the banks give the
    # division. `\baccounting\b` above is the same function under its generic
    # name and was already here; "Controllers" is what Goldman, Morgan Stanley
    # and Citi call it, and their "Controllers — Summer Analyst" rows carry no
    # other function word at all. 24 rows board-wide; the one that also states
    # a track is "Senior Manager Capital Markets Controllers", an experienced
    # hire that correctly declines. Plural on purpose: a singular "Controller"
    # is also a job title this has no evidence about.
    r"|\bcontrollers\b"
    # The internal IT department under its full name. The `\btechnology\s+...`
    # clauses below catch the department's job-title vocabulary but not the
    # spelled-out division name, so "2027 - Information Technology -
    # Industrial Placement - London" inherited Nomura's ib/st coverage and
    # ranked THIRD on the founder's live rail, chipped "matches IB + S&T".
    # 26 rows board-wide; the one that also states a track is "Lead Support
    # Analyst, Trading Applications, Information Technology", which is an IT
    # support job and correctly declines.
    r"|\binformation technology\b"
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
    r"|\bcorporate treasury\b|\bcorporate planning\b|\bcorporate infrastructure\b"
    # Custody and payments: the bank's post-trade and transaction-banking
    # plumbing, filed under the investment bank's roof and not a track. Both
    # surfaced on the founder's live rail chipped "IB role" — J.P. Morgan's
    # "Securities Services Leadership Program" and "Global Payments Summer
    # Analyst" — purely because the division prefix in front of them said
    # "Investment Bank" (see `_DIVISION_PREFIX`). 11 rows board-wide carry
    # one of the two phrases; none of them also states a track.
    r"|\bsecurities services\b|\bglobal payments\b"
    # ---- 2026-09-01 census: the 439 silent support-function titles ----
    # `role_function` answered "" for 1,316 of 2,723 open campus rows, and a
    # hand pass over a 40-row sample judged 29 of them answerable "none" by
    # rule. Measured with the extended vocabulary below: 439 of the 1,316 turn
    # over, 227 of them at firms whose `Firm.tracks` include ib or st, which
    # is to say 227 rows rendering "matches IB / S&T" today for a job that is
    # nothing of the kind — TD's "Personal Banking Associate Trainee", HSBC's
    # "Off-Cycle Actuarial Student Work Placement", RBC's "AI Data Analyst",
    # Vanguard's "College to Corporate IT Internship". Each clause below was
    # run against the whole open board before it was added, and the
    # co-occurrence count (titles that ALSO state a track today, and so change
    # answer) is quoted with it.
    #
    # Engineering, under every name it goes by. 159 open campus rows; 11 state
    # a track today and change answer, and 9 of those are the point rather
    # than the cost: "Trading Systems Engineer Graduate" (Optiver, 6 rows) and
    # "BMO Capital Markets Winter 2027, Full Stack Engineer" are software
    # jobs at a trading desk, not trading jobs, exactly as "Trading Operations
    # Analyst" already answers "none" here. The 2 genuine losses are a pair of
    # "Engineering & Construction - Infrastructure sector | Junior Consultant"
    # internships in Rome and Milan, where the word names the client SECTOR;
    # they now decline rather than claim consulting, which is this file's
    # standing call for a co-occurring non-track word.
    r"|\bengineer(ing|s)?\b|\bsoftware\b|\bhardware\b|\bfpga\b|\bprogrammer\b"
    r"|\bd[eé]veloppeur\b"
    # Actuarial (28 rows, 0 collisions), the audit and tax firms' own words
    # for their practices (119 rows for `assurance`, 0 collisions — PwC and
    # EY "Assurance" is the audit line, and `\brisk assurance\b` above was
    # already the narrower case of it), product control under its European
    # name (3 rows), market risk (4), and the legal department's junior
    # titles in three languages (2). None of these collides with anything.
    r"|\bactuar(y|ies|ial)\b|\bassurance\b|\bcontrolling\b|\bmarket risk\b"
    r"|\bparalegal\b|\bjuriste\b|\bimpuestos\b|\bchef de projet\b"
    # HR under the name Goldman and Morgan Stanley give it, and the internal
    # real-estate function under Goldman's. 12 and 2 rows, 0 collisions.
    r"|\bhuman capital\b|\bworkplace solutions\b"
    # Product management (6 rows). The one title that also states a track is
    # "Wealth Management, Product Management and Design", which is a
    # non-track row on both counts after this edit.
    r"|\bproduct management\b"
    # IT and DATA, both deliberately QUALIFIED rather than bare. `\bit\b` on
    # its own matches the English word "it" and, measured, took EY's "Junior
    # IT Consultant" and a French "Capital Market IT" role with it; `\bdata\b`
    # on its own hit 105 rows and broke 18, including Bank of America's
    # "Global Markets Quantitative Strategies Data Group" (a real S&T desk)
    # and Oliver Wyman's "Data & Analytics Consulting" (a real consulting
    # practice, and the reason `data\s*(&|and)\s*analytics` is NOT in the
    # list below). The qualified forms are the department's own job-title
    # vocabulary: 24 and 47 open campus rows, 1 and 3 collisions, and every
    # one of those four is a title this list should be catching anyway
    # ("Associate – IT Asset Management (ITAM)" answered `am` off the words
    # "Asset Management").
    r"|\bit\s+(?:intern(?:ship)?|analyst|support|services|audit|graduate"
    r"|program(?:me)?|placement|risk|infrastructure|asset management)\b"
    r"|\binformation systems\b"
    r"|\bdata\s+(?:analyst|analytics|scien(?:ce|tist)|engineer(?:ing)?"
    r"|management|governance|platform|quality|steward|warehouse)\b"
    # The bank's OTHER banks: the retail branch network, the private bank,
    # and the corporate/commercial/transaction banking arm. Coverage lets a
    # student pick six tracks and none of them is any of these — `cb` and `wm`
    # were both measured against the board on 2026-09-01 and both failed the
    # supply gate (18 hk+us rows across 7 firms for cb, 47% single-firm
    # concentration for wm), so they are HELD, not shipped, and until they
    # ship the honest answer for these titles is "not one of your tracks"
    # rather than the bank's own ib/st coverage inherited by silence.
    # 125 open campus rows carry a banking phrase; 2 state a track today
    # (HSBC's "Private Bank and Wealth Management ... Singapore", which is
    # both), and across the WHOLE 26,492-row board exactly ONE title carries
    # both "investment banking" and one of these — an experienced-hire
    # "Investment & Corporate Banking – Energy" req — so the tie-break the
    # research warns about ("Investment Banking must win over Commercial")
    # has no campus row to decide. Note this supersedes the note above about
    # bare `\bcorporate\b`: the bare word stays out because "Corporate
    # Finance" and "Corporate Advisory" are IB, and "Corporate Banking" is
    # now named explicitly instead of being left to it.
    r"|\b(?:personal|retail|consumer|private|corporate|commercial|business)"
    r"\s+bank(?:ing|er)?\b"
    # The corporate/private bank's front line under its own job title. 9 open
    # campus rows, 0 of which state a track today, and every one of them is
    # either "Relationship Management - Corporate and Institutional Banking"
    # or "Relationship Management - Private Bank": the phrase does not appear
    # on this board attached to anything else. Two of them are HSBC's
    # "International Wealth and Premier Banking" internships, which sat at
    # ranks 3 and 4 of the demo student's rail chipped "matches IB". Kept as
    # the whole phrase rather than a bare `\bmanager\b`, and NOT extended to
    # "client relationship management", which is institutional sales at an
    # asset manager and a different argument.
    r"|\brelationship manag(?:er|ers|ement)\b"
    # Back-office securities processing, which `\bsecurities services\b`
    # above does not reach. 1 open campus row, 0 collisions: Baird's
    # "Internship - Securities Processing (Year-Round)", rank 5 on the demo
    # student's rail chipped "matches IB". Deliberately not `securities
    # lending`, which is a real prime-brokerage desk.
    r"|\bsecurities (?:processing|operations)\b"
    # Wealth management and its front line, moved OFF the `am` pattern (see
    # `_ROLE_FUNCTION`). 108 open campus rows, 79 of which answer `am` today,
    # so a student who picked Asset Management gets Raymond James' "AMP
    # Financial Advisor Trainee" and Goldman's "Private Wealth Management —
    # New Associate" in an `?track=am` facet and in their picks. The eight
    # "Asset and Wealth Management" division rows keep answering `am`, because
    # that phrase is now spelled out in the am pattern and the span rule
    # protects it.
    r"|\bwealth management\b|\bprivate wealth\b|\bwealth advisor(?:y)?\b"
    r"|\bfinancial advisor\b|\badvisor trainee\b|\bclient associate\b"
    r"|\bprivate client\b"
    # The same function under the word the clause above misses, and the
    # corporate-finance planning desk that shares it. `\bfinancial advisor\b`
    # caught the retail brokerage's front line and left "Financial Planner"
    # and KeyBank's "Key Investment Services Internship (Certified Financial
    # Planner Track)" answering SILENT, so both inherited their bank's ib/st
    # coverage — which is how three of those internships, in Bellingham WA,
    # Chagrin Falls OH and Vandalia OH, reached the founder's fourteen-role
    # bulk-save offer on 2026-09-02 for a student recruiting IB and S&T in
    # Hong Kong and New York. "Financial Planning & Analysis" is the other
    # half: the company's own budgeting function, filed here for the same
    # reason `\bcontrollers\b` and `\bcorporate treasury\b` are.
    #
    # Measured across the whole 27,357-row board: 47 titles change answer, 46
    # of them silent -> "none" (10 KeyBank internships, 4 bare "Financial
    # Planner", 22 FP&A rows, the rest retail planning titles), and exactly
    # ONE loses a stated track — "Practice Consultant, Financial Planning",
    # an experienced-hire row that consults FOR financial planners, which is
    # the same "co-occurring non-track word, decline rather than guess" call
    # this file already makes for "Trading Operations Analyst". No open
    # campus row that states ib, st, am or pe is touched.
    r"|\bfinancial plan(?:ner|ning)\b",
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

#: A DIVISION name that only says where the job sits, stripped before the
#: title is read for what the job IS. J.P. Morgan files every campus posting
#: as "2027 Commercial & Investment Bank - <programme> - <city>", and the
#: ib pattern's `\binvestment bank(ing)?\b` matched the division on all of
#: them: 21 open campus rows on 2026-09-01, 20 answering "ib" — twelve of
#: them Markets (S&T) programmes, plus "Securities Services Leadership
#: Program" and "Global Payments Summer Analyst" both chipped "IB role" on
#: the founder's own rail. Same technique as `_HIRING_PROCESS`: remove the
#: words that are not about the job, then read what is left. Anchored to
#: the START of the title, with an optional intake year in front, because
#: that is the only place the board uses it as a prefix; "Investment Bank"
#: anywhere else in a title is still read as the job.
_DIVISION_PREFIX = re.compile(
    r"^\s*(?:20\d\d\s*)?[-–—:|]?\s*commercial\s*(?:&|and)\s*investment\s*bank(?:ing)?\b"
    r"\s*[-–—:|]?\s*",
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
    "none", because there is nothing else in it to be the job.

    A third thing that is not a claim about the function, handled first
    because it sits in front of everything else: a DIVISION prefix
    (`_DIVISION_PREFIX`). "2027 Commercial & Investment Bank - Markets
    Summer Analyst Program" is a Markets role that happens to be filed under
    the investment bank, and reading the prefix made it an IB one."""
    text = _DIVISION_PREFIX.sub("", title or "")
    track = _stated_track(text)
    if not _names_non_track(text):
        return track
    if track and _HIRING_PROCESS.search(text):
        rest = _HIRING_PROCESS.sub(" ", text)
        if _stated_track(rest) == track and not _names_non_track(rest):
            return track
    return "none"


#: `role_function` memoised on the title alone. Nine regexes over one string
#: is not expensive once; it is expensive ~1,900 times, which is how many
#: candidates `recommend()` scores for the founder on a single Opportunities
#: request — measured 2026-09-01 at ~34ms of a request that budgets single
#: digits for the whole picks build, and paid again by `role_matches_tracks`
#: for every row the advisor's snapshot and the digest's relevance filter
#: read.
#:
#: THE KEY IS THE TITLE AND NOTHING ELSE, which is what makes this safe. The
#: classifier is a pure function of the title over module-level constants: no
#: profile, no request, no clock, no database. So one student's answer is
#: every student's answer, an entry can never leak across a tenant boundary,
#: and the only thing that could stale an entry is an edit to `_ROLE_FUNCTION`
#: or `_NON_TRACK_FUNCTION` in this file — which is a source change, which
#: restarts the process. The input space is bounded by the board rather than
#: by traffic (13,464 distinct titles across 16,029 open rows), so `maxsize`
#: is set above it and the cache is a full memo table in practice.
#:
#: `directory.views` holds a sibling cache over the same function for the feed
#: path. Two caches, one classifier: this module must not import the view
#: layer, and a shared cache is not worth inverting that dependency for.
#: Tests that reach into the vocabulary must call `role_function_cached.
#: cache_clear()` — see the autouse fixture in `tests/test_recommend.py`.
role_function_cached = lru_cache(maxsize=32768)(role_function)


def role_matches_tracks(title: str, tracks) -> bool:
    """Whether a role's OWN TITLE **states** one of the tracks a student is
    recruiting for. An ALLOWLIST, not a blocklist — the role has to say it.

    Exists because "what's new at your firms" surfaces — the advisor's
    situation snapshot (`assistant.situation._new_role_events`), and until
    2026-08-31 also Today's own now-retired card
    (`crm.today._new_at_your_firms`) — select purely on the FIRM. That is
    right for the firm axis and wrong for everything on top of it: a
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
                                                 `assistant.situation.
                                                 _new_role_events`)
      student stated no tracks        -> True   (nothing to filter to)
    """
    # Retired slugs are not preferences (D-3): a student whose profile still
    # holds `corp-strat` is filtered as the ib-only student the picker would
    # now make them, and a corp-strat-ONLY profile degrades to the
    # no-tracks-stated case above, which is the honest read of a preference
    # the product has withdrawn.
    wanted = set(selectable_tracks(tracks))
    if not wanted:
        return True
    fn = role_function_cached(title)
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


# ---------------------------------------------------------------------------
# The RUNG of the ladder a role is on, read off its own title. Exists because
# the scorer was blind to it: measured 2026-09-01 on the founder's live board
# (a sophomore, class of 2029), 60 internship-bucket rows titled Associate /
# MBA / PhD passed every filter, 45 cleared `MIN_SCORE`, and 6 sat in his top
# 30 — RBC's and Guggenheim's "Investment Banking Summer Associate", PIMCO's
# "PhD Summer Intern", Barclays' "Banking Associate Summer Internship" whose
# own body says "must be pursuing an MBA". Every one of them named IB or
# quant research outright and sat in his region, so on the four axes the
# scorer had, they were excellent matches. They are for a different student.
#
# Two kinds of evidence, and the DEGREE kind wins. A degree word (PhD, MBA,
# undergraduate, BSc) is the posting stating who it is for; a rung word
# (Summer Associate, Analyst) is the industry's convention for the same
# thing, one step removed — a "PhD Summer Intern – Quantitative Research
# Analyst" is a PhD internship, not an analyst-level one, and only reading
# degrees first gets that right. Either kind answers "" the moment a title
# names two different levels ("BSc/MSc/PhD ... Internship", "Summer Analyst /
# Associate Program"): the posting admits more than one rung, and the honest
# answer is to decline rather than pick one.
#
# What is deliberately NOT here. Bare "Associate" is not a level: at PwC and
# Deloitte it is the undergraduate entry title ("Consulting - Associate -
# Strategy & Operations"), at McKinsey an MBA one, at Bridgewater an
# undergraduate internship ("Investment Associate Intern"). Only the shapes
# the banks reserve for the advanced-degree rung count — "Summer Associate",
# "Off-cycle Associate", "Associate Summer/Off-cycle Internship", and
# "Associate ... Graduate Program" (Barclays' MBA-entry programmes). "Senior"
# is left out too: SocGen's "Summer Senior Research Associate (Campus)" is a
# campus role. On the live campus buckets no title carries "experienced",
# "VP" or "vice president" at all; those patterns exist for the `other`
# bucket and for the next board that files them under campus.
# ---------------------------------------------------------------------------
_LEVEL_DEGREE: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rx, re.I), level) for rx, level in (
        (r"\bph\.?\s?d\b|\bpost-?docs?\b|\bdoctoral\b", "phd"),
        (r"\bmba\b", "mba"),
        (r"\bundergrad(?:uate)?s?\b|\bbachelor'?s?\b|\bbsc\b"
         r"|\bsophomores?\b|\bfreshm[ae]n\b", "undergrad"),
    )
)
_LEVEL_RUNG: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rx, re.I), level) for rx, level in (
        (r"\bexperienced\b|\bvice[\s-]president\b|\bvp\b", "experienced"),
        (r"\bsummer\s+associates?\b|\boff[\s-]?cycle\s+associates?\b"
         r"|\bassociates?\s+(?:summer|off[\s-]?cycle)\s+intern(?:ship)?s?\b"
         r"|\bassociates?\b[^|]*\bgraduate\s+program(?:me)?s?\b", "mba"),
        (r"\banalysts?\b", "undergrad"),
    )
)


def role_level(title: str) -> str:
    """The rung a role's own title names: "undergrad", "mba", "phd",
    "experienced", or "" when it names none — or more than one. See the
    block comment above for what counts and what deliberately does not."""
    text = title or ""
    for table in (_LEVEL_DEGREE, _LEVEL_RUNG):
        found = {level for rx, level in table if rx.search(text)}
        if len(found) == 1:
            return found.pop()
        if found:
            return ""
    return ""


#: Spellings `User.study_level` may arrive in, folded to this module's four
#: values. The column is being added concurrently and its vocabulary is not
#: pinned here, so the fold is generous about spelling and strict about
#: meaning: a value it cannot read ("masters", say) is a stated level this
#: module has no rung for, and answers "" rather than guessing.
_STUDY_LEVEL_ALIASES: Mapping[str, str] = {
    "undergrad": "undergrad", "undergraduate": "undergrad", "ug": "undergrad",
    "bachelor": "undergrad", "bachelors": "undergrad", "bsc": "undergrad",
    "mba": "mba",
    "phd": "phd", "doctoral": "phd", "doctorate": "phd",
}


def student_level(study_level: str, target_cycles) -> str:
    """The rung the student is on, or "" when it cannot be known.

    Stated wins: a `study_level` this module can read is the answer. When
    it is blank, ONE default and only one: a student whose every parseable
    target cycle is an internship or an insight week is read as an
    undergraduate — those are undergraduate programmes, and a student who
    named only them has described an undergraduate's plan. A student with
    no cycles, or with any full-time cycle, or a `study_level` in a
    vocabulary this cannot read, gets "" — and "" filters nothing, which is
    today's behaviour exactly."""
    key = re.sub(r"[^a-z]", "", (study_level or "").lower())
    if key:
        return _STUDY_LEVEL_ALIASES.get(key, "")
    buckets = set()
    for raw in target_cycles or ():
        parsed = parse_target_cycle(raw)
        if parsed is not None:
            buckets.add(parsed[0])
    if buckets and buckets <= {INTERNSHIP, INSIGHT}:
        return "undergrad"
    return ""


#: (student level, role level) pairs that are NOT a mismatch. Equality, plus
#: one asymmetry the industry itself makes: the banks' "Summer Associate"
#: rung is the advanced-degree internship (MBA, PhD and JD alike — Goldman's
#: "Quantitative Strats — Summer Associate" is written for PhDs), so a PhD
#: student is not mismatched by an MBA-rung role. The reverse is not true: a
#: "PhD Summer Intern" is for PhDs.
_LEVEL_COMPATIBLE: frozenset[tuple[str, str]] = frozenset({
    ("undergrad", "undergrad"), ("mba", "mba"), ("phd", "phd"), ("phd", "mba"),
})


def level_mismatch(student: str, role: str) -> bool:
    """True only when BOTH rungs are known and the pair is not compatible.
    Either side unknown -> False: nothing stated means nothing filtered."""
    return bool(student and role) and (student, role) not in _LEVEL_COMPATIBLE


def role_matches_level(
    bucket: str,
    class_year_derived: str,
    target_cycles,
    profile_class_year: int | None,
    *,
    title: str = "",
    study_level: str = "",
) -> bool:
    """Whether a role's own LEVEL — its programme bucket, the class year
    its shape implies, and the rung its title names — is compatible with
    the level a student is actually recruiting at. Same posture as the
    other two filters: nothing stated (by either side) means nothing is
    filtered.

    `title` and `study_level` are keyword-only and default to "", which
    turns the third check OFF: the two callers that predate it
    (`assistant.situation`, `crm.relevance`) keep exactly the behaviour
    they had until they choose to pass a title.

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

    3. The RUNG the title names (`role_level`) against the rung the student
       is on (`student_level`) — a "Summer Associate" or "PhD Summer
       Intern" for an undergraduate, a "Summer Analyst" for an MBA. The
       posting's own words this time, which is why the mismatch is a hard
       fail here and not a subtraction: the title said who it was for.
       Either side unknown, nothing happens — see `level_mismatch`.

    Deliberately does NOT duplicate `directory.views._eligibility`'s
    BLOCKING verdict (a stated class year or extracted grad-window that
    excludes this student outright) — that is a harder, title/body-STATED
    fact and the caller applies it separately, the same verdict
    `Candidate.blocked` already reads elsewhere. Checks 1 and 2 only ever
    act on the softer, INFERRED signals a role's shape carries, and never
    on nothing: a role with no derivable year and a student with cycles
    this can't place both pass.
    """
    if bucket and target_cycles:
        wanted_buckets = set()
        for raw in target_cycles:
            parsed = parse_target_cycle(raw)
            if parsed is not None:
                wanted_buckets.add(parsed[0])
        # An INSIGHT programme is never the wrong rung. It is the on-ramp to
        # the internship the student declared - Citi's Early ID, Nomura's
        # Discover, Evercore's Intro sessions all state "fast-tracked to
        # interviews for the Summer Internship" - and a sophomore who ticked
        # "2028 Summer Internship" has not told the product to hide them; she
        # has told it which internship they feed. Refusing them here cost the
        # founder his #1 pick (Nomura Discover, 90 pts) the day this check
        # reached `recommend()`, and the research is unambiguous that this
        # layer is where a first- or second-year's real options are (9 of 17
        # bulge brackets run it off the main board; when it does surface,
        # hiding it is the worst available outcome). `entry_level` stays
        # refused: a full-time role is a rung ABOVE, which is what this check
        # was written for.
        if (wanted_buckets and bucket not in wanted_buckets
                and not (bucket == "insight" and "internship" in wanted_buckets)):
            return False
    if class_year_derived and profile_class_year:
        derived = _int_or_none(class_year_derived)
        if derived is not None and abs(derived - profile_class_year) >= 2:
            return False
    if title and level_mismatch(student_level(study_level, target_cycles),
                                role_level(title)):
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
    fn = role_function_cached(c.title)
    if fn == "none":
        # The role said what it is and it is not one of these tracks. Claiming
        # "matches IB" here would be the card lying about the job — and so, it
        # turns out, was saying nothing at all: silence on this axis let the
        # firm's tier, market and warmth carry an Operations programme to the
        # top of the rail unopposed. See `W_TRACK_NONE`.
        #
        # A student who named NO tracks scores zero here, exactly as before:
        # there is no track for the posting's function to be outside of, and
        # penalising them would be the product inventing a preference on their
        # behalf.
        if not profile.tracks:
            return 0, []
        # "IB, S&T or PE", never "IB or S&T or PE": the tooltip is the only
        # place a student is told WHICH tracks the posting missed, and a
        # student recruiting for all six should be able to read the list.
        labels = [TRACK_SHORT.get(t, t.upper()) for t in profile.tracks]
        named = (labels[0] if len(labels) == 1
                 else ", ".join(labels[:-1]) + " or " + labels[-1])
        return W_TRACK_NONE, [Reason(
            "Not your tracks",
            f"The posting names its own function and it is not {named}. "
            f"{c.firm_name} does cover your tracks; this role does not.",
            "track",
        )]
    if fn:
        if fn not in profile.tracks:
            return 0, []
        name = TRACK_SHORT.get(fn, fn.upper())
        article = TRACK_ARTICLE.get(fn, "a")
        return W_TRACK_STATED, [Reason(
            f"{name} role",
            f"The posting itself is {article} {name} role, which you're "
            f"recruiting for.",
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
    "Your Network Here" already names names behind the login.

    AT A TEST-GATED FIRM THIS AXIS DOES NOT SCORE. The reason chip says so
    out loud rather than the score silently going quiet: a student with a
    real contact at SIG should be able to see that Coverage read the
    relationship and declined to count it, and why. See
    `W_NETWORK_ASSESSMENT` for what the zero encodes and what would change
    it. The chip is issued only where the student HAS a relationship —
    telling someone with no contacts at Optiver that their network does not
    score there is a sentence about nothing."""
    warmth = profile.warm_firms.get(c.firm_id)
    if warmth and c.recruiting_style == "assessment":
        return W_NETWORK_ASSESSMENT, [Reason(
            "Test-gated firm",
            "You know someone here, and this axis is not scored at this "
            "firm: its own process is a test or competition, and no route "
            "from a conversation into the pipeline is documented. Worth "
            "having anyway; it is not what moves this one.",
            "network",
        )]
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
        # a programme year that merely implies an ADJACENT class still
        # scores (as a labelled near miss), still says so, and still shows.
        # Silence never hides. A year two or more off is the rung filter's
        # call, next.
        if stated_class_mismatch(profile, c):
            continue
        # Nor the wrong RUNG of the ladder — `role_matches_level`, the same
        # yes/no the advisor's situation snapshot and the digest's relevance
        # filter already apply, which `recommend()` alone had never called.
        # Measured 2026-09-01 on the founder's live rail (class 2029, target
        # "2028 Summer Internship"): his #5 pick was Barclays' "Electronic
        # Trading Associate Graduate Program 2027" — a full-time programme
        # (he named only internships) whose intake implies 2027 graduates
        # (two years off his own) at the MBA rung (he is a sophomore). Three
        # separate reasons it is not for him, and the scorer had no axis
        # for any of them, so tier, track, region and a warm contact carried
        # it to 86 points.
        #
        # A skip rather than a subtraction, for the same reason the vetoes
        # above are: the bucket check acts on the student's own stated plan,
        # the rung check on the posting's own title, and a gap-2 intake is
        # one `_class_fit` already refuses to score — none of them is a
        # thing more tier or warmth should be able to buy back (a -40 would
        # still leave that Barclays row at 46, on the bar). The honest state
        # when nothing survives is an empty column, not the least-wrong six.
        #
        # EXCEPT where the posting has STATED this student's class. Its own
        # words outrank what its bucket or intake year would imply — the
        # rule this whole module runs on — so an insight programme that
        # says "for 2029 graduates" reaches a 2029 student who named only
        # Summer Internship cycles, and a 2029 entry-level programme that
        # states his class is not hidden by a plan he wrote before it opened.
        # Only the rung the title names (a "Summer Associate" for an
        # undergraduate) can still argue with a stated class, because that
        # is the posting's own words too. Without the exemption, the audit's
        # own regression case — a stated-class insight programme losing to a
        # prior-cycle near miss — would have flipped from "outranked" to
        # "gone". The derived year is computed live, as `_class_fit` does,
        # because the stored column lags the board (see the block comment
        # above `_CYCLE_BUCKETS`).
        if _stated_grad_window(profile, c) is None:
            derived, _note = derive_class_year(c.bucket, c.title, c.cohort)
            if not role_matches_level(
                c.bucket, derived, profile.target_cycles, profile.class_year,
                title=c.title, study_level=profile.study_level,
            ):
                continue
        elif level_mismatch(profile.level, role_level(c.title)):
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
