"""The Calendar: everything with a date on it, in one month grid.

THREE LAYERS, ONE TIMELINE, EACH HONESTLY LABELLED:

1. Coffee chats captured from the mailbox (`CalendarEvent`, source=capture).
2. Events the user typed in (`CalendarEvent`, source=manual).
3. CONFIRMED firm deadlines (`FirmDate`, read-only here).

Layer 3 is read-only on purpose: firm dates are shared directory data, not
the user's, so the calendar shows them and the weekly radar owns them. Only
dates the scan marked `confirmed_official` appear — the cadence engine acts
on that same bar, and a calendar that mixed rumours into it would put
countdowns on the page for events nobody has confirmed.
"""

from __future__ import annotations

import calendar as calmod
from datetime import date, datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from directory.models import FirmDate

from .forms import CalendarEventForm
from .models import CalendarEvent

# Monday-first weeks: every market this product covers starts its week on a
# Monday, and the cadence engine's business-day maths already assumes it.
_CAL = calmod.Calendar(firstweekday=0)


def _month_bounds(year: int, month: int):
    first = date(year, month, 1)
    last = date(year, month, calmod.monthrange(year, month)[1])
    return first, last


def _shift(year: int, month: int, by: int) -> tuple[int, int]:
    m = month - 1 + by
    return year + m // 12, m % 12 + 1


def _events_by_day(user, first: date, last: date) -> dict[date, list[dict]]:
    """Every layer, bucketed by the local day it falls on.

    Bucketing uses `timezone.localtime` rather than the stored UTC date: an
    8pm Hong Kong chat is stored as noon UTC, and putting it on the UTC day
    would show it a day early for exactly the user whose calendar it is.
    """
    buckets: dict[date, list[dict]] = {}

    def clock(at) -> str:
        """A time short enough to sit in a calendar chip: "9am", "12:30pm".

        Django's `time:"g:ia"` renders "12:00p.m." — three glyphs of noise on
        every on-the-hour entry, in the narrowest column on the page.
        """
        minutes = f":{at.minute:02d}" if at.minute else ""
        return f"{at.strftime('%I').lstrip('0') or '12'}{minutes}{at.strftime('%p').lower()}"

    # Layers 1 + 2 — the user's own events. `starts_at` is a datetime, so the
    # window is widened by a day at each end before filtering to be sure a
    # local-midnight event at either edge is caught.
    window_start = timezone.make_aware(datetime.combine(first - timedelta(days=1), datetime.min.time()))
    window_end = timezone.make_aware(datetime.combine(last + timedelta(days=2), datetime.min.time()))
    for ev in (CalendarEvent.objects.for_user(user)
               .filter(starts_at__gte=window_start, starts_at__lt=window_end)
               .select_related("contact")):
        local = timezone.localtime(ev.starts_at)
        day = local.date()
        if not (first <= day <= last):
            continue
        buckets.setdefault(day, []).append({
            "at_label": "" if ev.all_day else clock(local),
            "id": ev.id,
            "kind": ev.kind,
            "source": ev.source,
            "title": ev.title,
            "description": ev.description,
            "location": ev.location,
            "all_day": ev.all_day,
            "at": None if ev.all_day else local,
            "contact": ev.contact,
            "editable": True,
        })

    # Layer 3 — confirmed firm deadlines, read-only.
    for fd in (FirmDate.objects.filter(date__gte=first, date__lte=last, confidence=1.0)
               .select_related("firm")):
        label = {
            "app_open": "applications open",
            "app_close": "applications close",
            "insight_open": "insight programme opens",
            "insight_deadline": "insight deadline",
        }.get(fd.event_kind, fd.event_kind.replace("_", " "))
        buckets.setdefault(fd.date, []).append({
            "id": None,
            "kind": "deadline",
            "source": "directory",
            "title": f"{fd.firm.name} — {label}",
            "description": "",
            "location": "",
            "all_day": True,
            "at": None,
            "at_label": "",
            "contact": None,
            "editable": False,
        })

    for day in buckets:
        # All-day first, then by clock time: a dated-but-untimed item is a
        # fact about the whole day and belongs above the 3pm coffee.
        buckets[day].sort(key=lambda e: (not e["all_day"], e["at"] or timezone.now()))
    return buckets


