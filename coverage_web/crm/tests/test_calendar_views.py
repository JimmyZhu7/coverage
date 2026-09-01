"""Month, week and day: three ways to read one set of dates.

The page shipped with a month grid alone, and a month cell is 132px tall and
scrolls what it cannot fit — so a day with six things on it was legible only
by scrolling inside a box the size of a postage stamp. These tests pin the
three things that make the other two views worth having and are easy to get
subtly wrong:

* the RANGE each view covers, at the boundaries where a week belongs to two
  months or two years and a day sits on the edge of one;
* the ANCHOR surviving a switch, so Week from 15 March is the week that
  contains the 15th rather than the week that contains today;
* the URL carrying both, so any of the three survives a reload, a bookmark
  or a paste into someone else's browser.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from crm.models import CalendarEvent
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(email="jimmy@example.com", password="x")


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return user


def _get(client, **params):
    return client.get(reverse("crm:calendar"), params)


def _markup(resp) -> str:
    """The response with its <style> block removed.

    Every class name this page can render is also WRITTEN somewhere in that
    block, so `"is-out" in body` is true on a page that renders no such cell.
    Assertions about what was drawn have to read the markup, not the rules
    that would style it. (`_style_block` in test_calendar.py is the same split
    from the other side.)
    """
    body = resp.content.decode()
    head, _, rest = body.partition("<style>")
    return head + rest.partition("</style>")[2]


def _today_link(resp) -> str:
    """Just the Today control's own tag. `data-already-today` also appears in
    the click handler's selector at the foot of the page, so a bare `in body`
    is true whether or not the control carries the attribute."""
    match = re.search(r"<a[^>]*data-today-link[^>]*>", _markup(resp))
    assert match, "the Today control went missing"
    return match.group(0)


def _event(user, when: date, title: str, hour: int | None = None):
    """One of the user's own rows. All-day unless an hour is given, which is
    the split the day view's rail is built on."""
    at = timezone.make_aware(datetime.combine(when, datetime.min.time()).replace(
        hour=hour or 0))
    return CalendarEvent.all_objects.create(
        user=user, title=title, starts_at=at, all_day=hour is None)


def _firm_deadline(when: date, name="Goldman Sachs", slug="gs"):
    firm = Firm.objects.filter(slug=slug).first() or Firm.objects.create(
        slug=slug, name=name)
    return FirmDate.objects.create(firm=firm, cycle="sa2028", region="us",
                                   event_kind="app_close", date=when,
                                   confidence=1.0)


# ---------------------------------------------------------------------------
# Routing: the view mode is a querystring value, and an unknown one is not an
# error. Same forgiveness the month/day params already get — every one of
# these is hand-editable.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("view", ["month", "week", "day"])
def test_each_view_renders(client, logged_in, view):
    resp = _get(client, view=view, y=2027, m=3, d=15)
    assert resp.status_code == 200
    assert resp.context["view"] == view


@pytest.mark.parametrize("bad", ["", "year", "agenda", "MONTH", "1", None])
def test_an_unknown_view_falls_back_to_the_month(client, logged_in, bad):
    resp = _get(client, **({"view": bad} if bad is not None else {}))
    assert resp.status_code == 200
    assert resp.context["view"] == "month"


def test_the_calendar_still_opens_on_this_month_with_no_parameters(client, logged_in):
    """The URL every link in the product points at. It must keep landing on
    the current month, unparameterised, exactly as before."""
    today = timezone.localdate()
    resp = _get(client)
    assert resp.context["view"] == "month"
    assert resp.context["period_label"] == today.strftime("%B %Y")


# ---------------------------------------------------------------------------
# What each view covers. The boundaries are the whole test: a week that is
# entirely inside one month proves almost nothing.
# ---------------------------------------------------------------------------

def test_the_week_is_the_seven_days_containing_the_anchor(client, logged_in):
    resp = _get(client, view="week", y=2027, m=3, d=17)
    days = [c["date"] for c in resp.context["days"]]
    assert days == [date(2027, 3, 15) + timedelta(days=i) for i in range(7)]
    assert days[0].weekday() == 0, "Monday-first, like the month grid"


def test_a_week_spanning_two_months_shows_both_ends(client, logged_in):
    """29 March to 4 April 2027. The bug this guards against is a week view
    that quietly clips itself to the anchor's month and drops the days on the
    far side of the 31st."""
    _event(logged_in, date(2027, 3, 30), "March end")
    _event(logged_in, date(2027, 4, 2), "April start")

    resp = _get(client, view="week", y=2027, m=3, d=31)
    days = [c["date"] for c in resp.context["days"]]
    assert days[0] == date(2027, 3, 29) and days[-1] == date(2027, 4, 4)
    body = resp.content.decode()
    assert "March end" in body and "April start" in body
    # And the heading says which month each end is in, because "29 to 4" does
    # not.
    assert resp.context["period_label"] == "29 March to 4 April 2027"


