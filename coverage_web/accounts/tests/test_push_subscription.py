"""accounts.push_subscribe / push_unsubscribe — the endpoints Settings'
Notifications toggle POSTs to (static/js/push-subscribe.js), plus the
Settings page context that gates the toggle on VAPID being configured.

Covers: method requirements, malformed-payload handling, endpoint-host
validation, and tenant isolation (accounts.models.PushSubscription is a
private-zone model — coverage_web/tenancy.py — so a write must never let one
user touch another user's row).

EVERY ENDPOINT IN THIS FILE NAMES A REAL PUSH SERVICE. It did not use to:
the fixtures posted `https://push.example.com/...`, which reads like a
plausible stand-in and is exactly the shape of URL the subscribe view now
refuses (accounts/push.py's allowlist, added 2026-09-01 — an arbitrary host
here is a host the nightly alert cron will POST to unattended). A fixture
that could not exist in production was pinning behaviour that must not
exist either, so the constants below moved to FCM's real host rather than
the view being loosened to keep them passing.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from accounts.models import PushSubscription

User = get_user_model()

SUBSCRIBE = "accounts:push_subscribe"
UNSUBSCRIBE = "accounts:push_unsubscribe"

FCM = "https://fcm.googleapis.com/fcm/send/abc123"

VALID_PAYLOAD = {
    "endpoint": FCM,
    "keys": {"p256dh": "p256dh-value", "auth": "auth-value"},
}


@pytest.fixture
def user(db):
    return User.objects.create_user(email="push@example.com", password="x")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@example.com", password="x")


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return user


def _post_json(client, url_name, payload):
    return client.post(
        reverse(url_name), data=json.dumps(payload), content_type="application/json"
    )


# ---------------------------------------------------------------------------
# Auth / method requirements
# ---------------------------------------------------------------------------
def test_subscribe_requires_login(client):
    resp = _post_json(client, SUBSCRIBE, VALID_PAYLOAD)
    assert resp.status_code in (302, 403)  # redirected to login, never a 200


def test_subscribe_rejects_get(client, logged_in):
    resp = client.get(reverse(SUBSCRIBE))
    assert resp.status_code == 405


def test_unsubscribe_requires_login(client):
    resp = _post_json(client, UNSUBSCRIBE, {"endpoint": FCM})
    assert resp.status_code in (302, 403)


def test_unsubscribe_rejects_get(client, logged_in):
    resp = client.get(reverse(UNSUBSCRIBE))
    assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Malformed payloads
# ---------------------------------------------------------------------------
def test_subscribe_rejects_invalid_json(client, logged_in):
    resp = client.post(reverse(SUBSCRIBE), data=b"not json", content_type="application/json")
    assert resp.status_code == 400
    assert not PushSubscription.all_objects.exists()


def test_subscribe_rejects_a_json_array_not_object(client, logged_in):
    resp = client.post(reverse(SUBSCRIBE), data=json.dumps([1, 2]), content_type="application/json")
    assert resp.status_code == 400


def test_subscribe_rejects_missing_endpoint(client, logged_in):
    resp = _post_json(client, SUBSCRIBE, {"keys": {"p256dh": "a", "auth": "b"}})
    assert resp.status_code == 400
    assert not PushSubscription.all_objects.exists()


def test_subscribe_rejects_missing_keys(client, logged_in):
    resp = _post_json(client, SUBSCRIBE, {"endpoint": FCM})
    assert resp.status_code == 400


def test_subscribe_rejects_a_blank_p256dh(client, logged_in):
    resp = _post_json(client, SUBSCRIBE, {
        "endpoint": FCM,
        "keys": {"p256dh": "", "auth": "b"},
    })
    assert resp.status_code == 400


def test_unsubscribe_rejects_missing_endpoint(client, logged_in):
    resp = _post_json(client, UNSUBSCRIBE, {})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_subscribe_saves_the_subscription_for_the_logged_in_user(client, logged_in):
    resp = _post_json(client, SUBSCRIBE, VALID_PAYLOAD)
    assert resp.status_code == 201

    sub = PushSubscription.all_objects.get(endpoint=VALID_PAYLOAD["endpoint"])
    assert sub.user_id == logged_in.id
    assert sub.p256dh == "p256dh-value"
    assert sub.auth == "auth-value"


def test_subscribing_the_same_endpoint_again_updates_rather_than_duplicates(client, logged_in):
    _post_json(client, SUBSCRIBE, VALID_PAYLOAD)
    updated = dict(VALID_PAYLOAD, keys={"p256dh": "new-p256dh", "auth": "new-auth"})
    resp = _post_json(client, SUBSCRIBE, updated)

    assert resp.status_code == 201
    assert PushSubscription.all_objects.filter(endpoint=VALID_PAYLOAD["endpoint"]).count() == 1
    sub = PushSubscription.all_objects.get(endpoint=VALID_PAYLOAD["endpoint"])
    assert sub.p256dh == "new-p256dh"


def test_unsubscribe_deletes_the_callers_own_subscription(client, logged_in):
    _post_json(client, SUBSCRIBE, VALID_PAYLOAD)
    resp = _post_json(client, UNSUBSCRIBE, {"endpoint": VALID_PAYLOAD["endpoint"]})

    assert resp.status_code == 204
    assert not PushSubscription.all_objects.filter(endpoint=VALID_PAYLOAD["endpoint"]).exists()


def test_unsubscribing_an_unknown_endpoint_is_a_harmless_no_op(client, logged_in):
    resp = _post_json(client, UNSUBSCRIBE, {"endpoint": "https://fcm.googleapis.com/fcm/send/never-existed"})
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Endpoint host validation — the stored URL is what the nightly alert cron
# POSTs to, so an arbitrary one is a blind SSRF with a scheduler attached.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("endpoint", [
    "https://example.invalid/x",                       # the audit's own probe
    "http://fcm.googleapis.com/fcm/send/x",            # right host, wrong scheme
    "https://169.254.169.254/latest/meta-data/",        # cloud metadata
    "http://127.0.0.1:8000/ops/health/cron/",           # back at ourselves
    "https://evil-notify.windows.com/x",                # suffix without the dot
    "https://notify.windows.com.attacker.test/x",       # allowlisted host as a prefix
    "https://fcm.googleapis.com.attacker.test/x",       # allowlisted host as a prefix
    "https://fcm.googleapis.com@attacker.test/x",       # allowlisted host as userinfo
    "ftp://fcm.googleapis.com/x",
])
def test_subscribe_refuses_an_endpoint_that_is_not_a_push_service(client, logged_in, endpoint):
    resp = _post_json(client, SUBSCRIBE, {
        "endpoint": endpoint, "keys": {"p256dh": "a", "auth": "b"},
    })
    assert resp.status_code == 400
    assert not PushSubscription.all_objects.exists()


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/fcm/send/x",
    "https://android.googleapis.com/gcm/send/x",
    "https://updates.push.services.mozilla.com/wpush/v2/x",
    "https://web.push.apple.com/x",
    "https://sin.notify.windows.com/w/?token=x",
])
def test_subscribe_accepts_every_real_browser_push_service(client, logged_in, endpoint):
    resp = _post_json(client, SUBSCRIBE, {
        "endpoint": endpoint, "keys": {"p256dh": "a", "auth": "b"},
    })
    assert resp.status_code == 201
    assert PushSubscription.all_objects.filter(endpoint=endpoint).exists()


# ---------------------------------------------------------------------------
# Tenant isolation — the one write endpoint another user must never reach.
# ---------------------------------------------------------------------------
def test_subscribe_refuses_to_take_over_another_users_endpoint(client, user, other_user):
    """Knowing the endpoint string used to be enough to seize the row.

    `update_or_create(endpoint=...)` reassigned `user` to whoever posted
    last, which meant anyone holding a leaked endpoint could both silence
    the victim's deadline alerts and put their own on that browser. The
    endpoint is a bearer credential for the BROWSER, not for an HTTP client
    that merely learned the string.
    """
    PushSubscription.all_objects.create(
        user=user, endpoint=FCM, p256dh="a", auth="b"
    )
    client.force_login(other_user)

    resp = _post_json(client, SUBSCRIBE, VALID_PAYLOAD)

    assert resp.status_code == 409
    sub = PushSubscription.all_objects.get(endpoint=FCM)
    assert sub.user_id == user.id
    assert sub.p256dh == "a"


def test_a_freed_endpoint_can_be_claimed_by_the_next_account_on_that_browser(
    client, user, other_user
):
    """The shared-device case the old reassign-on-write behaviour existed
    for still works, it just runs through the first account's own opt-out
    instead of being taken from underneath it."""
    client.force_login(user)
    assert _post_json(client, SUBSCRIBE, VALID_PAYLOAD).status_code == 201
    assert _post_json(client, UNSUBSCRIBE, {"endpoint": FCM}).status_code == 204

    client.force_login(other_user)
    assert _post_json(client, SUBSCRIBE, VALID_PAYLOAD).status_code == 201
    assert PushSubscription.all_objects.get(endpoint=FCM).user_id == other_user.id


def test_a_user_cannot_unsubscribe_another_users_subscription(client, user, other_user):
    PushSubscription.all_objects.create(
        user=user, endpoint=VALID_PAYLOAD["endpoint"], p256dh="a", auth="b"
    )
    client.force_login(other_user)

    resp = _post_json(client, UNSUBSCRIBE, {"endpoint": VALID_PAYLOAD["endpoint"]})

    assert resp.status_code == 204  # still a no-op response, not an error that leaks existence
    assert PushSubscription.all_objects.filter(endpoint=VALID_PAYLOAD["endpoint"]).exists()


# ---------------------------------------------------------------------------
# Settings page context — gates the toggle on VAPID being configured.
# ---------------------------------------------------------------------------
@override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")
def test_settings_omits_the_toggle_entirely_when_vapid_is_unset(client, logged_in):
    """It used to render the real checkbox, permanently disabled, over the
    words "Not set up yet." — an interactive control for a state the reader
    cannot reach, on every deploy the product has ever had (no VAPID key has
    ever been configured and no PushSubscription row has ever existed).

    The rule is now the one Gmail Live already followed: unconfigured means
    the row is not drawn. Nothing about the feature moved — the toggle, its
    endpoints and accounts/push.py are untouched, and the row comes back the
    moment VAPID_PUBLIC_KEY is set (asserted by the two tests below). The
    card it lives in still renders, because its other rows are real."""
    body = client.get(reverse("accounts:settings")).content.decode()
    assert 'id="preferences"' in body
    assert 'id="push-alerts-toggle"' not in body
    assert "data-push-root" not in body


@override_settings(VAPID_PUBLIC_KEY="test-public-key", VAPID_PRIVATE_KEY="test-private-key")
def test_settings_reflects_an_existing_subscription_as_checked(client, logged_in):
    PushSubscription.all_objects.create(
        user=logged_in, endpoint=VALID_PAYLOAD["endpoint"], p256dh="a", auth="b"
    )
    resp = client.get(reverse("accounts:settings"))
    body = resp.content.decode()
    snippet = body[body.index('id="push-alerts-toggle"'):body.index('id="push-alerts-toggle"') + 200]
    assert "checked" in snippet


@override_settings(VAPID_PUBLIC_KEY="test-public-key", VAPID_PRIVATE_KEY="test-private-key")
def test_settings_exposes_the_vapid_public_key_for_the_client_script(client, logged_in):
    resp = client.get(reverse("accounts:settings"))
    assert b'data-vapid-public-key="test-public-key"' in resp.content
