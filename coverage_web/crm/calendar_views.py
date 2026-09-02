"""The Calendar: everything with a date on it, in a month, week or day view.

FOUR LAYERS, ONE TIMELINE, EACH HONESTLY LABELLED:

1. Coffee chats captured from the mailbox (`CalendarEvent`, source=capture).
2. Events the user typed in (`CalendarEvent`, source=manual).
3. CONFIRMED firm deadlines (`FirmDate`, read-only here).
4. Closing dates on the ROLES THIS USER TRACKS (`UserOpportunity`, read-only).

Layers 3 and 4 are read-only on purpose: both are directory data, not the
user's, so the calendar shows them and the weekly radar owns them. Only
dates the scan marked `confirmed_official` appear in layer 3 — the cadence
engine acts on that same bar, and a calendar that mixed rumours into it
would put countdowns on the page for events nobody has confirmed.

Layer 4 exists because layer 3 answers the wrong question. A FirmDate is the
whole firm's cycle ("Goldman Sachs, applications close"); what a student
needs on the day is the posting they actually starred. Those dates were
extracted, stored, and shown on the feed, and were invisible here and in the
subscribed feed — the two surfaces meant to tell you before it is too late.

Layer 4 does NOT copy layer 3's `confidence=1.0` bar, and that is a decision
rather than an oversight. A FirmDate below 1.0 is a date nobody has confirmed
the firm holds; an Opportunity below 1.0 is a date the posting itself states,
which `enrich_postings` read out of its prose instead of a published field —
92 of the 121 dated open roles. Excluding those would empty the layer to make
a point. They are shown and MARKED instead: "reported" travels with them onto
the grid, into the feed, and into the notification a phone raises, because the
honest move is to say where a date came from, not to withhold a real one.
"""

from __future__ import annotations

import calendar as calmod
from datetime import date, datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from analytics.models import UserOpportunity
from core.templatetags.textstyle import smart_title
from directory.deadlines import is_posting_closed
from directory.dupes import fold_duplicates

from .forms import CalendarEventForm
from .models import CalendarEvent
from .utils import (
    CONFIRMED_CONFIDENCE,
    FIRM_DATE_LABELS,
    _clock,
    confirmed_firm_dates as _confirmed_firm_dates,
)

# Monday-first weeks: every market this product covers starts its week on a
# Monday, and the cadence engine's business-day maths already assumes it.
_CAL = calmod.Calendar(firstweekday=0)


# The kinds the grid colours and the legend counts, in legend order. "opening"
# is a fourth because a date you cannot miss is not a deadline — see
# `_firm_date_kind` and layer 3.
CAL_KINDS = ("chat", "event", "deadline", "opening")

# Which FirmDate events are openings. Keyed off `event_kind` rather than a
# substring match on the label, so a future event whose name happens to
# contain "open" does not silently join them.
_OPENING_EVENTS = frozenset({"app_open", "insight_open"})


def _firm_date_kind(event_kind: str) -> str:
    """A FirmDate's calendar kind. Openings are their own kind; everything
    else on the firm-dates vocabulary is a date you can MISS."""
    return "opening" if event_kind in _OPENING_EVENTS else "deadline"


# Three ways to read the same dates, month first because that is the one the
# page shipped with and the one an unparameterised URL still lands on. Week
# and day exist because a month cell is 132px tall and scrolls its contents:
# a day with six things on it is legible in the month grid only by scrolling
# inside a box the size of a postage stamp.
CAL_VIEWS = ("month", "week", "day")

# The clock the day view rails against when the day itself does not widen it.
# Not 00:00-23:59: twenty-four rows for a surface whose entries cluster in
# office hours is mostly empty rule, and the rail exists to place the entries,
# not to draw a whole day.
_DAY_RAIL = (8, 21)


