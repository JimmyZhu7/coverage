"""billing.stripe_gateway — pay-as-you-go credit top-ups. Every test here
mocks the Stripe SDK itself (`stripe.checkout.Session.create`,
`stripe.Webhook.construct_event`); nothing makes a real network call, and
nothing requires real Stripe keys — matching `capture/gmail_live.py`'s own
test posture for GMAIL_LIVE_* (see capture/tests/test_gmail_live.py's
`TestIsConfigured`).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase
from django.urls import reverse

import stripe
from billing import credits as billing_credits
from billing import stripe_gateway
from billing.models import CreditLedger, ProcessedStripeEvent

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def student():
    return User.objects.create_user(email="topup-student@example.com", password="x")


def _fake_checkout_completed_event(event_id: str, user_id: int, pack_key: str) -> dict:
    """The shape `stripe.Webhook.construct_event` hands back — a dict-like
    Stripe `Event` object. A plain dict works fine here: `stripe_gateway.
    handle_webhook_event` only ever does `event["type"]` / `event["data"]
    ["object"]` / `event["id"]` subscripting, never attribute access."""
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "metadata": {"user_id": str(user_id), "pack_key": pack_key},
            }
        },
    }


# ---------------------------------------------------------------------------
# is_configured()
# ---------------------------------------------------------------------------
class TestIsConfigured:
    def test_false_when_either_setting_missing(self, settings):
        settings.STRIPE_SECRET_KEY = ""
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        assert stripe_gateway.is_configured() is False

        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = ""
        assert stripe_gateway.is_configured() is False

    def test_true_when_both_set(self, settings):
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        assert stripe_gateway.is_configured() is True


# ---------------------------------------------------------------------------
# create_checkout_session
# ---------------------------------------------------------------------------
class TestCreateCheckoutSession:
    def test_raises_cleanly_when_not_configured(self, settings, student):
        settings.STRIPE_SECRET_KEY = ""
        settings.STRIPE_WEBHOOK_SECRET = ""
        with pytest.raises(stripe_gateway.StripeGatewayError):
            stripe_gateway.create_checkout_session(
                student, "small", success_url="https://x/ok", cancel_url="https://x/cancel"
            )

    def test_builds_session_with_price_data_and_metadata(self, settings, student):
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        fake_session = MagicMock(url="https://checkout.stripe.com/pay/cs_test_123")
        with patch("stripe.checkout.Session.create", return_value=fake_session) as create:
            session = stripe_gateway.create_checkout_session(
                student, "large", success_url="https://x/ok", cancel_url="https://x/cancel"
            )

        assert session is fake_session
        _, kwargs = create.call_args
        assert kwargs["mode"] == "payment"
        assert kwargs["metadata"] == {"user_id": str(student.id), "pack_key": "large"}
        assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 1200
        # Ad-hoc price_data, never a pre-created Stripe Price ID — Jimmy
        # hasn't set those up in the Dashboard.
        assert "price" not in kwargs["line_items"][0]


# ---------------------------------------------------------------------------
# grant_purchase
# ---------------------------------------------------------------------------
class TestGrantPurchase:
    def test_writes_the_correct_ledger_row_and_balance(self, settings, student):
        settings.CREDIT_PLANS = {"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}}
        starting_balance = billing_credits.balance(student)  # writes this month's grant

        billing_credits.grant_purchase(student, "small", "evt_test_1")

        row = CreditLedger.objects.for_user(student).get(kind=CreditLedger.KIND_PURCHASE)
        assert row.delta == 60
        assert row.props == {"pack": "small", "stripe_event_id": "evt_test_1", "price_cents": 500}
        assert billing_credits.balance(student) == starting_balance + 60

    def test_large_pack_grants_160(self, settings, student):
        settings.CREDIT_PLANS = {"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}}
        billing_credits.balance(student)

        billing_credits.grant_purchase(student, "large", "evt_test_2")

        row = CreditLedger.objects.for_user(student).get(kind=CreditLedger.KIND_PURCHASE)
        assert row.delta == 160
        assert row.props["price_cents"] == 1200


# ---------------------------------------------------------------------------
# handle_webhook_event — idempotency is the important case here
# ---------------------------------------------------------------------------
class TestHandleWebhookEvent:
    def test_grants_credits_on_checkout_session_completed(self, settings, student):
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        settings.CREDIT_PLANS = {"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}}
        billing_credits.balance(student)
        event = _fake_checkout_completed_event("evt_test_once", student.id, "small")

        with patch("stripe.Webhook.construct_event", return_value=event):
            stripe_gateway.handle_webhook_event(b"{}", "sig")

        assert CreditLedger.objects.for_user(student).filter(kind=CreditLedger.KIND_PURCHASE).count() == 1
        assert ProcessedStripeEvent.objects.filter(stripe_event_id="evt_test_once").count() == 1

    def test_redelivering_the_same_event_does_not_double_grant(self, settings, student):
        """Stripe's documented guarantee is at-least-once delivery — a
        webhook redelivering the identical event.id must not grant twice.
        This is the case the whole ProcessedStripeEvent table exists for."""
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        settings.CREDIT_PLANS = {"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}}
        billing_credits.balance(student)
        event = _fake_checkout_completed_event("evt_test_dupe", student.id, "small")

        with patch("stripe.Webhook.construct_event", return_value=event):
            stripe_gateway.handle_webhook_event(b"{}", "sig")
            # A second, identical delivery — Stripe redelivering the same event.
            stripe_gateway.handle_webhook_event(b"{}", "sig")

        assert CreditLedger.objects.for_user(student).filter(kind=CreditLedger.KIND_PURCHASE).count() == 1
        assert ProcessedStripeEvent.objects.filter(stripe_event_id="evt_test_dupe").count() == 1

    def test_bad_signature_raises_stripe_gateway_error(self, settings, student):
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        with patch(
            "stripe.Webhook.construct_event",
            side_effect=stripe.SignatureVerificationError("bad sig", "sig_header"),
        ):
            with pytest.raises(stripe_gateway.StripeGatewayError):
                stripe_gateway.handle_webhook_event(b"{}", "bad-sig")

    def test_unrecognised_event_type_is_ignored(self, settings, student):
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        event = {"id": "evt_other", "type": "payment_intent.succeeded", "data": {"object": {}}}
        with patch("stripe.Webhook.construct_event", return_value=event):
            stripe_gateway.handle_webhook_event(b"{}", "sig")
        assert CreditLedger.objects.for_user(student).filter(kind=CreditLedger.KIND_PURCHASE).count() == 0

    def test_a_nonexistent_user_id_is_rejected_cleanly_not_a_crash(self, settings):
        """The checkout session's `metadata.user_id` names a real user at the
        MOMENT the session is created, but Stripe's delivery isn't
        instantaneous — a student can delete their Coverage account (a real,
        hard delete — accounts/services.py::delete_user_and_data) in the
        window between starting checkout and Stripe delivering the webhook.
        The handler must reject that delivery the same clean way it rejects
        an unrecognised pack_key, not raise `User.DoesNotExist` and 500 —
        which would also roll back the `ProcessedStripeEvent` idempotency
        row (same `atomic()` block), so Stripe's automatic retry would 500
        again, forever."""
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        event = _fake_checkout_completed_event("evt_ghost_user", 999_999, "small")

        with patch("stripe.Webhook.construct_event", return_value=event):
            stripe_gateway.handle_webhook_event(b"{}", "sig")  # must not raise

        assert CreditLedger.all_objects.filter(kind=CreditLedger.KIND_PURCHASE).count() == 0
        assert ProcessedStripeEvent.objects.filter(stripe_event_id="evt_ghost_user").count() == 1

    def test_a_non_numeric_user_id_is_rejected_cleanly_not_a_crash(self, settings):
        """Malformed metadata (a corrupted session, a hand-crafted test event)
        must 400, not 500 with an unhandled `ValueError` from `int()`."""
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        event = {
            "id": "evt_bad_user_id",
            "type": "checkout.session.completed",
            "data": {"object": {"metadata": {"user_id": "not-a-number", "pack_key": "small"}}},
        }

        with patch("stripe.Webhook.construct_event", return_value=event):
            stripe_gateway.handle_webhook_event(b"{}", "sig")  # must not raise

        assert CreditLedger.all_objects.filter(kind=CreditLedger.KIND_PURCHASE).count() == 0
        assert ProcessedStripeEvent.objects.filter(stripe_event_id="evt_bad_user_id").count() == 1

    def test_the_webhook_view_returns_200_not_500_for_a_deleted_user(self, settings):
        """End to end through the view: Stripe must see a clean response it
        won't endlessly retry, never a 500."""
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        client = Client()
        event = _fake_checkout_completed_event("evt_ghost_view", 999_999, "small")

        with patch("stripe.Webhook.construct_event", return_value=event):
            resp = client.post(
                reverse("billing:webhook"),
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="sig",
            )

        assert resp.status_code == 200


