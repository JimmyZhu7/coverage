"""accounts.push_subscribe / push_unsubscribe — the endpoints Settings'
Notifications toggle POSTs to (static/js/push-subscribe.js), plus the
Settings page context that gates the toggle on VAPID being configured.

Covers: method requirements, malformed-payload handling, and tenant
isolation (accounts.models.PushSubscription is a private-zone model —
coverage_web/tenancy.py — so a write must never let one user touch another
user's row).
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

VALID_PAYLOAD = {
    "endpoint": "https://push.example.com/abc123",
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
    resp = _post_json(client, UNSUBSCRIBE, {"endpoint": "https://push.example.com/x"})
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
    resp = _post_json(client, SUBSCRIBE, {"endpoint": "https://push.example.com/x"})
    assert resp.status_code == 400


def test_subscribe_rejects_a_blank_p256dh(client, logged_in):
    resp = _post_json(client, SUBSCRIBE, {
        "endpoint": "https://push.example.com/x",
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
    resp = _post_json(client, UNSUBSCRIBE, {"endpoint": "https://push.example.com/never-existed"})
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Tenant isolation — the one write endpoint another user must never reach.
# ---------------------------------------------------------------------------
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
def test_settings_renders_the_toggle_disabled_when_vapid_is_unset(client, logged_in):
    resp = client.get(reverse("accounts:settings"))
    body = resp.content.decode()
    assert 'id="push-alerts"' in body
    assert "disabled" in body[body.index('id="push-alerts-toggle"'):body.index('id="push-alerts-toggle"') + 200]


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