def test_a_week_spanning_two_years_names_both(client, logged_in):
    _event(logged_in, date(2026, 12, 30), "Old year")
    _event(logged_in, date(2027, 1, 2), "New year")

    resp = _get(client, view="week", y=2027, m=1, d=1)
    days = [c["date"] for c in resp.context["days"]]
    assert days[0] == date(2026, 12, 28) and days[-1] == date(2027, 1, 3)
    body = resp.content.decode()
    assert "Old year" in body and "New year" in body
    assert resp.context["period_label"] == "28 December 2026 to 3 January 2027"


def test_a_week_inside_one_month_says_so_once(client, logged_in):
    resp = _get(client, view="week", y=2027, m=3, d=17)
    assert resp.context["period_label"] == "15 to 21 March 2027"


def test_one_date_gets_three_very_differently_sized_headings(client, logged_in):
    """The premise `.cal-month { flex-basis: 100% }` rests on.

    Switching view keeps the anchor (the test above this section pins that),
    so the same date is labelled three ways, and the three are nowhere near
    the same width. Measured in the page's own 26px display black at 1440px,
    the widest label each view can produce runs 210px (month), 399px (day)
    and 471px (week), against 146px for the narrowest month.

    That is why the title cannot share a flex line with the controls: a range
    that wide is a shove, not a wobble. If these three ever converge on one
    length, the layout guard in test_calendar.py can be revisited -- until
    then it is load-bearing.
    """
    labels = {
        view: _get(client, view=view, y=2026, m=12, d=30).context["period_label"]
        for view in ("month", "week", "day")
    }
    assert labels == {
        "month": "December 2026",
        "week": "28 December 2026 to 3 January 2027",
        "day": "Wednesday 30 December 2026",
    }
    widths = [len(v) for v in labels.values()]
    assert max(widths) - min(widths) >= 15, (
        "The three views' headings for one date are now close enough in "
        "length to reconsider the bar's layout."
    )


def test_the_day_view_covers_exactly_one_date(client, logged_in):
    _event(logged_in, date(2027, 3, 15), "On the day")
    _event(logged_in, date(2027, 3, 16), "The day after")

    body = _get(client, view="day", y=2027, m=3, d=15).content.decode()
    assert "On the day" in body
    assert "The day after" not in body


def test_the_last_day_of_a_month_pages_into_the_next_one(client, logged_in):
    """A day at the edge of a month. `next` has to cross the boundary rather
    than clamp inside it, and `prev` from the 1st has to cross back."""
    resp = _get(client, view="day", y=2027, m=3, d=31)
    assert "y=2027&m=4&d=1" in resp.context["next_url"]

    resp = _get(client, view="day", y=2027, m=4, d=1)
    assert "y=2027&m=3&d=31" in resp.context["prev_url"]


def test_a_week_pages_by_seven_days_across_a_year_end(client, logged_in):
    """The anchor moves seven days, and the week it lands in is the week
    before/after. 31 December 2026 sits in 28 Dec to 3 Jan, so `next` has to
    reach January 2027 rather than wrap inside December."""
    resp = _get(client, view="week", y=2026, m=12, d=31)
    assert "y=2026&m=12&d=24" in resp.context["prev_url"]
    assert "y=2027&m=1&d=7" in resp.context["next_url"]

    back = _get(client, view="week", y=2026, m=12, d=24)
    assert [c["date"] for c in back.context["days"]][0] == date(2026, 12, 21)
    fwd = _get(client, view="week", y=2027, m=1, d=7)
    assert [c["date"] for c in fwd.context["days"]][0] == date(2027, 1, 4)


def test_the_month_still_pages_by_month(client, logged_in):
    """Month navigation is unchanged: it steps year/month, not days, which is
    what keeps January's `prev` a link the next request forgives rather than
    an unrepresentable date raised while drawing the page."""
    resp = _get(client, view="month", y=2027, m=1, d=15)
    assert "y=2026&m=12" in resp.context["prev_url"]
    assert "y=2027&m=2" in resp.context["next_url"]

    body = _get(client, y=2027, m=3).content.decode()
    assert "March 2027" in body


