"""The quiet-day header: what Today says when there is genuinely nothing.

THE MEASURED BUG. The founder sent ~50 personalised coffee-chat requests over
two days. Nothing was DUE — his follow-up window is business days out — so
Today rendered zero cards. Technically correct, experientially useless: "i
dont think its usable yet." Worse, on the same account the page's own
"Done for today" banner read "That's 57 of 14 this week" — a saturated pace
ring dressed as an achievement, on a day there was nothing to do.

The rules pinned here:

  1. The forecast (`_next_wave`) never disagrees with the engine. It mirrors
     `cadence.due_actions`' branch 6 using the SAME `business_days_since`
     and the user's own `followup_after_business_days` — never a parallel
     calendar guess.
  2. Counts equal what renders: a contact the relevance layer would drop
     once due (campaign-excluded, recruitment-hidden, already at the park
     threshold, or already due today) never inflates the forecast.
  3. The quiet header (`quiet` / `quiet_line`) appears only when the page is
     genuinely empty, and disappears the moment any section has content.
  4. Never a nag: the fallback line for a user with nothing to forecast is
     still one honest sentence, not a crash, not an empty string, and not a
     push to go do something.

Its own module rather than an append to `test_today.py`, matching
`test_today_seeds.py`'s precedent for the same reason: a distinct feature
with a distinct premise, landing in a file that is 2,200+ lines and
actively edited elsewhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from coverage_domain import cadence
from crm.models import Contact, Touch
from crm.today import _cockpit_context, _next_wave, _quiet_line

pytestmark = pytest.mark.django_db(transaction=True)


def _user(email="quiet@example.com", **kw):
    return get_user_model().objects.create_user(
        email=email, password="pw12345!", weekly_touch_goal=14, **kw
    )


def _touch(user, contact, kind, *, days_ago=0):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel="email",
        ts=timezone.now() - timedelta(days=days_ago),
    )


def _login_and_get(client, user) -> str:
    client.force_login(user)
    return client.get(reverse("crm:week")).content.decode()


def _wave_date(today, business_days):
    """The same walk `_next_wave` does, independently, so a test can assert
    the forecast without hardcoding a weekday that would break depending on
    which day the suite happens to run."""
    d = today
    while cadence.business_days_since(today, d) < business_days:
        d += timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# 1. The forecast agrees with the engine.
# ---------------------------------------------------------------------------
def test_forecast_matches_the_engines_own_due_date_for_a_known_wave():
    """Five contacts touched today, one outreach each, a 6-business-day
    follow-up window. `_next_wave` must name the exact date branch 6's own
    `business_days_since` reaches 6 — not a parallel weekday guess — and the
    count must be exactly the five contacts that produced it."""
    user = _user(cadence_params={"followup_after_business_days": 6})
    today = timezone.localdate()
    contacts = [Contact.all_objects.create(user=user, name=f"Wave {i}") for i in range(5)]
    for c in contacts:
        _touch(user, c, "outreach", days_ago=0)

    wave = _next_wave(user, today)
    assert wave is not None
    assert wave["date"] == _wave_date(today, 6)
    assert wave["count"] == 5

    # Run the actual engine forward to that date and confirm it agrees: the
    # forecast is a mirror of branch 6, not an independent implementation
    # that could quietly drift from it.
    contact_dicts = [
        {"id": c.id, "firm_id": None, "firm_text": "", "warmth": "cold",
         "thread_state": "no_reply", "region": None, "archived": False}
        for c in contacts
    ]
    touch_dicts = [
        {"contact_id": c.id, "ts": timezone.now(), "kind": "outreach"}
        for c in contacts
    ]
    as_of = timezone.make_aware(datetime.combine(wave["date"], datetime.min.time())) + timedelta(hours=9)
    engine_actions = cadence.due_actions(
        contact_dicts, touch_dicts, [], as_of=as_of, firms={},
        params={"followup_after_business_days": 6},
    )
    assert sum(1 for a in engine_actions if a["action"] == "follow_up") == 5

    # And the day before, the engine has not yet moved: zero due.
    day_before = as_of - timedelta(days=1)
    engine_actions_before = cadence.due_actions(
        contact_dicts, touch_dicts, [], as_of=day_before, firms={},
        params={"followup_after_business_days": 6},
    )
    assert sum(1 for a in engine_actions_before if a["action"] == "follow_up") == 0


def test_two_waves_land_on_two_different_dates_earliest_reported_first():
    """The founder's real shape: one batch sent a day earlier than another,
    so two distinct dates come due. `_next_wave` reports the earliest one
    and only that one's count — the page names the NEXT wave, not the sum
    of every future wave."""
    user = _user(cadence_params={"followup_after_business_days": 7})
    today = timezone.localdate()
    early = [Contact.all_objects.create(user=user, name=f"Early {i}") for i in range(3)]
    late = [Contact.all_objects.create(user=user, name=f"Late {i}") for i in range(2)]
    for c in early:
        _touch(user, c, "outreach", days_ago=1)
    for c in late:
        _touch(user, c, "outreach", days_ago=0)

    # Derived, not assumed: a one-calendar-day head start can still land on
    # the SAME due date if it crosses a weekend (Saturday and Sunday reach
    # the same following Monday in `business_days_since`), so the expected
    # count is computed from the dates rather than hardcoded, keeping this
    # test true regardless of which day of the week the suite runs.
    early_date = _wave_date(today - timedelta(days=1), 7)
    late_date = _wave_date(today, 7)
    expected_date = min(early_date, late_date)
    expected_count = 3 if early_date < late_date else 5

    wave = _next_wave(user, today)
    assert wave is not None
    assert wave["date"] == expected_date
    assert wave["count"] == expected_count


# ---------------------------------------------------------------------------
# 2. Counts equal what renders.
# ---------------------------------------------------------------------------
def test_a_contact_already_at_the_park_threshold_is_not_a_future_follow_up():
    """Branch 6 routes a contact at `max_cold_touches` outbound touches to
    `park`, never a second follow-up. Forecasting one for them would name a
    wave the queue will never actually produce."""
    user = _user(cadence_params={"followup_after_business_days": 6})
    today = timezone.localdate()
    c = Contact.all_objects.create(user=user, name="Maxed Out")
    _touch(user, c, "outreach", days_ago=40)
    _touch(user, c, "follow_up", days_ago=0)  # outbound == max_cold_touches (2)

    assert _next_wave(user, today) is None


def test_a_contact_already_due_today_is_not_counted_as_a_future_wave():
    """If the follow-up window has already elapsed, that contact belongs in
    TODAY's queue, not in a forecast of what's still coming."""
    user = _user(cadence_params={"followup_after_business_days": 2})
    today = timezone.localdate()
    c = Contact.all_objects.create(user=user, name="Already Due")
    _touch(user, c, "outreach", days_ago=20)

    assert _next_wave(user, today) is None


def test_a_recruitment_hidden_contact_never_inflates_the_forecast():
    """`crm.relevance.contact_relevance` drops a not-yet-replied,
    recruitment-hidden contact entirely once their follow-up comes due
    (REL_NONE) — so counting them here would forecast a wave bigger than
    the one that actually renders the day it lands."""
    user = _user(cadence_params={"followup_after_business_days": 6})
    today = timezone.localdate()
    counted = Contact.all_objects.create(user=user, name="Counted")
    hidden = Contact.all_objects.create(
        user=user, name="Hidden", recruitment_related=False,
    )
    for c in (counted, hidden):
        _touch(user, c, "outreach", days_ago=0)

    wave = _next_wave(user, today)
    assert wave is not None
    assert wave["count"] == 1


def test_a_user_with_nothing_scheduled_at_all_gets_a_sane_line():
    """No cold/no_reply contacts, no touches, nothing to forecast — the
    fallback line must still be one honest sentence, never a crash and
    never an empty string."""
    user = _user()
    today = timezone.localdate()
    assert _next_wave(user, today) is None
    line = _quiet_line(_next_wave(user, today))
    assert line
    assert "Quiet on the cadence" in line


# ---------------------------------------------------------------------------
# 3. The quiet header itself: when it renders, and when it must not.
# ---------------------------------------------------------------------------
def test_the_quiet_header_appears_when_the_page_is_genuinely_empty(client):
    """A real network, a real batch of outreach, nothing due yet — the exact
    founder scenario. One honest line, no achievement banner."""
    user = _user(cadence_params={"followup_after_business_days": 7})
    for i in range(6):
        c = Contact.all_objects.create(
            user=user, name=f"Sent {i:02d}", school_affiliation=True,
        )
        _touch(user, c, "outreach", days_ago=0)

    ctx = _cockpit_context(user)
    assert ctx["quiet"] is True
    assert ctx["quiet_line"]
    assert "Next wave" in ctx["quiet_line"]

    body = _login_and_get(client, user)
    assert ctx["quiet_line"] in body
    assert "Done for today." not in body
    assert "You're all caught up." not in body


def test_the_quiet_header_does_not_appear_when_the_lane_has_content(client):
    user = _user()
    for i in range(6):
        c = Contact.all_objects.create(
            user=user, name=f"Due {i:02d}", school_affiliation=True,
        )
        _touch(user, c, "outreach", days_ago=20)

    ctx = _cockpit_context(user)
    assert ctx["lanes"], "precondition: this queue has planned work in it"
    assert ctx["quiet"] is False
    assert ctx["quiet_line"] == ""
    assert "quiet-line" not in _login_and_get(client, user)


def test_the_quiet_header_does_not_appear_over_a_still_open_question(client):
    """A stale `confirm_chat` is a question the student still owes an
    answer to — not nothing, even though no plan lane is showing."""
    user = _user()
    # `school_affiliation`: the queue's relevance gate (crm.relevance) only
    # speaks about people at a tiered firm, people who share the student's
    # school, or people waiting on a reply — without it this contact's
    # confirm_chat is dropped before it can ever reach `still_open`.
    c = Contact.all_objects.create(
        user=user, name="Unconfirmed", thread_state="chat_scheduled",
        school_affiliation=True,
    )
    _touch(user, c, "outreach", days_ago=30)

    ctx = _cockpit_context(user)
    assert ctx["still_open"], "precondition: a stuck confirm_chat is present"
    assert ctx["quiet"] is False


def test_the_quiet_header_does_not_appear_alongside_a_pending_proposal(client):
    """A mailbox-scan proposal waiting for a tap is real content — the
    'batch window' the brief names."""
    from capture.models import ContactProposal

    user = _user()
    ContactProposal.all_objects.create(
        user=user, name="New Person", email="new@example.com",
        status=ContactProposal.STATUS_PENDING,
    )

    ctx = _cockpit_context(user)
    assert ctx["proposals"], "precondition: a pending proposal is present"
    assert ctx["quiet"] is False


def test_the_quiet_header_does_not_appear_over_a_real_park_backlog(client):
    """A real park backlog is a decision waiting on the student ("N contacts
    have gone quiet; park them below"), not nothing — this is a NARROWER
    rule than the seed gate's own SILENT test (test_today_seeds.py), which
    treats a park-only queue as quiet for a different question (whether a
    thin network still needs starter seeds). This header answers a
    different question — "is there a concrete forecast worth naming, with
    nothing else on the page" — and a park backlog answers that "no": the
    page already has something to say, so "Done for today ... gone quiet"
    keeps its earned line instead of being overwritten."""
    user = _user()
    for i in range(6):
        c = Contact.all_objects.create(
            user=user, name=f"Parked {i:02d}", school_affiliation=True,
        )
        _touch(user, c, "outreach", days_ago=40)
        _touch(user, c, "follow_up", days_ago=30)

    ctx = _cockpit_context(user)
    assert ctx["park_actions"], "precondition: this queue is park-eligible"
    assert not ctx["lanes"] and not ctx["held"] and not ctx["still_open"]
    assert ctx["quiet"] is False

    body = _login_and_get(client, user)
    assert "Done for today." in body
    assert "gone quiet" in body


def test_the_quiet_header_never_shows_for_a_brand_new_account_with_no_contacts(client):
    """Zero contacts keeps its own "No contacts yet" onboarding line —
    the quiet header is for an established network gone quiet, not day one."""
    user = _user()
    ctx = _cockpit_context(user)
    assert ctx["quiet"] is False
    body = _login_and_get(client, user)
    assert "No contacts yet." in body


def test_the_quiet_header_never_shows_while_starter_seeds_are_offered(client):
    """A thin, still-being-built network gets "Start here", not the quiet
    header — the two zero states mean opposite things and must not
    collide."""
    user = _user()
    c = Contact.all_objects.create(user=user, name="Ada Lovelace")
    _touch(user, c, "outreach", days_ago=0)

    ctx = _cockpit_context(user)
    assert ctx["seeds"], "precondition: this thin network gets starter seeds"
    assert ctx["quiet"] is False
    body = _login_and_get(client, user)
    assert "Start here" in body
