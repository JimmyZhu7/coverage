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
from django.utils import timezone
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


@login_required
@require_POST
def gmail_rescan(request):
    """"Scan Now" — a user-triggered, repeatable re-check of Gmail against
    ALL of the user's contacts. Distinct from the one-time automatic
    first-connect backfill above: this only QUEUES the work
    (`rescan_status="pending"`); the same `gmail_backfill` cron tick that
    already polls for pending first-connect backfills also picks up a
    pending rescan and runs `gmail_live.run_rescan` — see that command's
    docstring for why this stays queue-and-poll rather than running inline
    here (a year of per-contact Gmail searches, now plus a capped AI pass,
    is not something a POST's response can wait on).

    Refuses to queue a second rescan while one is already `pending` or
    `running`, so a student clicking the button five times in a row can't
    stack five runs — the Settings page also disables the button in that
    state, this is the server-side guarantee behind it.
    """
    connection = GmailConnection.all_objects.filter(
        user=request.user, status="active"
    ).first()
    if connection is None:
        messages.error(request, "Connect Gmail before running a scan.")
        return redirect(f"{reverse('accounts:settings')}#gmail-live")
    if connection.rescan_status in ("pending", "running"):
        messages.info(request, "A scan is already in progress.")
        return redirect(f"{reverse('accounts:settings')}#gmail-live")

    connection.rescan_status = "pending"
    connection.rescan_requested_at = timezone.now()
    connection.save(update_fields=["rescan_status", "rescan_requested_at"])
    messages.success(request, "Scan queued — check back in a few minutes.")
    return redirect(f"{reverse('accounts:settings')}#gmail-live")
