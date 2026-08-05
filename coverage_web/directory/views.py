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

from collections import Counter

from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, F, IntegerField, Max, Q, Value, When
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from analytics.events import record_event
# Read-only, cross-app import (build-plan.md §2's private zone). directory
# never writes crm rows; the opportunities feed only reads UserFirm via the
# tenant-scoped manager. No import cycle: crm.models imports directory.models.
from crm.models import Contact, UserFirm
from directory.classify import (
    BUCKET_LABELS, ENTRY_LEVEL, INSIGHT, INTERNSHIP, OTHER, REGION_LABELS,
    REGION_ORDER, TARGET_BUCKETS,
)
# The one definition of "closing soon" — see deadlines.py for why it isn't
# spelled out at each call site (and for the crm/views.py follow-up).
from directory.deadlines import CLOSING_SOON_DAYS, is_closing_soon
from directory.models import Firm, Opportunity
from directory.recommend import Candidate, Profile, recommend
from directory.timeline import EVENT_LABELS

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
# read as internal shorthand; the filter is public-facing.
TRACK_LABELS = {
    "ib": "Investment Banking",
    "st": "Sales & Trading",
    "pe": "Private Equity / Credit",
    "am": "Asset Management",
    "consulting": "Consulting",
    "corp-strat": "Corporate Strategy",
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


def deadline_marker(deadline, precision, *, today=None):
    """Format a deadline honestly, respecting its stated precision, and
    never fabricating one. A null deadline says so out loud.
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


def _sponsorship_tag(opp) -> dict | None:
    """Sponsorship pill: the posting's own field wins; when it's unknown,
    fall back to the firm-level fact from the seed. Still unknown -> no pill
    rather than a hedge.

    `Firm.sponsors` is a per-REGION JSON dict (`{"us": True, "hk": "unknown"}`
    — see `seed_directory._sponsors_blob`), never a bare bool, so it must be
    looked up by `opp.region`, not tested as a truthy/falsy scalar. The
    scalar test was unreachable on every one of the ~4,000 open rows even
    though 58 firms hold real per-region data.

    A blank `opp.region` (~1,223 open rows — mostly unparsed board
    locations) has no region to key the lookup on, so this returns None
    rather than falling back to *some* region of the firm's: a firm that
    sponsors in HK but not the US must never stamp "Sponsorship" on a role
    whose own market is unknown."""
    s = (opp.sponsorship or "unknown").lower()
    if s == "yes":
        return {"label": "Sponsorship", "css": "spon-known"}
    if s == "no":
        return {"label": "No Sponsorship", "css": "spon-none"}
    if not opp.region:
        return None
    firm_fact = (opp.firm.sponsors or {}).get(opp.region)
    if firm_fact is True or firm_fact == "true":
        return {"label": "Sponsorship", "css": "spon-known"}
    if firm_fact is False or firm_fact == "false":
        return {"label": "No Sponsorship", "css": "spon-none"}
    return None


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
    spon = _sponsorship_tag(opp)
    if spon:
        tags.append(spon)
    return {
        "id": opp.id,
        "firm_name": opp.firm.name,
        "firm_slug": opp.firm.slug,
        "title": opp.title,
        "location": opp.location,
        "url": opp.url,
        "region": opp.region,
        "role": {"value": bucket, "label": BUCKET_LABELS.get(bucket, bucket)},
        # Programme year (see classify.py); the template must not print it as
        # a class year. `class_year` beside it is the stated graduation year.
        "cohort": opp.cohort,
        "class_year": opp.class_year,
        "deadline": deadline_marker(opp.deadline, opp.deadline_precision, today=today),
        "tags": tags,
    }


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


# Track suffixes the seeds append to a cycle slug.
_CYCLE_TRACKS = {
    "ib": "IB", "pe": "PE", "st": "S&T", "am": "AM",
    "hk": "Hong Kong", "us": "US", "eu": "Europe", "sg": "Singapore",
}


def cycle_label(cycle: str) -> str:
    """`sa2028_ib` -> `SA 2028 · IB`.

    The column holds two spellings of one vocabulary — importers wrote
    `sa2028_ib`, the seeds wrote `SA 2028` — and the firm page printed
    whichever it found, so a student reading Jefferies saw the raw slug
    `SA2028_IB` sitting in the product's own body copy. Formatting on read
    rather than migrating: the stored value is what the importer matched on,
    and rewriting it would break re-imports for a display bug.
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
    track = _CYCLE_TRACKS.get(tail.lower(), tail.replace("-", " ").title() if tail else "")
    return f"{label} · {track}" if track else label


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
        "cycle": cycle_label(fd.cycle),
        "region": fd.region,
        "event_kind": fd.event_kind,
        "event_label": EVENT_LABELS.get(fd.event_kind, fd.event_kind.replace("_", " ").capitalize()),
        "date_text": date_text,
        "precision": prec,
        "confidence": confidence_marker(fd.confidence),
        "state": "confirmed" if confirmed else "rumored",
        "source_url": fd.source_url,
    }


