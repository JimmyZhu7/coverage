"""Tests for the Sign-In & Security section and the pages it links to
(docs/specs/settings-page.md audit #1, B3, and the sign-out-everywhere item).

What was broken: allauth's password-change, email-management and connected-
accounts routes have always WORKED, but nothing overrode their templates, so
they rendered allauth's raw defaults — no Coverage shell, black text on the
dark background, a bare "Menu:" list. And nothing on the Settings page linked
to any of them. A product's most basic account controls existed as
unreachable, broken-looking pages.

The through-line of the assertions below is honesty rule D6: every row must
reflect THIS account. "Change password" only where a usable password exists;
a connections row only where a provider is configured; a verified badge only
from `EmailAddress.verified`. A row that describes an account the user doesn't
have is worse than no row.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts import services

User = get_user_model()

pytestmark = pytest.mark.django_db

SETTINGS = "accounts:settings"


@pytest.fixture
def student():
    return User.objects.create_user(email="secure@example.com", password="pw-12345678")


@pytest.fixture
def logged_in(client, student):
    client.force_login(student)
    return student


# ---------------------------------------------------------------------------
# The card itself
# ---------------------------------------------------------------------------
def test_settings_renders_the_security_section_and_rails_it(client, logged_in):
    body = client.get(reverse(SETTINGS)).content.decode()
    assert 'id="security"' in body
    assert 'href="#security"' in body


def test_the_security_card_links_every_allauth_route(client, logged_in):
    body = client.get(reverse(SETTINGS)).content.decode()
    assert reverse("account_email") in body
    assert reverse("account_change_password") in body
    assert reverse("accounts:signout_all") in body


def test_a_password_account_is_offered_change_not_set(client, logged_in):
    """D6: never show a control that implies a password exists when it
    doesn't, or the reverse."""
    body = client.get(reverse(SETTINGS)).content.decode()
    assert reverse("account_change_password") in body
    assert reverse("account_set_password") not in body


def test_a_passwordless_account_is_offered_set_not_change(client, student):
    student.set_unusable_password()
    student.save(update_fields=["password"])
    client.force_login(student)
    body = client.get(reverse(SETTINGS)).content.decode()
    assert reverse("account_set_password") in body
    assert reverse("account_change_password") not in body


def test_the_connections_row_is_hidden_when_no_provider_is_configured(
    client, logged_in, settings
):
    """An empty connections page is a broken promise: a row that opens a page
    offering nothing to connect. The row is therefore conditional on a
    provider actually having credentials.

    (The spec listed "whether Google login will be configured" as an open
    question. It already is, locally — GOOGLE_OAUTH_CLIENT_ID is set, so
    ENABLED_SOCIAL_PROVIDERS is ['google'] and the row renders by default.
    This test pins the OTHER branch, which is the one that would otherwise
    never be exercised.)
    """
    settings.ENABLED_SOCIAL_PROVIDERS = []
    body = client.get(reverse(SETTINGS)).content.decode()
    assert reverse("socialaccount_connections") not in body


def test_the_connections_row_appears_once_a_provider_is_configured(
    client, logged_in, settings
):
    settings.ENABLED_SOCIAL_PROVIDERS = ["google"]
    body = client.get(reverse(SETTINGS)).content.decode()
    assert reverse("socialaccount_connections") in body


def test_the_verified_badge_comes_from_the_email_address_row(client, logged_in):
    body = client.get(reverse(SETTINGS)).content.decode()
    assert "Not yet verified" in body

    EmailAddress.objects.create(
        user=logged_in, email=logged_in.email, primary=True, verified=True
    )
    body = client.get(reverse(SETTINGS)).content.decode()
    assert "Verified" in body
    assert "Not yet verified" not in body


# ---------------------------------------------------------------------------
# The allauth pages now render inside the Coverage shell
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "route", ["account_change_password", "account_email"]
)
def test_the_allauth_pages_render_in_the_coverage_shell(client, logged_in, route):
    """The tell is `.acct-wrap` plus the shared nav that base.html supplies —
    allauth's own default template has neither."""
    resp = client.get(reverse(route))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "acct-wrap" in body
    assert "Back to settings" in body
    # allauth's raw default renders a bare "Menu:" list; its absence is the
    # proof the override is the template actually being used.
    assert "Menu:" not in body


def test_the_set_password_page_renders_in_the_shell(client, student):
    student.set_unusable_password()
    student.save(update_fields=["password"])
    client.force_login(student)
    resp = client.get(reverse("account_set_password"))
    assert resp.status_code == 200
    assert "acct-wrap" in resp.content.decode()


def test_the_connections_page_renders_in_the_shell(client, logged_in):
    resp = client.get(reverse("socialaccount_connections"))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "acct-wrap" in body
    # Pre-OAuth capture posture (deploy.md §4): no "sync"/"Gmail" claims, and
    # the page must say out loud that connecting is sign-in only.
    assert "never access to" in body