def test_paging_a_month_keeps_the_day_and_clamps_it(client, logged_in):
    """31 March, previous month. February has no 31st, so the anchor lands on
    the last day February has rather than 500ing or silently resetting."""
    resp = _get(client, view="month", y=2027, m=3, d=31)
    assert "y=2027&m=2&d=31" in resp.context["prev_url"]

    landed = _get(client, view="week", y=2027, m=2, d=31)
    assert landed.status_code == 200
    assert landed.context["anchor"] == date(2027, 2, 28)


# ---------------------------------------------------------------------------
# The switcher. Its one job is to not move you.
# ---------------------------------------------------------------------------

def test_switching_view_keeps_the_date_you_were_looking_at(client, logged_in):
    resp = _get(client, view="month", y=2027, m=3, d=15)
    assert resp.context["week_url"] == "?view=week&y=2027&m=3&d=15"
    assert resp.context["day_url"] == "?view=day&y=2027&m=3&d=15"

    # And following it lands on the week that CONTAINS the 15th.
    week = _get(client, view="week", y=2027, m=3, d=15)
    assert [c["date"] for c in week.context["days"]][0] == date(2027, 3, 15)
    assert week.context["anchor"] == date(2027, 3, 15)


def test_the_switcher_marks_the_view_you_are_in(client, logged_in):
    for view, label in (("month", "Month"), ("week", "Week"), ("day", "Day")):
        body = _get(client, view=view, y=2027, m=3, d=15).content.decode()
        assert f'aria-current="page">{label}<' in body, view
        assert body.count('aria-current="page"') == 1, view


def test_switching_from_a_month_you_are_not_in_starts_at_the_first(client, logged_in):
    """No `?d=` and a month that is not this one: the anchor is the 1st, not
    today's day-of-month landing in a month it has nothing to do with."""
    resp = _get(client, y=2027, m=3)
    assert resp.context["anchor"] == date(2027, 3, 1)


def test_switching_from_this_month_starts_at_today(client, logged_in):
    """The switcher's most common path by far: open the calendar, click Week.
    It has to give this week."""
    today = timezone.localdate()
    resp = _get(client)
    assert resp.context["anchor"] == today

    week = _get(client, view="week", y=today.year, m=today.month, d=today.day)
    dates = [c["date"] for c in week.context["days"]]
    assert today in dates


# ---------------------------------------------------------------------------
# Anchors off the wire. Every one of these is hand-editable.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["99", "0", "-3", "abc", "", "1e5"])
@pytest.mark.parametrize("view", ["month", "week", "day"])
def test_a_nonsense_anchor_day_is_clamped_not_a_500(client, logged_in, view, bad):
    resp = _get(client, view=view, y=2027, m=3, d=bad)
    assert resp.status_code == 200, (view, bad)
    assert 1 <= resp.context["anchor"].day <= 31


@pytest.mark.parametrize("view", ["month", "week", "day"])
def test_the_last_representable_month_is_not_a_500_in_any_view(client, logged_in, view):
    """`?y=9999&m=12` already fell back in month view because the grid's
    trailing week would reach year 10000. Week and day do their own date
    arithmetic and must not reopen the hole."""
    resp = _get(client, view=view, y=9999, m=12, d=31)
    assert resp.status_code == 200
    assert resp.context["anchor"].year == timezone.localdate().year


@pytest.mark.parametrize("view", ["month", "week", "day"])
def test_the_first_representable_month_is_not_a_500_in_any_view(client, logged_in, view):
    """The other end: a "previous week" from 1 January of year 1 would step
    below MINYEAR while the link is being drawn.

    The month case was already broken before any of this: `_events_by_day`
    widens its query window by a day at each end, and `first - timedelta(1)`
    off 1 January year 1 raised OverflowError from inside the view. Fixed
    with the same no-op-at-the-edges step the nav links use.
    """
    resp = _get(client, view=view, y=1, m=1, d=1)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# The counts and the entries follow the range, because all three views ask
# one function for "what is on these dates".
# ---------------------------------------------------------------------------

def test_the_tally_counts_the_week_not_the_month(client, logged_in):
    _firm_deadline(date(2027, 3, 17))
    _firm_deadline(date(2027, 3, 24), name="Morgan Stanley", slug="ms")

    week = _get(client, view="week", y=2027, m=3, d=17)
    assert week.context["counts"]["deadline"] == 1
    month = _get(client, view="month", y=2027, m=3, d=17)
    assert month.context["counts"]["deadline"] == 2