def _resolve_month(y, m, today: date) -> tuple[int, int]:
    """A year/month pair from untrusted input, or this month.

    Both entry points take these off the wire — one from a querystring a
    person can hand-edit, one from hidden fields on a POST — so both need the
    same forgiveness. `date(y, m, 1)` is the validation: it rejects month 13
    and year 0 alike, where an int() check alone would let them through to
    `monthrange` and 500.
    """
    try:
        year, month = int(y), int(m)
        date(year, month, 1)
        return year, month
    except (TypeError, ValueError):
        return today.year, today.month


def _month_context(user, year: int, month: int, today: date) -> dict:
    """Everything the template needs to draw one month.

    Shared by the normal render and the invalid-submission re-render. It used
    to be duplicated, and the copies had already drifted: the error path
    passed hard-coded zero counts, so mistyping a date made the month's real
    deadline tally read "0 deadlines" — the page contradicting itself at the
    exact moment the user is being told they got something wrong.
    """
    first, last = _month_bounds(year, month)
    buckets = _events_by_day(user, first, last)
    prev_y, prev_m = _shift(year, month, -1)
    next_y, next_m = _shift(year, month, 1)
    return {
        "weeks": [[{
            "date": d,
            "in_month": d.month == month,
            "is_today": d == today,
            "events": buckets.get(d, []),
        } for d in week] for week in _CAL.monthdatescalendar(year, month)],
        "month_label": first.strftime("%B %Y"),
        "year": year, "month": month,
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
        "today": today,
        "counts": {
            kind: sum(1 for evs in buckets.values() for e in evs if e["kind"] == kind)
            for kind in ("chat", "event", "deadline")
        },
        "weekday_names": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    }


@login_required
def calendar(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    year, month = _resolve_month(request.GET.get("y", today.year),
                                 request.GET.get("m", today.month), today)
    ctx = _month_context(request.user, year, month, today)

    # `?day=` — the per-day "+" in the grid. It prefills the date and opens
    # the panel, so adding something to the 14th is one click from the 14th
    # rather than a click plus retyping a date you were already looking at.
    # Out-of-range values are ignored rather than rejected: the day is a
    # convenience, and a bad one should still leave you a usable form.
    initial = {}
    try:
        picked = int(request.GET["day"])
        _, last = _month_bounds(year, month)
        if 1 <= picked <= last.day:
            initial["day"] = date(year, month, picked)
    except (KeyError, TypeError, ValueError):
        pass

    ctx["form"] = CalendarEventForm(user=request.user, initial=initial)
    ctx["form_open"] = bool(initial)
    return render(request, "crm/calendar.html", ctx)


@login_required
@require_POST
def calendar_add(request: HttpRequest) -> HttpResponse:
    """Add one event. Re-renders the month on error so the typed values and
    the message survive — a form that clears itself on a bad date is a form
    people stop using."""
    form = CalendarEventForm(request.POST, user=request.user)
    y = request.POST.get("y") or timezone.localdate().year
    m = request.POST.get("m") or timezone.localdate().month
    if form.is_valid():
        ev = form.save(commit=False)
        ev.user = request.user
        ev.source = CalendarEvent.SOURCE_MANUAL
        ev.save()
        return redirect(f"{request.path.rsplit('/add/', 1)[0]}/?y={y}&m={m}")

    today = timezone.localdate()
    year, month = _resolve_month(y, m, today)
    ctx = _month_context(request.user, year, month, today)
    ctx["form"] = form
    ctx["form_open"] = True
    return render(request, "crm/calendar.html", ctx, status=400)


@login_required
@require_POST
def calendar_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove one of the user's own events. Scoped through `for_user`, so a
    hand-crafted pk for someone else's row simply finds nothing."""
    y = request.POST.get("y") or timezone.localdate().year
    m = request.POST.get("m") or timezone.localdate().month
    CalendarEvent.objects.for_user(request.user).filter(pk=pk).delete()
    return redirect(f"/app/calendar/?y={y}&m={m}")
