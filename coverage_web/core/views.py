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
