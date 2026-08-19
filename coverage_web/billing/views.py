"""billing views — the two HTTP entry points for pay-as-you-go credit
top-ups (billing/stripe_gateway.py). See that module's docstring for the
`is_configured()` gate both of these respect.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import stripe_gateway


@login_required
@require_POST
def checkout(request, pack_key: str):
    """Starts a Stripe Checkout Session for one credit pack and redirects
    the browser to it. Not is_configured()-gated to a silent no-op like a
    GET-style feature (the Settings page already hides the buttons behind
    that gate) — a POST that reaches here despite the gate (a stale tab, a
    hand-crafted request) gets a clean message-and-redirect back to
    Settings instead of a 500, per docs/gmail-live-setup.md's "no-op
    cleanly everywhere" posture applied to a POST instead of a GET.
    """
    settings_url = reverse("accounts:settings") + "#credits"
    if not stripe_gateway.is_configured():
        messages.info(request, "Credit top-ups aren't available yet.")
        return redirect(settings_url)
    if pack_key not in stripe_gateway.CREDIT_PACKS:
        messages.error(request, "That credit pack doesn't exist.")
        return redirect(settings_url)

    success_url = request.build_absolute_uri(settings_url)
    cancel_url = request.build_absolute_uri(settings_url)
    session = stripe_gateway.create_checkout_session(
        request.user, pack_key, success_url=success_url, cancel_url=cancel_url
    )
    return redirect(session.url)


@csrf_exempt
@require_POST
def webhook(request):
    """Stripe calls this server-to-server — no login, no CSRF token to
    check (Stripe's own signature verification, inside
    `stripe_gateway.handle_webhook_event`, is the authentication that
    matters here, the same way a bearer-token API endpoint would replace
    session auth). Never gated on `is_configured()` the way the checkout
    view's redirect is: if Stripe isn't configured there is no webhook
    secret to verify against, so this simply can't verify anything and
    400s — the correct response either way, and one Stripe will never
    actually trigger, since Jimmy hasn't registered a webhook URL with
    Stripe yet.
    """
    if not stripe_gateway.is_configured():
        return HttpResponseBadRequest("Stripe is not configured on this deploy.")
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        stripe_gateway.handle_webhook_event(request.body, sig_header)
    except stripe_gateway.StripeGatewayError as exc:
        return HttpResponseBadRequest(str(exc))
    return HttpResponse(status=200)
