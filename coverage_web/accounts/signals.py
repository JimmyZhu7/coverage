"""Funnel instrumentation for the top of the onboarding funnel (§8).

`signup` is the first canonical product event (docs/build-plan.md §8's
event list). It fires from allauth's `user_signed_up` signal — the single
point every new account passes through, whether it arrives via Google
sign-in or the email/password form — so the funnel's first step is a query,
not a guess. `onboarded` and `import_completed` are recorded from the
onboarding/import views themselves (see views.py / services.py).
"""

from __future__ import annotations

from allauth.account.signals import user_signed_up
from django.dispatch import receiver

from analytics.events import record_event


@receiver(user_signed_up)
def record_signup(sender, request, user, **kwargs):
    record_event("signup", user=user)
