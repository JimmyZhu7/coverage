"""The Calendar: everything with a date on it, in one month grid.

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

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from analytics.models import UserOpportunity
from core.templatetags.textstyle import smart_title
from directory.deadlines import is_posting_closed
from directory.dupes import fold_duplicates
from directory.models import FirmDate

from .forms import CalendarEventForm
from .models import CalendarEvent
from .utils import FIRM_DATE_LABELS, _clock

# Monday-first weeks: every market this product covers starts its week on a
# Monday, and the cadence engine's business-day maths already assumes it.
_CAL = calmod.Calendar(firstweekday=0)

# How far the month rail reaches either side of today. A recruiting cycle runs
# roughly a year ahead — applications for SA 2028 open the summer before — so
# the rail leans forward, and keeps half a year back for looking up a chat you
# have already had.
_RAIL_BACK, _RAIL_FORWARD = 6, 17

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
    for fd in (FirmDate.objects.filter(date__gte=first, date__lte=last, confidence=1.0)
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
_CONFIRMED = 1.0


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


def _month_rail(user, year: int, month: int, today: date) -> list[dict]:
    """The scrolling strip of months above the grid, each with its own count.

    The counts are the point. A bare list of month names tells you nothing
    about where to look; a strip that shows October carrying eleven things and
    July carrying none turns navigation into a read of the cycle. Two grouped
    queries rather than one per month — the rail is 24 wide.
    """
    months: list[tuple[int, int]] = []
    y, m = _shift(today.year, today.month, -_RAIL_BACK)
    for _ in range(_RAIL_BACK + _RAIL_FORWARD + 1):
        months.append((y, m))
        y, m = _shift(y, m, 1)

    first = date(*months[0], 1)
    last_y, last_m = months[-1]
    last = date(last_y, last_m, calmod.monthrange(last_y, last_m)[1])

    counts: dict[tuple[int, int], int] = {}

    # TruncMonth converts to the ACTIVE timezone before truncating, which is
    # the same local-day rule `_events_by_day` buckets on. Without that the
    # rail and the grid could disagree about which month a late-night Hong
    # Kong chat belongs to.
    window_start = timezone.make_aware(datetime.combine(first, datetime.min.time()))
    window_end = timezone.make_aware(datetime.combine(last + timedelta(days=1), datetime.min.time()))
    for row in (CalendarEvent.objects.for_user(user)
                .filter(starts_at__gte=window_start, starts_at__lt=window_end)
                .annotate(bucket=TruncMonth("starts_at"))
                .values("bucket").annotate(n=Count("id"))):
        key = (row["bucket"].year, row["bucket"].month)
        counts[key] = counts.get(key, 0) + row["n"]

    for row in (FirmDate.objects.filter(date__gte=first, date__lte=last, confidence=1.0)
                .annotate(bucket=TruncMonth("date"))
                .values("bucket").annotate(n=Count("id"))):
        key = (row["bucket"].year, row["bucket"].month)
        counts[key] = counts.get(key, 0) + row["n"]

    # Layer 4 in the rail too. A month strip that counts three of the four
    # layers sends you to a month that looks empty and isn't. Folded in
    # Python rather than with .annotate()/.values(): the fold has to run on
    # Opportunity objects (see _fold_tracked), which a GROUP BY can't do, so
    # the rows are materialised first and bucketed by month here instead.
    for uo in _fold_tracked(list(_tracked_deadlines(user, first, last))):
        d = uo.opportunity.deadline
        key = (d.year, d.month)
        counts[key] = counts.get(key, 0) + 1

    # The bar is a comparison against the busiest month on the rail, not an
    # absolute — "eleven" means nothing on its own, but "twice October" does.
    busiest = max(counts.values(), default=0)

    rail = []
    for ry, rm in months:
        n = counts.get((ry, rm), 0)
        rail.append({
            "bar_pct": round(100 * n / busiest) if busiest else 0,
            "y": ry, "m": rm,
            "label": date(ry, rm, 1).strftime("%b"),
            "year": ry,
            # The year only when it changes, so the strip reads
            # "Nov Dec | 2027 Jan Feb" instead of repeating itself 24 times.
            "show_year": rm == 1 or (ry, rm) == months[0],
            "count": n,
            "active": ry == year and rm == month,
            "is_now": ry == today.year and rm == today.month,
        })
    return rail


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
    def cell(d: date) -> dict:
        events = buckets.get(d, [])
        return {
            "date": d,
            "in_month": d.month == month,
            "is_today": d == today,
            "events": events,
            # Intensity, capped at 3. A day with nine things on it is not
            # nine times busier to look at than a day with three — past a
            # point the tint stops carrying information and just goes muddy.
            # Cells are a fixed height and scroll, so the count is the only
            # thing that can signal a heavy day at a glance.
            "load": min(len(events), 3),
            "load_label": f"{len(events)} on this day" if events else "",
        }

    return {
        "weeks": [[cell(d) for d in week]
                  for week in _CAL.monthdatescalendar(year, month)],
        "rail": _month_rail(user, year, month, today),
        "month_label": first.strftime("%B %Y"),
        "year": year, "month": month,
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
        "today": today,
        "counts": {
            kind: sum(1 for evs in buckets.values() for e in evs if e["kind"] == kind)
            for kind in CAL_KINDS
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
    """
    from django.contrib.auth import get_user_model
    from django.http import Http404

    user = get_user_model().objects.filter(
        calendar_token=token, deleted_at__isnull=True
    ).first()
    if user is None or not token:
        raise Http404

    def esc(text: str) -> str:
        return (text or "").replace("\\", "\\\\").replace(";", r"\;").replace(
            ",", r"\,").replace("\n", r"\n")

    now = timezone.now()
    window_start = now - timedelta(days=30)
    window_end = now + timedelta(days=400)

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

    for fd in (FirmDate.objects.filter(
            confidence=1.0, date__isnull=False,
            date__gte=window_start.date(), date__lte=window_end.date())
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
    for uo in _fold_tracked(
        list(_tracked_deadlines(user, window_start.date(), window_end.date()))
    ):
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
