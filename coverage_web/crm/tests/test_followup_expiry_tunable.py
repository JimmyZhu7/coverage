"""The web side of the follow-up shelf life: `followup_expires_after_
business_days` as a Settings knob (crm.today.TUNABLE_CADENCE_PARAMS +
accounts.forms.CADENCE_LABELS) and as a change to the live queue.

The engine's own behaviour is covered in
coverage_domain/tests/test_cadence_followup_expiry.py.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from accounts.forms import CADENCE_LABELS, CADENCE_SEGMENTS
from coverage_domain.cadence import CADENCE_DEFAULTS
from crm.models import Contact, Touch
from crm.today import TUNABLE_CADENCE_PARAMS, _build_actions, _cadence_params, _cockpit_context

KEY = "followup_expires_after_business_days"


def _user(email="expiry@example.com", **kw):
    kw.setdefault("weekly_touch_goal", 14)
    return get_user_model().objects.create_user(email=email, password="pw12345!", **kw)


def _touch(user, contact, kind, *, days_ago=0):
    return Touch.all_objects.create(
        user=user, contact=contact, kind=kind, channel="email",
        ts=timezone.now() - timedelta(days=days_ago),
    )


# ---------------------------------------------------------------------------
# 1. The knob is wired in every place the Settings page needs it.
# ---------------------------------------------------------------------------
def test_the_key_is_tunable_with_the_agreed_range():
    assert TUNABLE_CADENCE_PARAMS[KEY] == (5, 60)


def test_the_key_has_its_paired_label():
    """A key in TUNABLE_CADENCE_PARAMS without a CADENCE_LABELS entry is a
    500 on Settings — the pairing is the whole reason both dicts say so."""
    label, unit, desc = CADENCE_LABELS[KEY]
    assert label == "Follow-Up Expiry"
    assert unit == "business days"
    assert desc
    assert KEY not in CADENCE_SEGMENTS, "a 5-60 window is a spinner, not a two-option segment"


def test_every_tunable_key_is_labelled_and_defaulted():
    """The invariant the new key joins, asserted over the whole set."""
    for key in TUNABLE_CADENCE_PARAMS:
        assert key in CADENCE_LABELS, key
        low, high = TUNABLE_CADENCE_PARAMS[key]
        assert low <= CADENCE_DEFAULTS[key] <= high, key


def test_the_default_sits_inside_the_range():
    low, high = TUNABLE_CADENCE_PARAMS[KEY]
    assert low <= CADENCE_DEFAULTS[KEY] == 15 <= high


@pytest.mark.django_db
@pytest.mark.parametrize("stored, kept", [
    (20, True), (5, True), (60, True),
    (4, False), (61, False), ("15", False), (True, False), (None, False),
])
def test_the_whitelist_keeps_in_range_integers_and_drops_the_rest(stored, kept):
    user = _user(cadence_params={KEY: stored})
    params = _cadence_params(user)
    assert (KEY in params) is kept
    if kept:
        assert params[KEY] == stored


@pytest.mark.django_db
def test_the_settings_page_renders_the_knob_with_its_default(client):
    user = _user()
    client.force_login(user)
    body = client.get(reverse("accounts:settings")).content.decode()
    assert re.search(rf'name="{KEY}"[^>]*placeholder="15"', body) or \
        re.search(rf'placeholder="15"[^>]*name="{KEY}"', body)
    assert "Follow-Up Expiry" in body


@pytest.mark.django_db
def test_the_settings_page_saves_the_knob(client):
    user = _user()
    client.force_login(user)
    body = client.get(reverse("accounts:settings")).content.decode()
    assert re.search(rf'name="{KEY}"', body)
    # Post the cadence section with every knob blank except this one.
    data = {key: "" for key in TUNABLE_CADENCE_PARAMS}
    data[KEY] = "30"
    data["section"] = "cadence"
    resp = client.post(reverse("accounts:settings"), data)
    assert resp.status_code in (200, 302)
    user.refresh_from_db()
    assert user.cadence_params.get(KEY) == 30


# ---------------------------------------------------------------------------
# 2. The live queue.
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_a_month_old_first_note_is_a_park_by_default_and_a_follow_up_at_sixty():
    """30 calendar days is 20-22 business days on every weekday: past the
    default 15, inside a tuned 60.

    REWRITTEN 2026-09-02. It asserted the card said "went unanswered ...
    weeks ago", and the copy pass of that night cut both halves — "weeks ago"
    because the ledger row directly beneath already prints the same silence in
    business days (the queue's own unit, and the one the expiry is measured
    in), "went unanswered" because the badge above the sentence reads PARK IT.
    See `crm/tests/test_act_card_copy_2026_09_02.py` for the rule.

    THE FACT THE PHRASE CARRIED is the only thing this ever needed from the
    string: that the engine took the EXPIRED branch and not the cap-reached
    one, which is what the tunable under test moves. Read off `ctx["expired"]`
    now — the same flag the sentence itself is built from, one step earlier —
    so a future copy pass cannot break this test and a future ENGINE change
    cannot slip past it.
    """
    user = _user()
    c = Contact.all_objects.create(user=user, name="Gone Quiet", school_affiliation=True)
    _touch(user, c, "outreach", days_ago=30)

    actions, _ = _build_actions(user)
    mine = [a for a in actions if a["contact"]["name"] == "Gone Quiet"]
    assert [a["action"] for a in mine] == ["park"]
    assert mine[0]["ctx"]["expired"] is True, (
        "this is the follow-up window expiring, not the touch cap filling up"
    )
    assert mine[0]["reason"] == (
        "Too late to follow up. Re-open only with a new reason."
    )

    user.cadence_params = {KEY: 60}
    user.save(update_fields=["cadence_params"])
    actions, _ = _build_actions(user)
    mine = [a for a in actions if a["contact"]["name"] == "Gone Quiet"]
    assert [a["action"] for a in mine] == ["follow_up"]


@pytest.mark.django_db(transaction=True)
def test_an_expired_note_lands_in_the_park_strip_not_the_plan():
    """`park` is CLASS_PARK on Today: a bulk strip, never a plan slot. An
    expired first note must go there, exactly like the two-touch park."""
    user = _user()
    for i in range(3):
        c = Contact.all_objects.create(user=user, name=f"Stale {i}", school_affiliation=True)
        _touch(user, c, "outreach", days_ago=30)

    ctx = _cockpit_context(user)
    assert not ctx["lanes"]
    assert sorted(a["contact"]["name"] for a in ctx["park_actions"]) == \
        ["Stale 0", "Stale 1", "Stale 2"]


@pytest.mark.django_db(transaction=True)
def test_a_ten_day_old_note_is_still_a_follow_up():
    """The weekday-proof "due" offset the engine's tests use, well inside the
    shelf life: nothing about the ordinary follow-up changed."""
    user = _user()
    c = Contact.all_objects.create(user=user, name="Still Fresh", school_affiliation=True)
    _touch(user, c, "outreach", days_ago=10)
    actions, _ = _build_actions(user)
    assert [a["action"] for a in actions if a["contact"]["name"] == "Still Fresh"] == ["follow_up"]
