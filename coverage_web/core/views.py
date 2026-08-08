from urllib.parse import quote

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from directory.classify import TARGET_BUCKETS, TRACKED_REGIONS
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
    marketing number."""
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
        },
    )


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
    from django.http import JsonResponse

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
