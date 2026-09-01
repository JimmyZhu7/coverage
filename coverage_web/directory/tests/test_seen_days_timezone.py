"""Cross-app consistency for "how many days ago was this first seen" — the
big finding of the 2026-09-01 cross-surface consistency audit (finding A).

Three surfaces used to answer this independently:

  * `directory.views._urgency_item`'s `seen_days` — the feed's "first seen
    Nd ago" text, the "Fresh" pill, and the elapsed-openness bar — computed
    `(now - o.first_seen).days`, a raw UTC timedelta floor.
  * `directory.open_runs.open_run_days` — the feed's "Open Nd" chip and
    Today's "N open, longest Nd" line — computed
    `(today - opp.first_seen.date()).days`, `today` being the account's
    LOCAL date compared against `first_seen`'s raw UTC `.date()`.
  * `crm.utils._calendar_days_ago` — the product's declared single source of
    truth: `local_date(as_of).date() - local_date(ts).date()`, entirely on
    the account's own clock.

Measured on the founder's live board 2026-08-31: of the 2,339 undated open
campus rows that print "first seen Nd ago", the first two disagreed on 2,202
of them (94%) — all three agreed on only 138 of the full 2,662 open campus
rows (5%). Both non-canonical implementations now route through
`crm.utils.local_date` / `crm.utils._calendar_days_ago`, so this module pins
that they agree with each other and with the calendar-date answer, on an
account whose local day differs from UTC's.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from directory.models import Firm, Opportunity
from directory.open_runs import onboarding_cutoffs, open_run_days
from directory.views import _FRESH_DAYS, _urgency_item

pytestmark = pytest.mark.django_db

HK = ZoneInfo("Asia/Hong_Kong")

# 2026-08-27 20:00 UTC is 2026-08-28 04:00 in Hong Kong — an account zone
# east of UTC (`accounts.middleware.TimezoneMiddleware` activates a user's
# own zone per request; see `crm.tests.test_today_timezone` for the
# identical fixture shape applied to the cadence engine).
NOW = datetime(2026, 8, 27, 20, 0, tzinfo=ZoneInfo("UTC"))
TODAY = date(2026, 8, 28)  # NOW's LOCAL date in Hong Kong

# 2026-08-17 15:00 UTC is 2026-08-17 23:00 in Hong Kong — 5 hours before HK
# midnight. From NOW, that is 10 days 5 hours of raw elapsed time: a UTC
# timedelta floors it to 10, exactly `_FRESH_DAYS`, so the OLD `seen_days`
# read this row as fresh. The LOCAL calendar-date difference (Aug 28 minus
# Aug 17) is 11 — one day past the boundary, not fresh. Same instant, same
# `first_seen`, two different verdicts depending on which clock reads it.
FIRST_SEEN = datetime(2026, 8, 17, 15, 0, tzinfo=ZoneInfo("UTC"))


def _firm(slug="gs", name="Goldman Sachs"):
    return Firm.objects.create(slug=slug, name=name)


def _opp(firm, *, url, first_seen=FIRST_SEEN):
    o = Opportunity.objects.create(
        firm=firm, title="Summer Analyst", bucket="internship",
        status="open", deadline=None, url=url,
    )
    # `.update()` bypasses `auto_now_add`, the same backdating trick
    # `directory.tests.test_open_runs._opp` uses.
    Opportunity.objects.filter(pk=o.pk).update(first_seen=first_seen)
    o.refresh_from_db()
    return o


def test_seen_days_is_the_local_calendar_answer_not_a_utc_floor():
    timezone.activate(HK)
    try:
        item = _urgency_item(
            _opp(_firm(), url="https://x/a"), now=NOW, today=TODAY, my_firm_ids=set(),
        )
    finally:
        timezone.deactivate()

    # 11, the LOCAL calendar-date answer — not 10, the raw UTC floor
    # `(now - first_seen).days` gave for this exact instant before the fix.
    assert item["seen_days"] == 11


def test_the_fresh_pill_flips_at_the_local_calendar_boundary():
    """The exact regression named in the audit: `seen_days` also feeds
    `is_fresh`, so the raw-floor bug did not just mislabel the "first seen"
    text — it could tell a student a role was Fresh a full calendar day
    after the account's own clock says otherwise."""
    timezone.activate(HK)
    try:
        item = _urgency_item(
            _opp(_firm(), url="https://x/b"), now=NOW, today=TODAY, my_firm_ids=set(),
        )
    finally:
        timezone.deactivate()

    assert item["seen_days"] == _FRESH_DAYS + 1
    assert item["is_fresh"] is False  # was True under the old UTC floor (10 <= 10)


def test_open_run_days_agrees_with_seen_days_for_the_same_row():
    """Until this fix, `seen_days` and `open_run_days` were independently
    computed and could disagree about the identical `first_seen` — see the
    module docstring's 94% figure. Both must now read 11 for this row."""
    timezone.activate(HK)
    try:
        firm = _firm()
        # A firm's OLDEST posting sets its onboarding cutoff (see
        # `onboarding_cutoffs`'s docstring); without one well before
        # `FIRST_SEEN`, `o` itself would be swallowed as the onboarding row
        # and `open_run_days` would correctly return `None` for it, making
        # this test unable to compare two real numbers.
        onboarding = _opp(
            firm, url="https://x/onboarding",
            first_seen=FIRST_SEEN - timedelta(days=90),
        )
        o = _opp(firm, url="https://x/c")

        item = _urgency_item(o, now=NOW, today=TODAY, my_firm_ids=set())
        cutoffs = onboarding_cutoffs([firm.id])
        run_days = open_run_days(o, TODAY, cutoffs)
        # The onboarding row itself must still read `None` — confirms the
        # cutoff fired at all, so `run_days` above is the real, non-onboarding
        # answer rather than a coincidental pass.
        onboarding_run_days = open_run_days(onboarding, TODAY, cutoffs)
    finally:
        timezone.deactivate()

    assert item["seen_days"] == 11
    assert run_days == 11
    assert onboarding_run_days is None
