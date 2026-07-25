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
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from analytics.events import record_event
# Read-only, cross-app import (build-plan.md §2's private zone). directory
# never writes crm rows; the opportunities feed only reads UserFirm via the
# tenant-scoped manager. No import cycle: crm.models imports directory.models.
from crm.models import UserFirm
from directory.classify import (
    BUCKET_LABELS, OTHER, REGION_LABELS, REGION_ORDER, TARGET_BUCKETS,
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
    fall back to the firm-level fact from the seed (sponsors: true/false).
    Still unknown -> no pill rather than a hedge."""
    s = (opp.sponsorship or "unknown").lower()
    if s == "yes":
        return {"label": "Sponsorship", "css": "spon-known"}
    if s == "no":
        return {"label": "No Sponsorship", "css": "spon-none"}
    firm_fact = opp.firm.sponsors
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
    return {
        "id": c.id,
        "firm_name": c.firm_name,
        "firm_slug": c.firm_slug,
        "monogram": _monogram(c.firm_name),
        "title": c.title,
        "url": c.url,
        "location": c.location,
        "deadline": c.deadline,
        "score": rec.score,
        "reasons": rec.reasons,
    }


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
        "cycle": fd.cycle,
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


def _facets(open_qs):
    """Filter options drawn from the live open set. Opportunity-level region
    is frequently blank on raw board data, so the firm's own regions stand in
    — that's what makes the filter actually bite against real scraped rows.
    Track comes from the firm's vertical (`Firm.tracks`: ib/consulting/...);
    role type is the opportunity's own classified `bucket` and is a separate
    facet — mixing the two dimensions into one "track" select is what this
    replaced."""
    regions, tracks, providers = set(), set(), set()
    for opp in open_qs:
        if opp.region in REGION_ORDER:
            regions.add(opp.region)
        for t in (opp.firm.tracks or []):
            tracks.add(t)
        if opp.source:
            providers.add(opp.source)
    return {
        # Region is one of the four target markets, derived from each role's
        # own location — shown in a fixed order, only those actually present.
        "regions": [{"value": r, "label": REGION_LABELS[r]} for r in REGION_ORDER if r in regions],
        "tracks": [
            {"value": t, "label": TRACK_LABELS.get(t, t)}
            for t in sorted(tracks, key=lambda t: TRACK_LABELS.get(t, t))
        ],
        "providers": sorted(providers),
    }


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


def _year_facet(qs):
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
    return [
        {"value": "", "label": "Any Year", "count": total},
        *[
            {"value": y, "label": y, "count": counts[y]}
            for y in sorted(counts, reverse=True)
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


def _urgency_item(o, *, now, today, my_firm_ids):
    """One feed card: firm identity + the honest urgency signal for this
    role (a real countdown when dated, freshness when rolling)."""
    name_parts = [p for p in o.firm.name.split() if p[:1].isalnum()]
    bucket = o.bucket or OTHER
    seen_days = (now - o.first_seen).days if o.first_seen else None
    item = {
        "id": o.id,
        "firm_name": o.firm.name,
        "firm_slug": o.firm.slug,
        "monogram": "".join(p[0] for p in name_parts[:2]).upper() or "?",
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
    }
    if o.deadline and o.deadline >= today:
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
        item.update({"dated": False, "days_left": None, "level": "rolling"})
    return item


def _urgency_feed(qs, *, now, today, my_firm_ids):
    """Rank the filtered set into the Closing-Soon and Fresh-&-Rolling bands.
    Dated roles sort by nearest deadline; rolling roles sort by your-firm
    first, then freshest-seen, then this-cycle cohort."""
    closing, rolling = [], []
    for o in qs:
        item = _urgency_item(o, now=now, today=today, my_firm_ids=my_firm_ids)
        (closing if item["dated"] else rolling).append(item)

    closing.sort(key=lambda i: (not i["is_mine"], i["days_left"]))

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
    facets = _facets(open_qs)

    role = request.GET.get("role", "").strip()
    # Programme/intake year, or `none` for the rows that state no year at all.
    year = request.GET.get("year", "").strip()
    region = request.GET.get("region", "").strip()
    track = request.GET.get("track", "").strip()
    provider = request.GET.get("provider", "").strip()
    # Multi-select firm filter: any number of ?firm=<slug> params.
    firm_slugs = [s.strip() for s in request.GET.getlist("firm") if s.strip()]
    query = request.GET.get("q", "").strip()

    qs = open_qs
    if region:
        # Location-based: only roles whose own location resolves to this market
        # (a title claiming "EMEA" doesn't count if the location says otherwise).
        qs = qs.filter(region__iexact=region)
    if track:
        qs = qs.filter(firm__tracks__contains=[track])
    if provider:
        qs = qs.filter(source__iexact=provider)
    if firm_slugs:
        qs = qs.filter(firm__slug__in=firm_slugs)
    if query:
        qs = qs.filter(
            Q(title__icontains=query)
            | Q(firm__name__icontains=query)
            | Q(location__icontains=query)
        )

    # Each facet's counts reflect every OTHER active filter, so its numbers
    # answer "under my current filters, how many of each?" honestly. That
    # means the two cross-cutting selects are counted against each other's
    # applied filter, not against a shared pre-filter set: role counts see the
    # year selection, year counts see the role selection. Two extra values()
    # scans over an already-narrowed queryset, for numbers that don't lie when
    # both selects are in use.
    year_facet = _year_facet(_apply_role_filter(qs, role))
    qs = _apply_year_filter(qs, year)

    bucket_counts = Counter(
        (b or OTHER) for b in qs.values_list("bucket", flat=True)
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
    hidden_other = bucket_counts.get(OTHER, 0) if role == "" else 0

    qs = _apply_role_filter(qs, role)

    # The urgency feed is the star: rank what to act on NOW. Two honest
    # bands, because the data has two kinds of urgency:
    #   1. Closing Soon — the few roles with a real posted deadline (a true
    #      countdown).
    #   2. Fresh & Rolling — the many rolling-review roles, where "apply
    #      early" is the whole game, so we rank by how recently WE first saw
    #      the posting (Coverage's own first_seen — a signal no ATS exposes).
    my_firm_ids: set[int] = set()
    if request.user.is_authenticated:
        my_firm_ids = set(
            UserFirm.objects.for_user(request.user).values_list("firm_id", flat=True)
        )
    feed = _urgency_feed(qs, now=now, today=today, my_firm_ids=my_firm_ids)

    # Firm clusters are the page: one firm, all its open roles listed below it
    # in its own scroll window. Each role keeps its honest urgency signal (a
    # real countdown when dated, freshness when rolling), and the whole list is
    # personalized from the survey — the user's target firms and their chosen
    # tracks/regions float to the top.
    user_regions = {r.lower() for r in (getattr(request.user, "regions", None) or [])}
    user_tracks = set(getattr(request.user, "tracks", None) or [])
    tier_by_firm: dict[int, int | None] = {}
    if request.user.is_authenticated:
        tier_by_firm = dict(
            UserFirm.objects.for_user(request.user).values_list("firm_id", "tier")
        )

    # Picked-for-you bar. Deliberately scored over the WHOLE open campus set,
    # not the filtered `qs`: it sits above the filter bar and answers "what
    # should I look at", which a filter narrowing the page below shouldn't
    # silently rewrite. Signed-out (and profile-less) users get no bar at all —
    # `recommend()` returns [] for an empty profile, and the template renders
    # an honest sign-in line instead of six generic cards pretending to be
    # tailored. See recommend.py for the scoring itself.
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

    qs = qs.order_by("firm__name", F("deadline").asc(nulls_last=True), "title")
    clusters: dict[int, dict] = {}
    for o in qs:
        cl = clusters.get(o.firm_id)
        if cl is None:
            category = FIRM_CATEGORIES.get(o.firm.slug) or next(
                (TRACK_LABELS.get(t, "") for t in (o.firm.tracks or [])), ""
            )
            name_parts = [p for p in o.firm.name.split() if p[:1].isalnum()]
            cl = clusters[o.firm_id] = {
                "firm_name": o.firm.name,
                "firm_slug": o.firm.slug,
                # First seeded email domain drives the logo lookup; the
                # monogram is the always-works fallback.
                "domain": (o.firm.domains or [""])[0],
                "monogram": "".join(p[0] for p in name_parts[:2]).upper() or "?",
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
        item = _urgency_item(o, now=now, today=today, my_firm_ids=set(tier_by_firm))
        cl["roles"].append(item)
        if item["dated"]:
            cl["closing_count"] += 1
            cl["next_days"] = (
                item["days_left"] if cl["next_days"] is None
                else min(cl["next_days"], item["days_left"])
            )

    # Roles inside a firm: dated soonest-first, then fresh rolling, then the rest.
    for cl in clusters.values():
        cl["roles"].sort(key=lambda i: (
            not i["dated"],
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

    # The two figures the stat strip actually renders. (The old hero widget's
    # total/for-you/funnel counts were dropped with it — they cost 5 queries a
    # request and nothing displayed them.)
    dash = {
        "closing_week": sum(
            1 for i in feed["closing"]
            if i["days_left"] is not None and i["days_left"] <= 7
        ),
        "fresh_count": feed["fresh_count"],
    }

    # Every covered firm (with an open campus role), for the multi-select.
    all_firms = [
        {"slug": s, "name": n}
        for s, n in open_qs.filter(bucket__in=TARGET_BUCKETS)
        .order_by("firm__name").values_list("firm__slug", "firm__name").distinct()
    ]

    context = {
        "clusters": cluster_list,
        "total": total,
        # Recommendation bar. `picks` empty + `has_profile` true is the honest
        # "nothing clears the bar" state; `has_profile` false is the
        # signed-out / empty-survey state. The template needs to tell those
        # two apart, so both flags travel.
        "picks": picks,
        "has_profile": bool(profile and not profile.is_empty),
        "facets": facets,
        "role_facet": role_facet,
        "year_facet": year_facet,
        "hidden_other": hidden_other,
        "dash": dash,
        "all_firms": all_firms,
        "selected": {
            "role": role,
            "year": year,
            "region": region,
            "track": track,
            "provider": provider,
            "firm": firm_slugs,
            "q": query,
        },
        "has_filters": any([role, year, region, track, provider, query]) or bool(firm_slugs),
    }

    if request.headers.get("HX-Request"):
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


def _lens_item(uo, *, today):
    """One row as it appears in a deadline lens (Closing Soon / Rolling).

    Carries its funnel stage with it — that label is what stops the lens from
    reading as a separate pile of roles."""
    o = uo.opportunity
    stage = uo.applied_status or "saved"
    return {
        "id": o.id,
        "firm_name": o.firm.name,
        "title": o.title,
        "url": o.url,
        "location": o.location,
        "stage": stage,
        "stage_label": _STAGE_LABELS.get(stage, stage.title()),
        "deadline": deadline_marker(o.deadline, o.deadline_precision, today=today),
        "days_left": (o.deadline - today).days if o.deadline else None,
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
    stages = [
        {"key": key, "label": label, "items": groups[key]}
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
    return render(request, "directory/my_applications.html", {
        "stages": stages,
        "lenses": lenses,
        "total": len(rows),
        "live_total": len(live),
        "closing_soon_days": CLOSING_SOON_DAYS,
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
    }
    return render(request, "directory/firm_detail.html", context)
