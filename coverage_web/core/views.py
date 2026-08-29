from types import SimpleNamespace
from urllib.parse import quote

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from billing import credits as billing_credits
from directory.classify import REGION_LABELS, TARGET_BUCKETS, TRACKED_REGIONS
from directory.models import Firm, Opportunity


# Recognizable names for the landing page's firm strip, in display order.
# Only slugs that exist in the DB render, so a missing firm never 404s the
# strip — it just gets shorter.
_STRIP_SLUGS = [
    "gs", "jpm", "bofa", "citi", "barclays", "mckinsey", "bain", "bcg",
    "blackstone", "kkr", "apollo", "janestreet", "point72", "blackrock",
    "eqt", "cicc",
]


def home(request):
    """Landing page. The two ledger figures are read-only counts over the
    shared zone — live campus roles (the three target buckets) and firms
    tracked — so the hero states what the instrument actually holds, not a
    marketing number.

    `region_names` is read from TRACKED_REGIONS/REGION_LABELS rather than
    typed into the template for the same reason: the Opportunities feature
    bullet named four markets ("Hong Kong, US, Singapore, Europe") for over
    a month after Mainland China and Japan joined TRACKED_REGIONS
    (5329e15) — the exact "four markets while the board tracked six" drift
    core.views.pricing's own docstring already warns about, just on the
    other page.
    """
    firms_by_slug = {
        f.slug: f for f in Firm.objects.filter(slug__in=_STRIP_SLUGS)
    }
    strip_firms = [firms_by_slug[s] for s in _STRIP_SLUGS if s in firms_by_slug]
    return render(
        request,
        "core/home.html",
        {
            "open_count": Opportunity.objects.filter(
                status="open", bucket__in=TARGET_BUCKETS
            ).count(),
            "firm_count": Firm.objects.count(),
            "strip_firms": strip_firms,
            "region_names": [REGION_LABELS[r] for r in TRACKED_REGIONS],
        },
    )


def _advisor_daily_cap(plan: str) -> int:
    """The real number of chat turns a student on `plan` can send before
    `billing.credits.can_spend`'s daily-burst guard cuts them off for the
    day — NOT `assistant.plans.limits_for(...).daily_cap`, which stopped
    being what gates a turn the moment the credit system
    (docs/credit-system-plan.md) replaced it; `assistant/plans.py`'s own
    docstring says so: "It no longer gates anything in agent.py." A message
    costs `message_cost` credits, and `can_spend` blocks once today's spend
    reaches `daily_burst`, so the real ceiling is that ratio's floor —
    reading the stale `daily_cap` instead overstated Pro's real daily
    allowance by 4x (60 shown vs. 45 // 3 = 15 actually enforced) once
    Pro's message_cost rose above 1.
    """
    config = billing_credits.plan_config(SimpleNamespace(plan=plan))
    cost = config["message_cost"]
    return config["daily_burst"] // cost if cost > 0 else 0


def _advisor_monthly_grant(plan: str) -> int:
    """Credits granted to `plan` each month — the pool that funds chat turns
    AND the Gmail residue scans, so it is the only figure on the page that
    describes Pro's real depth advantage.

    The page must not sell Pro as "more messages": a Pro message costs 3
    credits against a 45-credit daily burst, so both plans compute to the
    same 15 a day (see `_advisor_daily_cap`). The monthly grant is where the
    3x actually lives (60 vs 180), and like every other number here it is
    read from `CREDIT_PLANS` rather than typed into the template.
    """
    return billing_credits.plan_config(SimpleNamespace(plan=plan))["monthly_grant"]


def pricing(request):
    """Pricing page. One real tier (free) and one honest preview (Pro).

    Every number in the Free column is measured here rather than written into
    the template, because a pricing page's claims are the ones most likely to
    go stale and least likely to be re-read: the page spent this cycle
    advertising "four markets" while the board had tracked six for weeks.

    `firm_count` is firms WITH AN OPEN ROLE, not every firm on file. 119 firms
    are configured; 72 are actually hiring today, and the free plan should
    promise what a visitor will find rather than the size of the catalogue.
    """
    campus = Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
    return render(
        request,
        "core/pricing.html",
        {
            "open_count": campus.count(),
            "firm_count": campus.values("firm_id").distinct().count(),
            "market_count": len(TRACKED_REGIONS),
            "read_count": campus.exclude(raw__detail_text=None).count(),
            "sponsorship_count": campus.exclude(
                sponsorship__in=["", "unknown"]).count(),
            # Same rule as the counts above — the advisor's per-plan caps are
            # read from the credit system, the one place that actually
            # enforces them today (see _advisor_daily_cap), never typed into
            # the template.
            "advisor_free_cap": _advisor_daily_cap("free"),
            "advisor_pro_cap": _advisor_daily_cap("pro"),
            "advisor_free_grant": _advisor_monthly_grant("free"),
            "advisor_pro_grant": _advisor_monthly_grant("pro"),
        },
    )


