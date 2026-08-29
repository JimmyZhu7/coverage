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
from django.contrib.auth import get_user_model
from django.db.models import Count, Max
from django.db.models.functions import TruncDate
from django.shortcuts import render
from django.utils import timezone

from accounts.views import ONBOARDING_STEPS
from capture.models import GmailConnection
from crm.models import Contact, Touch
from directory.classify import TARGET_BUCKETS
from directory.models import Opportunity, ScrapeRun

from .models import ProductEvent

_WINDOW_DAYS = 30

# How many of a person's most recent events the drilldown prints. Fifty is
# roughly one sitting: enough to read a session forward from the page they
# landed on, short enough that the page stays a page.
_DRILLDOWN_EVENTS = 50

# The three events that prove a person REACHED a wizard step. Each carries the
# step as a prop (accounts/views.py); "furthest step" is the highest index any
# of them named. `completed` alone would undercount — a student sitting on
# step 2 having never pressed Continue has still got further than one who
# never opened it — and `skipped` counts because declining a question means
# you saw it.
_STEP_EVENTS = (
    "onboarding_step_viewed",
    "onboarding_step_completed",
    "onboarding_step_skipped",
)


# ---------------------------------------------------------------------------
# The pilot table.
#
# This page's own docstring promised it: "when Coverage has users beyond its
# founder this is where retention lives, so the queries are written
# per-user-capable". The per-user view was anticipated and never built, and the
# gap it left is the whole reason a pilot could not run — every number above is
# a total, and a total across ten students cannot tell you that four of them
# stopped on the same step. "Is this an MVP" gets a no until a stranger's stall
# has a row.
#
# Read-only by construction. Every query below is a SELECT on rows some other
# code path already wrote; nothing on this page creates, and the events it
# reads were recorded by the wizard and the OAuth callback at the moment the
# thing happened.
#
# Staff-only, inherited from `dashboard`'s own `@staff_member_required` — these
# are helpers, never routed, and adding a second entry point to real students'
# behaviour is not a thing this file should be able to do by accident.
def _pilot_rows(now):
    """One row per non-staff account, oldest signup first.

    Non-staff is the filter because the founder is the one account with
    thousands of events and a wizard he wrote; leaving him in the table means
    every column's eye-line is his row rather than the students'. He is still
    reachable by `?user=<id>` — the drilldown takes an id, not a list position.

    Grouped queries, not a loop of counts: ten users is nothing, but a table
    that goes quadratic the week the pilot works is a table that gets deleted
    the week the pilot works.
    """
    users = list(get_user_model().objects.filter(is_staff=False).order_by("date_joined"))
    ids = [u.id for u in users]
    if not ids:
        return []

    # `all_objects` throughout: this is the one page in Coverage that reads
    # ACROSS tenants on purpose, and it does so behind the staff gate. Each
    # query is still explicitly scoped to the ids of the rows being tabulated
    # rather than reading the whole table and grouping afterwards.
    contacts = {
        r["user"]: r["n"]
        for r in Contact.all_objects.filter(user_id__in=ids, archived=False)
        .values("user").annotate(n=Count("id"))
    }
    touches = {
        r["user"]: r["n"]
        for r in Touch.all_objects.filter(user_id__in=ids)
        .values("user").annotate(n=Count("id"))
    }
    last_seen = {
        r["user"]: r["last"]
        for r in ProductEvent.all_objects.filter(user_id__in=ids)
        .values("user").annotate(last=Max("ts"))
    }
    connected = set(
        GmailConnection.all_objects.filter(user_id__in=ids)
        .values_list("user_id", flat=True)
    )

    # Furthest step, computed in Python off the props rather than in SQL: the
    # step is a JSON key, and the ordering that matters is ONBOARDING_STEPS'
    # own, which no database ordering knows.
    furthest: dict[int, int] = {}
    for uid, props in ProductEvent.all_objects.filter(
        user_id__in=ids, event__in=_STEP_EVENTS
    ).values_list("user_id", "props"):
        step = (props or {}).get("step")
        if step in ONBOARDING_STEPS:
            idx = ONBOARDING_STEPS.index(step)
            if idx > furthest.get(uid, -1):
                furthest[uid] = idx

    rows = []
    for user in users:
        last = last_seen.get(user.id)
        idx = furthest.get(user.id)
        rows.append({
            "user": user,
            "signed_up": user.date_joined,
            # Blank, not "profile", when nothing was recorded. An account that
            # predates this instrumentation reached steps nobody wrote down,
            # and printing step 1 for it would be inventing a fact.
            "step": ONBOARDING_STEPS[idx] if idx is not None else "",
            "step_number": (idx + 1) if idx is not None else None,
            "onboarded_at": user.onboarded_at,
            "gmail": user.id in connected,
            "contacts": contacts.get(user.id, 0),
            "touches": touches.get(user.id, 0),
            "last_seen": last,
            "idle_days": (now - last).days if last else None,
        })
    return rows


def _pilot_drilldown(user_id: str, now):
    """One person's last `_DRILLDOWN_EVENTS` events, oldest first — the
    "watch a stranger's session after the fact" view.

    Newest N, then reversed, so the page reads the way the session happened:
    landed here, tried that, hit this error, left. Sorted `-ts, -id` before the
    slice because `ts` is `auto_now_add` and several events inside one request
    can share a timestamp to the microsecond; without the id tiebreak the two
    halves of a failed POST could print in either order.
    """
    if not (user_id or "").isdigit():
        return None
    subject = get_user_model().objects.filter(pk=int(user_id)).first()
    if subject is None:
        return None
    events = list(
        ProductEvent.all_objects.filter(user_id=subject.id).order_by("-ts", "-id")[
            :_DRILLDOWN_EVENTS
        ]
    )
    events.reverse()
    return {
        "user": subject,
        "events": events,
        "cap": _DRILLDOWN_EVENTS,
        "truncated": len(events) == _DRILLDOWN_EVENTS,
        "total": ProductEvent.all_objects.filter(user_id=subject.id).count(),
        "now": now,
    }


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
        "pilot": _pilot_rows(now),
        "drilldown": _pilot_drilldown(request.GET.get("user") or "", now),
        "onboarding_step_total": len(ONBOARDING_STEPS),
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