def _resolve_view(v) -> str:
    return v if v in CAL_VIEWS else "month"


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

    # Layers 1 + 2 — the user's own events. `starts_at` is a datetime, so the
    # window is widened by a day at each end before filtering to be sure a
    # local-midnight event at either edge is caught.
    #
    # Widened through `_step`, which is a no-op at the ends of the range
    # `date` can represent. `?y=1&m=1` reaches here with `first` on 1 January
    # of year 1, and a bare `- timedelta(days=1)` raised OverflowError from
    # inside the view: a 500 on a hand-edited querystring, the same failure
    # `_resolve_month`'s year-10000 check guards at the other end. Losing the
    # widening in that one case costs nothing — there are no events in year 1
    # — and the range filter below is inclusive either way.
    window_start = timezone.make_aware(
        datetime.combine(_step(first, -1), datetime.min.time()))
    window_end = timezone.make_aware(
        datetime.combine(_step(last, 2), datetime.min.time()))
    for ev in (CalendarEvent.objects.for_user(user)
               .filter(starts_at__gte=window_start, starts_at__lt=window_end)
               .select_related("contact")):
        local = timezone.localtime(ev.starts_at)
        day = local.date()
        if not (first <= day <= last):
            continue
        buckets.setdefault(day, []).append({
            "at_label": "" if ev.all_day else _clock(local),
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
            # Kept on the grid rather than dropped, on exactly the reasoning
            # layer 4 uses for a posting the firm has pulled: this is a
            # RECORD of the student's month, and a row that silently vanishes
            # teaches them to distrust the page. Today's queue is where a
            # cancelled chat stops appearing, because that page answers a
            # different question. The title already carries "Cancelled: " —
            # this flag is what lets the grid strike it through as well, so
            # it reads as retired at a glance and not just on close reading.
            "cancelled": ev.cancelled_at is not None,
        })

    # Layer 3 — confirmed firm dates, read-only. Deadlines AND openings: this
    # loop used to stamp `kind: "deadline"` on every FirmDate regardless of
    # its `event_kind`, and the month tally counts by kind, so August 2026's
    # legend read "6 deadlines" when one of the six was Goldman Sachs
    # applications OPEN on the 15th. The row's own text said "applications
    # open" while the count above it called it a deadline, and it wore the
    # deadline-coloured left border to match.
    #
    # An opening is the opposite kind of date: nothing is missed by ignoring
    # it, and it is the day a thing becomes possible. `_firm_date_kind` reads
    # the event, so `insight_open` is covered by the same rule rather than
    # waiting to be found in a month where one falls.
    for fd in (_confirmed_firm_dates().filter(date__gte=first, date__lte=last)
               .select_related("firm")):
        label = FIRM_DATE_LABELS.get(fd.event_kind, fd.event_kind.replace("_", " "))
        buckets.setdefault(fd.date, []).append({
            "id": None,
            "kind": _firm_date_kind(fd.event_kind),
            "source": "directory",
            "title": f"{fd.firm.name} · {label}",
            "description": "",
            "location": "",
            "all_day": True,
            "at": None,
            "at_label": "",
            "contact": None,
            "editable": False,
        })

    # Layer 4 — closing dates on roles this user tracks.
    for uo in _fold_tracked(list(_tracked_deadlines(user, first, last))):
        opp = uo.opportunity
        buckets.setdefault(opp.deadline, []).append({
            "id": None,
            "kind": "deadline",
            "source": "tracked",
            "title": _role_label(opp),
            "description": "",
            "location": "",
            "all_day": True,
            "at": None,
            "at_label": "",
            "contact": None,
            "editable": False,
            # The one row on this page that can send you somewhere useful:
            # the posting itself, on the day it closes.
            "url": opp.url,
            "stage": uo.applied_status or "saved",
            "reported": _is_reported(opp),
            # The grid said "closes this day" about postings the firm had
            # already pulled. The row stays (it is the student's own tracked
            # role, and a date they may still be planning around); what
            # changes is that it stops claiming to be a live deadline.
            "posting_closed": is_posting_closed(opp),
        })

    for day in buckets:
        # All-day first, then by clock time: a dated-but-untimed item is a
        # fact about the whole day and belongs above the 3pm coffee.
        buckets[day].sort(key=lambda e: (not e["all_day"], e["at"] or timezone.now()))
    return buckets


# See the module docstring: 1.0 means the board published the date as a field,
# anything less means we read it out of the posting's own words.
_CONFIRMED = CONFIRMED_CONFIDENCE


def _is_reported(opp) -> bool:
    return (opp.confidence or 0) < _CONFIRMED


