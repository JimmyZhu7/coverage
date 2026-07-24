"""Deterministic role-type classification for scraped postings.

Why this exists: the connectors report exactly what the ATS APIs return and
never invent taxonomy (see coverage_connectors/models.py), so every posting
used to land with `bucket=""` — and a "Vice President, Fund Finance" row sat
on the public calendar next to a spring week. The calendar's promise is the
three campus categories (insight programmes, internships, entry-level), so
the bucket has to be derived somewhere deliberate, testable, and cheap. That
somewhere is here: pure functions over the posting title (plus one board-level
campus hint), no LLM call per row, same posture as the staleness layer.

Buckets (the `Opportunity.bucket` vocabulary):

- ``insight``      pre-internship events: insight days/weeks, spring weeks,
                   discovery programmes, women-in-X / sophomore / diversity
                   events with no internship attached.
- ``internship``   summer/winter/off-cycle analyst & associate internships,
                   co-ops, industrial placements.
- ``entry_level``  full-time campus hires: graduate programmes, new-grad
                   roles, analyst development programmes, trainees.
- ``other``        everything else — experienced hires, HR/recruiting roles,
                   anything without an explicit campus signal.

Precision beats recall, deliberately. A missed classification degrades to
"other" (hidden behind an honest "N other roles" toggle in the UI); a false
positive puts a Senior DevOps Engineer on a student's deadline list and costs
trust — the one thing the brand can't spend. So every rule needs an explicit
signal, word-boundary matched ("intern" must never fire on "Internal Audit"),
and neutral titles like plain "Analyst" only get promoted when the *board*
itself is campus-scoped (Blackstone_Campus_Careers, ...students sites).

Ordering is load-bearing and documented at `classify_role`.
"""

from __future__ import annotations

import re

INSIGHT = "insight"
INTERNSHIP = "internship"
ENTRY_LEVEL = "entry_level"
OTHER = "other"

#: The buckets the product is about, in display order. `other` is not a
#: target — it exists so nothing is silently dropped.
TARGET_BUCKETS = (INSIGHT, INTERNSHIP, ENTRY_LEVEL)

BUCKET_LABELS = {
    INSIGHT: "Insight Programme",
    INTERNSHIP: "Internship",
    ENTRY_LEVEL: "Entry-Level",
    OTHER: "Other",
}


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(patterns), re.IGNORECASE)


# ---------------------------------------------------------------------------
# Region normalization — collapse a free-text location into one of the four
# markets the product targets (Hong Kong / US / Singapore / Europe), or "" for
# anything else. Boards report location wildly inconsistently ("London, UK";
# "New York, New York, United States"; "北京市 / 上海市"), so this maps the raw
# string to a canonical code the Region filter and the network scopes share.
# ---------------------------------------------------------------------------
REGION_LABELS = {"hk": "Hong Kong", "us": "United States", "sg": "Singapore", "eu": "Europe"}
REGION_ORDER = ("hk", "us", "sg", "eu")

# Checked in order; the first matching market wins. Keys are lowercase
# substrings (city / country / region tokens).
_REGION_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hk", ("hong kong", "hongkong", "香港", "hksar")),
    ("sg", ("singapore", "新加坡")),
    ("eu", (
        "london", "united kingdom", "u.k.", " uk", "england", "scotland",
        "frankfurt", "munich", "münchen", "berlin", "germany", "deutschland",
        "paris", "france", "amsterdam", "netherlands", "rotterdam",
        "zurich", "geneva", "switzerland", "milan", "italy", "madrid", "spain",
        "dublin", "ireland", "luxembourg", "brussels", "belgium",
        "stockholm", "sweden", "copenhagen", "denmark", "oslo", "norway",
        "warsaw", "poland", "sofia", "bulgaria", "lisbon", "portugal",
        "vienna", "austria", "europe", "emea",
    )),
    ("us", (
        "united states", "u.s.", "usa", "new york", ", ny", "jersey city",
        "chicago", ", il", "boston", ", ma", "san francisco", "menlo park",
        "los angeles", ", ca", "seattle", ", wa", "houston", "dallas", ", tx",
        "atlanta", ", ga", "charlotte", ", nc", "washington, d", "miami", ", fl",
        "nashville", ", tn", "malvern", "philadelphia", ", pa", "denver", ", co",
    )),
)


def normalize_region(location: str | None, *, fallback: str = "") -> str:
    """Map a free-text location to "hk" / "us" / "sg" / "eu", or `fallback`
    (default "") when it isn't one of the four target markets."""
    text = (location or "").lower()
    if not text:
        return fallback
    for code, keys in _REGION_KEYS:
        if any(k in text for k in keys):
            return code
    return fallback