@require_GET
def healthz(request):
    """Liveness check. 200 JSON whenever the app process can serve requests.

    Deliberately does not touch the database: a healthz endpoint should
    reflect whether the process itself is up, not whether every dependency is
    reachable. Deeper readiness checks can be added later as a separate route
    if a PaaS health check ever needs them.
    """
    return JsonResponse({"status": "ok"})


# The palette debounces at 140ms, so a human peaks around 7 requests a second
# in a burst and stops. The window is sized for that shape — bursts are free,
# sustained hammering is not — because this endpoint runs three ILIKE queries
# per call and is reachable signed-out. Cache-based and per-IP: no new
# dependency, no table, resets by itself.
_SEARCH_WINDOW_SECONDS = 10
_SEARCH_WINDOW_LIMIT = 40


def _search_throttled(request) -> bool:
    ip = (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
          or request.META.get("REMOTE_ADDR", "unknown"))
    key = f"search-rate:{ip}"
    burst = cache.get_or_set(key, 0, _SEARCH_WINDOW_SECONDS)
    if burst >= _SEARCH_WINDOW_LIMIT:
        return True
    try:
        cache.incr(key)
    except ValueError:
        # The key expired between read and increment: the window reset, so
        # this request starts the next one rather than being counted at all.
        cache.set(key, 1, _SEARCH_WINDOW_SECONDS)
    return False


@require_GET
def search(request):
    """The Cmd-K palette's data: contacts, firms, and open roles in one query.

    One endpoint rather than three because the palette's question is "take me
    to the thing named X" and the user should not have to know which table X
    lives in. Contacts are tenant-scoped and need a login; firms and roles are
    shared-zone. Everything is capped, `icontains`, newest-effort-first — a
    palette is navigation, not a report.
    """
    if _search_throttled(request):
        return HttpResponse("rate limited", status=429)

    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"contacts": [], "firms": [], "roles": []})

    out = {"contacts": [], "firms": [], "roles": []}

    if request.user.is_authenticated:
        from crm.models import Contact
        out["contacts"] = [
            {
                "name": c.name,
                "firm": c.firm.name if c.firm else c.firm_text,
                "warmth": c.warmth,
                "url": f"/app/contacts/{c.id}/",
            }
            for c in Contact.objects.for_user(request.user)
            .filter(archived=False, name__icontains=q)
            .select_related("firm")
            .order_by("name")[:8]
        ]

    out["firms"] = [
        {"name": f.name, "url": f"/firms/{f.slug}/"}
        for f in Firm.objects.filter(name__icontains=q).order_by("name")[:6]
    ]

    # A role we hold the description for opens on OUR page, at that role's
    # card, with the drawer already open. Sending a search result out to a
    # Workday shell that paints in four seconds — for text sitting in our own
    # database — was the palette undoing the drawer's entire reason to exist.
    # Roles we have not read still link out, because for those the firm's page
    # genuinely is the only copy.
    out["roles"] = [
        {
            "title": o.title,
            "firm": o.firm.name,
            "url": (f"/opportunities/?q={quote(o.title[:60])}&read={o.id}"
                    if (o.raw or {}).get("detail_text") else o.url),
            "external": not (o.raw or {}).get("detail_text"),
        }
        for o in Opportunity.objects.filter(
            status="open", bucket__in=TARGET_BUCKETS, title__icontains=q
        )
        .select_related("firm")
        .order_by("firm__name", "title")[:8]
    ]
    return JsonResponse(out)


@require_GET
def favicon(request):
    """Serve the real .ico AT /favicon.ico, with no redirect in the way.

    Safari is the reason this is a view instead of a RedirectView. It checks
    /favicon.ico by default and is unreliable about following a redirect to
    get there — and a redirect is what the previous two attempts at this bug
    left in place. Serving the bytes directly removes the hop entirely, which
    also costs nothing: it is one small file behind the same static finders
    every other asset uses, so it keeps working after collectstatic.
    """
    path = finders.find("img/favicon.ico")
    if path is None and settings.STATIC_ROOT:
        candidate = settings.STATIC_ROOT / "img" / "favicon.ico"
        path = str(candidate) if candidate.is_file() else None
    if path is None:
        raise Http404("favicon.ico is missing from static files")
    resp = FileResponse(open(path, "rb"), content_type="image/x-icon")
    # A week, not "permanent": the last cache decision here was a 301 that
    # would have been expensive to take back.
    resp["Cache-Control"] = "public, max-age=604800"
    return resp
