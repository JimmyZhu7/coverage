"""The email page is a CHANGE flow, not an address collection.

`ACCOUNT_CHANGE_EMAIL = True` turns allauth's account_email view from its
default multi-address manager (a mental model no consumer product ships)
into "your email, and a verified flow to replace it": add a new address,
verify it, and it atomically replaces the old one — which keeps working
until that confirmation. The settings card's "Manage Email" button points
at this page, so its behaviour is part of the Settings surface.

The spec flagged exactly one risk to smoke-test: the flow's interaction
with users who have no usable password. Both shapes are covered below.
"""

from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


def _user(email="change-me@example.com", *, password="x" * 14, verified=True):
    user = get_user_model().objects.create_user(email=email, password=password)
    EmailAddress.objects.create(
        user=user, email=email, primary=True, verified=verified
    )
    return user


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def test_the_flow_is_on() -> None:
    from django.conf import settings

    assert settings.ACCOUNT_CHANGE_EMAIL is True
    assert settings.ACCOUNT_MAX_EMAIL_ADDRESSES == 2


def test_adding_an_address_stages_a_replacement_not_a_collection():
    """The old address stays primary and usable; the new one waits, unverified.
    Nothing about this is an inventory of addresses."""
    user = _user()
    c = _client(user)

    resp = c.post(
        reverse("account_email"),
        {"action_add": "", "email": "new-addr@example.com"},
    )
    assert resp.status_code in (200, 302)

    rows = EmailAddress.objects.filter(user=user).order_by("-primary")
    assert rows.count() == 2
    old, new = rows[0], rows[1]
    assert old.email == "change-me@example.com" and old.primary and old.verified
    assert new.email == "new-addr@example.com" and not new.primary and not new.verified


def test_verifying_the_new_address_completes_the_swap():
    """The atomic half of the promise: confirmation makes the new address
    primary and REMOVES the old one — allauth's change-email contract."""
    from allauth.account.internal.flows.email_verification import verify_email

    user = _user()
    c = _client(user)
    c.post(reverse("account_email"), {"action_add": "", "email": "new-addr@example.com"})

    new = EmailAddress.objects.get(user=user, email="new-addr@example.com")

    # Verify through allauth's own flow (the link in the mail ends up here),
    # with a real request so signals that expect one get one.
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = user
    request.session = c.session
    from django.contrib.messages.storage.fallback import FallbackStorage

    request._messages = FallbackStorage(request)
    verify_email(request, new)

    rows = EmailAddress.objects.filter(user=user)
    assert rows.count() == 1
    final = rows.get()
    assert final.email == "new-addr@example.com"
    assert final.primary and final.verified


def test_a_passwordless_user_can_still_reach_the_page():
    """The spec's flagged uncertainty: a Google-only user has no usable
    password. The page must render for them, not 500 or redirect into a
    password wall."""
    user = get_user_model().objects.create_user(email="social-only@example.com")
    user.set_unusable_password()
    user.save()
    EmailAddress.objects.create(
        user=user, email="social-only@example.com", primary=True, verified=True
    )

    resp = _client(user).get(reverse("account_email"))
    assert resp.status_code == 200
