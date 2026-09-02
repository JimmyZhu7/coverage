"""The countdown flips at the stated instant, and at local midnight otherwise.

D-19's measurement: HSBC's Hong Kong close is 30 October, Citi's is
"Friday, October 30, 2026 at 23:59 HKT". For a Los Angeles student that is
08:59 on the 30th, so a rail keyed on `date >= today` carried the row —
urgent dot, alarm and all — for the next fifteen hours.

The fix is deliberately narrow, and the narrowness is what most of this file
pins: a row that states no hour behaves exactly as it did before (P3). That
is nearly every row, because a time is never derived from a bare date.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from crm.today import _next_deadlines
from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db

User = get_user_model()
HK = "Asia/Hong_Kong"
LA = "America/Los_Angeles"


@pytest.fixture
def user():
    return User.objects.create_user(email="student@example.com",
                                    password="pw12345!", regions=["us"],
                                    timezone=LA)


def _date(firm, *, on, kind="app_close", close_time=None, close_tz="",
          confidence=1.0, precision="day", cycle="sa2028"):
    return FirmDate.objects.create(
        firm=firm, cycle=cycle, event_kind=kind, date=on,
        confidence=confidence, precision=precision,
        close_time=close_time, close_tz=close_tz,
    )


def _hk_close_today_at_2359():
    """The row and the moment: a Hong Kong close at 23:59 HKT, seen from Los
    Angeles at 10:00 local — an hour after the door shut, on the same
    calendar day in both zones."""
    firm = Firm.objects.create(slug="citi", name="Citi")
    day = dt.date(2026, 10, 30)
    fd = _date(firm, on=day, close_time=dt.time(23, 59), close_tz=HK)
    now = dt.datetime(2026, 10, 30, 10, 0, tzinfo=ZoneInfo(LA))
    return firm, fd, day, now


def test_the_row_leaves_the_rail_once_the_stated_hour_has_passed(user):
    """The bug, measured. Same calendar day in Los Angeles, and the deadline
    is an hour gone."""
    _firm, _fd, day, now = _hk_close_today_at_2359()

    with timezone.override(ZoneInfo(LA)):
        rows = _dead(user, day, now)

    assert rows == []


def test_the_row_is_still_there_before_the_stated_hour(user):
    """08:00 in Los Angeles is 23:00 in Hong Kong. Still open, still on the
    rail, still counting down — the fix must not shorten a live deadline."""
    _firm, _fd, day, _now = _hk_close_today_at_2359()
    now = dt.datetime(2026, 10, 30, 7, 0, tzinfo=ZoneInfo(LA))

    with timezone.override(ZoneInfo(LA)):
        rows = _dead(user, day, now)

    assert [r["when"] for r in rows] == ["today"]


def test_a_row_with_no_stated_hour_survives_the_whole_day(user):
    """P3, and the case that covers nearly every row on the table: HSBC's own
    posting says "Closing Date: Fri Oct 30, 2026" and no time, so its
    countdown keeps flipping at local midnight exactly as it did before."""
    firm = Firm.objects.create(slug="hsbc", name="HSBC")
    day = dt.date(2026, 10, 30)
    _date(firm, on=day)
    now = dt.datetime(2026, 10, 30, 23, 30, tzinfo=ZoneInfo(LA))

    with timezone.override(ZoneInfo(LA)):
        rows = _dead(user, day, now)

    assert [r["when"] for r in rows] == ["today"]
    assert rows[0]["close_time_label"] == ""


def test_the_rail_does_not_shorten_when_a_row_drops(user):
    """Dropped AFTER the slice would quietly leave a four-row rail showing
    three. The next date takes the empty place."""
    day = dt.date(2026, 10, 30)
    gone = Firm.objects.create(slug="citi", name="Citi")
    _date(gone, on=day, close_time=dt.time(23, 59), close_tz=HK)
    for i in range(5):
        f = Firm.objects.create(slug=f"f{i}", name=f"Firm {i}")
        _date(f, on=day + dt.timedelta(days=i + 1))
    now = dt.datetime(2026, 10, 30, 10, 0, tzinfo=ZoneInfo(LA))

    with timezone.override(ZoneInfo(LA)):
        rows = _dead(user, day, now)

    assert len(rows) == 4
    assert "Citi" not in [r["firm"].name for r in rows]


def test_the_rail_carries_the_label_where_a_firm_stated_one(user):
    """"23:59 HKT, 08:59 your time" — the firm's own words first, the
    reader's clock second."""
    _firm, _fd, day, _now = _hk_close_today_at_2359()
    now = dt.datetime(2026, 10, 30, 7, 0, tzinfo=ZoneInfo(LA))

    with timezone.override(ZoneInfo(LA)):
        rows = _dead(user, day, now)

    assert rows[0]["close_time_label"] == "23:59 HKT, 08:59 your time"


def test_a_hong_kong_reader_is_not_told_their_own_time_twice(user):
    """The second half is a translation. There is nothing to translate when
    the student is already on the firm's clock."""
    _firm, _fd, day, _now = _hk_close_today_at_2359()
    now = dt.datetime(2026, 10, 30, 20, 0, tzinfo=ZoneInfo(HK))

    with timezone.override(ZoneInfo(HK)):
        rows = _dead(user, day, now)

    assert rows[0]["close_time_label"] == "23:59 HKT"


def _dead(user, today, now):
    """`_next_deadlines` at a fixed instant. The clock is the only thing
    frozen — everything else is the real reader."""
    import crm.today as today_mod

    real = today_mod.timezone.now
    today_mod.timezone.now = lambda: now
    try:
        return _next_deadlines(user, today)
    finally:
        today_mod.timezone.now = real
