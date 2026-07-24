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

from django.db.models import Case, F, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

# Read-only, cross-app import (build-plan.md §2's private zone). directory
# never writes crm rows; the opportunities feed only reads UserFirm via the
# tenant-scoped manager. No import cycle: crm.models imports directory.models.
from crm.models import UserFirm
from directory.classify import (
    BUCKET_LABELS, INSIGHT, OTHER, REGION_LABELS, REGION_ORDER, TARGET_BUCKETS,
)
from directory.models import Firm, Opportunity, ScrapeRun
from directory.timeline import EVENT_LABELS

# Past this many days without a re-verification, a listing wears a visible
# "may be stale" flag. The staleness banner is a feature, not noise
# (build-plan.md §7/§10, risk #6): we tell on ourselves rather than let a
# quietly-rotting date pass for fresh.
STALE_AFTER_DAYS = 14

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


def staleness_marker(last_verified, last_checked, *, now=None):
    """Age of the most recent verification, plus a stale flag past
    STALE_AFTER_DAYS. Falls back to last_checked when last_verified is
    absent; if neither exists, the listing is honestly "not yet verified".
    """
    now = now or timezone.now()
    verified = last_verified or last_checked
    if verified is None:
        return {"label": "not yet verified", "is_stale": True, "days": None, "verified": None}
    days = (now - verified).days
    if days <= 0:
        label = "verified today"
    elif days == 1:
        label = "verified 1 day ago"
    else:
        label = f"verified {days} days ago"
    return {"label": label, "is_stale": days > STALE_AFTER_DAYS, "days": days, "verified": verified}


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


def sponsorship_marker(sponsorship):
    """Tri-state, labelled honestly. "unknown" is a legitimate answer this
    early (see directory/models.py's note) — not silently rounded to "no".
    """
    s = (sponsorship or "unknown").lower()
    return {
        "yes": {"level": "known", "label": "sponsors visas"},
        "no": {"level": "none", "label": "no visa sponsorship"},
        "unknown": {"level": "unknown", "label": "sponsorship unknown"},
    }.get(s, {"level": "unknown", "label": f"sponsorship: {s}"})


def _class_of(bucket: str, cohort: str) -> str:
    """Display heuristic: a Summer 2027 internship (or insight cohort)
    targets students graduating the following year; an entry-level start
    year IS the graduation year. Empty cohort -> no tag, never a guess."""
    if not (cohort or "").isdigit():
        return ""
    year = int(cohort)
    if bucket in (INSIGHT, "internship"):
        year += 1
    return f"Class of {year}"


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
    student-facing trio: firm category, class year, sponsorship."""
    bucket = opp.bucket or OTHER
    category = FIRM_CATEGORIES.get(opp.firm.slug) or next(
        (TRACK_LABELS.get(t, "") for t in (opp.firm.tracks or [])), ""
    )
    tags = []
    if category:
        tags.append({"label": category, "css": "tag-cat"})
    class_of = _class_of(bucket, opp.cohort)
    if class_of:
        tags.append({"label": class_of, "css": "tag-class"})
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
        "cohort": opp.cohort,
        "deadline": deadline_marker(opp.deadline, opp.deadline_precision, today=today),
        "tags": tags,
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
        "firm_name": o.firm.name,
        "firm_slug": o.firm.slug,
        "monogram": "".join(p[0] for p in name_parts[:2]).upper() or "?",
        "category": FIRM_CATEGORIES.get(o.firm.slug, ""),
        "title": o.title,
        "url": o.url,
        "location": o.location,
        "bucket": bucket,
        "bucket_label": BUCKET_LABELS.get(bucket, bucket),
        "cohort": o.cohort,
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

    # Role counts reflect every OTHER active filter, so the select's numbers
    # answer "under my current filters, how many of each?" honestly.
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

    # Widget row: total open campus roles, roles at the user's own firms,
    # and their application funnel. Anonymous visitors see an em-dash for
    # the personal figures, not a zero pretending to be data.
    campus_all = open_qs.filter(bucket__in=TARGET_BUCKETS)
    dash = {
        "total_open": campus_all.count(),
        "for_you": None,
        "funnel": None,
    }
    if request.user.is_authenticated:
        my_firm_ids = list(
            UserFirm.objects.for_user(request.user).values_list("firm_id", flat=True)
        )
        dash["for_you"] = campus_all.filter(firm_id__in=my_firm_ids).count()
        from analytics.models import UserOpportunity  # local: avoids app-load order games

        uo = UserOpportunity.objects.for_user(request.user)
        dash["funnel"] = {
            "submitted": uo.filter(applied_status__iexact="submitted").count(),
            "interview": uo.filter(applied_status__iexact="interview").count(),
            "offer": uo.filter(applied_status__iexact="offer").count(),
        }

    # Urgency headline stats for the hero.
    dash["closing_week"] = sum(1 for i in feed["closing"] if i["days_left"] is not None and i["days_left"] <= 7)
    dash["closing_next"] = feed["closing"][0] if feed["closing"] else None
    dash["rolling_total"] = feed["rolling_total"]
    dash["fresh_count"] = feed["fresh_count"]

    # Every covered firm (with an open campus role), for the multi-select.
    all_firms = [
        {"slug": s, "name": n}
        for s, n in open_qs.filter(bucket__in=TARGET_BUCKETS)
        .order_by("firm__name").values_list("firm__slug", "firm__name").distinct()
    ]

    context = {
        "feed": feed,
        "clusters": cluster_list,
        "total": total,
        "facets": facets,
        "role_facet": role_facet,
        "hidden_other": hidden_other,
        "dash": dash,
        "all_firms": all_firms,
        "selected": {
            "role": role,
            "region": region,
            "track": track,
            "provider": provider,
            "firm": firm_slugs,
            "q": query,
        },
        "has_filters": any([role, region, track, provider, query]) or bool(firm_slugs),
        "latest_run": ScrapeRun.objects.order_by("-started").first(),
        "stale_after_days": STALE_AFTER_DAYS,
    }

    if request.headers.get("HX-Request"):
        return render(request, "directory/_results.html", context)
    return render(request, "directory/opportunities.html", context)


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
        "stale_after_days": STALE_AFTER_DAYS,
    }
    return render(request, "directory/firm_detail.html", context)