def _role_label(opp) -> str:
    """"Firm · Role", unless the role already says the firm.

    Scraped titles often carry their own firm name ("Bank of America |
    Insight Day"), and prefixing it produces "Bank of America · Bank of
    America | Insight Day" on the calendar and in the feed.

    The role title is standardized through `smart_title`, the same filter every
    OTHER surface applies to `Opportunity.title` (_rolecard.html,
    my_applications.html, _role_drawer.html). The calendar was the one page
    that applied nothing, so it showed raw scrape casing while the feed showed
    the standardized form — every tracked, dated role read differently on the
    two pages ("Discovery Program: Equity + Macro Research (On-site)" here
    against "(On-Site)" there). Applied HERE rather than in the template so the
    month grid, the narrow-screen agenda and the .ics feed cannot drift from
    each other; the .ics is the one that ends up on a phone's lock screen.

    The FIRM name is deliberately left raw: it is curated, not scraped, and
    `smart_title` would rewrite "PIMCO" to "Pimco".
    """
    title = smart_title((opp.title or "").strip())
    firm = (opp.firm.name or "").strip()
    if firm and title.lower().startswith(firm.lower()):
        return title
    return f"{firm} · {title}"


def _tracked_deadlines(user, first: date, last: date):
    """Tracked roles closing between two dates, newest stage wins.

    `dismissed=False` because a role you marked "not for me" should not turn
    up on your calendar, and `closed` (the terminal "Done" stage in
    directory.views.TRACK_CLOSED) because a finished application's deadline
    is history. The status string is written in exactly one place; it is
    matched here as a literal rather than imported to keep the directory app
    out of the CRM's import graph.

    A posting the SCRAPER has closed (`Opportunity.status`, a different fact
    from the `applied_status` matched above — see
    `directory.deadlines.is_posting_closed`) deliberately still comes back
    from this query. Both callers need the row: the grid marks it, and the
    .ics feed retitles it rather than dropping a VEVENT out from under a
    calendar the student already subscribed to. What neither does any more is
    alarm on it. Filtering here would have been the shorter patch and the
    wrong one — it would have deleted the row from their calendar instead of
    telling them the truth about it.
    """
    return (UserOpportunity.objects.for_user(user)
            .filter(dismissed=False,
                    opportunity__deadline__gte=first,
                    opportunity__deadline__lte=last)
            .exclude(applied_status="closed")
            .select_related("opportunity", "opportunity__firm"))


def _fold_tracked(rows: list) -> list:
    """Collapse identity duplicates among tracked deadlines: one requisition
    filed under two candidate-pool addresses (see directory/dupes.py) is one
    line on the calendar, not two.

    `UserOpportunity` carries none of the firm/title/location/deadline
    fields `fold_duplicates` keys on, so every row would key to the same
    bucket and the fold would swallow real tracked deadlines instead of the
    one genuine duplicate (this is the exact trap `my_applications` in
    directory/views.py avoids the same way). Folding runs on the underlying
    Opportunity objects, and survivors are mapped back to their
    UserOpportunity by object identity.

    When a student tracked both duplicate addresses at different funnel
    stages, the one with real progress wins the fold over
    `fold_duplicates`' own deadline/location/first_seen/id tie-break.
    """
    opps = [uo.opportunity for uo in rows]
    progressed_ids = {
        uo.opportunity_id for uo in rows
        if (uo.applied_status or "saved") != "saved"
    }
    survivors, _folded = fold_duplicates(opps, sticky_ids=progressed_ids)
    kept = {id(o) for o in survivors}
    return [uo for uo, opp in zip(rows, opps) if id(opp) in kept]


def _resolve_month(y, m, today: date) -> tuple[int, int]:
    """A year/month pair from untrusted input, or this month.

    Both entry points take these off the wire — one from a querystring a
    person can hand-edit, one from hidden fields on a POST — so both need the
    same forgiveness. `date(y, m, 1)` is the validation: it rejects month 13
    and year 0 alike, where an int() check alone would let them through to
    `monthrange` and 500.

    THE MONTH AFTER IT HAS TO BE REPRESENTABLE TOO, which the first check
    alone does not give. `date(9999, 12, 1)` is a perfectly valid date, so
    `?y=9999&m=12` passed — and then `monthdatescalendar(9999, 12)` builds the
    grid's trailing week out of the first days of January 10000 and raises
    "year 10000 is out of range" from inside the template context, i.e. a 500
    on a hand-edited querystring. Checking the NEXT month is the same shape of
    guard as the existing one and covers the grid's overhang at both ends
    (`_shift` back from January lands on year 0, which the first check already
    rejects on the follow-up request).
    """
    try:
        year, month = int(y), int(m)
        date(year, month, 1)
        date(*_shift(year, month, 1), 1)
        return year, month
    except (TypeError, ValueError, OverflowError):
        return today.year, today.month


