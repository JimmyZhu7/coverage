"""Signup says out loud what signup already does (D-4, 2026-09-02).

`ACCOUNT_EMAIL_VERIFICATION` is "optional" and does not change here. That
flip is paired, in one commit, with setting `EMAIL_URL` to a real provider
(the comment at the setting spells the pairing out and
`core/tests/test_production_settings.py` guards it), because turning it on
first locks every new account out of the product.

What ships without the provider is the half that is honest either way.
allauth really does send a confirmation mail on signup today. Before this,
nothing in the UI mentioned it, the address stayed unverified forever, and
the one page that showed the state was /accounts/email/, which the student
had no route to from the wizard they land in.

The load-bearing case is the LAST one: a user with no EmailAddress row at
all — createsuperuser, a fixture, the cutover import — was never sent
anything, so telling them to go and check their inbox would be the product
stating a fact it cannot source (P1).
"""

from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

User = get_user_model()
WIZARD = "accounts:onboarding"

pytestmark = pytest.mark.django_db


def _newcomer(email="newcomer@example.com", *, verified=False, row=True, primary=True):
    user = User.objects.create_user(email=email, password="pw12345!")
    if row:
        EmailAddress.objects.create(
            user=user, email=email, primary=primary, verified=verified
        )
    return user


def _wizard(client, user) -> str:
    client.force_login(user)
    return client.get(f"{reverse(WIZARD)}?step=profile").content.decode()


def test_the_wizard_says_a_confirmation_mail_went_out_and_names_the_address():
    user = _newcomer()

    body = _wizard(Client(), user)

    assert "newcomer@example.com" in body
    assert "confirmation link" in body
    assert "not verified yet" in body


def test_the_wizard_links_to_the_page_that_owns_the_resend():
    """The resend lives on /accounts/email/, which is allauth's own view and
    the one definition of that action (P5). The fix here is the route to it,
    not a second endpoint that does the same thing."""
    user = _newcomer()

    body = _wizard(Client(), user)

    assert reverse("account_email") in body
    assert "Resend or change it" in body


def test_a_verified_address_says_nothing():
    user = _newcomer(email="done@example.com", verified=True)

    body = _wizard(Client(), user)

    assert "not verified yet" not in body


def test_an_account_that_was_never_sent_a_mail_is_never_told_to_check_for_one():
    """No EmailAddress row means allauth's signup flow never ran for this
    account and no confirmation was ever posted. P1: a fact the product
    cannot source is left unsaid, not softened."""
    user = _newcomer(email="imported@example.com", row=False)

    body = _wizard(Client(), user)

    assert "not verified yet" not in body
    assert "confirmation link" not in body


def test_the_email_page_offers_a_resend_for_an_unverified_signup_address():
    """`ACCOUNT_CHANGE_EMAIL = True` renders email_change.html, whose resend
    control sits in its pending-address branch — and a freshly signed-up
    address IS that branch (allauth's `EmailAddress.objects.get_new` is any
    unverified row). Pinned because the whole affordance depends on a shape
    the template never had to handle before: pending with no verified
    address behind it."""
    user = _newcomer(email="pending@example.com")
    client = Client()
    client.force_login(user)

    body = client.get(reverse("account_email")).content.decode()

    assert "Re-send Verification" in body
    assert "pending@example.com" in body


def test_the_resend_button_actually_sends_another_confirmation():
    from django.core import mail

    user = _newcomer(email="resend@example.com")
    client = Client()
    client.force_login(user)
    mail.outbox.clear()

    response = client.post(
        reverse("account_email"), {"action_send": "", "email": "resend@example.com"}
    )

    assert response.status_code in (200, 302)
    assert len(mail.outbox) == 1
    assert "resend@example.com" in mail.outbox[0].to


def test_a_row_allauth_never_marked_primary_still_counts_as_this_address():
    """Measured on the live seeded demo account, 2026-09-02: its EmailAddress
    row carries `primary=False`, so a lookup keyed on `primary=True` alone
    reads it as "no row" and says nothing about an address that really is
    unverified. Both this line and the Settings card's verified badge read
    `_account_email_row`, which falls back to the address itself (P5)."""
    user = _newcomer(email="seeded@example.com", primary=False)

    body = _wizard(Client(), user)

    assert "seeded@example.com" in body
    assert "not verified yet" in body


def test_verification_is_still_optional_because_the_provider_is_still_missing():
    """The half that did NOT ship, pinned so a later pass cannot flip it on
    its own. Mandatory verification without outbound mail is an account that
    can sign up and then never sign in."""
    from django.conf import settings

    assert settings.ACCOUNT_EMAIL_VERIFICATION == "optional"