def test_the_tally_counts_the_day_not_the_week(client, logged_in):
    _event(logged_in, date(2027, 3, 17), "Mine", hour=15)
    _event(logged_in, date(2027, 3, 18), "Also mine", hour=15)

    assert _get(client, view="day", y=2027, m=3,
                d=17).context["counts"]["event"] == 1
    assert _get(client, view="week", y=2027, m=3,
                d=17).context["counts"]["event"] == 2


@pytest.mark.parametrize("view", ["month", "week", "day"])
def test_an_empty_period_says_which_period_is_empty(client, logged_in, view):
    resp = _get(client, view=view, y=2027, m=3, d=15)
    assert f"Nothing on the calendar this {view}." in resp.content.decode()


# ---------------------------------------------------------------------------
# The day view's hour rail. Two of the four layers carry a real time and two
# never can — the rail is where that distinction has to hold.
# ---------------------------------------------------------------------------

def test_a_timed_entry_lands_in_its_own_hour(client, logged_in):
    _event(logged_in, date(2027, 3, 15), "Coffee with Ada", hour=15)
    resp = _get(client, view="day", y=2027, m=3, d=15)

    placed = {h["label"]: [e["title"] for e in h["events"]] for h in resp.context["hours"]}
    assert placed["3pm"] == ["Coffee with Ada"]
    assert all(not evs for label, evs in placed.items() if label != "3pm")


def test_a_dateless_entry_sits_in_the_all_day_band_not_at_midnight(client, logged_in):
    """A confirmed firm date has no clock time and never will. Dropping it on
    the rail at 12am would invent an appointment nobody scheduled."""
    _firm_deadline(date(2027, 3, 15))
    resp = _get(client, view="day", y=2027, m=3, d=15)

    assert [e["kind"] for e in resp.context["all_day_events"]] == ["deadline"]
    assert all(not h["events"] for h in resp.context["hours"])
    assert "Applications close" in resp.content.decode()


def test_the_rail_widens_to_reach_an_entry_outside_office_hours(client, logged_in):
    _event(logged_in, date(2027, 3, 15), "Early flight", hour=5)
    _event(logged_in, date(2027, 3, 15), "New York call", hour=23)

    labels = [h["label"] for h in _get(client, view="day", y=2027, m=3,
                                      d=15).context["hours"]]
    assert labels[0] == "5am" and labels[-1] == "11pm"
    assert "12pm" in labels, "and noon reads as noon, not 0pm"


def test_an_empty_day_still_draws_a_rail(client, logged_in):
    """The axis is the view. A day with nothing on it should read as an empty
    day, not as a missing page."""
    labels = [h["label"] for h in _get(client, view="day", y=2027, m=3,
                                       d=15).context["hours"]]
    assert labels[0] == "8am" and labels[-1] == "9pm"


def test_the_day_view_opens_its_entries(client, logged_in):
    """One date across the page's full width has room for where, who and the
    notes without a click; the seven-column grids do not."""
    _event(logged_in, date(2027, 3, 15), "Superday", hour=9)
    ev = CalendarEvent.objects.for_user(logged_in).get()
    ev.description = "Bring copies of the resume."
    ev.save(update_fields=["description"])

    day = _get(client, view="day", y=2027, m=3, d=15).content.decode()
    assert '<details class="cal-ev-wrap" open>' in day

    week = _get(client, view="week", y=2027, m=3, d=15).content.decode()
    assert '<details class="cal-ev-wrap" open>' not in week
    assert '<details class="cal-ev-wrap">' in week


# ---------------------------------------------------------------------------
# Today, in all three modes.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("view", ["month", "week", "day"])
def test_today_is_reachable_from_every_view_and_stays_in_it(client, logged_in, view):
    away = _get(client, view=view, y=2027, m=3, d=15)
    assert f'href="{reverse("crm:calendar")}?view={view}" data-today-link' in (
        _markup(away))
    # Not on screen from March 2027, so the link does its real navigation.
    assert "data-already-today" not in _today_link(away)

    landed = _get(client, view=view)
    assert landed.context["view"] == view
    assert landed.context["today_in_range"] is True
    assert "data-already-today" in _today_link(landed)


@pytest.mark.parametrize("view", ["month", "week", "day"])
def test_the_view_you_are_in_marks_today_for_the_control_to_find(
        client, logged_in, view):
    """`data-already-today` suppresses the reload and scrolls to today's own
    marker instead. Each view has to actually RENDER a marker for it, or the
    script has nothing to scroll to and the click does nothing twice over."""
    markup = _markup(_get(client, view=view))
    # A class attribute that ENDS in the marker, so this cannot pass on the
    # selector strings the click handler carries in its own source.
    assert ' is-today"' in markup, view
    if view == "day":
        assert 'class="cal-dayv is-today"' in markup


