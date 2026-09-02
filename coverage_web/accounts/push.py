"""Web Push sending — the one place `pywebpush.webpush()` gets called from.

WHY THIS IS ITS OWN MODULE, NOT INLINE IN THE MANAGEMENT COMMAND. Same
reasoning as `crm/digest.py`'s split from `send_weekly_digest.py`: assembly
(who gets alerted about what — `send_deadline_push_alerts`) stays separate
from mechanism (how one push actually gets encrypted, signed, and posted —
here), so either can be read, tested, or replaced without touching the
other. `accounts.views.push_subscribe` also imports `is_configured()`, so a
shared module is the only way both callers agree on what "configured" means.

VAPID identifies this server to the browsers' own push relays (Chrome/
Firefox/Safari's infrastructure) — not a third-party push SaaS, and not
billed per-message. `pywebpush` does the real cryptography (AES128GCM
payload encryption per RFC 8291, VAPID JWT signing per RFC 8292) rather than
this module hand-rolling either; see coverage_web/pyproject.toml for why
that line was drawn.

Blank `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` (the default — see
settings/base.py) means `is_configured()` is False and every send call
below is a deliberate no-op, exactly the posture `directory.ai_extract`
already established for `ANTHROPIC_API_KEY`: the app boots, every test
passes, and the feature activates the moment real keys land, no code change
either way.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from django.conf import settings

from pywebpush import WebPushException, webpush

# ---------------------------------------------------------------------------
# The push services a real browser subscription can name.
#
# WHY AN ALLOWLIST AND NOT A FORMAT CHECK. `push_subscribe` stores whatever
# `endpoint` a POST hands it, and `send_deadline_push_alerts` then makes a
# VAPID-signed POST to that URL every day, from inside the deploy, with no
# human in the loop. Without this an authenticated student could point the
# nightly cron at any host reachable from the server — a cloud metadata
# endpoint, an internal admin port, an attacker's collector — and read the
# outcome by watching whether the row survived. That is a blind SSRF with a
# scheduler attached, and it was reachable from an ordinary account (probed
# 2026-09-01: `{"endpoint": "https://example.invalid/x"}` answered 201).
#
# A browser never invents this host. The Push API's endpoint always names
# the browser vendor's own relay, and there are four of them:
#   - Chrome and every Chromium browser: FCM. `android.googleapis.com` is
#     the legacy GCM host older Chrome builds still mint against; both are
#     Google's, both are real, so both are listed rather than guessing at a
#     minimum Chrome version.
#   - Firefox: Mozilla's autopush.
#   - Safari (macOS and iOS): Apple's.
#   - Edge on Windows: WNS, which is the one that is NOT a fixed host —
#     it hands out a per-region subdomain (e.g. `sin.notify.windows.com`),
#     so it is matched as a suffix. The suffix is anchored on a leading dot
#     so `evil-notify.windows.com` and `notify.windows.com.attacker.test`
#     both fail.
PUSH_SERVICE_HOSTS = frozenset({
    "fcm.googleapis.com",
    "android.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
})
PUSH_SERVICE_HOST_SUFFIXES = (".notify.windows.com",)


def is_allowed_endpoint(endpoint: str) -> bool:
    """True when `endpoint` names a real browser push service over HTTPS.

    Scheme is checked too, and only `https` passes: an `http://` endpoint
    would send a VAPID assertion in clear text, and no push service offers
    one. `urlsplit().hostname` is used rather than `netloc` because it drops
    any `user:pass@` prefix and any `:port`, which are exactly the two ways
    a crafted URL tries to make a hostname check read the wrong substring.
    """
    if not endpoint:
        return False
    parts = urlsplit(endpoint)
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    if host in PUSH_SERVICE_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in PUSH_SERVICE_HOST_SUFFIXES)


class SubscriptionExpired(Exception):
    """The push service reports this subscription is gone (404/410) — the
    documented Web Push contract for "the browser unsubscribed, uninstalled,
    or the token rotated past recovery." Caller's job, not this module's: a
    subscription is a row in someone else's table (`accounts.models.
    PushSubscription`), and deleting it is a decision the caller (the
    management command) should make visibly, not a side effect buried in a
    send function."""


def is_configured() -> bool:
    return bool(
        getattr(settings, "VAPID_PUBLIC_KEY", "")
        and getattr(settings, "VAPID_PRIVATE_KEY", "")
    )


def send_notification(subscription, *, title: str, body: str, url: str) -> None:
    """Send one push message to one subscription.

    `subscription` is anything with `.endpoint`, `.p256dh`, `.auth` —
    `accounts.models.PushSubscription` in production, a lightweight stand-in
    in tests. Raises `SubscriptionExpired` on a 404/410 (delete-and-move-on,
    per the caller's contract) and re-raises any other `WebPushException`
    (a transient failure — logged and skipped by the caller, never treated
    as "this subscription is dead").

    No-ops silently when VAPID isn't configured — callers that reach this
    function already checked `is_configured()` themselves (the management
    command exits before finding any subscriptions to loop over), so this
    guard exists only to keep an accidental call from a future call site
    from raising a confusing crypto error instead of doing nothing.
    """
    if not is_configured():
        return

    payload = json.dumps({"title": title, "body": body, "url": url})
    claim_email = (getattr(settings, "VAPID_CLAIM_EMAIL", "") or "").strip()

    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=payload,
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{claim_email}" if claim_email else "mailto:"},
        )
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            raise SubscriptionExpired from exc
        raise
