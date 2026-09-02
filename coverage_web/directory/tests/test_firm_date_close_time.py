"""D-19: the hour a deadline closes, and the zone it was stated in.

`FirmDate.date` is a bare day. Every renderer therefore read a deadline as
lasting until midnight in the READER's zone, and real closes are instants:
Citi's Hong Kong SA 2027 deadline is "Friday, October 30, 2026 at 23:59 HKT",
which for a Los Angeles student is 08:59 that morning. The row sat on the
deadlines rail, alarm and all, for the rest of that Californian day.

The decision was narrowed twice, and both narrowings are what these tests
mostly pin: the pair is populated ONLY for `confirmed_official` rows whose
own source states a time, and a time is NEVER derived. 25 of the 41 live rows
are estimates, and an hour on an estimate is not a better date, it is an
invented one.
"""

from __future__ import annotations

from datetime import date, time

import pytest
from django.db import IntegrityError, transaction

from directory.models import Firm, FirmDate

pytestmark = pytest.mark.django_db

HK = "Asia/Hong_Kong"
LA = "America/Los_Angeles"


@pytest.fixture
def firm():
    return Firm.objects.create(slug="citi", name="Citi")


def _row(firm, **kw):
    base = dict(firm=firm, cycle="sa2027", track="", region="hk",
                event_kind="app_close", date=date(2026, 10, 30),
                precision="", confidence=1.0)
    base.update(kw)
    return FirmDate.objects.create(**base)


# ---------------------------------------------------------------------------
# The constraints. Both are on the column rather than in a validator for the
# reason every other constraint on this model is: `full_clean()` runs on none
# of the writers here.
# ---------------------------------------------------------------------------

def test_a_time_without_a_zone_is_rejected(firm):
    """"23:59" alone is not a fact, it is a number fifteen hours wide — and
    reading a deadline in the wrong zone is the whole bug being fixed."""
    with pytest.raises(IntegrityError), transaction.atomic():
        _row(firm, close_time=time(23, 59), close_tz="")


def test_a_zone_without_a_time_is_rejected(firm):
    """The same emptiness wearing a label."""
    with pytest.raises(IntegrityError), transaction.atomic():
        _row(firm, close_time=None, close_tz=HK)


def test_neither_is_the_normal_row_and_is_allowed(firm):
    """Most rows will never carry either, and that has to stay cheap."""
    fd = _row(firm)

    assert fd.close_time is None and fd.close_tz == ""


def test_a_time_may_not_sit_on_a_month_precision_row(firm):
    """"Applications will open in the fall 2026" is a legitimate confirmed
    date and an illegitimate place to hang an hour: combining the two would
    mint an instant on the first of the month that nobody stated."""
    with pytest.raises(IntegrityError), transaction.atomic():
        _row(firm, precision="month", close_time=time(23, 59), close_tz=HK)


def test_a_time_may_not_sit_on_an_estimate(firm):
    """25 of the 41 live rows are estimates. This is the one the decision was
    narrowed for."""
    with pytest.raises(IntegrityError), transaction.atomic():
        _row(firm, precision="estimated", confidence=0.6,
             close_time=time(23, 59), close_tz=HK)


def test_a_time_may_not_sit_on_a_row_with_no_day(firm):
    """A row whose day is "to be confirmed" has no instant to combine with."""
    with pytest.raises(IntegrityError), transaction.atomic():
        _row(firm, date=None, close_time=time(23, 59), close_tz=HK)


# ---------------------------------------------------------------------------
# closes_at — the one place the day and the hour are combined.
# ---------------------------------------------------------------------------

def test_the_instant_is_the_firms_own_clock(firm):
    fd = _row(firm, close_time=time(23, 59), close_tz=HK)
    instant = fd.closes_at()

    assert instant.utcoffset().total_seconds() == 8 * 3600
    assert f"{instant:%Y-%m-%d %H:%M %Z}" == "2026-10-30 23:59 HKT"


def test_the_worked_example_a_hong_kong_close_is_a_los_angeles_morning(firm):
    """The measurement behind the decision: 23:59 HKT on the 30th is 08:59 on
    the 30th in Los Angeles, so a rail that drops the row at local midnight
    carries it for fifteen hours after the door shut."""
    from zoneinfo import ZoneInfo

    local = _row(firm, close_time=time(23, 59), close_tz=HK).closes_at().astimezone(ZoneInfo(LA))

    assert f"{local:%Y-%m-%d %H:%M}" == "2026-10-30 08:59"


def test_a_row_with_no_stated_hour_has_no_instant(firm):
    """None, not midnight. Midnight is a time no firm stated, and every
    reader falls back to today's local-midnight behaviour on None (P3)."""
    assert _row(firm).closes_at() is None


def test_an_unresolvable_zone_degrades_to_no_instant(firm):
    """A renderer is the wrong place to discover a missing zone key, so this
    lands where a row with no time lands rather than raising."""
    fd = _row(firm, close_time=time(23, 59), close_tz=HK)
    FirmDate.objects.filter(pk=fd.pk).update(close_tz="Mars/Olympus_Mons")
    fd.refresh_from_db()

    assert fd.closes_at() is None
    assert fd.close_time_label(LA) == ""


# ---------------------------------------------------------------------------
# close_time_label — "23:59 HKT, 08:59 your time".
# ---------------------------------------------------------------------------

def test_the_label_states_the_firms_hour_and_the_readers(firm):
    fd = _row(firm, close_time=time(23, 59), close_tz=HK)

    assert fd.close_time_label(LA) == "23:59 HKT, 08:59 your time"


def test_the_readers_half_is_dropped_when_the_zones_agree(firm):
    """A student in Hong Kong reading a Hong Kong deadline should just see the
    deadline. "23:59 HKT, 23:59 your time" is noise."""
    fd = _row(firm, close_time=time(23, 59), close_tz=HK)

    assert fd.close_time_label(HK) == "23:59 HKT"


def test_an_unknown_reader_zone_still_gets_the_firms_own_words(firm):
    """The firm's stated hour is a fact whether or not we know where the
    reader is. Only the second half needs both."""
    fd = _row(firm, close_time=time(23, 59), close_tz=HK)

    assert fd.close_time_label("") == "23:59 HKT"


def test_the_abbreviation_follows_the_date_not_a_stored_label(firm):
    """The reason the column holds an IANA key and not "HKT": Los Angeles is
    PDT in October and PST in November, and nobody should have to maintain a
    table of which is which."""
    october = _row(firm, close_time=time(9, 0), close_tz=LA,
                   date=date(2026, 10, 30), region="us")
    november = _row(firm, close_time=time(9, 0), close_tz=LA,
                    date=date(2026, 11, 30), region="us", event_kind="app_open")

    assert october.close_time_label() == "09:00 PDT"
    assert november.close_time_label() == "09:00 PST"


def test_a_row_with_no_stated_hour_renders_nothing(firm):
    """Blank, not a hedge. Every template branches on truthiness and prints
    nothing, which is what "we do not know" should look like."""
    assert _row(firm).close_time_label(LA) == ""
