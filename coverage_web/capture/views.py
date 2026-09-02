"""Capture app views — Gmail Live (docs/build-plan.md §5's "v2"; see
capture/gmail_live.py). This is a SEPARATE, incremental OAuth consent — the
login flow never touches these views or that client's credentials.

The BCC/forward inbound-email webhook (§5's v1) was retired 2026-08-19 once
Gmail Live made it redundant — a real, connected Gmail account needs no
habit change (BCC/forward) to get the same touches logged.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from analytics.events import record_event
from capture import gmail_live, google_revoke
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

    # The product's magic moment left no trace in the event stream. ~70
    # `record_event` call sites existed and not one of them fired here, so the
    # single step that turns Coverage from an empty CRM into a populated one
    # was the only part of the funnel that could not be counted: a connection
    # was discoverable as a GmailConnection row, but "when did they connect,
    # and how long after signing up" was unanswerable.
    #
    # Recorded HERE, after the exchange succeeded and the row exists — not on
    # the redirect out to Google in `gmail_connect`, which is a click on a
    # button, not a connection, and which a cancelled consent screen would
    # otherwise record as a success. No address in the props: the event stream
    # is read on a staff page and a mailbox address is not a metric.
    record_event(
        "gmail_connected",
        user=request.user,
        plan=request.user.plan,
        realtime=connection.watch_expiration is not None,
    )
    messages.success(request, f"Gmail connected: {connection.gmail_address}.")
    if connection.watch_expiration is None:
        if request.user.plan == "pro":
            # connect_gmail stored a perfectly good connection but could not
            # register the Pub/Sub watch (see its comment on why that is not
            # fatal). Everything except real-time push still works, and the
            # daily gmail_watch_renew retries on its own — but saying so
            # beats letting the user wonder why nothing arrives instantly.
            messages.warning(
                request,
                "Real-time updates aren't active yet — Coverage will keep "
                "retrying. Your historical scan will still run.",
            )
        else:
            # Expected, not a failure: real-time sync is Pro-only
            # (docs/pricing-rebalance-plan.md §7) and `connect_gmail` never
            # even attempts `register_watch` for a Free plan, so there is
            # nothing here to retry. A plain info note, not the "still
            # retrying" warning above, which would wrongly imply this fixes
            # itself.
            messages.info(
                request,
                "Real-time sync is a Pro feature. Your historical scan will "
                "still run, and Scan Now works anytime on any plan.",
            )
    return redirect(f"{reverse('accounts:settings')}#gmail-live")


@login_required
@require_POST
def gmail_disconnect(request):
    """Hands the grant back to Google, then deletes the stored connection.

    This used to delete the row and stop there, on the reasoning that
    myaccount.google.com/permissions is a better place to revoke and a
    second call is a second thing that can fail silently. True about the
    control; wrong about the promise. A button labelled "Disconnect" that
    leaves a live grant on Google's side is telling the student something
    that is not so, and the grant then outlives every trace of it in this
    app. `capture/google_revoke.py` is best-effort and never raises, so the
    row still goes either way and that Google control is still there —
    it is the backstop now, not the only path.
    """
    for connection in GmailConnection.all_objects.filter(user=request.user):
        google_revoke.revoke_connection(connection)
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
    connection = GmailConnection.all_objects.select_related("user").filter(
        user=request.user, status="active"
    ).first()
    if connection is None:
        messages.error(request, "Connect Gmail before running a scan.")
        return redirect(f"{reverse('accounts:settings')}#gmail-live")
    if connection.rescan_status in ("pending", "running"):
        messages.info(request, "A scan is already in progress.")
        return redirect(f"{reverse('accounts:settings')}#gmail-live")

    unlocks_at = gmail_live.free_rescan_unlocks_at(connection)
    if unlocks_at is not None:
        messages.error(
            request,
            "Free plan: one scan every "
            f"{settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS} days. Next scan "
            f"available {timezone.localtime(unlocks_at):%-d %b}. Pro scans any time.",
        )
        return redirect(f"{reverse('accounts:settings')}#gmail-live")

    connection.rescan_status = "pending"
    connection.rescan_requested_at = timezone.now()
    connection.save(update_fields=["rescan_status", "rescan_requested_at"])
    messages.success(request, "Scan queued — check back in a few minutes.")
    return redirect(f"{reverse('accounts:settings')}#gmail-live")
