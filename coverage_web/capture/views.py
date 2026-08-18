"""Capture app views — Gmail Live (docs/build-plan.md §5's "v2"; see
capture/gmail_live.py). This is a SEPARATE, incremental OAuth consent — the
login flow never touches these views or that client's credentials.

The BCC/forward inbound-email webhook (§5's v1) was retired 2026-08-19 once
Gmail Live made it redundant — a real, connected Gmail account needs no
habit change (BCC/forward) to get the same touches logged.
"""

from __future__ import annotations

import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from capture import gmail_live
from capture.models import GmailConnection

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
