"""The Calendar: three layers on one timeline, and what each refuses to do.

Coverage knew a chat had been SCHEDULED but never when — the Today page's
"Coming up" says so in its own docstring, reporting when a chat was set up
"because we do not store a chat datetime anywhere". Meanwhile the capture
extractors were already pulling a real time off calendar invites and
dropping it. These tests pin the destination that was missing, plus the
tenancy and honesty rules that matter once a page starts showing dates.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from capture.gmail import apply_findings
from crm.models import CalendarEvent, Contact
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(email="jimmy@example.com", password="x")


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return user


def _at(days=0, hour=15):
    return timezone.localtime(timezone.now()).replace(
        hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)


# ---------------------------------------------------------------------------
# Layer 1 — chats captured from the mailbox.
# ---------------------------------------------------------------------------

def test_a_finding_with_a_time_becomes_a_calendar_chat(user):
    contact = Contact.all_objects.create(user=user, name="Ada Lovelace", email="ada@gs.com")
    when = _at(days=3)
    apply_findings(user, [{
        "name": "Ada Lovelace", "found": True, "email": "ada@gs.com",
        "thread_id": "t-1", "chat_status": "scheduled",
        "chat_scheduled_at": when.isoformat(),
    }])
    ev = CalendarEvent.objects.for_user(user).get()
    assert ev.kind == CalendarEvent.KIND_CHAT
    assert ev.source == CalendarEvent.SOURCE_CAPTURE
    assert ev.contact_id == contact.id
    assert "Ada Lovelace" in ev.title
    assert timezone.localtime(ev.starts_at).hour == when.hour


def test_the_twice_daily_sync_updates_one_row_not_many(user):
    """Keyed on (user, thread). The same invite resurfaces in the search
    window every run for days; stacking it would fill the month with one
    chat."""
    Contact.all_objects.create(user=user, name="Ada Lovelace", email="ada@gs.com")
    finding = {"name": "Ada Lovelace", "found": True, "email": "ada@gs.com",
               "thread_id": "t-1", "chat_status": "scheduled",
               "chat_scheduled_at": _at(days=3).isoformat()}
    for _ in range(4):
        apply_findings(user, [finding])
    assert CalendarEvent.objects.for_user(user).count() == 1


def test_a_reschedule_on_the_same_thread_moves_the_event(user):
    Contact.all_objects.create(user=user, name="Ada Lovelace", email="ada@gs.com")
    base = {"name": "Ada Lovelace", "found": True, "email": "ada@gs.com",
            "thread_id": "t-1", "chat_status": "scheduled"}
    apply_findings(user, [{**base, "chat_scheduled_at": _at(days=3).isoformat()}])
    moved = _at(days=5)
    apply_findings(user, [{**base, "chat_scheduled_at": moved.isoformat()}])

    ev = CalendarEvent.objects.for_user(user).get()
    assert timezone.localtime(ev.starts_at).date() == moved.date(), "moved, not duplicated"


def test_a_chat_with_no_stated_time_makes_no_event(user):
    """Most findings have no time. That is not an error and must not invent
    one — a made-up slot on a calendar is worse than an absent one."""
    Contact.all_objects.create(user=user, name="Ada Lovelace", email="ada@gs.com")
    apply_findings(user, [{"name": "Ada Lovelace", "found": True,
                           "email": "ada@gs.com", "thread_id": "t-1",
                           "chat_status": "scheduled"}])
    assert CalendarEvent.objects.for_user(user).count() == 0


def test_an_unreadable_time_makes_no_event(user):
    Contact.all_objects.create(user=user, name="Ada Lovelace", email="ada@gs.com")
    apply_findings(user, [{"name": "Ada Lovelace", "found": True,
                           "email": "ada@gs.com", "thread_id": "t-1",
                           "chat_status": "scheduled",
                           "chat_scheduled_at": "next Tuesday-ish"}])
    assert CalendarEvent.objects.for_user(user).count() == 0


def test_a_dry_run_puts_nothing_on_the_calendar(user):
    Contact.all_objects.create(user=user, name="Ada Lovelace", email="ada@gs.com")
    result = apply_findings(user, [{
        "name": "Ada Lovelace", "found": True, "email": "ada@gs.com",
        "thread_id": "t-1", "chat_status": "scheduled",
        "chat_scheduled_at": _at(days=2).isoformat()}], dry_run=True)
    assert CalendarEvent.all_objects.count() == 0
    assert result.chats_scheduled == 1, "but it still reports what it would add"


# ---------------------------------------------------------------------------
# Layer 2 — hand-added events.
# ---------------------------------------------------------------------------

def test_adding_an_event_by_hand(client, logged_in):
    day = _at(days=2).date()
    resp = client.post(reverse("crm:calendar_add"), {
        "title": "Superday", "day": day.isoformat(), "at": "09:30",
        "kind": "event", "location": "Their office",
        "description": "Bring copies of the resume.",
    })
    assert resp.status_code == 302
    ev = CalendarEvent.objects.for_user(logged_in).get()
    assert ev.title == "Superday"
    assert ev.source == CalendarEvent.SOURCE_MANUAL
    assert ev.all_day is False
    assert timezone.localtime(ev.starts_at).hour == 9


def test_an_event_with_no_time_is_all_day_not_midnight(client, logged_in):
    """"Superday on the 14th" is a fact about a day. Storing it as 00:00
    would sort it above a 9am chat and claim a precision nobody gave."""
    client.post(reverse("crm:calendar_add"), {
        "title": "Superday", "day": _at(days=2).date().isoformat(),
        "at": "", "kind": "event",
    })
    assert CalendarEvent.objects.for_user(logged_in).get().all_day is True


def test_a_bad_date_re_renders_the_form_with_what_was_typed(client, logged_in):
    resp = client.post(reverse("crm:calendar_add"), {
        "title": "Superday", "day": "not-a-date", "kind": "event",
    })
    assert resp.status_code == 400
    body = resp.content.decode()
    assert "Superday" in body, "the typed title survives the error"
    assert CalendarEvent.all_objects.count() == 0


def test_the_contact_dropdown_only_offers_your_own_people(client, logged_in, django_user_model):
    other = django_user_model.objects.create_user(email="other@x.com", password="x")
    Contact.all_objects.create(user=other, name="Someone Elses Contact")
    mine = Contact.all_objects.create(user=logged_in, name="My Contact")

    body = client.get(reverse("crm:calendar")).content.decode()
    assert "My Contact" in body
    assert "Someone Elses Contact" not in body
    assert str(mine.id) in body


def test_deleting_removes_only_your_own_event(client, logged_in, django_user_model):
    other = django_user_model.objects.create_user(email="other@x.com", password="x")
    theirs = CalendarEvent.all_objects.create(
        user=other, title="Theirs", starts_at=timezone.now())
    mine = CalendarEvent.all_objects.create(
        user=logged_in, title="Mine", starts_at=timezone.now())

    client.post(reverse("crm:calendar_delete", args=[theirs.pk]))
    assert CalendarEvent.all_objects.filter(pk=theirs.pk).exists(), "not yours to delete"

    client.post(reverse("crm:calendar_delete", args=[mine.pk]))
    assert not CalendarEvent.all_objects.filter(pk=mine.pk).exists()


# ---------------------------------------------------------------------------
# Layer 3 — confirmed firm deadlines, read-only.
# ---------------------------------------------------------------------------

def test_only_confirmed_deadlines_reach_the_calendar(client, logged_in):
    """The cadence engine acts only on `confirmed_official`; a calendar that
    drew rumours at the same weight would put countdowns on the page for
    events nobody has confirmed."""
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    today = timezone.localdate()
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="us",
                            event_kind="app_close",
                            date=today.replace(day=15), confidence=1.0)
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="hk",
                            event_kind="app_open",
                            date=today.replace(day=16), confidence=0.3)

    body = client.get(reverse("crm:calendar")).content.decode()
    assert "applications close" in body
    assert "applications open" not in body


def test_the_month_can_be_paged_and_a_bad_month_falls_back_to_today(client, logged_in):
    ok = client.get(reverse("crm:calendar"), {"y": 2027, "m": 3})
    assert ok.status_code == 200
    assert "March 2027" in ok.content.decode()

    bad = client.get(reverse("crm:calendar"), {"y": "abc", "m": "99"})
    assert bad.status_code == 200, "a hand-typed querystring must not 500"
    assert timezone.localdate().strftime("%B %Y") in bad.content.decode()


def test_the_calendar_needs_a_login(client):
    resp = client.get(reverse("crm:calendar"))
    assert resp.status_code == 302 and "login" in resp["Location"]


# ---------------------------------------------------------------------------
# The page as a thing you can USE.
# ---------------------------------------------------------------------------

def test_a_bad_submission_still_reports_the_months_real_counts(client, logged_in):
    """The error re-render used to pass hard-coded zeroes, so mistyping a date
    made the month's real deadlines read "0" — the page contradicting itself
    at the moment it tells you you got something wrong."""
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    today = timezone.localdate()
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="us",
                            event_kind="app_close",
                            date=today.replace(day=15), confidence=1.0)

    resp = client.post(reverse("crm:calendar_add"), {
        "title": "Superday", "day": "not-a-date", "kind": "event",
        "y": today.year, "m": today.month,
    })
    assert resp.status_code == 400
    assert "<b>1</b> deadlines" in resp.content.decode()


def test_clicking_a_day_prefills_that_date_and_opens_the_form(client, logged_in):
    today = timezone.localdate()
    body = client.get(reverse("crm:calendar"),
                      {"y": today.year, "m": today.month, "day": 14}).content.decode()
    assert f'value="{today.replace(day=14).isoformat()}"' in body
    assert "<details class=\"cal-add\" id=\"add\" open>" in body


def test_an_impossible_day_is_ignored_not_an_error(client, logged_in):
    """The day is a convenience. A hand-typed 99 should still leave a usable
    form rather than a 500 or a date in the following month."""
    for bad in ("99", "0", "-3", "abc", ""):
        resp = client.get(reverse("crm:calendar"), {"day": bad})
        assert resp.status_code == 200, bad
        assert "cal-add\" id=\"add\" open" not in resp.content.decode(), bad


def test_your_own_events_can_be_removed_from_the_page(client, logged_in):
    """The delete endpoint shipped with no way to reach it. This pins that a
    rendered event carries its own Remove control."""
    ev = CalendarEvent.all_objects.create(
        user=logged_in, title="Superday", starts_at=timezone.now(),
        description="Bring copies of the resume.")
    body = client.get(reverse("crm:calendar")).content.decode()
    assert reverse("crm:calendar_delete", args=[ev.pk]) in body
    assert "Bring copies of the resume." in body, "and the notes are readable, not just a tooltip"


def test_a_firm_deadline_offers_no_remove_button(client, logged_in):
    """Directory data is not the user's to delete from here."""
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="us",
                            event_kind="app_close",
                            date=timezone.localdate().replace(day=15), confidence=1.0)
    body = client.get(reverse("crm:calendar")).content.decode()
    assert "applications close" in body
    assert "Remove" not in body


