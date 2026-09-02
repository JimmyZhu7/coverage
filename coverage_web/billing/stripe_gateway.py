"""billing.stripe_gateway — pay-as-you-go credit top-ups on top of the
ledger `billing/credits.py` already owns (docs/credit-system-plan.md's
"Stripe later" note: "paid top-up packs, if ever wanted, are just positive
ledger rows with `kind='purchase'`. No schema change needed.").

STRIPE IS NOT SET UP ON THIS DEPLOY YET, and this module is built so that's
fine: `is_configured()` is the runtime gate, mirroring
`capture/gmail_live.py::is_configured()` — every entry point checks it (or
calls a function that does) before touching the Stripe SDK, so an
unconfigured deploy shows a disabled "Coming soon" block on Settings and a
webhook endpoint that 400s cleanly, rather than 500ing on a missing key.
`import stripe` itself is safe with no key present; only actually calling
the SDK requires one, which is exactly what that gate is protecting.

Checkout Sessions are built with ad-hoc `price_data`, not pre-created
Stripe Price IDs — Jimmy hasn't set those up in the Dashboard, and dynamic
`price_data` means this never needs Dashboard configuration to work, only
the two env vars below. `user.id` and the pack key travel in `metadata` so
the webhook (which runs with no request context at all) can identify what
to grant without a database lookup keyed on anything Stripe-specific.

Idempotency: Stripe redelivers webhook events (at-least-once delivery is
the documented guarantee), so `handle_webhook_event` must not double-grant
on a duplicate delivery of the same `event.id`. Enforced with
`ProcessedStripeEvent` (billing/models.py) — a plain, non-tenant model,
because a webhook event isn't tied to any request-time tenant context — and
a `get_or_create` inside `transaction.atomic()`, so two concurrent
deliveries of the same event race on that row's unique constraint, not on
the ledger.

PAID, NOT MERELY COMPLETED. `checkout.session.completed` fires when the
customer finishes the form, which is not the same event as the money
arriving. With Stripe's dynamic payment methods on (the default), a delayed
method — a bank debit, a voucher — completes the session with
`payment_status="unpaid"` and settles hours or days later, or never. So the
grant is gated on `payment_status == "paid"` and the delayed settlement is
picked up from `checkout.session.async_payment_succeeded` instead; the
matching `async_payment_failed` needs no handler, because a session that
never reached "paid" was never granted anything to reverse.

API VERSION IS PINNED (`STRIPE_API_VERSION`), not left at the account
default. Unpinned, the shape of every object this module reads — the
`payment_status` field above included — changes the day Stripe's dashboard
default rolls forward, with no deploy and no way to notice except a
webhook that quietly stops granting. The constant is pinned to the version
the installed SDK is itself built against, and a test asserts the two still
agree so an SDK bump that moves the default is a red test rather than a
silent behaviour change in production.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

import stripe

from . import credits as billing_credits
from .models import ProcessedStripeEvent

logger = logging.getLogger(__name__)

#: The Stripe API version every call from this module is made against —
#: see the module docstring. Kept equal to `stripe.api_version`, the version
#: the installed SDK is generated for, so pinning changes nothing about how
#: the SDK behaves today and only stops the ACCOUNT default (a dashboard
#: setting, changeable without a deploy) from silently moving underneath us.
#: `test_stripe_gateway.py` asserts the equality, so bumping the `stripe`
#: dependency is the moment this constant is reconsidered.
STRIPE_API_VERSION = "2026-07-29.dahlia"

#: The two events that mean "this checkout session is paid for". The first
#: fires immediately for a card; the second is how a delayed payment method
#: (bank debit, voucher) reports settlement, hours or days after the session
#: itself completed unpaid. `checkout.session.async_payment_failed` is
#: deliberately absent: nothing was granted on the unpaid completion, so
#: there is nothing to reverse.
GRANTING_EVENT_TYPES = frozenset({
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
})

#: Two packs, both sized against docs/credit-system-plan.md's governing
#: ratio — grant <= 15 credits per dollar of price, to hold >= 70% margin
#: at full burn ($0.02/credit planning cost). Price in cents throughout, to
#: avoid float arithmetic on money.
CREDIT_PACKS: dict[str, dict] = {
    "small": {
        "label": "$5 — 60 credits",
        "price_cents": 500,
        "credits": 60,  # 12 credits/$ ≈ 76% margin at full burn
    },
    "large": {
        "label": "$12 — 160 credits",
        "price_cents": 1200,
        "credits": 160,  # 13.3 credits/$ ≈ 73% margin at full burn
    },
}


class StripeGatewayError(Exception):
    """Raised for a Stripe-shaped failure the caller must react to: calling
    `create_checkout_session` while not configured, or a webhook signature
    that doesn't verify. Not raised for "the pack key doesn't exist" —
    that's a plain `KeyError` at the caller, a programmer error rather than
    a runtime condition."""


def is_configured() -> bool:
    """The feature's actual off-switch — see this module's docstring on why
    every entry point gates on this rather than assuming keys are present.
    Mirrors `capture/gmail_live.py::is_configured()`'s shape exactly."""
    return bool(settings.STRIPE_SECRET_KEY and settings.STRIPE_WEBHOOK_SECRET)


