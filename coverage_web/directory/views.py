"""Public opportunities UI (docs/build-plan.md §7 M1): the urgency feed plus
per-firm detail pages.

This is the trust surface. The whole product's credibility with a
spreadsheet-native audience rests on the listing being *real* — so the
honesty markers (confidence band, staleness age, "no deadline posted",
confirmed-vs-rumored cycle dates) are computed here, in plain Python, and
handed to dumb templates as pre-formatted dicts. Two reasons for putting
the logic in the view rather than template tags:

- Testability/greppability: `pytest coverage_web/directory` can assert on
  the marker functions directly, and a reader can find every honesty rule
  in one file.
- Scope: this agent owns only views.py, urls.py, and templates/directory/.
  A `templatetags/` package would live outside that boundary.

Nothing here mutates data or touches the private zone except the
read-only `UserFirm.objects.for_user(...)` calls that flag a signed-in
user's targeted firms in the feed.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import date

from collections import Counter, defaultdict, namedtuple
from functools import lru_cache

from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, F, IntegerField, Max, Q, Value, When
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.timesince import timesince
from django.views.decorators.http import require_POST

from analytics.events import record_event
# Read-only, cross-app import (build-plan.md §2's private zone). directory
# never writes crm rows; the opportunities feed only reads UserFirm via the
# tenant-scoped manager. No import cycle: crm.models imports directory.models.
from crm import campaigns as crm_campaigns
from crm.models import Contact, UserFirm
# `_calendar_days_ago` — same cross-app read as the two imports above. This
# feed's "first seen Nd ago" used to be a raw `(now - first_seen).days`
# elapsed floor, timezone-independent and effectively `elapsed_hours // 24`.
# `crm.utils._calendar_days_ago` is the product's declared single source of
# truth for "how many days ago" (its own docstring names the failure mode:
# two surfaces disagreeing once elapsed time crosses a local calendar-date
# boundary). Measured on the founder's live board 2026-09-01: of the 2,339
# undated open campus rows that print "first seen Nd ago", the raw floor and
# the calendar-date diff disagreed on 2,202 of them (94%). See `_urgency_item`.
from crm.utils import _calendar_days_ago
from directory.classify import (
    BUCKET_LABELS, ENTRY_LEVEL, INSIGHT, INTERNSHIP, OTHER, REGION_LABELS,
    REGION_ORDER, RETIRED_TRACKS, SELECTABLE_TRACKS, TARGET_BUCKETS,
    derive_class_year,
    TRACK_LABELS as _TRACK_LABELS_BASE,
)
# The one definition of "closing soon" — see deadlines.py for why it isn't
# spelled out at each call site (and for the crm/views.py follow-up).
from directory.deadlines import (
    CLOSING_SOON_DAYS,
    closing_soon_window,
    is_closing_soon,
    is_posting_closed,
)
from directory import estimates
from directory.boards import UNREACHABLE_BY_POLICY
from directory.dupes import fold_duplicates, fold_families
from directory.facts import paragraphs
from directory.models import Firm, Opportunity
from directory.open_runs import (
    CYCLE_OBSERVATION_MIN_SAMPLE as _CYCLE_OBSERVATION_MIN_SAMPLE,
    onboarding_cutoffs,
    open_run_days,
)
from directory.recommend import (
    DEFAULT_LIMIT, MIN_SCORE, Candidate, Profile, parse_target_cycle, recommend,
    role_function, score_candidate,
)
from directory.sponsorship import effective_sponsorship, firm_policy_map, firm_policy_q
from directory.timeline import (
    EVENT_LABELS, TRACK_SHORT, cycle_slug_for_target,
)

# Firm category labels — the insider taxonomy students actually sort firms
# by ("who has coverage on this account" energy, per the brand voice). Keyed
# by Firm.slug; display-only, never used for filtering logic. Firms outside
# the map fall back to their primary track label.
FIRM_CATEGORIES = {
    # Bulge bracket
    "gs": "Bulge Bracket", "ms": "Bulge Bracket", "jpm": "Bulge Bracket",
    "bofa": "Bulge Bracket", "baml": "Bulge Bracket", "citi": "Bulge Bracket",
    "barclays": "Bulge Bracket", "db": "Bulge Bracket", "ubs": "Bulge Bracket",
    # Elite boutiques
    "evercore": "Elite Boutique", "lazard": "Elite Boutique", "pjt": "Elite Boutique",
    "moelis": "Elite Boutique", "centerview": "Elite Boutique",
    "rothschild": "Elite Boutique", "guggenheim": "Elite Boutique",
    "solomonpartners": "Elite Boutique", "pwp": "Elite Boutique",
    "qatalyst": "Elite Boutique", "greenhill": "Elite Boutique",
    # Middle market / full-service
    "williamblair": "Middle Market", "hl": "Middle Market", "baird": "Middle Market",
    "pipersandler": "Middle Market", "raymondjames": "Middle Market",
    "jefferies": "Middle Market", "rbc": "Global Bank", "wf": "Global Bank",
    "mizuho": "Global Bank", "stanchart": "Global Bank", "hsbc": "Global Bank",
    "nomura": "Global Bank", "bnp": "Global Bank", "socgen": "Global Bank",
    "huatai": "Global Bank", "cs": "Global Bank",
    # Private markets
    "blackstone": "Private Markets", "kkr": "Private Markets", "apollo": "Private Markets",
    "carlyle": "Private Markets", "tpg": "Private Markets", "baincapital": "Private Markets",
    "ares": "Private Markets", "oaktree": "Private Markets", "blueowl": "Private Markets",
    "brookfield": "Private Markets", "warburg": "Private Markets", "silverlake": "Private Markets",
    # Asset managers
    "blackrock": "Asset Manager", "wellington": "Asset Manager", "invesco": "Asset Manager",
    "fidelityintl": "Asset Manager", "fidelity": "Asset Manager", "pimco": "Asset Manager",
    "troweprice": "Asset Manager", "capitalgroup": "Asset Manager",
    # Consulting
    "mckinsey": "Consulting", "bain": "Consulting", "bcg": "Consulting",
    "oliverwyman": "Consulting", "brattle": "Consulting", "pwc": "Consulting",
    # Tech
    "palantir": "Tech", "google": "Tech", "microsoft": "Tech", "amazon": "Tech",
    "tencent": "Tech", "bytedance": "Tech",
}

# Human labels for the firms.yaml track slugs. Raw slugs ("ib", "corp-strat")
# read as internal shorthand; the filter is public-facing. The labels are
# classify.TRACK_LABELS — the SAME dict accounts/forms.py's Settings
# checkboxes read — so the two pages can never disagree about a slug's label
# again (they used to: Settings said "Private Equity", this filter said
# "Private Equity / Credit", both for "pe"). Labelling a slug is not offering
# it: which tracks are OFFERED is classify.SELECTABLE_TRACKS, which
# `_track_facet` filters its options through.
TRACK_LABELS = {
    **_TRACK_LABELS_BASE,
    # MLT and SEO Career, the two firms on this track, are not employers —
    # they are access programmes that place students INTO the firms above.
    # The slug had no label at all, which is why /firms/mlt/ printed the bare
    # word PIPELINE where every other firm printed a desk. Not a preference
    # option (classify.TRACKED_TRACKS excludes it) — display-only here.
    "pipeline": "Career Access Programme",
}


# EVENT_LABELS (the firm_dates event vocabulary) now lives in
# directory/timeline.py, shared between the firm-detail table and the
# cycle-timeline heat map.


# ---------------------------------------------------------------------------
# Honesty markers — small pure functions, each independently testable.
# ---------------------------------------------------------------------------

def confidence_marker(value):
    """Render a raw 0.0-1.0 confidence float as an honest band.

    `0.0` (the common early state — nothing has scored this yet) reads as
    "unrated", not "low": collapsing "we haven't assessed this" into a
    confident-sounding low score would be the exact silent lie the brand
    refuses to tell.
    """
    value = value or 0.0
    if value <= 0:
        return {"level": "unrated", "label": "unrated", "value": 0.0, "pct": 0}
    if value >= 0.8:
        level = "high"
    elif value >= 0.5:
        level = "medium"
    else:
        level = "low"
    return {"level": level, "label": level, "value": round(value, 2), "pct": round(value * 100)}


# Where a deadline came from. `Opportunity.confidence` is 1.0 when the
# provider handed us the date in a structured field and below it when
# `enrich_postings` read it out of the posting's own prose. The vast majority
# of dated campus roles are the second kind — see `PROSE_READ_DEADLINES`
# below for the count and the day it was taken. Both are worth showing; only
# one of them is a quotation of a field, and a page that renders them
# identically is claiming a certainty it does not have.
#
# "Reported" rather than "unconfirmed": the date IS what the posting says. The
# word is about the reading being ours, not about doubting the firm.
_CONFIRMED_AT = 1.0

#: THE MEASUREMENT, IN ONE PLACE, WITH THE DAY IT WAS TAKEN.
#:
#: This used to be two bare numbers in the comment above and they went stale
#: twice: "92 of the 121" survived until the board had nearly trebled (2.8x
#: off), and the "327 of the 341" that replaced it was 8% off within a day.
#: A share of the board is not a constant — it moves every time a connector
#: runs — so a number written in prose with nothing to check it against is a
#: claim with a half-life.
#:
#: Three things follow from that, and they are the whole design here.
#: (1) The figures live in ONE object, so a reader and a future editor cannot
#: find two copies. (2) The object carries `measured_on`, so anyone can see
#: how old the claim is without reading git blame. (3) `query` names the
#: exact predicate the numbers count, so re-taking the measurement is
#: mechanical rather than a reconstruction — run that query, replace these
#: three values, move the date.
#:
#: WHY NOT A TEST THAT RE-COUNTS THE LIVE BOARD. That was the preferred shape
#: and it is not available: pytest runs against an empty per-worktree test
#: database (`settings/base.py`'s `TEST.NAME`), so a test asserting these
#: numbers against `Opportunity.objects` would assert 0 == 354 and the only
#: way to make it pass would be to weaken it into meaninglessness. What a
#: test CAN pin, and `test_feed_honesty.py` does, is that the predicate named
#: in `query` is the predicate `deadline_provenance` actually branches on,
#: and that the date is present and parseable — so the numbers can go stale
#: but they can never describe a different question than the code asks.
PROSE_READ_DEADLINES = {
    "measured_on": date(2026, 9, 2),
    "query": ("status='open', bucket in TARGET_BUCKETS, deadline is not null, "
              "confidence < _CONFIRMED_AT"),
    "dated_open_campus": 407,
    "prose_read": 354,
}


def deadline_provenance(opp) -> dict | None:
    """"Reported" for a prose-read deadline, None for a stated one."""
    if opp.deadline is None or (opp.confidence or 0) >= _CONFIRMED_AT:
        return None
    return {"label": "reported",
            "why": "Read from the posting's own text, not a field the board published"}


# Precisions whose LABEL refuses to name a day. The rule the two halves of
# this function keep between them: a countdown may not count days on a date
# the label just declined to print as a day. "~ Sep 2026" beside "closes in 4
# days" is one row stating a month-level guess and an exact day count about
# the same date, and the reader believes the specific one — which is how a
# `~`-prefixed estimate reaches a student as a deadline to plan around.
#
# A GUARD, NOT A LIVE PATH — and the distinction is the point of this note.
# Measured 2026-09-02: `Opportunity.deadline_precision` holds exactly two
# values across the whole table, "" (15,621 rows) and "day" (624), and ZERO
# rows carry "month" or "estimated". Everything this constant gates is
# therefore dead on today's data, and a reader who assumes otherwise will
# spend time reasoning about a rendering path nothing reaches.
#
# It stays anyway, and is not a leftover. `deadline_precision` is a bare
# `CharField` with no vocabulary constraint and a fully editable
# `OpportunityAdmin` over it, so a `month` value is one admin save away —
# exactly like the `confidence=95.0` write that
# `opportunities_confidence_in_range` exists for. `FirmDate` already carries
# 25 `estimated` rows through the same kind of column, so the value is not
# hypothetical, only absent HERE. The failure it prevents is silent: without
# it a month-level guess renders beside an exact day countdown, and the
# reader believes the specific one.
_INEXACT_PRECISIONS = ("month", "estimated")


def _month_distance(deadline, today) -> int:
    """Whole calendar months from `today`'s month to `deadline`'s. 0 = this
    month. Deliberately month arithmetic and not `days // 30`: the unit has
    to be the one the label prints, or the countdown is a day count wearing
    a coarser word."""
    return (deadline.year - today.year) * 12 + (deadline.month - today.month)


def deadline_marker(deadline, precision, *, today=None):
    """Format a deadline honestly, respecting its stated precision, and
    never fabricating one. A null deadline says so out loud, and an inexact
    one gets a countdown in its own unit rather than a day count it cannot
    support — see `_INEXACT_PRECISIONS`.

    LABEL, NOT "No deadline posted". This is the one function the firm page
    (`_card.html`, `_role_drawer.html`) reads for that fact, and until
    2026-09-01 it said "No deadline posted" — a third wording for the exact
    claim the feed makes as "No date posted, first seen Nd ago"
    (`_urgency_item`, test-pinned in `test_feed_honesty.py` and
    `test_styles_block.py`) and My Applications makes as bare "No date
    posted" (`_apps_body.html`, hardcoded rather than reading this label at
    all). "First seen", never "posted", is the feed's own considered
    wording — a bulk import once wore the word "New" on 794 of 805 roles,
    which is the same category of overclaim "posted" makes about a date
    nobody stated. Standardized to "No date posted" here, the bare half of
    the feed's phrase, not the full "first seen Nd ago" one: this function
    has no `first_seen` in scope (only `deadline`/`precision`/`today`), and
    both its callers already carry a DIFFERENT freshness fact where the feed
    has none — the role drawer prints `checked_ago` (when we last verified
    the posting is still live), and both the drawer and the firm row mark
    `unconfirmed` (`_unconfirmed_note`) when that last check could not
    reconfirm the URL. Adding "first seen" there would be a second
    elapsed-time clause on a single-role view already carrying one; the
    feed's version earns its place because triaging ~2,600 rows at once is
    exactly the job "how long has Coverage known about this" is useful for,
    and neither of these two single-role views is that job.
    """
    if deadline is None:
        return {"posted": False, "label": "No date posted", "countdown": "", "past": False}
    prec = (precision or "").lower()
    if prec == "month":
        label = f"{deadline:%b %Y}"
    elif prec == "estimated":
        label = f"~ {deadline:%b %Y}"
    else:  # "day" or unspecified -> the exact date
        label = f"{deadline:%b} {deadline.day}, {deadline.year}"

    today = today or timezone.localdate()
    if prec in _INEXACT_PRECISIONS:
        # `past` moves to the same coarser unit as the countdown. A "Sep 2026"
        # deadline is not passed on Sep 15 — nothing ever said which September
        # day it was, so the danger-red "past" styling would be asserting a
        # day the row does not hold, in the other direction.
        months = _month_distance(deadline, today)
        if prec == "estimated":
            # No "closes": the date is our estimate, not the firm's statement,
            # and the verb is what makes it read as one.
            countdown = ("estimated date passed" if months < 0 else
                         "estimated this month" if months == 0 else
                         "estimated next month" if months == 1 else
                         f"estimated in {months} months")
        else:
            countdown = ("deadline passed" if months < 0 else
                         "closes this month" if months == 0 else
                         "closes next month" if months == 1 else
                         f"closes in {months} months")
        return {"posted": True, "label": label, "countdown": countdown,
                "past": months < 0, "precision": prec}

    days = (deadline - today).days
    if days < 0:
        countdown = "deadline passed"
    elif days == 0:
        countdown = "closes today"
    elif days == 1:
        countdown = "closes tomorrow"
    else:
        countdown = f"closes in {days} days"
    return {"posted": True, "label": label, "countdown": countdown, "past": days < 0, "precision": prec}


def _class_tag(opp) -> dict | None:
    """A "Class of YYYY" pill, but ONLY from `Opportunity.class_year` — i.e.
    only when the posting itself said it.

    This replaced a display heuristic that derived the tag from `cohort` by
    adding a year for internships and insight programmes. It read confidently
    and it was wrong: `cohort` is the programme/intake year ("2027 Summer
    Internship - Account Analyst, Tokyo"), so the old rule stamped a
    graduation year on every dated posting when only ~3 titles in the whole
    open set state one. Firms differ on which class a given summer programme
    targets, degrees run different lengths, and the offset flips by region.
    Guessing that in a pill labelled "Class of" is exactly the silent lie the
    honesty markers elsewhere in this file exist to avoid. No stated class
    year -> no pill; the programme year still shows, labelled as such."""
    if not (opp.class_year or "").strip():
        return None
    return {"label": f"Class of {opp.class_year}", "css": "tag-class"}


# The sponsorship PILL used to live here, as `_sponsorship_tag`. It is gone,
# not moved: it reached exactly one surface — `/firms/<slug>/`, via `_card`'s
# tags — while the Opportunities FEED, where the filter runs and the decision
# actually gets made, built its own sponsorship chip off the raw
# `Opportunity.sponsorship` column and therefore showed a firm-policy row
# NOTHING at all. Filtering "Sponsors visas" returned 304 such rows with no
# label saying why they matched, which is the opposite of the promise the
# whole four-state design exists to keep. On the firm page the two carriers
# also collided outright: two Barclays rows printed the pill "No Sponsorship"
# beside the chip "No sponsorship", one posting field in two visual languages.
#
# `_fact_chips` is now the single carrier on every surface, and it keeps the
# rule the pill established: a firm-sourced answer says "· firm policy" IN THE
# LABEL rather than borrowing the posting-stated wording, because a firm's
# general policy is a weaker claim about one specific role than the posting's
# own words — the same asymmetry that makes `_eligibility` block on one and
# only warn on the other.


# Where a role IS, resolved once for every surface that prints it.
#
# The firm page and the feed disagreed about the same empty `location`. The
# firm row printed the literal "Location not listed" (19 times on /firms/hsbc/,
# 21 on /firms/bofa/); the feed card omitted the span entirely (51 of 666 cards
# rendered an empty `.rolecard-sub`). One role read location-unknown on one
# page and location-silent on the other.
#
# "Location not listed" was also the wrong claim on its own page. Every one of
# HSBC's 19 campus rows names its city inside its own TITLE — "New York
# Investment Banking Graduate NY 10001" above the words "Location not listed"
# — and `_card` already carried `opp.region`, so the page held the market and
# said it did not.
#
# So: the city when the posting gave one, the market when it gave only that,
# and silence when it gave neither. Silence rather than a sentence, because
# the feed's quiet cards were the honest half of the disagreement.
#
# `other` is deliberately NOT a fallback. It means "somewhere outside the six
# markets we track", which is a filter bucket, not a place — printing "Other
# Markets" where a reader expects a city states nothing and looks like a bug.
# `global` IS one: Bank of America's virtual recruitment events and KKR's
# talent community are placeless BY DESIGN, and "Global / Virtual" is the fact.
_PLACE_FALLBACK = {r: REGION_LABELS[r] for r in REGION_LABELS if r != "other"}

_PLACE_WHY = ("The posting did not state a city. This is the market it was "
              "filed under.")

# ---------------------------------------------------------------------------
# TIDYING A SCRAPED PLACE STRING, AT RENDER TIME.
#
# `location` is evidence and stays raw in the database (the same rule that
# keeps `smart_title`/`smart_location` in the template layer). But `_place`
# handed that raw string straight to the page, so the place line — the one
# element a student reads to answer "is this in a market I can work in" —
# arrived in whatever shape a careers site happened to emit. Measured on the
# live corpus (2,599 open campus roles / 16,561 open rows):
#
#   "2 Locations"                                97 feed / 1,321 open  a COUNT
#   "9-10 TAUNUSANLAGE:FRANKFURT AM MAIN"         5 feed /    21 open  colon join
#   "NY 10001"                                    8 feed /     8 open  a ZIP
#   "Denver, CO, US, 80206"                      29 feed /   108 open  postal tail
#   "New York, 745 7th Avenue"                   21 feed /   270 open  street
#   "Chicago, …; Greenwich, …; Houston, …" ×6    33 feed /   123 open  list, 160ch
#   "Online via Microsoft Teams&#160"             1 feed /     1 open  entity
#
# 172 of 2,599 feed place lines (6.6%) were one of those. The two existing
# repair paths cannot close it: `normalize_workday_locations` has already
# been run to exhaustion (0 of 11,350 open Workday rows would change today)
# and its slot-gap rule does not see a colon join, while
# `backfill_detail_locations` needs a stored `detail_location` that 0 of the
# 1,321 "N Locations" rows actually have. So the gap is in the NORMALIZATION,
# and the render path is where it can be closed for every source at once.
#
# EVERY RULE HERE SUBTRACTS. Nothing is inferred, completed, or reworded: a
# segment is either kept as the source wrote it or dropped, and whatever was
# dropped is still quoted back in the `why` tooltip. That is the difference
# between tidying and inventing, and only the first is allowed on this page.

# Workday joins a site address to its city with a BARE colon and no spaces
# ("90 WESTERN PKY:BEDFORD", "RBC WATERPARK PLACE, 88 QUEENS QUAY W:TORONTO").
# The city is the right half in every one of the 7 distinct shapes this
# produces across all 24,775 rows — with ONE counter-example that decides the
# guard below: "Istanbul, Büyükdere Caddesi No:175", where the colon is part
# of a house number and the right half is not a place at all. So the split
# only fires when the right half reads like a place AND the left reads like
# an address; anything else keeps the string it arrived with. Spaces around
# the colon mean something else again (Evercore's "Crum Auditorium : RRH
# 1.400" is a room), which the lookarounds exclude.
_PLACE_COLON = re.compile(r"(?<=\S):(?=\S)")

# Workday's aggregate placeholder for a multi-city posting. It is a count, not
# a place, and answers nothing a student asked.
_PLACE_COUNT = re.compile(r"^\d+\s+locations?$", re.I)

# One place among several. Boards list them semicolon-separated.
_PLACE_LIST = re.compile(r"\s*;\s*")

# Segment separators INSIDE one place: a comma, or a spaced dash ("Toronto -
# 18 York Street"). Unspaced dashes are part of names ("Santander-Platz").
_PLACE_SEG = re.compile(r"\s*,\s*|\s+[-–]\s+")

# A whole segment that is only a postal code: 10001, 10001-1234, 65760,
# L-1855 (Luxembourg), K1A 0B1 (Canada), EC2N 4AY (UK).
_PLACE_POSTAL = re.compile(
    r"^(?:\d{4,6}(?:-\d{4})?"
    r"|[A-Z]{1,2}-\d{3,5}"
    r"|[A-Z]\d[A-Z][ ]?\d[A-Z]\d"
    r"|[A-Z]{1,2}\d[A-Z\d]?[ ]?\d[A-Z]{2})$", re.I)

# "NY 10001" — a state code with a ZIP welded on. The state is the fact; the
# ZIP is leaked source data. (These 8 rows are the residue of
# `backfill_sitemap_postal_titles`, which correctly moved the code OUT of the
# title and into `location`, where nothing then trimmed it.)
_PLACE_STATE_ZIP = re.compile(r"^([A-Z]{2})\s+\d{5}(?:-\d{4})?$")

# A street address rather than a place. Two shapes, both requiring a NUMBER:
# a house-number head ("745 7th Avenue", the same anchored rule the Workday
# connector uses), or a street/premises word standing beside a standalone
# digit run ("Marina Bay Financial Tower 2"). The digit is load-bearing —
# without it "Ave Maria, FL" and "Avenue Road" would read as addresses. So
# would every "St. Louis" if `st` were in the word list, which is why it is
# not: only words that are unambiguous outside an address qualify.
_PLACE_STREET_HEAD = re.compile(r"^\d{1,6}(?:[-/]\d{1,6})?[ ,-]\S")
_PLACE_STREET_WORD = re.compile(
    r"\b(street|road|avenue|ave|boulevard|blvd|parkway|pkwy|pky|quay|plaza|"
    r"suite|floor|tower|building|bldg|anlage|strasse|straße)\b\.?", re.I)
_PLACE_DIGIT_RUN = re.compile(r"\b\d{1,6}\b")


def _is_street(segment: str) -> bool:
    return bool(_PLACE_STREET_HEAD.match(segment)
                or (_PLACE_STREET_WORD.search(segment)
                    and _PLACE_DIGIT_RUN.search(segment)))


def _split_colon_join(text: str) -> str:
    """Workday's `<site address>:<CITY>` run, reduced to the city."""
    m = None
    for m in _PLACE_COLON.finditer(text):
        pass                      # the LAST bare colon is the site/city seam
    if m is None:
        return text
    left, right = text[:m.start()].strip(), text[m.end():].strip()
    place_like = (any(ch.isalpha() for ch in right)
                  and not _PLACE_POSTAL.match(right))
    address_like = bool(_PLACE_DIGIT_RUN.search(left)
                        or _PLACE_STREET_WORD.search(left))
    return right if (place_like and address_like) else text


def _tidy_entry(entry: str) -> str:
    """One place, minus the parts of it that are not a place.

    Returns `entry` UNCHANGED when nothing is dropped. That is not an
    optimisation: `_PLACE_SEG` splits on a spaced dash so the street rule can
    see "Toronto - 18 York Street", and re-joining on ", " unconditionally
    would have rewritten 1,100-odd innocent rows' punctuation ("Singapore -
    Central" -> "Singapore, Central") for no gain. Tidying subtracts; it does
    not restyle a line it has no complaint about.
    """
    segs = [s.strip() for s in _PLACE_SEG.split(entry) if s.strip()]
    trimmed = [_PLACE_STATE_ZIP.sub(r"\1", s) for s in segs]
    kept = [s for s in trimmed if not _PLACE_POSTAL.match(s)]
    # A row whose EVERY segment is a postal code ("L-1855") keeps them: an
    # empty place line is a worse answer than an imprecise one, and silence
    # here would claim the posting said nothing when it did.
    kept = kept or trimmed
    # Same guard for the street rule — "1 New York Plaza" is a whole location
    # and dropping it would leave the card blank, so a street segment only
    # goes when a place-shaped segment survives it.
    place_like = [s for s in kept if not _is_street(s)] or kept
    if place_like == segs:
        return entry.strip()
    return ", ".join(place_like)


def tidy_place(raw: str) -> tuple[str, int]:
    """The printable form of a scraped `location`, and how many further places
    the same string listed.

    `("", 0)` when the string carries no place at all (Workday's "2
    Locations"), so the caller can fall through to the market the row was
    filed under instead of printing a count where a city goes.
    """
    text = html_lib.unescape(raw or "").replace("\xa0", " ")
    text = _split_colon_join(text)
    text = re.sub(r"\s+", " ", text).strip(" ,;")
    if not text or _PLACE_COUNT.match(text):
        return "", 0
    entries = [e for e in (_tidy_entry(p) for p in _PLACE_LIST.split(text)) if e]
    if not entries:
        return "", 0
    # A six-city list is 160 characters in a slot that ellipsises at ~34, so
    # what a student actually read was "Chicago, Illinois, United States;
    # Greenwich, Connect…". Name the first and COUNT the rest. The count rides
    # OUT of the string on purpose: it is copy we wrote, and `smart_location`
    # is a filter for text a board wrote — piping it through produced "+5
    # More".
    return entries[0], len(entries) - 1


# What a tidied line says it did, so nothing is dropped silently.
_PLACE_TIDIED = "The posting listed: "
_PLACE_COUNT_WHY = ("The board gave a count of locations instead of naming "
                    "them ({raw}). This is the market it was filed under.")


def _place(opp) -> dict:
    """One role's place, and how well we know it. `text` may be "" — a caller
    that prints something for an empty `text` is re-introducing the defect.
    `more` is how many further places the same posting listed (0 usually)."""
    raw = (opp.location or "").strip()
    text, more = tidy_place(raw)
    if text:
        return {"text": text, "more": more, "exact": True,
                "why": "" if (text == raw and not more) else _PLACE_TIDIED + raw}
    market = _PLACE_FALLBACK.get(opp.region or "", "")
    if market:
        # A location that tidied away to nothing was never a place, so this
        # row is in exactly the position a blank-location row is in: we hold
        # the market and not the city. Saying so — coarse, and marked coarse
        # — beats printing "2 Locations" in the slot where a city goes.
        return {"text": market, "more": 0, "exact": False,
                "why": (_PLACE_COUNT_WHY.format(raw=raw) if raw else _PLACE_WHY)}
    return {"text": "", "more": 0, "exact": False, "why": ""}


def _card(opp, *, now, today):
    """Bundle one opportunity into a template-ready card. `tags` carries only
    the stated class year now — firm category dropped off it below."""
    bucket = opp.bucket or OTHER
    tags = []
    # No firm-category tag here any more. `_card.html` filtered it out on
    # every render (the firm page's own header already names the firm and
    # its category, per that template's opening comment), so this was
    # computing FIRM_CATEGORIES/TRACK_LABELS lookups for a tag that could
    # never reach the screen. See the template's git history for the filter
    # this removes the other half of.
    class_tag = _class_tag(opp)
    if class_tag:
        tags.append(class_tag)
    place = _place(opp)
    # No sponsorship pill here any more. `_fact_chips` below now carries the
    # sponsorship answer AND its source on every surface, so appending the
    # pill as well printed the same fact twice on this page and only this
    # page: two Barclays rows rendered the pill "No Sponsorship" beside the
    # chip "No sponsorship", in two different visual languages, for one
    # posting field. One carrier, both surfaces — see the note at the top of
    # this module where the pill used to be defined.
    return {
        "id": opp.id,
        "firm_name": opp.firm.name,
        "firm_slug": opp.firm.slug,
        # Minus a trailing city the place line below already prints — see
        # `_title_without_place_echo`. Never minus anything else.
        "title": _title_without_place_echo(opp.title, place["text"]),
        "location": opp.location,
        # What the row PRINTS — see `_place`. `location` above stays raw for
        # callers that need the stated string itself.
        "place": place,
        "url": opp.url,
        "region": opp.region,
        "role": {"value": bucket, "label": BUCKET_LABELS.get(bucket, bucket)},
        # Programme year (see classify.py); the template must not print it as
        # a class year. `class_year` beside it is the stated graduation year.
        "cohort": opp.cohort,
        "class_year": opp.class_year,
        "deadline": deadline_marker(opp.deadline, opp.deadline_precision, today=today),
        "reported": deadline_provenance(opp),
        # Whether the drawer has anything to show for this role. Same gate the
        # feed cards use: never offer to open what we do not hold.
        "has_text": bool((opp.raw or {}).get("detail_text")),
        "tags": tags,
        # The same chips the feed cards carry. This page renders its own card
        # markup, which is how it spent a release showing strictly less about
        # a role than the feed did about the same row.
        "facts": _fact_chips(opp),
        # {} on a clean confirmation; a label+why when our last check of this
        # URL could not reconfirm it — see `_unconfirmed_note`.
        "unconfirmed": _unconfirmed_note(opp),
    }


# The title's own trailing city, when the place line one row below already
# prints it. 297 of 2,599 open campus cards end their title in the very city
# their `.rolecard-sub` names ("2027 APAC Banking Summer Analyst - Hong Kong"
# above "Hong Kong, Hong Kong") — the same fact twice on one card, in two
# rows, and the expensive one: a scraped title is clamped to two lines and
# 268 of those 297 were hitting the clamp. Dropping the echo takes that to
# 207, so 61 cards stop losing their END to an ellipsis in order to spend
# their width restating their own location.
#
# THE GUARANTEE IS AGAINST THE PRINTED PLACE, not `Opportunity.location`.
# `_family_key` below tests the raw column because grouping siblings is a
# different question, but a DISPLAY cut has to be checked against what the
# display shows: "Building 400-Whippany Campus, Jefferson Park" contains
# "Whippany" and tidies to "Jefferson Park", so keying off the raw column
# would have deleted the only mention of the city on the card. Every 4+
# letter word of the tail must survive into `place.text` or the title keeps
# it.
def _title_without_place_echo(title: str, place_text: str) -> str:
    m = _TITLE_TAIL.search(title or "")
    if not m or not place_text:
        return title
    tail = m.group(1).strip()
    words = [w for w in re.split(r"[ ,]+", tail.lower()) if len(w) >= 4]
    base = (title[:m.start()] or "").strip()
    place = place_text.lower()
    if base and words and all(w in place for w in words):
        return base
    return title


def _monogram(name: str) -> str:
    """Two-letter firm initials for the logo tile — the always-works fallback
    the firm columns already use, lifted out so the recommendation cards can
    share it instead of re-deriving it."""
    parts = [p for p in (name or "").split() if p[:1].isalnum()]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def _pick_card(rec) -> dict:
    """One `recommend.Recommendation` as a template-ready dict, same posture as
    `_card` above: the template renders, it doesn't compute. The reasons are
    passed through verbatim — shortening them here would break the promise that
    what the card says is exactly what the scorer decided."""
    c = rec.candidate
    bucket = c.bucket or OTHER
    return {
        "id": c.id,
        "firm_name": c.firm_name,
        "firm_slug": c.firm_slug,
        "monogram": _monogram(c.firm_name),
        "title": c.title,
        "url": c.url,
        "location": c.location,
        "deadline": c.deadline,
        # The role's own kind ("Internship", "Insight Programme", ...). Two
        # roles at one firm can differ on this where they cannot differ on any
        # of the firm-level scoring axes, so it is one of the few fields that
        # earns its place on the row itself.
        "bucket": bucket,
        "bucket_label": BUCKET_LABELS.get(bucket, bucket),
        "score": rec.score,
        "reasons": list(rec.reasons),
    }


def _reason_key(r):
    """A reason's identity for de-duplication: the exact (kind, text, detail)
    triple.

    Matching on `text` alone would be wrong and dishonest. "Tier 1" carries a
    different sentence for each firm ("<firm> is a Tier 1 firm on your target
    list"), so collapsing two firms' Tier 1 chips into one would print one
    firm's justification over another's role. Only a byte-identical
    justification may be stated once."""
    return (r.kind, r.text, r.detail)


#: HOW MUCH OF A PICK'S REASONING GOES ON ITS OWN ROW, in characters.
#:
#: WHAT IT ENCODES. `.rr-why` is one line that never wraps and ellipsises at
#: the row's edge (see its rule in `_styles.html`), so anything past the edge
#: is cut by the browser MID-WORD. Measured on the founder's board: the Picked
#: rows read "Tier 2 · matches IB · US · For 2028-2029 grads (yours) · You
#: know so…" — a fragment of a word is not a shorter fact, it is a rendering
#: fault, and it was the fourth line of a four-line row.
#:
#: WHY 50 (and not the ~53 the row fits: the "+N" is appended AFTER this
#: budget, so the last 3 characters are reserved for it, 2 of them now spent
#: on the separator that keeps it from reading as part of the last reason).
#: `.rr-main` measures ~316px in a 366px column and `.rr-why` is
#: `--fs-xs` (12px), which is ~5.9px per character in Instrument Sans, so ~53
#: characters reach the edge. 50 leaves room for the "+N" that replaces what
#: did not fit. The cut is made on WHOLE reasons here rather than on
#: characters in CSS, so the line always ends on a complete claim.
#:
#: WHAT WOULD CHANGE IT. A wider column, a different `--fs-xs`, or a decision
#: to let this line wrap. All three are measurable the same way this was.
_PICK_WHY_CHARS = 50

#: What separates one reason from the next on `.rr-why`, and now also what
#: separates the last reason from the "+N" that counts the rest. This line is
#: plain text inside one span, so the separator is a real character here
#: rather than `.rr-meta`'s drawn hairline.
_PICK_WHY_SEP = " · "


def _pick_why_line(reasons, verdict=None) -> dict:
    """One pick's reasoning as `{pick_why, pick_why_title}`.

    THE TITLE IS ALWAYS EVERY REASON. This is the only place a role's full
    justification renders on the feed, so nothing below is allowed to shorten
    it — the visible line is a budget, never a filter on what the product is
    willing to say.

    TWO CUTS, and they are different in kind:

      * The `class` reason is dropped from the VISIBLE line when the row
        already carries a year verdict. It is not a fourth fact then, it is
        the third rendering of one: the meta line above says "Your year" and
        prints the posting's own window ("Grad 2028-2029" or "Class of
        2028"), and this line was adding "For 2028-2029 grads (yours)". Its
        sentence stays in the title. A row with NO year verdict keeps it —
        there it is the only place the year is answered.
      * Whatever will not fit `_PICK_WHY_CHARS` is counted rather than
        printed. Counted, never silently dropped: the line ends "+2" and the
        title holds the two.

    THE COUNT IS AN ITEM ON THE LINE, NOT A SUFFIX ON THE LAST REASON
    (2026-09-02). It used to join with a bare space while every reason joined
    with " · ", so the founder's own Picked rows read "Tier 1 · HK · 2027
    intake, a year early for you +2" — four of his six — and the "+2" read as
    the tail of the sentence it was stuck to rather than as a count of what
    follows it. One separator, the line's own, is the whole fix: in a middot
    list "A · B · C · +2" already means two more of the same kind.

    IT IS STILL APPENDED AFTER THE BUDGET, AND THAT IS THE DESIGN. A first
    pass here charged the suffix to `_PICK_WHY_CHARS` instead, which reads as
    the more careful choice and is not: measured on the founder's board it
    evicted "2027 intake, a year early for you" to make room, turning a
    47-character line carrying three real reasons into "Tier 1 · HK · +3
    more" — 21 characters, 29 of the budget unspent, and one fewer thing the
    student is actually told. The constant's own note says it is set to 50
    rather than the ~53 that reach the row's edge precisely so the count has
    room outside it. Two of those three characters now go to the separator.
    """
    kind = (verdict or {}).get("kind") or ""
    title = " ".join(r.detail for r in reasons)
    visible = [r for r in reasons
               if not (kind.startswith("year_") and r.kind == "class")]
    shown, left = [], _PICK_WHY_CHARS
    for r in visible:
        cost = len(r.text) + (len(_PICK_WHY_SEP) if shown else 0)
        if shown and cost > left:
            break
        shown.append(r.text)
        left -= cost
    line = _PICK_WHY_SEP.join(shown)
    hidden = len(visible) - len(shown)
    if hidden:
        line = f"{line}{_PICK_WHY_SEP}+{hidden}" if line else f"+{hidden}"
    return {"pick_why": line, "pick_why_title": title}


def _cycle_not_open_note(profile, open_qs) -> str:
    """One honest sentence when NONE of the student's target cycles has any
    live postings, else "". Checked against the whole open campus board, not
    the picks: "your cycle is not open yet" must mean the BOARD lacks it,
    never that six other roles merely outscored it.

    A student can name more than one cycle now (see `Profile.target_cycles`),
    so this only fires when EVERY parseable one is closed — the moment any
    one of them has live postings, there's nothing to warn about.

    "LIVE POSTINGS" MEANS POSTINGS THIS STUDENT COULD BE LOOKING FOR. The
    first cut asked the board `bucket=internship, cohort=2028` and took any
    row as proof the cycle was open. Measured 2026-09-01 on the founder's
    board (HK/US, IB/S&T, target "2028 Summer Internship"): exactly four
    rows answered — two Evercore "Intro to Evercore" campus info sessions
    with no region at all, and two PwC Canadian CPA co-ops in Québec and
    Edmonton — and those four silenced the note, so he saw four Hong Kong
    2027 internships with nothing in the header saying his own cycle had
    not opened. So the check is scoped two ways: to the regions the
    student named (a blank region is not "in your regions", the same rule
    `role_matches_regions` argues out), and to titles that do not name a
    function outside the track vocabulary (`role_function(title) !=
    "none"` — a CPA co-op is not the summer-internship cycle a finance
    student named). A silent title still counts, on purpose: "2028 Summer
    Analyst" states no track and IS the cycle. A student who named no
    regions gets the board-wide check, as before."""
    parsed = [
        (raw.strip(), parse_target_cycle(raw))
        for raw in getattr(profile, "target_cycles", None) or []
    ]
    parsed = [(label, cycle) for label, cycle in parsed if cycle is not None]
    if not parsed:
        return ""
    regions = [r.lower() for r in (getattr(profile, "regions", None) or ())]

    def _open(bucket, year) -> bool:
        qs = open_qs.filter(bucket=bucket, cohort=str(year))
        if regions:
            qs = qs.filter(region__in=regions)
        # Python, not SQL, and only over the handful of rows a cohort
        # match returns: `role_function` is a regex classifier (see
        # `_role_function` below for the cache and the cost argument).
        return any(
            _role_function(title or "") != "none"
            for title in qs.values_list("title", flat=True)
        )

    closed_labels = [
        label for label, (bucket, year) in parsed if not _open(bucket, year)
    ]
    if len(closed_labels) < len(parsed):
        return ""
    if len(closed_labels) == 1:
        names = closed_labels[0]
    elif len(closed_labels) == 2:
        names = f"{closed_labels[0]} and {closed_labels[1]}"
    else:
        names = ", ".join(closed_labels[:-1]) + f", and {closed_labels[-1]}"
    # No em dash: house copy style. "In your regions" only when the check was
    # scoped to them — the board may hold the cycle somewhere the student is
    # not looking, and the sentence must not claim more than the query asked.
    #
    # SHORTENED 2026-09-02, without dropping either claim. It used to read
    # "{names} postings haven't opened{where} yet. These are today's closest
    # fits." — 106 characters, which wrapped to two italic lines inside a
    # 320px column header and, stacked under the open estimate below it, was
    # half of the five-line preamble a student read before reaching a role.
    # Both facts survive: the cycle is not open (scoped to where we looked),
    # and what is in the column instead. "Postings", "today's" and "These
    # are" were the words carrying neither.
    where = " in your regions" if regions else ""
    return f"{names} not open{where} yet. Closest fits below."


#: HOW MANY FIRMS HAVE TO AGREE BEFORE A MARKET'S OPEN WINDOW IS PRINTED.
#:
#: WHAT IT ENCODES. `cycle_open_estimate` below names a MONTH RANGE, and a
#: range is only a range if several firms drew it. One firm's date is that
#: firm's date, and printing it as "when your cycle opens in Hong Kong" would
#: turn one observation into a market-wide claim — the exact move P1 forbids.
#:
#: WHY THREE. It is the number `research-us-ib-calendar.md §10` already
#: settles on for the same question in the other direction: a firm with fewer
#: than three observations shows nothing rather than a guess (Grade A
#: negative). Borrowed rather than re-chosen, so the timeline page and this
#: sentence hold one idea of "enough evidence to say something" instead of
#: two. Three is also the smallest number at which a range can be wrong in
#: only one direction — two dates always look like a clean interval even when
#: both are outliers.
#:
#: WHAT WOULD CHANGE IT. A market where the firms genuinely cluster (every
#: bulge bracket in Hong Kong opening the same September) could justify
#: dropping to two, and a market where they scatter could justify raising it.
#: Both are measurable from `FirmDate` the moment there are enough rows to
#: measure: today Hong Kong has 7 qualifying firms and the US has 16, so the
#: threshold binds nothing on the founder's board and exists for the markets
#: the corpus has barely started on.
CYCLE_OPEN_MIN_FIRMS = 3


def _cycle_open_parts(profile, *, today=None):
    """The forecast as `(claim, firm_count)`, or None when it cannot be made.

    SPLIT OUT 2026-09-02 so the two surfaces that print it can spend different
    amounts of room on it without either one re-deriving a date. The claim is
    "Estimated to open United States Nov 2026 to Jan 2027"; the firm count is
    what the provenance sentence is built from. `cycle_open_estimate` joins
    them into the one sentence the weekly digest sends; `cycle_open_note`
    keeps them apart, because the Picked column's header spent three of its
    five preamble lines on the provenance half of that sentence.

    The body below is `cycle_open_estimate`'s, unchanged: same query, same
    thresholds, same claim. Only the last two lines differ, where it used to
    join the pieces and now hands them back.

    It names when the student's OWN cycle is expected to open, per market.

    THE POINT OF THE ITEM. `_cycle_not_open_note` above already says the
    cycle has not opened; measured on the founder's board it fires and then
    stops, which leaves a student reading "2028 Summer Internship postings
    haven't opened in your regions yet" with no answer to the only question
    that sentence raises. `FirmDate` has held the answer the whole time: 28
    `app_open` rows for `sa2028`, 7 in Hong Kong and 16 in the US matching
    his tracks. This is those rows, read.

    EVERY DATE HERE IS A FORECAST AND SAYS SO. `research-us-ib-calendar.md`
    §7 (Grade A/B) is explicit that every SA 2028 date is one. So the
    sentence carries the word "estimated" whenever every row behind it is
    `precision="estimated"`, and where any row is firmer the wording drops to
    "expected" rather than claiming a confirmed date — no row here is ever
    rendered as a date the firm published.

    FOUR SCOPES, EACH FOR ITS OWN REASON:

      * the student's CYCLE, via `cycle_slug_for_target`, so a 2028 student
        is never shown the 2027 calendar;
      * their REGIONS, because the HK/US split is real and Grade A on the
        HK leg (`research-hongkong.md §1`) — September against February;
      * their TRACKS, because `FirmDate.track` was split out of `cycle`
        precisely so a student's stated desks could be matched, and a `pe`
        date is not this student's cycle. A row with a blank track is
        cycle-wide and counts for everyone;
      * FUTURE dates only. A date already past cannot be an answer to "when
        does it open", and this is also what makes the sentence robust to
        the six HK `app_close` rows still labelled `sa2028` that WS-CRM-02
        will relabel `sa2027`: the one `app_open` row with the same defect
        (Nomura HK, 2026-09-01, a year early) is dropped by this test rather
        than dragging the Hong Kong range back twelve months. When those
        rows are relabelled nothing here changes.

    Returns None — and every caller prints nothing — when the student named no
    cycle, no regions, or when no market clears `CYCLE_OPEN_MIN_FIRMS`. P3:
    a thin profile and a thin corpus both get today's behaviour, which is
    silence.
    """
    from .models import FirmDate

    today = today or timezone.localdate()
    cycles = [
        slug for slug in (
            cycle_slug_for_target(*parsed)
            for parsed in (
                parse_target_cycle(raw)
                for raw in getattr(profile, "target_cycles", None) or []
            )
            if parsed is not None
        ) if slug
    ]
    regions = [r.lower() for r in (getattr(profile, "regions", None) or ())]
    if not cycles or not regions:
        return None
    tracks = [t.lower() for t in (getattr(profile, "tracks", None) or ())]

    rows = FirmDate.objects.filter(
        event_kind="app_open", cycle__in=cycles, region__in=regions,
        date__gt=today,
    )
    if tracks:
        # A cycle-wide row (blank track) speaks for every desk; a
        # desk-scoped row speaks only for its own. Same rule the timeline
        # page applies, and the reason the column was split out of `cycle`.
        rows = rows.filter(Q(track="") | Q(track__in=tracks))

    by_region: dict[str, list] = {}
    for fd in rows.select_related("firm"):
        by_region.setdefault(fd.region.lower(), []).append(fd)

    parts, all_estimated = [], True
    for region in regions:
        found = by_region.get(region) or []
        # Count FIRMS, not rows: Goldman files a cycle-wide US row and an
        # `ib` one, and two rows from one firm are one firm's opinion.
        if len({fd.firm_id for fd in found}) < CYCLE_OPEN_MIN_FIRMS:
            continue
        dates = sorted(fd.date for fd in found if fd.date)
        if not dates:
            continue
        all_estimated = all_estimated and all(
            (fd.precision or "") == "estimated" for fd in found
        )
        lo, hi = dates[0], dates[-1]
        span = (f"{lo:%b %Y}" if (lo.year, lo.month) == (hi.year, hi.month)
                else f"{lo:%b %Y} to {hi:%b %Y}")
        parts.append(f"{REGION_LABELS.get(region, region.upper())} {span}")
    if not parts:
        return None

    # No em dash (P7). "Estimated" is a word about the DATES, so it leads the
    # clause they sit in rather than trailing as a disclaimer nobody reads.
    lead = "Estimated to open" if all_estimated else "Expected to open"
    where = parts[0] if len(parts) == 1 else " and ".join(parts)
    return f"{lead} {where}", len({fd.firm_id for fd in rows})


#: The provenance half of the forecast, in one place so the digest's sentence
#: and the column's tooltip cannot drift into two different disclaimers.
def _cycle_open_provenance(firms: int) -> str:
    return f"from past cycles at {firms} firms. Not a firm's own published date."


def cycle_open_estimate(profile, *, today=None) -> str:
    """The whole forecast as ONE sentence, or "" when the corpus cannot say.

    This is the string `crm.digest._cycle_note` appends to the weekly email,
    where there is room for the disclaimer inline and no hover to put it on.
    Byte-for-byte what it has always been; see `_cycle_open_parts` for the
    query, the thresholds and the evidence rules behind every word of it.
    """
    built = _cycle_open_parts(profile, today=today)
    if built is None:
        return ""
    claim, firms = built
    return f"{claim}, {_cycle_open_provenance(firms)}"


def cycle_open_note(profile, *, today=None) -> dict:
    """The same forecast, split for the Picked column: `text` on the line,
    `why` on the hover. `{}` when there is nothing to say.

    WHY THE SPLIT. The column's header printed the full sentence and it ran to
    three italic lines inside a 320px box — "Estimated to open United States
    Nov 2026 to Jan 2027, from past cycles at 16 firms. Not a firm's own
    published date." Two of those lines are the answer to "where did this come
    from", which is the definition of a tooltip on this board: a fact a
    student ACTS on is a line, a fact about where the fact came from is a
    hover. Nothing is dropped and nothing is softened — "Estimated" still
    leads the visible half, so the line can never read as a published date
    even to someone who never hovers.
    """
    built = _cycle_open_parts(profile, today=today)
    if built is None:
        return {}
    claim, firms = built
    provenance = _cycle_open_provenance(firms)
    return {"text": claim, "why": provenance[0].upper() + provenance[1:]}


def _group_picks(cards):
    """Regroup the flat, ranked pick list into firm blocks and lift every
    reason that is common to a whole block out of the individual rows.

    WHY. Three of the four scoring axes (tier, track, region) read FIRM
    properties, so two roles at the same firm are structurally incapable of
    differing on them. Printed once per card, that produced a row of cards
    whose "why" strings were byte-identical — every card saying the same
    thing tells the student nothing, and buries the one thing that does
    differ (the role, its deadline, where it is) under chips that don't.

    This is a PRESENTATION regroup and nothing else. It does not rescore and
    does not reorder: blocks appear in the order their best-ranked role
    appeared, and roles keep their order inside a block.

    A reason rises to the highest level at which it is universal:
      * shared     — identical on every pick, whatever the firm
      * firm level — identical on every role at that one firm
      * role level — whatever is left, i.e. the genuinely distinguishing bits

    WHAT THE PAGE USES. `shared` renders as the one line in the pinned Picked
    column's header. Everything below it — the firm-level reasons of a block
    PLUS the role-level reasons of each row — renders as that row's own
    quiet "why" line on its card (`pick_why`, built where the column is
    assembled in `opportunities`). So every reason the scorer produced
    prints exactly once on the page again: once in the header if every pick
    carries it, otherwise once on the card it belongs to. This docstring
    spent a year admitting the opposite: the tiers were computed and
    nothing rendered them, and measured 2026-09-01 the founder's six picks
    shared NO reason at all (three firms, three different tier sentences),
    so the header was empty and six cards stood with zero visible
    justification — including four whose only class chip said, in a
    tooltip nobody hovers, "a year off ... not a fit".

    Returns `(shared, blocks)`.
    """
    if not cards:
        return [], []

    blocks: dict[str, dict] = {}
    order: list[dict] = []
    for c in cards:
        b = blocks.get(c["firm_slug"])
        if b is None:
            b = blocks[c["firm_slug"]] = {
                "firm_name": c["firm_name"],
                "firm_slug": c["firm_slug"],
                "monogram": c["monogram"],
                "roles": [],
            }
            order.append(b)
        b["roles"].append(c)

    def universal(rows):
        """The reasons carried, identically, by every row in `rows`. Walks
        row 0 to emit them in the scorer's fixed axis order (see _AXES)."""
        common = set(_reason_key(r) for r in rows[0]["reasons"])
        for row in rows[1:]:
            common &= set(_reason_key(r) for r in row["reasons"])
        return [r for r in rows[0]["reasons"] if _reason_key(r) in common]

    shared = universal(cards)
    shared_keys = {_reason_key(r) for r in shared}
    for b in order:
        b["reasons"] = [
            r for r in universal(b["roles"]) if _reason_key(r) not in shared_keys
        ]
        printed = shared_keys | {_reason_key(r) for r in b["reasons"]}
        for role in b["roles"]:
            role["reasons"] = [
                r for r in role["reasons"] if _reason_key(r) not in printed
            ]
    return shared, order


# Track suffixes the seeds append to a cycle slug. TRACKS ONLY: the region
# codes (hk/us/eu/sg) used to sit in this same dict, which is what made the
# firm page say the market twice. `cycle_label("sa2028_hk")` expanded the
# suffix to "Hong Kong" and seated it in the slot that otherwise holds a desk,
# and _timeline.html then appended the row's own `region` after it — so seven
# rows read "SA 2028 · HONG KONG · HK" under the uppercase .tl-scope, directly
# beneath rows that read "SA 2028 · HK". It also meant one slot held two
# vocabularies: a track on 20 rows, a market on 7.
#
# Splitting the suffix by KIND (rather than labelling `region` through
# REGION_LABELS, the intuitive fix) is what actually removes the duplication —
# labelling would have produced "SA 2028 · HONG KONG · HONG KONG".
# Now `directory.timeline.TRACK_SHORT`, so the desk abbreviations this page
# prints and the ones the vocabulary module defines cannot drift apart. The
# dict here covered four of the six preference-eligible tracks; the two it
# missed (consulting, corp-strat) fell through to `.title()`, which spells
# corp-strat "Corp Strat" by luck and would have spelled a seventh slug
# whatever its hyphens happened to say.
_CYCLE_TRACKS = TRACK_SHORT


def _cycle_suffix(cycle: str) -> str:
    """The `_`-suffix of a cycle slug, lowercased. "" for a human cycle."""
    raw = (cycle or "").strip()
    if not raw or " " in raw:           # already human ("SA 2028")
        return ""
    return raw.partition("_")[2].lower()


def cycle_region(cycle: str) -> str:
    """The market a cycle slug names in its own suffix, if it names one.

    `sa2028_hk` -> `hk`; `sa2028_ib` -> "" (that is a desk, not a market).
    REGION_LABELS is the one vocabulary of what a region code is, so this
    cannot drift from the Region filter's idea of the same thing.
    """
    suffix = _cycle_suffix(cycle)
    return suffix if suffix in REGION_LABELS else ""


def cycle_label(cycle: str, track: str = "") -> str:
    """`("sa2028", "ib")` -> `SA 2028 · IB`. Cycle and TRACK; never the market.

    Since migration 0014 the desk lives in `FirmDate.track` and `cycle` holds
    only a season+year slug, so the normal call is `cycle_label(fd.cycle,
    fd.track)`. The suffix-parsing below is kept anyway, and deliberately: it
    is the only thing that renders a legacy spelling readably if one ever
    reaches this function again — a row written by a pre-0014 process, a
    fixture, or a `manage.py shell` insert. Formatting a stale value costs
    nothing; printing `SA2028_IB` in the product's own body copy, which is what
    this function was written to stop, costs a student's trust in the page.

    The column holds two spellings of one vocabulary — importers wrote
    `sa2028_ib`, the seeds wrote `SA 2028` — and the firm page printed
    whichever it found, so a student reading Jefferies saw the raw slug
    `SA2028_IB` sitting in the product's own body copy. Formatting on read
    rather than migrating: the stored value is what the importer matched on,
    and rewriting it would break re-imports for a display bug.

    A region suffix is dropped here and handed to `cycle_region` instead, so
    the market is named exactly once per row. See `_CYCLE_TRACKS`.
    """
    raw = (cycle or "").strip()
    if not raw:
        return ""
    if " " in raw:                      # already human ("SA 2028")
        return raw
    head, _, tail = raw.partition("_")
    season = head[:2].upper()
    year = head[2:]
    if season in ("SA", "FT") and year.isdigit():
        label = f"{season} {year}"
    else:
        label = head.replace("-", " ").title()
    if tail.lower() in REGION_LABELS:   # a market, not a desk — see cycle_region
        return label
    # The row's own `track` column first; a suffix only where there is no
    # column to read. A column that DISAGREES with a legacy suffix is the same
    # class of thing `_firm_date_row` resolves for region — stated beats
    # inherited, and the stated one is the one a writer can still correct.
    desk = (track or tail or "").strip().lower()
    desk = _CYCLE_TRACKS.get(desk, desk.replace("-", " ").title() if desk else "")
    return f"{label} · {desk}" if desk else label


# How a firm_dates row says where its date came from. `FirmDate.source_url` is
# a URLField, but URLField only validates under `full_clean()` and neither
# importer calls it (import_firm_dates.py and seed_directory.py both write
# `str(entry.get("source", ""))` verbatim; scripts/demo_seed.py writes the
# literal "seed:demo"). So the column holds two different KINDS of value —
# real citations and provenance tokens — and 26 of the 39 rows are tokens.
#
# The token rows are the reason this map exists. "seed:historical-pattern" is
# not a citation; it is the note "we estimated this from previous cycles". The
# page used to render it as `<a href="seed:historical-pattern">source</a>`,
# byte-identical in style to the two real goldmansachs.com links above it, so
# a student got four identical SOURCE pills of which two navigated nowhere.
# Apollo's "seed:demo" row was worse: a hard past date wearing CONFIRMED and
# offering a demo placeholder as its proof.
#
# Resolved on READ, not migrated: the stored string is what the importer
# matched on, the same reason `cycle_label` formats rather than rewrites.
_SOURCE_NOTES = {
    "seed:historical-pattern": (
        "from past cycles",
        "Estimated from this firm's previous cycles — the firm has not "
        "published this date yet.",
    ),
    "seed:demo": (
        "sample data",
        "Placeholder from the demo seed, not a date this firm published.",
    ),
    # The two 2026-09-01 research passes. These are NOT citations of the firm
    # — a firm that has not opened its SA 2028 cycle has published nothing to
    # cite — but they are not "unverified" either, which is where every
    # unmapped token lands and which would have libelled the best-evidenced
    # estimates in the table. What they cite is a dated research file with
    # graded sources: the Hong Kong one rests on seven Grade-A SA 2027
    # postings, the US one on 21 firms measured across two cycles.
    "research:hongkong": (
        "from the prior HK cycle",
        "Estimated from this market's own SA 2027 dates, read off the firms' "
        "postings (Grade A). The firm has not published this date yet.",
    ),
    "research:us-ib-calendar": (
        "from measured US lead times",
        "Estimated from how far ahead these firms posted in the two previous "
        "cycles (Grade C+, 21 firms). The firm has not published this date yet.",
    ),
}


def _source_marker(raw: str) -> dict:
    """Where a cycle date came from, as something the page can render honestly.

    A link ONLY when the value is a real http(s) URL a click can open.
    Anything else is a provenance NOTE — same information, said in words,
    rendered as plain text rather than as a link that goes nowhere.
    """
    val = (raw or "").strip()
    if not val:
        return {}
    if val.lower().startswith(("http://", "https://")):
        return {"url": val, "label": "source",
                "why": "Opens the page this date was read from."}
    label, why = _SOURCE_NOTES.get(
        val,
        ("unverified",
         "No published page behind this date — it has not been verified "
         "against the firm's own site."),
    )
    return {"label": label, "why": why}


# A `FirmDate` counts as CONFIRMED only when both halves agree: a high
# enough `confidence` (the firm holds this date) AND a `precision` that
# names a real day or a real month (the row locates it — "estimated" is a
# month-level GUESS about a date nobody stated, and stays out no matter how
# high `confidence` reads; see `crm.utils`'s identical split, which exists
# because six CRM readers once re-spelled this bar as `confidence == 1.0`
# alone and put a countdown on an estimate). One pair of constants, every
# reader in this module included, so a third copy of the bar never drifts
# from this one.
_CONFIRMED_FIRM_DATE_CONFIDENCE = 0.8
_CONFIRMED_FIRM_DATE_PRECISIONS = ("day", "month", "")


def _firm_date_row(fd, *, today):
    """One firm_dates row as a timeline entry. confirmed vs rumored is read
    off confidence + precision: a high-confidence, non-estimated date is
    "confirmed"; anything softer is shown as "rumored" with its confidence
    visible, never laundered into certainty."""
    prec = (fd.precision or "").lower()
    d = fd.date
    if d is None:
        date_text = "date to be confirmed"
    elif prec == "month":
        date_text = f"{d:%b %Y}"
    elif prec == "estimated":
        date_text = f"~ {d:%b %Y}"
    else:
        date_text = f"{d:%b} {d.day}, {d.year}"

    confirmed = ((fd.confidence or 0.0) >= _CONFIRMED_FIRM_DATE_CONFIDENCE
                 and prec in _CONFIRMED_FIRM_DATE_PRECISIONS)
    return {
        # The row's own id, so `_timeline` can hang `directory.estimates`'
        # annotations off it without this function needing to know they exist.
        "id": fd.id,
        "cycle": cycle_label(fd.cycle, fd.track),
        # The stored slug rides along beside its label so `_timeline` can match
        # it against the cycles the student STATED without re-parsing prose.
        "cycle_slug": fd.cycle,
        "track": fd.track,
        # The row's own column when it has one, else the market its cycle slug
        # names. Stated beats inferred; a slug whose suffix DISAGREES with the
        # column is a data error rather than a second fact worth printing (no
        # such rows live — the suffix and the column agree on all 7).
        "region": fd.region or cycle_region(fd.cycle),
        "event_kind": fd.event_kind,
        "event_label": EVENT_LABELS.get(fd.event_kind, fd.event_kind.replace("_", " ").capitalize()),
        # The raw date rides along beside its rendered text so the timeline can
        # compare rows to each other — see `_drop_contradicted_openings`.
        "date": d,
        "date_text": date_text,
        # "23:59 HKT, 08:59 your time", and "" on every row whose firm never
        # stated an hour. Deliberately a SEPARATE key from `date_text`
        # rather than appended to it: `date_text` already carries three
        # shapes keyed off precision, one of which is "~ Oct 2027", and a
        # time may only ever ride a day-level row. Keeping them apart means
        # the constraint that stops an hour landing on an estimate
        # (`firm_dates_close_time_needs_a_day`) is the only rule the
        # renderer has to trust. `get_current_timezone_name` is the zone
        # `TimezoneMiddleware` activated for this request.
        "time_text": fd.close_time_label(timezone.get_current_timezone_name()),
        # ALREADY GONE. This page had no date cutoff of any kind: 10 of the
        # 41 live rows sit in the past, and the founder's own firm pages
        # rendered Morgan Stanley's 6 Aug insight deadline and BlackRock's
        # 31 Aug close in exactly the type the still-open rows wear. A
        # timeline is history as well as forecast, so the row STAYS (P4:
        # mark, never drop) — it is the only place a student can see what
        # this firm's last cycle actually did — but it says it is history
        # rather than reading as something still to act on. A dateless row is
        # not past; "date to be confirmed" is a future event nobody has
        # placed yet.
        "is_past": d is not None and d < today,
        "precision": prec,
        "confidence": confidence_marker(fd.confidence),
        "state": "confirmed" if confirmed else "rumored",
        "source": _source_marker(fd.source_url),
    }


# An estimated OPENING is only worth printing while nothing on the same page
# contradicts it. Four firms (hsbc, jpm, ms, ubs) carried both a dated
# `app_close` in Aug-Oct 2026 and a `seed:historical-pattern` `app_open` of
# "~ Sep 2027" — and because `cycle_label` drops the region suffix, the stored
# `SA 2028` and `sa2028_hk` rows print the SAME scope, "SA 2028 · hk". Sorted
# by date the close lands first, so /firms/hsbc/ said the HK cycle closes on
# Oct 30 2026 and opens ten months LATER, while listing a role under that
# cycle closing in 76 days.
#
# The two rows came from two incompatible conventions: the seeds date a
# HK intake that opens the September before its summer, the human `SA 2028`
# rows hang off postings that were live in Aug 2026. Nothing on the row could
# tell a reader which convention it followed, so the page asserted both.
#
# THAT DIAGNOSIS WAS HALF RIGHT AND THE FIX WAS AIMED AT THE WRONG HALF. The
# 2026-08-02 radar run stamped `sa2028` on six Hong Kong closes that belong to
# SA 2027 (see `import_firm_dates.infer_cycle`), so on those four pages there
# was no contradiction to resolve: the close and the opening are dates in
# DIFFERENT cycles, and suppressing the opening hid the only SA 2028 Hong Kong
# date on file. The suppression still exists, because a genuine same-cycle
# same-market contradiction is still worth silencing, but it is keyed off the
# stored cycle now rather than the printed one — see `_contradiction_scope`.
#
# Suppressing the ESTIMATE rather than the dated close is the only direction
# that loses nothing: an estimate of when a cycle will open is a guess, and a
# deadline already on file for that same cycle and market is evidence the
# guess is wrong. A CONFIRMED opening after a close is left alone on purpose —
# that is a genuine data conflict, and hiding it would hide the bug.
def _cycle_scope(row) -> tuple[str, str]:
    """What the timeline PRINTS as a row's scope — cycle label plus market.

    Keyed off the rendered strings, not the stored `cycle`, because the
    contradiction a reader sees is between two rows that read identically.
    """
    return (row["cycle"], row["region"])


# WHY THIS ONE KEYS OFF THE STORED SLUG AND `_flag_conflicting_closes` DOES
# NOT. The printed scope was the right key while two spellings of one cycle
# could print alike; it is the wrong key for deciding that one row makes
# another IMPOSSIBLE. `cycle_label` renders `sa2027` and `sa2028` differently,
# but it also renders every row with no cycle on file as the same empty
# string — and the six Hong Kong closes the 2026-08-02 radar run stamped
# `sa2028` were SA 2027 all along (see `import_firm_dates.infer_cycle`). So
# this function suppressed the CORRECT "~ Sep 2027" SA 2028 opening on
# hsbc, jpm, ms and ubs on the strength of a deadline belonging to the cycle
# before it. Relabelling those six rows fixes the data; keying off the stored
# `cycle` and the row's own `region` is what stops the same shape recurring,
# because a close can now only contradict an opening filed under the SAME
# cycle slug and the SAME market. A blank cycle contradicts nothing and is
# contradicted by nothing: "not stated" is not a scope two rows can share.
def _contradiction_scope(row) -> tuple[str, str] | None:
    """The (cycle slug, market) a row can be contradicted WITHIN, or None.

    None for a row that states no cycle or no market — an unscoped date is
    not evidence about a scoped one, and treating it as such is how a US row
    with no market on it came to speak for Hong Kong.
    """
    cycle = str(row.get("cycle_slug") or "").strip()
    region = str(row.get("region") or "").strip()
    if not cycle or not region:
        return None
    return (cycle, region)


def _drop_contradicted_openings(rows: list[dict]) -> list[dict]:
    """Drop a rumored `app_open` that a dated `app_close` in the same cycle
    AND the same market already places in the past."""
    closes: dict[tuple[str, str], object] = {}
    for row in rows:
        if row["event_kind"] == "app_close" and row["date"] is not None:
            scope = _contradiction_scope(row)
            if scope is None:
                continue
            if scope not in closes or row["date"] < closes[scope]:
                closes[scope] = row["date"]

    keep = []
    for row in rows:
        scope = _contradiction_scope(row)
        close = closes.get(scope) if scope is not None else None
        contradicted = (
            row["event_kind"] == "app_open"
            and row["state"] == "rumored"
            and row["date"] is not None
            and close is not None
            and row["date"] > close
        )
        if not contradicted:
            keep.append(row)
    return keep


# Two `app_close` rows can both survive to here for the same printed scope —
# the DB's uniqueness constraint is on (firm, cycle, region, event_kind), and
# the JPM shape above is exactly a case where two different `cycle` spellings
# ("SA 2028" and "sa2028_hk") name the same scope once `_cycle_scope` renders
# them. The founder's own jpm page showed an Aug 30 close and a Sep 3 close,
# both badged "confirmed", with nothing telling a student which to believe.
#
# Three ways out were considered and rejected:
#   - Newer date wins: a re-scrape can be a bad read of the firm's page: an
#     older row is not automatically stale, it may be the one that is right.
#   - Higher-confidence row wins: the two rows can tie (same band, same
#     precision), which this repo's confidence vocabulary is coarse enough to
#     make common — and a tie has no honest winner.
#   - Keep both silently confirmed: this is the live bug. A student reads
#     "confirmed" on the wrong one and misses the real deadline.
#
# So neither row gets to keep `state == "confirmed"` alone. Both stay on the
# page — dropping one is how a real deadline goes missing — but each is
# re-labelled "conflicting" with the reason attached, the same honest-
# ambiguity move as `_unconfirmed_note`: say what we don't know rather than
# assert a guess.
def _flag_conflicting_closes(rows: list[dict]) -> list[dict]:
    """Mark every `app_close` row whose printed scope carries more than one
    distinct close date, so the timeline can't badge either one "confirmed"
    alone."""
    dates_by_scope: dict[tuple[str, str], set] = {}
    for row in rows:
        if row["event_kind"] == "app_close" and row["date"] is not None:
            dates_by_scope.setdefault(_cycle_scope(row), set()).add(row["date"])

    conflicted = {scope for scope, dates in dates_by_scope.items() if len(dates) > 1}
    if not conflicted:
        return rows

    for row in rows:
        if row["event_kind"] == "app_close" and _cycle_scope(row) in conflicted:
            row["state"] = "conflicting"
            row["conflict"] = {
                "label": "conflicting dates on file",
                "why": ("Coverage has more than one closing date on file for "
                        "this cycle and cannot tell which is current. Check "
                        "the firm's own posting before relying on either."),
            }
    return rows


# ---------------------------------------------------------------------------
# The one place a student's stated preference reaches the timeline.
#
# THE GAP THIS CLOSES. The founder's profile states a target cycle ("2028
# Summer Internship"), the ranker parses it, and `recommend.W_CYCLE` is worth
# 15 points — and it fires on 2 of the 2,617 open campus rows, because SA 2028
# postings do not exist yet. That is not a missing preference field. It is a
# preference the student stated, that the system stored and parsed correctly,
# aimed at the one dataset that cannot answer it: the listings feed is 1,108
# rows of SA 2027 and 1,333 rows that state no intake year at all.
#
# The dataset that CAN answer it is this one. 38 of the 41 firm_dates rows are
# sa2028, all 34 future-dated rows sit at a firm the founder has tiered, and
# every one of them is a date about the cycle he said he is recruiting for.
# Until 0014 closed the vocabulary there was no way to ask: the same cycle was
# spelled four ways, so no query could group it.
#
# WHAT IS AND IS NOT ASSERTED. This marks a row only when the student's OWN
# stated `target_cycles` parses to the row's cycle slug — a fact they entered,
# echoed back in their own words. Nothing here is inferred, and nothing
# inferred belongs here: the tiered-firm list, who they email and what they
# save are all readable signals about preference, but a page that told a
# student "your cycle" on the strength of an inference would be asserting
# something they never said. Ordering is untouched for the same reason a
# timeline is chronological — the marker changes what a row SAYS, not where it
# sits, so a nearer deadline never falls below a further one.
# ---------------------------------------------------------------------------
def _stated_cycle_slugs(user) -> set[str]:
    """The `FirmDate.cycle` slugs a signed-in student's own profile names.

    Empty for anonymous users, for a profile with no target cycle, and for a
    stated cycle that has no slug in this corpus (an Insight week — see
    `timeline.cycle_slug_for_target`). Empty means "mark nothing", never
    "mark everything".
    """
    if not getattr(user, "is_authenticated", False):
        return set()
    slugs = set()
    for raw in (getattr(user, "target_cycles", None) or []):
        parsed = parse_target_cycle(str(raw or ""))
        if parsed is None:
            continue
        slug = cycle_slug_for_target(*parsed)
        if slug:
            slugs.add(slug)
    return slugs


def _timeline(firm, *, today, user=None):
    rows = firm.firm_dates.all().order_by(F("date").asc(nulls_last=True), "cycle", "event_kind")
    rows = _drop_contradicted_openings([_firm_date_row(fd, today=today) for fd in rows])
    rows = _flag_conflicting_closes(rows)

    # WHAT THE BOARD ITSELF SAYS ABOUT THESE DATES (`directory.estimates`).
    #
    # Three annotations, none of which can change a row: `found_on` on every
    # estimate (25 rows on file all carry 2026-07-03 and the page printed no
    # date at all, so nothing said the guess was two months old or that it was
    # the same guess on all 25); `superseded` where an observed opening wave
    # has overtaken an estimate; `contradicted` where a stated date sits on
    # the wrong side of everything the scraper watched. The two facts render
    # side by side with their provenances — an observation never overwrites an
    # assertion (P1).
    notes = estimates.annotate(firm)
    for row in rows:
        row.update(notes.get(row["id"], {}))

    wanted = _stated_cycle_slugs(user)
    for row in rows:
        if row["cycle_slug"] and row["cycle_slug"] in wanted:
            row["for_you"] = {
                "label": "your cycle",
                "why": f"You told us you're recruiting for {row['cycle']} — "
                       f"this date belongs to that cycle.",
            }
    return rows


# ---------------------------------------------------------------------------
# Observed activity — `FirmCycleObservation` surfaced on the firm page. See
# that model's docstring for what it is and isn't: a MEASURED distribution
# of when this firm's own postings opened and closed, never a curated date
# and never a prediction. Nothing here may say more than the model actually
# counted.
# ---------------------------------------------------------------------------

# The sample floor below every measured-cycle claim in the product, now
# defined once in `directory.open_runs` and re-exported here under the name
# `_cycle_observed` has always read it by. It moved because a second surface
# (the Today rail's open-run line) needed the same gate, and two constants
# agreeing by coincidence is exactly how they stop agreeing. The reasoning
# for the number itself travels with the definition.
CYCLE_OBSERVATION_MIN_SAMPLE = _CYCLE_OBSERVATION_MIN_SAMPLE


# ---------------------------------------------------------------------------
# Role-level pagination (WS-OPP-17).
#
# WHAT THE MEASUREMENT ACTUALLY SAID, and where the plan's diagnosis was
# wrong. `audit-perf-tests.md §1` measured `/opportunities/?role=all` at 7.5 MB
# and 1.76 s and the item proposed "page the columns for role=all the way the
# campus scope already does". Re-measured 2026-09-02 before touching anything:
# BOTH scopes were already column-paged at `COLS_PAGE` = 12, and the page was
# still 5.27 MB and 2.5 s warm. The weight was never the column count. It was
# the roles INSIDE the columns: 4,077 top-level cards across those 12 at
# `role=all`, and the two heaviest columns alone held 1,382 and 1,173. Campus
# is the same defect one size down, 1,114 cards over 12 columns with 656 of
# them in one. So the fix is the one the item is named after and not the one
# its body describes, and it applies to both scopes rather than only to
# `role=all`.
#
# WHY 24. It is twice `COLS_PAGE`, which is the shape the page wants: one
# column should be able to say more about a firm than the board says about
# firms. It is roughly two screens of scrolling inside a single column at
# 1280px, which is past the point where a student is reading and into the
# point where they should be filtering. And everything past it is one click
# away on the firm page, which is the surface whose entire job is listing one
# firm's whole board.
#
# MARKED, NEVER DROPPED (P4). A capped column says how many roles it is not
# showing and links to the page that shows them. The column header's "N open"
# is untouched and still counts every role, and so do the stat strip and the
# facet counts, which are computed over the full list well before this runs —
# a student is never told a smaller number, only shown fewer cards.
ROLES_PER_COLUMN = 24


def _cap_roles_per_column(clusters) -> None:
    """Trim each column to `ROLES_PER_COLUMN` top-level cards, in place.

    City-variant siblings are NOT counted or trimmed separately: they render
    inside their family head's disclosure (`_group_city_variants`), so a head
    that survives brings its whole family with it and a head that does not
    takes its family with it. Counting them here would make the cap depend on
    how many cities a programme happens to run in, which is not a fact about
    how long the column is.

    Mutates the dicts, which are shared with `cluster_list` — deliberate, and
    safe only because every count this page states is already computed: `total`
    off `open_count`, the stat strip and the facets over the unsliced rows.
    """
    for cl in clusters:
        heads = [r for r in cl["roles"] if not r.get("in_group")]
        if len(heads) <= ROLES_PER_COLUMN:
            cl["roles_hidden"] = 0
            continue
        kept = heads[:ROLES_PER_COLUMN]
        shown = len(kept) + sum(len(r.get("variants") or []) for r in kept)
        cl["roles"] = kept
        cl["roles_hidden"] = max(cl["open_count"] - shown, 0)


def _window_text(first, last) -> str:
    """`Aug 9 to Aug 29`, or `Aug 9` when the window is a single day. Words,
    not a dash: the rest of this page's copy avoids em dashes in
    user-facing text, and a plain "to" reads unambiguously either way."""
    if first is None or last is None:
        return ""
    if first == last:
        return f"{first:%b} {first.day}"
    return f"{first:%b} {first.day} to {last:%b} {last.day}"


def _cycle_observed(firm) -> list[dict]:
    """Per-region measured posting activity for the firm page, one dict per
    `FirmCycleObservation` row that clears `CYCLE_OBSERVATION_MIN_SAMPLE` on
    at least one side (opens or closes independently — the model gates them
    with different trust filters, see its docstring, so a firm can clear one
    side and not the other).

    A row that clears neither — every onboarding-only "honest zero" row
    (`opened_count == closed_count == 0`) included — contributes nothing:
    not an empty entry, not a "no data yet" sentence, nothing. That is the
    silence the honesty rules call for, and it is enforced here rather than
    in the template so the template never has the option to render a
    below-threshold number by accident.
    """
    out = []
    for row in firm.cycle_observations.all().order_by("region"):
        opened_text = ""
        if row.opened_count >= CYCLE_OBSERVATION_MIN_SAMPLE:
            opened_text = (
                f"Opened {row.opened_count} posting"
                f"{'s' if row.opened_count != 1 else ''}, "
                f"{_window_text(row.open_window_first, row.open_window_last)}."
            )
        closed_text = ""
        if row.closed_count >= CYCLE_OBSERVATION_MIN_SAMPLE:
            closed_text = (
                f"Closed {row.closed_count}, "
                f"{_window_text(row.close_window_first, row.close_window_last)}."
            )
            # Not a footnote (see FirmCycleObservation's docstring): shown
            # only alongside a close claim that already cleared the sample
            # gate, so it can only ever ADD context to a real number, never
            # stand in for one that got suppressed.
            if row.excluded_suspect_closes:
                n = row.excluded_suspect_closes
                closed_text += (
                    f" ({n} more close{'s' if n != 1 else ''} excluded as unreliable.)"
                )
        if not opened_text and not closed_text:
            continue  # neither side clears the sample gate — say nothing
        out.append({
            # "" (unstated), "other" and "global" all render through the same
            # map classify.REGION_LABELS already uses for the Region filter,
            # never as a raw code — see the model's region field comment.
            # "" has no entry in REGION_LABELS by design (classify.py), which
            # is exactly the case where the qualifier should drop out rather
            # than print anything.
            "region_label": REGION_LABELS.get(row.region, ""),
            "opened_text": opened_text,
            "closed_text": closed_text,
        })
    return out


# ---------------------------------------------------------------------------
# Region filter (?region=). Region is one of the target markets a role's own
# LOCATION resolves to (classify.py), not a claim in its title.
#
# `none` is a first-class option for the same reason `YEAR_NONE` is: 297 of the
# 886 open campus rows resolve to no region at all — a third of the inventory.
# Before this existed, picking "Hong Kong" silently deleted those 297 with no
# trace, which is the "Region implies completeness" over-claim the redesign
# spec's §D1 names. It is labelled "Other / Unstated", NOT "No Region", because
# a blank conflates two genuinely different things — a location string the
# parser could not resolve, and a real market the product does not track
# (Sydney, Mumbai) — and the data cannot tell them apart, so the label must
# not pretend it can.
# ---------------------------------------------------------------------------
REGION_NONE = "none"
# "Unstated", no longer "Other / Unstated": stated-but-untracked locations
# have their own real region now (classify.REGION_LABELS["other"]), so this
# option is left meaning exactly what it says — the posting never said.
REGION_NONE_LABEL = "Unstated"


def _apply_region_filter(qs, region):
    """Narrow to one market, or to the rows that resolve to none.

    Anything unrecognised (a hand-typed or stale querystring) is a no-op rather
    than an empty page — the same posture `_apply_role_filter` and
    `_apply_year_filter` already take. NOTE: this IS a behaviour change. The
    old inline `qs.filter(region__iexact=region)` gave `?region=uk` an empty
    feed with no explanation; the three filters now agree with each other."""
    if region == REGION_NONE:
        return qs.filter(region="")
    if region.lower() in REGION_ORDER:
        return qs.filter(region__iexact=region)
    return qs


def _region_facet(qs, selected=""):
    """Region options with live per-option counts, drawn from the (otherwise
    filtered) set — so each number answers "under my current filters, how
    many?" rather than "in the whole table".

    One GROUP BY, not a Python walk over every row: the facet this replaced
    iterated the whole role-scoped queryset with its firm join attached, which
    measured 11.8ms at campus scope and 53.9ms at `role=all`. This computes
    strictly more (options AND counts) in 1.5-2.0ms.

    `selected` is load-bearing, not cosmetic. Options are cross-filtered
    against every OTHER active filter, so a live selection can legitimately
    fall to zero (region=hk + track=consulting with no consulting roles in
    Hong Kong). Dropping a zero-count option would drop the one the user
    picked, leaving the <select> with nothing selected — and the next htmx GET
    would then serialize whatever option happens to be first, silently moving
    the user's filter. Same failure mode as the role group's missing checked
    radio; same fix."""
    counts = dict(
        qs.values_list("region").annotate(n=Count("id")).values_list("region", "n")
    )
    options = [{"value": "", "label": "Any Region", "count": sum(counts.values())}]
    options += [
        {"value": r, "label": REGION_LABELS[r], "count": counts.get(r, 0)}
        for r in REGION_ORDER
        if counts.get(r) or r == selected
    ]
    # "Other / Unstated" only when there is something unstated to offer (or it
    # is the live selection). An option promising zero rows is noise, and the
    # count is a promise: it must be a number worth reading.
    if counts.get("") or selected == REGION_NONE:
        options.append({
            "value": REGION_NONE,
            "label": REGION_NONE_LABEL,
            "count": counts.get("", 0),
        })
    return options


# `role_function` is not cheap enough to run per row on every render: 229ms
# for the 16,029 open rows at `?role=all` (measured 2026-09-01), against a
# facet budget the docstring below quotes in single-digit milliseconds. The
# input is `Opportunity.title`, a bounded vocabulary from a bounded table
# (13,464 distinct titles across those 16,029 rows) and never anything a
# request supplies, so memoising on it is bounded by the board, not by
# traffic. Warm, the same sweep costs 0.6ms. The cache is process-local and
# the classifier is pure, so a stale entry is impossible without an edit to
# `recommend.py` — which restarts the process anyway.
_role_function = lru_cache(maxsize=32768)(role_function)


def _row_tracks(firm_tracks, title):
    """The tracks ONE row answers to — THE single definition of role-level
    track membership, called by both `_track_facet` (counts) and
    `_apply_track_filter` (rows) so the two cannot drift. A facet number that
    disagreed with the list under it would break the count promise the whole
    filter bar is built on.

    Three cases, and they are `recommend._track_fit`'s three cases on purpose
    (that function's docstring argues them out at length):

      title names a track      -> that track, and only it. "2027 Internal
                                  Audit Analyst" is not an IB role because
                                  its bank covers IB, and "Investment Banking
                                  Summer Analyst" IS one wherever it is posted.
      title names a function   -> no track at all. Audit, Controllers, Branch,
        outside the vocabulary    IT, HR: real work, none of it these tracks.
      title names nothing      -> the FIRM's coverage, exactly as before.
        ("2027 Summer Analyst")

    THE THIRD CASE IS THE LOAD-BEARING ONE. 1,336 of the 2,723 open campus
    rows (49%) state no function in their title at all, and requiring a
    positive title match would delete every one of them from every track
    facet. The rule is "drop rows that state a DIFFERENT function", never
    "keep only rows that state THIS one" — silent rows degrade to today's
    behaviour, which is the honest answer when the posting did not say."""
    fn = _role_function(title or "")
    if fn == "none":
        return ()
    if fn:
        return (fn,)
    return tuple(firm_tracks or ())


def _firm_tracks_map():
    """Every firm's `tracks` list, one flat read (~100 rows)."""
    return dict(Firm.objects.values_list("id", "tracks"))


def _apply_track_filter(qs, track):
    """Rows whose OWN function answers to `track`, falling back to the firm's
    coverage only where the title is silent — see `_row_tracks`.

    WHAT THIS REPLACED, AND WHY. The filter was `firm__tracks__contains=
    [track]`, i.e. a claim about the EMPLOYER standing in for a claim about
    the JOB. Measured on the live board 2026-09-01, `?track=ib` returned
    1,125 open campus roles of which 189 (17%) named investment banking in
    their title; 215 named an explicitly non-track function (Risk,
    Controllers, Branch, IT, HR) and 198 named a DIFFERENT track. The other
    four facets were no better: st 17%, consulting 15%, pe 8%, am 5%. A
    student filtering to "Investment Banking" was shown, overwhelmingly,
    roles that are not investment banking — while `recommend._track_fit`, on
    the same rows, had already been reading the role's own function for
    months. The two surfaces disagreed about what "IB" means; this is that
    disagreement resolved in the recommender's favour.

    Effect, same measurement, open campus scope: ib 1,125 -> 724, st 1,049 ->
    574, consulting 1,002 -> 692, am 168 -> 228, pe 64 -> 59. 675 rows naming
    an explicitly non-track function leave every facet; `am` and `consulting`
    GAIN the rows that name their function at a firm not tagged for it, which
    is the same "the role speaks for itself" rule pointing the other way.

    PYTHON, NOT SQL, AND LAST IN THE CHAIN. `role_function` is a regex
    cascade with span arithmetic (`recommend._names_non_track`); there is no
    honest way to say it in a WHERE clause, and reimplementing it in one is
    how the two definitions of "IB" started disagreeing in the first place.
    So this scans, which is why `_apply_filters` runs it AFTER every other
    filter — the scan is over the smallest queryset the request allows (2,723
    rows at the default campus scope, 724 ids out). Measured warm at that
    scope: 2.3ms to build the id list, 4.7ms for filter-and-count end to
    end."""
    if not track:
        return qs
    tracks_by_firm = _firm_tracks_map()
    ids = [
        pk
        for pk, firm_id, title in (
            qs.order_by().values_list("id", "firm_id", "title")
        )
        if track in _row_tracks(tracks_by_firm.get(firm_id), title)
    ]
    return qs.filter(id__in=ids)


def _track_facet(qs, selected=""):
    """Track options with live per-option counts. Track is the vertical
    (ib/consulting/...) — a different dimension from the role's own classified
    BUCKET (insight / internship / entry-level), which is what the separate
    Role Type facet shows, and the two must not re-merge into one select.
    That separation is untouched here: this facet reads the role's FUNCTION,
    never its bucket, and the Role Type control is not involved.

    Track used to be read purely off `Firm.tracks`, a claim about the
    employer. It is now the role's own function where the title states one
    and the firm's coverage where it does not — see `_apply_track_filter` for
    the measurement that forced the change (2026-09-01) and `_row_tracks` for
    the rule. These counts go through that same helper, so each one is still
    exactly what picking that option returns.

    OVERLAP, deliberately, same posture as `_year_facet`: a firm carrying both
    `ib` and `st` counts its SILENT roles under BOTH, so these counts can sum
    to more than the total. Deduping would mean picking one of a firm's real
    verticals to lie about. Each individual number still keeps the count
    promise — pick that track and you get exactly that many.

    COST. This is a GROUP BY over (firm_id, title) plus a Python walk, where
    it used to be a GROUP BY over firm_id alone; the walk is unavoidable
    because `role_function` is Python (see `_apply_track_filter`). Measured
    2026-09-01, with `_role_function`'s cache warm: 3.5ms at the campus
    scope and 13.7ms at `?role=all`, against the 2.0ms / 15.2ms the firm_id
    GROUP BY cost. The first render of a fresh process pays the cache miss
    instead — 207ms to classify all 13,464 distinct open titles, once.

    See `_region_facet` for why `selected` is always kept in the options."""
    tracks_by_firm = _firm_tracks_map()
    counts: Counter[str] = Counter()
    total = 0
    for firm_id, title, n in (
        qs.order_by()
        .values_list("firm_id", "title")
        .annotate(n=Count("id"))
        .values_list("firm_id", "title", "n")
    ):
        total += n
        for t in _row_tracks(tracks_by_firm.get(firm_id), title):
            counts[t] += n
    # RETIRED SLUGS ARE NOT OPTIONS, even when rows carry them and even when
    # one is the current selection. `corp-strat` was retired from the picker
    # (D-3) because it returns almost nothing — 5 open campus rows named it
    # by title on 2026-09-02 against 602 for ib — and a facet that still
    # offered it would be the same dead promise on a second surface. The rows
    # are not dropped: they stay in "Any Track" and in every other facet, and
    # a firm's own card still prints "Corp Strat" from TRACK_LABELS. This is
    # the one place `selected` is not force-kept (see `_region_facet`), for
    # the same reason it is not offered: a value nobody can choose is not a
    # value the bar has to hold on to.
    return [
        {"value": "", "label": "Any Track", "count": total},
        *[
            {"value": t, "label": TRACK_LABELS.get(t, t), "count": counts[t]}
            for t in sorted(
                (set(counts) | ({selected} if selected else set()))
                & set(SELECTABLE_TRACKS),
                key=lambda t: TRACK_LABELS.get(t, t),
            )
        ],
    ]


# The role-select vocabulary, in display order. "" (the default) means "the
# three campus buckets"; "all" and "other" are explicit opt-ins so experienced
# postings never leak into the default view but are never silently deleted
# either.
ROLE_CHOICES = [
    ("", "All Campus Roles"),
    *[(b, BUCKET_LABELS[b]) for b in TARGET_BUCKETS],
    (OTHER, "Other / Experienced"),
    ("all", "Everything We Scraped"),
]

# The role values that reach `_apply_role_filter` as themselves. Anything else
# in `?role=` falls through to the campus scope (see `_apply_role_filter`), and
# the segmented control has to render THAT — see `_effective_role` below.
ROLE_VALUES = frozenset(v for v, _ in ROLE_CHOICES)

# The opt-in modes. Neither is a campus bucket and neither appears as a normal
# segment; they are reachable only by deep link or by the subset sentence's
# "Show everything" link, and when one is active the bar grows a fifth,
# muted segment so it states its own mode honestly.
ROLE_OPTIN = (OTHER, "all")

# Short display labels for the segmented control. DISPLAY ONLY — the values
# still come from ROLE_CHOICES, which stays the single source of truth for the
# `?role=` vocabulary. The pills need shorter text than the old <select>
# options did: at 375px the four campus segments render as a 2x2 grid whose
# cells are ~160px, where "Insight Programme (46)" wraps to two lines and
# "Insight (46)" does not.
SEGMENT_LABELS = {
    "": "All Campus",
    INSIGHT: "Insight",
    INTERNSHIP: "Internship",
    ENTRY_LEVEL: "Entry-Level",
    OTHER: "Other / Experienced",
    "all": "Everything",
}

# The four segments that are always drawn, in display order.
SEGMENT_VALUES = ("", *TARGET_BUCKETS)

#: The columns `directory.dupes.fold_duplicates` reads, so the segmented
#: control can fold the board before it counts it (see `_folded_count` in
#: `opportunities`) without materialising ~16,000 model instances to do it.
#:
#: All nine, including the two only `_survivor_rank` touches. A count does not
#: depend on WHICH copy of a cluster survives, so `first_seen` and `id` could
#: be left out and let `getattr`'s defaults stand in — and then the day
#: someone drops those defaults, this path breaks on a board nobody is
#: looking at. Two integers a row is the cheaper mistake.
_FACET_FOLD_FIELDS = ("id", "firm_id", "bucket", "title", "location",
                      "deadline", "cohort", "sponsorship", "first_seen")

#: One board row reduced to those columns. A `namedtuple` rather than a dict
#: because `fold_duplicates` reads its rows with `getattr` — it is written
#: against the model and must go on being — and rather than the model itself
#: because instantiating 16,655 `Opportunity` objects to ask how many of them
#: are repeats costs more than the fold does.
_FacetRow = namedtuple("_FacetRow", _FACET_FOLD_FIELDS)

# Target-first ordering for firm pages: insight/internship/entry_level ahead
# of experienced rows, campus buckets in TARGET_BUCKETS order.
_BUCKET_ORDER = Case(
    *[When(bucket=b, then=Value(i)) for i, b in enumerate(TARGET_BUCKETS)],
    default=Value(len(TARGET_BUCKETS)),
    output_field=IntegerField(),
)


def _apply_role_filter(qs, role):
    """Cut a queryset down to the selected role bucket. The default ("") is
    the product's promise — only the three campus buckets; unclassified ""
    rows count as `other` so nothing pre-classifier ever poses as campus."""
    if role == "all":
        return qs
    if role == OTHER:
        return qs.filter(Q(bucket=OTHER) | Q(bucket=""))
    if role in TARGET_BUCKETS:
        return qs.filter(bucket=role)
    return qs.filter(bucket__in=TARGET_BUCKETS)


def _effective_role(role):
    """The role the page will ACTUALLY render, which is not always the role in
    the querystring: `_apply_role_filter` sends every unrecognised value to the
    campus scope.

    The segmented control must reflect the effective role, not the raw one.
    A `?role=banana` that rendered campus roles under a radio group with no
    checked member would leave the form with no `role` to serialize at all —
    the same mode-reset failure the fifth segment exists to prevent, arriving
    by a different door."""
    return role if role in ROLE_VALUES else ""


def _apply_filters(qs, sel, *, skip=()):
    """Apply every active filter in `sel` except the ones named in `skip`.

    This exists so the cross-filter posture is stated once instead of being
    re-derived at four call sites. THE RULE (unchanged, now enforceable): a
    facet's counts are computed against every OTHER active filter and never
    against its own, so each number honestly answers "under my current
    filters, how many?". `skip=("region",)` is literally how the Region facet
    asks that question.

    `sel` is the same dict that ships to the template as `selected`, so the
    controls and the counts can only ever read one description of the request.
    """
    if "region" not in skip:
        qs = _apply_region_filter(qs, sel["region"])
    if "provider" not in skip and sel["provider"]:
        qs = qs.filter(source__iexact=sel["provider"])
    if "sponsorship" not in skip and sel.get("sponsorship"):
        qs = _apply_sponsorship_filter(qs, sel["sponsorship"])
    if "firm" not in skip and sel["firm"]:
        qs = qs.filter(firm__slug__in=sel["firm"])
    if "q" not in skip and sel["q"]:
        qs = qs.filter(
            Q(title__icontains=sel["q"])
            | Q(firm__name__icontains=sel["q"])
            | Q(location__icontains=sel["q"])
        )
    if "year" not in skip:
        qs = _apply_year_filter(qs, sel["year"])
    if "role" not in skip:
        qs = _apply_role_filter(qs, sel["role"])
    # Track goes LAST, and that ordering is load-bearing rather than
    # cosmetic. Every other clause is a WHERE the database evaluates, so
    # their order is the planner's business; `_apply_track_filter` is a
    # Python scan of the rows that reach it, so it wants to reach as few as
    # possible. Filters are conjunctive and none of them join a multi-valued
    # relation, so moving it here cannot change the result — only its cost.
    if "track" not in skip:
        qs = _apply_track_filter(qs, sel["track"])
    return qs


# ---------------------------------------------------------------------------
# Sponsorship filter (?sponsorship=). Three real answers, and the third one is
# the honest majority: `unknown` means the posting did not say, which on this
# data is most of them. It is offered as a choice rather than hidden, because
# "show me the ones nobody has answered" is a real question when the answered
# set is small — and because a filter that silently dropped 4,000 unanswered
# rows into a fourth invisible state would be the region bug again.
SPONSORSHIP_CHOICES = (
    ("", "Any Sponsorship"),
    ("yes", "Sponsors visas"),
    ("no", "No sponsorship"),
    ("unknown", "Not stated"),
)
# Blank and "unknown" are the same fact stored two ways (the column defaults
# to "unknown"; older rows carry ""), so the filter and the facet must both
# treat them as one bucket or the counts will not sum to the total.
_SPONSOR_SILENT = ("", "unknown")


def _apply_sponsorship_filter(qs, value: str):
    """"yes"/"no"/"unknown" against the EFFECTIVE answer — the posting's own
    field, or (only when the posting is silent) the firm's per-region policy
    from `directory.sponsorship`. A role showing a "No Sponsorship · firm
    policy" pill used to still pass a "Sponsors visas" filter, because this
    function read `sponsorship` alone while the pill already fell back to
    `Firm.sponsors`; see `directory.sponsorship.effective_sponsorship` for
    the one precedence rule both now share."""
    if value not in ("yes", "no", "unknown"):
        return qs
    policy = firm_policy_map()
    silent = Q(sponsorship__in=_SPONSOR_SILENT)
    if value == "unknown":
        firm_answered = firm_policy_q("yes", policy) | firm_policy_q("no", policy)
        return qs.filter(silent).exclude(firm_answered)
    return qs.filter(Q(sponsorship=value) | (silent & firm_policy_q(value, policy)))


def _sponsorship_facet(qs, current: str) -> list[dict]:
    """Options with live counts, same contract as the other counted facets.

    Counted the same way `_apply_sponsorship_filter` filters: a silent
    posting whose firm has a per-region policy counts under "yes"/"no", not
    "unknown". One query (`values_list`) plus the same small firm-policy map
    the filter builds — cheap regardless of how many rows are in `qs`."""
    policy = firm_policy_map()
    per = {"": 0, "yes": 0, "no": 0, "unknown": 0}
    for sponsorship, firm_id, region in qs.values_list("sponsorship", "firm_id", "region"):
        per[""] += 1
        stated = (sponsorship or "unknown").lower()
        if stated in ("yes", "no"):
            per[stated] += 1
            continue
        per[policy.get((firm_id, region), "unknown")] += 1
    return [{"value": v, "label": label, "count": per.get(v, 0),
             "selected": v == current}
            for v, label in SPONSORSHIP_CHOICES]


# ---------------------------------------------------------------------------
# Year filter (?year=). The value being filtered is the PROGRAMME year — the
# intake a posting runs in, "2027 Summer Internship — Account Analyst". It is
# NOT a graduation year, and the UI label says so; see classify.py for why the
# two are separate columns.
#
# A row matches year Y when EITHER its `cohort` (programme year) or its
# `class_year` (the graduation year the posting stated outright) is Y. The
# second half matters for a handful of rows only, but for those rows the
# stated class year is the truthful answer to "which year is this for?", and a
# student who picks 2027 should see "Class of 2027 Investment Analyst" whether
# or not its title also happens to carry a programme year.
#
# `none` is a first-class option, not an afterthought: ~89% of the live open
# set states no year at all. Silently excluding those the moment someone picks
# a year would hide most of the feed behind a control that looks like it only
# narrows a little. So "no year stated" is selectable, counted, and the
# unfiltered default keeps showing everything.
# ---------------------------------------------------------------------------
YEAR_NONE = "none"
YEAR_NONE_LABEL = "No Year Stated"


def _apply_year_filter(qs, year):
    """Narrow to one programme/intake year, or to the rows that state none.
    Anything unrecognised (a hand-typed querystring) is a no-op rather than an
    empty page — same posture as the role filter's fallthrough."""
    if year == YEAR_NONE:
        # No year anywhere: title-derived columns, both prose facts (the
        # graduation window and the programme start year), and the
        # convention-derived class year. The facet counts every one of those
        # as a year this posting carries, so this option must exclude all of
        # them or its count breaks the per-option promise.
        return qs.filter(cohort="", class_year="", class_year_derived="",
                         raw__facts__grad__years__isnull=True,
                         raw__facts__start__years__isnull=True)
    if year.isdigit():
        return qs.filter(Q(cohort=year) | Q(class_year=year)
                         | Q(class_year_derived=year)
                         | Q(raw__facts__grad__years__contains=[year])
                         | Q(raw__facts__start__years__contains=[year]))
    return qs


def _year_facet(qs, selected=""):
    """Year options drawn from the live (otherwise-filtered) set, each with its
    own count, so the select answers "under my current filters, how many?".

    Counts are per-option and can sum to slightly more than the total: one row
    can carry both a programme year and a different stated class year (a real
    example on the live board is a 2027 summer intake whose title adds "(Class
    of 2028)"), and it honestly belongs under both. Deduping it into one
    bucket would mean picking one of the two years to lie about."""
    counts: Counter[str] = Counter()
    stated = 0
    total = 0
    # The third source is the posting's own prose: the facts extractor's grad
    # window (raw.facts.grad.years, evidence-phrase and all). 69 open campus
    # roles stated their year ONLY there — "be a graduating student of 2028"
    # in the description, nothing in the title — and the facet filed every
    # one under "No Year Stated" while the eligibility lens, reading the same
    # fact, was issuing verdicts on it. Two features disagreeing about
    # whether a posting stated its year is the kind of inconsistency this
    # facet exists to prevent.
    #
    # The fourth is the programme start year (raw.facts.start.years), the
    # body-stated twin of the title cohort: Crédit Agricole's 44 "Date prévue
    # de prise de fonction 01/06/2026" rows and HSBC's "Start Date: Tue Jun
    # 01, 2027" labels state their intake ONLY there, and all of them sat
    # under "No Year Stated".
    #
    # The fifth is the convention-derived class year (see
    # classify.derive_class_year), which is an inference and is labelled as
    # one everywhere it is shown — but it is still a year this posting is
    # about, and a student filtering to their own graduating class wants the
    # summer internships that hire it. It only ever exists on rows that state
    # no year of their own, so it cannot displace a stated one.
    #
    # ONE JSONB READ PER ROW, NOT TWO. This used to select the two windows as
    # two separate paths — `raw__facts__grad__years` and
    # `raw__facts__start__years` — which compiles to two `raw #> '{...}'`
    # expressions over one column, and Postgres detoasts the whole `raw`
    # datum once per expression rather than caching it across the target
    # list. `raw` is TOASTed on this table (927-byte average rows, 72 MB
    # including the toast relation), so the second path was pure duplicated
    # I/O: measured on the founder's live board 2026-09-01, two paths cost
    # 76 ms over 2,723 campus rows and 100 ms over all 16,029 open rows,
    # against 52 ms and 72 ms for the single `raw__facts` read below. The
    # extra bytes on the wire (the whole facts dict rather than two arrays)
    # are cheaper than the second detoast by a wide margin.
    #
    # The remaining ~50 ms IS the detoast, and no query shape removes it —
    # the three plain columns alone cost 2 ms for the same 2,723 rows. Only a
    # materialised column (the audit's own recommendation, at ingest in
    # `enrich_postings`) would, and no such column exists today:
    # `refresh_grad_facts` writes this fact back into `raw["facts"]["grad"]`,
    # not into a field. That is a schema change, and it is not this one.
    for cohort, class_year, derived, facts in qs.values_list(
            "cohort", "class_year", "class_year_derived", "raw__facts"):
        total += 1
        years = {y for y in (cohort or "", class_year or "", derived or "") if y}
        facts = facts if isinstance(facts, dict) else {}
        for key in ("grad", "start"):
            window = facts.get(key)
            if not isinstance(window, dict):
                continue
            for y in window.get("years") or ():
                if isinstance(y, str) and y.isdigit():
                    years.add(y)
        for y in years:
            counts[y] += 1
        if years:
            stated += 1
    # The live selection always renders as an option, even when the other
    # filters have driven it to zero (year=2027 + region=hk with no 2027 roles
    # in Hong Kong). Two reasons, and the second is load-bearing rather than
    # cosmetic: a <select> that dropped the value it is currently set to would
    # display a selection it does not have, AND the out-of-band count refresh
    # restores each select's state from exactly this option's `selected`
    # attribute — with no matching option there is nothing to restore from.
    # See `_filter_counts.html`. Same guarantee as `_region_facet` /
    # `_track_facet` make for their own selections.
    years = set(counts)
    if selected.isdigit():
        years.add(selected)
    return [
        {"value": "", "label": "Any Year", "count": total},
        *[
            {"value": y, "label": y, "count": counts[y]}
            for y in sorted(years, reverse=True)
        ],
        {"value": YEAR_NONE, "label": YEAR_NONE_LABEL, "count": total - stated},
    ]


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

# Horizon (days) over which a real deadline drives the "fuse" bar — beyond
# this a dated role isn't meaningfully more urgent than a rolling one.
_FUSE_HORIZON = 45
# Same horizon, reused deliberately for the rolling card's elapsed-time
# footer bar (see `_urgency_item`'s `elapsed_pct`): a rolling card and a
# dated card that both read "about half a bar" then mean the same order of
# magnitude of time, which is the only reason the two bars can sit in the
# same visual slot without one implicitly out-arguing the other.
_ELAPSED_HORIZON = _FUSE_HORIZON
# How recently a rolling posting must have first been seen to count as "fresh".
_FRESH_DAYS = 10
# How long a posting may go with no positive liveness signal before the card
# says so on ABSOLUTE age, regardless of what our own checks reported. Three
# days is `health.py`'s own `CONSECUTIVE_FAILURES` threshold for calling a
# connector failed rather than blipping, borrowed rather than re-chosen so the
# operator alarm and the student-facing note fire on the same evidence instead
# of holding two different ideas of stale.
_UNCONFIRMED_AFTER_DAYS = 3
# How many rolling cards the feed shows before deferring to browse-by-firm.
_ROLLING_FEED_CAP = 30

# HOW FAR PAST ITS OWN STATED DEADLINE A STILL-LISTED POSTING GOES BEFORE THE
# PRODUCT STOPS OFFERING TO SAVE IT.
#
# WHAT IT ENCODES. Not "this posting is closed" — that inference is
# forbidden, and for a measured reason: Citi labels the datum "Anticipated
# Posting Close Date" and 11 of 17 postings that stated one were still live
# past it, one of them by eight months (`research-ats-lifecycle.md` unsafe
# #1 and #2, Grade A). A stated deadline is a plan, not an event, and the
# board still listing the row is real evidence against calling it dead. So
# `status` stays "open", nothing is closed, and the outbound link stays: the
# firm's own page remains the record. What changes is only the affordance —
# the product declines to tell a student to put it in their pipeline.
#
# WHY 30 DAYS. It is the point where the two explanations for "past its date
# and still up" swap places. Inside a month, "the date was approximate and
# the firm is still reading applications" is ordinary and Citi's 11-of-17
# is exactly that population. Past a month, the better explanation is that
# nobody has taken the posting down — which is a fact about the firm's ATS
# hygiene, not an invitation. Measured 2026-09-02 on the live board: 17 open
# rows sit past their own stated deadline, 6 of them by more than 30 days,
# and just 1 of those 6 is a campus row (Stifel, 262 days past, whose prose
# read verifies against `raw.detail_text`). Accenture's campus row, 2 days
# past, keeps its Save, which is the case the threshold exists to protect.
#
# WHAT WOULD CHANGE IT. A measurement of how often a row more than 30 days
# past its stated date is still genuinely accepting applications. Coverage
# cannot see that today — every Workday path returns 200 and absence from a
# search is not proof of closure (`research-ats-lifecycle.md` unsafe #5) —
# so the number is a judgement about which explanation is likelier, held at
# the coarsest unit that makes the judgement, and it should move the moment
# a firm's own reply rate past its deadline is observable.
_ABANDONED_AFTER_DAYS = 30


def _abandoned_note(o, *, today=None) -> dict:
    """"The firm left this up and nobody took it down", or {}.

    ONE DEFINITION (P5). Three surfaces need this answer — the feed card
    (`_urgency_item`), the htmx swap that re-renders one card's control
    (`_track_control`), and the tests that pin both — and the day two of them
    computed their own day arithmetic is the day a student could un-save a
    row and be handed back the Save button the feed had just withheld.

    Deliberately NOT a status change and NOT a filter. `status` stays "open",
    the row keeps its place in the feed (sorted last, as passed rows already
    are) and keeps its outbound link. See `_ABANDONED_AFTER_DAYS` for why a
    prose deadline may not close a row, and P4: the card stays and says why.

    A row with no deadline, a future deadline, or a closed status gets {}: a
    closed row already has its own honest message (`_role_drawer.html`'s
    closed branch, `_posting_closed_note`) and does not need a second, weaker
    one guessing at the same thing.
    """
    if o.deadline is None or is_posting_closed(o):
        return {}
    overdue = ((today or timezone.localdate()) - o.deadline).days
    if overdue <= _ABANDONED_AFTER_DAYS:
        return {}
    return {
        "label": "Looks abandoned",
        "days": overdue,
        "why": (f"This posting's own stated deadline passed {overdue} days "
                f"ago and the firm still lists it. That usually means nobody "
                f"has taken it down. Check the firm's page before spending "
                f"time on it."),
    }


def _unconfirmed_note(o, *, as_of_date=None) -> dict:
    """Whether Coverage's own most recent check of this posting actually
    reconfirmed it is live, as something a template can render honestly. {}
    when it did (or there is nothing to compare — every open row carries
    both timestamps from ingest, so in practice this is only {} on a clean
    confirmation).

    `as_of_date` is the request's own local today, supplied by the per-row
    caller (`_urgency_item`) so the staleness clock is read once for the page
    rather than once per card; omitted, it reads the clock itself, exactly as
    the three one-shot callers do. See `crm.utils._calendar_days_ago`.

    `last_checked` moves on every check outcome; `last_verified` moves ONLY
    on a positive liveness signal (ingest's own list-presence stamp, or a
    connector's `verify()` returning "verified-open" — see reverify.py and,
    for the sharpest example, oracle.py's `verify()` docstring). When a
    row's latest check did NOT reconfirm it, `last_checked` runs ahead of
    `last_verified` and `status` is deliberately left at "open" — absence
    from a provider's search is not proof of closure (oracle.py documents
    the false JPM-4731 closure this guards against). That caution is
    correct for the DATA, but it left the card and the apply link reading
    identically whether the last check found the posting alive or came back
    unable to say — reverify.py's own docstring describes a "Verified N
    Days Ago" pill that "never lies", but no template ever rendered it. This
    is that pill's honest complement: it speaks only when the evidence is
    genuinely thinner than "open" implies.

    Returns {} for `status == "closed"` rows on purpose: this note's whole
    premise is that `status` stays "open" here because absence from a
    provider's search isn't proof of closure (see the docstring above).
    That premise is false once `status` has actually been flipped to
    "closed" — `closed_at` is set precisely because a check DID confirm
    closure (see `closed_at`'s comment in models.py) — so there is no
    "can't confirm either way" gap left to describe. A closed row needs an
    honest closed message, not this one; see `_role_drawer.html`'s own
    `{% if o.status == 'closed' %}` branch."""
    if o.status == "closed":
        return {}
    if not o.last_verified:
        return {}
    # Our own check ran, and came back unable to reconfirm.
    check_failed = bool(o.last_checked and o.last_checked > o.last_verified)
    # WHY AN ABSOLUTE TEST HAS TO SIT BESIDE THE RELATIVE ONE. The comparison
    # above is between two timestamps written by the same code path, so it can
    # only speak when that path RUNS. A board that fails outright never reaches
    # ingest's stamp at all: both timestamps freeze together, `last_checked <=
    # last_verified` comes back equal, and the row renders as freshly
    # confirmed. The docstring above used to assume this away ("every open row
    # carries both timestamps from ingest, so in practice this is only {} on a
    # clean confirmation") — true only while the connector works, which is
    # exactly the case this note exists for.
    #
    # Measured 2026-09-01: 92 open campus rows were more than 3 days stale, 37
    # of them silent under the relative test alone, and 23 of those were
    # rendering a live countdown. All 23 were HSBC, whose board had been
    # throwing an SSL error since 2026-08-25 — one card read "closes in 2
    # days" off a page last read 6 days earlier. `health.py` was already
    # printing "stale data being presented as fresh" to an operator-only
    # channel for that firm; nothing said it to the student looking at it.
    stale_days = _calendar_days_ago(o.last_verified, as_of_date=as_of_date)
    if not check_failed and stale_days < _UNCONFIRMED_AFTER_DAYS:
        return {}
    return {
        # The number, as a number, so a template can PRINT it rather than
        # find it inside `why`. Until this key existed the age was rendered
        # in exactly one place in the product — the drawer's "Read from the
        # posting Nd ago" — which a student reaches only by opening it. The
        # feed asserts "Closes in 8 days" over the same evidence and said
        # nothing about how old that evidence was; measured 2026-09-01, a
        # quarter of the rows under the "Closing in 10 days" ribbon were
        # reading off pages six days stale. `days` is present whenever the
        # note fires, including the check-failed branch, where it is the
        # answer to "since when" that branch's own sentence leaves out.
        "days": stale_days,
        "label": "Not recently confirmed live",
        "why": (("Our last check of this posting could not confirm it is "
                 "still live. ")
                if check_failed else
                (f"We have not been able to confirm this posting in "
                 f"{stale_days} days. ")) +
               ("It still shows as open because we also can't confirm it "
                "closed. Verify on the firm's own site before relying on "
                "this link."),
    }


# The chips a feed card can carry, in the order a student needs them. Order is
# a judgement about attention, not about how confident the extraction is: if a
# role will not sponsor you, that is the one fact that can end the decision
# before any of the others matter. Pay comes next because no campus board
# shows it; the language wall next because it is the other hard gate; then
# eligibility, then the ones that cost you time rather than rule you out.
#
# TWO, not three, and that is a measurement rather than a preference. At
# 1440px a feed card is 302px wide, leaving 266px inside the fact row, and a
# mono chip measures ~97px: two fit with room (198px), three do not (299px).
# The rest of what a posting says is on the posting, one click away, and a
# chip cut in half is worse than a chip that isn't there.
_FACT_CHIP_ORDER = ("sponsorship", "study", "language", "pay", "grad", "gpa",
                    "duration", "cover_letter", "transcript", "assessment")
# THREE, AS OF 2026-09-02, AND THE MEASUREMENT ABOVE IS WHY IT COULD MOVE.
# The two-chip cap was correct for the line it was measured on: one nowrap
# row, every part `flex: none`, a single `:last-child` allowed to shrink, and
# 266px to fit chips into. A third chip did not fit and a chip cut in half is
# worse than a chip that is not there.
#
# That line no longer exists. `.rr-meta` wraps (see `_rolecard.html`'s header
# note: the truncation policy moved to one named 205px cap on `.rr-loc`), so
# the constraint the 2 was derived from — horizontal room on one line — is
# gone, and a third chip costs a line break rather than a cut chip.
#
# What the 2 was costing, measured on the live board: 128 open rows state a
# third fact behind the cap and every one of them also states sponsorship or
# a year of study, i.e. the hidden chip is competing with a wall. It is not
# raised further than 3: the drawer now carries EVERY stated fact beside the
# sentence that produced it (see `_FACT_LABELS` and `role_description`), so
# the card's job is the decisive few and the drawer's is completeness. A
# fourth chip would be the card trying to be the drawer.
_FACT_CHIPS_MAX = 3

# The two verdict kinds `_language_fit` issues. Neither blocks — see that
# function for why a posting's language line is a warning at most.
_LANGUAGE_KINDS = ("language_warn", "language_ok")


def _language_chip_label(fact, lang_verdict) -> str:
    """The language chip's text: the personal reading when the student has
    stated their languages ("Mandarin needed · not in your profile",
    "Mandarin · you speak it"), the posting's bare fact ("Mandarin needed")
    when they have not — which is exactly the chip this feed showed before
    `User.languages` existed."""
    if lang_verdict:
        return lang_verdict["label"]
    return f"{fact['value']} needed"

# A class-standing fact ("Penultimate year", "Final year", "First year" — see
# facts.py's `_STUDY_STAGE`) and a grad-year fact, BOTH stated by the same
# posting, are two spellings of one requirement often enough to merge — but
# only when they cannot disagree. `_STUDY_STAGE`'s own comment is why the
# module never converts a stage into a year: "penultimate" means something
# different on a three-year UK degree and a four-year US one. What CAN be
# checked without inventing that translation is breadth — "final year" and
# "first year" name ONE imminent year; "penultimate year" straddles two (this
# year's or next year's intake). A grad-year fact wider than that is a
# DIFFERENT, more specific claim than the stage phrase, not a repeat of it, so
# it must stay two chips — merging would hide a real mismatch instead of a
# duplicate. Measured live: 55 open TARGET_BUCKETS rows carry both, and every
# one agrees (Penultimate year + Grad 2028; Penultimate year + Grad
# 2027-2028; Final year + Grad 2027; ...) — none contradict.
_CLASS_STANDING_SPAN = {"First year": 1, "Final year": 1, "Penultimate year": 2}


def _standing_matches_grad(study_value, grad_fact):
    """Whether a class-standing fact and a grad-year fact, stated by the SAME
    posting, describe one requirement rather than two — see
    `_CLASS_STANDING_SPAN` above for why "matches" means "not wider than"
    rather than an exact translation."""
    span = _CLASS_STANDING_SPAN.get(study_value)
    if span is None:
        return False
    years = (grad_fact or {}).get("years") or []
    return 1 <= len(years) <= span


# "Current student" / "Current student or recent graduate" is not a class
# standing — facts.py's own comment on `_STUDY_STAGE` calls it out as a
# distinct stage, not a stand-in for one. On THIS product it is also close to
# uninformative: `extract_facts` (the management command that populates
# `raw["facts"]`) only ever runs over `TARGET_BUCKETS` — insight, internship,
# entry_level, the three buckets `classify.py` defines as campus recruiting —
# so every row that can carry this fact at all is already, by the
# classifier's own criterion, a current-student-or-recent-grad row. Measured
# live: 26 "Current student" + 2 "Current student or recent graduate" rows,
# 100% of them insight/internship/entry_level, and 0 of the 13,962 open
# `other` (experienced-hire) rows carry ANY `facts` at all, this one
# included. Suppressed HERE, at render time, gated on the OPPORTUNITY'S OWN
# bucket rather than deleted from facts.py's extraction: the day extraction
# widens to the `other` bucket, or a firm-page surface starts showing facts
# for experienced roles, this same phrase on that row would be exactly the
# signal a reader needs (an experienced-hire posting that also welcomes
# current students is worth flagging), so the extractor keeps finding it —
# only this campus-scoped chip stops repeating it.
_NON_DISCRIMINATING_STUDY_ON_CAMPUS = {
    "Current student", "Current student or recent graduate",
}


def _fact_chips(o, *, verdict=None) -> list[dict]:
    """What this posting states about applying, as at most three chips.

    Every chip carries `why` — the sentence it was extracted from — which the
    template hangs on `title`. That is the honesty contract from
    directory/facts.py reaching the page: a chip that cannot show the words
    that produced it should not be on the card.

    `verdict` suppresses the fact that PRODUCED it. A visa_out verdict is
    computed from `o.sponsorship == "no"`, so rendering both put "Won't
    sponsor you" and "No sponsorship" side by side on the same card — the
    same fact twice, the second one crowding a real one (a stated grad year)
    off the end of a three-chip row. The verdict is the better of the two:
    it is the personalised reading, and it only exists where both sides
    stated.

    `year_out` is the same failure and gets the same treatment. It is built
    from `facts["grad"]`, so the card rendered "For 2027–2028 grads" (verdict)
    immediately followed by "Grad 2027–2028" (fact) — identical years, the
    identical source sentence in both tooltips, and the identical grey. On the
    first /opportunities/ load 101 of 491 cards carried the pair, across eight
    firms. It costs more than repetition: `_FACT_CHIPS_MAX` is 2, so the
    duplicate eats a slot, and 60 of the 227 affected rows DB-wide have a
    different real fact (GPA, duration, assessment, cover letter) waiting
    behind it — one page-1 card read "For 2027–2028 grads · Grad 2027–2028 ·
    GPA 3.0".

    Suppression is scoped to the BLOCKING verdict on purpose. `year_ok` says
    "Your year" and `year_likely` "Likely your year" — neither names a year at
    all (2026-09-02: they used to carry the READER's class year in brackets,
    which put a year on the row twice in two formats), so the fact chip is the
    only place a student can read what the posting actually stated, and it
    stays. Anonymous visitors get no verdict at all and are untouched.

    Two more duplications, both FACT-vs-FACT rather than verdict-vs-fact, and
    the correct de-dup for each turned out to be value-dependent rather than
    kind-dependent — see `_standing_matches_grad` and
    `_NON_DISCRIMINATING_STUDY_ON_CAMPUS` for the evidence behind each:

    - A class-standing fact ("Penultimate year") and a grad-year fact ("Grad
      2028") that AGREE merge into the one grad chip, quoting both sentences.
      A class-standing fact that does not agree with the grad fact beside it
      (or has no grad fact at all) is never touched — an apparent mismatch is
      surfaced, not papered over.
    - "Current student"/"Current student or recent graduate" is suppressed
      outright, but only where the posting's own bucket is already one of the
      three campus buckets this fact is redundant with.
    """
    facts = (o.raw or {}).get("facts") or {}
    made = {}
    kind = (verdict or {}).get("kind")
    # The language answer, wherever `_eligibility` put it: as the verdict
    # itself when nothing else spoke, or riding on a year verdict under
    # `verdict["language"]` — see `_eligibility` for why it never outranks
    # the year branch. A visa verdict returns before language is read at all
    # and carries no such key, so under a visa wall the chip stays the bare
    # fact. None for anonymous visitors and for students who have not filled
    # Languages in, which leaves the chip exactly as it always was.
    lang_verdict = (verdict if kind in _LANGUAGE_KINDS
                    else (verdict or {}).get("language"))

    # SPONSORSHIP, WITH ITS SOURCE. Read through `effective_sponsorship`, not
    # off `o.sponsorship` — that was the last surface still reading the raw
    # column, and it is the one surface where the decision actually gets made.
    #
    # The consequence, live: the feed's sponsorship filter counts firm-policy
    # rows (304 of them the day this was found), and every one of those cards
    # arrived with NO sponsorship chip at all — matched by a fact the card
    # never showed. A student filtering "Sponsors visas" got rows that looked
    # identical to unfiltered ones and no way to tell whether the answer came
    # from the posting or from the firm's general policy. "Never guess, always
    # show your work" is the whole four-state design; the label existed and
    # simply was not on the page where it mattered.
    #
    # The firm-sourced chip says so IN THE LABEL, not only the tooltip: the
    # tooltip is hover-only, and this product's students are on phones. A
    # firm's policy is a weaker claim about one specific role than the
    # posting's own words — which is exactly why `_eligibility` softens a
    # firm-sourced "no" to a non-blocking warning — so the card must not let
    # the two read the same.
    spon, spon_source = effective_sponsorship(o)
    if kind in ("visa_out", "visa_firm_no"):
        spon = "unknown"   # the verdict beside it already says this
    if spon == "no":
        made["sponsorship"] = (
            {"label": "No sponsorship · firm policy", "css": "fact-wall",
             "why": ("The posting itself does not say. This firm's stated "
                     "policy is not to sponsor visas in this market.")}
            if spon_source == "firm" else
            {"label": "No sponsorship", "css": "fact-wall",
             "why": "The posting says it cannot sponsor a visa"})
    elif spon == "yes":
        made["sponsorship"] = (
            {"label": "Sponsors · firm policy", "css": "fact-ok",
             "why": ("The posting itself does not say. This firm's stated "
                     "policy is to sponsor visas in this market.")}
            if spon_source == "firm" else
            {"label": "Sponsors visas", "css": "fact-ok",
             "why": "The posting says sponsorship is available"})

    labels = {
        "pay": lambda f: f["value"],
        "study": lambda f: f["value"],
        "language": lambda f: _language_chip_label(f, lang_verdict),
        "grad": lambda f: f"Grad {f['value']}",
        "gpa": lambda f: f"GPA {f['value']} pref." if f.get("hedge") else f"GPA {f['value']}",
        "duration": lambda f: f["value"],
        "cover_letter": lambda f: "Cover letter",
        "transcript": lambda f: "Transcript",
        "assessment": lambda f: f["value"],
    }
    # Walls are the facts that can END the decision: a visa answer, a language
    # you do not speak, a year of study you are not in.
    css = {"language": "fact-wall", "study": "fact-wall", "pay": "fact-pay"}
    study_fact, grad_fact = facts.get("study"), facts.get("grad")
    standing_merge = bool(study_fact) and _standing_matches_grad(
        study_fact.get("value"), grad_fact)
    for fact_kind, label in labels.items():
        if fact_kind == "grad" and kind == "year_out":
            continue           # the verdict beside it already says this
        if fact_kind == "language" and kind in _LANGUAGE_KINDS:
            continue           # the verdict beside it already says this
        if fact_kind == "study" and standing_merge:
            continue           # merges into the grad chip below instead
        fact = facts.get(fact_kind)
        if not fact:
            continue
        if (fact_kind == "study"
                and fact.get("value") in _NON_DISCRIMINATING_STUDY_ON_CAMPUS
                and o.bucket in TARGET_BUCKETS):
            continue           # true of ~every row this feed can show at all
        entry = {"label": label(fact), "css": css.get(fact_kind, "fact-plain"),
                 "why": fact.get("phrase", "")}
        if fact_kind == "language" and lang_verdict:
            # The personal reading rides on a year verdict here, so the chip
            # carries it: a match reads green ("you speak it"); a miss keeps
            # the wall styling, because the posting's own words did not
            # change — only what the reader knows about themselves did.
            entry["css"] = ("fact-ok" if lang_verdict["kind"] == "language_ok"
                            else css["language"])
            entry["why"] = lang_verdict["why"]
        if fact_kind == "grad" and standing_merge:
            # One chip, both sentences: the label carries the stage AND the
            # year, and the tooltip keeps the stage phrase's own evidence
            # trail alive rather than letting it vanish with the chip it came
            # from — see the docstring's "quoting both sentences". The `css`
            # stays `fact-wall` — the standing chip's own styling, not plain
            # grad's — because a stage-plus-year requirement is still a wall
            # ("a year of study you are not in"), not a neutral detail.
            entry["label"] = f"{study_fact['value']} · {entry['label']}"
            entry["css"] = css["study"]
            stage_phrase = study_fact.get("phrase", "")
            if stage_phrase and stage_phrase != entry["why"]:
                entry["why"] = f"{stage_phrase} {entry['why']}".strip()
        made[fact_kind] = entry

    return [made[k] for k in _FACT_CHIP_ORDER if k in made][:_FACT_CHIPS_MAX]


def _eligibility_profile(user):
    """What the signed-in user has stated about themselves, for verdicts.
    None for anonymous visitors and users who have stated nothing — a
    verdict requires BOTH sides to have spoken.

    `languages` and `study_level` ride along (2026-09-01) for the lenses that
    read them: `_language_fit` below, and the year branch's reader of
    `study_level`. Both through `getattr` like the rest, so a user-shaped
    stub without the newer columns still yields a profile. `languages` is
    normalised here — stripped, lowercased — so every reader compares the
    same spelling against the extractor's title-cased names."""
    if not getattr(user, "is_authenticated", False):
        return None
    class_year = getattr(user, "class_year", None)
    work_auth = getattr(user, "work_authorization", None) or {}
    languages = [
        str(lang).strip().lower()
        for lang in (getattr(user, "languages", None) or [])
        if str(lang).strip()
    ]
    study_level = (getattr(user, "study_level", "") or "").strip()
    if not class_year and not work_auth and not languages and not study_level:
        return None
    return {"class_year": class_year, "work_auth": work_auth,
            "languages": languages, "study_level": study_level}


def _language_fit(o, profile):
    """A posting's stated language read against the languages the student
    said they can work in — a warning or a match, and NEVER a wall.

    Two findings drive the design. The gate is real: Barclays' Hong Kong
    posting states "fluent in written Chinese and spoken Mandarin if applying
    to the role in Hong Kong SAR", and practitioners put Mandarin on about
    95% of first-year HK IB desks. And the gate is mostly unstated: 8 HK
    campus rows on the live board carry a Mandarin fact, and the rest are
    silent and no less gated. So the language has to be a fact about the
    STUDENT (`User.languages`) matched against the posting's own words,
    never inferred from postings alone — and a posting that names a language
    the student lacks must not block, because the line is often softer than
    it reads and the real gate lives in the silent rows this function cannot
    see. A block here would hide the one posting in seven that was honest
    enough to say so, and none of the six that were not.

    Both sides must have spoken, per the verdict contract: no stated
    language means None, and a student with no languages listed gets None
    too, which leaves their card exactly as it was before the field existed.
    """
    spoken = {
        str(lang).strip().lower()
        for lang in (profile or {}).get("languages") or []
        if str(lang).strip()
    }
    if not spoken:
        return None
    fact = ((o.raw or {}).get("facts") or {}).get("language") or {}
    # `langs` is the extractor's own list; rows extracted before it carried
    # one have only `value`, the " · "-joined display string.
    named = [str(l).strip() for l in (fact.get("langs") or []) if str(l).strip()]
    if not named:
        named = [p.strip() for p in str(fact.get("value") or "").split("·")
                 if p.strip()]
    if not named:
        return None
    phrase = fact.get("phrase", "")
    missing = [lang for lang in named if lang.lower() not in spoken]
    if missing:
        return {"kind": "language_warn", "blocking": False,
                "label": f"{missing[0]} needed · not in your profile",
                "why": (f"{phrase} Your profile does not list {missing[0]}. "
                        "Add it in Settings if you can work in it; this "
                        "never hides a role.").strip()}
    shown = " · ".join(named)
    tail = {1: "you speak it", 2: "you speak both"}.get(len(named), "you speak them")
    return {"kind": "language_ok", "blocking": False,
            "label": f"{shown} · {tail}",
            "why": f"{phrase} Your profile lists {shown}.".strip()}


def _eligibility(o, profile):
    """A PERSONAL verdict on one posting, or None.

    The whole facts pipeline led here: postings state their graduation
    windows and sponsorship stance, Settings knows the user's class year and
    visa status per region, and until now nothing cross-referenced them — a
    feed where 232 of the 240 eligibility-stating roles excluded this user's
    year ranked them identically to the 8 that named it.

    The contract is the extractors' own, applied pairwise: a verdict exists
    ONLY where both sides stated. No stated window means no verdict (never
    "you are probably fine"); no class year in Settings means no verdict.
    Blocking verdicts (wrong stated year, refuses-your-visa) carry
    `blocking: True`, which the fit filter and Picked-for-you read; the
    positive year match earns a chip but blocks nothing.

    Language (`_language_fit`) is the third pairing, and the one that never
    blocks: a visa verdict returns before it is read; a year verdict carries
    it under `"language"` so the card's language chip can still say the
    personal reading; it is the verdict itself only when both are silent.
    """
    if not profile:
        return None
    # Visa first: it is the harder wall. Only when the role NAMES a market
    # and the user has answered for that market as "needs sponsorship" does
    # the sponsorship answer matter at all — `effective_sponsorship` (see
    # directory/sponsorship.py) is what decides that answer, reading the
    # posting first and the firm's per-region policy only when the posting
    # is silent, same precedence the pill and the feed filter use.
    #
    # A posting's own "no" is a hard wall (`blocking: True`) because it is a
    # statement about THIS role. A firm-sourced "no" is softened to a
    # non-blocking warning — a firm's general policy is a weaker claim about
    # one specific role than the posting's own words, and the product's rule
    # is never to block a pick on a guess (docs/founder-decisions-2026-08-20
    # .md, Decision 3).
    region = (o.region or "").lower()
    if region and profile["work_auth"].get(region) == "sponsorship":
        value, source = effective_sponsorship(o)
        if value == "no" and source == "posting":
            return {"kind": "visa_out", "blocking": True,
                    "label": "Won't sponsor you here",
                    "why": ("This posting says it cannot sponsor a visa, and your "
                            "Settings say you need sponsorship in this market")}
        if value == "no" and source == "firm":
            return {"kind": "visa_firm_no", "blocking": False,
                    "label": "Firm policy: may not sponsor here",
                    "why": ("The posting itself does not say, but this firm's "
                            "stated policy is not to sponsor visas in this "
                            "market, and your Settings say you need "
                            "sponsorship here.")}
    # LANGUAGE, as a warning or a match, never a wall (`_language_fit`). It
    # sits here, after the visa branch, but must not outrank the year branch
    # below: a blocking `year_out` is what `Candidate.blocked` and the fit
    # filter read, and `year_ok` is what the bulk-save offer counts, so a
    # non-blocking language chip returned first would silently un-block
    # wrong-year roles and un-count right-year ones for exactly the students
    # who filled the field in. So the year branch is asked first — this same
    # function, with languages withheld (the visa branch above has already
    # answered None for this pairing, so re-entering it changes nothing) —
    # and the language verdict either rides along on what it says or stands
    # alone when it says nothing. Measured live: 33 of 214 language-stating
    # rows also state a grad window, so the ordering is not hypothetical.
    lang = _language_fit(o, profile)
    if lang is not None:
        rest = _eligibility(o, {**profile, "languages": []})
        return {**rest, "language": lang} if rest else lang
    cy = profile["class_year"]
    # The TITLE's own explicit statement ("Class of 2027") — `Opportunity.
    # class_year`, extractors.extract_class_year. This is the rarest and most
    # authoritative signal on the board (~3 rows in 4,000 state one), and
    # `directory.recommend._class_fit` already treats it as such, checking it
    # before the body-derived window below. Missing it here left a real gap:
    # `recommend()`'s own "no combination of tier/track/region may outrank a
    # stated mismatch" promise is enforced by `Candidate.blocked`, which reads
    # only THIS function's verdict — so a title-stated wrong-class role at a
    # firm the student is warm with, tiered, and on-track for could clear
    # MIN_SCORE and rank as a pick, `_class_fit`'s -25 notwithstanding,
    # because nothing here ever produced a blocking verdict for it.
    stated = (o.class_year or "").strip()
    if cy and stated.isdigit():
        stated_year = int(stated)
        if stated_year == cy:
            return {"kind": "year_ok", "blocking": False,
                    # NO PARENTHESISED YEAR (2026-09-02). It used to read
                    # "Your year (2029)", and `cy` is the READER's own class
                    # year, which is the one number on this row the reader
                    # already knows. Meanwhile the same row prints the
                    # posting's own window beside it — "Class of 2029" from
                    # `.rr-cls` on this branch, "Grad 2028-2029" from the
                    # grad chip on the branch below — so the year rendered
                    # twice in two formats on one line while the meta wrapped
                    # to three. The verdict keeps its job (a statement about
                    # the reader against the posting); the figure stays where
                    # it is a fact about the POSTING, plus this tooltip.
                    "label": "Your year",
                    "why": f"The posting states it is for the Class of {stated_year}."}
        return {"kind": "year_out", "blocking": True,
                "label": f"For {stated_year} grads",
                "why": (f"The posting states it is for the Class of "
                        f"{stated_year}, not your {cy}.")}
    grad = ((o.raw or {}).get("facts") or {}).get("grad")
    years = [int(y) for y in (grad or {}).get("years") or () if str(y).isdigit()]
    if cy and years:
        lo, hi = min(years), max(years)
        # An open-ended window -- "graduating in 2028 or later", "December
        # 2028 onwards", "graduation no earlier than June 2026" -- names a
        # floor and no ceiling. The extractor (facts.extract_grad_years)
        # marks it `open_high` and enumerates the years up to its own
        # horizon; the FLAG is what says "and everyone after", so the
        # ceiling is lifted to wherever this student is rather than trusted
        # as the posting's. Before the flag existed the same sentence was
        # stored as the closed window [2028, 2028] and blocked every later
        # class: 34 open campus rows told a 2029 student a role that includes
        # them was not for them. A row written before the flag carries none
        # and still reads closed, exactly as it did.
        if grad.get("open_high"):
            hi = max(hi, cy)
        if lo <= cy <= hi:
            # Bare "Your year" here for the same reason as the stated branch
            # above: the grad chip beside it prints the posting's own window,
            # so the parenthesised class year was the second rendering of one
            # fact on a line already wrapping to three rows.
            return {"kind": "year_ok", "blocking": False,
                    "label": "Your year",
                    "why": grad.get("phrase", "")}
        label = grad.get("value") or (str(lo) if lo == hi else f"{lo}–{hi}")
        return {"kind": "year_out", "blocking": True,
                "label": f"For {label} grads",
                "why": grad.get("phrase", "")}
    # The convention-derived year, and only ever as a POSITIVE, non-blocking
    # signal. A summer 2027 internship conventionally hires the 2028 class,
    # which is worth surfacing to a 2028 student — but the posting never said
    # it, so the same inference must not be turned around to tell a 2029
    # student this role is not for them. A mismatch returns no verdict at
    # all, exactly as if we had never worked anything out. The label says
    # "Likely", not "Your year", and carries its reasoning in the tooltip.
    if cy and o.class_year_derived and int(o.class_year_derived) == cy:
        return {"kind": "year_likely", "blocking": False,
                "label": "Likely your year",
                "why": derive_class_year(o.bucket or "", o.title or "",
                                         o.cohort or "")[1]}
    return None


def _urgency_item(o, *, now, today, my_firm_ids, profile=None, cutoffs=None):
    """One feed card: firm identity + the honest urgency signal for this
    role (a real countdown when dated, freshness when rolling, or an
    explicit "deadline passed" state — see the three-way split below).

    `cutoffs` is `directory.open_runs.onboarding_cutoffs()` for at least the
    firms in this batch. It is a caller-supplied map rather than a lookup
    done in here because this function runs once per row and that map is one
    grouped aggregate for the whole page; computing it per call would be an
    N+1 over the exact query that exists to avoid one. Omitted, the row
    simply carries no elapsed-openness fact — no fallback, no guess.
    """
    bucket = o.bucket or OTHER
    # LOCAL-CALENDAR days, not a raw elapsed floor — see the import comment
    # above. This used to be `(now - o.first_seen).days`, a UTC timedelta
    # floor that is really `elapsed_hours // 24` and drifts from every other
    # "how long ago" fact in the product once elapsed time crosses a local
    # calendar-date boundary. It fed three things off one number: the "first
    # seen Nd ago" text, the "Fresh" pill (`is_fresh` below), and the
    # elapsed-openness bar — so the drift was invisible in the text (both
    # readings are close) but silently flipped the Fresh pill's verdict on
    # rows sitting right at the `_FRESH_DAYS` boundary.
    #
    # `as_of_date=today` rather than `as_of=now`: `today` IS `local_date(now)
    # .date()` — every caller of this function derives the pair from one
    # instant (`now = timezone.now(); today = timezone.localdate()`), which is
    # already required for `open_run_days` and the deadline branches below to
    # agree with this number. Handing the converted day in skips one
    # `timezone.localtime` per row; see `_calendar_days_ago`'s own note.
    seen_days = (_calendar_days_ago(o.first_seen, as_of_date=today)
                 if o.first_seen else None)
    place = _place(o)
    # ONE VERDICT PER ROW. `facts` and `verdict` below are the same call —
    # `_eligibility` walks the visa, language and graduation branches over
    # `raw.facts` — and asking it twice per card doubled that walk for
    # nothing: the two answers are equal by construction (pure function, same
    # arguments) and the chip builder wants exactly the one the card carries.
    verdict = _eligibility(o, profile)
    item = {
        "id": o.id,
        "firm_name": o.firm.name,
        "firm_slug": o.firm.slug,
        "monogram": _monogram(o.firm.name),
        "category": FIRM_CATEGORIES.get(o.firm.slug, ""),
        # Minus a trailing city the place line below already prints — see
        # `_title_without_place_echo`. Never minus anything else.
        "title": _title_without_place_echo(o.title, place["text"]),
        "url": o.url,
        "location": o.location,
        # Same resolver the firm page reads — see `_place`. The two surfaces
        # used to disagree about a blank `location`, this is where they stop.
        "place": place,
        "bucket": bucket,
        "bucket_label": BUCKET_LABELS.get(bucket, bucket),
        # Programme/intake year — rendered next to the role type, never as a
        # class year. `class_year` is the separate, stated-only graduation
        # year and gets its own pill (see classify.py).
        "cohort": o.cohort,
        "class_year": o.class_year,
        "is_mine": o.firm_id in my_firm_ids,
        # HOW THIS FIRM ACTUALLY HIRES, on the card that offers the role.
        #
        # `Firm.recruiting_style` has been a column since the CRM half of
        # this shipped, and until now it reached no opportunity surface at
        # all — it was read by `crm/coverage.py`, `crm/sourcing.py` and one
        # line of `crm/views.py` and nowhere else. So 302 open campus rows at
        # 15 assessment firms (SIG 77, Jump 45, IMC 39, Optiver 35, DRW 22,
        # Virtu 19, …, measured 2026-09-02) carried the same framing as a
        # Citi row, and a pick at one of them could say "You know someone
        # here" as if that were the lever.
        #
        # WHAT THE CHIP MAY SAY, and the limit is the source's own. Jane
        # Street's FAQ declines one-to-one coffee chats by policy and Citadel
        # Securities' campus funnel is entirely competitions and events
        # (`research-st-quant.md` Q3, Grade A). What NO source shows is that
        # networking is counterproductive — only that no mechanism is
        # documented. So the copy says the firm hires by assessment and never
        # says networking hurts. `test_feed_honesty.py` greps for the
        # forbidden phrasings.
        "assessment": (
            {"label": "Test-gated",
             "why": ("This firm's own process is a test or competition. "
                     "Coverage does not score your network here because "
                     "there is no documented path from a chat to the "
                     "pipeline. See the firm page.")}
            if o.firm.recruiting_style == Firm.RECRUITING_STYLE_ASSESSMENT
            else {}
        ),
        "seen_days": seen_days,
        "is_fresh": seen_days is not None and seen_days <= _FRESH_DAYS,
        # How long THIS posting has been open, or None when that is not a
        # fact we hold — see `directory.open_runs` for the full argument,
        # including why a "typically open N days" figure is not shippable off
        # a 39-day observation window and this elapsed one is. Distinct from
        # `seen_days` above, which is a raw age with no onboarding filter on
        # it and no claim that the posting is still open: 880 of the 2581
        # live campus rows have a `seen_days` and correctly get no
        # `open_run_days`, because their `first_seen` records when Coverage
        # arrived rather than when they did.
        "open_run_days": (
            None if cutoffs is None else open_run_days(o, today, cutoffs)
        ),
        "facts": _fact_chips(o, verdict=verdict),
        "reported": deadline_provenance(o),
        "verdict": verdict,
        # Whether the Read control has anything to open. Checked here, not in
        # the template, so the card never offers a drawer that would come back
        # empty.
        "has_text": bool((o.raw or {}).get("detail_text")),
        # {} on a clean confirmation; a label+why when our last check of this
        # URL could not reconfirm it — see `_unconfirmed_note`.
        "unconfirmed": _unconfirmed_note(o, as_of_date=today),
        # HOW OLD THE EVIDENCE UNDER THE COUNTDOWN IS, on every row, as an
        # absolute number of local calendar days since the last POSITIVE
        # liveness signal (`last_verified`, not `last_checked` — see
        # `_unconfirmed_note` for why those are different facts).
        #
        # Distinct from `seen_days`, which is how long Coverage has KNOWN
        # about the posting, and from `open_run_days`, which is how long it
        # has been open. This is the one a deadline rests on: "Closes in 8
        # days" is a claim about a page, and this says when that page was
        # last read. `_rolecard.html` hangs it on the deadline column's
        # `title` for every row and prints it visibly past
        # `_UNCONFIRMED_AFTER_DAYS`, which is the same threshold
        # `_unconfirmed_note` uses rather than a second idea of stale (P5).
        #
        # Degradation is the measurement: 2,627 of 2,723 open campus rows
        # were verified inside 24 hours on 2026-09-01, so the visible half
        # fires on about 3% of the board and the rest gain a tooltip only.
        # `None` when a row carries no `last_verified` at all, which is
        # where the tooltip is simply absent rather than guessed at.
        "verified_days": (
            _calendar_days_ago(o.last_verified, as_of_date=today)
            if o.last_verified else None
        ),
    }
    # Three states, not two. "Rolling" must mean "no posted deadline" (it is
    # tested that way at my_applications, views.py's `rolling` lens above) —
    # so a dated role whose deadline has already PASSED cannot fall into the
    # rolling branch just because `deadline >= today` failed. The old
    # two-branch `if o.deadline and o.deadline >= today: ... else: rolling`
    # did exactly that: a role dated last week rendered "Rolling · New" with
    # "apply early" copy, while /firms/<slug>/ correctly said "deadline
    # passed" for the same row — two Coverage surfaces disagreeing about the
    # same fact. A passed deadline is dated (so the freshness badge, gated on
    # `not dated` in the template, never shows on it) but neither "closing"
    # nor "rolling"; `_urgency_feed` below sorts it to the very end.
    if o.deadline is None:
        # "Rolling" is a CLAIM, and until now the page made it about every
        # undated role — ~600 of which simply never said anything about when
        # they close. The word is kept for the postings whose own text states
        # rolling review (facts["rolling"], extracted from the description we
        # already hold) and the rest say the true thing instead: no date was
        # posted. Same sort position, same band; only the label divides.
        stated = bool(((o.raw or {}).get("facts") or {}).get("rolling"))
        item.update({"dated": False, "days_left": None, "level": "rolling",
                     "rolling_stated": stated,
                     "rolling_why": (((o.raw or {}).get("facts") or {})
                                     .get("rolling", {}).get("phrase", ""))})
        # The card's own weaker cousin of `fuse_pct`: a real deadline earns a
        # bar that BURNS DOWN toward a claimed date, but there is no claimed
        # date here to burn down toward — only a fact this row's `meta-seen`
        # text already states in words ("first seen 18d ago"). This is that
        # same fact again, in the footer slot the fuse would otherwise leave
        # visually empty on this card, so a dated and a rolling card don't
        # read as two different levels of finish. Growing rather than
        # burning (it fills as MORE time passes, the fuse empties as LESS
        # remains), uncoloured, and never animated — see `_rolecard.html`
        # and `_styles.html`'s `.rolecard-observed` for why that difference
        # in motion and colour is load-bearing, not decorative: a measured
        # elapsed time is a weaker claim than a real deadline and must never
        # look like a stronger one. `None` only if `first_seen` is somehow
        # unset, which `auto_now_add` should make impossible in practice.
        item["elapsed_pct"] = (
            None if seen_days is None else
            max(0, min(100, round(min(seen_days, _ELAPSED_HORIZON) / _ELAPSED_HORIZON * 100)))
        )
    # An inexact date keeps its exact `days_left` and loses its day-level
    # VOICE. The two are different jobs and only one of them can lie.
    #
    # `days_left` is an ordering and aggregation key — `_urgency_feed`'s sort,
    # the firm cluster's `next_days` (which must never go negative), the
    # cluster role sort, and the closing-this-week count all read it, and each
    # breaks differently on a coarser value. So it stays the true day count:
    # a "Sep 2026" row still sorts among the September rows, which is right.
    #
    # What the reader SEES is the part that has to respect precision. The
    # countdown borrows `deadline_marker`'s own phrasing rather than restating
    # it, so the feed and the firm page can never word the same row two ways;
    # the urgency band drops to "upcoming" (a month-level date cannot earn the
    # red "closes today" treatment); and the fuse bar — which burns down to a
    # specific afternoon — is suppressed outright rather than drawn against a
    # day nothing stated.
    elif o.deadline >= today:
        days = (o.deadline - today).days
        prec = (o.deadline_precision or "").lower()
        inexact = prec in _INEXACT_PRECISIONS
        item.update({
            "dated": True,
            "days_left": days,
            "countdown": (
                deadline_marker(o.deadline, prec, today=today)["countdown"]
                if inexact else
                "Closes today" if days == 0 else
                "Closes tomorrow" if days == 1 else
                f"Closes in {days} days"
            ),
            # The same fact at column width, for the compact row's fixed
            # deadline column (_rolecard.html) — "Closes in 12 days" does not
            # fit 44px and the full sentence stays the accessible name.
            #
            # An INEXACT date gets a month, never a day count: `days` exists
            # for these rows but nothing ever said which day of the month, so
            # "18d" would assert a day the row does not hold. That is the same
            # reason `fuse_pct` is None here — see its comment below.
            "countdown_short": (
                f"{_month_distance(o.deadline, today)}mo" if inexact else
                "Today" if days == 0 else
                "1d" if days == 1 else
                f"{days}d" if days > 0 else "Past"
            ),
            "level": (
                "upcoming" if inexact else
                "today" if days <= 2 else "soon" if days <= 7 else "upcoming"
            ),
            # Remaining fraction of the fuse (100 = far out, ~0 = closing).
            # None on an inexact date: a fuse is a day-level claim.
            # `_styles.html`'s `.fuse-fill` renders at `width: var(--fuse)`,
            # animating DOWN from a full bar to that width — so this number
            # IS the bar's own remaining length, not "how much has burned".
            # A stray `1 -` here inverted the mapping: a role closing TODAY
            # computed to 100 (a full, unburnt-looking bar) and a role 45
            # days out computed to the floor of 4 (nearly invisible) — the
            # exact opposite of "the closer the deadline, the shorter the
            # fuse" the `.fuse-passed` rule two lines below (`fuse_pct: 0`)
            # already assumes as its other endpoint.
            "fuse_pct": (
                None if inexact else
                max(4, round(min(days, _FUSE_HORIZON) / _FUSE_HORIZON * 100))
            ),
        })
    else:
        item.update({
            "dated": True,
            "days_left": (o.deadline - today).days,  # negative: days overdue
            "countdown": "Deadline passed",
            "level": "passed",
            "fuse_pct": 0,
            # LONG past, and the board still lists it. NOT a closed row — see
            # `_abandoned_note` and `_ABANDONED_AFTER_DAYS` for why a prose
            # date may never close one — so this is a note and a withdrawn
            # Save button, nothing else. `{}` on every row inside the window,
            # which keeps the template's condition one truthiness test.
            "abandoned": _abandoned_note(o, today=today),
        })
    return item


# A trailing title segment that restates the row's own location — the shape
# firms use to post one programme per city ("GCB Summer Analyst - 2027 -
# Hong Kong"). The location check is the whole rule: a first attempt grouped
# on any stripped tail and promptly merged "Internship - Financial Engineer"
# with "Internship - Cyber Security" — different JOBS, not one job in two
# cities. A tail only counts as a city when every word of it (4+ chars)
# appears in the row's own location field, so desk names can never match.
_TITLE_TAIL = re.compile(r"\s*[-–—(]\s*([^-–—()]+?)\s*\)?\s*$")


def _family_key(o):
    """(base title, tail) when the title ends in its own location, else None."""
    m = _TITLE_TAIL.search(o.title or "")
    if not m:
        return None
    tail = m.group(1).strip()
    loc = (o.location or "").lower()
    words = [w for w in re.split(r"[ ,]+", tail.lower()) if len(w) >= 4]
    if tail and loc and words and all(w in loc for w in words):
        return (o.title[:m.start()].strip().lower(), tail)
    return None


def _group_city_variants(items, opps):
    """Fold one-programme-many-cities rows into their first sibling.

    DISPLAY-ONLY, and deliberately so: the save-semantics question ("does
    starring a grouped card star all six cities?") is dissolved rather than
    answered. Siblings keep their whole card — their own Save, their own
    Read, their own deadline — tucked behind a "+N more locations" disclosure
    on the first family member. Nothing about any row's meaning changes;
    only how much column the family spends when collapsed.
    """
    fams: dict = {}
    for item, o in zip(items, opps):
        fk = _family_key(o)
        item["variants"] = []
        item["in_group"] = False
        if fk is None:
            continue
        key = (o.bucket, o.cohort, fk[0])
        head = fams.get(key)
        if head is None:
            fams[key] = item
        else:
            item["in_group"] = True
            head["variants"].append(item)


def _urgency_feed(qs, *, now, today, my_firm_ids, profile=None, cutoffs=None,
                  items=None):
    """Rank the filtered set into the Closing-Soon and Fresh-&-Rolling bands.
    Dated roles sort by nearest deadline; rolling roles sort by your-firm
    first, then freshest-seen, then this-cycle cohort.

    `items` is an optional `{id: item}` map the caller has ALREADY built with
    `_urgency_item` over these same rows — the same caller-supplies-the-batch
    posture `cutoffs` holds one line down, and for the same reason. The
    `opportunities` view renders this band and the firm clusters off one
    `rows` list, and used to build a card per row twice: 5,166 `_urgency_item`
    calls at campus scope and 30,068 at `?role=all`, each one re-running
    `_calendar_days_ago`, `_place` and the chip builders over a row whose
    answer was already sitting in the other loop's dict.

    The band takes a SHALLOW COPY of each supplied item rather than the item
    itself, which is what keeps this a pure speed-up. The cluster loop mutates
    its own dicts after this call — `_group_city_variants` writes `variants` /
    `in_group`, and the save-star pass writes `track_status` — and every one
    of those is a top-level key on a card the band never showed before. Handing
    the band the same objects would have quietly given its cards the clusters'
    stars and grouping."""
    closing, rolling = [], []
    # One grouped aggregate for the whole band, scoped to the firms actually
    # in it — see `_urgency_item`'s `cutoffs` note. The `opportunities` view
    # renders this band AND the firm clusters off the same `rows`, so it
    # passes its own map in rather than paying for the identical aggregate
    # twice on one page; `None` means nobody supplied one and this is a
    # standalone call.
    qs = list(qs)
    if cutoffs is None:
        cutoffs = onboarding_cutoffs({o.firm_id for o in qs})
    for o in qs:
        prebuilt = items.get(o.id) if items is not None else None
        item = dict(prebuilt) if prebuilt is not None else _urgency_item(
            o, now=now, today=today, my_firm_ids=my_firm_ids,
            profile=profile, cutoffs=cutoffs)
        (closing if item["dated"] else rolling).append(item)

    # Passed-deadline rows are "dated" (see `_urgency_item`) but are neither
    # urgent nor rolling — sort them after every live-deadline row regardless
    # of `is_mine`/`days_left`, which would otherwise put the most-overdue
    # role first (most negative `days_left` sorts smallest).
    closing.sort(key=lambda i: (i["level"] == "passed", not i["is_mine"], i["days_left"]))

    def rolling_key(i):
        # your-firm first, then fresher first, then earlier cohort (this
        # cycle before next), then firm name.
        seen = i["seen_days"] if i["seen_days"] is not None else 9999
        return (not i["is_mine"], seen, i["cohort"] or "9999", i["firm_name"].lower())

    rolling.sort(key=rolling_key)
    return {
        "closing": closing,
        "rolling": rolling[:_ROLLING_FEED_CAP],
        "rolling_total": len(rolling),
        "rolling_more": max(0, len(rolling) - _ROLLING_FEED_CAP),
        "fresh_count": sum(1 for i in rolling if i["is_fresh"]),
    }


def cycle_months(months: int = 12) -> list[dict]:
    """Deadline density for the next N months, template-ready.

    Reads role deadlines AND confirmed firm dates, so the shape reflects
    everything the product would put a countdown on. Height is relative to
    the busiest month (the calendar rail's convention); an empty month is a
    baseline tick, because "nothing closes in July" is the insight that tells
    a student when to breathe.
    """
    from collections import Counter

    from .models import FirmDate

    today = timezone.localdate()
    counts: Counter = Counter()
    for d in Opportunity.objects.filter(
            status="open", bucket__in=TARGET_BUCKETS,
            deadline__gte=today).values_list("deadline", flat=True):
        counts[(d.year, d.month)] += 1
    # `confidence=1.0` alone, with no `precision` check, is exactly the bar
    # `_firm_date_row` was written to replace — a `precision="estimated"`
    # row (a month-level GUESS, rendered "~ Sep 2027" everywhere else) could
    # sit at confidence 1.0 and bump this band as if the firm had stated the
    # day. No live row pairs the two today, but nothing stops one (see
    # `_CONFIRMED_FIRM_DATE_PRECISIONS`'s comment) — this reads the SAME
    # confirmed bar `_firm_date_row` uses for the timeline right below it on
    # the same firm page, so the two can't drift apart.
    for d in FirmDate.objects.filter(
            confidence__gte=_CONFIRMED_FIRM_DATE_CONFIDENCE,
            precision__in=_CONFIRMED_FIRM_DATE_PRECISIONS,
            date__gte=today,
            event_kind__in=("app_close", "insight_deadline")).values_list("date", flat=True):
        counts[(d.year, d.month)] += 1

    out = []
    y, m = today.year, today.month
    for _ in range(months):
        n = counts.get((y, m), 0)
        out.append({"label": date(y, m, 1).strftime("%b"), "count": n,
                    "is_now": (y, m) == (today.year, today.month)})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    busiest = max((r["count"] for r in out), default=0)
    for r in out:
        r["pct"] = round(100 * r["count"] / busiest) if busiest else 0
    return out


def _qs_without(request, param: str) -> str:
    """The live querystring minus one parameter — the scope-line link's
    contract: built from the REAL request, never hardcoded, because the
    bare-`?` version of this shipped once and sent people to the very page
    that hid what it promised to reveal."""
    q = request.GET.copy()
    q.pop(param, None)
    return q.urlencode()


def opportunities(request, *, pick_only=False):
    """The Opportunities page (public, no login): open campus roles joined
    to firms, sorted by deadline proximity (nulls last), with querystring
    filters for role type / region / track / provider / firm / free-text.
    Mounted at the root-level /opportunities/ (site IA), still owned by the
    directory app.

    The default view is the product's actual promise: insight programmes,
    internships, and entry-level roles only. Experienced/"other" postings are
    hidden by default but counted out loud ("N other roles hidden") with an
    explicit way in — never silently dropped, never mixed into the campus
    list (build-plan honesty posture applied to classification).

    htmx: the filter bar re-fetches this same URL and swaps only the results
    list; an HX-Request gets the list partial, a plain GET gets the full
    page. Works with JS off, too (the form is a normal GET form).

    `pick_only` asks for the Picked column alone, as htmx's out-of-band half
    of a dismissal made from a card elsewhere on the page. Keyword-only and
    defaulted, so the URLconf still calls this with a request and nothing
    else."""
    now = timezone.now()
    today = timezone.localdate()

    open_qs = Opportunity.objects.filter(status="open").select_related("firm")

    # Roles the user has said are not for them leave the feed. Reversible —
    # `hidden_count` below offers them back — but a decision the board ignores
    # is not a decision. Signed-out visitors have no dismissals, so this costs
    # them a set literal and no query.
    hidden_ids: set[int] = set()
    # Rows this user has a relationship with (tracked, applied, dismissed).
    # Only used to break ties when two copies of one posting are folded below:
    # showing the copy they never touched would make their own pipeline state
    # look lost.
    sticky_ids: set[int] = set()
    if request.user.is_authenticated:
        from analytics.models import UserOpportunity
        mine = list(
            UserOpportunity.objects.for_user(request.user)
            .values_list("opportunity_id", "dismissed")
        )
        sticky_ids = {oid for oid, _ in mine}
        hidden_ids = {oid for oid, dismissed in mine if dismissed}
        if hidden_ids:
            open_qs = open_qs.exclude(id__in=hidden_ids)

    role = request.GET.get("role", "").strip()
    # Programme/intake year, or `none` for the rows that state no year at all.
    year = request.GET.get("year", "").strip()
    region = request.GET.get("region", "").strip()
    # A retired track in the query string reads as no track at all (D-3).
    # The facet does not offer `corp-strat` any more, so honouring it here
    # would filter the board to a value the control cannot show as chosen:
    # the bar would say "Any Track" over five rows. An old bookmark gets the
    # whole board back, which is what the bar it renders will say.
    track = request.GET.get("track", "").strip()
    if track in RETIRED_TRACKS:
        track = ""
    provider = request.GET.get("provider", "").strip()
    # Sponsorship (?sponsorship=yes|no|unknown). The first question an
    # international student asks about any US posting, answerable on the rows
    # `enrich_postings` has read, and until now not askable at all.
    sponsorship = request.GET.get("sponsorship", "").strip()
    # Multi-select firm filter: any number of ?firm=<slug> params.
    firm_slugs = [s.strip() for s in request.GET.getlist("firm") if s.strip()]
    query = request.GET.get("q", "").strip()

    selected = {
        "role": role,
        "year": year,
        "region": region,
        "track": track,
        "provider": provider,
        "sponsorship": sponsorship,
        "firm": firm_slugs,
        "q": query,
    }

    # ---- Facets. THE CONSISTENCY RULE: every closed-vocabulary control shows
    # a live per-option count; open-ended (Search) and identity (Companies)
    # controls show none. Each facet is computed against every OTHER active
    # filter and never its own — `skip=("region",)` is that sentence in code —
    # so every number honestly answers "under my current filters, how many?".
    #
    # Region and Track gained counts here. Cost was the open question: on a
    # public unauthenticated page, is full cross-filtering affordable? Measured
    # against the live 4,342-row open set before choosing. It is, comfortably —
    # the GROUP BY implementations are cheaper than the row-by-row Python walk
    # they replaced (campus scope: 6.8ms for options AND counts vs 11.8ms for
    # options alone; role=all worst case: 15.2ms vs 53.9ms). No fallback taken.
    #
    # These read `open_qs`, not a pre-narrowed queryset, so each one composes
    # its own filter set from scratch and cannot inherit a filter it is meant
    # to exclude.
    facets = {
        "regions": _region_facet(
            _apply_filters(open_qs, selected, skip=("region",)), region
        ),
        "tracks": _track_facet(
            _apply_filters(open_qs, selected, skip=("track",)), track
        ),
        "sponsorship": _sponsorship_facet(
            _apply_filters(open_qs, selected, skip=("sponsorship",)), sponsorship
        ),
        # URL-only filter (?provider=): no student thinks in ATS providers, so
        # it earns no control in the bar and therefore no counts. The option
        # list still travels so the vocabulary is inspectable.
        #
        # `.order_by()` IS THE DISTINCT. `Opportunity.Meta.ordering` is
        # `["-first_seen"]`, and Django adds every ordering column to the
        # SELECT list of a `.distinct()` — so this compiled to `SELECT
        # DISTINCT source, first_seen ... ORDER BY first_seen DESC`, which is
        # distinct over PAIRS and therefore not distinct at all. Measured on
        # the live board 2026-09-01: 16,029 rows and 37 ms, and because
        # `sorted()` does not dedupe, the list handed to the template held
        # every duplicate — 16,029 entries of an 18-value vocabulary. Clearing
        # the ordering the facet never wanted gives 18 rows in 5 ms.
        "providers": sorted(
            s for s in
            open_qs.order_by().values_list("source", flat=True).distinct()
            if s
        ),
    }
    year_facet = _year_facet(
        _apply_filters(open_qs, selected, skip=("year",)), year
    )

    # ---- Segment counts, FOLDED, which is what makes them the same number
    # the page then renders. Until 2026-09-02 this was a `Counter` over
    # `values_list("bucket")` — one row of the board, one unit of the count —
    # while the strip below counts the rows a student actually sees, i.e.
    # AFTER `fold_duplicates` collapses repeat requisitions. So the segmented
    # control said "All Campus (3095)" and the strip eight pixels under it
    # said "2958 Open Roles", and the page had to spend a footnote explaining
    # 137. The footnote is gone because the difference is: each segment now
    # answers "click me and this is how many roles you get".
    #
    # PER SCOPE, NEVER A SUM OF BUCKETS. Each segment folds the rows that
    # segment's own page would fold — `all` over everything, `All Campus`
    # over the three campus buckets together, a single bucket over itself —
    # because that is the only construction that is exact for every segment
    # rather than exact for the ones whose clusters happen not to straddle a
    # bucket boundary. (None straddles one on the live board today: measured
    # 2026-09-02, folding the three campus buckets together and summing three
    # separate folds both give 2958. That is data, not an invariant, and a
    # count is a promise.)
    #
    # STICKY IDS ARE NOT PASSED, and their absence is not an approximation.
    # `fold_duplicates` reads them only in `_survivor_rank`, which chooses
    # WHICH copy of a cluster survives; the number of survivors, and the
    # number folded away, are identical either way — pinned by
    # test_board_surface.py::test_the_segment_count_does_not_depend_on_who_is_asking.
    # That is the whole reason this count can be folded at all while staying
    # a shared, per-board figure rather than a personal one.
    #
    # THE COST, measured end to end on the live 16,655-row open board
    # 2026-09-02, median of five warm renders: the default campus page went
    # 364 ms to 424 ms, and `?role=all` went 1100 ms to 1061 ms — faster,
    # because the memo table this made worth adding to `dupes.normalize_label`
    # also pays for the fold that page was already doing. Same number of
    # queries; the old `Counter` over `values_list("bucket")` becomes this one
    # with eight more columns on it. Only the segments that are DRAWN are
    # folded: `other` is a deep-link opt-in and its 13,547 rows are folded
    # only on `?role=other`.
    #
    # `?dupes=1` switches the fold off for the whole render (see the fold
    # itself, far below). The counts follow it: a student who asked to see
    # repeat listings is asking the segments to count them, and a pill that
    # went on quoting the folded number while the board beside it showed the
    # unfolded one would be the same defect this whole block removes, wearing
    # the escape hatch as a disguise.
    dupes_shown = request.GET.get("dupes", "").strip() == "1"
    scoped = _apply_filters(open_qs, selected, skip=("role",))
    fold_rows = [
        _FacetRow(*t) for t in scoped.order_by().values_list(*_FACET_FOLD_FIELDS)
    ]
    rows_by_bucket: dict[str, list] = defaultdict(list)
    for r in fold_rows:
        rows_by_bucket[r.bucket or OTHER].append(r)

    def _folded_count(value):
        """How many roles `?role=<value>` would put on the board."""
        if value == "all":
            subset = fold_rows
        elif value == "":
            subset = [r for r in fold_rows if r.bucket in TARGET_BUCKETS]
        else:
            # `_apply_role_filter` files a blank bucket under `other`, and so
            # does `rows_by_bucket` above, so this covers both.
            subset = rows_by_bucket.get(value, [])
        return len(subset) if dupes_shown else len(fold_duplicates(subset)[0])

    # `role_facet` (the full `ROLE_CHOICES` list with labels) is gone with the
    # Counter: no template ever read it, it existed only to build this dict,
    # and building all six entries would mean folding the `other` bucket on
    # every request to answer a question only a deep link asks.
    role_count = {v: _folded_count(v) for v in (*SEGMENT_VALUES, "all")}
    # `role == OTHER`, the same condition `role_optin_segment` below draws its
    # pill on, so the count exists exactly when something reads it.
    if role == OTHER:
        role_count[role] = _folded_count(role)

    # ---- The segmented control (row 1). Scope is not a filter: "which campus
    # role?" is a facet, but "campus vs everything" is a MODE, and a <select>
    # gave all six values equal billing while hiding the page's single most
    # important scoping decision behind a closed control.
    #
    # `other` stays deliberately absent from the drawn segments — it is an
    # opt-in, reachable only by deep link, never a sibling option. `all` used
    # to be the same kind of opt-in, reachable only through the subset
    # sentence's "Show everything" link; that sentence was removed from the
    # header (2026-08-27, "take this thing away") and its escape hatch had to
    # stay one action away, so `all` is now drawn here too — a real, always
    # visible fifth segment ("Everything") with its own live count, not a
    # deep-link-only acknowledgment. `ROLE_OPTIN` (still `(OTHER, "all")`) is
    # left alone: `firm_detail`'s own simpler `?role=` toggle still reads it
    # and still treats "all" as an opt-in there, where there is no segmented
    # control to hold it.
    effective_role = _effective_role(role)
    FEED_SEGMENT_VALUES = (*SEGMENT_VALUES, "all")
    role_segments = [
        {
            "value": v,
            "label": SEGMENT_LABELS[v],
            "count": role_count.get(v, 0),
            "checked": v == effective_role,
            # Stable ids: the radio's `for=` target and the count span the
            # out-of-band swap addresses (see `_filter_counts.html`).
            "input_id": f"seg-role-{v or 'campus'}",
            "count_id": f"cnt-role-{v or 'campus'}",
            "css": f"seg-{v or 'campus'}",
        }
        for v in FEED_SEGMENT_VALUES
    ]

    # THE NUMBER THE CHECKED SEGMENT SHOWS, named so the page can hold it
    # against the stat strip's `total` further down. It used to be there so
    # the strip could FOOTNOTE the difference; there is no difference left to
    # footnote, and that is the point.
    #
    # `board_count == total` exactly, whenever "Eligible only" is off — both
    # are the same rows after the same fold. The one cut that can still
    # separate them is the fit hide, which is genuinely personal ("your
    # stated year", "your visa") and so cannot reach a shared segment count.
    # `board_count - hidden_fit == total` is therefore the identity now,
    # pinned by
    # test_board_surface.py::test_the_board_count_and_the_strip_total_reconcile,
    # and `_results.html` states the subtrahend only when it is non-zero —
    # next to a checkbox the student ticked, which is also the way back.
    #
    # `hidden_dupes` is no longer part of the identity and the page no longer
    # names it. That restores the 2026-08-28 decision recorded in
    # `opportunities.html`: repeat listings fold silently, because a firm
    # re-filing one job as several requisitions is noise on every reading of
    # this page and never a role a student was trying to reach. It stays in
    # the context for the tests that pin the fold, not for the template.
    #
    # Measured on the founder's live board when this shipped: segment 2958,
    # strip 2958, 137 rows folded and nothing to say about them.
    board_count = role_count.get(effective_role, 0)

    # THE CONDITIONAL FIFTH SEGMENT — deep-link honesty, and a real bug guard,
    # now for `other` alone. `all` graduated to a normal segment above, so
    # gating this on the full `ROLE_OPTIN` tuple would draw it twice — once as
    # the always-visible pill, once again here — and check two radios sharing
    # one value.
    #
    # Two jobs remain for `other`. (1) With `?role=other` active the bar must
    # say so rather than drawing five pills, none checked, over a feed showing
    # thousands of experienced rows. (2) Far less obvious and far more
    # damaging: a radio GROUP WITH NO CHECKED MEMBER SERIALIZES NOTHING.
    # Without this segment, `?role=other` renders five unchecked radios, and
    # the moment the student touches Region or Search the htmx GET goes out
    # with no `role` key at all — the mode silently resets to campus and every
    # experienced row vanishes mid-interaction. A checked fifth radio keeps
    # `role` in the serialization, which is why it is an input and not a
    # decorative chip.
    role_optin_segment = None
    if role == OTHER:
        role_optin_segment = {
            "value": role,
            "label": SEGMENT_LABELS[role],
            "count": role_count.get(role, 0),
            "input_id": f"seg-role-{role}",
            "count_id": f"cnt-role-{role}",
        }

    # ---- The region honesty line. Picking a concrete market excludes every
    # row whose location resolved to nothing (297 of 886 on the live set), and
    # before this the page said nothing about it. The number is read straight
    # off the Region facet's own "Other / Unstated" option — already crossed
    # against every other active filter — so the sentence and the option can
    # never disagree.
    hidden_region = 0
    if region.lower() in REGION_ORDER:
        hidden_region = next(
            (o["count"] for o in facets["regions"] if o["value"] == REGION_NONE), 0
        )
    show_unregioned_params = request.GET.copy()
    show_unregioned_params["region"] = REGION_NONE
    show_unregioned_qs = show_unregioned_params.urlencode()

    qs = _apply_filters(open_qs, selected)

    # The user's target firms, read ONCE. This used to be two queries against
    # the same rows — a `firm_id` set here and a `firm_id -> tier` dict thirty
    # lines below — which is the same table fetched twice per request for
    # strictly less information the first time. The set is just the dict's
    # keys, so it is derived rather than re-queried.
    tier_by_firm: dict[int, int | None] = {}
    if request.user.is_authenticated:
        tier_by_firm = dict(
            UserFirm.objects.for_user(request.user).values_list("firm_id", "tier")
        )
    my_firm_ids: set[int] = set(tier_by_firm)

    # The urgency feed is the star: rank what to act on NOW. Two honest
    # bands, because the data has two kinds of urgency:
    #   1. Closing Soon — the few roles with a real posted deadline (a true
    #      countdown).
    #   2. Fresh & Rolling — the many rolling-review roles, where "apply
    #      early" is the whole game, so we rank by how recently WE first saw
    #      the posting (Coverage's own first_seen — a signal no ATS exposes).
    # ONE trip to the database for the page's big query. `qs` used to be
    # iterated twice — once here, once for the firm clusters below — which
    # executed the same ~900-row, firm-joined SELECT twice per view. The
    # cluster loop's ordering is applied here; `_urgency_feed` sorts its
    # bands in Python and never cared about SQL order.
    rows = list(qs.order_by("firm__name", F("deadline").asc(nulls_last=True), "title"))

    # ---- Duplicate folding (?dupes=1 to switch it off), on the MATERIALISED
    # rows. WHICH copy survives depends on who is asking (a copy the student
    # already tracked wins the tie), so the survivor never reaches a shared
    # count — but HOW MANY survive does not, which is what lets the segmented
    # control above fold before it counts and quote the same number this
    # produces. See `_folded_count`.
    #
    # Some firms genuinely file one job as several requisitions — SIG posts
    # every 2027 internship under two iCIMS job numbers, Deutsche Bank runs
    # apprentice intakes as parallel reqs — and those reqs close on their own
    # schedules, so the rows must all stay in the database and stay
    # close-tracked. Only the render is collapsed. See directory.dupes.
    # `dupes_shown` is parsed WAY up at the top of this view, before the facet
    # counts, because those counts have to answer the same question this does:
    # a student who asked to see repeat listings is asking the segments to
    # count them too.
    rows, hidden_dupes = ([r for r in rows], 0) if dupes_shown else fold_duplicates(
        rows, sticky_ids=sticky_ids
    )

    # The undo, built from the LIVE querystring like every other one on this
    # page — a hardcoded `?dupes=1` would silently drop the student's filters.
    show_dupes_params = request.GET.copy()
    show_dupes_params["dupes"] = "1"
    show_dupes_qs = show_dupes_params.urlencode()

    # ---- The fit filter (?fit=1), applied to the MATERIALISED rows. It is
    # deliberately not in _apply_filters: it depends on who is asking, so it
    # must never shape the shared facet counts, and it hides only roles whose
    # own text BLOCKS this user (wrong stated year, refuses their visa) —
    # silence never hides. The scope line below owns the honesty: the count
    # of hidden rows is stated, with one click to bring them back.
    # ---- Column lazy-loading (?cols=N). Parsed HERE, before the feed and
    # the recommender run, because a non-zero cursor is what lets both be
    # skipped: a cols= request is the sentinel asking for more columns, and
    # the first version of this parsed the cursor after the work it was
    # supposed to avoid. Counts are still computed over the FULL list before
    # slicing — the strip describes the board, not the loaded fraction.
    COLS_PAGE = 12
    try:
        cols_from = max(int(request.GET.get("cols", 0)), 0)
    except ValueError:
        cols_from = 0
    # Heavy work is skipped only when BOTH halves hold: a cursor AND the htmx
    # header. The sentinel's own noscript link (and any bookmarked ?cols= URL)
    # arrives without the header and renders the FULL page — the first cut
    # keyed the skip on the cursor alone, so exactly the no-JS fallback the
    # sentinel carries for honesty was the request that crashed on feed=None.
    cols_fragment = bool(cols_from) and bool(request.headers.get("HX-Request"))
    # The mirror-image fragment: `pick_only` asks for the Picked column alone
    # (see the early return that uses it, far below). A KEYWORD, not a
    # querystring flag like `cols=`, because nothing about it belongs in a
    # URL: `_refresh_feed` is its only caller, and a `?pick=1` in the address
    # bar would leak into every "show me the hidden ones" link this very block
    # builds out of the live request.
    pick_fragment = pick_only and bool(request.headers.get("HX-Request"))

    elig_profile = _eligibility_profile(request.user)
    fit = request.GET.get("fit", "").strip() == "1" and elig_profile is not None
    hidden_fit = 0
    if fit:
        keep = []
        for o in rows:
            v = _eligibility(o, elig_profile)
            if v and v["blocking"]:
                hidden_fit += 1
            else:
                keep.append(o)
        rows = keep

    # Once for the page, shared by the urgency band below and the firm
    # clusters further down — both build their items off this same `rows`.
    cutoffs = onboarding_cutoffs({o.firm_id for o in rows})

    # Every feed item by role id, built ONCE for the page. The urgency band
    # below and the firm clusters further down both render a card per row, and
    # each used to build its own: two `_urgency_item` calls per row, 30,068 of
    # them at `?role=all`, for an identical dict. The band now takes copies of
    # these (see `_urgency_feed`'s `items`), the clusters take them by
    # reference and annotate them in place, and the Picked column copies the
    # annotated ones — which is the same reference contract the map already
    # had, just established a few lines earlier.
    item_by_id: dict[int, dict] = {
        o.id: _urgency_item(o, now=now, today=today, my_firm_ids=my_firm_ids,
                            profile=elig_profile, cutoffs=cutoffs)
        for o in rows
    }

    feed = (None if cols_fragment else
            _urgency_feed(rows, now=now, today=today, my_firm_ids=my_firm_ids,
                          profile=elig_profile, cutoffs=cutoffs,
                          items=item_by_id))

    # Firm clusters are the page: one firm, all its open roles listed below it
    # in its own scroll window. Each role keeps its honest urgency signal (a
    # real countdown when dated, freshness when rolling), and the whole list is
    # personalized from the survey — the user's target firms and their chosen
    # tracks/regions float to the top.
    user_regions = {r.lower() for r in (getattr(request.user, "regions", None) or [])}
    user_tracks = set(getattr(request.user, "tracks", None) or [])

    # Picked-for-you. Scored over the WHOLE open campus set, never the filtered
    # `qs` — a filter must not be able to reorder the ranking or promote a
    # weaker pick into view. (What a filter DOES reach is which of these picks
    # the column displays; that is done further down, where the column is
    # built, and the two halves are deliberately separate.)
    #
    # Signed-out and profile-less users get nothing: `recommend()` returns []
    # for an empty profile, and the template renders an honest sign-in line
    # instead of six generic cards pretending to be tailored. See recommend.py
    # for the scoring itself.
    picks: list = []
    # `{role id: how many branch offices this one card stands for}`, from the
    # same fold that produced the picks — see `picked_roles`. Never a second
    # guess at it, and printed on the card, because a fold nobody can see is
    # the invisible filter this product does not ship (P4).
    pick_places: dict[int, int] = {}
    profile = None
    if request.user.is_authenticated and not cols_fragment:
        # The student's live relationships, collapsed to the warmest per
        # firm. "warm" = a conversation actually happened (chatted or
        # advocate); "replied" = they answered but no chat yet. Cold and
        # archived rows stay out — a contact you added and never reached is
        # not a relationship, and an archived one is a closed door. So do
        # campaign-hidden rows, for the same reason one step further back: an
        # alum who answered a club panel invitation genuinely replied, and
        # counting that as a relationship at their bank would push this
        # student's role recommendations toward a firm he has no recruiting
        # foothold at at all. `crm/campaigns.py`.
        warm_by_firm: dict[int, str] = {}
        for fid, warmth in (Contact.objects.for_user(request.user)
                            .filter(archived=False, firm__isnull=False,
                                    warmth__in=("replied", "chatted", "advocate"))
                            .exclude(id__in=crm_campaigns.excluded_contact_ids(
                                request.user))
                            .values_list("firm_id", "warmth")):
            rank = "warm" if warmth in ("chatted", "advocate") else "replied"
            if warm_by_firm.get(fid) != "warm":
                warm_by_firm[fid] = rank
        profile = Profile.from_user(request.user, tier_by_firm,
                                    warm_firms=warm_by_firm)
        if not profile.is_empty:
            # `picked_roles` owns the whole column — the blocking-verdict
            # filter, the duplicate fold, the ranking, the city-variant fold
            # — because Today's ribbon reads the same column and the two must
            # not be able to disagree about it. It scores `open_qs`, not the
            # filtered `rows`, so a filter can never reorder the ranking or
            # promote a weaker pick (see the note above).
            #
            # `today=today` is the page's own clock, not the ranker's default.
            # `recommend` keeps itself free of Django and so falls back to
            # `date.today()` — the SERVER's local date — while every
            # date-sensitive surface in this view (`today` above, the urgency
            # feed, `deadlines.closing_soon_window`) reads
            # `timezone.localdate()`, i.e. the date in `settings.TIME_ZONE`
            # (UTC). On any host whose OS clock is not UTC the two are a
            # different day for part of every day — eight hours of it on the
            # founder's own machine — and in that window the picks dropped a
            # role as expired that the feed beside them still rendered as
            # closing today. One clock per request, passed in.
            recs, pick_places = picked_roles(
                request.user, open_qs=open_qs, elig_profile=elig_profile,
                rec_profile=profile, sticky_ids=sticky_ids, today=today,
            )
            picks = [_pick_card(r) for r in recs]
    pick_shared, pick_blocks = _group_picks(picks)

    clusters: dict[int, dict] = {}
    # The picks are also rendered as the pinned first column of the feed, so
    # their cards are collected during the same pass rather than re-queried.
    pick_ids = {p["id"] for p in picks}
    pick_items: dict[int, dict] = {}
    # `item_by_id` — every feed item by role id — is built above the urgency
    # band now, so the band and these columns share one build per row. The
    # dicts are still the SAME objects the firm columns render, held by
    # reference, which is what lets the Picked column show the rows "Save
    # all" is offering without building or querying a second set; the column
    # takes its COPY further down, after the track-status annotation.
    for o in rows:
        cl = clusters.get(o.firm_id)
        if cl is None:
            category = FIRM_CATEGORIES.get(o.firm.slug) or next(
                (TRACK_LABELS.get(t, "") for t in (o.firm.tracks or [])), ""
            )
            cl = clusters[o.firm_id] = {
                "firm_name": o.firm.name,
                "firm_slug": o.firm.slug,
                # The firm's own mark, fetched once into our media by
                # `fetch_firm_logos` — never hotlinked, so looking at this
                # board tells no third party which firms you are chasing.
                # Blank for the ~7 firms whose only favicon is 16px, and the
                # monogram remains the always-works fallback for them.
                "logo_url": o.firm.logo.url if o.firm.logo else "",
                "monogram": _monogram(o.firm.name),
                "category": category,
                # No "sponsorship" key. It was `_sponsorship_tag(o)` computed
                # from whichever role happened to open the cluster — the wrong
                # granularity for a per-ROLE fact — and no template ever read
                # it. Each role card carries its own answer via `_fact_chips`.
                "is_mine": o.firm_id in tier_by_firm,
                "tier": tier_by_firm.get(o.firm_id),
                # OR-ed in per ROW below, never read off the firm. It was
                # `Firm.regions` overlapping the student's OR `Firm.tracks`
                # overlapping theirs, which is a claim about the employer:
                # measured 2026-09-01 on the founder's board (HK/US, IB/S&T),
                # 68 of 82 clusters matched and 24 of those had no row in his
                # regions naming one of his tracks at row level — the firm
                # record said "hk" or "ib" somewhere, and the column sorted
                # level with firms whose rows actually were what he asked
                # for. Row level is `_row_tracks` (a silent title still
                # inherits the firm's coverage, so this cannot delete the
                # 49% of rows that state no function) AND the row's own
                # region, both against what the student stated — and a
                # student who stated neither matches everything, which
                # sorts the same as matching nothing.
                "match": False,
                "closing_count": 0,
                "next_days": None,
                "roles": [],
            }
        cl["match"] = cl["match"] or (
            (not user_regions or (o.region or "").lower() in user_regions)
            and (not user_tracks
                 or bool(user_tracks & set(_row_tracks(o.firm.tracks, o.title))))
        )
        item = item_by_id[o.id]
        cl.setdefault("_opps", []).append(o)
        cl["roles"].append(item)
        # Kept by reference here; the Picked column takes its COPY further
        # down, after the track-status annotation has run over these dicts.
        # Copying at this point would freeze a pre-annotation snapshot and
        # every card in that column would draw an un-saved star.
        if o.id in pick_ids:
            pick_items[o.id] = item
        # A passed deadline is `dated` (see `_urgency_item`) but must not
        # inflate the firm's "N closing" pill or drag `next_days` negative —
        # both would misrepresent a dead posting as live urgency, and a
        # negative `next_days` would rank the firm as closing SOONEST in
        # `_cluster_key` below.
        if item["dated"] and item["level"] != "passed":
            cl["closing_count"] += 1
            cl["next_days"] = (
                item["days_left"] if cl["next_days"] is None
                else min(cl["next_days"], item["days_left"])
            )

    # Roles inside a firm: dated soonest-first, then passed, then fresh
    # rolling, then the rest. (`i["level"] == "passed"` as the second key
    # keeps passed-deadline rows out of the days-left ordering, which would
    # otherwise put the most-overdue role first.)
    #
    # `cl["roles"]` and `cl["_opps"]` are built in lockstep above (one
    # `item`/`o` appended per row, same loop iteration) so they start
    # index-aligned — `_group_city_variants` below relies on that alignment
    # to zip each item back to the Opportunity it came from. Sorting only
    # `cl["roles"]` and leaving `cl["_opps"]` in original insertion order
    # broke that alignment: `_group_city_variants` then computed each item's
    # family key from the WRONG Opportunity, folding unrelated postings
    # (different divisions, different cities) under another role's "+N more
    # locations" disclosure. Confirmed live on Morgan Stanley's "2027
    # Technology Summer Analyst Program (Hong Kong)" card, which picked up a
    # Mumbai wealth-management role and a Seattle Parametric role as if they
    # were the same programme in another city. Sorting both lists together
    # as paired tuples keeps them aligned through the reorder.
    for cl in clusters.values():
        paired = sorted(
            zip(cl["roles"], cl.get("_opps", [])),
            key=lambda pair: (
                not pair[0]["dated"],
                pair[0]["level"] == "passed",
                pair[0]["days_left"] if pair[0]["days_left"] is not None else 9999,
                not pair[0]["is_fresh"],
                pair[0]["seen_days"] if pair[0]["seen_days"] is not None else 9999,
                pair[0]["title"].lower(),
            ),
        )
        cl["roles"] = [item for item, _o in paired]
        cl["_opps"] = [o for _item, o in paired]
        cl["open_count"] = len(cl["roles"])
        cl["rolling_count"] = cl["open_count"] - cl["closing_count"]

    # Personalized firm order: my target firms first (T1 before T2 …), then
    # survey track/region matches, then whoever is closing soonest, then A–Z.
    def _cluster_key(c):
        return (
            not c["is_mine"],
            c["tier"] if c["tier"] is not None else 99,
            not c["match"],
            c["next_days"] if c["next_days"] is not None else 9999,
            c["firm_name"].lower(),
        )

    cluster_list = sorted(clusters.values(), key=_cluster_key)

    # City-variant families fold within each firm's column (display-only —
    # see _group_city_variants). Done after sorting because it mutates the
    # items in place, and the transient _opps list is dropped here so it can
    # never leak into a template context.
    for cl in cluster_list:
        _group_city_variants(cl["roles"], cl.pop("_opps", []))
    total = sum(c["open_count"] for c in cluster_list)
    cols_next = cols_from + COLS_PAGE if len(cluster_list) > cols_from + COLS_PAGE else None
    cluster_page = cluster_list[cols_from:cols_from + COLS_PAGE]
    _cap_roles_per_column(cluster_page)
    personalized = bool(tier_by_firm or user_regions or user_tracks)

    # Annotate each role with the user's own track status (saved / applied /
    # …) so the card's star renders in the right state. One query for the lot.
    if request.user.is_authenticated:
        from analytics.models import UserOpportunity

        tracked = dict(
            UserOpportunity.objects.for_user(request.user)
            .filter(dismissed=False)
            .values_list("opportunity_id", "applied_status")
        )
        for cl in cluster_list:
            for r in cl["roles"]:
                if r["id"] in tracked:
                    r["track_status"] = tracked[r["id"]] or "saved"

    # ---- Continuation slices stop here. ------------------------------------
    # A cols= request is the lazy-load sentinel asking for MORE COLUMNS, and
    # the fragment consumes exactly three things: the slice, the next cursor,
    # and the live querystring. The first version of the lazy loader ran the
    # whole page anyway — recommendations scored over every candidate, the
    # feed bands dressed a second time, four cross-filtered facets, the cycle
    # aggregation — so each scroll of the sentinel cost as much as the page
    # it was meant to lighten. Everything a fragment needs is already true
    # here: filters applied, fit applied, verdicts and save-stars annotated,
    # variants folded.
    if cols_fragment:
        return render(request, "directory/_columns.html", {
            "clusters": cluster_page,
            "cols_next": cols_next,
            "cols_qs": _qs_without(request, "cols"),
        })

    # ---- The Picked column ------------------------------------------------
    # The picks render as the pinned FIRST column of the feed, styled apart
    # from the firm columns. Two consequences of that move, both deliberate:
    #
    # 1. IT IS BUILT AFTER `total` AND OUTSIDE `cluster_list`. Every pick is
    #    already listed under its own firm further along the row, so folding
    #    this column into either would double-count the roles in "N Open
    #    Roles" and add a phantom firm to "N Firms".
    #
    # 2. IT RESPONDS TO THE FILTERS. While the bar sat ABOVE the filter bar,
    #    ignoring them was the honest choice — it answered "what should I look
    #    at" and a filter below it had no business rewriting that. Sitting
    #    INSIDE the filtered pile inverts the reasoning: a column standing
    #    beside four filtered columns, showing internships while the page is
    #    filtered to Insight, would be the only thing on screen lying about
    #    what it contains. The SCORING is still done over the whole open
    #    campus set, so a filter never changes the ranking or promotes a
    #    weaker pick; it only hides picks the student just said they don't
    #    want to see, and the header says how many it hid.
    # BUILT EVEN WHEN THE SCORER RETURNED NOTHING (2026-09-02). It used to be
    # `if picks:` and nothing else, which meant the whole column vanished on
    # a profile whose candidates all fell under `MIN_SCORE` — the one profile
    # that most needs an explanation gets an unannotated grid of firm columns
    # and no hint that "Picked for you" exists at all.
    #
    # That state has been reachable for a while and is about to become
    # ordinary: the recommender's region and level penalties are what push a
    # thin board under the bar, and the audit's own estimate is that the
    # founder's rail may hold one or two rows rather than six once they land.
    # `recommend()` already returns [] correctly and `_results.html` already
    # has copy for an empty column; the two had simply never met.
    #
    # The empty column carries the same two sentences a full one does (the
    # cycle note and the open estimate), which is what makes it an answer
    # rather than an absence: "nothing scored high enough" plus "here is when
    # yours opens" is a complete thought. Signed-out and empty-profile
    # visitors are unchanged — `profile` is None for them and the branch this
    # sits in never runs.
    pick_cluster = None
    if picks or (profile is not None and not profile.is_empty):
        # A COPY of each card, never the shared dict: this column names the
        # firm on every card (its cards come from several firms), and setting
        # that flag on the shared item would print the firm name on the firm's
        # own column too.
        # Each card's OWN reasons — everything `_group_picks` did not lift
        # into the header: the block's firm-level reasons (the "Tier 1"
        # that differs in sentence per firm) plus the role's own. Read off
        # `pick_blocks` rather than the pick dicts, because `_group_picks`
        # strips the pick dicts down to role level in place. Printed as ONE
        # quiet line per card, chip texts joined the way `Recommendation.
        # why` joins them, with every full sentence in the title.
        why_by_id = {
            role["id"]: [*b["reasons"], *role["reasons"]]
            for b in pick_blocks for role in b["roles"]
        }
        visible = [
            {
                **pick_items[p["id"]],
                "show_firm": True,
                # What the city-variant fold did, said out loud on the card
                # that survived it. `None` on every other row, so the card
                # prints nothing rather than a chip meaning zero.
                "places": pick_places.get(p["id"]),
                "pick_reasons": why_by_id.get(p["id"], []),
                **_pick_why_line(why_by_id.get(p["id"], []),
                                 pick_items[p["id"]].get("verdict")),
            }
            for p in picks if p["id"] in pick_items
        ]
        # WHAT "SAVE ALL" WILL WRITE — the unsaved roles of the column as it
        # is rendered, filters and all. Resolved HERE, once, and stashed
        # below, so the count in the header, the count in the confirm and the
        # ids the confirm writes are one fact (see `track_eligible` for the
        # 206/209/208 measurement that forced that discipline on the banner
        # this column replaced).
        pick_save = pick_save_ids(request.user, visible)
        # Built even when the filter hid EVERY pick. A column that silently
        # vanishes the moment you touch a filter reads as breakage, and this
        # page's whole posture is to name what it is holding back rather than
        # quietly shrink — the same rule as the subset sentence and the
        # unregioned-roles line. Empty, it collapses to one honest sentence.
        pick_cluster = {
            "roles": visible,
            "open_count": len(visible),
            "firm_count": len({r["firm_slug"] for r in visible}),
            # THE COLUMN'S ONE PRIMARY ACTION. `save_count` is `len()` of the
            # very list stashed for the confirm to write — never a second
            # count of the same thing — and 0 renders as a sentence saying
            # everything here is saved, never as an empty button.
            "save_count": len(pick_save),
            # Never silently truncated: if the filter hid picks, the column's
            # header says so in its own words.
            "hidden_by_filter": len(picks) - len(visible),
            "total_picks": len(picks),
            # Only the reasons true of EVERY pick are chipped in the header
            # — `pick_shared`, exactly as computed for the old bar. Not a
            # dedupe of all reasons: "Tier 1" carries a different sentence per
            # firm (see `_reason_key`), so three firms would put three
            # identical-looking chips in one header, each justifying a
            # different role. Everything below the shared level keeps its full
            # sentence in the disclosure.
            "reasons": pick_shared,
            # The one sentence that reframes the whole column for an
            # early-cycle student. Jimmy's stored cycle is "2028 Summer
            # Internship"; in August 2026 the board holds ZERO cohort-2028
            # internships because that recruiting has not opened — so every
            # pick is at best adjacent, and the column was presenting
            # prior-cycle near-misses as if they were the thing he asked
            # for. Say it, once, in the header: what he is waiting for is
            # not listed YET, and what is shown is the closest fit today.
            "cycle_note": _cycle_not_open_note(profile, open_qs),
            # …and WHEN it opens, which is the question that sentence raises
            # and never answered. Read from `FirmDate`, labelled as the
            # forecast it is, and {} whenever the corpus cannot say.
            #
            # `cycle_open_note`, not `cycle_open_estimate`: the claim renders
            # as the line and its provenance as the line's `title`. The whole
            # sentence is still built in one place and the digest still sends
            # it whole — see `_cycle_open_parts`.
            "cycle_open": cycle_open_note(profile, today=today),
            # WHY THE COLUMN IS EMPTY, when it is, and only for the reason
            # the filters do not already explain. `hidden_by_filter` above
            # covers "your filters hid them"; this covers the other empty,
            # which until now rendered as no column at all: the scorer read
            # the whole open campus board and nothing on it cleared the bar.
            #
            # It says the bar exists rather than naming its number: 25 is a
            # scoring internal with no meaning to a student, and quoting it
            # would invite exactly the "why is this 24" question the number
            # cannot answer. What a student CAN act on is the two levers that
            # move it, which are their own Settings.
            "nothing_scored": not picks,
        }

    # THE SAVE-ALL OFFER, stashed, so the count in the column header and the
    # ids the confirm writes are the same fact rather than two separately
    # derived ones. The banner this replaced learned that the hard way: it
    # counted the feed's folded rows while the write re-derived its own set
    # from the whole table, and one page load produced 206 in the confirm
    # sentence, 209 rows written and 208 on My Applications' tile (see
    # `track_eligible`).
    #
    # Rewritten on EVERY render of this view, htmx swaps included, because
    # the column is re-rendered on every one of them: whatever number is on
    # screen is the offer that is live. Stale offers cannot accumulate —
    # there is one key and the newest write wins.
    if request.user.is_authenticated:
        request.session[PICK_SAVE_OFFER_SESSION_KEY] = (
            pick_save if pick_cluster else []
        )

    # The two figures the stat strip actually renders. (The old hero widget's
    # total/for-you/funnel counts were dropped with it — they cost 5 queries a
    # request and nothing displayed them.)
    dash = {
        # `0 <=`, not just `<= 7`: a passed-deadline row is also in `closing`
        # (see `_urgency_item`'s three-way split) with a NEGATIVE `days_left`,
        # which would otherwise satisfy `<= 7` and count an already-dead
        # posting as "closing this week".
        "closing_week": sum(
            1 for i in feed["closing"]
            if i["days_left"] is not None and 0 <= i["days_left"] <= 7
        ),
        "fresh_count": feed["fresh_count"],
        # Drives the stat-strip's "Fresh" label so it can never again say a
        # window other than the one `_FRESH_DAYS` actually computes — the
        # bug this replaced hardcoded "This Week" text next to a 10-day
        # window.
        "fresh_days": _FRESH_DAYS,
    }

    # Every covered firm (with an open campus role), for the multi-select.
    all_firms = [
        {"slug": s, "name": n}
        for s, n in open_qs.filter(bucket__in=TARGET_BUCKETS)
        .order_by("firm__name").values_list("firm__slug", "firm__name").distinct()
    ]

    # ---- The Picked column alone, for an out-of-band swap. -----------------
    # A `pick_only` call is a "Not for me" that happened somewhere ELSE on the
    # page (a role card deep inside a firm column) asking this view to restate
    # the one thing that click just changed and that is nowhere near it: the
    # Picked column, its "Save all" count, and the id list stashed above for
    # the confirm to write.
    #
    # It stops HERE, after that stash has been rewritten, and the ordering is
    # the whole point. The number on screen, the number in the confirm
    # sentence and the ids `track_eligible` will actually write are one fact
    # resolved once. A dismissal that updated only the number on screen — or
    # only the stash — would put the two back out of agreement by a new route,
    # and a subtler one than the original (see `track_eligible`).
    if pick_fragment:
        return render(request, "directory/_pickcol.html",
                      {"pick_cluster": pick_cluster, "oob": True})

    context = {
        "hidden_region": hidden_region,
        "show_unregioned_qs": show_unregioned_qs,
        "hidden_fit": hidden_fit,
        "show_unfit_qs": _qs_without(request, "fit"),
        "hidden_dupes": hidden_dupes,
        "show_dupes_qs": show_dupes_qs,
        # The "Show repeat listings" checkbox's own checked state
        # (opportunities.html) — the control that replaced the header
        # sentence's lone `show_dupes_qs` link, so it needs to render
        # checked/unchecked like every other filter-bar toggle.
        "dupes_shown": dupes_shown,
        # The paged slice renders; the full list still backs every count
        # above, so the strip describes the board, not the loaded fraction.
        "clusters": cluster_page,
        "all_cluster_count": len(cluster_list),
        "cols_next": cols_next,
        "cols_qs": _qs_without(request, "cols"),
        "total": total,
        # The checked segment's own number, so the strip can footnote the
        # difference between it and `total`. See `board_count` above for why
        # the two are not the same number and must not be made one.
        "board_count": board_count,
        # Recommendation bar. `picks` empty + `has_profile` true is the honest
        # "nothing clears the bar" state; `has_profile` false is the
        # signed-out / empty-survey state. The template needs to tell those
        # two apart, so both flags travel.
        "picks": picks,
        # `pick_blocks` survives only as the recommend bar's guard: non-empty
        # means "there are picks", which is the one state that section does NOT
        # render (the pinned column carries them instead). `pick_shared` is not
        # passed — it reaches the template inside `pick_cluster.reasons`, and a
        # second copy under its own key was read by nothing.
        "pick_blocks": pick_blocks,
        # The picks as the feed's pinned first column. None when there is
        # nothing to pin — no profile, no picks, or the live filter hid every
        # one of them. Deliberately NOT part of `clusters`: see its
        # construction above for why the counts would go wrong.
        "pick_cluster": pick_cluster,
        "has_profile": bool(profile and not profile.is_empty),
        # Whether the firm order was actually shaped by anything the student
        # told Coverage (target firms, survey regions/tracks). Computed above
        # alongside `_cluster_key`; it was never wired into this dict, so the
        # "Sorted for you" honesty line at _results.html:22 could never render.
        "personalized": personalized,
        "facets": facets,
        # `role_facet` itself does NOT travel: it exists only to build
        # `role_count`, which `role_segments`/`role_optin_segment` below
        # already fold in. No template ever read the raw list.
        # Row 1: the four drawn segments, plus the conditional fifth when an
        # opt-in mode is active. The template renders these; it does not
        # re-derive bucket membership.
        "role_segments": role_segments,
        "role_optin_segment": role_optin_segment,
        "year_facet": year_facet,
        # How many of the row-2 controls are engaged. Drives the mobile
        # disclosure's "Filters · 2" summary AND the decision to server-render
        # it open, so a deep-linked filter is never invisible at 375px.
        # Sponsorship counts here like any other: a filter this badge omits is
        # a filter a phone user can have active and never see.
        "filters_more_active": (
            sum(1 for v in (year, region, track, sponsorship) if v)
            + (1 if firm_slugs else 0)
        ),
        "dash": dash,
        "all_firms": all_firms,
        "selected": selected,
        "has_filters": (any([role, year, region, track, provider, sponsorship, query])
                        or bool(firm_slugs) or fit),
        # The fit filter's own honesty trio: whether it is on, whether the
        # user CAN use it (a verdict needs their Settings to have spoken),
        # and how many rows it hid this request.
        "fit": fit,
        "fit_available": elig_profile is not None,
        # `_results.html` is included on a full render and returned bare on an
        # htmx swap. The out-of-band count fragment must only ship on the
        # second: on a full page load the counts are already correct in the
        # markup, and an OOB element in the initial document is inert noise
        # that htmx would never process.
        "is_htmx": bool(request.headers.get("HX-Request")),
    }

    if context["is_htmx"]:
        return render(request, "directory/_results.html", context)
    return render(request, "directory/opportunities.html", context)


# ---------------------------------------------------------------------------
# Opportunity tracking — the writable side of UserOpportunity. `saved` is the
# feed's one-click star; `submitted`/`interview`/`offer` are the funnel states
# managed on My Applications. Every write is scoped through `.for_user`.
#
# `closed` (shown as "Done") is the terminal state. Three candidates were on
# the table and the other two are wrong:
#
#   - "Offer means done." It doesn't. An offer you are still deciding on is the
#     most live row on the page, and the overwhelmingly common terminal
#     outcome is a rejection, which never produces an offer at all. Folding
#     both into one bucket would leave a student with no way to say "this one
#     is over" about the 90% of applications that end without an offer.
#   - "Reuse `UserOpportunity.dismissed`." That flag already means something
#     else — "hide this from me", a pre-funnel not-interested signal — and
#     every query on this page (and in the feed) filters `dismissed=False`, so
#     a Done row would become invisible by construction. Nothing in the app
#     currently sets it to True; that is an escape hatch to leave alone, not a
#     free field to repurpose.
#
# So: a fifth `applied_status` value. It needs NO migration — `applied_status`
# is a plain CharField(max_length=32) with no `choices` and no DB constraint,
# so the vocabulary lives here, in the one place that writes it.
TRACK_CLOSED = "closed"
_TRACK_STATES = ("saved", "submitted", "interview", "offer", TRACK_CLOSED)
_FUNNEL_STATES = ("submitted", "interview", "offer")

# The pipeline, in display order. This list IS the partition: every tracked
# row lands in exactly one of these, so the section counts sum to the total.
_STAGES = (
    ("saved", "Saved"),
    ("submitted", "Applied"),
    ("interview", "Interviewing"),
    ("offer", "Offer"),
    (TRACK_CLOSED, "Done"),
)
_STAGE_LABELS = dict(_STAGES)


def _track_control(request, opp):
    """Render just the one card's star + status, for an htmx outerHTML swap."""
    from analytics.models import UserOpportunity

    uo = UserOpportunity.objects.for_user(request.user).filter(
        opportunity=opp, dismissed=False
    ).first()
    return render(request, "directory/_track_control.html", {
        "r": {
            "id": opp.id,
            "track_status": (uo.applied_status or "saved") if uo else None,
            # THE SWAP HAS TO KNOW THIS TOO. The partial withdraws Save on a
            # posting the firm has left up long past its own stated deadline
            # (see `_abandoned_note`), and this is the OTHER path that
            # renders it: a student who un-saves such a row gets this
            # response back, and without the flag the Save button would
            # reappear — the one control the feed had just declined to
            # offer, handed back by the click that cleared it.
            "abandoned": _abandoned_note(opp),
        },
    })


# The fact kinds a drawer shows in full, and what to call each one. The card
# has room for two chips; this has room for everything the posting states,
# each beside the sentence it came from — which is the difference between a
# claim and a quotation.
_FACT_LABELS = (
    ("pay", "Pay"),
    ("study", "Year of study"),
    ("language", "Language"),
    ("grad", "Graduating"),
    ("start", "Start date"),
    ("gpa", "GPA"),
    ("duration", "Length"),
    ("cover_letter", "Cover letter"),
    ("transcript", "Transcript"),
    ("assessment", "Assessment"),
    ("rolling", "Closing"),
)


def _drawer_sponsorship(o) -> dict:
    """What this posting says about sponsoring a visa, for the drawer's
    "What the posting states" block.

    Always returns a dict — the silent case included — because silence is
    the answer on most rows and a drawer that simply omits the line leaves
    the reader to supply their own guess about the most decisive fact on the
    page. `value` is the sentence; `phrase` is the evidence behind it, or ""
    where the answer came from the firm rather than the posting and there is
    no posting sentence to quote.

    NEVER A DERIVED BADGE. `research-eligibility-language.md §6` (Grade A):
    the stated claims are four incommensurable kinds, and Barclays appends a
    legal right-to-work disclosure to every posting including ones that also
    say it will sponsor. So this surfaces a sentence or says nothing was
    said; `test_feed_honesty.py` greps the templates to keep it that way.
    """
    value, source = effective_sponsorship(o)
    phrase = ((o.raw or {}).get("facts") or {}).get("sponsorship", {}).get("phrase", "")
    if value == "no":
        return {
            "label": "Visa sponsorship",
            "value": ("This firm's stated policy is not to sponsor visas in "
                      "this market" if source == "firm" else
                      "The posting says it cannot sponsor a visa"),
            "phrase": "" if source == "firm" else phrase,
        }
    if value == "yes":
        return {
            "label": "Visa sponsorship",
            "value": ("This firm's stated policy is to sponsor visas in this "
                      "market" if source == "firm" else
                      "The posting says sponsorship is available"),
            "phrase": "" if source == "firm" else phrase,
        }
    return {
        "label": "Visa sponsorship",
        "value": "Not stated in this posting",
        "phrase": "",
    }


def picked_roles(user, *, open_qs=None, elig_profile=None, rec_profile=None,
                 sticky_ids=(), today=None):
    """`(recommendations, places_by_id)` — the "Picked for you" column, once.

    THE ONE DERIVATION, and the reason this function exists at all. Two
    surfaces read this column: the Opportunities feed draws it, and Today's
    ribbon counts what is unsaved in it. They used to answer two DIFFERENT
    questions (the feed ranked; the ribbon counted a separate year-gated
    "eligible unsaved" set) and so could not be compared at all. Now they
    call this, and a number that disagrees with the column is not reachable
    — the same property the 206/209/208 incident bought for the old banner
    (see `track_eligible`), applied one level up.

    WHAT THE GATES ARE, and that they are all `recommend()`'s own. A pick has
    already survived: the blocking-verdict exclusion below (wrong stated year,
    a posting that refuses this student's visa), `stated_class_mismatch`, the
    RUNG filter — `role_matches_level` with the student's `study_level`, or
    `level_mismatch` where the posting stated a window — the passed-deadline
    exclusion, `MIN_SCORE`, and `MAX_PER_FIRM`. That is a strictly harder bar
    than the retired bulk-save banner's fit gate applied, and the study-level
    half of it is applied harder here than that gate ever did.

    ONE ROW PER DISTINCT JOB, LAST. `fold_duplicates` runs over the candidates
    (two copies of one posting score identically, so an unfolded input spends
    two of six slots saying the same thing) and it deliberately treats the
    stated city as a hard divider, because on a BOARD folding London into New
    York deletes a job from the catalogue. A shortlist is not a board: a firm
    that opens one programme in nine branch offices is ONE decision here, and
    `fold_families` is the product's single answer to "same programme, another
    city" (see `dupes.fold_families`, and `_family_key`, which the feed's own
    "+N more locations" disclosure already groups on).

    AFTER THE GATES, BEFORE THE CAP, and both halves of that are load-bearing.

    Fold the candidates first and a family's survivor could be a row that
    fails a gate while a sibling passes — a wrong-cohort copy winning the fold
    and taking a qualifying one down with it. So the ranker runs first.

    But fold after `MAX_PER_FIRM` and the count is wrong in the other
    direction: the cap admits two of a firm's nine branch requisitions, the
    fold sees two, and the card announces "2 cities" about a programme open in
    nine. A fold that miscounts what it folded is the invisible filter with a
    number painted on it. So the first call is uncapped and unlimited — every
    row that CLEARS the gates, ranked — the fold runs over all of it, and the
    survivors go back through `recommend()` for the cap and the limit. Two
    calls rather than a local reimplementation of the greedy cap, because one
    page may not hold two answers to "how many picks may one firm have" (P5),
    and scoring is pure so the second pass reproduces the first pass's order
    exactly.

    `places_by_id` rides the same call, never a second guess at it: a card
    that stood for nine branch offices says so, because a fold nobody can see
    is the invisible filter this product does not ship (P4).

    Every argument is optional and defaulted to the same value the feed
    computes for itself, so a caller holding none of them (Today's ribbon)
    gets the identical column for four extra queries rather than a second
    definition of it.
    """
    from analytics.models import UserOpportunity

    if open_qs is None:
        # `select_related("firm")` IS LOAD-BEARING, not a tidy-up. Every row
        # here is asked for an `_eligibility` verdict, whose visa branch reads
        # `directory.sponsorship.effective_sponsorship` -> `opp.firm.sponsors`
        # for every posting silent on sponsorship. Unjoined, that is one
        # `SELECT ... FROM firms WHERE id = ?` per row: measured on the
        # founder's live board (2026-09-01) 1,332 of 1,866 folded rows were
        # silent, and Today ran 1,397 queries for one page load. With the join
        # the same block costs 2.
        #
        # NOT `.defer("raw")` on top of it: `_eligibility` reads `raw.facts`
        # for the graduation-window branch, so deferring the column trades
        # 1,332 firm fetches for 647 deferred-field loads and measured SLOWER
        # (531 ms against the 373 ms it was meant to fix). The row is wide on
        # purpose here.
        open_qs = Opportunity.objects.filter(status="open").select_related("firm")
        # The feed drops the student's own dismissals before it ranks; a
        # caller that did not would let a "not for me" row hold a pick slot
        # here and nowhere else, which is exactly how two surfaces start
        # disagreeing about one column.
        hidden = set(
            UserOpportunity.objects.for_user(user)
            .filter(dismissed=True).values_list("opportunity_id", flat=True)
        )
        if hidden:
            open_qs = open_qs.exclude(id__in=hidden)
    if elig_profile is None:
        elig_profile = _eligibility_profile(user)
    profile = _scoring_profile(user) if rec_profile is None else rec_profile
    if profile.is_empty:
        return [], {}
    candidates = [
        Candidate.from_opportunity(o)
        for o in fold_duplicates(
            [
                o for o in open_qs.filter(bucket__in=TARGET_BUCKETS)
                # A pick is a RECOMMENDATION, held to a higher bar than a
                # listing: a role whose own text blocks this user (wrong
                # stated year, refuses their visa) may still be worth seeing
                # on the board, but the product must not point at it and say
                # "for you" — and must certainly not write it in bulk.
                if not (lambda v: v and v["blocking"])(_eligibility(o, elig_profile))
            ],
            sticky_ids=sticky_ids,
        )[0]
    ]
    # `max(…, 1)`, never a bare `len()`: `recommend(limit=0)` is a caller
    # asking for nothing and correctly returns nothing, so an empty board
    # would skip the call entirely — and the guard that pins the feed and the
    # picks onto one clock works by watching that call happen.
    whole = max(len(candidates), 1)
    ranked = recommend(profile, candidates, today=today,
                       limit=whole, max_per_firm=whole)
    # The fold reads Opportunity rows (deadline, cohort, sponsorship,
    # location — `_competing_claims` and `_survivor_rank` want the model, not
    # the scoring candidate), so it runs over the postings behind the ranking.
    # Ranked order is preserved: `fold_families` keeps input order and the
    # survivor of a family is the copy `_survivor_rank` prefers.
    by_id = {o.id: o for o in open_qs.filter(id__in=[r.candidate.id for r in ranked])}
    ordered = [by_id[r.candidate.id] for r in ranked if r.candidate.id in by_id]

    def _family(o):
        # Bucket and cohort as well as the base title, the same triple
        # `_group_city_variants` uses: an internship and a full-time role that
        # share a name are not one job, and neither are two intakes.
        #
        # AND THE REGION, which the retired bulk-save offer's version did not
        # need and this one does. That offer folded AFTER a hard region gate,
        # so every row reaching its fold was already in a market the student
        # had named and a family could not span two. This column has no hard
        # region gate — `recommend()` charges a wrong market and lets a strong
        # row survive it — so a family CAN span markets, and then
        # `_survivor_rank` (a data-quality rule, blind to this student)
        # decides which market survives.
        #
        # Measured on the founder's column 2026-09-02 without this term:
        # Nomura runs its 2027 Global Markets programme in Hong Kong (6293)
        # and Singapore (6292), the Singapore copy won the fold, it scores
        # below the Hong Kong one because Singapore is a market he never
        # named, and a Hong Kong Global Markets internship he had been shown
        # all week fell out of the column entirely. A false fold costs a job
        # never seen — `dupes`' own rule — and this one cost the exact job the
        # student was there for.
        #
        # Keyed on the market rather than fixed by picking the best-ranked
        # member, because the market is the unit the student actually stated.
        # Within one market the members differ only by town, score
        # identically, and `_survivor_rank`'s completeness rule is the right
        # tie-break; across markets they are different decisions and the fold
        # has no business making one for them.
        fk = _family_key(o)
        return None if fk is None else (o.bucket, o.cohort, o.region, fk[0])

    distinct, places = fold_families(ordered, _family, sticky_ids=sticky_ids)
    kept = {o.id for o in distinct}
    survivors = recommend(profile, [c for c in candidates if c.id in kept],
                          today=today)
    return survivors, {r.candidate.id: places[r.candidate.id]
                       for r in survivors if r.candidate.id in places}


def pick_save_ids(user, picks, *, touched=None) -> list[int]:
    """The ids "Save all" will write: every role in `picks` this student has
    no relationship with yet.

    `picks` is whatever the column is SHOWING — the filtered list, not the
    ranked six — because the button sits in that column's header and a bulk
    write must never reach a role the student cannot see from the button. The
    column is its own peek; there is no second panel to keep in agreement
    with it, which is the whole point of folding the old banner into it.

    "Touched" is tracked OR dismissed, both. A saved role has nothing to add;
    a dismissed one the student has already answered, and "not for me"
    outranks "picked for you".

    NO ABANDONED CHECK, and that is a property of the column rather than an
    omission. The old banner needed one — a posting whose stated deadline
    passed weeks ago and which the firm never took down is one the feed
    declines to draw a Save button for, and a bulk write must not reach past
    a card's own refusal. It cannot arise here: `recommend()` skips any
    candidate whose deadline is already in the past, so a pick's deadline is
    always future or absent and `_abandoned_note` is {} for every row this
    ever sees. Re-testing it would be a second, weaker statement of a rule
    the ranker already enforces (P5).
    """
    from analytics.models import UserOpportunity

    if touched is None:
        touched = set(
            UserOpportunity.all_objects.filter(user=user)
            .values_list("opportunity_id", flat=True)
        )
    ids = [p["id"] for p in picks if p["id"] not in touched]
    # Sorted so the stashed batch is deterministic and two renders of the
    # same board stash the same list.
    return sorted(ids)


def _scoring_profile(user) -> Profile:
    """The recommender's `Profile` for this student. Two queries.

    The tier of every firm on their target list, and the warmest live
    relationship they hold at each firm — the two personal maps the scorer
    reads and this module owns the querying of (`recommend.py` never touches
    the ORM). Collapsing warmth to one rank per firm is part of the fact, not
    a caller's convenience: "chatted or advocate" is warm, "replied" is not
    yet, and archived and campaign-hidden contacts are not relationships at
    all (see `_network_fit` and `crm/campaigns.py`).

    Extracted because three callers now need the same profile — the feed's
    Picked column, the role drawer's "why this" panel, and the bulk-save
    offer's fit gate — and the second and third would otherwise each restate
    those two reads. One definition per fact (P5): a warmth rule that drifted
    between them would make the drawer justify a pick on evidence the board
    does not have.

    The feed builds this inline instead, because it already holds the tier
    map for the rest of the page and passes the result down; that is the same
    profile by construction, not a fourth copy of the rule.
    """
    tier_by_firm = dict(
        UserFirm.objects.for_user(user).values_list("firm_id", "tier")
    )
    warm_by_firm: dict[int, str] = {}
    for fid, warmth in (Contact.objects.for_user(user)
                        .filter(archived=False, firm__isnull=False,
                                warmth__in=("replied", "chatted", "advocate"))
                        .exclude(id__in=crm_campaigns.excluded_contact_ids(user))
                        .values_list("firm_id", "warmth")):
        rank = "warm" if warmth in ("chatted", "advocate") else "replied"
        if warm_by_firm.get(fid) != "warm":
            warm_by_firm[fid] = rank
    return Profile.from_user(user, tier_by_firm, warm_firms=warm_by_firm)


def _drawer_pick_why(user, opp) -> list:
    """The scorer's own reasons for THIS role and THIS student, or [].

    Two queries for a signed-in reader (the tier map and the warmest contact
    per firm — see `_scoring_profile`), none for anyone else. They are the
    same two the Opportunities page runs once for the whole board; here they
    run for one role, on a panel the student opened deliberately, which is
    the cheapest place in the product to ask a personal question.

    `Reason` objects, not a joined string: the template prints the short text
    and hangs the full sentence on `title`, exactly as the card does. Joining
    here would hand the drawer a display decision the card makes differently.

    [] whenever the student has stated nothing (`Profile.is_empty`) or the
    role scores under the bar. Both are the same answer — the product has no
    recommendation to justify — and neither is an error state.
    """
    if not user.is_authenticated:
        return []
    profile = _scoring_profile(user)
    if profile.is_empty:
        return []
    score, reasons = score_candidate(profile, Candidate.from_opportunity(opp))
    return list(reasons) if score >= MIN_SCORE else []


def role_description(request, pk):
    """One posting's own description, read from the copy we already hold.

    Every role card links out to a Workday page that renders client-side and
    takes seconds to paint, for text `enrich_postings` has already fetched and
    stored. This serves that text back immediately.

    It never invents the absence: a role we never fetched says so and offers
    the link, rather than rendering an empty drawer that reads as breakage.
    """
    opp = get_object_or_404(Opportunity.objects.select_related("firm"), pk=pk)
    raw = opp.raw or {}
    facts = raw.get("facts") or {}
    today = timezone.localdate()
    # One role, one firm, one cheap read — and nothing at all for a signed-out
    # reader (this view is deliberately not `login_required`; the posting text
    # is public, a student's contacts are not). See `_people_at_firms`.
    net = _role_people(
        opp.firm,
        _people_at_firms(
            request.user, [opp.firm_id], today=today, cap=ROLE_PEOPLE_MAX
        ).get(opp.firm_id),
    ) if request.user.is_authenticated else None
    return render(request, "directory/_role_drawer.html", {
        "o": opp,
        "firm": opp.firm,
        "net": net,
        # The drawer was the THIRD surface printing `location` raw, after the
        # feed card and the firm row. `_place` exists because two surfaces
        # disagreeing about one row's location is a defect; three is the same
        # defect with more places to notice it — the card said "Putrajaya"
        # while the drawer it opens said "PERSIARAN IRC 2, IOI RESORT CITY IOI
        # CITY TOWER ONE:PUTRAJAYA" about the identical row.
        "place": _place(opp),
        "bucket_label": BUCKET_LABELS.get(opp.bucket, opp.bucket),
        "blocks": paragraphs(raw.get("detail_text")),
        "fetched": bool(raw.get("detail_text")),
        "deadline": deadline_marker(opp.deadline, opp.deadline_precision, today=today),
        "reported": deadline_provenance(opp),
        # The drawer's closing line claimed the text was current "when we last
        # checked it" and then declined to say when that was — the one honesty
        # sentence on the panel with no number in it.
        "checked_ago": timesince(opp.last_checked, depth=1) if opp.last_checked else "",
        # {} on a clean confirmation; a label+why when our last check of this
        # URL could not reconfirm it, so the apply link doesn't render with
        # unqualified confidence — see `_unconfirmed_note`.
        "unconfirmed": _unconfirmed_note(opp),
        "facts": [{"label": label, **facts[kind]}
                  for kind, label in _FACT_LABELS if kind in facts],
        # THE SPONSORSHIP LINE, which the card has had all along and the
        # drawer never did. The drawer is where the student DECIDES (this
        # file's own opening comment), and it was the one surface that could
        # not answer the question most able to end a decision outright.
        #
        # `effective_sponsorship`, not `o.sponsorship`: the same resolver the
        # card reads, which prefers the posting's own words and falls back to
        # the firm's per-region policy only where the posting is silent — and
        # says WHICH in the label, because a firm policy is a weaker claim
        # about one role than the posting's own sentence.
        #
        # NOT A BADGE AND NOT A BOOLEAN. The stated claims are four
        # incommensurable kinds and Barclays appends a right-to-work
        # disclosure to every posting including ones that also say it will
        # sponsor (`research-eligibility-language.md §6`, Grade A), so what
        # renders is the extracted sentence with its label. Where the posting
        # says nothing and no firm policy is on file, the drawer says so
        # rather than leaving the reader to assume either answer.
        "sponsorship": _drawer_sponsorship(opp),
        # THE PERSONAL VERDICT, the same one the card carries. `None` for a
        # signed-out reader and for a student who has stated nothing — a
        # verdict needs both sides to have spoken (see `_eligibility`).
        "verdict": _eligibility(opp, _eligibility_profile(request.user)),
        # WHY COVERAGE RATES THIS ONE. The card's Picked column prints
        # `pick_why`; the drawer that card opens printed nothing, so the
        # student who clicked BECAUSE the product said "this one" arrived at
        # the panel where they decide with the recommendation's reasoning
        # left behind on the card.
        #
        # Recomputed rather than passed in. The card could send the string it
        # already holds, and that would be cheaper — but it would also mean
        # this panel renders whatever the request says it should, and the two
        # copies could drift the moment a reason's wording changes. This is
        # `score_candidate`, the same pure function the ranker runs, over
        # this one role.
        #
        # NOT "THIS IS A PICK". Whether a role IS in the top six requires
        # ranking the whole board, which is not a thing to do inside a
        # single-role fetch. The bar it uses instead is `MIN_SCORE`, which is
        # exactly the bar the ranker applies before ordering anything — so a
        # row that shows reasons here is a row that could be a pick, and a
        # row below the bar shows none rather than a weak justification for
        # something the product is not recommending.
        "pick_why": _drawer_pick_why(request.user, opp),
    })


# The session key `track_eligible` stashes its batch under, so a redirect to
# My Applications can offer an "Undo" that removes exactly the rows THIS
# bulk save created — never a hand-saved row, and never an earlier batch's
# rows once the student has looked at them (see `_my_applications_context`,
# which pops this the one time it renders). Session-backed rather than a
# DB column: the batch is only ever meant to be undoable in the moment right
# after the write, not queried or audited later, and `product_events` (via
# `record_event` below) already carries the durable count for that.
BULK_SAVE_SESSION_KEY = "bulk_save_batch"

#: The ids the Picked column's "Save all" is currently OFFERING — written by
#: `opportunities` on every render, read by `track_eligible` as the exact set
#: to write. The point of the stash is that the number in the header, the
#: number in the confirm sentence and the rows the confirm creates are one
#: fact, resolved once, rather than two queries that answered slightly
#: different questions (they did: see `track_eligible`). Session-backed for
#: the same reason the undo batch above is — it is meaningful only between the
#: render and the click that follows it, and nothing later ever needs to query
#: it.
PICK_SAVE_OFFER_SESSION_KEY = "pick_save_offer"


@login_required
@require_POST
def track_eligible(request):
    """Save every role in the Picked for you column that isn't saved yet.

    WHAT "ALL" MEANS, and the measurement that decided it. Until 2026-09-02
    this wrote a different list from a different question: every open role
    whose own text NAMED the student's class year, unranked, uncapped, offered
    in a blue banner above the board. That list and the Picked column were
    genuinely different — measured on the founder's board that morning, 2 of
    the 9 offered roles were among his 6 picks — and the founder's call was to
    keep one surface: "merge the two into the pick for you widget".

    The column won, and the year gate went with the banner, because in the
    column that gate is not doing the work it was written for. A pick can only
    ever be year_ok or year-SILENT: a stated wrong year is `blocking` (see
    `_eligibility`), and `recommend()` refuses blocked candidates outright, so
    a posting that named a cohort this student is not in cannot be in the
    column to be saved. All the gate could still decide is whether SILENCE
    disqualifies — and this product's rule, stated in `_eligibility`'s own
    contract, is that a posting that does not state its window gets no verdict
    in either direction.

    Measured on the founder's column the same day (user 6, class 2029, tracks
    ib+st, regions hk+us): 6 picks, all unsaved, 2 stating a year (Jefferies'
    2027 Treasury Summer Analyst, Houlihan Lokey's Summer 2027 Research
    Intern) and 4 silent on it — Nomura Global Markets Hong Kong, Nomura
    Investment Banking Hong Kong, HSBC Investment Banking Hong Kong, HSBC
    Markets Sales and Trading Hong Kong. Those four are the dead centre of
    what he recruits for. Keeping the gate would have put a "Save all" under a
    heading of six roles and written two of them.

    The gate was earning its place on the BANNER because the banner had no fit
    test at all: 56 roles, of which 48 named a function he does not recruit
    for or sat in a market he never named. The column has the fit — see
    `picked_roles` for the six exclusions every pick has already survived,
    including the study-level rung filter applied harder than the banner's
    ever was — so the year test is no longer the only thing standing between a
    student and 56 rows of junk. And every card in the column already draws
    its own Save button: a "Save all" that skipped four of the six cards
    beneath it would be the button contradicting the column it sits in.

    Two guards, both from the customer-perspective walk that found one click
    dumping 207 roles into a 1-role pipeline with no way back:

    CONFIRM. The column header's button carries `hx-confirm`, so the styled
    dialog states the exact count before anything is issued. But that is a
    template affordance, not a guarantee — anyone can POST this endpoint
    directly. `confirmed=1` is the actual gate: without it, nothing is
    written, full stop, no matter what the client claims the count was.

    UNDO. Every id this call creates is stashed in the session (see
    `BULK_SAVE_SESSION_KEY` above) for `track_eligible_undo` to reverse.

    THE SET IS THE COLUMN'S, NOT THIS VIEW'S. It used to re-derive its own
    from `Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)`
    — the whole table — while the banner counted the FEED's materialised rows,
    which are folded for duplicates and stripped of the user's hidden rows.
    One page load therefore produced three numbers for one action: the confirm
    said 206, the write made 209, and My Applications' tile then read 208. So
    this reads `PICK_SAVE_OFFER_SESSION_KEY`, the exact ids the column counted
    when it rendered.

    The per-row checks below still run over that set, and can only ever REMOVE
    from it: a role dismissed in another tab, or closed by a scrape, between
    the render and the click must not be saved just because it was offered a
    moment ago. Saving FEWER than the confirm said is a fact about the last
    thirty seconds; saving MORE is the product doing something the student
    never agreed to.
    """
    from analytics.models import UserOpportunity

    if request.POST.get("confirmed") != "1":
        return HttpResponseBadRequest("confirmation required")

    offered = request.session.get(PICK_SAVE_OFFER_SESSION_KEY) or []
    if not offered:
        # Nothing was offered on this session's last look at the feed, so
        # there is no number this call could honour. Same posture as the
        # confirm gate above: refuse rather than fall back to a set the
        # student was never shown.
        return HttpResponseBadRequest("no save-all offer to confirm")

    profile = _eligibility_profile(request.user)
    touched = dict(
        UserOpportunity.all_objects.filter(user=request.user)
        .values_list("opportunity_id", "dismissed")
    )
    saved_ids: list[int] = []
    for o in Opportunity.objects.filter(
            id__in=offered, status="open", bucket__in=TARGET_BUCKETS):
        # The COLUMN's own gate, re-applied — not the retired year test. A
        # posting that started blocking this student between the render and
        # the click (a scrape that filled in its graduation window, a
        # sponsorship answer changed in Settings) is one `recommend()` would
        # no longer rank, so it is one this must no longer write.
        v = _eligibility(o, profile)
        if v and v["blocking"]:
            continue
        if o.id in touched:
            continue
        # get_or_create, not create: `touched` was read once, above, before
        # this loop started, so a concurrent write for the same (user,
        # opportunity) pair -- a double-click, two tabs open on the same
        # offer -- can land in the gap between that read and this line.
        # UserOpportunity enforces uniqueness on exactly that pair, so a
        # bare `.create()` there raises IntegrityError and 500s the whole
        # confirm; `track_opportunity`'s own upsert already takes this same
        # defence for the identical race.
        UserOpportunity.all_objects.get_or_create(user=request.user, opportunity=o)
        saved_ids.append(o.id)
    saved = len(saved_ids)
    # The offer is consumed either way: a second POST of the same confirm
    # (a double-click, a back-then-resubmit) must not re-run against a batch
    # the student has already acted on.
    request.session.pop(PICK_SAVE_OFFER_SESSION_KEY, None)
    if saved:
        record_event("eligible_bulk_saved", user=request.user, count=saved)
        # Overwrites any earlier, presumably-already-seen batch — only the
        # most recent bulk save is ever offered an undo.
        request.session[BULK_SAVE_SESSION_KEY] = {"ids": saved_ids, "count": saved}
    from django.contrib import messages
    from django.shortcuts import resolve_url

    messages.success(
        request,
        (f"Saved {saved} picked role." if saved == 1
         else f"Saved {saved} picked roles.")
        if saved else "Nothing new to save: every role picked for you is already tracked.")
    from django.shortcuts import redirect

    dest = resolve_url("my_applications" if saved else "opportunities")
    # htmx's own redirect header, because the button is an `hx-post` (that is
    # what makes `hx-confirm` fire the site's styled dialog). A 302 body would
    # be followed by the XHR and swapped into the page; `HX-Redirect` makes
    # the browser navigate, so the student lands on My Applications with the
    # flash message and the Undo strip, exactly as a form submit did.
    if request.headers.get("HX-Request"):
        return HttpResponse(status=204, headers={"HX-Redirect": dest})
    return redirect(dest)


@login_required
@require_POST
def track_eligible_undo(request):
    """Reverse the most recent `track_eligible` bulk save, and only that.

    Scoped to the exact ids that call created (via the session batch) AND to
    rows still sitting untouched in Saved — `applied_status` blank or the
    legacy literal "saved", never dismissed. A role the student has since
    moved to Applied/Interviewing/Offer/Done, or dismissed, is real progress
    or a real decision and undo must never eat it, even if it happened to be
    one of the ids this batch created.
    """
    from django.db.models import Q

    from analytics.models import UserOpportunity

    batch = request.session.get(BULK_SAVE_SESSION_KEY)
    ids = (batch or {}).get("ids") or []
    if not ids:
        return HttpResponseBadRequest("nothing to undo")

    removed, _ = (
        UserOpportunity.objects.for_user(request.user)
        .filter(Q(applied_status="") | Q(applied_status="saved"),
                opportunity_id__in=ids, dismissed=False)
        .delete()
    )
    request.session.pop(BULK_SAVE_SESSION_KEY, None)
    record_event("eligible_bulk_undone", user=request.user, count=removed)
    from django.contrib import messages

    messages.success(
        request,
        f"Undid the bulk save. Removed {removed} role{'' if removed == 1 else 's'}."
        if removed else
        "Nothing left to undo — those roles already moved on.")
    from django.shortcuts import redirect

    return redirect("my_applications")


@login_required
@require_POST
def clear_saved(request):
    """Bulk-remove every role sitting in Saved — the other side of the
    one-click bulk save. Applied/Interviewing/Offer/Done rows are never
    touched: this only ever empties the one stage a mis-click (or a bulk
    save) can flood, never real funnel progress.

    Confirm-gated the same way `track_eligible` is (see there): the
    template's own `<details>` states the count before the real button
    renders, and `confirmed=1` is the actual server-side gate a plain POST
    can't skip. Tenant-scoped through `.for_user`, so this can only ever
    touch the signed-in user's own rows.
    """
    from django.db.models import Q

    from analytics.models import UserOpportunity

    if request.POST.get("confirmed") != "1":
        return HttpResponseBadRequest("confirmation required")

    removed, _ = (
        UserOpportunity.objects.for_user(request.user)
        .filter(Q(applied_status="") | Q(applied_status="saved"), dismissed=False)
        .delete()
    )
    # A batch mid-undo-window that just got cleared has nothing left to undo.
    request.session.pop(BULK_SAVE_SESSION_KEY, None)
    record_event("saved_bulk_cleared", user=request.user, count=removed)
    from django.contrib import messages

    messages.success(
        request,
        f"Cleared {removed} saved role{'' if removed == 1 else 's'}."
        if removed else "Nothing in Saved to clear.")
    from django.shortcuts import redirect

    return redirect("my_applications")


#: Fields a feed POST carries that describe the ACTION rather than the board.
#: Everything else in the body arrived via the dismiss control's
#: `hx-include=".filters"` — the live filter bar, verbatim — and is exactly
#: what the re-render needs as its querystring.
_ACTION_FIELDS = ("csrfmiddlewaretoken", "status", "from", "next", "show_firm")


def _refresh_feed(request, *, pick_only=False):
    """Re-render the Opportunities feed (or just its Picked column) after a
    write that changed what the board is allowed to offer.

    WHY RE-RENDER RATHER THAN PATCH THE NUMBER. Dismissing a role changes
    three things at once: which roles the Picked column holds, the "Save all"
    count in its header, and the id list stashed in the session for
    `track_eligible` to write. Decrementing the visible number and leaving the
    stash alone is the 206/209/208 bug (see `track_eligible`) reintroduced
    from the other end — the screen and the write disagreeing, only now the
    screen is the one that is wrong. Running the real view is what keeps all
    three one fact, because the real view is where that fact is computed.

    THE FILTERS RIDE IN THE POST. The dismiss controls carry
    `hx-include=".filters"`, so the live filter bar is in `request.POST` and
    the board this rebuilds is the board the student is actually looking at —
    not the unfiltered default, which would count roles their filters had
    already excluded. Everything that is not a filter is dropped by name
    above; a stray field would land in the querystring and be ignored by
    `_apply_filters` anyway, but it would also travel into the "show me the
    hidden ones" links this render builds out of the request.
    """
    filters = request.POST.copy()
    for field in _ACTION_FIELDS:
        filters.pop(field, None)
    request.GET = filters
    return opportunities(request, pick_only=pick_only)


def _dismiss_undo_offer(opp):
    """The one-shot "Not for me / Undo" stub a dismissal comes back with.

    IN THE RESPONSE, NOT IN THE SESSION, for the same reason
    `crm.views._dismiss_undo_offer` chose that: the offer is handed back in
    the very swap that removed the role, so it can ride in the markup and get
    the right lifetime for free. Any later render of this page — a filter
    keystroke, a reload — draws no stub. Nothing is stored, so nothing goes
    stale.

    It carried a `source` until 2026-09-02, read by the bulk-save banner's
    peek panel to stay open across the swap. That panel is gone — the Picked
    column is the list a bulk save reads from now, and it has no open/shut
    state to preserve — so there is one caller and one shape.
    """
    return {"id": opp.id, "firm_name": opp.firm.name, "title": opp.title}


def _one_rolecard(request, opp, *, show_firm=False):
    """One feed card, rebuilt on its own — the undo half of a card dismissal.

    Uses `_urgency_item`, the same builder the feed's own loop uses, so the
    card that comes back is the card that left rather than a second, thinner
    rendering of the same role. `track_status` is None by construction: undo
    deletes the row outright (see `track_opportunity`'s `undismiss` branch),
    so the card returns untracked, which is what it was before the click.

    `show_firm` travels from the dismissed card's own markup rather than being
    re-derived: it is true only for the copy pinned in the Picked column, and
    the server has no way to know which of a role's two possible copies was
    the one clicked.
    """
    item = _urgency_item(
        opp, now=timezone.now(), today=timezone.localdate(),
        my_firm_ids=set(
            UserFirm.objects.for_user(request.user).values_list("firm_id", flat=True)
        ),
        profile=_eligibility_profile(request.user),
        # One firm, so the aggregate this rebuild pays for is a single
        # indexed group — and passing it is what keeps "the card that comes
        # back is the card that left" true of the elapsed-openness fact too.
        cutoffs=onboarding_cutoffs([opp.firm_id]),
    )
    item["track_status"] = None
    item["show_firm"] = show_firm
    return render_to_string("directory/_rolecard.html", {"r": item}, request=request)


@login_required
@require_POST
def track_opportunity(request, pk):
    """Set (or clear) the current user's track status for one opportunity.
    `status=clear` removes the row; any of _TRACK_STATES upserts it. Returns
    the re-rendered control for an htmx swap."""
    from analytics.models import UserOpportunity
    from django.shortcuts import resolve_url

    opp = get_object_or_404(Opportunity, pk=pk)
    status = (request.POST.get("status") or "saved").strip().lower()

    if status == "clear":
        UserOpportunity.objects.for_user(request.user).filter(opportunity=opp).delete()
        record_event("opportunity_untracked", user=request.user)
    elif status == "dismiss":
        # "Not for me." Save had no opposite, so a student scrolling 894 roles
        # could act on a row or scroll past it forever — the feed never
        # shrank, and deciding against something left no trace. `dismissed`
        # was modelled for exactly this and nothing ever set it.
        uo, _ = UserOpportunity.all_objects.get_or_create(
            user=request.user, opportunity=opp
        )
        uo.dismissed = True
        uo.applied_status = ""
        uo.save(update_fields=["dismissed", "applied_status"])
        record_event("opportunity_dismissed", user=request.user)
    elif status == "undismiss":
        # Reversible by construction: hiding is a judgement, and judgements
        # about a cycle you have not started change.
        UserOpportunity.objects.for_user(request.user).filter(opportunity=opp).delete()
        record_event("opportunity_undismissed", user=request.user)
    elif status not in _TRACK_STATES:
        return HttpResponseBadRequest("unknown status")
    else:
        uo, _ = UserOpportunity.all_objects.get_or_create(
            user=request.user, opportunity=opp
        )
        uo.applied_status = "" if status == "saved" else status
        uo.dismissed = False
        # Stamp applied_at the first time the role enters the funnel.
        if status in _FUNNEL_STATES and uo.applied_at is None:
            uo.applied_at = timezone.now()
        uo.save(update_fields=["applied_status", "applied_at", "dismissed"])
        record_event("opportunity_tracked", user=request.user, status=status)

    # Five callers, five response shapes:
    #  - the feed swaps just the one card's control;
    #  - a "Not for me" on a card swaps that card for its own Undo stub, and
    #    brings the scope block back out of band (`from=card`);
    #  - a "Not for me" inside the bulk-save peek re-renders the whole results
    #    body (`from=peek`), because the peek lives at the top of the page
    #    where there is no scroll position to lose and every count on the
    #    board just moved;
    #  - My Applications' own forms swap the whole funnel+lenses+stages
    #    partial, because a status change MOVES a row between sections
    #    rather than just changing it in place (see `_apps_body.html`'s own
    #    docstring) — distinguished from the feed by the `next` field only
    #    those forms send, not by guessing from the target;
    #  - anyone with JS off (or any other same-site caller) gets the
    #    original full-page redirect.
    is_my_applications = request.POST.get("next", "").rstrip("/").endswith(
        resolve_url("my_applications").rstrip("/")
    )
    origin = request.POST.get("from", "")
    undone = status in ("dismiss", "undismiss")
    if request.headers.get("HX-Request"):
        if is_my_applications:
            return render(request, "directory/_apps_body.html", _my_applications_context(request))
        # `from=peek` is gone with the bulk-save banner's peek panel: the
        # Picked column is now the only list a bulk save reads from, and a
        # "Not for me" inside it is an ordinary card dismissal, handled below.
        if origin == "card" and undone:
            # TWO fragments in one response. The card's own target is
            # `closest .rolecard`, so the first replaces the card in place —
            # with the Undo stub on a dismissal, with the real card again on
            # an undo. The second is an out-of-band swap of the PICKED COLUMN,
            # which sits at the head of the grid and holds the one thing this
            # click may have changed and cannot see: whether the role was a
            # pick, and what "Save all" would now write. Patching only the
            # card would leave that header promising a number the confirm can
            # no longer honour, and would leave a dismissed role sitting in a
            # column headed "Picked for you".
            #
            # The undo lives ON the stub rather than in that column on
            # purpose: a control at the top of the page is invisible to
            # someone who just clicked a card 600 rows down, and an undo you
            # cannot see is not an undo.
            show_firm = request.POST.get("show_firm") == "1"
            card = (
                render_to_string(
                    "directory/_rolecard_dismissed.html",
                    {"r": _dismiss_undo_offer(opp), "show_firm": show_firm},
                    request=request,
                )
                if status == "dismiss"
                else _one_rolecard(request, opp, show_firm=show_firm)
            )
            col = _refresh_feed(request, pick_only=True)
            return HttpResponse(card + col.content.decode(col.charset))
        if status == "dismiss":
            # No `from` — an older card, or any other caller. The card's own
            # target is `closest .rolecard`, so an empty body removes the row.
            # Anything else here would leave a control behind on a card the
            # user just said was not for them.
            return HttpResponse("")
        return _track_control(request, opp)
    from django.shortcuts import redirect
    from django.utils.http import url_has_allowed_host_and_scheme

    # Only allow a same-site `next`; never bounce to an attacker-supplied host.
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(nxt)
    return redirect(resolve_url("my_applications"))


def _urgency_band(days_left):
    """Bucket a countdown into the three bands My Applications styles.

    Returned as a (key, label) pair so the row can carry a WORD as well as a
    colour: "now" rows are red *and* say "act now", which is what keeps the
    urgency legible to a student who cannot separate the red from the amber
    (ux: color-not-only, severity High)."""
    if days_left is None:
        return {"key": "none", "label": ""}
    if days_left <= 2:
        return {"key": "now", "label": "act now"}
    if days_left <= 7:
        return {"key": "soon", "label": "this week"}
    return {"key": "later", "label": ""}


def _posting_closed_note(uo) -> dict | None:
    """How one tracked row says "the firm took this posting down", or None
    when it hasn't been taken down.

    WHICH "CLOSED" THIS IS. `Opportunity.status == "closed"` — the nightly
    reverify pass observing the posting is gone. NOT `applied_status ==
    TRACK_CLOSED` (the "Done" stage above), which is the student's own
    marking. Every surface that read a student's tracked roles checked only
    the second, so a role the firm killed last night rendered here exactly
    like a live one.

    SUBMITTED AND SAVED ARE NOT THE SAME NEWS, so they don't get the same
    sentence. A posting closing after you applied is the expected next thing
    that happens to it; your application is untouched, and telling that
    student "no longer accepting applications" invents a loss where there
    isn't one. A posting closing while it was only ever saved is a door
    shutting on something they never got through. One fact, two meanings, and
    the copy is the only place that difference can live — the row is marked
    either way, and neither one is hidden or deleted.

    The wording tracks `templates/directory/_role_drawer.html`'s closed
    caution deliberately: that is the same sentence about the same fact, one
    click away on this same page, and two phrasings would read as two
    different findings.
    """
    o = uo.opportunity
    if not is_posting_closed(o):
        return None
    submitted = (uo.applied_status or "saved") in _FUNNEL_STATES
    # `depth=1`, matching the drawer this sentence tracks (see the docstring
    # above) and `checked_ago` a few call sites over — `timesince`'s default
    # `depth=2` renders two units ("1 hour, 38 minutes ago"), noise in a
    # sentence meant to be read at a glance rather than studied.
    when = f", confirmed {timesince(o.closed_at, depth=1)} ago" if o.closed_at else ""
    tail = ("Your application still stands." if submitted
            else "It's no longer accepting applications.")
    return {
        "submitted": submitted,
        "label": "Closed",
        "note": f"This posting is closed{when}. {tail}",
    }


def _lens_item(uo, *, today):
    """One row as it appears in a deadline lens (Closing Soon / Rolling).

    Carries its funnel stage with it — that label is what stops the lens from
    reading as a separate pile of roles."""
    o = uo.opportunity
    stage = uo.applied_status or "saved"
    days_left = (o.deadline - today).days if o.deadline else None
    # Same split the feed's item builder uses above (see the "Rolling is a
    # CLAIM" comment ~line 1338): "Rolling" is only earned for postings whose
    # own text states rolling review; every other undated row is neutral,
    # not "reviewed as they arrive". Without this, the lens's static note
    # asserted the earned claim about every undated row, including the ones
    # that never said it.
    rolling_facts = (((o.raw or {}).get("facts") or {}).get("rolling") or {})
    return {
        "id": o.id,
        "firm_name": o.firm.name,
        "title": o.title,
        "url": o.url,
        "location": o.location,
        # What the row PRINTS — see `_place`, the same resolver the feed
        # card and firm row use. `location` above stays raw for callers
        # (the weekly digest, deadline push alerts) that need the stated
        # string itself.
        "place": _place(o),
        "stage": stage,
        "stage_label": _STAGE_LABELS.get(stage, stage.title()),
        "deadline": deadline_marker(o.deadline, o.deadline_precision, today=today),
        "reported": deadline_provenance(o),
        "days_left": days_left,
        "urgency": _urgency_band(days_left),
        "rolling_stated": bool(rolling_facts),
        "rolling_why": rolling_facts.get("phrase", ""),
        # The same chips the feed and the firm page carry. This is the page a
        # student reads when deciding what to do THIS WEEK, and it was the one
        # surface that knew nothing about sponsorship, pay or a language wall.
        "facts": _fact_chips(o),
        "has_text": bool((o.raw or {}).get("detail_text")),
        # Set only when the SCRAPER has confirmed the posting gone. The
        # countdown above stays as-is and the template renders this instead of
        # it: "closes in 3 days" on a posting that is already down is the lie
        # this whole change exists to stop.
        "posting_closed": _posting_closed_note(uo),
    }


def _stage_net(uo, o, people_by_firm) -> dict | None:
    """The people block for one funnel card, or None to draw nothing.

    See the `net` comment in `_stage_card`: Done rows suppress the empty
    prompt but keep the block when there are real names on it — a role you
    finished at a firm where you know two people is still a relationship the
    card should not hide.
    """
    net = _role_people(o.firm, (people_by_firm or {}).get(o.firm_id))
    if net and not net["people"] and (uo.applied_status or "saved") == TRACK_CLOSED:
        return None
    return net


def _stage_card(uo, *, today, people_by_firm=None) -> dict:
    """One tracked role as the funnel sections render it.

    `people_by_firm` is the ONE grouped read `_my_applications_context` does
    for the whole page (see `_people_at_firms`); this only looks its own firm
    up in it. Passing the map rather than the user is what keeps the page at
    one query for people instead of one per card.
    """
    o = uo.opportunity
    return {
        "id": o.id,
        "opportunity_id": o.id,
        "firm_name": o.firm.name,
        # Your people at this firm — the thing that decides whether "Applied"
        # is the next move or "ping Maya first" is. See `_role_people`.
        #
        # A Done row keeps the block only when it has real names on it. The
        # EMPTY prompt ("Nobody at X yet — add someone") is a call to act on a
        # role still in play; on a terminal card it is a nag about an
        # application that is already over, repeated once per finished row.
        "net": _stage_net(uo, o, people_by_firm),
        "title": o.title,
        # The disambiguator. Firms post the same title per city with the city
        # only in `location` — the first populated-funnel walkthrough had two
        # "Quantitative Intern (Summer 2027)" cards reading as a duplicate
        # save, and the same-titled BofA forum sitting in Applied AND
        # Interviewing reading as one application in two stages at once.
        "location": o.location,
        # What the card PRINTS — see `_place`, the same resolver the feed
        # card and firm row use. `location` above stays raw.
        "place": _place(o),
        "url": o.url,
        "deadline": deadline_marker(o.deadline, o.deadline_precision, today=today),
        "reported": deadline_provenance(o),
        "facts": _fact_chips(o),
        "has_text": bool((o.raw or {}).get("detail_text")),
        # The funnel sections are the partition — every tracked row shows up
        # in exactly one of them, including rows in no lens at all. Without
        # this a scraper-closed posting was indistinguishable from a live one
        # in the one section a student is guaranteed to read.
        "posting_closed": _posting_closed_note(uo),
    }


def _tracked_rows(user):
    """The user's tracked, non-dismissed rows, folded exactly as Browse
    Openings folds them — the one partition of "what is this student
    tracking" that My Applications and the weekly digest (`crm.digest`) both
    read, so the two surfaces can never disagree about which rows exist or
    which of a folded pair survives.

    Identity duplicates (one requisition filed under two candidate-pool
    addresses, e.g. a tal.net posting listed on both `pl/1` and `pl/2`) must
    fold here exactly as they do on Browse Openings, or a student who tracked
    the same job under both addresses sees it twice and any count built on
    this list overstates their pipeline.

    `fold_duplicates` keys on `firm_id`/`title`/`location`/`deadline`, none of
    which UserOpportunity carries — every row would key to the same
    `(None, '')` bucket and the fold would discard real tracked applications
    instead of the one genuine duplicate. So it runs on the underlying
    Opportunity objects, in the same order as `rows`, and the survivors are
    mapped back to their UserOpportunity by (Python) object identity rather
    than by re-deriving anything from the folded rows.

    If a student tracked both duplicate addresses at different funnel stages
    (applied on one, still saved on the other), the stage with real progress
    is the one worth keeping — fold_duplicates' own tie-break (deadline, then
    location, then sponsorship, then first_seen, then id) knows nothing about
    funnel stage, so the progressed opportunity is marked sticky to win the
    fold."""
    from analytics.models import UserOpportunity

    rows = list(
        UserOpportunity.objects.for_user(user)
        .filter(dismissed=False)
        .select_related("opportunity", "opportunity__firm")
        .order_by("opportunity__firm__name", "opportunity__title")
    )
    opps = [uo.opportunity for uo in rows]
    progressed_ids = {
        uo.opportunity_id for uo in rows
        if (uo.applied_status or "saved") != "saved"
    }
    survivors, _folded = fold_duplicates(opps, sticky_ids=progressed_ids)
    kept = {id(o) for o in survivors}
    return [uo for uo, opp in zip(rows, opps) if id(opp) in kept]


def _my_applications_context(request):
    """The user's tracked roles: one funnel partition, plus two deadline
    lenses over the live rows.

    OVERLAP, decided deliberately. The five funnel stages are the partition —
    every tracked row is in exactly one of them, so `total` equals the sum of
    the stage counts and the page can never imply the student is tracking more
    roles than they are. Closing Soon and Rolling are *cross-sections* of those
    same rows, not additional ones: a Saved role closing on Friday is genuinely
    both, and hiding it from one of the two would break whichever section the
    student happened to be reading.

    So a role can render twice, and the page says so out loud rather than
    hoping nobody notices: the lens band is introduced as "the same N roles,
    seen by deadline", each lens count is written as "x of N", and every lens
    card wears its funnel-stage pill. The one authoritative count — the number
    a student would quote — is the funnel total in the header.

    Done rows are excluded from both lenses. A finished application has no
    deadline urgency left in it, and letting one back into Closing Soon would
    put a dead role at the top of the page.

    POSTING CLOSED is the fifth lens and the second meaning of "dead". Done is
    the student's own marking; `Opportunity.status == "closed"` is the nightly
    reverify pass watching the firm take the posting down, which no surface on
    this page ever read — so a role that died last night sat in Closing Soon
    counting down to a deadline it no longer had. Those rows are moved, never
    dropped: a tracked role is the student's own record, and deleting one they
    may have actually applied to would be a far worse bug than the one being
    fixed here. They stay in their funnel stage too, marked there as well.

    RETURNS A CONTEXT DICT, not a response — split out of `my_applications`
    so `track_opportunity` can rebuild the same context after a status
    change and re-render `directory/_apps_body.html` for an htmx swap,
    without a second, drifting copy of this query logic. `my_applications`
    itself is now a thin wrapper: call this, render the full page."""
    today = timezone.localdate()
    rows = _tracked_rows(request.user)

    # Your people at every firm on this page, in ONE query for the whole page
    # (see `_people_at_firms`) rather than one per card. A pipeline of 30
    # roles is 30 cards and, before this, would have been 30 contact reads.
    people_by_firm = _people_at_firms(
        request.user,
        {uo.opportunity.firm_id for uo in rows},
        today=today,
        cap=APPS_PEOPLE_MAX,
    )

    # setdefault so any unexpected legacy status can't KeyError the page.
    groups: dict[str, list] = {key: [] for key, _ in _STAGES}
    for uo in rows:
        groups.setdefault(uo.applied_status or "saved", []).append(uo)
    # Every stage travels, including the empty ones. The funnel rail at the top
    # of the page draws all five so the shape of the pipeline is readable at a
    # glance — a stage holding three roles gets a bar three times the one
    # holding one, and a stage holding none says "0" rather than vanishing and
    # leaving the student to wonder whether it exists. `pct` is that bar, as a
    # share of the busiest stage; it is presentation arithmetic the template
    # cannot do, which is why it is computed here.
    biggest = max((len(groups[key]) for key, _ in _STAGES), default=0)
    stages = [
        {
            "key": key,
            "label": label,
            # Prepared rows, not raw UserOpportunity objects. The template used
            # to reach through `uo.opportunity` for a firm and a title and
            # could reach nothing else, so the five funnel sections showed
            # strictly less about a role than the two deadline lenses on the
            # same page did about the same row.
            "items": [
                _stage_card(uo, today=today, people_by_firm=people_by_firm)
                for uo in groups[key]
            ],
            "count": len(groups[key]),
            "pct": round(100 * len(groups[key]) / biggest) if biggest else 0,
        }
        for key, label in _STAGES
    ]

    # The lenses read the LIVE rows only (everything that isn't Done).
    live = [uo for uo in rows if (uo.applied_status or "saved") != TRACK_CLOSED]
    # A posting the reverify pass has confirmed gone has no deadline urgency
    # left in it for anyone, so it leaves the four dated lenses and gets its
    # own below. Partitioned HERE, once, rather than tested inside each of the
    # four: the fractions printed beside every lens ("1 of 12 live") invite
    # the student to subtract, so a row that quietly vanished from all four
    # would leave a hole they can find. Five lenses, still one partition.
    shut_rows = [uo for uo in live if is_posting_closed(uo.opportunity)]
    dated = [uo for uo in live if not is_posting_closed(uo.opportunity)]
    closing = [
        _lens_item(uo, today=today) for uo in dated
        if is_closing_soon(uo.opportunity.deadline, today=today)
    ]
    closing.sort(key=lambda i: (i["days_left"], i["firm_name"].lower()))
    # Rolling is defined by the ABSENCE of a deadline, not by a far-off one: a
    # role with a real deadline three months out is dated, just not urgent, and
    # calling it rolling would be a small lie about the posting. Rolling roles
    # carry no countdown and are never styled as overdue.
    rolling = [
        _lens_item(uo, today=today) for uo in dated
        if uo.opportunity.deadline is None
    ]
    # The two lenses above leave a hole, and the fractions printed beside them
    # ("1 of 12 live", "9 of 12 live") invite the student to subtract and find
    # it. `is_closing_soon` is a two-sided window, so a dated row lands outside
    # it in BOTH directions: further out than the window, or already behind it.
    # Both get a lens, so every live row is accounted for on this band.
    window_first, window_last = closing_soon_window(today)
    later = [
        _lens_item(uo, today=today) for uo in dated
        if uo.opportunity.deadline is not None
        and uo.opportunity.deadline > window_last
    ]
    later.sort(key=lambda i: (i["days_left"], i["firm_name"].lower()))
    passed = [
        _lens_item(uo, today=today) for uo in dated
        if uo.opportunity.deadline is not None
        and uo.opportunity.deadline < window_first
    ]
    passed.sort(key=lambda i: (i["days_left"], i["firm_name"].lower()))
    # Sorted by firm, not by days_left: a closed posting's countdown is the
    # one number on the row that means nothing any more, and half of these
    # rows carry no deadline at all (a rolling role can be pulled too), so
    # `days_left` is None for them and would not sort against an int.
    shut = [_lens_item(uo, today=today) for uo in shut_rows]
    shut.sort(key=lambda i: (i["firm_name"].lower(), i["title"].lower()))

    # Read down the band and you read down the calendar: what has gone, what
    # is going, what is coming, what never had a date. `empty_state` is what
    # separates the two lenses that are worth a sentence when empty (both are
    # coaching: go find roles that close soon / everything you track is dated)
    # from the two that are just noise when empty.
    lenses = [
        # First in the band because it is the most finished thing on it: the
        # other four are degrees of "when", this one is "never again". Keyed
        # `posting_closed`, NOT `closed`, so it can never be confused at a
        # call site or in a test with the `closed` STAGE below, which is the
        # student's own Done marking and a different fact entirely.
        {
            "key": "posting_closed",
            "label": "Posting Closed",
            "items": shut,
            # Kept short on purpose — see the template's own note on why the
            # lens band cut its prose (2026 redesign: "cleaner, less words").
            # "Taken down by the firm" went with that cut: it is what the
            # heading already says, and every row below states it in full,
            # with a timestamp and a stage-aware second clause, via
            # `_posting_closed_note`. What the heading does NOT say — and no
            # row says for a role that was only ever saved — is that a pulled
            # posting is still tracked, so that is the half the note keeps.
            "note": "Still on your list.",
            "empty_state": False,
        },
        {
            "key": "passed",
            "label": "Deadline Passed",
            "items": passed,
            # No note. "The posted date has gone by" was the heading again in
            # different words, which is the one thing a note under a heading
            # must not be. The other four notes each state something their
            # heading cannot: a window, a boundary, or what happens next.
            "note": "",
            "empty_state": False,
        },
        {
            "key": "closing",
            "label": "Closing Soon",
            "items": closing,
            "note": f"Within {CLOSING_SOON_DAYS} days.",
            "empty_state": True,
        },
        {
            "key": "later",
            "label": "Further Out",
            "items": later,
            "note": f"More than {CLOSING_SOON_DAYS} days away.",
            "empty_state": False,
        },
        {
            "key": "rolling",
            # "No Deadline", not "Rolling". The key stays `rolling` (call
            # sites and tests read it) but the heading no longer does, for
            # the reason this bucket is built the way it is a few lines up:
            # it is defined by the ABSENCE of a date, and "Rolling" is a
            # CLAIM only the posting itself can make. The feed retracted the
            # blanket version of that claim (see the feed item builder's
            # `rolling` branch and test_feed_honesty.py), and the rows here
            # retracted it too — a row says "Rolling" only when its own text
            # states rolling review, and "No date posted" otherwise. The
            # heading was the last place on this surface still asserting it
            # about all of them, which also made the two row markers under it
            # read as two spellings of the heading rather than as the
            # different facts they are.
            "label": "No Deadline",
            "items": rolling,
            # No note: "No posted deadline" was the heading in other words.
            "note": "",
            "empty_state": True,
        },
    ]
    # Roles the student marked "not for me". They live here rather than in
    # the feed for the obvious reason, but they must live SOMEWHERE: a hidden
    # thing with no way back is a decision the product made permanent on the
    # user's behalf.
    from analytics.models import UserOpportunity

    hidden = list(
        UserOpportunity.objects.for_user(request.user)
        .filter(dismissed=True)
        .select_related("opportunity", "opportunity__firm")
        .order_by("opportunity__firm__name", "opportunity__title")
    )
    # The bulk-save undo offer. NOT popped here — the ids must survive this
    # very render so the Undo button it draws has something to submit to
    # (`track_eligible_undo` reads the same session key). Instead this
    # renders it once by flagging it "shown": a batch already flagged is
    # left out of the context on every later render — reload, htmx swap,
    # whatever — the same one-shot lifetime `django.contrib.messages` gives
    # every flash message on the product, without deleting the one thing
    # the Undo control on THIS page still needs. `track_eligible_undo`
    # deletes the session key outright once it actually reverses the batch.
    batch = request.session.get(BULK_SAVE_SESSION_KEY)
    if batch and not batch.get("shown"):
        recent_bulk_save = batch
        request.session[BULK_SAVE_SESSION_KEY] = {**batch, "shown": True}
    else:
        recent_bulk_save = None
    return {
        "stages": stages,
        "lenses": lenses,
        "total": len(rows),
        "live_total": len(live),
        "closing_soon_days": CLOSING_SOON_DAYS,
        "hidden": hidden,
        "recent_bulk_save": recent_bulk_save,
    }


@login_required
def my_applications(request):
    """The full My Applications page. See `_my_applications_context` for
    everything about what it shows; this is just the render."""
    return render(request, "directory/my_applications.html", _my_applications_context(request))


# How many rows one kind group prints before it hands the rest to the feed.
#
# Chosen off the measured distribution, not taste. Across the 81 firms with
# any campus roles the median is 10 and the mean 31: at 12 the median firm is
# untouched — every row still prints, no overflow link renders, no market line
# renders, the page is byte-for-byte what it was. The cap exists for the eight
# firms above 60, where the same template was building a wall (PwC 716 rows =
# 66,832px at 1280, 97,229px at 375; `?role=all` 1,496 rows = 126,639px).
# 12 rows is also about one screen at 1280, so a capped group reads as a
# sample you can take in, not a list you have to leave.
ROLE_ROWS_PER_GROUP = 12


def _role_groups(cards, *, cap=ROLE_ROWS_PER_GROUP):
    """The role rows as kind groups, each cut to `cap` rows.

    This used to be a `{% regroup %}` in the template, which could group but
    could not cap — and an uncapped group is how one firm's page became 74
    screens of scroll. Capping in the template (`|slice`) would have printed
    the rows and lost the count of what it dropped, which is the one number
    the reader needs.

    THE CAP IS PER GROUP, DELIBERATELY, not per page. `cards` arrives sorted
    campus-buckets-first, so a flat page cap would have shown 12 internships
    at PwC and silently dropped all 319 entry-level rows — the page's original
    bug (a scope nobody was told about) rebuilt inside the fix. Every kind
    that has rows keeps its heading, its true total, and a sample.

    Rows within a group arrive deadline-ascending-nulls-last, so a capped
    group leads with whatever is actually closing. Nothing is re-sorted here.
    """
    order: list[dict] = []
    by_value: dict[str, dict] = {}
    for c in cards:
        value = c["role"]["value"]
        g = by_value.get(value)
        if g is None:
            g = by_value[value] = {
                "value": value,
                "label": c["role"]["label"],
                "cards": [],
            }
            order.append(g)
        g["cards"].append(c)
    for g in order:
        g["total"] = len(g["cards"])
        g["more"] = max(g["total"] - cap, 0)
        g["cards"] = g["cards"][:cap]
    return order, any(g["more"] for g in order)


def _open_markets(opps):
    """Which markets the rows in scope sit in, counted.

    A cap hides rows, and this page's whole character is that it says what it
    is hiding (see `firm_detail`'s docstring). "385 more" says how many; it
    does not say that 136 of PwC's campus roles are in Singapore and 253 in
    Europe, which is the one thing a student who cannot work in the US needs
    from a firm page. Constant size — eight markets at the very most — so it
    costs the same line whether the firm has 20 roles or 1,496.

    Order and words come from REGION_ORDER / REGION_LABELS, the same pair the
    feed's own region facet reads, so the two surfaces cannot drift into
    calling `sg` "Singapore" on one page and "SG" on the other. `""` is
    "Unstated" in the facet's exact wording: the posting never said, which is
    a different fact from a market we do not track, and the label must not
    blur them.

    Deliberately not links. The feed is where filtering happens; a row of
    clickable market counts here would be a filter bar in disguise on the one
    page that documented its decision not to grow one.
    """
    counts = Counter(o.region or "" for o in opps)
    out = [
        {"label": REGION_LABELS[r], "n": counts[r]}
        for r in REGION_ORDER
        if counts.get(r)
    ]
    if counts.get(""):
        out.append({"label": REGION_NONE_LABEL, "n": counts[""]})
    return out


def firm_detail(request, slug):
    """A single firm's page: its open campus openings plus its cycle timeline
    (firm_dates, confirmed vs rumored).

    SCOPE. This page used to render `status="open"` with no bucket filter, and
    it was the only user-facing surface in the app that did. The result: the
    contacts board said "Barclays 13 Open", and one click later this page said
    "Open Roles 925" and spent 87% of its height on German-language and Pune
    back-office requisitions — with nothing anywhere on the page explaining
    which number was the lie. Neither number was; they were answering
    different questions, and only one of them was asked out loud.

    So firm detail now takes the same scope every other surface takes (the
    three campus buckets) and, like the feed, states that scope and offers the
    same `?role=` opt-in out of it. Experienced rows are not hidden — they are
    one click away and counted in the sentence that hides them.

    LENGTH. Scoping fixed which rows print, not how many. Measured on the live
    corpus: PwC's campus scope is 716 rows, and the page printed every one of
    them — 66,832px at 1280 and 97,229px at 375, or 74 and 120 screens. Nearly
    all of it was one section: everything above "Open Roles" (subnav, header,
    the network slice, the timeline) measured 318px on PwC and 571px on
    Goldman, so the role list WAS the page, 99% of its height.

    And it was the page twice. `/opportunities/?firm=pwc` renders those exact
    716 rows in 1,194px, inside the firm column's own scroll window, with a
    filter bar, region and year facets, search, save stars and ranking. This
    page was a second, 56x taller copy of a surface built for the volume.

    So the role list stops trying to be that surface. Each kind group prints
    at most ROLE_ROWS_PER_GROUP rows and then hands the rest to the feed,
    filtered to this firm and this kind. On the median firm (10 campus roles)
    no group reaches the cap, so nothing changes: no overflow link, no market
    line, the same flat list. What the cap hides, the page states — the count
    per group, and `_open_markets` for the shape of the rest.

    WHAT THIS PAGE IS FOR, once the appendix is an appendix: the firm's
    identity, its cycle dates, and `_my_network_at` — the only section on it
    that differs between two students. Those are constant-size and they were
    already on top; they now read as the page rather than as a preamble to a
    wall.
    """
    firm = get_object_or_404(Firm, slug=slug)
    now = timezone.now()
    today = timezone.localdate()

    # Firm detail's own `?role=` vocabulary: the campus scope (the default)
    # plus the two opt-ins the feed already names. A bucket-specific role is
    # deliberately NOT offered here — this page carries one scope line, not a
    # filter bar, and a `?role=internship` deep link would leave that line
    # describing a scope the rows do not have. Anything unrecognised falls
    # back to campus, the same posture as `_effective_role`.
    role = request.GET.get("role", "")
    if role not in ROLE_OPTIN:
        role = ""

    open_qs = firm.opportunities.filter(status="open")

    # Campus buckets first (insight, internship, entry_level), experienced
    # rows after, so the opt-in views still lead with the roles the product
    # is for. The fold runs over the FULL open set, before the role filter,
    # so the scope sentence's campus/other counts describe the same folded
    # universe the cards are drawn from — counting the raw queryset instead
    # put "912" in the sentence above a section headed "Experienced 790",
    # the two numbers separated only by which side of fold_duplicates()
    # they were computed on (round 10 recheck, Barclays).
    all_open = list(
        open_qs.select_related("firm").order_by(
            _BUCKET_ORDER, F("deadline").asc(nulls_last=True), "title"
        )
    )
    # Same identity-duplicate fold as Browse Openings and My Applications
    # (see directory.dupes.fold_duplicates): one firm posts the same role
    # under two candidate-pool req numbers, and without this the firm page
    # showed byte-identical cards twice and its own "Open Roles" count
    # disagreed with the feed's count for the same firm.
    all_open, _folded = fold_duplicates(all_open)
    campus_total = sum(1 for o in all_open if o.bucket in TARGET_BUCKETS)
    other_total = len(all_open) - campus_total
    # Mirror _apply_role_filter's vocabulary over the folded list: "" (and
    # anything unrecognised, already normalised above) is the campus scope,
    # OTHER includes pre-classifier "" buckets, "all" is everything.
    if role == "all":
        opps = all_open
    elif role == OTHER:
        opps = [o for o in all_open if o.bucket in (OTHER, "")]
    else:
        opps = [o for o in all_open if o.bucket in TARGET_BUCKETS]
    cards = [_card(o, now=now, today=today) for o in opps]
    role_groups, capped = _role_groups(cards)
    context = {
        "firm": firm,
        "cards": cards,
        # The rows the page actually prints, already grouped and capped. The
        # template no longer regroups: see `_role_groups`.
        "role_groups": role_groups,
        # True when ANY group had to drop rows. The market line hangs off this
        # one flag (the per-group overflow link hangs off that group's own
        # `more`), so a firm small enough to print in full grows neither.
        "capped": capped,
        # Markets of the rows IN SCOPE, not of the firm: on `?role=other` this
        # describes the experienced rows the page is then showing, which is
        # the set the reader is looking at.
        "markets": _open_markets(opps) if capped else [],
        # `user=` so the timeline can mark the rows belonging to the cycle the
        # student actually stated. Signed-out passes an anonymous user and
        # gets no marker, which is the honest answer: there is no stated
        # cycle to match against.
        "timeline": _timeline(firm, today=today, user=request.user),
        # Measured, not asserted (see `_cycle_observed`) — a firm can have
        # this and nothing in `timeline`, or vice versa; the two sections
        # answer different questions and neither implies the other.
        "cycle_observed": _cycle_observed(firm),
        # Still the full in-scope count, never the printed count. The heading
        # answers "how many are open here", and every other surface (contacts
        # board, feed) answers it with this same number — the 925-vs-13 bug in
        # the docstring above is what a page-local count looks like.
        "total": len(cards),
        "role": role,
        "campus_total": campus_total,
        "other_total": other_total,
        # P9, made visible on the one page where its absence is a lie. This
        # firm HAS a campus board, Coverage knows its address, and Coverage
        # does not read it because the tenant's own robots.txt says not to
        # (D-20). Without this note the page shows a firm's experienced reqs
        # and no internship, which reads as "the programme is not running".
        # The note names the reason and hands over the link.
        "unreachable_board": UNREACHABLE_BY_POLICY.get(firm.slug),
        **_my_network_at(request.user, firm, today=today),
    }
    return render(request, "directory/firm_detail.html", context)


# Warmth, strongest first — the order the roster and the "best" pick use.
_WARMTH_RANK = {"advocate": 0, "chatted": 1, "replied": 2, "cold": 3}


def _my_network_at(user, firm, *, today) -> dict:
    """The signed-in user's own relationship slice for this firm.

    The product's whole pitch is "everyone tracks deadlines, nobody tracks the
    relationship" — and this page, the one place a firm's deadlines and a
    student's people could meet, showed only the deadlines. It offered "Add a
    Contact Here" while the contacts already there were invisible, so the page
    argued for the product's thesis and then declined to demonstrate it.

    Signed-out gets nothing, deliberately: there is no relationship to show,
    and inventing an empty shell would be noise on a page a visitor reads as
    a firm profile.
    """
    if not user.is_authenticated:
        return {"my_contacts": [], "my_total": 0, "my_advocates": 0, "my_next": None}

    # Campaign-hidden people are out, same as the Network board
    # (`crm.views.contact_list`). This block answers "who do I know here",
    # which is the identical claim the board makes about the identical
    # people — a firm page that says "4 contacts, warmest here: Ayda Yang"
    # after the student answered that Ayda arrived on his club panel blast is
    # the board's bug relocated one click to the right. `my_total` and
    # `my_advocates` are counted off this same list, so they move with it.
    rows = list(
        Contact.objects.for_user(user)
        .filter(firm=firm, archived=False)
        .exclude(id__in=crm_campaigns.excluded_contact_ids(user))
        .annotate(last_ts=Max("touches__ts"))
        .order_by("name")
    )
    rows.sort(key=lambda c: (_WARMTH_RANK.get(c.warmth, 9), c.name.lower()))

    people = [
        {
            "c": c,
            "days_since": (today - timezone.localtime(c.last_ts).date()).days
            if c.last_ts else None,
        }
        for c in rows
    ]
    # The one person worth opening first: warmest, and among equals the one
    # who has waited longest to hear from you. Both this pick and the list
    # below start from the SAME warmth-tier-first ordering (`rows.sort`
    # above), so whenever the top tier holds exactly one person, `my_next`
    # and `my_contacts[0]` are that same person — "Warmest here: James Bai"
    # directly above a list whose first row is James Bai again. `my_next`
    # only earns its own line when it names someone other than whoever the
    # list already puts first; otherwise the callout is a restatement, not
    # new information, and stays off the page.
    my_next = max(
        people,
        key=lambda p: (-_WARMTH_RANK.get(p["c"].warmth, 9), p["days_since"] or 0),
    )["c"] if people else None
    my_next_restates_top_row = bool(
        my_next and people and my_next.id == people[0]["c"].id
    )
    return {
        "my_contacts": people,
        "my_total": len(people),
        "my_advocates": sum(1 for c in rows if c.warmth == "advocate"),
        "my_next": my_next,
        "my_next_restates_top_row": my_next_restates_top_row,
    }


# ---------------------------------------------------------------------------
# "Your people here" — the firm-page network slice, joined onto a ROLE.
# ---------------------------------------------------------------------------
# The landing headline is "the deadline and the person behind it, one place",
# and until now the two only ever met on /firms/<slug>/ — a page a student
# reaches by deliberately navigating away from the role they were deciding
# about. The two surfaces where the decision actually happens (the feed's
# role drawer, and a saved role's card on My Applications) knew the deadline
# and knew nothing about the relationship, so the product argued its own
# thesis on a page nobody visits mid-decision.
#
# This is the same slice `_my_network_at` renders on the firm page, in the
# same vocabulary and the same component look (warmth dot, name, role, mono
# days-since, warmest first), cut down to the warmest few. The firm page
# stays the roster; these are the "before you apply, you know Maya here"
# version of it.
#
# WHAT IT DELIBERATELY DOES NOT DO: it does not compute a cadence "due
# action" and offer Compose. `cadence.due_actions` needs the user's whole
# contact set, their whole touch history and the shared firm_dates to decide
# who is due — one grouped query cannot answer it, and a cheaper local
# approximation would be a SECOND source of truth about who is due, free to
# disagree with Today. Every row links to the contact page instead, which is
# where Compose, the saved draft and the touch log already live. The two
# facts that tell a student whether to act — warmth (tier + position) and how
# long since the last touch — travel on the row itself.

# The warmth ladder in words, for the row's accessible label and its tooltip.
# The dot carries it visually and the sort order carries it structurally; a
# colour is never the only channel (ux: color-not-only).
_WARMTH_WORDS = {
    "advocate": "In your corner",
    "chatted": "You have chatted",
    "replied": "They replied",
    "cold": "No reply yet",
}

# How many names a role shows before it stops being a hint and starts being a
# roster. The drawer is a full-width panel and can hold three; a My
# Applications card is one cell of a 300px-minimum grid and can hold two
# without pushing its own stage control off the bottom.
ROLE_PEOPLE_MAX = 3
APPS_PEOPLE_MAX = 2


def _people_at_firms(user, firm_ids, *, today, cap) -> dict:
    """`{firm_id: {"people": [...], "total": n, "more": n}}` for every firm
    named, in ONE query no matter how many firms are asked about.

    My Applications lists every role a student tracks — a dozen cards across
    a dozen firms is ordinary — so the obvious per-card `Contact.objects
    .for_user(user).filter(firm=...)` would be a dozen queries that grow with
    the pipeline. One `firm_id__in` read plus a Python group-by is flat: the
    page costs the same one query at 1 card as at 50. `assertNumQueries` in
    `directory/tests/test_role_people.py` pins that so it cannot regress.

    Tenancy: `Contact` is private-zone, so this goes through `.for_user`
    (coverage_web/tenancy.py) — `Contact.objects` unscoped raises
    `TenantScopeError` by construction.
    """
    if not getattr(user, "is_authenticated", False):
        return {}
    firm_ids = {fid for fid in firm_ids if fid}
    if not firm_ids:
        return {}

    # Same exclusion the firm page and the Network board apply, for the same
    # reason: "Your people here" on a role card and in My Applications is a
    # claim about the student's recruiting network, and somebody he told us
    # was not part of it must not be the person a role card offers him. Costs
    # one extra `.for_user` read that returns nothing on every account that
    # has never classified a send.
    rows = list(
        Contact.objects.for_user(user)
        .filter(firm_id__in=firm_ids, archived=False)
        .exclude(id__in=crm_campaigns.excluded_contact_ids(user))
        .annotate(last_ts=Max("touches__ts"))
    )
    by_firm: dict[int, list] = {}
    for c in rows:
        by_firm.setdefault(c.firm_id, []).append(c)

    out = {}
    for fid, contacts in by_firm.items():
        # Same ordering the firm page uses (`_my_network_at`): warmth tier
        # first, name inside a tier. Whoever leads this list is the person to
        # open, which is why no separate "warmest here" callout is needed at
        # this size — at a cap of two or three the list IS the callout.
        contacts.sort(key=lambda c: (_WARMTH_RANK.get(c.warmth, 9), c.name.lower()))
        out[fid] = {
            "people": [_person_row(c, today=today) for c in contacts[:cap]],
            "total": len(contacts),
            "more": max(0, len(contacts) - cap),
        }
    return out


def _person_row(c, *, today) -> dict:
    """One contact as a role surface renders them. `last_ts` is the annotation
    `_people_at_firms` attached; reading `c.touches` here instead would be the
    N+1 this helper exists to avoid."""
    last_ts = getattr(c, "last_ts", None)
    return {
        "id": c.id,
        "name": c.name,
        "role": c.role,
        "warmth": c.warmth,
        "warmth_label": _WARMTH_WORDS.get(c.warmth, c.warmth),
        "days_since": (today - timezone.localtime(last_ts).date()).days
        if last_ts else None,
    }


def _role_people(firm, slice_) -> dict | None:
    """The block one role renders, or None when there is nothing honest to
    draw.

    `None` means "render no block at all": a role with no firm to join on.
    An EMPTY block (a firm, no contacts) is a different answer and a real
    one — it is the product's own pitch addressed to this firm by name, with
    the add-contact form pre-filled — so it returns a dict with an empty
    `people` list rather than None.
    """
    if firm is None or not getattr(firm, "id", None):
        return None
    slice_ = slice_ or {"people": [], "total": 0, "more": 0}
    return {
        "firm_name": firm.name,
        "firm_slug": firm.slug,
        **slice_,
    }