def _resolve_anchor(y, m, d, today: date) -> date:
    """The single date the page is pointed at, from untrusted input.

    `?d=` IS NOT `?day=`. The anchor moves the view (which month, which week,
    which day is on screen); `?day=` prefills the add form and opens it. Two
    day-shaped parameters on one page is confusable enough to be worth saying
    out loud, and merging them was the shorter option and the wrong one — a
    week-to-week click would then open the add panel every time you paged.

    Year and month go through `_resolve_month`, which already rejects the
    values that would 500 the grid. The day is CLAMPED rather than rejected,
    on the same reasoning `?day=` is ignored rather than rejected: a
    hand-edited anchor should still leave a usable page. Clamping is also
    what carries the day-of-month across a month step, so paging from 31
    March lands on 30 April rather than nowhere.

    With no `?d=` at all the anchor is today when today falls in the month on
    screen, and the 1st when it does not. That is what makes the switcher
    land where you are looking: opening the calendar cold and clicking Week
    gives this week, not the week of the 1st.
    """
    year, month = _resolve_month(y, m, today)
    last = calmod.monthrange(year, month)[1]
    try:
        day = int(d)
    except (TypeError, ValueError):
        day = today.day if (year, month) == (today.year, today.month) else 1
    return date(year, month, min(max(day, 1), last))


def _step(anchor: date, days: int) -> date:
    """The anchor moved by whole days, or the anchor itself at the ends of the
    range `date` can represent. Only reachable by hand-editing the querystring
    to year 1, where drawing a "previous week" link raises OverflowError from
    inside the template context — i.e. a 500 on a URL nobody legitimately
    visits. Same shape of guard as `_resolve_month`'s year-10000 check."""
    try:
        return anchor + timedelta(days=days)
    except OverflowError:
        return anchor


def _view_range(view: str, anchor: date) -> tuple[date, date]:
    """The dates a view actually SHOWS, inclusive.

    The month is the month itself, not the grid's leading and trailing
    overhang. Those cells stay deliberately empty: a neighbouring month's
    deadline drawn in this month's grid would also land in this month's
    tally, and the legend counts what the range holds.
    """
    if view == "day":
        return anchor, anchor
    if view == "week":
        # Monday-first, matching `_CAL` and the cadence engine's business-day
        # maths. `date(1, 1, 1)` is itself a Monday, so this subtraction can
        # never step below MINYEAR however the querystring is edited.
        start = anchor - timedelta(days=anchor.weekday())
        return start, _step(start, 6)
    return _month_bounds(anchor.year, anchor.month)


def _period_label(view: str, first: date, last: date) -> str:
    """What the heading says. A week is the one case that has to name two
    months or two years, because a week that straddles either is otherwise
    two unlabelled numbers with no year on them.

    The week format abbreviates the month (`%b`, "Aug" not "August").
    Reported live off this exact string: at 1440px in the heading's former
    26px display face, "31 August to 6 September 2026" ran 471px wide —
    wider than the switcher, Today, Subscribe and Add combined — and the
    worst case, "28 December 2026 to 3 January 2027", ran the same. Both
    months a straddling week can name now abbreviate: the worst case is "28
    Dec 2026 to 3 Jan 2027", 244px in the heading's current (smaller) type —
    see the `.cal-month` comment in calendar.html for the full re-measured
    table. Month and day keep the full name: neither is the case this was
    too wide for, and a lone "Aug 2026" reads as a truncation nobody asked
    for when "August 2026" already fit.
    """
    if view == "day":
        return f"{first:%A} {first.day} {first:%B %Y}"
    if view == "month":
        return first.strftime("%B %Y")
    if first.year != last.year:
        return f"{first.day} {first:%b} {first.year} to {last.day} {last:%b} {last.year}"
    if first.month != last.month:
        return f"{first.day} {first:%b} to {last.day} {last:%b %Y}"
    return f"{first.day} to {last.day} {first:%b %Y}"


def _qs(view: str, anchor: date, **extra) -> str:
    """A link back to this page carrying BOTH the view and the anchor, which
    is what makes any of these three URLs survive a reload or a paste into
    someone else's browser."""
    params = {"view": view, "y": anchor.year, "m": anchor.month, "d": anchor.day}
    params.update(extra)
    return "?" + urlencode(params)


