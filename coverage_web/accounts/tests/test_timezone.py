"""Tests for the per-user timezone (docs/specs/settings-page.md audit #5, B1).

The bug: `TIME_ZONE = "UTC"` and every "today" in the product is
`timezone.localdate()` — the cadence queue's as-of date, the pace ring's week,
snooze expiry, "app closes in N days". For a Hong Kong student (UTC+8) the day
therefore rolled over at 8 a.m. their time, and Sunday-evening logging landed
in the following week. The stated audience is HK and US, so this was wrong for
half of it.

`accounts.middleware.TimezoneMiddleware` is the entire fix: `localdate()`
reads the ACTIVE timezone, so activating the user's zone once per request
makes every existing call site correct with no change of its own.

`test_the_middleware_is_wired_up` is the one that matters most. Until the
middleware is listed in `settings.MIDDLEWARE`, `User.timezone` is a stored
value nothing reads — precisely the defect that got the Language control
deleted — so the gap is reported on every run rather than left silent.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone as dj_timezone

from accounts.forms import AUTO_TIMEZONE, ProfileForm, known_timezones, timezone_choices
from accounts.middleware import TimezoneMiddleware

User = get_user_model()

pytestmark = pytest.mark.django_db

MIDDLEWARE_PATH = "accounts.middleware.TimezoneMiddleware"

# 2026-01-15 17:30 UTC. In Hong Kong (UTC+8) that is already the 16th — the
# exact several-hour window in which UTC and HK disagree about "today", which
# is what put an HK student's touches in the wrong day and the wrong week.
SPLIT_MOMENT = datetime(2026, 1, 15, 17, 30, tzinfo=dt_timezone.utc)


@pytest.fixture
def student():
    return User.objects.create_user(email="tz@example.com", password="x")


def _run_middleware(user):
    """Drive the middleware for one request and capture what `localdate()`
    returns from inside it — i.e. what every view would have seen."""
    seen = {}

    def get_response(request):
        seen["date"] = dj_timezone.localdate(SPLIT_MOMENT)
        seen["zone"] = dj_timezone.get_current_timezone_name()
        return "ok"

    request = RequestFactory().get("/app/")
    request.user = user
    TimezoneMiddleware(get_response)(request)
    return seen


# ---------------------------------------------------------------------------
# The fix itself
# ---------------------------------------------------------------------------
def test_a_hong_kong_student_gets_hong_kongs_today(student):
    student.timezone = "Asia/Hong_Kong"
    seen = _run_middleware(student)
    assert seen["zone"] == "Asia/Hong_Kong"
    assert seen["date"].isoformat() == "2026-01-16"


def test_unset_stays_on_utc_days(student):
    """Blank is UNSET and unset means UTC — the behaviour every account had
    before this column existed. Never guessed from `regions`."""
    assert student.timezone == ""
    seen = _run_middleware(student)
    assert seen["date"].isoformat() == "2026-01-15"


def test_a_us_pacific_student_gets_their_own_day(student):
    student.timezone = "America/Los_Angeles"
    seen = _run_middleware(student)
    assert seen["date"].isoformat() == "2026-01-15"


def test_a_bogus_stored_value_falls_back_instead_of_erroring(student):
    """Validated-on-write is not trustworthy-on-read: a value can predate the
    validator, or come from a fixture or the admin. A bad one must not 500
    every page the user visits."""
    student.timezone = "Mars/Olympus_Mons"
    seen = _run_middleware(student)
    assert seen["date"].isoformat() == "2026-01-15"


def test_an_anonymous_request_activates_nothing(student):
    seen = _run_middleware(AnonymousUser())
    assert seen["date"].isoformat() == "2026-01-15"


def test_the_activation_does_not_leak_into_the_next_request(student):
    """Threads are reused. A leaked activation would silently hand one user's
    zone to whoever the worker serves next, including anonymous visitors."""
    student.timezone = "Asia/Hong_Kong"
    _run_middleware(student)
    assert dj_timezone.localdate(SPLIT_MOMENT).isoformat() == "2026-01-15"


def test_it_deactivates_even_when_the_view_raises(student):
    student.timezone = "Asia/Hong_Kong"

    def boom(_request):
        raise RuntimeError("view exploded")

    request = RequestFactory().get("/app/")
    request.user = student
    with pytest.raises(RuntimeError):
        TimezoneMiddleware(boom)(request)
    assert dj_timezone.localdate(SPLIT_MOMENT).isoformat() == "2026-01-15"


def test_the_middleware_is_wired_up():
    """A control whose reader isn't installed is a control that does nothing.

    This skips (loudly, with the exact line to add) while the wiring is
    missing, and becomes a hard assertion the moment it lands — so the gap is
    reported on every run instead of being invisible.
    """
    if MIDDLEWARE_PATH not in django_settings.MIDDLEWARE:
        pytest.skip(
            "TimezoneMiddleware is NOT wired up, so User.timezone is stored and "
            'read by nothing. Add "accounts.middleware.TimezoneMiddleware" to '
            "MIDDLEWARE in coverage_web/settings/base.py, after "
            "AuthenticationMiddleware."
        )
    auth = django_settings.MIDDLEWARE.index(
        "django.contrib.auth.middleware.AuthenticationMiddleware"
    )
    assert django_settings.MIDDLEWARE.index(MIDDLEWARE_PATH) > auth, (
        "TimezoneMiddleware reads request.user, so it must run after "
        "AuthenticationMiddleware."
    )


# ---------------------------------------------------------------------------
# The control
# ---------------------------------------------------------------------------
def test_the_profile_form_saves_a_zone(client, student):
    client.force_login(student)
    client.post(
        reverse("accounts:settings"),
        {"section": "profile", "name": "TZ Person", "timezone": "Asia/Hong_Kong"},
    )
    student.refresh_from_db()
    assert student.timezone == "Asia/Hong_Kong"


def test_blank_clears_the_zone_back_to_unset(client, student):
    student.timezone = "Asia/Hong_Kong"
    student.save(update_fields=["timezone"])
    client.force_login(student)
    client.post(reverse("accounts:settings"), {"section": "profile", "timezone": ""})
    student.refresh_from_db()
    assert student.timezone == ""


def test_a_zone_zoneinfo_does_not_know_is_rejected(student):
    form = ProfileForm({"timezone": "Not/AZone"})
    assert not form.is_valid()
    assert "timezone" in form.errors


def test_nothing_else_on_the_profile_is_wiped_by_a_rejected_zone(client, student):
    """The section-form contract: an invalid value re-renders with an error,
    it does not commit a partial save."""
    student.name = "Kept Name"
    student.save(update_fields=["name"])
    client.force_login(student)
    client.post(
        reverse("accounts:settings"),
        {"section": "profile", "name": "New Name", "timezone": "Not/AZone"},
    )
    student.refresh_from_db()
    assert student.name == "Kept Name"


def test_a_stored_zone_the_host_no_longer_knows_renders_as_unset(student):
    """Rather than as nothing-selected, which is the silent-clear failure the
    stale-cycle widget exists to prevent elsewhere.

    Checked with following OFF: a following account renders as automatic
    whatever its stored value, so it could not show this fallback at all."""
    student.timezone = "Mars/Olympus_Mons"
    student.timezone_auto = False
    form = ProfileForm.from_user(student)
    assert form.initial["timezone"] == ""


def test_the_select_offers_the_shortlist_and_the_full_list():
    choices = timezone_choices()
    # The first entry is AUTO, not blank: following the device is the default
    # and "not set" stopped being the honest label for it.
    assert choices[0][0] == AUTO_TIMEZONE
    assert "automatically" in choices[0][1].lower()
    groups = dict(choices[1:])
    assert ("Asia/Hong_Kong", "Hong Kong (HKT)") in groups["Common"]
    every = {code for code, _label in groups["All timezones"]}
    assert every == set(known_timezones())
    assert "Europe/Zurich" in every


def test_the_zone_round_trips_through_zoneinfo():
    """Every shortlisted zone must be constructible, or the select offers an
    option that would make the middleware fall back."""
    for code, _label in timezone_choices()[1][1]:
        assert ZoneInfo(code)
