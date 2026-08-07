"""The founder dashboard — /analytics/, staff only.

ProductEvent has been written on every meaningful action since the cutover
and read by exactly one thing: the CSV export. This page is the reader. It
answers the questions a founder dogfooding a product actually has — is the
board growing, is the pipeline still running, am I using the thing I built —
and it answers them from rows the app already writes, inventing no metric.

Staff-only and deliberately plain: it is an instrument, not a surface. When
Coverage has users beyond its founder this is where retention lives, so the
queries are written per-user-capable (`values("user")`) rather than assuming
one row.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.shortcuts import render
from django.utils import timezone

from crm.models import Contact, Touch
from directory.classify import TARGET_BUCKETS
from directory.models import Opportunity, ScrapeRun

from .models import ProductEvent

_WINDOW_DAYS = 30


@staff_member_required
def dashboard(request):
    now = timezone.now()
    since = now - timedelta(days=_WINDOW_DAYS)

    events = ProductEvent.all_objects.filter(ts__gte=since)
    by_day = {
        row["day"]: row["n"]
        for row in events.annotate(day=TruncDate("ts"))
        .values("day").annotate(n=Count("id"))
    }
    days = [(now - timedelta(days=i)).date() for i in range(_WINDOW_DAYS - 1, -1, -1)]
    busiest = max(by_day.values(), default=0)
    activity = [{"day": d, "n": by_day.get(d, 0),
                 "pct": round(100 * by_day.get(d, 0) / busiest) if busiest else 0,
                 "label": d.strftime("%b %-d")}
                for d in days]

    # The pipeline's own runs, newest first per stage — the honest answer to
    # "is the automation still running", which no other page states.
    stages = []
    for connector, label in (("all", "Scrape"), ("enrich", "Enrich"),
                             ("extract", "Extract"), ("reverify", "Reverify")):
        run = ScrapeRun.objects.filter(connector=connector).order_by("-started").first()
        stages.append({
            "label": label,
            "when": run.started if run else None,
            "status": run.status if run else "never run",
            "stats": run.stats if run else {},
        })

    campus = Opportunity.objects.filter(status="open", bucket__in=TARGET_BUCKETS)
    return render(request, "analytics/dashboard.html", {
        "window_days": _WINDOW_DAYS,
        "activity": activity,
        "event_total": events.count(),
        "top_events": Counter(events.values_list("event", flat=True)).most_common(8),
        "stages": stages,
        "board": {
            "open_campus": campus.count(),
            "dated": campus.exclude(deadline=None).count(),
            "with_text": campus.filter(raw__detail_text__isnull=False).count(),
            "firms": campus.values("firm").distinct().count(),
        },
        "crm": {
            "contacts": Contact.all_objects.filter(archived=False).count(),
            "touches_30d": Touch.all_objects.filter(ts__gte=since).count(),
            "with_opener": Contact.all_objects.filter(archived=False)
                           .exclude(opener="").count(),
        },
    })
