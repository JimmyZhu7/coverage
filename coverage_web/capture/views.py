"""Capture app views (docs/build-plan.md §5).

- ``inbound``  — the Postmark-style inbound-email webhook (POST, CSRF-exempt,
  shared-secret authenticated). The one untrusted entry point; everything it
  accepts is treated as data.
- ``health``   — the "is my capture working?" strip (§7 M3 DoD / risk 3).
- ``review``   — the ``needs_review`` one-click confirmation queue.
- ``confirm`` / ``ignore`` — actions on a queued event.
"""

from __future__ import annotations

import hmac
import json
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from django.conf import settings

from capture import gmail_live, services
from capture.models import GmailConnection
from crm.models import CaptureEvent


def _authenticate(request) -> bool:
    """Constant-time check of the shared secret. Accepted either as the
    ``X-Capture-Token`` header (preferred — keeps the secret out of access
    logs) or a ``?token=`` query parameter (documented fallback for webhook
    consoles that can't set custom headers)."""
    expected = settings.CAPTURE_INBOUND_SECRET
    provided = request.headers.get("X-Capture-Token") or request.GET.get("token", "")
    if not provided or not expected:
        return False
    # Compare as bytes: str compare_digest raises TypeError on non-ASCII
    # input, turning a garbage token into an unauthenticated 500.
    return hmac.compare_digest(
        str(provided).encode("utf-8"), str(expected).encode("utf-8")
    )


@csrf_exempt
@require_POST
def inbound(request):
    """Inbound-email webhook. Authenticates with ``CAPTURE_INBOUND_SECRET``,
    parses a Postmark-style JSON body, and runs the deterministic pipeline."""
    if not _authenticate(request):
        return JsonResponse({"status": "unauthorized"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"status": "bad_request", "reason": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"status": "bad_request", "reason": "expected_object"}, status=400)

    result = services.ingest_inbound_email(payload)
    return JsonResponse({"status": result.status, **result.detail}, status=result.http_status)


@login_required
def health(request):
    """Capture-health strip for the signed-in user."""
    context = services.capture_health(request.user)
    return render(request, "capture/health.html", context)


@login_required
def review(request):
    """The needs_review confirmation queue."""
    events = services.needs_review_events(request.user)
    context = {
        "events": events,
        "kinds": ["reply_received", "chat_scheduled", "chat", "outreach"],
    }
    return render(request, "capture/review_queue.html", context)


@login_required
@require_POST
def confirm(request, event_id: int):
    """One-click confirm: apply a chosen touch kind to a queued event."""
    kind = request.POST.get("touch_kind", "reply_received")
    try:
        services.confirm_event(request.user, event_id, kind)
    except (
        CaptureEvent.DoesNotExist,
        ValueError,
        services.AmbiguousContactError,
        services.UnidentifiableContactError,
    ):
        # Stale/foreign id, bad kind, (still) an ambiguous name match, or an
        # event that names nobody to log against (a forward, typically) —
        # the event stays needs_review either way. Fall through to a fresh
        # queue rather than surface an error for what the review queue
        # itself exists to handle. Confirming a forward is the one case the
        # queue can't yet resolve on its own: the user has to add or pick the
        # contact first, then log the touch from the contact page.
        pass
    return redirect(reverse("capture:review"))


@login_required
@require_POST
def ignore(request, event_id: int):
    """Dismiss a queued event without logging a touch."""
    try:
        services.ignore_event(request.user, event_id)
    except CaptureEvent.DoesNotExist:
        pass
    return redirect(reverse("capture:review"))


# ---------------------------------------------------------------------------
# Gmail Live (docs/build-plan.md §5's "v2" — see capture/gmail_live.py).
# Unlike everything above, this is a SEPARATE, incremental OAuth consent —
# the login flow never touches these views or that client's credentials.
# ---------------------------------------------------------------------------

_STATE_SESSION_KEY = "gmail_live_oauth_state"


@login_required
def gmail_connect(request):
    """Redirects to Google's consent screen for the Gmail-read scope, using
    the SEPARATE Gmail Live OAuth client (never the login one)."""
    if not gmail_live.is_configured():
        raise Http404("Gmail Live is not configured on this deploy.")
    redirect_uri = request.build_absolute_uri(reverse("capture:gmail_callback"))
    state = secrets.token_urlsafe(24)
    request.session[_STATE_SESSION_KEY] = state
    return redirect(gmail_live.build_auth_url(redirect_uri, state))


@login_required
def gmail_callback(request):
    """Google's redirect back after consent (or a denial/error)."""
    if not gmail_live.is_configured():
        raise Http404("Gmail Live is not configured on this deploy.")

    expected_state = request.session.pop(_STATE_SESSION_KEY, None)
    if not expected_state or request.GET.get("state") != expected_state:
        messages.error(request, "Gmail connect request expired — try again.")
        return redirect(f"{reverse('accounts:settings')}#gmail-live")

    if request.GET.get("error"):
        # The user hit "Cancel" on Google's consent screen, or Google itself
        # errored. Either way there is nothing to exchange.
        messages.error(request, "Gmail connect was cancelled.")
        return redirect(f"{reverse('accounts:settings')}#gmail-live")

    code = request.GET.get("code", "")
    redirect_uri = request.build_absolute_uri(reverse("capture:gmail_callback"))
    try:
        connection = gmail_live.connect_gmail(request.user, code, redirect_uri)
    except gmail_live.GmailLiveError as exc:
        messages.error(request, str(exc))
        return redirect(f"{reverse('accounts:settings')}#gmail-live")

    messages.success(request, f"Gmail connected: {connection.gmail_address}.")
    return redirect(f"{reverse('accounts:settings')}#gmail-live")


@login_required
@require_POST
def gmail_disconnect(request):
    """Deletes the stored connection. Does NOT call Google's revoke
    endpoint — the user already has a direct, more legible way to do that
    (https://myaccount.google.com/permissions), and duplicating it here
    would just be a second place for that call to silently fail."""
    GmailConnection.all_objects.filter(user=request.user).delete()
    messages.success(request, "Gmail disconnected.")
    return redirect(f"{reverse('accounts:settings')}#gmail-live")
