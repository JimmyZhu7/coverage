"""Handing the Gmail grant back to Google.

Disconnecting Gmail and deleting a Coverage account both used to delete the
encrypted refresh-token row and stop there, leaving the OAuth grant live at
Google indefinitely — after account deletion, live for an account that no
longer exists. "Disconnect" and "delete everything" both promised otherwise.

The revoke is best-effort by design: Google is a third party in the middle
of a request the user is waiting on, and a slow or failing revoke must never
be able to stop a disconnect or a deletion. These tests pin both halves —
the call happens, and nothing about it can raise.
"""

from __future__ import annotations

from unittest import mock

import pytest
import requests
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from capture import google_revoke
from capture.models import GmailConnection

pytestmark = pytest.mark.django_db

User = get_user_model()

# A valid Fernet key, so `gmail_live.encrypt_token`/`decrypt_token` round-trip
# in these tests exactly as they do in production. Test-only value.
TOKEN_KEY = "K1Wm3Q5vJ8Hn2Xr7Tc0YbLp4Zd6AeSg9FiUoNjMvQxE="
REFRESH = "1//0g-a-refresh-token"


@pytest.fixture
def student(db):
    return User.objects.create_user(email="student@example.com", password="x")


@pytest.fixture
def connection(student):
    with override_settings(GMAIL_LIVE_TOKEN_KEY=TOKEN_KEY):
        from capture import gmail_live

        return GmailConnection.all_objects.create(
            user=student,
            gmail_address="student@gmail.com",
            refresh_token_encrypted=gmail_live.encrypt_token(REFRESH),
            status="active",
        )


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


# ---------------------------------------------------------------------------
# The call itself.
# ---------------------------------------------------------------------------
def test_the_token_is_posted_to_googles_revoke_endpoint():
    with mock.patch.object(google_revoke.requests, "post",
                           return_value=_Resp(200)) as post:
        assert google_revoke.revoke_token(REFRESH) is True

    (url,), kwargs = post.call_args
    assert url == "https://oauth2.googleapis.com/revoke"
    assert kwargs["data"] == {"token": REFRESH}
    assert kwargs["timeout"] == 5


def test_the_token_travels_in_the_body_not_the_query_string():
    """A revoke URL with the token in it would be logged by every proxy
    between here and Google, which is the opposite of the point."""
    with mock.patch.object(google_revoke.requests, "post",
                           return_value=_Resp(200)) as post:
        google_revoke.revoke_token(REFRESH)

    (url,), kwargs = post.call_args
    assert REFRESH not in url
    assert "params" not in kwargs


def test_an_already_invalid_token_counts_as_revoked():
    """Google answers 400 for a token the user already revoked in their own
    Google account. The grant is not live, which is the outcome we wanted;
    treating it as a failure would log noise for a success."""
    with mock.patch.object(google_revoke.requests, "post", return_value=_Resp(400)):
        assert google_revoke.revoke_token(REFRESH) is True


@pytest.mark.parametrize("status", [401, 500, 503])
def test_a_real_failure_is_reported_not_raised(status, caplog):
    with mock.patch.object(google_revoke.requests, "post", return_value=_Resp(status)):
        assert google_revoke.revoke_token(REFRESH) is False
    assert "revoke returned HTTP" in caplog.text


def test_a_network_failure_is_reported_not_raised(caplog):
    with mock.patch.object(google_revoke.requests, "post",
                           side_effect=requests.ConnectionError("boom")):
        assert google_revoke.revoke_token(REFRESH) is False
    assert "failed to reach Google" in caplog.text


def test_an_unreadable_stored_token_is_skipped_not_fatal(connection, caplog):
    """A wrong or rotated GMAIL_LIVE_TOKEN_KEY makes `decrypt_token` raise
    loudly, which is right everywhere else and wrong here: it must not turn
    "disconnect" or "delete my account" into a 500."""
    with override_settings(GMAIL_LIVE_TOKEN_KEY=""), \
            mock.patch.object(google_revoke.requests, "post") as post:
        assert google_revoke.revoke_connection(connection) is False
    post.assert_not_called()
    assert "token unreadable" in caplog.text