# ---------------------------------------------------------------------------
# Title cleanup — boards prepend location/region routing to titles ("EMEA |
# Frankfurt | Women in Banking Dinner") and append requisition codes
# ("Project Intern (J19302)"). Strip both so the displayed title is the role.
# ---------------------------------------------------------------------------
# Region-ish words that show up as a leading pipe-segment (not real title text).
_TITLE_REGION_WORDS = {
    "emea", "apac", "amer", "amrs", "americas", "global", "asia",
    "asia pacific", "eu", "us", "usa", "uk", "na", "latam", "mena", "anz",
}
# A trailing requisition code in brackets: has a digit, no spaces inside —
# "(J19302)", "(R-788678)", "[REQ-30087]". A worded parenthetical with spaces
# ("(Summer 2027)") has a space and is left alone.
_REQ_CODE = re.compile(r"\s*[\(\[](?=[^)\]\s]*\d)[A-Za-z0-9][\w.\-/]*[\)\]]\s*$")
_WS = re.compile(r"\s{2,}")


def _is_location_segment(seg: str) -> bool:
    s = seg.strip().lower()
    if not s or len(s) > 24:
        return False
    return s in _TITLE_REGION_WORDS or bool(normalize_region(s))


def clean_title(title: str | None) -> str:
    """Strip leading location/region pipe-segments and a trailing requisition
    code, then collapse whitespace. Idempotent: cleaning a clean title is a
    no-op, so it's safe to re-run over already-stored rows."""
    t = (title or "").strip()
    if not t:
        return t
    if "|" in t:
        parts = [p.strip() for p in t.split("|")]
        while len(parts) > 1 and _is_location_segment(parts[0]):
            parts.pop(0)
        t = " | ".join(parts)
    t = _REQ_CODE.sub("", t)
    t = _WS.sub(" ", t)
    return t.strip(" |·-–—")


# Insight events. "insight" alone is NOT enough — "Market Insights Analyst"
# is a real data job — so it must be qualified by an event/programme word or
# an early/spring prefix.
_INSIGHT = _rx(
    r"insights?\s+(?:day|week|programme|program|event|evening|series|session)",
    r"\binsight\s+into\b",                      # "Insight into Operations Opportunities"
    r"(?:early|spring|first[\s-]?year|1st[\s-]?year)\s+insights?\b",
    r"\bspring\s*week\b",
    r"\bpre[\s-]?internship\b",
    r"\bdiscovery\s+(?:day|programme|program)\b",
    r"\bopen\s+day\b",
    r"\btaster\b",
    r"\bshadow(?:ing)?\s+(?:day|programme|program)\b",
    # Campus recruiting events are insight-type by nature: a "Virtual Event"
    # or "Q&A" listing on a campus events board is an attend-this, not an
    # apply-to-this.
    r"\b(?:virtual|in[\s-]?person)\s+event\b",
    r"\brecruitment\s+event\b",
    r"\bq\s*&\s*a\b",
)

# Explicit internship signals. \b keeps "intern" off "Internal"/"International".
_INTERNSHIP = _rx(
    r"\bintern(?:ship)?s?\b",
    r"\bsummer\s+analyst\b",
    r"\bsummer\s+associate\b",
    r"\bwinter\s+analyst\b",
    r"\boff[\s-]?cycle\b",
    r"\bco[\s-]?op\b",
    r"\bindustrial\s+placement\b",
    r"\bplacement\s+year\b",
    r"\bsummer\s+placement\b",
    r"\b(?:student|work)\s+placement\b",
    r"\byear\s+in\s+industry\b",
    r"\bsummer\s+(?:programme|program)\b",
    r"\bsummer\s+scholar\b",
    r"\bpraktikum\b",              # German for internship (UBS/DB/European boards)
    r"\bwerkstudent\b",           # German working-student
    r"\bworking\s+student\b",
    "实习",                        # Chinese "internship/intern" (CICC/Beisen boards):
                                   # catches 实习生, 项目实习, 暑期实习. No \b — CJK has
                                   # no word boundaries; the term only appears in an
                                   # actual intern title, so precision holds.
)

