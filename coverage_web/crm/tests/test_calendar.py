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


# --- a reschedule that lands on a DIFFERENT thread ------------------------ #
#
# The shape below is a real one from the founder's mailbox (read-only,
# 2026-08-25): lily.liu@barclays.com sent "Accepted: Jimmy <> Lily Coffee
# Chat" on thread 1a0346eb227c8a0b, and then "New Time Proposed: Jimmy (USC)
# <> Lily Coffee Chat" on a DIFFERENT thread, 1a038f4c6b1e59b3 — Google
# starts a fresh thread for a counter-proposal. Keyed on the thread alone
# that is two chats, one of them at a time Lily had just moved away from.
# The .ics UID is the same on both, because RFC 5545 holds it constant
# across REQUEST / REPLY / COUNTER for one event.

LILY_UID = "0abc1def2ghi3jkl@google.com"


def _lily(thread_id, when, *, sent_at=None, uid=LILY_UID, summary="Coffee Chat"):
    return {
        "name": "Lily Liu", "found": True, "email": "lily.liu@barclays.com",
        "thread_id": thread_id, "chat_status": "scheduled",
        "chat_scheduled_at": when.isoformat(), "ics_uid": uid,
        "occurred_at": sent_at.isoformat() if sent_at else None,
        "evidence": f"Calendar invite received: {summary}",
    }


def test_a_counter_proposal_on_a_new_thread_moves_the_chat(user):
    Contact.all_objects.create(user=user, name="Lily Liu", email="lily.liu@barclays.com")
    accepted, proposed = _at(days=3, hour=15), _at(days=4, hour=11)

    apply_findings(user, [_lily("1a0346eb227c8a0b", accepted)])
    apply_findings(user, [_lily("1a038f4c6b1e59b3", proposed)])

    ev = CalendarEvent.objects.for_user(user).get()
    assert timezone.localtime(ev.starts_at) == proposed, "moved, not duplicated"
    assert ev.thread_id == "1a038f4c6b1e59b3", "the thread that spoke last"
    assert ev.ics_uid == LILY_UID


def test_the_older_invite_cannot_drag_the_chat_back(user):
    """Findings are only sorted by time in the backfill. Applied out of
    order, the stale "Accepted:" must not win: one row at the wrong time is
    the same wrong answer as two rows, just quieter."""
    Contact.all_objects.create(user=user, name="Lily Liu", email="lily.liu@barclays.com")
    accepted, proposed = _at(days=3, hour=15), _at(days=4, hour=11)
    monday = timezone.now() - timedelta(days=2)

    apply_findings(user, [_lily("1a038f4c6b1e59b3", proposed, sent_at=monday)])
    apply_findings(user, [
        _lily("1a0346eb227c8a0b", accepted, sent_at=monday - timedelta(days=1))
    ])

    ev = CalendarEvent.objects.for_user(user).get()
    assert timezone.localtime(ev.starts_at) == proposed
    assert ev.thread_id == "1a038f4c6b1e59b3"


def test_a_chat_already_duplicated_is_collapsed_on_the_next_sync(user):
    """The rows this bug already wrote are on disk with no UID at all. The
    next sync has to reconcile them, not just stop adding more."""
    contact = Contact.all_objects.create(
        user=user, name="Lily Liu", email="lily.liu@barclays.com")
    accepted, proposed = _at(days=3, hour=15), _at(days=4, hour=11)
    for thread, when in (("1a0346eb227c8a0b", accepted), ("1a038f4c6b1e59b3", proposed)):
        CalendarEvent.all_objects.create(
            user=user, contact=contact, thread_id=thread, title="Chat with Lily Liu",
            starts_at=when, kind=CalendarEvent.KIND_CHAT,
            source=CalendarEvent.SOURCE_CAPTURE,
        )

    apply_findings(user, [
        _lily("1a0346eb227c8a0b", accepted),
        _lily("1a038f4c6b1e59b3", proposed),
    ])

    ev = CalendarEvent.objects.for_user(user).get()
    assert timezone.localtime(ev.starts_at) == proposed
    assert ev.thread_id == "1a038f4c6b1e59b3"


def test_two_genuinely_separate_invites_stay_two_events(user):
    """The fix must not over-merge: two chats with the same person are two
    rows, and it is their distinct UIDs that say so."""
    Contact.all_objects.create(user=user, name="Lily Liu", email="lily.liu@barclays.com")
    apply_findings(user, [
        _lily("thread-a", _at(days=3), uid="first@google.com"),
        _lily("thread-b", _at(days=10), uid="second@google.com"),
    ])
    assert CalendarEvent.objects.for_user(user).count() == 2


