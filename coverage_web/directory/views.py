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

from collections import Counter

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
from directory.classify import (
    BUCKET_LABELS, ENTRY_LEVEL, INSIGHT, INTERNSHIP, OTHER, REGION_LABELS,
    REGION_ORDER, TARGET_BUCKETS, derive_class_year,
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
from directory.dupes import fold_duplicates
from directory.facts import paragraphs
from directory.models import Firm, Opportunity
from directory.recommend import Candidate, Profile, parse_target_cycle, recommend
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
# read as internal shorthand; the filter is public-facing. The six
# preference-eligible tracks are classify.TRACK_LABELS — the SAME dict
# accounts/forms.py's Settings checkboxes read — so the two pages can never
# disagree about a slug's label again (they used to: Settings said "Private
# Equity", this filter said "Private Equity / Credit", both for "pe").
TRACK_LABELS = {
    **_TRACK_LABELS_BASE,
    # MLT and SEO Career, the two firms on this track, are not employers —
    # they are access programmes that place students INTO the firms above.
    # The slug had no label at all, which is why /firms/mlt/ printed the bare
    # word PIPELINE where every other firm printed a desk. Not a preference
    # option (classify.TRACKED_TRACKS excludes it) — display-only here.
    "pipeline": "Career Access Programme",
}


def _labelled(slugs, labels: dict[str, str], *, by_label: bool = False) -> list[str]:
    """Raw slugs through a label map, deduped, in that facet's own order.

    `by_label` picks WHICH order, because the two facets do not share one:
    `_region_facet` walks REGION_ORDER (hk before us), `_track_facet` sorts
    alphabetically by label. A firm's stored array order is arbitrary, so
    matching each facet is what makes the eyebrow and the filter agree.

    Falls back to the slug when the map has no entry, so a track added to
    firms.yaml before its label lands degrades to the old behaviour rather
    than vanishing — but every slug the live data holds IS mapped, and
    `test_firm_scope` fails the build if a new one is not.
    """
    unique = list(dict.fromkeys(s for s in (slugs or []) if s))
    if by_label:
        return sorted((labels.get(s, s) for s in unique), key=str.casefold)
    order = {key: i for i, key in enumerate(labels)}
    unique.sort(key=lambda s: (order.get(s, len(order)), s))
    return [labels.get(s, s) for s in unique]

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
# provider handed us the date in a structured field and 0.6 when
# `enrich_postings` read it out of the posting's own prose — and 92 of the 121
# dated open roles are the second kind. Both are worth showing; only one of
# them is a quotation of a field, and a page that renders them identically is
# claiming a certainty it does not have.
#
# "Reported" rather than "unconfirmed": the date IS what the posting says. The
# word is about the reading being ours, not about doubting the firm.
_CONFIRMED_AT = 1.0


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
# `Opportunity.deadline_precision` is a bare `CharField` with no vocabulary
# constraint and a fully editable `OpportunityAdmin` over it, so this is one
# admin save away, exactly like the `confidence=95.0` write that
# `opportunities_confidence_in_range` exists for. `FirmDate` already carries
# 25 `estimated` rows; the two columns mean the same thing.
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
    """
    if deadline is None:
        return {"posted": False, "label": "No deadline posted", "countdown": "", "past": False}
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
    """Bundle one opportunity into a template-ready card. Tags are the
    student-facing trio: firm category, stated class year, sponsorship."""
    bucket = opp.bucket or OTHER
    category = FIRM_CATEGORIES.get(opp.firm.slug) or next(
        (TRACK_LABELS.get(t, "") for t in (opp.firm.tracks or [])), ""
    )
    tags = []
    if category:
        tags.append({"label": category, "css": "tag-cat"})
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


def _cycle_not_open_note(profile, open_qs) -> str:
    """One honest sentence when NONE of the student's target cycles has any
    live postings, else "". Checked against the whole open campus board, not
    the picks: "your cycle is not open yet" must mean the BOARD lacks it,
    never that six other roles merely outscored it.

    A student can name more than one cycle now (see `Profile.target_cycles`),
    so this only fires when EVERY parseable one is closed — the moment any
    one of them has live postings, there's nothing to warn about."""
    parsed = [
        (raw.strip(), parse_target_cycle(raw))
        for raw in getattr(profile, "target_cycles", None) or []
    ]
    parsed = [(label, cycle) for label, cycle in parsed if cycle is not None]
    if not parsed:
        return ""
    closed_labels = [
        label for label, (bucket, year) in parsed
        if not open_qs.filter(bucket=bucket, cohort=str(year)).exists()
    ]
    if len(closed_labels) < len(parsed):
        return ""
    if len(closed_labels) == 1:
        names = closed_labels[0]
    elif len(closed_labels) == 2:
        names = f"{closed_labels[0]} and {closed_labels[1]}"
    else:
        names = ", ".join(closed_labels[:-1]) + f", and {closed_labels[-1]}"
    # No em dash: house copy style. Two short sentences read cleaner here
    # anyway.
    return (f"{names} postings haven't opened yet. "
            f"These are today's closest fits.")


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

    WHAT THE PAGE CURRENTLY USES. Only `shared` is rendered — as the chips in
    the pinned Picked column's header. The firm- and role-level tiers are
    still computed and still correct, but nothing displays them since the
    long-form disclosure was removed; `blocks` survives as the recommend
    bar's "are there picks?" guard. This docstring used to promise that every
    reason the scorer produced is printed exactly once somewhere, which is no
    longer true, and saying so here is cheaper than letting the next reader
    trust it. The tiering is kept rather than deleted because it is the thing
    a per-card "why" would be rebuilt from.

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

    confirmed = (fd.confidence or 0.0) >= 0.8 and prec in ("day", "month", "")
    return {
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
# The two rows come from two incompatible conventions: the seeds date a
# HK intake that opens the September before its summer, the human `SA 2028`
# rows hang off postings that are live now. Nothing on the row can tell a
# reader which convention it follows, so the page asserts both.
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


def _drop_contradicted_openings(rows: list[dict]) -> list[dict]:
    """Drop a rumored `app_open` that a dated `app_close` in the same printed
    scope already places in the past."""
    closes: dict[tuple[str, str], object] = {}
    for row in rows:
        if row["event_kind"] == "app_close" and row["date"] is not None:
            scope = _cycle_scope(row)
            if scope not in closes or row["date"] < closes[scope]:
                closes[scope] = row["date"]

    keep = []
    for row in rows:
        close = closes.get(_cycle_scope(row))
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


def _track_facet(qs, selected=""):
    """Track options with live per-option counts. Track is the FIRM's vertical
    (`Firm.tracks`: ib/consulting/...), a different dimension from the role's
    own classified bucket — merging the two into one select is what the
    separate Role Type facet replaced, and they must not re-merge.

    OVERLAP, deliberately, same posture as `_year_facet`: a firm carrying both
    `ib` and `st` counts its roles under BOTH, so these counts can sum to more
    than the total. Deduping would mean picking one of a firm's real verticals
    to lie about. Each individual number still keeps the count promise — pick
    that track and you get exactly that many.

    Two small queries instead of a row-by-row firm join: one GROUP BY firm_id
    over the filtered set (~100 rows), one flat read of every firm's tracks.

    See `_region_facet` for why `selected` is always kept in the options."""
    tracks_by_firm = dict(Firm.objects.values_list("id", "tracks"))
    counts: Counter[str] = Counter()
    total = 0
    for firm_id, n in (
        qs.values_list("firm_id").annotate(n=Count("id")).values_list("firm_id", "n")
    ):
        total += n
        for t in (tracks_by_firm.get(firm_id) or []):
            counts[t] += n
    return [
        {"value": "", "label": "Any Track", "count": total},
        *[
            {"value": t, "label": TRACK_LABELS.get(t, t), "count": counts[t]}
            for t in sorted(
                set(counts) | ({selected} if selected else set()),
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
    if "track" not in skip and sel["track"]:
        qs = qs.filter(firm__tracks__contains=[sel["track"]])
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
    for cohort, class_year, derived, grad_years, start_years in qs.values_list(
            "cohort", "class_year", "class_year_derived",
            "raw__facts__grad__years", "raw__facts__start__years"):
        total += 1
        years = {y for y in (cohort or "", class_year or "", derived or "") if y}
        for y in (*(grad_years or ()), *(start_years or ())):
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
# How recently a rolling posting must have first been seen to count as "fresh".
_FRESH_DAYS = 10
# How many rolling cards the feed shows before deferring to browse-by-firm.
_ROLLING_FEED_CAP = 30


def _unconfirmed_note(o) -> dict:
    """Whether Coverage's own most recent check of this posting actually
    reconfirmed it is live, as something a template can render honestly. {}
    when it did (or there is nothing to compare — every open row carries
    both timestamps from ingest, so in practice this is only {} on a clean
    confirmation).

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
    if not o.last_checked or not o.last_verified or o.last_checked <= o.last_verified:
        return {}
    return {
        "label": "Not recently confirmed live",
        "why": ("Our last check of this posting could not confirm it is "
                "still live. It still shows as open because we also can't "
                "confirm it closed — verify on the firm's own site before "
                "relying on this link."),
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
_FACT_CHIPS_MAX = 2

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
    "Your year (2029)" and `year_likely` "Likely your year (2029)" — neither
    repeats the window, so the fact chip is the only place a student can read
    what the posting actually stated, and it stays. Anonymous visitors get no
    verdict at all and are untouched.

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
        "language": lambda f: f"{f['value']} needed",
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
    verdict requires BOTH sides to have spoken."""
    if not getattr(user, "is_authenticated", False):
        return None
    class_year = getattr(user, "class_year", None)
    work_auth = getattr(user, "work_authorization", None) or {}
    if not class_year and not work_auth:
        return None
    return {"class_year": class_year, "work_auth": work_auth}


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
                    "label": f"Your year ({cy})",
                    "why": f"The posting states it is for the Class of {stated_year}."}
        return {"kind": "year_out", "blocking": True,
                "label": f"For {stated_year} grads",
                "why": (f"The posting states it is for the Class of "
                        f"{stated_year}, not your {cy}.")}
    grad = ((o.raw or {}).get("facts") or {}).get("grad")
    if cy and grad and grad.get("years"):
        years = [int(y) for y in grad["years"]]
        if min(years) <= cy <= max(years):
            return {"kind": "year_ok", "blocking": False,
                    "label": f"Your year ({cy})",
                    "why": grad.get("phrase", "")}
        return {"kind": "year_out", "blocking": True,
                "label": f"For {grad['value']} grads",
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
                "label": f"Likely your year ({cy})",
                "why": derive_class_year(o.bucket or "", o.title or "",
                                         o.cohort or "")[1]}
    return None


def _urgency_item(o, *, now, today, my_firm_ids, profile=None):
    """One feed card: firm identity + the honest urgency signal for this
    role (a real countdown when dated, freshness when rolling, or an
    explicit "deadline passed" state — see the three-way split below)."""
    bucket = o.bucket or OTHER
    seen_days = (now - o.first_seen).days if o.first_seen else None
    place = _place(o)
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
        "seen_days": seen_days,
        "is_fresh": seen_days is not None and seen_days <= _FRESH_DAYS,
        "facts": _fact_chips(o, verdict=_eligibility(o, profile)),
        "reported": deadline_provenance(o),
        "verdict": _eligibility(o, profile),
        # Whether the Read control has anything to open. Checked here, not in
        # the template, so the card never offers a drawer that would come back
        # empty.
        "has_text": bool((o.raw or {}).get("detail_text")),
        # {} on a clean confirmation; a label+why when our last check of this
        # URL could not reconfirm it — see `_unconfirmed_note`.
        "unconfirmed": _unconfirmed_note(o),
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
    # NOT FIXED HERE, and deliberately: this branch builds its own countdown
    # rather than calling `deadline_marker`, so it does not carry that
    # function's rule that a date whose precision refuses to name a day gets
    # no day count (see `_INEXACT_PRECISIONS`). A `deadline_precision` of
    # "month"/"estimated" therefore still reaches the feed as "Closes in 4
    # days", with a day-level urgency band and a fuse bar burning down to a
    # specific afternoon. Correcting it is not a local edit: `days_left` set
    # here is an ORDERING and AGGREGATION key for `_urgency_feed`'s sort, the
    # firm cluster's `next_days` (which must never go negative — see its
    # comment below), the cluster role sort, and the closing-this-week count,
    # and a coarser value breaks each differently. Zero live rows carry an
    # inexact precision today, so this is latent, not shipped.
    elif o.deadline >= today:
        days = (o.deadline - today).days
        item.update({
            "dated": True,
            "days_left": days,
            "countdown": ("Closes today" if days == 0 else
                          "Closes tomorrow" if days == 1 else
                          f"Closes in {days} days"),
            "level": "today" if days <= 2 else "soon" if days <= 7 else "upcoming",
            # Remaining fraction of the fuse (100 = far out, ~0 = closing).
            "fuse_pct": max(4, round((1 - min(days, _FUSE_HORIZON) / _FUSE_HORIZON) * 100)),
        })
    else:
        item.update({
            "dated": True,
            "days_left": (o.deadline - today).days,  # negative: days overdue
            "countdown": "Deadline passed",
            "level": "passed",
            "fuse_pct": 0,
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


def _urgency_feed(qs, *, now, today, my_firm_ids, profile=None):
    """Rank the filtered set into the Closing-Soon and Fresh-&-Rolling bands.
    Dated roles sort by nearest deadline; rolling roles sort by your-firm
    first, then freshest-seen, then this-cycle cohort."""
    closing, rolling = [], []
    for o in qs:
        item = _urgency_item(o, now=now, today=today, my_firm_ids=my_firm_ids,
                             profile=profile)
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
    for d in FirmDate.objects.filter(
            confidence=1.0, date__gte=today,
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


def _last_checked() -> str:
    """"3 hours" / "2 days" since the newest scrape run, or ""."""
    from .models import ScrapeRun

    latest = ScrapeRun.objects.exclude(connector="reverify").order_by("-started").first()
    return timesince(latest.started, depth=1) if latest else ""


def opportunities(request, *, dismiss_undo=None, scope_only=False):
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

    `dismiss_undo` is the one thing this view does not compute for itself: the
    "you just said not for me / Undo" strip, handed in by `_refresh_feed` when
    a dismissal is what triggered this render. `scope_only` asks for the scope
    block alone. Both are keyword-only and defaulted, so the URLconf still
    calls this with a request and nothing else."""
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
    track = request.GET.get("track", "").strip()
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
        "providers": sorted(
            s for s in open_qs.values_list("source", flat=True).distinct() if s
        ),
    }
    year_facet = _year_facet(
        _apply_filters(open_qs, selected, skip=("year",)), year
    )

    bucket_counts = Counter(
        (b or OTHER)
        for b in _apply_filters(open_qs, selected, skip=("role",)).values_list(
            "bucket", flat=True
        )
    )
    role_facet = [
        {
            "value": value,
            "label": label,
            "count": (
                sum(bucket_counts.values())
                if value == "all"
                else sum(bucket_counts.get(b, 0) for b in TARGET_BUCKETS)
                if value == ""
                else bucket_counts.get(value, 0)
            ),
        }
        for value, label in ROLE_CHOICES
    ]
    role_count = {r["value"]: r["count"] for r in role_facet}

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
    # rows for the same reason the fit filter is: it depends on who is asking
    # (a copy the student already tracked wins the tie), so it must never
    # reach the shared facet counts. The counts describe the BOARD; this
    # describes one student's render of it.
    #
    # Some firms genuinely file one job as several requisitions — SIG posts
    # every 2027 internship under two iCIMS job numbers, Deutsche Bank runs
    # apprentice intakes as parallel reqs — and those reqs close on their own
    # schedules, so the rows must all stay in the database and stay
    # close-tracked. Only the render is collapsed. See directory.dupes.
    # `dupes_shown` names the toggle's own state: the "Show repeat listings"
    # checkbox in the filter bar (opportunities.html) is this control now,
    # not a lone escape-hatch link — the count that used to live in the
    # header's subset sentence rides that checkbox's label instead.
    dupes_shown = request.GET.get("dupes", "").strip() == "1"
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
    # The mirror-image fragment: `scope_only` asks for the scope block alone
    # (see `scope_context` and the early return that uses it, far below). A
    # KEYWORD, not a querystring flag like `cols=`, because nothing about it
    # belongs in a URL: `_refresh_feed` is its only caller, and a `?scope=1`
    # in the address bar would leak into every "show me the hidden ones" link
    # this very block builds out of the live request.
    scope_fragment = scope_only and bool(request.headers.get("HX-Request"))

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

    feed = (None if cols_fragment else
            _urgency_feed(rows, now=now, today=today, my_firm_ids=my_firm_ids,
                          profile=elig_profile))

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
            picks = [
                _pick_card(r)
                for r in recommend(
                    profile,
                    # The page's own clock, not the ranker's default. `recommend`
                    # keeps itself free of Django and so falls back to
                    # `date.today()` — the SERVER's local date — while every
                    # date-sensitive surface in this view (`today` above, the
                    # urgency feed, `deadlines.closing_soon_window`) reads
                    # `timezone.localdate()`, i.e. the date in `settings.
                    # TIME_ZONE` (UTC). On any host whose OS clock is not UTC
                    # the two are a different day for part of every day — eight
                    # hours of it on the founder's own machine — and in that
                    # window the picks dropped a role as expired that the feed
                    # beside them still rendered as closing today. One clock
                    # per request, passed in.
                    today=today,
                    candidates=[
                        Candidate.from_opportunity(o)
                        # Folded first, and scored second. Two copies of one
                        # posting score identically by construction, so an
                        # unfolded input spends two of six pick slots saying
                        # the same thing — the most expensive place on the
                        # page to repeat yourself. This reads `open_qs`, not
                        # the filtered `rows`, so the ranking still sees the
                        # whole board (see the note above).
                        for o in fold_duplicates(
                            [
                                o for o in open_qs.filter(bucket__in=TARGET_BUCKETS)
                                # A pick is a RECOMMENDATION, held to a higher
                                # bar than a listing: a role whose own text
                                # blocks this user (wrong stated year, refuses
                                # their visa) may still be worth seeing on the
                                # board, but the product must not point at it
                                # and say "for you".
                                if not (lambda v: v and v["blocking"])(
                                    _eligibility(o, elig_profile))
                            ],
                            sticky_ids=sticky_ids,
                        )[0]
                    ],
                )
            ]
    pick_shared, pick_blocks = _group_picks(picks)

    clusters: dict[int, dict] = {}
    # The picks are also rendered as the pinned first column of the feed, so
    # their cards are collected during the same pass rather than re-queried.
    pick_ids = {p["id"] for p in picks}
    pick_items: dict[int, dict] = {}
    # Every feed item by role id, so the bulk-save peek can show the rows the
    # banner is offering without building or querying a second set. The dicts
    # here are the SAME objects the firm columns render, held by reference —
    # see `_bulk_save_peek`, which is the only reader.
    item_by_id: dict[int, dict] = {}
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
                "match": bool(user_regions & {r.lower() for r in (o.firm.regions or [])})
                or bool(user_tracks & set(o.firm.tracks or [])),
                "closing_count": 0,
                "next_days": None,
                "roles": [],
            }
        item = _urgency_item(o, now=now, today=today, my_firm_ids=my_firm_ids,
                             profile=elig_profile)
        cl.setdefault("_opps", []).append(o)
        cl["roles"].append(item)
        item_by_id[o.id] = item
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
    pick_cluster = None
    if picks:
        # A COPY of each card, never the shared dict: this column names the
        # firm on every card (its cards come from several firms), and setting
        # that flag on the shared item would print the firm name on the firm's
        # own column too.
        visible = [
            {**pick_items[p["id"]], "show_firm": True}
            for p in picks if p["id"] in pick_items
        ]
        # Built even when the filter hid EVERY pick. A column that silently
        # vanishes the moment you touch a filter reads as breakage, and this
        # page's whole posture is to name what it is holding back rather than
        # quietly shrink — the same rule as the subset sentence and the
        # unregioned-roles line. Empty, it collapses to one honest sentence.
        pick_cluster = {
            "roles": visible,
            "open_count": len(visible),
            "firm_count": len({r["firm_slug"] for r in visible}),
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
        }

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

    # THE BULK-SAVE OFFER, resolved to ids here and stashed, so the confirm
    # dialog's number and the write are the same fact rather than two
    # separately-derived ones (see `_eligible_unsaved_ids` for the 206/209/208
    # measurement that forced this). `track_eligible` reads the stash; it no
    # longer queries for a set of its own.
    #
    # Rewritten on EVERY render of this view, htmx swaps included, because the
    # banner is re-rendered on every one of them: whatever number is on screen
    # is the offer that is live. Stale offers cannot accumulate — there is one
    # key and the newest write wins.
    bulk_save_offer = (
        _eligible_unsaved_ids(request.user, rows, elig_profile)
        if elig_profile and elig_profile.get("class_year") else []
    )
    if request.user.is_authenticated:
        request.session[BULK_SAVE_OFFER_SESSION_KEY] = bulk_save_offer
    # The same list again, resolved to the rows already on the page, so the
    # banner can show WHICH roles it is offering before the student commits.
    # Derived from `bulk_save_offer` and nothing else — see `_bulk_save_peek`.
    bulk_save_peek = _bulk_save_peek(bulk_save_offer, item_by_id)

    # THE SCOPE BLOCK — every sentence at the top of the results that states
    # what this board is NOT showing you, plus the "Save them all" offer and
    # its peek. Assembled as its own dict because it is swappable on its own:
    # a "Not for me" click on a card 600 rows down changes both the hidden
    # count and the offer, and neither is anywhere near the card. See
    # `_scope.html` and the `scope_only` return below.
    scope_context = {
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
        # The lens→pipeline bridge's trigger: open roles whose text names the
        # user's year and which they have never touched (tracked or
        # dismissed both count as touched — "not for me" outranks "your
        # year"). Computed over the FULL row set, not the paged slice.
        "eligible_unsaved": len(bulk_save_offer),
        # The peek panel behind that number: `rows` (capped), `more` (what the
        # cap left out) and `total` (== `eligible_unsaved`, by construction).
        "bulk_save_peek": bulk_save_peek,
        # Present only on the render a dismissal caused (see `_refresh_feed`).
        # Every other render — a filter keystroke, a reload, a lazy-loaded
        # column — draws no strip, which is the whole intended lifetime of a
        # one-shot undo. Nothing is stored, so nothing goes stale; same
        # posture as `crm.views._dismiss_undo_offer`.
        "dismiss_undo": dismiss_undo,
    }

    # ---- The scope block alone, for an out-of-band swap. --------------------
    # A `scope_only` call is a dismissal that happened somewhere ELSE on the
    # page (a role card deep inside a firm column) asking this view to restate
    # the two things that dismissal just changed: how many roles are hidden,
    # and what the "Save them all" offer now covers.
    #
    # It stops HERE, after the session stash above has been rewritten, and that
    # ordering is the whole point. The number on screen, the number in the
    # confirm sentence and the ids `track_eligible` will actually write are one
    # fact resolved once (see `_eligible_unsaved_ids` for the 206/209/208
    # measurement that forced that). A dismissal that updated only the number
    # on screen — or only the stash — would put the three back out of
    # agreement by a new route, and a subtler one than the original.
    if scope_fragment:
        return render(request, "directory/_scope.html",
                      {**scope_context, "oob": True})

    context = {
        # Every "here is what this board is not showing you" sentence, the
        # bulk-save offer and its peek, resolved once above and spread here so
        # the full page and the `scope_only` fragment can never state two
        # different versions of the same counts.
        **scope_context,
        # The paged slice renders; the full list still backs every count
        # above, so the strip describes the board, not the loaded fraction.
        "clusters": cluster_page,
        "all_cluster_count": len(cluster_list),
        "cols_next": cols_next,
        "cols_qs": _qs_without(request, "cols"),
        # When the scrape last ran. The strip's pulsing dot said "live"
        # while the data is radar-cadence; naming the age is what makes the
        # pulse honest.
        "checked_ago": _last_checked(),
        "total": total,
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
        "role_facet": role_facet,
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
        "r": {"id": opp.id, "track_status": (uo.applied_status or "saved") if uo else None},
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
    })


def _eligible_unsaved_ids(user, rows, profile) -> list[int]:
    """The exact roles the "Save them all" banner is offering, as ids.

    Returns the LIST, not a count, and the caller stashes it in the session
    (`BULK_SAVE_OFFER_SESSION_KEY`) so the confirm writes precisely what the
    banner named. `track_eligible` used to re-derive its own set from
    `Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)` —
    the whole table — while this counted `rows`, which is the FEED's
    materialised list AFTER `fold_duplicates` and the user's hidden-row
    exclusions. Two different questions, so two different answers on one page
    load: measured live on the dev board, the banner said 206 and the save
    wrote 209, the three extras being repeat listings the feed had folded
    and the save had not. My Applications then folded them again and its tile
    read 208 — three numbers for one action, in a confirm dialog, which is
    the one place a number has to be exact.

    Sorted so the stashed batch is deterministic and two renders of the same
    board produce the same offer.
    """
    from analytics.models import UserOpportunity

    touched = set(
        UserOpportunity.all_objects.filter(user=user)
        .values_list("opportunity_id", flat=True)
    )
    return sorted(
        o.id for o in rows
        if o.id not in touched
        and (lambda v: v and v["kind"] == "year_ok")(_eligibility(o, profile))
    )


#: How many of the offered roles the peek panel prints before it stops naming
#: them and starts counting them. The offer runs to 223 roles on the live dev
#: board; a panel that rendered all of them would be a page, not a peek, and
#: would scroll past the button it is meant to explain. Eight is what fits
#: above the fold at 375px without the panel needing its own scrollbar, and
#: it is enough rows to see WHAT KIND of roles the offer is made of — which
#: is the question a student actually has before clicking "Save them all".
#: The remainder is never silently dropped: the panel says how many it did
#: not print.
BULK_SAVE_PEEK_MAX = 8


def _bulk_save_peek(offer_ids, item_by_id, *, cap=BULK_SAVE_PEEK_MAX):
    """The first few roles behind the "Save them all" banner, for its peek.

    THE SET IS THE OFFER'S. `offer_ids` is the very list `_eligible_unsaved_ids`
    produced and `opportunities` stashes under `BULK_SAVE_OFFER_SESSION_KEY`
    for `track_eligible` to write — so what the panel names, what the confirm
    counts and what the click saves are one fact resolved once. Re-deriving
    the rows from a second query is precisely the mistake that put 206, 209
    and 208 on one page load (see `_eligible_unsaved_ids`), and it would be a
    worse mistake here: a count that disagrees is an error, but a NAMED role
    that never gets saved is a promise broken by name.

    It costs no query either. `item_by_id` holds the feed items the firm
    columns already built for these same rows, so the panel reads what is on
    the page rather than asking the database a fourth question about it.

    Sorted by what a student is deciding on: dated roles soonest-first, then
    passed deadlines and undated ones, then alphabetically. That order is what
    makes the cap honest — the roles the cap hides are the ones with the least
    to say about acting today.
    """
    def _key(r):
        dated = r["dated"] and r["level"] != "passed"
        return (
            0 if dated else 1,
            r["days_left"] if dated else 0,
            (r["firm_name"] or "").lower(),
            (r["title"] or "").lower(),
        )

    # `if i in item_by_id` is belt-and-braces, not a filter: every offered id
    # came out of the same `rows` these items were built from. It is here so a
    # future caller passing a narrower item map degrades to a shorter list
    # rather than a 500.
    rows = sorted((item_by_id[i] for i in offer_ids if i in item_by_id), key=_key)
    return {
        "rows": rows[:cap],
        # Stated, never implied. A panel that just stopped at eight would be
        # telling a student the offer is eight roles.
        "more": max(0, len(rows) - cap),
        "total": len(rows),
    }


def _eligible_unsaved_count(user, rows, profile) -> int:
    """How many roles the "Save them all" offer covers, for surfaces that
    only print the number (Today's chip — `crm.today._cockpit_context`).

    Deliberately `len()` of the id list rather than its own count: this and
    the banner disagreeing is the same defect one level up, and the Today
    chip really did read 209 against the feed banner's 206 on the same board.
    Callers must pass a row list that has already been through
    `directory.dupes.fold_duplicates`, for the same reason — a repeat listing
    is one role, and counting it twice here is what made the third number."""
    return len(_eligible_unsaved_ids(user, rows, profile))


# The session key `track_eligible` stashes its batch under, so a redirect to
# My Applications can offer an "Undo" that removes exactly the rows THIS
# bulk save created — never a hand-saved row, and never an earlier batch's
# rows once the student has looked at them (see `_my_applications_context`,
# which pops this the one time it renders). Session-backed rather than a
# DB column: the batch is only ever meant to be undoable in the moment right
# after the write, not queried or audited later, and `product_events` (via
# `record_event` below) already carries the durable count for that.
BULK_SAVE_SESSION_KEY = "bulk_save_batch"

#: The ids the "Save them all" banner is currently OFFERING — written by
#: `opportunities` on every render, read by `track_eligible` as the exact set
#: to write. The point of the stash is that the number in the confirm sentence
#: and the rows the confirm creates are one fact, resolved once, rather than
#: two queries that answered slightly different questions (they did: see
#: `_eligible_unsaved_ids`). Session-backed for the same reason the undo batch
#: above is — it is meaningful only between the render and the click that
#: follows it, and nothing later ever needs to query it.
BULK_SAVE_OFFER_SESSION_KEY = "bulk_save_offer"


@login_required
@require_POST
def track_eligible(request):
    """Save every open role whose own text names the user's class year.

    The lens→pipeline bridge: the eligibility work produced "Your year"
    verdicts, and then left the user to find and star those roles one by
    one. This saves them in a click — and ONLY them: year_ok verdicts,
    which by the verdict contract exist only where the posting stated its
    window AND Settings stated a class year. A role already tracked keeps
    its stage untouched, and a dismissed role stays dismissed — "not for
    me" outranks "your year", because the user said so.

    Two guards this used not to have, added after a customer-perspective
    walk found one click dumping 207 roles into a 1-role pipeline with no
    way back:

    CONFIRM. The banner's own `<details>` discloses "Save N roles to My
    Applications?" before the real submit button ever renders (see
    `directory/_results.html`), so a plain click never writes. But that is
    a template affordance, not a guarantee — anyone can POST this endpoint
    directly. `confirmed=1` is the actual gate: without it, nothing is
    written, full stop, no matter what the client claims the count was.

    UNDO. Every id this call creates is stashed in the session (see
    `BULK_SAVE_SESSION_KEY` above) for `track_eligible_undo` to reverse.

    THE SET IS THE BANNER'S, NOT THIS VIEW'S. It used to re-derive its own
    from `Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)`
    — the whole table — while the banner counted the FEED's materialised rows,
    which are folded for duplicates and stripped of the user's hidden rows.
    One page load therefore produced three numbers for one action: the confirm
    said 206, the write made 209, and My Applications' tile then read 208. So
    this reads `BULK_SAVE_OFFER_SESSION_KEY`, the exact ids the banner named
    when it rendered.

    The per-row checks below still run over that set, and can only ever REMOVE
    from it: a role dismissed in another tab, or closed by a scrape, between
    the render and the click must not be saved just because it was offered a
    moment ago. Saving FEWER than the confirm said is a fact about the last
    thirty seconds; saving MORE is the product doing something the student
    never agreed to.
    """
    from analytics.models import UserOpportunity

    profile = _eligibility_profile(request.user)
    if not profile or not profile.get("class_year"):
        return HttpResponseBadRequest("no class year in Settings")

    if request.POST.get("confirmed") != "1":
        return HttpResponseBadRequest("confirmation required")

    offered = request.session.get(BULK_SAVE_OFFER_SESSION_KEY) or []
    if not offered:
        # Nothing was offered on this session's last look at the feed, so
        # there is no number this call could honour. Same posture as the
        # confirm gate above: refuse rather than fall back to a set the
        # student was never shown.
        return HttpResponseBadRequest("no bulk-save offer to confirm")

    touched = dict(
        UserOpportunity.all_objects.filter(user=request.user)
        .values_list("opportunity_id", "dismissed")
    )
    saved_ids: list[int] = []
    for o in Opportunity.objects.filter(
            id__in=offered, status="open", bucket__in=TARGET_BUCKETS):
        v = _eligibility(o, profile)
        if not (v and v["kind"] == "year_ok"):
            continue
        if o.id in touched:
            continue
        UserOpportunity.all_objects.create(user=request.user, opportunity=o)
        saved_ids.append(o.id)
    saved = len(saved_ids)
    # The offer is consumed either way: a second POST of the same confirm
    # (a double-click, a back-then-resubmit) must not re-run against a batch
    # the student has already acted on.
    request.session.pop(BULK_SAVE_OFFER_SESSION_KEY, None)
    if saved:
        record_event("eligible_bulk_saved", user=request.user, count=saved)
        # Overwrites any earlier, presumably-already-seen batch — only the
        # most recent bulk save is ever offered an undo.
        request.session[BULK_SAVE_SESSION_KEY] = {"ids": saved_ids, "count": saved}
    from django.contrib import messages

    messages.success(
        request,
        (f"Saved {saved} role that names your year." if saved == 1
         else f"Saved {saved} roles that name your year.")
        if saved else "Nothing new to save: every role naming your year is already tracked.")
    from django.shortcuts import redirect

    return redirect("my_applications" if saved else "opportunities")


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


def _refresh_feed(request, *, scope_only=False, dismiss_undo=None):
    """Re-render the Opportunities feed (or just its scope block) after a
    write that changed what the board is allowed to offer.

    WHY RE-RENDER RATHER THAN PATCH THE NUMBER. Dismissing a role changes four
    things at once: the hidden count, the "Save them all" sentence, the peek
    behind it, and the id list stashed in the session for `track_eligible` to
    write. Decrementing the visible number and leaving the stash alone is the
    206/209/208 bug (see `_eligible_unsaved_ids`) reintroduced from the other
    end — the screen and the write disagreeing, only now the screen is the one
    that is wrong. Running the real view is what keeps all four one fact,
    because the real view is where that fact is computed.

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
    return opportunities(request, dismiss_undo=dismiss_undo, scope_only=scope_only)


def _dismiss_undo_offer(opp, *, source):
    """The one-shot "Not for me / Undo" strip a dismissal comes back with.

    IN THE RESPONSE, NOT IN THE SESSION, for the same reason
    `crm.views._dismiss_undo_offer` chose that: the offer is handed back in
    the very swap that removed the role, so it can ride in the markup and get
    the right lifetime for free. Any later render of this page — a filter
    keystroke, a reload — draws no strip. Nothing is stored, so nothing goes
    stale.

    `source` is where the click happened, and the peek reads it to stay open
    across the swap: a student pruning a list of eight roles should not have
    to reopen the panel between each one.
    """
    return {"id": opp.id, "firm_name": opp.firm.name, "title": opp.title,
            "source": source}


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
        if origin == "peek" and undone:
            return _refresh_feed(
                request,
                dismiss_undo=(_dismiss_undo_offer(opp, source="peek")
                              if status == "dismiss" else None),
            )
        if origin == "card" and undone:
            # TWO fragments in one response. The card's own target is
            # `closest .rolecard`, so the first replaces the card in place —
            # with the Undo stub on a dismissal, with the real card again on
            # an undo. The second is an out-of-band swap of the scope block,
            # which is at the top of the page and states two things this
            # click just changed: how many roles are hidden, and how many the
            # "Save them all" offer now covers. Patching only the card would
            # leave the banner promising a number the confirm can no longer
            # honour.
            #
            # The undo lives ON the stub rather than in that scope block on
            # purpose: a strip at the top of the page is invisible to someone
            # who just clicked a card 600 rows down, and an undo you cannot
            # see is not an undo.
            show_firm = request.POST.get("show_firm") == "1"
            card = (
                render_to_string(
                    "directory/_rolecard_dismissed.html",
                    {"r": _dismiss_undo_offer(opp, source="card"),
                     "show_firm": show_firm},
                    request=request,
                )
                if status == "dismiss"
                else _one_rolecard(request, opp, show_firm=show_firm)
            )
            scope = _refresh_feed(request, scope_only=True)
            return HttpResponse(card + scope.content.decode(scope.charset))
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
    when = f", confirmed {timesince(o.closed_at)} ago" if o.closed_at else ""
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
            # Each row still carries the fuller sentence itself, via
            # `_posting_closed_note`, for the student reading that one row.
            "note": "Taken down by the firm. Stays on your list.",
            "empty_state": False,
        },
        {
            "key": "passed",
            "label": "Deadline Passed",
            "items": passed,
            "note": "The posted date has gone by.",
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
            "label": "Rolling",
            "items": rolling,
            # "Reviewed as they arrive, so apply early" used to be stated for
            # every row here, the same invented claim the feed retracted
            # (views.py's feed item builder, ~line 1338: most undated roles
            # never say how they're reviewed). Rows whose own posting states
            # rolling review are marked "Rolling" individually below
            # (r.rolling_stated); this note now only says what is true of
            # the whole bucket.
            "note": "No posted deadline.",
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
        # The eyebrow used to `join` firm.regions and firm.tracks raw, under a
        # `text-transform: uppercase`, so /firms/hsbc/ said "HK · IB" and
        # /firms/alibaba/ said "CORP-STRAT" — a hyphenated internal slug that
        # does not read as an abbreviation of anything. `pipeline` had no
        # label in TRACK_LABELS at all. Meanwhile the Opportunities facets,
        # built from the same two maps, spell them "Hong Kong" and "Corporate
        # Strategy". views.py:98 already states the position: raw slugs read
        # as internal shorthand and this page is public-facing.
        #
        # These are firms.yaml's DECLARATION, not this page's rows, and the
        # template prefixes them "Recruits:" for exactly that reason. Labels
        # fixed how the words READ, not what they CLAIMED, and bare they
        # claimed the wrong thing: 25 of the 42 firms that declare a region
        # have a top live market they never declared, and 13 have no live row
        # in ANY declared market. See firm_detail.html's eyebrow comment — the
        # short version is that a bare "Hong Kong" over ten Mainland China
        # roles is the 925-vs-13 defect in the docstring above, with markets
        # instead of counts.
        #
        # Still the declaration and NOT `_open_markets`: 50 of 131 firms have
        # no open campus row at all, 33 of them declaring a region, so there
        # is frequently no live market to derive and the declaration is the
        # only honest thing left to print. Swapping meaning by whether rows
        # happen to exist would put two answers in one slot, which is the
        # defect this prefix exists to close.
        "eyebrow_regions": _labelled(firm.regions, REGION_LABELS),
        "eyebrow_tracks": _labelled(firm.tracks, TRACK_LABELS, by_label=True),
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
        # Still the full in-scope count, never the printed count. The heading
        # answers "how many are open here", and every other surface (contacts
        # board, feed) answers it with this same number — the 925-vs-13 bug in
        # the docstring above is what a page-local count looks like.
        "total": len(cards),
        "role": role,
        "campus_total": campus_total,
        "other_total": other_total,
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
