"""One definition of "which client is this request from", for throttling.

WHY THIS EXISTS. `core.views._search_throttled` and
`billing.views._waitlist_throttled` each built their own throttle key out of
`X-Forwarded-For`'s FIRST hop. That hop is whatever the client typed. Nothing
between the client and Django overwrites it: Render appends, it does not
replace, and locally there is no proxy at all. So a script wanting to defeat
either throttle only had to send a different `X-Forwarded-For` per request and
it got a fresh window every time, which is a throttle that stops exactly the
traffic it does not need to stop (`audit-security.md` finding 10).

THE RULE. Trust only hops our own infrastructure appended. `X-Forwarded-For`
grows left to right, each proxy appending the peer it actually saw, so the
RIGHTMOST entries are the trustworthy ones and the leftmost is the client's
free text. `TRUSTED_PROXY_HOPS` says how many proxies sit in front of Django;
the address that interests us is the one the innermost trusted proxy saw, i.e.
the Nth entry from the right.

The default is 0, which means "no proxy in front of me, so `X-Forwarded-For`
is entirely untrusted" and the key comes from `REMOTE_ADDR` alone. That is
correct for `runserver`, correct for the test client, and correct for any
deployment that has not declared its edge — a deployment that guesses at a
proxy count it does not have is back to trusting the client. Render sets it to
1 via the environment (`render.yaml`).

Deliberately NOT reused for django-axes: axes resolves its own IP through
django-ipware and has its own proxy settings, and P5's "one definition per
fact" is about one definition per fact we own, not about reaching into a
dependency's.
"""

from __future__ import annotations

from django.conf import settings

# What a request with no usable address is keyed under. A single shared bucket
# is the safe direction: unattributable traffic throttles together rather than
# each getting its own free window.
UNKNOWN = "unknown"


def client_ip(request) -> str:
    """The address to throttle this request under.

    Never returns attacker-controlled text unless the deployment has declared
    trusted proxies that are not actually there.
    """
    hops = getattr(settings, "TRUSTED_PROXY_HOPS", 0) or 0
    if hops > 0:
        forwarded = [
            part.strip()
            for part in (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")
            if part.strip()
        ]
        # Fewer entries than declared proxies means the header was stripped or
        # the count is wrong; either way the leftmost entry is not safe to
        # read, so fall through to REMOTE_ADDR rather than guess.
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return (request.META.get("REMOTE_ADDR") or "").strip() or UNKNOWN