def test_an_invite_with_no_uid_still_keys_on_its_thread(user):
    """The UID is the better key, not a required one. A sender whose .ics
    will not parse keeps exactly the behaviour they had before."""
    Contact.all_objects.create(user=user, name="Lily Liu", email="lily.liu@barclays.com")
    moved = _at(days=6)
    apply_findings(user, [_lily("thread-a", _at(days=3), uid=None)])
    apply_findings(user, [_lily("thread-a", moved, uid=None)])

    ev = CalendarEvent.objects.for_user(user).get()
    assert timezone.localtime(ev.starts_at) == moved
    assert ev.ics_uid == ""


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
    assert "Applications close" in body
    assert "Applications open" not in body


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
    body = resp.content.decode()
    # One deadline, singular. The negative guard is load-bearing: the
    # singular string is a substring of the plural one, so the positive
    # assertion alone still passes against the old hard-coded "s".
    assert "<b>1</b> Deadline" in body
    assert "<b>1</b> Deadlines" not in body
    assert "<b>0</b> Chats" in body and "<b>0</b> Events" in body


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
    assert "Applications close" in body
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
    assert "Applications close" in body
    assert "Applications open" not in body, "rumours stay off the feed too"


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

def _tracked_role(user, *, day, title="Summer Analyst", status="", dismissed=False,
                  firm=None, url=None, posting_status="open"):
    """`day` is a day of THIS month, the same anchoring `_firm_date` uses.

    It used to be `days`, an offset from today, which reads better and is
    wrong: the grid and the month rail are both bounded by `_month_bounds`,
    so in the last week of any month `today + 7 days` files the fixture under
    NEXT month and the assertion goes looking for it on a page the test never
    asked for. Nothing about these tests is actually relative to today —
    `_tracked_deadlines` has no past/future filter and `is_posting_closed`
    reads `Opportunity.status`, not the date — so the month is pinned instead.
    Days 15-18 are used throughout: every month has them.

    `status` is the STUDENT's funnel stage (`applied_status`);
    `posting_status` is the POSTING's own (`Opportunity.status`, written by the
    nightly reverify pass). Both spell "closed" and they mean different
    things — see directory/deadlines.py's `is_posting_closed`."""
    from analytics.models import UserOpportunity
    from directory.models import Opportunity

    firm = firm or Firm.objects.filter(slug="gs").first() or Firm.objects.create(
        slug="gs", name="Goldman Sachs")
    opp = Opportunity.objects.create(
        firm=firm, title=title, status=posting_status,
        deadline=timezone.localdate().replace(day=day),
        url=url or f"https://gs.com/{title.lower().replace(' ', '-')}-{day}")
    UserOpportunity.all_objects.create(
        user=user, opportunity=opp, applied_status=status, dismissed=dismissed)
    return opp


def _grid(client, when):
    """The month grid for the month `when` falls in.

    A bare GET of the calendar renders THIS month, so a fixture written as
    "five days out" silently falls off the grid for the last week of every
    month — a presence assertion then fails, and an absence assertion passes
    for the wrong reason, according to the date the suite happens to run.
    (Six of the tests below did exactly that on 2026-08-27, with the product
    working correctly.) `_firm_date` further down already sidesteps this by
    pinning to the 15th; asking for the deadline's OWN month is the same
    guard without changing what each fixture date means.
    """
    return client.get(reverse("crm:calendar"), {"y": when.year, "m": when.month})


def test_a_tracked_roles_deadline_lands_on_the_calendar(logged_in, client):
    _tracked_role(logged_in, day=15, title="Summer Analyst")
    body = client.get("/app/calendar/").content.decode()
    assert "Summer Analyst" in body


def test_an_untracked_roles_deadline_stays_off_the_calendar(logged_in, client):
    from directory.models import Opportunity

    firm = Firm.objects.create(slug="ms", name="Morgan Stanley")
    opp = Opportunity.objects.create(
        firm=firm, title="Nobody Tracks This", status="open",
        # In-month, per `_tracked_role`: a deadline that drifts off this
        # month's grid would pass this assertion for the wrong reason.
        deadline=timezone.localdate().replace(day=15),
        url="https://ms.com/untracked")
    body = _grid(client, opp.deadline).content.decode()
    assert "Nobody Tracks This" not in body


