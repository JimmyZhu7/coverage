"""Template context shared across every page.

`social_providers` exposes the sign-in provider list to the auth templates so
the markup never hard-codes which providers exist. A provider whose client_id
is unset renders as a visibly disabled button rather than a live one that
would dead-end at the OAuth redirect: the design is complete the moment
credentials land, and honest until then.
"""

from __future__ import annotations

from django.conf import settings

# id -> (label, provider_login_url id). The order here is the display order.
_PROVIDERS: list[tuple[str, str]] = [
    ("google", "Google"),
    ("apple", "Apple"),
    ("microsoft", "Microsoft"),
    ("linkedin_oauth2", "LinkedIn"),
]


def social_providers(request):
    configured = set(getattr(settings, "ENABLED_SOCIAL_PROVIDERS", []))
    return {
        "social_providers": [
            {"id": pid, "label": label, "configured": pid in configured}
            for pid, label in _PROVIDERS
        ]
    }
