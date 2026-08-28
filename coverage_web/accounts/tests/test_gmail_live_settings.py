"""Phase 2's Settings-page surface: the "Scan Now" button and its
disabled/in-flight state on /welcome/settings/#gmail-live.
`accounts.views._gmail_live_context` supplies `rescan_in_progress`, which
`templates/accounts/settings.html`'s Gmail Live section renders from.

Requires `GMAIL_LIVE_*` configured (`gmail_live.is_configured()` gates the
whole section — see that function) so these tests exercise the actual
`{% if gmail_live.available %}` branch rather than the "nothing renders"
early-out every other settings test implicitly relies on.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from capture.models import GmailConnection

User = get_user_model()

pytestmark = pytest.mark.django_db

_GMAIL_LIVE_SETTINGS = dict(
    GMAIL_LIVE_CLIENT_ID="test-client-id",
    GMAIL_LIVE_CLIENT_SECRET="test-secret",
    GMAIL_LIVE_PUBSUB_TOPIC="projects/test/topics/gmail",
    GMAIL_LIVE_TOKEN_KEY="l7Xu3f7cA2k9m4qz1p6vD8y2wR5tB0nS9cE4hJ7iK2o=",
)


@pytest.fixture
def student():
    return User.objects.create_user(email="settings-gmail@example.com", password="x")


@pytest.fixture
def logged_in(client, student):
    client.force_login(student)
    return client


@override_settings(**_GMAIL_LIVE_SETTINGS)
def test_scan_now_button_is_enabled_when_no_rescan_is_in_flight(logged_in, student):
    GmailConnection.all_objects.create(
        user=student, gmail_address="settings-gmail@example.com",
        refresh_token_encrypted="x", status="active", rescan_status="none",
    )
    resp = logged_in.get(reverse("accounts:settings"))
    html = resp.content.decode()
    assert "Scan Now" in html
    assert "Rescan Gmail" in html
    # The button itself must not carry `disabled` in this state.
    button_start = html.index('action="' + reverse("capture:gmail_rescan"))
    button_chunk = html[button_start:button_start + 400]
    assert "disabled" not in button_chunk


@pytest.mark.parametrize("in_flight_status", ["pending", "running"])
@override_settings(**_GMAIL_LIVE_SETTINGS)
def test_scan_now_button_is_disabled_while_a_rescan_is_in_flight(logged_in, student, in_flight_status):
    GmailConnection.all_objects.create(
        user=student, gmail_address="settings-gmail@example.com",
        refresh_token_encrypted="x", status="active", rescan_status=in_flight_status,
    )
    resp = logged_in.get(reverse("accounts:settings"))
    html = resp.content.decode()
    button_start = html.index('action="' + reverse("capture:gmail_rescan"))
    button_chunk = html[button_start:button_start + 400]
    assert "disabled" in button_chunk
    assert "Scanning" in button_chunk


@override_settings(**_GMAIL_LIVE_SETTINGS)
def test_no_gmail_live_section_at_all_when_not_connected(logged_in, student):
    """No `GmailConnection` row means the whole card still renders (it's
    the connect prompt), but there is nothing to rescan yet, so no
    "Scan Now" control should appear."""
    resp = logged_in.get(reverse("accounts:settings"))
    html = resp.content.decode()
    assert "Gmail Live" in html
    assert "Scan Now" not in html


@override_settings(
    GMAIL_LIVE_CLIENT_ID="", GMAIL_LIVE_CLIENT_SECRET="",
    GMAIL_LIVE_PUBSUB_TOPIC="", GMAIL_LIVE_TOKEN_KEY="",
)
def test_gmail_live_section_absent_entirely_when_unconfigured(logged_in, student):
    """With no GMAIL_LIVE_* settings (the default in every test/deploy
    without it set up), the whole section — including any "Scan Now"
    control — must not render at all. `gmail_live.is_configured()` is the
    off-switch for the whole feature, not just the button.

    Force-unset explicitly rather than relying on the environment's
    defaults: the founder's own local `.env` sets all four (the live poll
    loop depends on it), which used to make this test fail on his machine
    while passing in a clean checkout — the assertion is about the
    UNCONFIGURED state, not about whatever happens to be in the
    environment the suite runs in."""
    resp = logged_in.get(reverse("accounts:settings"))
    html = resp.content.decode()
    assert "gmail-live" not in html
    assert "Scan Now" not in html