def test_a_connection_with_no_stored_token_makes_no_request(student):
    empty = GmailConnection.all_objects.create(
        user=student, gmail_address="x@gmail.com", refresh_token_encrypted="",
    )
    with mock.patch.object(google_revoke.requests, "post") as post:
        assert google_revoke.revoke_connection(empty) is False
    post.assert_not_called()


# ---------------------------------------------------------------------------
# Gmail disconnect.
# ---------------------------------------------------------------------------
def test_disconnect_revokes_before_it_deletes(client, student, connection):
    client.force_login(student)
    with override_settings(GMAIL_LIVE_TOKEN_KEY=TOKEN_KEY), \
            mock.patch.object(google_revoke.requests, "post",
                              return_value=_Resp(200)) as post:
        resp = client.post(reverse("capture:gmail_disconnect"))

    assert resp.status_code == 302
    assert post.call_args.kwargs["data"] == {"token": REFRESH}
    assert not GmailConnection.all_objects.filter(user=student).exists()


def test_disconnect_still_disconnects_when_google_is_down(client, student, connection):
    """A student who wants out gets out. The stored token is the thing this
    app controls, and it goes either way."""
    client.force_login(student)
    with override_settings(GMAIL_LIVE_TOKEN_KEY=TOKEN_KEY), \
            mock.patch.object(google_revoke.requests, "post",
                              side_effect=requests.Timeout("slow")):
        resp = client.post(reverse("capture:gmail_disconnect"))

    assert resp.status_code == 302
    assert not GmailConnection.all_objects.filter(user=student).exists()


def test_disconnect_only_revokes_the_callers_own_grant(client, student, connection):
    other = User.objects.create_user(email="other@example.com", password="x")
    with override_settings(GMAIL_LIVE_TOKEN_KEY=TOKEN_KEY):
        from capture import gmail_live

        GmailConnection.all_objects.create(
            user=other,
            gmail_address="other@gmail.com",
            refresh_token_encrypted=gmail_live.encrypt_token("someone-elses-token"),
            status="active",
        )

    client.force_login(student)
    with override_settings(GMAIL_LIVE_TOKEN_KEY=TOKEN_KEY), \
            mock.patch.object(google_revoke.requests, "post",
                              return_value=_Resp(200)) as post:
        client.post(reverse("capture:gmail_disconnect"))

    assert post.call_count == 1
    assert post.call_args.kwargs["data"] == {"token": REFRESH}
    assert GmailConnection.all_objects.filter(user=other).exists()


# ---------------------------------------------------------------------------
# Account deletion.
# ---------------------------------------------------------------------------
def test_deleting_an_account_revokes_its_gmail_grant(student, connection):
    from accounts.services import delete_user_and_data

    with override_settings(GMAIL_LIVE_TOKEN_KEY=TOKEN_KEY), \
            mock.patch.object(google_revoke.requests, "post",
                              return_value=_Resp(200)) as post:
        counts = delete_user_and_data(student)

    assert post.call_args.kwargs["data"] == {"token": REFRESH}
    assert counts["gmail_connection"] == 1
    assert not User.objects.filter(pk=student.pk).exists()


def test_deletion_is_not_blocked_by_a_failing_revoke(student, connection):
    """Leaving a stale grant behind is bad. Refusing to delete somebody's
    account because Google was slow is worse."""
    from accounts.services import delete_user_and_data

    with override_settings(GMAIL_LIVE_TOKEN_KEY=TOKEN_KEY), \
            mock.patch.object(google_revoke.requests, "post",
                              side_effect=requests.ConnectionError("boom")):
        delete_user_and_data(student)

    assert not User.objects.filter(pk=student.pk).exists()
    assert not GmailConnection.all_objects.filter(user_id=student.pk).exists()


def test_deleting_an_account_with_no_gmail_makes_no_request(student):
    from accounts.services import delete_user_and_data

    with mock.patch.object(google_revoke.requests, "post") as post:
        delete_user_and_data(student)
    post.assert_not_called()