def test_today_in_range_follows_the_week_not_the_month(client, logged_in):
    """A week in this month that does not contain today. The month-view test
    for this could not tell the two apart."""
    today = timezone.localdate()
    other = today + timedelta(days=14)
    resp = _get(client, view="week", y=other.year, m=other.month, d=other.day)
    assert resp.context["today_in_range"] is False
    assert "data-already-today" not in _today_link(resp)


# ---------------------------------------------------------------------------
# Writes come back to where they were made.
# ---------------------------------------------------------------------------

def test_adding_from_a_week_returns_to_that_week(client, logged_in):
    resp = client.post(reverse("crm:calendar_add"), {
        "title": "Superday", "day": "2027-03-17", "kind": "event",
        "view": "week", "y": 2027, "m": 3, "d": 17,
    })
    assert resp.status_code == 302
    assert resp["Location"].endswith("?view=week&y=2027&m=3&d=17")


def test_a_bad_submission_re_renders_the_view_it_came_from(client, logged_in):
    resp = client.post(reverse("crm:calendar_add"), {
        "title": "Superday", "day": "not-a-date", "kind": "event",
        "view": "day", "y": 2027, "m": 3, "d": 17,
    })
    assert resp.status_code == 400
    assert resp.context["view"] == "day"
    assert resp.context["anchor"] == date(2027, 3, 17)


def test_removing_from_a_day_returns_to_that_day(client, logged_in):
    ev = _event(logged_in, date(2027, 3, 15), "Superday", hour=9)
    resp = client.post(reverse("crm:calendar_delete", args=[ev.pk]),
                       {"view": "day", "y": 2027, "m": 3, "d": 15})
    assert resp["Location"] == "/app/calendar/?view=day&y=2027&m=3&d=15"
    assert not CalendarEvent.objects.for_user(logged_in).exists()


def test_a_day_cell_adds_on_its_own_date_not_the_anchors(client, logged_in):
    """The week of 29 March holds days in April. The "+" on 1 April has to
    prefill 1 April, which the old month-only link could not express."""
    resp = _get(client, view="week", y=2027, m=3, d=31)
    april = [c for c in resp.context["days"] if c["date"] == date(2027, 4, 1)][0]
    assert april["add_url"] == "?view=week&y=2027&m=4&d=1&day=1#add"

    body = client.get(reverse("crm:calendar"),
                      {"view": "week", "y": 2027, "m": 4, "d": 1,
                       "day": 1}).content.decode()
    assert 'value="2027-04-01"' in body


# ---------------------------------------------------------------------------
# The month, unchanged. These are the shapes the other two views were built
# around rather than on top of.
# ---------------------------------------------------------------------------

def test_the_month_still_draws_six_weeks_of_cells(client, logged_in):
    resp = _get(client, y=2027, m=3)
    assert all(len(week) == 7 for week in resp.context["weeks"])
    markup = _markup(resp)
    assert '<div class="cal-week">' in markup
    assert 'class="cal-week is-week"' not in markup, "that modifier is week view's"


def test_the_month_still_greys_the_days_that_are_not_in_it(client, logged_in):
    resp = _get(client, y=2027, m=3)
    flat = [c for week in resp.context["weeks"] for c in week]
    assert any(not c["in_month"] for c in flat)
    assert "is-out" in _markup(resp)


def test_the_week_view_greys_nothing(client, logged_in):
    """Both halves of a straddling week are equally the week you asked for."""
    resp = _get(client, view="week", y=2027, m=3, d=31)
    assert all(c["in_month"] for c in resp.context["days"])
    assert "is-out" not in _markup(resp)
    assert 'class="cal-week is-week"' in _markup(resp)


def test_the_narrow_screen_agenda_lists_the_period_it_is_showing(client, logged_in):
    """The agenda is the phone layout for both grids. It used to walk the
    month's own week rows; it now reads one flat list, so the week view can
    hand it seven days through the same loop."""
    _event(logged_in, date(2027, 3, 30), "March end")
    _event(logged_in, date(2027, 4, 2), "April start")

    month = _get(client, y=2027, m=3).context["agenda_days"]
    assert all(c["date"].month == 3 for c in month)
    week = _get(client, view="week", y=2027, m=3, d=31).context["agenda_days"]
    assert len(week) == 7 and week[0]["date"] == date(2027, 3, 29)
