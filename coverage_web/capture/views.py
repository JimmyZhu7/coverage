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

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from django.conf import settings

from capture import services
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
    return hmac.compare_digest(str(provided), str(expected))


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
    except (CaptureEvent.DoesNotExist, ValueError):
        pass  # stale/foreign id or bad kind — fall through to a fresh queue
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
