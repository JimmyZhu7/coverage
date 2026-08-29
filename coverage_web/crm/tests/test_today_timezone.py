"""The queue is scored against the STUDENT'S calendar day, not UTC's.

`settings.TIME_ZONE` is UTC; `accounts.middleware` activates the account's own
zone per request, and the founder's is Asia/Hong_Kong (+8). So for eight hours
out of every twenty-four — HK midnight to HK 8am, which is precisely when a
student on that clock opens the page — `timezone.now().date()` is still
YESTERDAY in Hong Kong.

`_build_actions` handed that raw UTC instant to `cadence.due_actions` as
`as_of`, and the engine does `today = as_of.date()` on it. Every business-day
threshold in the queue therefore ran a day behind during that window: the
follow-up clock, the park-after clock, and — the one these tests pin — the
`closes_on` filter that decides what counts as CRITICAL.

The fix is `timezone.localtime(...)`: the same instant, expressed in the
account's zone, so `.date()` yields the day the student is actually living in.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from crm.models import Contact, Touch, UserFirm
from crm.today import _build_actions
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db(transaction=True)

HK = ZoneInfo("Asia/Hong_Kong")

# 2026-08-27 20:00 UTC is 2026-08-28 04:00 in Hong Kong. The two calendar
# dates genuinely differ, which is the whole point of the fixture — pick any
# instant in the HK 00:00-08:00 band and the same disagreement holds.
INSTANT = datetime(2026, 8, 27, 20, 0, tzinfo=ZoneInfo("UTC"))
UTC_DAY = date(2026, 8, 27)
HK_DAY = date(2026, 8, 28)


def _hk_user(email):
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", timezone="Asia/Hong_Kong",
    )


def _frozen():
    """Patch the clock at the module Django itself reads, so `timezone.now`,
    `timezone.localtime` and `timezone.localdate` all agree — a test that
    freezes only one of them proves nothing about the other two."""
    return mock.patch("django.utils.timezone.now", return_value=INSTANT)


def test_the_fixture_actually_straddles_a_date_boundary():
    """Guard the guard. If this instant ever stops disagreeing between the two
    zones, every test below would pass while testing nothing."""
    assert INSTANT.astimezone(ZoneInfo("UTC")).date() == UTC_DAY
    assert INSTANT.astimezone(HK).date() == HK_DAY
    assert UTC_DAY != HK_DAY


def test_the_engine_is_asked_about_the_students_day_not_utcs():
    """The contract, stated directly: whatever instant reaches
    `cadence.due_actions`, `as_of.date()` must be the day the student is in.
    Everything the engine computes hangs off that one line."""
    user = _hk_user("tz-contract@example.com")
    Contact.all_objects.create(user=user, name="Anyone", school_affiliation=True)

    seen = {}
    from crm import today as today_mod

    real = today_mod.cadence.due_actions

    def spy(*args, **kw):
        seen["as_of"] = kw["as_of"]
        return real(*args, **kw)

    timezone.activate(HK)
    try:
        with _frozen(), mock.patch.object(today_mod.cadence, "due_actions", spy):
            _build_actions(user)
    finally:
        timezone.deactivate()

    assert seen["as_of"].date() == HK_DAY, (
        "the engine was handed a UTC calendar date; every business-day "
        "threshold in the queue is running a day behind"
    )
    # Same moment in time, only re-expressed. A fix that shifted the instant
    # would silently move every hours-based clock in the engine too.
    assert seen["as_of"] == INSTANT


def test_a_deadline_that_passed_last_night_is_not_a_critical_card(client):
    """THE MEASURED CONSEQUENCE, and why this belongs with the severity work.

    `cadence._closing_soon` drops an application close with `d < today`. A
    close dated 2026-08-27 is yesterday in Hong Kong and must be gone. Read
    against the UTC date it is still "today", survives the filter, and fires
    a `reping` — which `_is_critical` exempts from the daily cap and from
    Snooze, so the page's loudest, most un-dismissable card was being spent
    on a deadline the student had already missed.

    FAILS BEFORE THE FIX: revert `_build_actions`'s `now` to a bare
    `timezone.now()` and this test reports a `reping`.
    """
    user = _hk_user("tz-closed@example.com")
    firm = Firm.objects.create(name="Closed Bank", slug="closed-bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="us", event_kind="app_close",
        date=UTC_DAY, confidence=1.0,
    )
    contact = Contact.all_objects.create(
        user=user, name="Closed Person", firm=firm, warmth="replied",
        region="us", school_affiliation=True,
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="email", channel="email",
        ts=INSTANT - timedelta(days=40),
    )

    timezone.activate(HK)
    try:
        with _frozen():
            actions, _ = _build_actions(user)
    finally:
        timezone.deactivate()

    kinds = [a["action"] for a in actions if a["contact"]["id"] == contact.id]
    assert "reping" not in kinds, (
        f"a deadline that closed yesterday in the student's own timezone "
        f"still produced a critical re-ping: {kinds}"
    )


def test_a_deadline_still_open_today_keeps_its_critical_card(client):
    """The other half, so the fix is not just "drop more things". A close
    dated today in Hong Kong is live and must still raise its re-ping —
    otherwise this change would trade a false critical for a missed one,
    which is the strictly worse error."""
    user = _hk_user("tz-open@example.com")
    firm = Firm.objects.create(name="Open Bank", slug="open-bank")
    UserFirm.all_objects.create(user=user, firm=firm, tier=1)
    FirmDate.objects.create(
        firm=firm, cycle="sa2028", region="us", event_kind="app_close",
        date=HK_DAY, confidence=1.0,
    )
    contact = Contact.all_objects.create(
        user=user, name="Open Person", firm=firm, warmth="replied",
        region="us", school_affiliation=True,
    )
    Touch.all_objects.create(
        user=user, contact=contact, kind="email", channel="email",
        ts=INSTANT - timedelta(days=40),
    )

    timezone.activate(HK)
    try:
        with _frozen():
            actions, _ = _build_actions(user)
    finally:
        timezone.deactivate()

    kinds = [a["action"] for a in actions if a["contact"]["id"] == contact.id]
    assert "reping" in kinds, (
        f"a deadline still open today in the student's timezone lost its "
        f"critical card: {kinds}"
    )
