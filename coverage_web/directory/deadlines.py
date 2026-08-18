"""The single definition of "closing soon", shared by every surface that
counts it.

Why this module exists: two pages already claim to answer the same question.
The Today dashboard's stat card (`crm/views._dashboard_context`) counts open
campus roles with `deadline__range=(today, today + timedelta(days=9))`, and My
Applications now shows a Closing Soon section over the user's own tracked
roles. If those two ever disagree — one saying 10 days and the other a week —
the product has told the student two different things about the same date, and
the whole honesty posture in `views.py` is worth nothing. So the window lives
here once, as data plus three tiny helpers, and callers import it.

The window is deliberately kept as a *length* rather than a pair of dates:
`today` is a parameter everywhere so tests can pin it, and no caller has to
re-derive the off-by-one.

STATUS: `crm/views.py` still inlines its own copy of the arithmetic. That file
is outside this change's ownership, so it was left alone; switching it to
`closing_soon_filter(campus)` is a one-line follow-up and is the only thing
standing between "the definitions agree today" and "the definitions cannot
drift". `test_closing_soon.py` pins the equivalence in the meantime, so the
two implementations diverging will fail the suite rather than ship silently.

`is_posting_closed` lives here for the same reason the window does. It answers
the other half of "does this row still deserve urgency", and four surfaces
needed it at once — My Applications, the weekly digest, the push alerts and
the calendar/.ics feed all read a student's tracked roles and every one of
them ignored `Opportunity.status`, so a posting the nightly pass had already
watched the firm take down still rendered as live and still alerted. This
module is the only place all four can import from without pulling
`directory.views` into the CRM's import graph.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone

#: How many days wide the Closing Soon window is, counting today as day one.
#: Ten, matching the Today widget's `(today, today + timedelta(days=9))` —
#: which is an INCLUSIVE range on both ends, hence `days - 1` below. A role
#: closing today is closing soon; a role closing on day 10 still is; day 11
#: is not. The number itself is a product judgement (roughly "this and next
#: week"), not a law of nature — but it must be ONE number.
CLOSING_SOON_DAYS = 10

#: `Opportunity.status` once a reverify pass has watched the firm take the
#: posting down (`closed_at` is stamped in the same moment, models.py).
POSTING_CLOSED = "closed"


def is_posting_closed(opportunity) -> bool:
    """True when the SCRAPER has confirmed the firm pulled this posting.

    NOT `UserOpportunity.applied_status == "closed"` (`directory.views.
    TRACK_CLOSED`), which is the STUDENT marking their own application Done.
    Two different facts, two different authors, the same word — and until this
    predicate existed only the student-authored one was ever surfaced, which
    is exactly how a dead role kept rendering as live. Named for the posting
    so the two can't be misread for each other at a call site.

    Tests `== POSTING_CLOSED` rather than `!= "open"`: `status` is a bare
    CharField defaulting to `""` with no choices, so a row the scraper has
    never re-checked carries neither value. An is-open test would drop every
    one of those from the lists a student reads, which is a worse bug than
    the one it would fix.
    """
    return (getattr(opportunity, "status", "") or "") == POSTING_CLOSED


def closing_soon_window(today: date | None = None) -> tuple[date, date]:
    """The inclusive `(first, last)` deadline dates that count as closing soon.

    Defaults to the server's local today, matching every other date-sensitive
    view in the app (`timezone.localdate()`, not `date.today()`, so a deployed
    TZ setting is honoured)."""
    today = today or timezone.localdate()
    return today, today + timedelta(days=CLOSING_SOON_DAYS - 1)


def is_closing_soon(deadline: date | None, *, today: date | None = None) -> bool:
    """True when a single deadline falls inside the window.

    A null deadline is NOT closing soon — it is rolling, which is a different
    kind of urgency and gets its own section. Returning False here rather than
    letting a null slide into a date comparison is what keeps a rolling role
    from ever rendering as overdue."""
    if deadline is None:
        return False
    first, last = closing_soon_window(today)
    return first <= deadline <= last


def closing_soon_filter(qs, *, today: date | None = None, field: str = "deadline"):
    """Narrow a queryset to the rows closing soon.

    `field` is the lookup path to the date, so this works both on
    `Opportunity` (`deadline`) and on rows that reach it through a join —
    `UserOpportunity` passes `opportunity__deadline`."""
    first, last = closing_soon_window(today)
    return qs.filter(**{f"{field}__range": (first, last)})
