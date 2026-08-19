"""billing views — the two HTTP entry points for pay-as-you-go credit
top-ups (billing/stripe_gateway.py). See that module's docstring for the
`is_configured()` gate both of these respect.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from analytics.events import record_event

from . import stripe_gateway
from .models import ProWaitlist


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


# ---------------------------------------------------------------------------
# Pro waitlist (billing/models.py::ProWaitlist) — the pricing page's
# "Notify me when Pro opens" control.
# ---------------------------------------------------------------------------

# Same shape as core.views._search_throttled: a cache-based, per-IP burst
# guard, no new table. A "notify me" form is reachable signed-out, so a
# script hammering it is the one thing worth bounding — a real visitor never
# submits this more than once.
_WAITLIST_WINDOW_SECONDS = 60
_WAITLIST_WINDOW_LIMIT = 5


def _waitlist_throttled(request) -> bool:
    ip = (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
          or request.META.get("REMOTE_ADDR", "unknown"))
    key = f"waitlist-rate:{ip}"
    burst = cache.get_or_set(key, 0, _WAITLIST_WINDOW_SECONDS)
    if burst >= _WAITLIST_WINDOW_LIMIT:
        return True
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, _WAITLIST_WINDOW_SECONDS)
    return False


@require_POST
def waitlist_join(request):
    """"Notify me when Pro opens" (templates/core/pricing.html's Pro card).
    Open to signed-out visitors — the email field IS the account for
    someone who hasn't signed up yet — so this is deliberately not
    `@login_required`, unlike every other billing view in this module.

    Writes one `ProWaitlist` row (deduped by email — `get_or_create` turns a
    repeat join into a no-op read, not an IntegrityError) and a
    `pro_waitlist_joined` product event, then bounces back to Pricing with a
    plain confirmation. No email is sent from here — see ProWaitlist's own
    docstring on why.
    """
    pricing_url = reverse("core:pricing")

    if _waitlist_throttled(request):
        messages.error(request, "Too many requests — try again in a minute.")
        return redirect(pricing_url)

    raw_email = (request.POST.get("email") or "").strip()
    if not raw_email and request.user.is_authenticated:
        raw_email = request.user.email
    email = raw_email.lower()

    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Enter a valid email to join the waitlist.")
        return redirect(pricing_url)

    user = request.user if request.user.is_authenticated else None
    _, created = ProWaitlist.all_objects.get_or_create(
        email=email, defaults={"user": user, "source": "pricing_page"},
    )
    if created:
        record_event("pro_waitlist_joined", user=user, source="pricing_page")
        messages.success(request, "You're on the list. We'll let you know when Pro opens.")
    else:
        messages.info(request, "You're already on the list.")
    return redirect(pricing_url)