# Experienced/HR veto. Runs AFTER insight+internship (an HR *internship* is
# still an internship) but BEFORE the entry-level and affinity rules, so
# "Graduate Recruitment Manager" and "Head of Diversity" land in `other`
# instead of masquerading as campus roles.
_SENIOR = _rx(
    r"\bsenior\b",
    r"\bvp\b",
    r"\bvice\s+president\b",
    r"\bdirector\b",
    r"\bprincipal\b",
    r"\bhead\s+of\b",
    r"\bchief\b",
    r"\bmanager\b",
    r"\bexperienced\b",
    r"\bmid[\s-]?level\b",
    r"\blateral\b",
    r"\brecruit(?:er|ing|ment)\b",
    r"\btalent\s+acquisition\b",
)

# Full-time campus signals.
_ENTRY = _rx(
    r"\bgraduates?\b",
    r"\bnew\s+grad(?:uate)?s?\b",
    r"\bcampus\b",
    r"\bentry[\s-]?level\b",
    r"\bfull[\s-]?time\s+analyst\b",
    r"\banalyst\s+(?:development\s+)?(?:programme|program)\b",
    r"\brotational\s+(?:programme|program)\b",
    r"\btrainee\b",
    r"\bapprentice(?:ship)?\b",
    r"\buniversity\s+hire\b",
    r"\bearly\s+careers?\b",
    r"\bnew\s+analyst\b",          # Goldman Sachs' term for a full-time new-grad hire
    r"\bwmp\s+analyst\b",          # GS Wealth Management Program analyst (new grad)
    "校园招聘", "校招",             # Chinese campus recruitment (CICC/Beisen boards)
    "应届",                        # fresh graduate
    "管培生", "管理培训生",         # management trainee
)

# Eligibility/affinity events with no internship attached ("Women in Banking
# Programme — London 2026"). Checked last among the positive rules: a
# "Sophomore Summer Analyst" is an internship, not an event.
_AFFINITY = _rx(
    r"\bwomen\s+in\b",
    r"\bsophomore\b",
    r"\bfreshman\b",
    r"\bfirst[\s-]generation\b",
    r"\bdiversity\b",
)

# Neutral junior words that a campus-scoped board may promote to entry_level.
_NEUTRAL_JUNIOR = _rx(r"\banalyst\b", r"\bassociate\b", r"\bstudent\b")

# Board identifiers (Workday site slugs, Greenhouse tokens, ...) that mean the
# whole board is early-careers, e.g. "Blackstone_Campus_Careers", "Students",
# "RBCEARLYTALENT1", "solomonpartnersstudentsgraduates".
_CAMPUS_BOARD = _rx(r"campus", r"student", r"early", r"graduate", r"earlytalent")


def classify_role(title: str, *, campus_hint: bool = False) -> str:
    """Bucket a posting title. Rule order (load-bearing):

    1. insight events            ("Pre-Internship" must beat the intern rule)
    2. explicit internships
    3. experienced/HR veto       -> other
    4. full-time campus signals  -> entry_level
    5. affinity events           -> insight
    6. campus-board fallback: a neutral Analyst/Associate/Student title on a
       board that is itself campus-scoped is an entry-level hire
       ("Investment Banking Analyst" on a ...students board).
    7. everything else           -> other
    """
    t = title or ""
    if _INSIGHT.search(t):
        return INSIGHT
    if _INTERNSHIP.search(t):
        return INTERNSHIP
    if _SENIOR.search(t):
        return OTHER
    if _ENTRY.search(t):
        return ENTRY_LEVEL
    if _AFFINITY.search(t):
        return INSIGHT
    if campus_hint and _NEUTRAL_JUNIOR.search(t):
        return ENTRY_LEVEL
    return OTHER


_COHORT_YEAR = re.compile(r"\b20(?:2[4-9]|3[0-5])\b")


def extract_cohort(title: str) -> str:
    """First plausible cohort year (2024-2035) in the title, else "".
    "2027 Summer Analyst Program" -> "2027". Never fabricates a year."""
    m = _COHORT_YEAR.search(title or "")
    return m.group(0) if m else ""


def board_is_campus(board) -> bool:
    """True when a board is campus-scoped. Some connectors are campus-only
    by construction (a fixed campus query, no identifier to sniff); the
    rest are sniffed by their provider-specific identifying field (Workday
    `site`, Greenhouse `token`, Lever `org`, tal.net `board_url`, sitemap
    `path_filter`) — duck typing keeps this module dependency-free."""
    if board.__class__.__name__ in ("GoldmanSachsBoard", "McKinseyBoard"):
        return True
    ident = " ".join(
        str(getattr(board, attr, "") or "")
        for attr in ("site", "token", "org", "board_url", "path_filter")
    )
    return bool(_CAMPUS_BOARD.search(ident))