def _timeline(firm, *, today):
    rows = firm.firm_dates.all().order_by(F("date").asc(nulls_last=True), "cycle", "event_kind")
    return [_firm_date_row(fd, today=today) for fd in rows]


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
REGION_NONE_LABEL = "Other / Unstated"


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
        return qs.filter(cohort="", class_year="")
    if year.isdigit():
        return qs.filter(Q(cohort=year) | Q(class_year=year))
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
    for cohort, class_year in qs.values_list("cohort", "class_year"):
        total += 1
        years = {y for y in (cohort or "", class_year or "") if y}
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


def _fresh_label(seen_days: int | None) -> str:
    """What the "New" badge actually measures, spelled out: `first_seen` is
    when the row entered OUR db, not when the firm posted it — so the badge
    must say "first seen", never bare "New". (Bug: after a bulk import, 794
    of 805 open roles wore "New" because every backfilled row's `first_seen`
    was the import timestamp, days after the firm actually listed it.)"""
    if seen_days is None:
        return ""
    if seen_days == 0:
        return "First seen today"
    if seen_days == 1:
        return "First seen 1d ago"
    return f"First seen {seen_days}d ago"


def _urgency_item(o, *, now, today, my_firm_ids):
    """One feed card: firm identity + the honest urgency signal for this
    role (a real countdown when dated, freshness when rolling, or an
    explicit "deadline passed" state — see the three-way split below)."""
    bucket = o.bucket or OTHER
    seen_days = (now - o.first_seen).days if o.first_seen else None
    item = {
        "id": o.id,
        "firm_name": o.firm.name,
        "firm_slug": o.firm.slug,
        "monogram": _monogram(o.firm.name),
        "category": FIRM_CATEGORIES.get(o.firm.slug, ""),
        "title": o.title,
        "url": o.url,
        "location": o.location,
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
        "fresh_label": _fresh_label(seen_days),
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
        item.update({"dated": False, "days_left": None, "level": "rolling"})
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


def _urgency_feed(qs, *, now, today, my_firm_ids):
    """Rank the filtered set into the Closing-Soon and Fresh-&-Rolling bands.
    Dated roles sort by nearest deadline; rolling roles sort by your-firm
    first, then freshest-seen, then this-cycle cohort."""
    closing, rolling = [], []
    for o in qs:
        item = _urgency_item(o, now=now, today=today, my_firm_ids=my_firm_ids)
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


def _last_checked() -> str:
    """"3 hours" / "2 days" since the newest scrape run, or ""."""
    from django.utils.timesince import timesince

    from .models import ScrapeRun

    latest = ScrapeRun.objects.exclude(connector="reverify").order_by("-started").first()
    return timesince(latest.started, depth=1) if latest else ""


def opportunities(request):
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
    page. Works with JS off, too (the form is a normal GET form)."""
    now = timezone.now()
    today = timezone.localdate()

    open_qs = Opportunity.objects.filter(status="open").select_related("firm")

    # Roles the user has said are not for them leave the feed. Reversible —
    # `hidden_count` below offers them back — but a decision the board ignores
    # is not a decision. Signed-out visitors have no dismissals, so this costs
    # them a set literal and no query.
    hidden_ids: set[int] = set()
    if request.user.is_authenticated:
        from analytics.models import UserOpportunity
        hidden_ids = set(
            UserOpportunity.objects.for_user(request.user)
            .filter(dismissed=True)
            .values_list("opportunity_id", flat=True)
        )
        if hidden_ids:
            open_qs = open_qs.exclude(id__in=hidden_ids)

    role = request.GET.get("role", "").strip()
    # Programme/intake year, or `none` for the rows that state no year at all.
    year = request.GET.get("year", "").strip()
    region = request.GET.get("region", "").strip()
    track = request.GET.get("track", "").strip()
    provider = request.GET.get("provider", "").strip()
    # Multi-select firm filter: any number of ?firm=<slug> params.
    firm_slugs = [s.strip() for s in request.GET.getlist("firm") if s.strip()]
    query = request.GET.get("q", "").strip()

    selected = {
        "role": role,
        "year": year,
        "region": region,
        "track": track,
        "provider": provider,
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
    # `other` and `all` are deliberately absent from the four drawn segments —
    # they are opt-ins, reachable by deep link or by the subset sentence's
    # "Show everything" link, never a sibling option.
    effective_role = _effective_role(role)
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
        for v in SEGMENT_VALUES
    ]

    # THE CONDITIONAL FIFTH SEGMENT — deep-link honesty, and a real bug guard.
    #
    # Two jobs. (1) With `?role=all` active the bar must say so rather than
    # drawing four campus pills, none checked, over a feed showing 4,342 rows.
    # (2) Far less obvious and far more damaging: a radio GROUP WITH NO CHECKED
    # MEMBER SERIALIZES NOTHING. Without this segment, `?role=all` renders four
    # unchecked radios, and the moment the student touches Region or Search the
    # htmx GET goes out with no `role` key at all — the mode silently resets to
    # campus and 3,456 roles vanish mid-interaction. A checked fifth radio
    # keeps `role` in the serialization, which is why it is an input and not a
    # decorative chip.
    role_optin_segment = None
    if role in ROLE_OPTIN:
        role_optin_segment = {
            "value": role,
            "label": SEGMENT_LABELS[role],
            "count": role_count.get(role, 0),
            "input_id": f"seg-role-{role}",
            "count_id": f"cnt-role-{role}",
        }

    # `effective_role`, not `role`: an unrecognised `?role=` renders the campus
    # scope, so it is hiding the experienced rows too and must say so.
    hidden_other = bucket_counts.get(OTHER, 0) if effective_role == "" else 0

    # The "Show everything" escape hatch for `hidden_other`: role forced to
    # "all", every other active filter preserved. Built from the live
    # querystring, not hardcoded — the bug this replaced rendered a bare `?`
    # (no `show_all_qs` in context at all), which is `/opportunities/?`: the
    # default view, i.e. the exact page that hides the roles it promised to
    # reveal.
    show_all_params = request.GET.copy()
    show_all_params["role"] = "all"
    show_all_qs = show_all_params.urlencode()

    # ---- The region honesty line. Picking a concrete market excludes every
    # row whose location resolved to nothing (297 of 886 on the live set), and
    # before this the page said nothing about it. The number is read straight
    # off the Region facet's own "Other / Unstated" option — already crossed
    # against every other active filter — so the sentence and the option can
    # never disagree. Same live-querystring construction as `show_all_qs`.
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
    feed = _urgency_feed(rows, now=now, today=today, my_firm_ids=my_firm_ids)

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
    if request.user.is_authenticated:
        profile = Profile.from_user(request.user, tier_by_firm)
        if not profile.is_empty:
            picks = [
                _pick_card(r)
                for r in recommend(
                    profile,
                    [
                        Candidate.from_opportunity(o)
                        for o in open_qs.filter(bucket__in=TARGET_BUCKETS)
                    ],
                )
            ]
    pick_shared, pick_blocks = _group_picks(picks)

    clusters: dict[int, dict] = {}
    # The picks are also rendered as the pinned first column of the feed, so
    # their cards are collected during the same pass rather than re-queried.
    pick_ids = {p["id"] for p in picks}
    pick_items: dict[int, dict] = {}
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
                "sponsorship": _sponsorship_tag(o),
                "is_mine": o.firm_id in tier_by_firm,
                "tier": tier_by_firm.get(o.firm_id),
                "match": bool(user_regions & {r.lower() for r in (o.firm.regions or [])})
                or bool(user_tracks & set(o.firm.tracks or [])),
                "closing_count": 0,
                "next_days": None,
                "roles": [],
            }
        item = _urgency_item(o, now=now, today=today, my_firm_ids=my_firm_ids)
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
    for cl in clusters.values():
        cl["roles"].sort(key=lambda i: (
            not i["dated"],
            i["level"] == "passed",
            i["days_left"] if i["days_left"] is not None else 9999,
            not i["is_fresh"],
            i["seen_days"] if i["seen_days"] is not None else 9999,
            i["title"].lower(),
        ))
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
    total = sum(c["open_count"] for c in cluster_list)
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

    context = {
        "clusters": cluster_list,
        "hidden_count": len(hidden_ids),
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
        "hidden_other": hidden_other,
        "show_all_qs": show_all_qs,
        "hidden_region": hidden_region,
        "show_unregioned_qs": show_unregioned_qs,
        # How many of the four row-2 controls are engaged. Drives the mobile
        # disclosure's "Filters · 2" summary AND the decision to server-render
        # it open, so a deep-linked filter is never invisible at 375px.
        "filters_more_active": (
            sum(1 for v in (year, region, track) if v) + (1 if firm_slugs else 0)
        ),
        "dash": dash,
        "all_firms": all_firms,
        "selected": selected,
        "has_filters": any([role, year, region, track, provider, query]) or bool(firm_slugs),
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


@login_required
@require_POST
def track_opportunity(request, pk):
    """Set (or clear) the current user's track status for one opportunity.
    `status=clear` removes the row; any of _TRACK_STATES upserts it. Returns
    the re-rendered control for an htmx swap."""
    from analytics.models import UserOpportunity

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

    # The feed swaps just the one card's control (htmx); the My Applications
    # page posts a plain form and wants a redirect back to itself.
    if request.headers.get("HX-Request"):
        if status == "dismiss":
            # The card's own target is `closest .rolecard`, so an empty body
            # removes the row from the feed. Anything else here would leave a
            # control behind on a card the user just said was not for them.
            return HttpResponse("")
        return _track_control(request, opp)
    from django.shortcuts import redirect, resolve_url
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


def _lens_item(uo, *, today):
    """One row as it appears in a deadline lens (Closing Soon / Rolling).

    Carries its funnel stage with it — that label is what stops the lens from
    reading as a separate pile of roles."""
    o = uo.opportunity
    stage = uo.applied_status or "saved"
    days_left = (o.deadline - today).days if o.deadline else None
    return {
        "id": o.id,
        "firm_name": o.firm.name,
        "title": o.title,
        "url": o.url,
        "location": o.location,
        "stage": stage,
        "stage_label": _STAGE_LABELS.get(stage, stage.title()),
        "deadline": deadline_marker(o.deadline, o.deadline_precision, today=today),
        "days_left": days_left,
        "urgency": _urgency_band(days_left),
    }


@login_required
def my_applications(request):
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
    put a dead role at the top of the page."""
    from analytics.models import UserOpportunity

    today = timezone.localdate()
    rows = list(
        UserOpportunity.objects.for_user(request.user)
        .filter(dismissed=False)
        .select_related("opportunity", "opportunity__firm")
        .order_by("opportunity__firm__name", "opportunity__title")
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
            "items": groups[key],
            "count": len(groups[key]),
            "pct": round(100 * len(groups[key]) / biggest) if biggest else 0,
        }
        for key, label in _STAGES
    ]

    # The lenses read the LIVE rows only (everything that isn't Done).
    live = [uo for uo in rows if (uo.applied_status or "saved") != TRACK_CLOSED]
    closing = [
        _lens_item(uo, today=today) for uo in live
        if is_closing_soon(uo.opportunity.deadline, today=today)
    ]
    closing.sort(key=lambda i: (i["days_left"], i["firm_name"].lower()))
    # Rolling is defined by the ABSENCE of a deadline, not by a far-off one: a
    # role with a real deadline three months out is dated, just not urgent, and
    # calling it rolling would be a small lie about the posting. Rolling roles
    # carry no countdown and are never styled as overdue.
    rolling = [
        _lens_item(uo, today=today) for uo in live
        if uo.opportunity.deadline is None
    ]

    lenses = [
        {
            "key": "closing",
            "label": "Closing Soon",
            "items": closing,
            "note": f"Deadline inside the next {CLOSING_SOON_DAYS} days.",
        },
        {
            "key": "rolling",
            "label": "Rolling",
            "items": rolling,
            "note": "No posted deadline. Reviewed as they arrive, so apply early.",
        },
    ]
    # Roles the student marked "not for me". They live here rather than in
    # the feed for the obvious reason, but they must live SOMEWHERE: a hidden
    # thing with no way back is a decision the product made permanent on the
    # user's behalf.
    hidden = list(
        UserOpportunity.objects.for_user(request.user)
        .filter(dismissed=True)
        .select_related("opportunity", "opportunity__firm")
        .order_by("opportunity__firm__name", "opportunity__title")
    )
    return render(request, "directory/my_applications.html", {
        "stages": stages,
        "lenses": lenses,
        "total": len(rows),
        "live_total": len(live),
        "closing_soon_days": CLOSING_SOON_DAYS,
        "hidden": hidden,
    })


def firm_detail(request, slug):
    """A single firm's page: its open openings plus its cycle timeline
    (firm_dates, confirmed vs rumored)."""
    firm = get_object_or_404(Firm, slug=slug)
    now = timezone.now()
    today = timezone.localdate()

    # Campus buckets first (insight, internship, entry_level), experienced
    # rows after — the firm page shows everything but leads with the roles
    # the product is for.
    opps = firm.opportunities.filter(status="open").select_related("firm").order_by(
        _BUCKET_ORDER, F("deadline").asc(nulls_last=True), "title"
    )
    context = {
        "firm": firm,
        "cards": [_card(o, now=now, today=today) for o in opps],
        "timeline": _timeline(firm, today=today),
        "total": opps.count(),
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

    rows = list(
        Contact.objects.for_user(user)
        .filter(firm=firm, archived=False)
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
    return {
        "my_contacts": people,
        "my_total": len(people),
        "my_advocates": sum(1 for c in rows if c.warmth == "advocate"),
        # The one person worth opening first: warmest, and among equals the
        # one who has waited longest to hear from you.
        "my_next": max(
            people,
            key=lambda p: (-_WARMTH_RANK.get(p["c"].warmth, 9), p["days_since"] or 0),
        )["c"] if people else None,
    }
