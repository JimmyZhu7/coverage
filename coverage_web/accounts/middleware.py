"""Per-user timezone activation (docs/specs/settings-page.md audit #5, B1).

`TIME_ZONE = "UTC"` and every "today" in the product is
`django.utils.timezone.localdate()` — the cadence queue's as-of date, the pace
ring's week boundary, snooze expiry, "app closes in N days". With no per-user
zone, a Hong Kong student (UTC+8) rolled over to a new day at 8 a.m. their
time, and their Sunday-evening logging landed in the following week. The
stated audience is HK and US, so that was wrong for half of it.

This is the whole fix. `localdate()` reads the *active* timezone, so
activating the user's zone once per request makes every existing call site
correct with no change of its own, and `coverage_domain.cadence` already takes
the resulting date as an explicit parameter rather than computing one.

WIRING (REQUIRED — one line, in a file this change did not own):

    # settings/base.py, MIDDLEWARE, immediately after AuthenticationMiddleware
    # (this reads request.user):
    "accounts.middleware.TimezoneMiddleware",

Until that line lands, `User.timezone` is a stored value nothing reads — the
exact defect that got the Language control deleted. `test_timezone.py::
test_the_middleware_is_wired_up` reports the gap on every run and turns into a
real assertion the moment the line exists.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone


class TimezoneMiddleware:
    """Activate `request.user.timezone` for the duration of the request.

    Deactivating (rather than activating UTC explicitly) on the unset path is
    deliberate: it returns Django to `settings.TIME_ZONE`, so if the project
    default ever moves the unset case follows it instead of being pinned to a
    second, stale copy of the same decision.

    The stored value is validated on write (`ProfileForm.clean_timezone`), but
    validated-on-write is not the same as trustworthy-on-read: a value could
    predate the validator, arrive from a fixture or the admin, or name a zone
    the host's tzdata no longer carries. A bad one falls back to the project
    default rather than 500-ing every page the user visits.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        name = ""
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            name = (getattr(user, "timezone", "") or "").strip()
        if name:
            try:
                timezone.activate(ZoneInfo(name))
            except (ZoneInfoNotFoundError, ValueError):
                timezone.deactivate()
        else:
            timezone.deactivate()
        try:
            return self.get_response(request)
        finally:
            # Threads and worker processes are reused. Leaving an activation
            # behind would leak one user's zone into the next request served
            # on the same thread — including anonymous ones, which never
            # activate anything and so would silently inherit it.
            timezone.deactivate()
