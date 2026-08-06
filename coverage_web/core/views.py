from urllib.parse import quote

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from directory.classify import TARGET_BUCKETS
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
    """Pricing page. One real tier (free) and one honest preview (Pro), with
    the same live counts the landing uses so the free column states exactly
    what it holds."""
    return render(
        request,
        "core/pricing.html",
        {
            "open_count": Opportunity.objects.filter(
                status="open", bucket__in=TARGET_BUCKETS
            ).count(),
            "firm_count": Firm.objects.count(),
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


@require_GET
def search(request):
    """The Cmd-K palette's data: contacts, firms, and open roles in one query.

    One endpoint rather than three because the palette's question is "take me
    to the thing named X" and the user should not have to know which table X
    lives in. Contacts are tenant-scoped and need a login; firms and roles are
    shared-zone. Everything is capped, `icontains`, newest-effort-first — a
    palette is navigation, not a report.
    """
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