@pytest.mark.parametrize(
    "route",
    ["account_change_password", "account_email", "socialaccount_connections"],
)
def test_the_account_pages_require_a_login(client, route):
    resp = client.get(reverse(route))
    assert resp.status_code == 302


def test_changing_the_password_actually_works_through_the_override(client, logged_in):
    """The template was rewritten by hand, so the form's field names have to
    still be the ones allauth's view expects — a page that renders beautifully
    and never saves would be a worse regression than the unstyled one."""
    resp = client.post(
        reverse("account_change_password"),
        {"oldpassword": "pw-12345678",
         "password1": "new-pw-87654321",
         "password2": "new-pw-87654321"},
        follow=True,
    )
    assert resp.status_code == 200
    logged_in.refresh_from_db()
    assert logged_in.check_password("new-pw-87654321")


# ---------------------------------------------------------------------------
# Sign out everywhere
# ---------------------------------------------------------------------------
def test_the_confirm_page_states_what_happens_before_anything_does(client, logged_in):
    """GET must never end a session — the spec's shared destructive-action
    pattern: a confirm page states the blast radius, the POST executes it."""
    other = Client()
    other.force_login(logged_in)
    before = Session.objects.count()

    resp = client.get(reverse("accounts:signout_all"))
    assert resp.status_code == 200
    assert "This device stays signed in" in resp.content.decode()
    assert Session.objects.count() == before


def test_signing_out_everywhere_kills_other_sessions_and_spares_this_one(
    client, logged_in
):
    other = Client()
    other.force_login(logged_in)
    assert other.get(reverse(SETTINGS)).status_code == 200

    resp = client.post(reverse("accounts:signout_all"), follow=True)
    assert resp.status_code == 200

    # The other browser is signed out...
    assert other.get(reverse(SETTINGS)).status_code == 302
    # ...and the one that asked is not.
    assert client.get(reverse(SETTINGS)).status_code == 200


def test_it_never_touches_another_users_sessions(client, logged_in):
    stranger = User.objects.create_user(email="stranger@example.com", password="x")
    stranger_client = Client()
    stranger_client.force_login(stranger)

    services.sign_out_other_sessions(logged_in, keep_session_key=None)

    assert stranger_client.get(reverse(SETTINGS)).status_code == 200


def test_it_deletes_only_this_users_rows_and_counts_only_those(client, logged_in):
    """The blunt version of the test above, asserted on the table itself.

    A full-table scan that decodes every session is exactly the shape that
    could quietly over-delete, and the symptom (someone else silently signed
    out) would never be reported as a bug — it would look like a flaky login.
    So this pins the arithmetic: rows in, rows out, per user, plus the
    untouched anonymous rows that carry no `_auth_user_id` at all.
    """
    stranger = User.objects.create_user(email="bystander@example.com", password="x")
    for _ in range(2):
        Client().force_login(stranger)
    for _ in range(3):
        Client().force_login(logged_in)
    # An anonymous session — no `_auth_user_id` key at all.
    anon = Client()
    anon.session["cart"] = "something"
    anon.session.save()

    def owned_by(user):
        return sum(
            1 for row in Session.objects.all()
            if row.get_decoded().get("_auth_user_id") == str(user.pk)
        )

    before_stranger, before_total = owned_by(stranger), Session.objects.count()
    assert owned_by(logged_in) == 4  # the request's own + 3 others

    ended = services.sign_out_other_sessions(
        logged_in, keep_session_key=client.session.session_key
    )

    assert ended == 3
    assert owned_by(logged_in) == 1
    assert owned_by(stranger) == before_stranger == 2
    assert Session.objects.count() == before_total - 3
    assert Session.objects.filter(session_key=anon.session.session_key).exists()


def test_an_expired_row_is_neither_deleted_nor_counted(client, logged_in):
    """An expired session can't authenticate anyone, so counting it would
    inflate the receipt the user is being asked to trust."""
    ghost = Client()
    ghost.force_login(logged_in)
    stale = Session.objects.get(session_key=ghost.session.session_key)
    stale.expire_date = timezone.now() - timedelta(days=1)
    stale.save(update_fields=["expire_date"])

    ended = services.sign_out_other_sessions(
        logged_in, keep_session_key=client.session.session_key
    )

    assert ended == 0
    assert Session.objects.filter(session_key=stale.session_key).exists()


def test_the_flash_reports_the_real_count(client, logged_in):
    """"Signed out everywhere" with nothing to sign out of is a claim the user
    can't check. A zero is the honest answer when it's the honest answer."""
    resp = client.post(reverse("accounts:signout_all"), follow=True)
    assert "This is your only session." in resp.content.decode()

    other = Client()
    other.force_login(logged_in)
    resp = client.post(reverse("accounts:signout_all"), follow=True)
    assert "Signed out on 1 other device." in resp.content.decode()


def test_signout_all_requires_a_login(client):
    assert client.get(reverse("accounts:signout_all")).status_code == 302
    assert client.post(reverse("accounts:signout_all")).status_code == 302
