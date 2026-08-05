"""Route-level tests for the inbound webhook + capture views.

``transaction=True`` because a successful POST runs the apply step
(``log_touch`` on a separate connection). Auth-failure paths don't hit the DB
but share the marker for simplicity.
"""

from __future__ import annotations

import json

import pytest
from django.conf import settings
from django.urls import reverse

from crm.models import CaptureEvent, Touch

pytestmark = pytest.mark.django_db(transaction=True)

USER_EMAIL = "student@example.com"
SECRET = settings.CAPTURE_INBOUND_SECRET


def _post(client, payload, *, token=SECRET, header=True):
    kwargs = {"content_type": "application/json"}
    if token is not None and header:
        kwargs["HTTP_X_CAPTURE_TOKEN"] = token
    url = reverse("capture:inbound")
    if token is not None and not header:
        url = f"{url}?token={token}"
    return client.post(url, data=json.dumps(payload), **kwargs)


def test_missing_secret_is_401(client, make_payload, capture_addr):
    payload = make_payload(from_email="jane@bank.example", to=[(USER_EMAIL, "")], capture_addr=capture_addr)
    resp = _post(client, payload, token=None)
    assert resp.status_code == 401


def test_wrong_secret_is_401(client, user, make_payload, capture_addr):
    payload = make_payload(from_email="jane@bank.example", to=[(USER_EMAIL, "")], capture_addr=capture_addr)
    resp = _post(client, payload, token="not-the-secret")
    assert resp.status_code == 401
    assert CaptureEvent.all_objects.count() == 0


def test_non_ascii_secret_is_401_not_500(client, user, make_payload, capture_addr):
    """A garbage token with non-ASCII bytes must fail closed as a 401 —
    str-mode compare_digest raises TypeError, which surfaced as a 500."""
    payload = make_payload(from_email="jane@bank.example", to=[(USER_EMAIL, "")], capture_addr=capture_addr)
    resp = _post(client, payload, token="é-not-the-secret")
    assert resp.status_code == 401
    assert CaptureEvent.all_objects.count() == 0


def test_valid_secret_via_query_param_is_accepted(client, user, known_contact, make_payload, capture_addr):
    payload = make_payload(
        from_email=known_contact.email, to=[(USER_EMAIL, "")], capture_addr=capture_addr,
        text="Thanks!",
    )
    resp = _post(client, payload, header=False)  # ?token=
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"


def test_invalid_json_is_400(client, user):
    url = reverse("capture:inbound")
    resp = client.post(url, data="{not json", content_type="application/json", HTTP_X_CAPTURE_TOKEN=SECRET)
    assert resp.status_code == 400


def test_get_is_405(client):
    resp = client.get(reverse("capture:inbound"), HTTP_X_CAPTURE_TOKEN=SECRET)
    assert resp.status_code == 405


def test_unknown_address_returns_202_no_event(client, user, make_payload):
    payload = make_payload(
        from_email="jane@bank.example",
        to=[("u-nobody@in.coverage.app", "")],
        capture_addr="u-nobody@in.coverage.app",
    )
    resp = _post(client, payload)
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert CaptureEvent.all_objects.count() == 0


def test_webhook_idempotency_end_to_end(client, user, known_contact, make_payload, capture_addr):
    payload = make_payload(
        from_email=known_contact.email, to=[(USER_EMAIL, "")], capture_addr=capture_addr,
        message_id="<http-dedup@mail.example>", text="Sounds good.",
    )
    first = _post(client, payload)
    second = _post(client, payload)

    assert first.json()["status"] == "applied"
    assert second.json()["status"] == "duplicate"
    assert CaptureEvent.objects.for_user(user).count() == 1
    assert Touch.objects.for_user(user).filter(contact=known_contact).count() == 1


# --- health + review views require login --------------------------------- #

def test_health_requires_login(client):
    resp = client.get(reverse("capture:health"))
    assert resp.status_code in (301, 302)  # redirect to login


def test_health_shows_address_and_counts(client, user, known_contact, make_payload, capture_addr):
    _post(client, make_payload(
        from_email=known_contact.email, to=[(USER_EMAIL, "")], capture_addr=capture_addr, text="Hi",
    ))
    client.force_login(user)
    resp = client.get(reverse("capture:health"))
    assert resp.status_code == 200
    assert capture_addr.encode() in resp.content
    assert b"Last BCC received" in resp.content
    assert b"Nothing has come through yet" not in resp.content


def test_health_credits_the_mailbox_sync_when_bcc_is_unused(client, user):
    """The page used to read only the BCC pipeline, so an account fed by the
    twice-daily Gmail sync saw "No captured email yet" over four zeroes
    forever — a nav item permanently reporting the product broken while it
    was applying 104 findings a day. That was the founder's own account."""
    from analytics.models import Import

    Import.all_objects.create(
        user=user, kind="gmail_findings", filename="findings.json",
        row_stats={"findings": 104, "touches_logged": 5},
    )
    client.force_login(user)
    body = client.get(reverse("capture:health")).content.decode()
    assert "through the mailbox sync" in body
    assert "104 findings" in body
    assert "Nothing has come through yet" not in body


def test_health_is_honest_when_nothing_has_run_at_all(client, user):
    client.force_login(user)
    body = client.get(reverse("capture:health")).content.decode()
    assert "Nothing has come through yet" in body
    # No zero-counter table for a user who has never captured anything: four
    # zeroes read as a broken system rather than an unused one.
    assert "Applied (logged a touch)" not in body


def test_review_confirm_via_view_applies_touch(client, user, known_contact, make_payload, capture_addr):
    # Produce a needs_review event (auto-reply).
    _post(client, make_payload(
        from_email=known_contact.email, to=[(USER_EMAIL, "")], capture_addr=capture_addr,
        subject="Automatic reply: away", headers=[("Auto-Submitted", "auto-replied")],
    ))
    ev = CaptureEvent.objects.for_user(user).filter(status="needs_review").first()
    assert ev is not None

    client.force_login(user)
    resp = client.post(reverse("capture:confirm", args=[ev.id]), data={"touch_kind": "reply_received"})
    assert resp.status_code in (301, 302)

    ev.refresh_from_db()
    assert ev.status == "applied"
    known_contact.refresh_from_db()
    assert known_contact.warmth == "replied"