def _nav_urls(view: str, anchor: date) -> tuple[str, str]:
    """Previous and next, one period at a time, in every view.

    The month steps by (year, month) rather than by days on purpose: January's
    "previous" has always been year 0 month 12, a link whose follow-up request
    falls back to today rather than 500ing. Building a `date` here to carry the
    anchor would raise before the link is even drawn.
    """
    if view in ("day", "week"):
        by = 1 if view == "day" else 7
        return _qs(view, _step(anchor, -by)), _qs(view, _step(anchor, by))
    out = []
    for by in (-1, 1):
        y, m = _shift(anchor.year, anchor.month, by)
        out.append("?" + urlencode(
            {"view": "month", "y": y, "m": m, "d": anchor.day}))
    return out[0], out[1]


def _hour_label(hour: int) -> str:
    return f"{hour % 12 or 12}{'am' if hour < 12 else 'pm'}"


def _hour_rail(events: list[dict]) -> list[dict]:
    """The day view's time axis: one row per hour, entries in the hour they
    start.

    Placement is by hour row rather than by proportional offset, and entries
    that share an hour stack rather than sitting side by side. Both are
    deliberate. Only two of the four layers carry a time at all, and one of
    those two (`CalendarEvent.ends_at`) is nullable and usually null — so a
    proportional layout would be drawing most blocks at an invented length,
    and side-by-side overlap resolution would be geometry in service of a
    collision this data cannot currently express.

    The window widens to whatever the day actually holds, so a 7am flight and
    an 11pm call are on the rail rather than off the end of it.
    """
    timed = [e for e in events if not e["all_day"] and e["at"]]
    hours = [e["at"].hour for e in timed]
    lo = min([_DAY_RAIL[0], *hours])
    hi = max([_DAY_RAIL[1], *hours])
    return [{"label": _hour_label(h),
             "events": [e for e in timed if e["at"].hour == h]}
            for h in range(lo, hi + 1)]


def _period_context(user, view: str, anchor: date, today: date) -> dict:
    """Everything the template needs to draw one period, whichever it is.

    Shared by the normal render and the invalid-submission re-render. It used
    to be duplicated, and the copies had already drifted: the error path
    passed hard-coded zero counts, so mistyping a date made the month's real
    deadline tally read "0 deadlines" — the page contradicting itself at the
    exact moment the user is being told they got something wrong.

    ONE query per render whatever the view: `_events_by_day` takes a date
    range, so month, week and day differ in the range they ask for and in
    nothing else. The three views cannot disagree about what is on a day.
    """
    first, last = _view_range(view, anchor)
    buckets = _events_by_day(user, first, last)
    prev_url, next_url = _nav_urls(view, anchor)

    def cell(d: date, in_range: bool = True) -> dict:
        events = buckets.get(d, [])
        return {
            "date": d,
            "in_month": in_range,
            "is_today": d == today,
            "events": events,
            # Intensity, capped at 3. A day with nine things on it is not
            # nine times busier to look at than a day with three — past a
            # point the tint stops carrying information and just goes muddy.
            # Cells are a fixed height and scroll, so the count is the only
            # thing that can signal a heavy day at a glance.
            "load": min(len(events), 3),
            "load_label": f"{len(events)} on this day" if events else "",
            # The cell's OWN date, not the anchor's month. A week cell can be
            # in the next month, and "+ on the 1st" has to prefill the 1st of
            # that month rather than of the month the anchor happens to sit in.
            "add_url": _qs(view, d, day=d.day) + "#add",
            # A bare "1" in a week that straddles two months says nothing.
            # Month view never renders this: its own header row and the
            # is-out tint already answer the question.
            "month_tag": d.strftime("%b") if (d == first or d.day == 1) else "",
        }

    ctx = {
        "view": view,
        "anchor": anchor,
        "period_label": _period_label(view, first, last),
        "period_noun": view,
        "year": anchor.year, "month": anchor.month,
        "prev_url": prev_url, "next_url": next_url,
        "month_url": _qs("month", anchor),
        "week_url": _qs("week", anchor),
        "day_url": _qs("day", anchor),
        "today": today,
        # Whether the Today control has anywhere to go. In month view this
        # used to be spelled out in the template as a year+month comparison;
        # it is the same question in all three views, asked once.
        "today_in_range": first <= today <= last,
        "counts": {
            kind: sum(1 for evs in buckets.values() for e in evs if e["kind"] == kind)
            for kind in CAL_KINDS
        },
        "weekday_names": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "empty_note": f"Nothing on the calendar this {view}.",
    }
    if view == "month":
        weeks = [[cell(d, in_range=d.month == anchor.month) for d in week]
                 for week in _CAL.monthdatescalendar(anchor.year, anchor.month)]
        ctx["weeks"] = weeks
        # The narrow-screen agenda reads a flat list rather than walking the
        # grid, so week view can hand it seven days and month view the days
        # that are actually in the month, through one template loop.
        ctx["agenda_days"] = [c for week in weeks for c in week if c["in_month"]]
    elif view == "week":
        days = [cell(_step(first, i)) for i in range(7)]
        ctx["days"] = days
        ctx["agenda_days"] = days
    else:
        cur = cell(anchor)
        ctx["day_cell"] = cur
        ctx["all_day_events"] = [e for e in cur["events"] if e["all_day"]]
        ctx["hours"] = _hour_rail(cur["events"])
    return ctx


