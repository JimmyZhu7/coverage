"""Following the device's timezone, and the one case where it must not.

The product's stated audience flies: a USC student networking in Hong Kong
for three weeks, then back to Los Angeles. Every "today" in Coverage is
`timezone.localdate()` — the cadence queue, the pace week, chat times — so a
stale zone silently shifts all of it, and the only signal is that things look
subtly wrong. Asking the user to remember a Settings visit on landing is
asking them to notice a bug we could have avoided.

The server cannot work this out: an IP lookup is wrong behind a VPN and
creepy either way. The browser already knows, because the OS told it, and it
reports a real IANA name. So the design is a copy, gated on one flag.

The flag is the interesting part, and it is why this file exists. "Follow my
device" and "use the zone I picked" are both legitimate, and guessing wrong
in the second direction is much worse: a student deliberately working US
hours from abroad would find the product quietly overruling them on every
page load.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from accounts.forms import AUTO_TIMEZONE, ProfileForm

pytestmark = pytest.mark.django_db

DETECT = "accounts:timezone_detect"


@pytest.fixture
def student():
    return get_user_model().objects.create_user(
        email="flyer@example.com", password="x", capture_slug="flyerslug1"
    )


def _post(client, tz):
    return client.post(reverse(DETECT), {"timezone": tz})


# ---------------------------------------------------------------------------
# Following
# ---------------------------------------------------------------------------
def test_a_following_account_takes_the_browsers_zone(client, student):
    client.force_login(student)
    resp = _post(client, "Asia/Hong_Kong")
    assert resp.status_code == 200, "changed, so the page needs a reload"
    student.refresh_from_db()
    assert student.timezone == "Asia/Hong_Kong"
    assert student.timezone_auto is True, "following stays on"


def test_landing_somewhere_new_moves_the_zone_again(client, student):
    """The whole point: LA -> HK -> LA with no Settings visit."""
    client.force_login(student)
    _post(client, "Asia/Hong_Kong")
    resp = _post(client, "America/Los_Angeles")
    assert resp.status_code == 200
    student.refresh_from_db()
    assert student.timezone == "America/Los_Angeles"


def test_the_same_zone_again_writes_nothing(client, student):
    """This runs on EVERY page load. The common case is no change, and it
    must not be a database write — or a reload, which 200 would trigger."""
    student.timezone = "Asia/Hong_Kong"
    student.save(update_fields=["timezone"])
    client.force_login(student)
    assert _post(client, "Asia/Hong_Kong").status_code == 204


def test_a_zone_python_does_not_know_is_dropped(client, student):
    """Whatever a browser reports, only names `zoneinfo` can construct may
    reach the column — TimezoneMiddleware reads it on every request."""
    client.force_login(student)
    assert _post(client, "Mars/Olympus_Mons").status_code == 204
    student.refresh_from_db()
    assert student.timezone == ""


def test_detection_needs_a_login(client):
    assert _post(client, "Asia/Hong_Kong").status_code in (302, 403)


def test_detection_refuses_GET(client, student):
    client.force_login(student)
    assert client.get(reverse(DETECT)).status_code == 405


# ---------------------------------------------------------------------------
# Not following — the choice that must survive
# ---------------------------------------------------------------------------
def test_a_hand_picked_zone_is_never_overruled_by_the_device(client, student):
    """A student working US hours from Hong Kong set US Eastern on purpose.
    Their laptop says Asia/Hong_Kong on every page load; the product must not
    argue with them."""
    student.timezone = "America/New_York"
    student.timezone_auto = False
    student.save(update_fields=["timezone", "timezone_auto"])
    client.force_login(student)

    assert _post(client, "Asia/Hong_Kong").status_code == 204
    student.refresh_from_db()
    assert student.timezone == "America/New_York"


def test_the_page_does_not_even_ship_the_script_when_not_following(client, student):
    """Cheaper and safer than relying on the endpoint to refuse: a
    non-following account never runs detection at all."""
    student.timezone_auto = False
    student.save(update_fields=["timezone_auto"])
    client.force_login(student)
    body = client.get("/app/").content.decode()
    assert "resolvedOptions" not in body

    student.timezone_auto = True
    student.save(update_fields=["timezone_auto"])
    body = client.get("/app/").content.decode()
    assert "resolvedOptions" in body


# ---------------------------------------------------------------------------
# The Settings control is the switch between those two worlds
# ---------------------------------------------------------------------------
def _profile_post(**over):
    data = {"name": "", "school": "", "class_year": "", "target_cycle": "",
            "regions": [], "tracks": [], "timezone": AUTO_TIMEZONE}
    data.update(over)
    return data


def test_picking_a_zone_by_hand_turns_following_off(student):
    form = ProfileForm(_profile_post(timezone="America/New_York"))
    assert form.is_valid(), form.errors
    form.apply_to(student)
    student.refresh_from_db()
    assert student.timezone == "America/New_York"
    assert student.timezone_auto is False


def test_choosing_automatic_turns_following_back_on(student):
    student.timezone = "America/New_York"
    student.timezone_auto = False
    student.save(update_fields=["timezone", "timezone_auto"])

    form = ProfileForm(_profile_post(timezone=AUTO_TIMEZONE))
    assert form.is_valid(), form.errors
    form.apply_to(student)
    student.refresh_from_db()
    assert student.timezone_auto is True
    # The stored zone is left alone: it is still correct until the next page
    # load says otherwise, and blanking it would hand back a UTC day for no
    # reason.
    assert student.timezone == "America/New_York"


def test_a_following_account_shows_automatic_selected_not_its_detected_zone(student):
    """Rendering the detected zone as the selection would invite the user to
    "confirm" it and silently turn following off."""
    student.timezone = "Asia/Hong_Kong"
    student.timezone_auto = True
    student.save(update_fields=["timezone", "timezone_auto"])
    assert ProfileForm.from_user(student).initial["timezone"] == AUTO_TIMEZONE


def test_the_sentinel_is_not_a_real_zone():
    """It travels through a select carrying ~600 genuine IANA names."""
    from accounts.forms import known_timezones
    assert AUTO_TIMEZONE not in known_timezones()
