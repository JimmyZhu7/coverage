"""The board can show you the same posting twice for two unrelated reasons,
and they need two different fixes. This module holds both, as pure functions.

CLASS A — one posting, several addresses. An ATS hands the same requisition
out under more than one URL, so the `(firm, url)` unique key files each copy
as a distinct posting. `provider_identity` answers "which real posting is this
URL pointing at?", and ingest uses it to update the row it already has instead
of minting a second one. Three mechanisms are covered, each confirmed against
live rows before its rule was written (see each pattern's own note).

CLASS B — several postings, one job. The employer really did open two or more
requisitions with identical titles in the same city (SIG posts every 2027
internship under two iCIMS job numbers; Deutsche Bank runs whole apprentice
intakes as parallel reqs). There is no shared identifier to canonicalize
toward, and the reqs have independent lifecycles — they can close on different
days — so merging them would destroy real data. `fold_duplicates` therefore
suppresses the copies at DISPLAY time only. Every row stays in the database
and keeps being close-tracked on its own.

Why Class A is not simply a `canonical_url()` extension, which is how the
`xf-` fix in `coverage_connectors.talnet` handles the same shape of problem:
that trick needs a canonical URL that still RESOLVES, and tal.net has none.
Probed live 2026-08-14 against bankcampuscareers / evercore / morganstanley:
the `/pl/<n>` pool segment is mandatory (dropping it 404s), its valid values
are board-specific (`pl/1` serves Bank of America's copy but 404s on
Evercore's, whose pools are 2 and 3), and the posting page's own
`rel="canonical"` still carries the pool. There is no string every copy can be
rewritten to that a student could actually click, so the collapse has to
happen against the stored rows rather than inside the URL.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------- Class A

# tal.net. `opp/<id>` is the posting; everything around it is navigation
# chrome that varies per candidate pool and per brand. Coverage configures two
# boards per firm (boards.py: `vacancy/1` jobs + `vacancy/2` events), the pool
# number reappears in every listing URL as `pl/<n>`, and a posting listed on
# both pools therefore arrives twice. Verified live 2026-08-14: BofA opp 14594
# under `pl/1` and `pl/2` returns the same posting, and Evercore opp 3145
# returns the same posting under `brand-5/pl/3`, `brand-6/pl/2` AND
# `brand-5/pl/2` — so brand is decorative too and is deliberately NOT part of
# the identity.
_TALNET_RE = re.compile(
    r"^https?://(?P<host>[^/]+\.tal\.net)/.*?/opp/(?P<opp>\d+)", re.IGNORECASE
)

# iCIMS. Job URLs are `/jobs/<id>/<slug>/job`, and the slug is generated from
# the CURRENT title. Rename the posting and the slug changes, which mints a
# new row and closes the old one even though `<id>` never moved — observed on
# SIG job 11260, where "Operations Analyst, Hong Kong" became "Business
# Operations Analyst (Middle/Back Office), Hong Kong" and Coverage recorded
# both, one of them wrongly closed.
_ICIMS_RE = re.compile(
    r"^https?://(?P<host>[^/]+\.icims\.com)/jobs/(?P<job>\d+)/", re.IGNORECASE
)

# Workday. `/<site>/job/<location>/<slug>` where the slug ends in the
# requisition id. On a multi-location posting Workday picks one location for
# the URL and that choice DRIFTS between scrapes — Blackstone's 2027 Summer
# Analyst moved from `Berkeley-Square-House-London` to `London`, Morgan
# Stanley's VEA Team Manager from `Tempe-Arizona…` to `Dallas-Texas…` — each
# drift minting a new row and closing the live one. 17 such pairs on the
# current data, every one with a byte-identical slug.
#
# The slug is kept WHOLE. Stripping a trailing `-<digits>` as a "collision
# suffix" is the obvious generalisation and it is wrong: it collapses Bain
# Capital's `REQ_108333` / `REQ_108333-1` correctly but also collapses
# Oaktree's `2026-397`, `2026-21` and `2026-83` — real, unrelated requisition
# ids in `YYYY-NNN` form — into one bogus group of 25 postings. Requisition
# grammar is per-tenant, so the only safe rule is to discriminate on what
# differs AROUND an identical id, never to parse the id.
#
# Deliberately anchored with `$`, which excludes the `…/apply` variants: those
# are a sixth path segment and, checked against the live rows, they never pair
# up with a non-apply copy, so there is nothing to gain and a shape to guess at.
_WORKDAY_RE = re.compile(
    r"^https?://(?P<host>[^/]+\.myworkdayjobs\.com)/(?P<site>[^/]+)"
    r"/job/(?P<loc>[^/]+)/(?P<slug>[^/?#]+)$",
    re.IGNORECASE,
)


def _workday_has_req(slug: str) -> bool:
    """Does this Workday slug actually carry a requisition id?

    The identity below ignores the URL's location segment, so it is only
    sound when something ELSE in the URL pins the posting down. Huatai's board
    ends every slug at a bare `_` with no id at all
    (`Private-Wealth-Management---Business-Analyst_`); for those rows title and
    city are the only signal, which is Class B evidence, not Class A. Refusing
    an identity here is what keeps two genuinely different Huatai openings from
    being treated as one — the exact false positive this fix was written to
    avoid.
    """
    head, sep, req = slug.rpartition("_")
    return bool(sep) and any(c.isdigit() for c in req)


def provider_identity(url: str) -> tuple[str, ...] | None:
    """The real posting `url` points at, as a hashable key, or None when the
    URL's provider gives no id we can trust.

    None is the honest and common answer. It means "this URL is the only
    handle we have on this posting", which leaves the existing `(firm, url)`
    behaviour exactly as it was.
    """
    u = (url or "").strip()
    if not u:
        return None

    m = _TALNET_RE.match(u)
    if m:
        return ("talnet", m.group("host").lower(), m.group("opp"))

    m = _ICIMS_RE.match(u)
    if m:
        return ("icims", m.group("host").lower(), m.group("job"))

    m = _WORKDAY_RE.match(u)
    if m and _workday_has_req(m.group("slug")):
        return ("workday", m.group("host").lower(), m.group("site"), m.group("slug"))

    return None


def identity_fragment(identity: Sequence[str]) -> str:
    """A substring that MUST appear in any URL carrying `identity`, for
    narrowing a database lookup before the exact check.

    Narrowing only — it is allowed to over-match (`/opp/1459` also matches
    `/opp/14590`), because every candidate it returns is then confirmed with
    `provider_identity` equality. It must never UNDER-match, which is why each
    fragment is taken verbatim out of the pattern that produced the identity.
    """
    kind = identity[0]
    if kind == "talnet":
        return f"/opp/{identity[2]}"
    if kind == "icims":
        return f"/jobs/{identity[2]}/"
    if kind == "workday":
        return f"/{identity[3]}"
    raise ValueError(f"unknown identity kind {kind!r}")


# --------------------------------------------------------------------------- Class B

# Curly quotes, en/em dashes and non-breaking spaces are the same characters to
# a reader and different bytes to a comparison. Deutsche Bank's Jaipur
# apprentice intake posts "Apprentice hiring for 2026 – 2027" and "Apprentice
# Hiring for 2026- 2027" for what is plainly the same programme.
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-",
                         "–": "-", "—": "-", "―": "-",
                         "‘": "'", "’": "'",
                         "“": '"', "”": '"',
                         " ": " "})
_EDGE_NOISE = re.compile(r"^[\s\-–—,;:.·|/]+|[\s\-–—,;:.·|/]+$")
# A poster reaches for '-', ',', '/', ':', ';' or '|' near-interchangeably to
# join the same two clauses of a title, and sometimes uses no separator at
# all. Confirmed live: DRW's Cumberland/FICCO req is titled with a comma on
# one Greenhouse posting and a dash on the sibling posting of the SAME words
# (ids 3402/3398 New York, 3401/3399 Greenwich); Fidelity International's
# Tokyo "Wholesale Sales[,] Senior Manager" the same way (ids 1363/1360,
# comma vs dash, both independently verified open); a read-only sweep of all
# 15,479 open rows grouped by firm+location turned up 19 more firm+location
# groups differing ONLY in which of these characters (or none) joined the
# same words — MUFG ("AVP-" vs "AVP " with no separator at all), TD
# Securities, Morgan Stanley (comma+slash vs comma+" / "), PwC Makati,
# Raymond James, Ares, Societe Generale, SIG (dash vs pipe) and Barclays Pune
# (dash / en-dash / no separator, one 7-row cluster). The previous version of
# this function canonicalized whitespace AROUND an existing dash or comma
# into two different fixed forms (space-free "-" vs ", ") but never equated
# the two separator styles with each other or with slash/colon/pipe/no-
# separator renderings, so every pair above hashed to a different
# `duplicate_key()` and fold_duplicates() left both rows on the board.
# Collapsing every one of these characters straight to a space — rather than
# canonicalizing toward one preferred spelling — folds all of the shapes at
# once, including "no separator" for free, while still leaving genuinely
# different WORDS (PwC Barcelona's "Legal" vs "Fiscal" practice areas) apart.
_SEPARATORS = re.compile(r"[-,:;/|]")


def normalize_label(value: str) -> str:
    """Casefolded, whitespace- and punctuation-insensitive form of a title or
    location, for asking "is this the same words?" — never for display."""
    s = (value or "").translate(_DASHES).casefold()
    s = _SEPARATORS.sub(" ", s)
    s = " ".join(s.split())
    return _EDGE_NOISE.sub("", s)


# normalize_label already equates punctuation and separator style ("Wholesale
# Sales, Senior Manager" == "Wholesale Sales - Senior Manager"), but leaves
# WORD ORDER as a hard divider. A poster reorders the same clause as often as
# they reswap its separator: Brookfield posts the identical opening as both
# "Manager, Finance" (id=955) and "Finance Manager" (id=10488), same firm,
# same Toronto location, both open — and a full bag-of-words sweep of every
# open row (round 5) found 18 such groups across Stifel, TD Securities,
# Barclays, PwC, Morgan Stanley, Deutsche Bank, Point72 ("Quantitative
# Researcher - Macro" / "Macro Quantitative Researcher") and Blue Owl ("Real
# Assets Accounting, Senior Associate" / "Senior Associate - Real Assets
# Accounting"). Sorting the normalized words makes the key order-insensitive
# on top of separator-insensitive.
#
# REPORT-ONLY, DELIBERATELY. This feeds `duplicate_key()` — the
# dedupe_opportunities LOOKALIKE listing, which only prints candidates for a
# human to judge — and NOT `fold_duplicates()`, which hides cards from the
# live feed. Word order alone is too weak a signal to hide a job on:
# Brookfield genuinely runs "Finance Manager" (id=955, Infrastructure) and
# "Manager, Finance" (id=10488, Energy) as two different open Toronto
# requisitions, both deadline-less, so neither the deadline veto nor location
# clustering would save that pair. A false SPLIT costs a student a scroll; a
# false FOLD costs them a job they never saw. fold_duplicates() therefore
# stays word-order strict until some corroborating signal (business unit, or
# the same requisition id) can tell an anagram apart from a genuine repost.
def _title_bag(title: str) -> tuple[str, ...]:
    """Sorted normalized words of a title, for "same words, any order?"."""
    return tuple(sorted(normalize_label(title).split(" ")))


def duplicate_key(row: Any) -> tuple[Any, tuple[str, ...], str]:
    """The grouping key: same firm, same words for the role (any order),
    same words for the place."""
    return (
        getattr(row, "firm_id", None),
        _title_bag(getattr(row, "title", "")),
        normalize_label(getattr(row, "location", "")),
    )


# Workday's LIST endpoint hands over only an aggregate count ("2 Locations")
# once a posting carries more than one location entry — never the city names
# — and that count is stored verbatim into `Opportunity.location`
# (enrich_postings.py's own `_N_LOCATIONS_PLACEHOLDER` documents the same
# shape on the fetch side). A count is not a place: Franklin Templeton's
# "Head of Global Talent Acquisition" is one real global hire split into a
# US-tagged Workday req (location "4 Locations") and a UK-tagged req
# ("2 Locations") for candidate-sourcing purposes — both postings' own body
# text states the identical 6-city pool, and both carry
# `raw["detail_location"] = None`, i.e. Coverage never recovered real
# place-name text for either row. Treating "4 Locations" and "2 Locations" as
# two DIFFERENT stated cities (which is what plain `normalize_label` did —
# they simply don't casefold to the same string) makes the placeholder a
# hard divider it was never entitled to be. `_cluster_by_location` folds it
# into the blank bucket instead, the same bucket a genuinely missing location
# already falls into.
_N_LOCATIONS_PLACEHOLDER = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)

# A location string is sometimes a real city wrapped in a qualifier that
# names something OTHER than a place: a country/region code, or (on DBS's
# Workday board specifically) the posting entity's own internal code. Neither
# is a place name, so leaving it in the comparison invents a hard divider
# between two rows that mean the same city — confirmed live on three DBS
# pairs sharing one requisition-shape each: "PRC - Shanghai" / "Shanghai"
# (WD80532 / WD83461), "Vadodara-DBIL" / "Vadodara" (WD85424 / WD87755), and
# "Ahmedabad-DBIL" / "Ahmedabad" (WD85423 / WD86836) — "DBIL" is DBS Bank
# India Limited's own entity code, not a place.
#
# This is deliberately a curated allow-list, not a general "strip the extra
# word" rule. A bare word-superset rule was checked against a full sweep of
# DBS's 721 open rows and it ALSO fires on "Ghatkopar Mumbai" vs "Mumbai" and
# "New Taipei" vs "Taipei" — a real Mumbai suburb and a real, administratively
# separate city, not qualifier noise. Only tokens confirmed to name something
# other than a place are ever stripped; every other extra word stays a hard
# divider, per this module's own rule for genuinely different cities.
_LOCATION_QUALIFIERS = frozenset({"prc", "dbil"})


def _location_cluster_key(location: str) -> str:
    """`normalize_label(location)` for CLUSTERING only: known non-place
    qualifier tokens dropped, and the Workday "N Locations" placeholder
    folded to the same blank key a missing location already uses. Never used
    for display, and never for `duplicate_key()` — that function's whole job
    is to stay the strict, unforgiving form of the location."""
    normalized = normalize_label(location)
    if _N_LOCATIONS_PLACEHOLDER.match(normalized):
        return ""
    words = [w for w in normalized.split(" ") if w not in _LOCATION_QUALIFIERS]
    return " ".join(words)


def _survivor_rank(row: Any, sticky_ids: frozenset) -> tuple:
    """Which copy the student should see. Lower sorts first.

    The order is fixed and deliberately boring, so the board shows the same
    copy on every request and the choice can be argued with:

    1. A copy the student has already tracked or applied to. Showing the OTHER
       copy would render their own pipeline state as if it were never set.
    2. A copy with a deadline, over one without. A date is the single most
       actionable fact the card carries.
    3. A copy with a location, over one without. Same reason, weaker fact —
       tal.net's second pool routinely drops the city its first pool states.
    4. The one seen first. It is the row that has been on the board longest,
       so it is the one any link or memory points at.
    5. Lowest id, purely so the result is total and never depends on input
       order.
    """
    first_seen = getattr(row, "first_seen", None)
    return (
        0 if getattr(row, "id", None) in sticky_ids else 1,
        0 if getattr(row, "deadline", None) else 1,
        0 if (getattr(row, "location", "") or "").strip() else 1,
        (0, first_seen) if first_seen is not None else (1, None),
        getattr(row, "id", 0) or 0,
    )


def _cluster_by_location(members: list[Any]) -> list[list[Any]]:
    """Split same-firm, same-title rows into one cluster per place.

    A stated city is a hard divider: "Summer Analyst, London" and "Summer
    Analyst, New York" are two jobs and folding them would delete a city from
    the board. A BLANK location is not a place, though — it is a missing
    answer, and tal.net's second candidate pool drops the city its first pool
    states on the very same posting. So blanks are absorbed into the stated
    cluster when there is exactly ONE candidate to absorb them into, and left
    alone as their own cluster whenever the title spans several cities, where
    guessing which one they belong to would be exactly that, a guess.
    """
    by_place: dict[str, list[Any]] = {}
    for row in members:
        by_place.setdefault(_location_cluster_key(getattr(row, "location", "")), []).append(row)

    blanks = by_place.pop("", [])
    if blanks:
        if len(by_place) == 1:
            next(iter(by_place.values())).extend(blanks)
        else:
            by_place[""] = blanks
    return list(by_place.values())


def fold_duplicates(
    rows: Iterable[Any], *, sticky_ids: Iterable[int] = (),
) -> tuple[list[Any], int]:
    """`(rows_to_show, how_many_were_folded)`, preserving input order.

    Rows fold only when the firm, the role and the place all say the same
    thing. The database is never touched: this hides a copy from one render,
    and every row goes on being verified and closed independently, which is
    the whole point — two requisitions with the same title genuinely can close
    on different days.

    THE VETO: a cluster holding two or more DIFFERENT stated deadlines is left
    completely alone. Identical title and city with different act-by dates is
    the signature of a repeating series rather than a duplicate — Bank of
    America runs "Quantitative Strategies & Data Group | Recruitment" on both
    2026-08-18 and 2026-09-09, and BOCI posts several — and hiding either would
    cost the student a date they can still make. A cluster where only SOME
    copies state a deadline still folds, keeping the dated copy: that is one
    posting recorded twice at different completeness, not two events.
    """
    sticky = frozenset(sticky_ids)
    rows = list(rows)

    by_role: dict[tuple, list[Any]] = {}
    for row in rows:
        key = (getattr(row, "firm_id", None),
               normalize_label(getattr(row, "title", "")))
        by_role.setdefault(key, []).append(row)

    keep: set[int] = set()
    folded = 0
    for members in by_role.values():
        if len(members) == 1:
            keep.add(id(members[0]))
            continue
        for cluster in _cluster_by_location(members):
            if len(cluster) == 1:
                keep.add(id(cluster[0]))
                continue
            stated = {d for d in (getattr(m, "deadline", None) for m in cluster)
                      if d is not None}
            if len(stated) > 1:
                keep.update(id(m) for m in cluster)
                continue
            winner = min(cluster, key=lambda m: _survivor_rank(m, sticky))
            keep.add(id(winner))
            folded += len(cluster) - 1

    return [r for r in rows if id(r) in keep], folded