@login_required
def calendar(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    view = _resolve_view(request.GET.get("view"))
    anchor = _resolve_anchor(request.GET.get("y", today.year),
                             request.GET.get("m", today.month),
                             request.GET.get("d"), today)
    ctx = _period_context(request.user, view, anchor, today)

    # `?day=` — the per-day "+" in the grid. It prefills the date and opens
    # the panel, so adding something to the 14th is one click from the 14th
    # rather than a click plus retyping a date you were already looking at.
    # Out-of-range values are ignored rather than rejected: the day is a
    # convenience, and a bad one should still leave you a usable form.
    initial = {}
    try:
        picked = int(request.GET["day"])
        _, last = _month_bounds(anchor.year, anchor.month)
        if 1 <= picked <= last.day:
            initial["day"] = date(anchor.year, anchor.month, picked)
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
    today = timezone.localdate()
    # The view and anchor ride on the form as hidden fields, so adding
    # something from the week of 9 March returns to the week of 9 March
    # rather than dropping back to this month's grid.
    view = _resolve_view(request.POST.get("view"))
    anchor = _resolve_anchor(request.POST.get("y") or today.year,
                             request.POST.get("m") or today.month,
                             request.POST.get("d"), today)
    if form.is_valid():
        ev = form.save(commit=False)
        ev.user = request.user
        ev.source = CalendarEvent.SOURCE_MANUAL
        ev.save()
        return redirect(
            f"{request.path.rsplit('/add/', 1)[0]}/{_qs(view, anchor)}")

    ctx = _period_context(request.user, view, anchor, today)
    ctx["form"] = form
    ctx["form_open"] = True
    return render(request, "crm/calendar.html", ctx, status=400)


@login_required
@require_POST
def calendar_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove one of the user's own events. Scoped through `for_user`, so a
    hand-crafted pk for someone else's row simply finds nothing."""
    today = timezone.localdate()
    view = _resolve_view(request.POST.get("view"))
    anchor = _resolve_anchor(request.POST.get("y") or today.year,
                             request.POST.get("m") or today.month,
                             request.POST.get("d"), today)
    CalendarEvent.objects.for_user(request.user).filter(pk=pk).delete()
    return redirect(f"/app/calendar/{_qs(view, anchor)}")


@login_required
@require_POST
def calendar_token_reset(request: HttpRequest) -> HttpResponse:
    """Mint a new ICS feed token and show the new URL once.

    The feed below is authenticated by a token IN THE URL PATH, which is the
    only thing Calendar.app and Google Calendar can carry — they fetch from
    their own servers with no cookies of ours. That makes the token a bearer
    credential for a read-only copy of the user's calendar, and a path
    component lands in places a header never would: proxy access logs, the
    calendar provider's own fetch logs, a browser's history, a screen share.
    Until this view existed, a student who had leaked one had no way back:
    the column was writable from a Django shell and nowhere else.

    `User.save()` regenerates the token whenever the column is empty (see
    accounts/models.py), so clearing it is the whole operation — one
    definition of how a token is minted, not a second one here.

    THE NEW URL IS RETURNED, NEVER LOGGED. It goes into a one-shot Django
    message, which is rendered into the response for this user and then
    consumed. Nothing on this path writes the token to a logger, and the
    redirect target carries no query string, so it cannot reach an access
    log either. That is the same rule the feed's own leak is about; a reset
    view that logged what it minted would just move the problem.
    """
    request.user.calendar_token = None
    # `save()` fills the empty column with a fresh `secrets.token_urlsafe(24)`
    # before it writes, so the attribute already holds the new value here —
    # no re-read needed.
    request.user.save(update_fields=["calendar_token"])
    messages.success(
        request,
        "New calendar link: webcal://"
        + request.get_host()
        + reverse("crm:calendar_ics", args=[request.user.calendar_token])
        + ". The old link is dead. Subscribe again in your calendar app.",
    )
    return redirect(f"{reverse('accounts:settings')}#security")


def calendar_ics(request: HttpRequest, token: str) -> HttpResponse:
    """The calendar as an ICS feed, for the calendar app the user already
    lives in.

    Token-authenticated, not session: Calendar.app and Google Calendar fetch
    feeds from their own servers with no cookies. The token is its own column
    (never the capture slug — that one can WRITE into the CRM) and revoking
    every stale subscription is "blank the field, save".

    Contents mirror the page exactly: the user's own events with real times,
    confirmed firm deadlines, and the closing dates of the roles this user
    tracks, all as all-day entries. Unconfirmed dates stay out for the same
    reason they stay off the page — a rumour with an alarm on it is worse
    than a rumour.

    Every closing date carries two VALARMs, a week out and a day out. That is
    the whole point of subscribing rather than visiting: a deadline you have
    to remember to go and look at is one you can still miss. Dates that open
    something (applications open, insight programme opens) get no alarm —
    nothing is lost by reading those late.

    THIS VIEW ACTIVATES THE USER'S TIMEZONE ITSELF, and has to. Every other
    calendar surface gets it free from `accounts.middleware.TimezoneMiddleware`,
    which reads `request.user` — but this request is authenticated by a token
    in the URL, not a session, so `request.user` is anonymous and the
    middleware deactivates back to `settings.TIME_ZONE` ("UTC"). Under UTC,
    `timezone.localtime(ev.starts_at).date()` below resolves an all-day event
    on the UTC day: a Hong Kong all-day event stored as 16:00Z the previous
    day landed on the grid (middleware active, Asia/Hong_Kong) on one date
    and in the subscribed feed on the date before it. Two surfaces reading
    one row and disagreeing about which day it is on is exactly the class of
    bug the module docstring's honesty rules exist to prevent, and the feed
    is the copy that ends up on a phone. `window_start.date()` below has the
    same dependency.

    Deactivated in a `finally` for the same reason the middleware does it:
    this runs on a reused worker thread, and a leaked activation would put
    one student's zone on whatever request the thread serves next.
    """
    from django.contrib.auth import get_user_model
    from django.http import Http404

    from accounts.middleware import activate_for_user

    user = get_user_model().objects.filter(
        calendar_token=token, deleted_at__isnull=True
    ).first()
    if user is None or not token:
        raise Http404
    activate_for_user(user)
    try:
        return _ics_body(user)
    finally:
        timezone.deactivate()


def _ics_body(user) -> HttpResponse:
    """The feed itself. Split out of `calendar_ics` only so the timezone
    activation there can wrap it in a `try/finally` without indenting a
    hundred lines of iCalendar assembly."""

    def esc(text: str) -> str:
        return (text or "").replace("\\", "\\\\").replace(";", r"\;").replace(
            ",", r"\,").replace("\n", r"\n")

    now = timezone.now()
    window_start = now - timedelta(days=30)
    window_end = now + timedelta(days=400)
    # The two DATE-typed layers below (firm dates, tracked closes) are
    # filtered on calendar dates, so the window's edges are read on the
    # user's own calendar — `.date()` on these UTC instants would move both
    # edges by a day for every user east of Greenwich, silently dropping the
    # deadline that falls exactly on the far edge.
    first_day = timezone.localdate(window_start)
    last_day = timezone.localdate(window_end)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Coverage//Calendar//EN",
        "X-WR-CALNAME:Coverage",
        "X-WR-CALDESC:Chats and confirmed recruiting deadlines from Coverage",
    ]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    for ev in (CalendarEvent.objects.for_user(user)
               .filter(starts_at__gte=window_start, starts_at__lt=window_end)
               .select_related("contact")):
        lines += ["BEGIN:VEVENT",
                  f"UID:coverage-ev-{ev.id}@coverage.app",
                  f"DTSTAMP:{stamp}",
                  f"SUMMARY:{esc(ev.title)}"]
        if ev.all_day:
            day = timezone.localtime(ev.starts_at).date()
            lines.append(f"DTSTART;VALUE=DATE:{day:%Y%m%d}")
        else:
            utc = ev.starts_at.astimezone(dt_timezone.utc)
            lines.append(f"DTSTART:{utc:%Y%m%dT%H%M%S}Z")
            end = (ev.ends_at or ev.starts_at + timedelta(minutes=30))
            lines.append(f"DTEND:{end.astimezone(dt_timezone.utc):%Y%m%dT%H%M%S}Z")
        if ev.location:
            lines.append(f"LOCATION:{esc(ev.location)}")
        if ev.description:
            lines.append(f"DESCRIPTION:{esc(ev.description)}")
        lines.append("END:VEVENT")

    def alarms(summary: str) -> list[str]:
        """A week out and a day out, as DISPLAY alarms.

        TRIGGER is relative to DTSTART and negative, so -P7D fires seven days
        BEFORE the close. Calendar apps that ignore VALARM on subscribed
        feeds (iOS does, unless the subscription is set to keep alerts) lose
        nothing else — the event itself still lands.
        """
        out = []
        for trigger, when in (("-P7D", "in a week"), ("-P1D", "tomorrow")):
            out += ["BEGIN:VALARM", "ACTION:DISPLAY",
                    f"TRIGGER;RELATED=START:{trigger}",
                    f"DESCRIPTION:{esc(f'{summary} · {when}')}",
                    "END:VALARM"]
        return out

    for fd in (_confirmed_firm_dates()
               .filter(date__gte=first_day, date__lte=last_day)
               .select_related("firm")):
        label = FIRM_DATE_LABELS.get(fd.event_kind, fd.event_kind.replace("_", " "))
        summary = f"{fd.firm.name} · {label}"
        lines += ["BEGIN:VEVENT",
                  f"UID:coverage-fd-{fd.id}@coverage.app",
                  f"DTSTAMP:{stamp}",
                  f"SUMMARY:{esc(summary)}",
                  f"DTSTART;VALUE=DATE:{fd.date:%Y%m%d}"]
        # Only the dates you can MISS get an alarm.
        if "close" in fd.event_kind or "deadline" in fd.event_kind:
            lines += alarms(summary)
        lines.append("END:VEVENT")

    # Layer 4 — the roles this user tracks, on the day they close. These are
    # the rows that most deserve the alarm: a starred posting is a stated
    # intention, and this feed is what turns it into a reminder.
    #
    # A POSTING THE FIRM HAS PULLED IS RETITLED, NOT DROPPED. Three options
    # were on the table and the other two are worse:
    #
    #   * Omit the VEVENT. This feed is SUBSCRIBED — the event is already
    #     sitting in the student's own calendar app, so omitting it deletes it
    #     from their week at the next refresh, silently and with no way to ask
    #     why. A thing that vanishes teaches you to distrust the feed; the
    #     entry that says what happened teaches you what happened.
    #   * STATUS:CANCELLED. Semantically the closest iCalendar has, but client
    #     behaviour splits: some strike it through, some hide it, some drop it
    #     outright — which lands us back at a silent disappearance on exactly
    #     the phones this feed exists for.
    #
    # So the event stays on its day, says "Closed:" first, and raises no
    # alarm. The alarm is the only part that was actively harmful: a VALARM is
    # a phone waking someone up to act, and there is nothing left to do.
    for uo in _fold_tracked(list(_tracked_deadlines(user, first_day, last_day))):
        opp = uo.opportunity
        # The marker rides in the SUMMARY, not the description: a phone
        # notification shows the summary and nothing else, and that
        # notification is the whole reason this feed exists. Same argument
        # puts "Closed:" at the FRONT rather than tensing the verb — "closes"
        # against "closed" is one character on a lock screen, which is no
        # signal at all.
        reported = _is_reported(opp)
        closed = is_posting_closed(opp)
        summary = (f"Closed: {_role_label(opp)}" if closed
                   else f"{_role_label(opp)} closes"
                        + (" (reported)" if reported else ""))
        lines += ["BEGIN:VEVENT",
                  f"UID:coverage-uo-{uo.id}@coverage.app",
                  f"DTSTAMP:{stamp}",
                  f"SUMMARY:{esc(summary)}",
                  f"DTSTART;VALUE=DATE:{opp.deadline:%Y%m%d}"]
        note = ("Read from the posting's own text, not a field the board "
                "published.\n" if reported and not closed else "")
        if closed:
            note = ("The firm has taken this posting down. It stays on your "
                    "calendar so the date still makes sense.\n")
        if opp.url:
            lines += [f"URL:{esc(opp.url)}",
                      f"DESCRIPTION:{esc(note + opp.url)}"]
        elif note:
            lines.append(f"DESCRIPTION:{esc(note)}")
        if not closed:
            lines += alarms(summary)
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return HttpResponse("\r\n".join(lines) + "\r\n",
                        content_type="text/calendar; charset=utf-8")