def create_checkout_session(user, pack_key: str, success_url: str, cancel_url: str):
    """Builds and returns a Stripe Checkout Session for `pack_key` (one of
    `CREDIT_PACKS`). The caller redirects the browser to `session.url`.

    Raises `StripeGatewayError` if called while `not is_configured()` —
    callers must check first, the same discipline `gmail_live.build_auth_url`
    expects of its own callers. Deliberately not silent here: unlike a
    no-op UI gate, a caller that reaches this function has already decided
    to charge someone's card, so a swallowed misconfiguration would be a
    checkout that quietly never happens rather than a checkout that never
    starts.
    """
    if not is_configured():
        raise StripeGatewayError(
            "Stripe is not configured on this deploy — set STRIPE_SECRET_KEY "
            "and STRIPE_WEBHOOK_SECRET before calling create_checkout_session()."
        )
    pack = CREDIT_PACKS[pack_key]
    return stripe.checkout.Session.create(
        api_key=settings.STRIPE_SECRET_KEY,
        stripe_version=STRIPE_API_VERSION,
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": pack["price_cents"],
                    "product_data": {"name": f"Coverage credits — {pack['label']}"},
                },
                "quantity": 1,
            }
        ],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"user_id": str(user.id), "pack_key": pack_key},
    )


def handle_webhook_event(payload: bytes, sig_header: str) -> None:
    """Verifies `payload`/`sig_header` against `STRIPE_WEBHOOK_SECRET`
    (`stripe.Webhook.construct_event` — Stripe's own signature check, not a
    hand-rolled one) and, on a PAID checkout session, grants the purchased
    credits.

    Raises `StripeGatewayError` on a signature that doesn't verify or a
    payload that isn't valid JSON — the view maps that to a 400, which is
    what tells Stripe's retry logic this delivery was rejected rather than
    silently accepted.

    Idempotent by construction: `ProcessedStripeEvent` records `event.id`
    inside the same `atomic()` block the grant is written in, and a
    duplicate delivery hits that row's unique constraint and returns
    without granting again — see this module's docstring for why a plain,
    non-tenant model is the right shape for that record.

    An unpaid session returns WITHOUT recording the event as processed. That
    asymmetry is deliberate and is the one place this function's idempotency
    rule bends: a delayed payment method's `completed` delivery and its later
    `async_payment_succeeded` delivery are two DIFFERENT event ids, so
    marking the unpaid one processed would not block the paid one anyway —
    but leaving it unrecorded also means a redelivery of the unpaid event
    (Stripe retries a 5xx) re-evaluates `payment_status` fresh rather than
    being short-circuited by a decision taken when the money had not arrived.
    """
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise StripeGatewayError(f"invalid Stripe webhook payload/signature: {exc}") from exc

    # The version the ENDPOINT was registered with, which is a Dashboard
    # setting this code cannot set — only report on. A warning, never a
    # rejection: dropping a real payment because someone created the
    # endpoint on a different version would be a worse failure than reading
    # a slightly older object shape. See docs/deploy.md §8.
    event_api_version = event.get("api_version")
    if event_api_version and event_api_version != STRIPE_API_VERSION:
        logger.warning(
            "Stripe webhook endpoint is on API version %s; this app is pinned "
            "to %s — re-create the endpoint on the pinned version.",
            event_api_version, STRIPE_API_VERSION,
        )

    if event["type"] not in GRANTING_EVENT_TYPES:
        return

    session = event["data"]["object"]
    # THE MONEY CHECK. `completed` means the customer finished the form, not
    # that the payment settled — see the module docstring. Without this, a
    # bank-debit checkout grants credits at form-submit time and the funds
    # may never arrive.
    if session.get("payment_status") != "paid":
        logger.info(
            "Stripe checkout session %s is %r, not paid — no credits granted.",
            session.get("id"), session.get("payment_status"),
        )
        return

    metadata = session.get("metadata") or {}
    user_id = metadata.get("user_id")
    pack_key = metadata.get("pack_key")
    # `int(user_id)` raises `ValueError` on anything non-numeric, and even a
    # well-formed id can legitimately name nobody: Stripe's delivery isn't
    # instantaneous, and a student can hard-delete their Coverage account
    # (accounts/services.py::delete_user_and_data) in the window between
    # starting checkout and this webhook arriving. Both are "nothing to
    # grant, nothing to retry" — the same shape as an unrecognised pack_key
    # below — not a crash: an unhandled exception here would 500, and
    # because `ProcessedStripeEvent` is written inside the SAME atomic block
    # as the grant, the 500 would roll that row back too, so Stripe's
    # automatic retry would hit this exact path and 500 again, forever.
    user = None
    if user_id:
        try:
            user = get_user_model().objects.filter(pk=int(user_id)).first()
        except (TypeError, ValueError):
            user = None
    if user is None or pack_key not in CREDIT_PACKS:
        # A checkout session Stripe completed that this app didn't create
        # the metadata for (a stray test event, a pack retired since the
        # session was opened, an account deleted mid-flight). Nothing to
        # grant, and nothing to retry — recording it as processed stops it
        # being reconsidered forever.
        _mark_processed(event["id"])
        return

    with transaction.atomic():
        _, created = ProcessedStripeEvent.objects.get_or_create(
            stripe_event_id=event["id"]
        )
        if not created:
            # Already handled by an earlier delivery of this same event —
            # the idempotency guarantee this whole function exists for.
            return
        billing_credits.grant_purchase(user, pack_key, event["id"])


def _mark_processed(stripe_event_id: str) -> None:
    with transaction.atomic():
        ProcessedStripeEvent.objects.get_or_create(stripe_event_id=stripe_event_id)