@pytest.mark.parametrize("hour,minute,expected", [
    (9, 0, "9am"), (9, 30, "9:30am"), (12, 0, "12pm"), (12, 5, "12:05pm"),
    (15, 0, "3pm"), (0, 0, "12am"), (23, 45, "11:45pm"),
])
def test_the_chip_time_is_short_and_gets_noon_and_midnight_right(
        client, logged_in, hour, minute, expected):
    """`time:"g:ia"` renders "12:00p.m." — noise in the narrowest column on
    the page. Stripping the leading zero off %I turns 12 into an empty
    string, so noon and midnight are the cases worth pinning."""
    when = timezone.localtime(timezone.now()).replace(
        day=14, hour=hour, minute=minute, second=0, microsecond=0)
    CalendarEvent.all_objects.create(
        user=logged_in, title="Superday", starts_at=when, all_day=False)
    body = client.get(reverse("crm:calendar")).content.decode()
    assert f'<span class="cal-ev-time">{expected}</span>' in body


def _style_block(body: str) -> str:
    return body.split("<style>", 1)[1].split("</style>", 1)[0]


def _outside_media(css: str) -> str:
    """The stylesheet's base rules: @media blocks and comments both removed.

    Comments have to go first. A rule this file forbids is exactly the thing a
    comment next to it wants to NAME, so scanning raw source makes the guard
    fire on the explanation of the bug rather than the bug — which is how it
    failed the first time it ran.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    out, i = [], 0
    while i < len(css):
        at = css.find("@media", i)
        if at == -1:
            out.append(css[i:])
            break
        out.append(css[i:at])
        depth, j = 0, css.index("{", at)
        while j < len(css):
            depth += (css[j] == "{") - (css[j] == "}")
            if depth == 0:
                break
            j += 1
        i = j + 1
    return "".join(out)


def test_the_agenda_is_never_hidden_by_a_rule_that_outranks_the_breakpoint(client, logged_in):
    """The narrow layout switch, guarded at the source.

    `.cal-agenda { display: none }` written OUTSIDE a media query has the same
    specificity as the `display: block` inside the max-width one, so the later
    of the two wins at every width. Written last, it hid the agenda on narrow
    screens while the grid was already hidden there: below 820px the month
    rendered as nothing at all. The fix is two mutually exclusive queries, and
    this pins it — any bare display rule for `.cal-agenda` is the bug coming
    back.

    Same failure family as the `.wa-col-short` rule on the settings page, which
    is why it is worth a test rather than a comment.
    """
    css = _style_block(client.get(reverse("crm:calendar")).content.decode())
    base = _outside_media(css)
    assert ".cal-agenda {" not in base, (
        "A bare `.cal-agenda` display rule outranks the breakpoint that shows "
        "it. Put it inside `@media (min-width: 821px)` instead."
    )
    assert "@media (max-width: 820px)" in css and "@media (min-width: 821px)" in css
    # And the counterweight: the scan is reading a real stylesheet.
    assert ".cal-grid {" in base


# ---------------------------------------------------------------------------
# The grid as a readable surface: uniform cells, intensity, the month rail.
# ---------------------------------------------------------------------------

def test_a_day_is_tinted_by_how_much_is_on_it(client, logged_in):
    day = timezone.localtime(timezone.now()).replace(
        day=20, hour=9, minute=0, second=0, microsecond=0)
    for i in range(2):
        CalendarEvent.all_objects.create(
            user=logged_in, title=f"Thing {i}", starts_at=day + timedelta(hours=i))
    body = client.get(reverse("crm:calendar")).content.decode()
    assert 'class="cal-day load-2' in body
    assert 'class="cal-day load-0' in body, "quiet days stay untinted"


def test_intensity_stops_climbing_past_three(client, logged_in):
    """Capped on purpose. A day with nine things is not three times harder to
    read than a day with three, and an uncapped ramp just goes muddy."""
    day = timezone.localtime(timezone.now()).replace(
        day=20, hour=9, minute=0, second=0, microsecond=0)
    for i in range(9):
        CalendarEvent.all_objects.create(
            user=logged_in, title=f"Thing {i}", starts_at=day + timedelta(minutes=i))
    body = client.get(reverse("crm:calendar")).content.decode()
    assert "load-3" in body
    assert "load-4" not in body and "load-9" not in body
    assert "9 on this day" in body, "the exact count still reaches the tooltip"


def test_the_rail_spans_two_years_around_today_and_marks_where_you_are(client, logged_in):
    today = timezone.localdate()
    body = client.get(reverse("crm:calendar")).content.decode()
    assert body.count('class="mrail-m') == 24
    back = _shift_for_test(today.year, today.month, -6)
    fwd = _shift_for_test(today.year, today.month, 17)
    assert f'href="?y={back[0]}&m={back[1]}"' in body
    assert f'href="?y={fwd[0]}&m={fwd[1]}"' in body
    assert 'aria-current="page"' in body
    # Twenty zeroes down the strip is twenty things to read past. An
    # empty column says "nothing" by being flat; the count still reaches
    # a screen reader through the link's accessible name.
    assert 'Feb 2026, 0 items' in body, 'the count stays in the a11y name'
    assert '<span class="mrail-n" aria-hidden="true"></span>' in body, \
        'and an empty month prints no visible figure'


def test_the_rail_counts_both_your_events_and_confirmed_deadlines(client, logged_in):
    today = timezone.localdate()
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="us",
                            event_kind="app_close",
                            date=today.replace(day=15), confidence=1.0)
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="hk",
                            event_kind="app_open",
                            date=today.replace(day=16), confidence=0.3)
    CalendarEvent.all_objects.create(
        user=logged_in, title="Superday",
        starts_at=timezone.localtime(timezone.now()).replace(day=20))

    rail = client.get(reverse("crm:calendar")).context["rail"]
    now = next(m for m in rail if m["is_now"])
    assert now["count"] == 2, "one confirmed deadline plus one event; the rumour is not counted"
    assert now["bar_pct"] == 100, "and it is the busiest month on the rail"


def test_a_month_you_are_not_looking_at_is_not_marked_active(client, logged_in):
    today = timezone.localdate()
    other_y, other_m = _shift_for_test(today.year, today.month, 3)
    rail = client.get(reverse("crm:calendar"),
                      {"y": other_y, "m": other_m}).context["rail"]
    active = [m for m in rail if m["active"]]
    assert len(active) == 1 and (active[0]["y"], active[0]["m"]) == (other_y, other_m)
    now = next(m for m in rail if m["is_now"])
    assert not now["active"], "today's month is still marked, but as now, not as active"


def _shift_for_test(year, month, by):
    m = month - 1 + by
    return year + m // 12, m % 12 + 1


def test_every_week_lays_out_on_identical_columns(client, logged_in):
    """Each week is its own grid container, so a bare `1fr` — which is
    `minmax(auto, 1fr)` — lets a long title set that column's minimum in ITS
    week alone, and the six weeks stop lining up down the page. Reported as
    "each date box should be the same". A zero floor makes the tracks purely
    proportional so all six grids agree.
    """
    css = _style_block(client.get(reverse("crm:calendar")).content.decode())
    grid = [line for line in css.splitlines() if ".cal-week {" in line]
    assert grid, "the week grid rule moved; this guard needs updating"
    assert "repeat(7, minmax(0, 1fr))" in grid[0], (
        "Week columns must have a zero minimum, or long entry titles widen "
        "their own week's column and the weeks misalign."
    )
    assert "repeat(7, 1fr)" not in grid[0]


# ---------------------------------------------------------------------------
# Timezone anchoring: what a reported time MEANS.
# ---------------------------------------------------------------------------

def test_an_offset_carrying_time_passes_through_untouched(user):
    """"10am HKT" arrives as +08:00. The stored instant must be exactly that
    instant — 02:00 UTC — regardless of server or account settings."""
    Contact.all_objects.create(user=user, name="Jim Li", email="jim@jefferies.com")
    apply_findings(user, [{
        "name": "Jim Li", "found": True, "email": "jim@jefferies.com",
        "thread_id": "t-hk", "chat_status": "scheduled",
        "chat_scheduled_at": "2026-08-05T10:00:00+08:00"}])
    ev = CalendarEvent.objects.for_user(user).get()
    assert ev.starts_at.isoformat() == "2026-08-05T02:00:00+00:00"


def test_a_naive_time_is_anchored_to_the_users_own_zone(user, django_user_model):
    """The sync runs in a management command, which never passes through
    TimezoneMiddleware — so "the current timezone" there is the server's UTC.
    A naive "10am" from a Hong Kong user's thread must still mean 10am in
    Hong Kong (02:00 UTC), not 10am UTC (6pm her time)."""
    user.timezone = "Asia/Hong_Kong"
    user.save(update_fields=["timezone"])
    Contact.all_objects.create(user=user, name="Jim Li", email="jim@jefferies.com")
    apply_findings(user, [{
        "name": "Jim Li", "found": True, "email": "jim@jefferies.com",
        "thread_id": "t-naive", "chat_status": "scheduled",
        "chat_scheduled_at": "2026-08-05T10:00:00"}])
    ev = CalendarEvent.objects.for_user(user).get()
    assert ev.starts_at.isoformat() == "2026-08-05T02:00:00+00:00"


def test_an_unset_or_garbage_zone_falls_back_to_the_project_default(user):
    """Same discipline as TimezoneMiddleware: a blank or unloadable zone name
    must not crash the sync — it falls back to settings.TIME_ZONE (UTC)."""
    user.timezone = "Not/A_Zone"
    user.save(update_fields=["timezone"])
    Contact.all_objects.create(user=user, name="Jim Li", email="jim@jefferies.com")
    apply_findings(user, [{
        "name": "Jim Li", "found": True, "email": "jim@jefferies.com",
        "thread_id": "t-bad", "chat_status": "scheduled",
        "chat_scheduled_at": "2026-08-05T10:00:00"}])
    ev = CalendarEvent.objects.for_user(user).get()
    assert ev.starts_at.isoformat() == "2026-08-05T10:00:00+00:00"


# ---------------------------------------------------------------------------
# The ICS feed — the calendar reaching the calendar app the user lives in.
# ---------------------------------------------------------------------------

def test_the_feed_serves_events_and_confirmed_deadlines(client, user):
    c = Contact.all_objects.create(user=user, name="Ada Lovelace")
    CalendarEvent.all_objects.create(
        user=user, contact=c, title="Chat with Ada Lovelace",
        starts_at=_at(days=2), kind="chat", thread_id="t-ics")
    firm = Firm.objects.create(slug="gs", name="Goldman Sachs")
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="us",
                            event_kind="app_close",
                            date=timezone.localdate() + timedelta(days=9),
                            confidence=1.0)
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="hk",
                            event_kind="app_open",
                            date=timezone.localdate() + timedelta(days=9),
                            confidence=0.3)

    body = client.get(f"/app/calendar/feed/{user.calendar_token}.ics").content.decode()
    assert "BEGIN:VCALENDAR" in body
    assert "Chat with Ada Lovelace" in body
    assert "applications close" in body
    assert "applications open" not in body, "rumours stay off the feed too"


def test_a_wrong_token_is_a_404_not_an_empty_calendar(client, user):
    resp = client.get("/app/calendar/feed/not-a-real-token.ics")
    assert resp.status_code == 404


def test_the_feed_is_tenant_scoped(client, user, django_user_model):
    other = django_user_model.objects.create_user(
        email="other-ics@x.com", password="x")
    CalendarEvent.all_objects.create(
        user=other, title="Their private chat", starts_at=_at(days=1))
    body = client.get(f"/app/calendar/feed/{user.calendar_token}.ics").content.decode()
    assert "Their private chat" not in body


def test_the_feed_needs_no_session(client, user):
    """Calendar apps fetch from their own servers, cookie-less. The token IS
    the auth."""
    c = client.get(f"/app/calendar/feed/{user.calendar_token}.ics")
    assert c.status_code == 200
    assert c["Content-Type"].startswith("text/calendar")


# ---------------------------------------------------------------------------
# Layer 4 — the closing dates of roles this user tracks.
#
# The layer exists because layer 3 answers a different question: a FirmDate is
# the firm's whole cycle, while these are the postings the user actually
# starred. They were extracted, stored, shown on the feed, and invisible on the
# two surfaces that exist to warn you.
# ---------------------------------------------------------------------------

def _tracked_role(user, *, days, title="Summer Analyst", status="", dismissed=False,
                  firm=None, url=None):
    from analytics.models import UserOpportunity
    from directory.models import Opportunity

    firm = firm or Firm.objects.filter(slug="gs").first() or Firm.objects.create(
        slug="gs", name="Goldman Sachs")
    opp = Opportunity.objects.create(
        firm=firm, title=title, status="open",
        deadline=timezone.localdate() + timedelta(days=days),
        url=url or f"https://gs.com/{title.lower().replace(' ', '-')}-{days}")
    UserOpportunity.all_objects.create(
        user=user, opportunity=opp, applied_status=status, dismissed=dismissed)
    return opp


def test_a_tracked_roles_deadline_lands_on_the_calendar(logged_in, client):
    _tracked_role(logged_in, days=5, title="Summer Analyst")
    body = client.get("/app/calendar/").content.decode()
    assert "Summer Analyst" in body


def test_an_untracked_roles_deadline_stays_off_the_calendar(logged_in, client):
    from directory.models import Opportunity

    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    Opportunity.objects.create(
        firm=firm, title="Nobody Tracks This", status="open",
        deadline=timezone.localdate() + timedelta(days=5),
        url="https://ms.com/untracked")
    body = client.get("/app/calendar/").content.decode()
    assert "Nobody Tracks This" not in body


def test_a_dismissed_role_stays_off_the_calendar(logged_in, client):
    _tracked_role(logged_in, days=5, title="Not For Me", dismissed=True)
    body = client.get("/app/calendar/").content.decode()
    assert "Not For Me" not in body


def test_a_finished_application_stops_showing_its_deadline(logged_in, client):
    _tracked_role(logged_in, days=5, title="Already Done", status="closed")
    body = client.get("/app/calendar/").content.decode()
    assert "Already Done" not in body


def test_one_users_tracked_deadline_is_not_anothers(client, user, django_user_model):
    other = django_user_model.objects.create_user(email="other-uo@x.com", password="x")
    _tracked_role(other, days=5, title="Their Saved Role")
    client.force_login(user)
    body = client.get("/app/calendar/").content.decode()
    assert "Their Saved Role" not in body


def test_the_feed_carries_tracked_deadlines_with_alarms(client, user):
    _tracked_role(user, days=6, title="Summer Analyst",
                  url="https://gs.com/sa-2028")
    body = client.get(f"/app/calendar/feed/{user.calendar_token}.ics").content.decode()
    assert "Summer Analyst closes" in body
    assert "https://gs.com/sa-2028" in body
    # A week out and a day out. The alarms are the reason to subscribe rather
    # than visit: a deadline you have to remember to check is one you can miss.
    assert "TRIGGER;RELATED=START:-P7D" in body
    assert "TRIGGER;RELATED=START:-P1D" in body


def test_a_date_that_opens_something_gets_no_alarm(client, user):
    firm = Firm.objects.create(slug="jpm", name="J.P. Morgan")
    FirmDate.objects.create(firm=firm, cycle="SA 2028", region="us",
                            event_kind="app_open",
                            date=timezone.localdate() + timedelta(days=9),
                            confidence=1.0)
    body = client.get(f"/app/calendar/feed/{user.calendar_token}.ics").content.decode()
    assert "applications open" in body
    assert "BEGIN:VALARM" not in body, "nothing is lost by reading an opening late"
