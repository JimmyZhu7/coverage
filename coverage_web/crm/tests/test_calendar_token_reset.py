"""Resetting the ICS feed link.

The feed at `/app/calendar/feed/<token>.ics` is authenticated by a token in
the URL PATH, because that is the only credential Calendar.app and Google
Calendar can carry: they fetch from their own servers, with none of our
cookies. A path component is also the worst place to keep a secret. It lands
in proxy access logs, in the calendar provider's fetch logs, in browser
history, and in any screen share of the Subscribe button.

Until 2026-09-01 the only way to revoke one was a Django shell. These tests
pin the Settings control that replaces that, and the rule that goes with it:
the new token is shown to the user once and written to no log.
"""

from __future__ import annotations

import logging

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

pytestmark = pytest.mark.django_db

User = get_user_model()
RESET = "crm:calendar_token_reset"


@pytest.fixture
def student(db):
    return User.objects.create_user(email="student@example.com", password="x")


@pytest.fixture
def logged_in(client, student):
    client.force_login(student)
    return student


def _feed(token):
    return reverse("crm:calendar_ics", args=[token])


# ---------------------------------------------------------------------------
# Method and auth.
# ---------------------------------------------------------------------------
def test_reset_requires_login(client, student):
    resp = client.post(reverse(RESET))
    assert resp.status_code == 302
    assert "/accounts/login/" in resp["Location"]
    student.refresh_from_db()
    assert student.calendar_token


def test_a_get_cannot_revoke_anything(client, logged_in):
    """A link, a prefetch, or an image tag pointed here must not burn
    somebody's calendar subscriptions."""
    before = logged_in.calendar_token
    assert client.get(reverse(RESET)).status_code == 405
    logged_in.refresh_from_db()
    assert logged_in.calendar_token == before


# ---------------------------------------------------------------------------
# What the reset does.
# ---------------------------------------------------------------------------
def test_the_old_link_dies_and_the_new_one_works(client, logged_in):
    old = logged_in.calendar_token
    assert client.get(_feed(old)).status_code == 200

    client.post(reverse(RESET))

    logged_in.refresh_from_db()
    new = logged_in.calendar_token
    assert new and new != old
    assert client.get(_feed(old)).status_code == 404
    assert client.get(_feed(new)).status_code == 200


def test_the_new_url_is_shown_once(client, logged_in):
    resp = client.post(reverse(RESET), follow=True)
    logged_in.refresh_from_db()

    body = resp.content.decode()
    assert logged_in.calendar_token in body, "the user must be able to re-subscribe"

    again = client.get(reverse("accounts:settings")).content.decode()
    assert logged_in.calendar_token not in again, (
        "a one-shot message, not a token printed on the page from then on"
    )


def test_the_token_reaches_no_log(client, logged_in, caplog):
    """The leak this whole control exists for is a token in a log. A reset
    view that logged what it minted would just move the problem one file
    across."""
    with caplog.at_level(logging.DEBUG):
        client.post(reverse(RESET))

    logged_in.refresh_from_db()
    assert logged_in.calendar_token not in caplog.text


def test_the_redirect_carries_no_token_in_its_url(client, logged_in):
    """Query strings land in access logs the same way paths do."""
    resp = client.post(reverse(RESET))
    logged_in.refresh_from_db()
    assert resp.status_code == 302
    assert logged_in.calendar_token not in resp["Location"]
    assert resp["Location"].endswith("#security")


def test_one_students_reset_leaves_another_alone(client, student):
    other = User.objects.create_user(email="other@example.com", password="x")
    other_token = other.calendar_token

    client.force_login(student)
    client.post(reverse(RESET))

    other.refresh_from_db()
    assert other.calendar_token == other_token
    assert client.get(_feed(other_token)).status_code == 200


def test_resetting_twice_gives_two_different_tokens(client, logged_in):
    client.post(reverse(RESET))
    logged_in.refresh_from_db()
    first = logged_in.calendar_token

    client.post(reverse(RESET))
    logged_in.refresh_from_db()
    assert logged_in.calendar_token != first


# ---------------------------------------------------------------------------
# The control itself.
# ---------------------------------------------------------------------------
def test_settings_offers_the_reset_as_a_post(client, logged_in):
    body = client.get(reverse("accounts:settings")).content.decode()
    assert reverse(RESET) in body
    row = body[body.index(reverse(RESET)) - 400:body.index(reverse(RESET)) + 400]
    assert 'method="post"' in row
    assert "csrfmiddlewaretoken" in row