class ConcurrentWebhookDeliveryTest(TransactionTestCase):
    """A real transactional test (see billing/tests/test_credits.py's own
    `ConcurrentGrantTest` for why `TransactionTestCase` and not the default
    `django_db` fixture is needed here): two threads racing
    `handle_webhook_event` with the SAME event ID — simulating Stripe's two
    near-simultaneous webhook deliveries — must still land exactly one
    grant, never two."""

    def setUp(self):
        self.student = User.objects.create_user(email="racer-topup@example.com", password="x")

    def test_concurrent_identical_deliveries_never_double_grant(self):
        with self.settings(
            STRIPE_SECRET_KEY="sk_test_x",
            STRIPE_WEBHOOK_SECRET="whsec_x",
            CREDIT_PLANS={"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}},
        ):
            billing_credits.balance(self.student)
            event = _fake_checkout_completed_event("evt_test_race", self.student.id, "small")
            errors = []

            def _go():
                try:
                    with patch("stripe.Webhook.construct_event", return_value=event):
                        stripe_gateway.handle_webhook_event(b"{}", "sig")
                except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
                    errors.append(exc)

            threads = [threading.Thread(target=_go) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert errors == []
            assert (
                CreditLedger.objects.for_user(self.student)
                .filter(kind=CreditLedger.KIND_PURCHASE)
                .count()
                == 1
            )
            assert ProcessedStripeEvent.objects.filter(stripe_event_id="evt_test_race").count() == 1


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
class TestCheckoutView:
    def test_returns_clean_response_when_not_configured(self, settings, student):
        settings.STRIPE_SECRET_KEY = ""
        settings.STRIPE_WEBHOOK_SECRET = ""
        client = Client()
        client.force_login(student)

        resp = client.post(reverse("billing:checkout", args=["small"]))

        assert resp.status_code == 302  # redirect back to Settings, not a 500
        assert reverse("accounts:settings") in resp["Location"]

    def test_redirects_to_stripe_when_configured(self, settings, student):
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        client = Client()
        client.force_login(student)
        fake_session = MagicMock(url="https://checkout.stripe.com/pay/cs_test_123")

        with patch("stripe.checkout.Session.create", return_value=fake_session):
            resp = client.post(reverse("billing:checkout", args=["small"]))

        assert resp.status_code == 302
        assert resp["Location"] == "https://checkout.stripe.com/pay/cs_test_123"

    def test_unknown_pack_key_is_a_clean_redirect_not_a_500(self, settings, student):
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        client = Client()
        client.force_login(student)

        resp = client.post(reverse("billing:checkout", args=["huge"]))

        assert resp.status_code == 302
        assert reverse("accounts:settings") in resp["Location"]


class TestWebhookView:
    def test_returns_400_when_not_configured(self, settings):
        settings.STRIPE_SECRET_KEY = ""
        settings.STRIPE_WEBHOOK_SECRET = ""
        client = Client()

        resp = client.post(reverse("billing:webhook"), data=b"{}", content_type="application/json")

        assert resp.status_code == 400

    def test_returns_400_on_bad_signature(self, settings):
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        client = Client()

        with patch(
            "stripe.Webhook.construct_event",
            side_effect=stripe.SignatureVerificationError("bad sig", "sig_header"),
        ):
            resp = client.post(
                reverse("billing:webhook"),
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="bad",
            )

        assert resp.status_code == 400

    def test_returns_200_on_valid_event(self, settings, student):
        settings.STRIPE_SECRET_KEY = "sk_test_x"
        settings.STRIPE_WEBHOOK_SECRET = "whsec_x"
        settings.CREDIT_PLANS = {"free": {"monthly_grant": 60, "message_cost": 1, "daily_burst": 15}}
        billing_credits.balance(student)
        client = Client()
        event = _fake_checkout_completed_event("evt_view_test", student.id, "small")

        with patch("stripe.Webhook.construct_event", return_value=event):
            resp = client.post(
                reverse("billing:webhook"),
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="sig",
            )

        assert resp.status_code == 200
        assert CreditLedger.objects.for_user(student).filter(kind=CreditLedger.KIND_PURCHASE).count() == 1