def test_a_dismissed_role_stays_off_the_calendar(logged_in, client):
    _tracked_role(logged_in, day=15, title="Not For Me", dismissed=True)
    body = client.get("/app/calendar/").content.decode()
    assert "Not For Me" not in body


def test_a_finished_application_stops_showing_its_deadline(logged_in, client):
    _tracked_role(logged_in, day=15, title="Already Done", status="closed")
    body = client.get("/app/calendar/").content.decode()
    assert "Already Done" not in body


def test_one_users_tracked_deadline_is_not_anothers(client, user, django_user_model):
    other = django_user_model.objects.create_user(email="other-uo@x.com", password="x")
    opp = _tracked_role(other, day=15, title="Their Saved Role")
    client.force_login(user)
    body = _grid(client, opp.deadline).content.decode()
    assert "Their Saved Role" not in body


def test_the_feed_carries_tracked_deadlines_with_alarms(client, user):
    _tracked_role(user, day=16, title="Summer Analyst",
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
    assert "Applications open" in body
    assert "BEGIN:VALARM" not in body, "nothing is lost by reading an opening late"


def test_a_prose_read_deadline_is_marked_on_the_calendar(logged_in, client):
    """Layer 4 does not copy layer 3's confidence bar — a posting's own stated
    date is worth showing even when we read it out of prose rather than a
    published field (92 of 121 dated open roles). It is MARKED instead of
    withheld, on the grid and in the feed a phone subscribes to."""
    opp = _tracked_role(logged_in, day=17, title="Reported Analyst")
    opp.confidence = 0.6
    opp.save(update_fields=["confidence"])

    body = _grid(client, opp.deadline).content.decode()
    assert "Reported Analyst" in body
    assert "reported date" in body, "the caveat must be spoken, not only hovered"

    ics = client.get(f"/app/calendar/feed/{logged_in.calendar_token}.ics").content.decode()
    assert "(reported)" in ics


def test_a_provider_stated_deadline_is_not_marked(logged_in, client):
    opp = _tracked_role(logged_in, day=17, title="Stated Analyst")
    opp.confidence = 1.0
    opp.save(update_fields=["confidence"])

    ics = client.get(f"/app/calendar/feed/{logged_in.calendar_token}.ics").content.decode()
    assert "Stated Analyst closes" in ics
    assert "(reported)" not in ics


# ---------------------------------------------------------------------------
# Layer 4 — a tracked role's title reads the same here as it does in the feed.
#
# The calendar applied no smart_title at all: `_role_label` returned the raw
# `Opportunity.title` and _calendar_event.html rendered {{ ev.title }}
# unfiltered, so every tracked, dated role read differently on the two pages.
# ---------------------------------------------------------------------------
def test_a_tracked_role_reads_the_same_on_the_calendar_as_in_the_feed(client, logged_in):
    from analytics.models import UserOpportunity
    from core.templatetags.textstyle import smart_title
    from directory.models import Opportunity

    raw = "Discovery Program: equity + macro research (On-site)"
    firm = Firm.objects.create(slug="pjt", name="PJT Partners")
    today = timezone.localdate()
    opp = Opportunity.objects.create(
        firm=firm, title=raw, bucket="internship", status="open",
        url="https://pjt.com/discovery", deadline=today.replace(day=15),
    )
    UserOpportunity.all_objects.create(user=logged_in, opportunity=opp)

    body = client.get(reverse("crm:calendar")).content.decode()
    shown = smart_title(raw)
    assert shown in body, "the calendar shows the standardized title"
    assert raw not in body, "and not the raw scrape casing"
    # The clause-boundary rule is what makes the two agree on this title.
    assert "Discovery Program: Equity + Macro Research (On-Site)" == shown


def test_the_ics_feed_carries_the_same_standardized_title(client, logged_in):
    """The .ics summary is the copy that lands on a phone's lock screen, so it
    is the one that most needs to match. Same helper, so it cannot drift."""
    from analytics.models import UserOpportunity
    from directory.models import Opportunity

    firm = Firm.objects.create(slug="bofa", name="Bank of America")
    opp = Opportunity.objects.create(
        firm=firm, title="campus insight forum: the power to lead",
        bucket="event", status="open", url="https://bofa.com/forum",
        deadline=timezone.localdate() + timedelta(days=5),
    )
    UserOpportunity.all_objects.create(user=logged_in, opportunity=opp)

    logged_in.refresh_from_db()
    body = client.get(
        reverse("crm:calendar_ics", args=[logged_in.calendar_token])
    ).content.decode()
    assert "Campus Insight Forum: The Power to Lead" in body


def test_a_curated_firm_name_is_not_recased_by_the_calendar(client, logged_in):
    """`smart_title` rewrites "PIMCO" to "Pimco". Firm names are curated, not
    scraped, so only the ROLE half of the label goes through the filter."""
    from analytics.models import UserOpportunity
    from directory.models import Opportunity

    firm = Firm.objects.create(slug="pimco", name="PIMCO")
    opp = Opportunity.objects.create(
        firm=firm, title="summer analyst programme", bucket="internship",
        status="open", url="https://pimco.com/sa",
        deadline=timezone.localdate().replace(day=15),
    )
    UserOpportunity.all_objects.create(user=logged_in, opportunity=opp)

    body = client.get(reverse("crm:calendar")).content.decode()
    assert "PIMCO · Summer Analyst Programme" in body


# ---------------------------------------------------------------------------
# Layer 4 — identity duplicates must fold, the same way Browse Openings and
# My Applications do. _tracked_deadlines() never called fold_duplicates, so
# a requisition filed under two candidate-pool addresses and tracked under
# both showed up twice on the grid, twice in the month rail's count, and
# twice in the .ics feed. See directory/dupes.py and the round-8 dedup
# finding this fix addresses.
# ---------------------------------------------------------------------------

def _duplicate_pair(user, *, day, status_a="", status_b=""):
    """`day` is a day of this month, for the reason `_tracked_role` gives."""
    from analytics.models import UserOpportunity
    from directory.models import Opportunity

    firm = Firm.objects.filter(slug="bofa-dup").first() or Firm.objects.create(
        slug="bofa-dup", name="Bank of America")
    deadline = timezone.localdate().replace(day=day)
    opp_a = Opportunity.objects.create(
        firm=firm, title="Campus Insight Forum", location="New York, NY",
        bucket="event", status="open", deadline=deadline,
        url="https://bankcampuscareers.tal.net/pl/1/opp/14594")
    opp_b = Opportunity.objects.create(
        firm=firm, title="Campus Insight Forum", location="New York, NY",
        bucket="event", status="open", deadline=deadline,
        url="https://bankcampuscareers.tal.net/pl/2/opp/14594")
    UserOpportunity.all_objects.create(user=user, opportunity=opp_a, applied_status=status_a)
    UserOpportunity.all_objects.create(user=user, opportunity=opp_b, applied_status=status_b)
    return opp_a, opp_b


def test_a_tracked_identity_duplicate_shows_once_on_the_grid(logged_in, client):
    """Counted through the context, not the rendered HTML: the template
    includes each day's events twice (the grid cell and the narrow-screen
    agenda), so a raw text count would double even a correctly-folded
    single event."""
    _duplicate_pair(logged_in, day=15)
    resp = client.get("/app/calendar/")
    tracked = [e for week in resp.context["weeks"] for cell in week
               for e in cell["events"] if e["source"] == "tracked"]
    assert len(tracked) == 1
    assert tracked[0]["title"] == "Bank of America · Campus Insight Forum"


def test_the_month_rail_count_does_not_double_count_the_duplicate(logged_in, client):
    """Layer 4's rail count used a raw .annotate()/Count("id") over
    _tracked_deadlines(), which has no idea two rows are one posting."""
    opp_a, _ = _duplicate_pair(logged_in, day=15)
    # A second, genuinely distinct tracked deadline in the same month, so a
    # fold that swallowed everything (the UserOpportunity trap) would also
    # be caught by this count, not just an unfolded duplicate.
    _tracked_role(logged_in, day=16, title="Distinct Analyst Role")

    deadline = opp_a.deadline
    resp = _grid(client, deadline)
    # By the deadline's own month rather than `is_now`: near a month end the
    # two fixtures above land in NEXT month, and the count under test is the
    # one on the month they are actually in. See `_grid`.
    month = next(m for m in resp.context["rail"]
                 if (m["y"], m["m"]) == (deadline.year, deadline.month))
    assert month["count"] == 2


def test_the_ics_feed_carries_the_duplicate_once(client, logged_in):
    """Counted by VEVENT, not by substring: each event's two VALARMs repeat
    the summary text in their own DESCRIPTION, so a bare substring count
    would read 3 even for one correctly-folded event."""
    _duplicate_pair(logged_in, day=16)
    body = client.get(
        reverse("crm:calendar_ics", args=[logged_in.calendar_token])
    ).content.decode()
    assert body.count("SUMMARY:Bank of America · Campus Insight Forum closes") == 1


def test_the_progressed_duplicate_wins_on_the_calendar(logged_in, client):
    """Applied on one address, only saved on the other: the applied copy is
    what the student actually acted on, so it is the one that should
    survive the fold rather than an arbitrary tie-break."""
    opp_a, opp_b = _duplicate_pair(logged_in, day=15, status_a="saved", status_b="submitted")
    resp = client.get("/app/calendar/")
    events = [e for day in resp.context["weeks"] for cell in day
              for e in cell["events"] if e["source"] == "tracked"]
    assert len(events) == 1
    assert events[0]["stage"] == "submitted"


# ---------------------------------------------------------------------------
# An opening is not a deadline
#
# Live on August 2026: the legend read "1 chat · 0 events · 6 deadlines" while
# one of the six rows was "Goldman Sachs · applications open" on the 15th. The
# layer-3 loop stamped kind="deadline" on every FirmDate regardless of its
# `event_kind`, and the tally counts by kind — so the row's own text and the
# count above it disagreed, and the opening wore the deadline-coloured border
# to match.
# ---------------------------------------------------------------------------
def _firm_date(event_kind, day=15, *, slug="gs", name="Goldman Sachs"):
    firm = Firm.objects.get_or_create(slug=slug, defaults={"name": name})[0]
    today = timezone.localdate()
    return FirmDate.objects.create(
        firm=firm, cycle="SA 2028", region="us", event_kind=event_kind,
        date=today.replace(day=day), confidence=1.0)


def _month(client):
    today = timezone.localdate()
    return client.get(reverse("crm:calendar"),
                      {"y": today.year, "m": today.month})


def test_an_applications_open_row_is_not_counted_as_a_deadline(client, logged_in):
    """The live August 2026 row, exactly."""
    _firm_date("app_open")
    resp = _month(client)
    assert resp.context["counts"]["deadline"] == 0
    assert resp.context["counts"]["opening"] == 1
    body = resp.content.decode()
    assert "<b>0</b> Deadlines" in body
    assert "<b>1</b> Opening" in body and "<b>1</b> Openings" not in body


def test_an_insight_open_row_is_covered_by_the_same_rule(client, logged_in):
    """`insight_open` would have miscounted the same way in any month one
    fell in. Keyed off event_kind, so it never had to be found first."""
    _firm_date("insight_open")
    assert _month(client).context["counts"] == {
        "chat": 0, "event": 0, "deadline": 0, "opening": 1}


@pytest.mark.parametrize("event_kind", ["app_close", "insight_deadline"])
def test_the_dates_you_can_actually_miss_are_still_deadlines(client, logged_in, event_kind):
    _firm_date(event_kind)
    counts = _month(client).context["counts"]
    assert counts["deadline"] == 1 and counts["opening"] == 0


def test_a_mixed_month_splits_the_tally_instead_of_lumping_it(client, logged_in):
    """August 2026's real shape: closes, an insight deadline, and one
    opening. The honest reading is 3 deadlines and 1 opening, not 4."""
    _firm_date("app_close", day=1, slug="mlt", name="MLT")
    _firm_date("app_close", day=2, slug="apollo", name="Apollo")
    _firm_date("insight_deadline", day=6, slug="ms", name="Morgan Stanley")
    _firm_date("app_open", day=15)
    counts = _month(client).context["counts"]
    assert counts["deadline"] == 3
    assert counts["opening"] == 1


def test_the_opening_no_longer_wears_the_deadline_colour(client, logged_in):
    """The row carried .cal-ev-deadline, so it was red-barred as well as
    miscounted. Matched as a class attribute — the page inlines its own
    stylesheet, which names both classes in rules."""
    _firm_date("app_open")
    body = _month(client).content.decode()
    assert re.search(r'class="cal-ev cal-ev-opening[^"]*"[^>]*'
                     r'title="Goldman Sachs · Applications open"', body)
    assert not re.search(r'class="cal-ev cal-ev-deadline[^"]*"[^>]*'
                         r'title="Goldman Sachs · Applications open"', body)


def test_the_page_head_lists_what_the_page_now_counts(client, logged_in):
    assert "Chats, events, deadlines, openings" in _month(client).content.decode()


def test_an_opening_still_reaches_the_subscribed_feed_without_an_alarm(client, logged_in):
    """The ICS export alarms only on dates you can miss. Reclassifying must
    not drop the opening from the feed, nor start alarming on it."""
    _firm_date("app_open")
    body = client.get(
        reverse("crm:calendar_ics", args=[logged_in.calendar_token])
    ).content.decode()
    assert "SUMMARY:Goldman Sachs · Applications open" in body
    assert "BEGIN:VALARM" not in body


# ---------------------------------------------------------------------------
# A posting the FIRM took down (Opportunity.status), which is a different fact
# from the student's own "Done" stage tested above.
#
# The incident: layer 4 read neither the grid nor the .ics against
# `Opportunity.status`, so a dead posting kept its "closes this day" row and,
# worse, kept firing VALARM reminders at -P7D and -P1D inside the student's own
# calendar app for a role that no longer existed.
# ---------------------------------------------------------------------------

def test_a_closed_posting_raises_no_alarm_in_the_subscribed_feed(client, user):
    """The harm being fixed. A VALARM is a phone waking someone up to act, and
    a pulled posting leaves nothing to act on."""
    _tracked_role(user, day=16, title="Dead Role", posting_status="closed",
                  url="https://gs.com/dead")
    body = client.get(f"/app/calendar/feed/{user.calendar_token}.ics").content.decode()

    assert "BEGIN:VALARM" not in body
    assert "TRIGGER;RELATED=START:-P7D" not in body


def test_a_closed_posting_keeps_its_event_rather_than_vanishing(client, user):
    """The deliberate choice against dropping the VEVENT. This feed is
    SUBSCRIBED, so the event is already in the student's own calendar app;
    omitting it would delete it from their week at the next refresh, silently.
    It stays on its day and says what happened instead."""
    _tracked_role(user, day=16, title="Dead Role", posting_status="closed",
                  url="https://gs.com/dead")
    body = client.get(f"/app/calendar/feed/{user.calendar_token}.ics").content.decode()

    assert body.count("BEGIN:VEVENT") == 1
    assert "Goldman Sachs · Dead Role" in body
    assert "https://gs.com/dead" in body


def test_a_closed_postings_summary_leads_with_closed_not_a_tensed_verb(client, user):
    """The marker rides at the FRONT of the SUMMARY because a lock-screen
    notification shows the summary and nothing else, and "closes" against
    "closed" is one character of difference in that position."""
    _tracked_role(user, day=16, title="Dead Role", posting_status="closed")
    body = client.get(f"/app/calendar/feed/{user.calendar_token}.ics").content.decode()

    assert "SUMMARY:Closed: Goldman Sachs · Dead Role" in body
    assert "Dead Role closes" not in body


def test_a_closed_posting_stops_claiming_to_close_on_the_grid(client, logged_in):
    """The in-app month grid told the same lie in its own tense."""
    _tracked_role(logged_in, day=15, title="Dead Role", posting_status="closed")
    body = client.get("/app/calendar/").content.decode()

    assert "Dead Role" in body, "the student's own tracked row is never dropped"
    assert "Closed: Goldman Sachs · Dead Role" in body
    assert "Dead Role closes this day" not in body


def test_an_open_posting_beside_a_closed_one_keeps_its_alarms(client, user):
    """The over-reach guard: one dead row must not strip the alarms off the
    live rows sharing the feed."""
    _tracked_role(user, day=16, title="Dead Role", posting_status="closed",
                  url="https://gs.com/dead")
    _tracked_role(user, day=18, title="Live Role", url="https://gs.com/live")
    body = client.get(f"/app/calendar/feed/{user.calendar_token}.ics").content.decode()

    assert "SUMMARY:Goldman Sachs · Live Role closes" in body
    assert "TRIGGER;RELATED=START:-P7D" in body
    assert body.count("BEGIN:VALARM") == 2, "two alarms, both from the live row"


def test_a_posting_the_scraper_never_rechecked_still_alarms(client, user):
    """The over-filtering guard. `Opportunity.status` defaults to "" and most
    rows have never been reverified, so the rule is `== "closed"` rather than
    `!= "open"` — otherwise the feed would go silent for nearly everyone."""
    _tracked_role(user, day=16, title="Unchecked Role", posting_status="")
    body = client.get(f"/app/calendar/feed/{user.calendar_token}.ics").content.decode()

    assert "Unchecked Role closes" in body
    assert "TRIGGER;RELATED=START:-P7D" in body
