"""The Google Calendar grant: connecting it, disconnecting it, and the
Settings option that does both.

OFFLINE. Google, the `Flow` and the Calendar client are all mocked — this
tests what happens once an OAuth code exchange has already succeeded, and
what the views and the Settings page do around it. Same posture and the same
seams as test_gmail_connect.py, deliberately, because these are the same
kind of thing and should fail the same way when they break.

The scope assertions in `TestScopesStayApart` are the ones worth reading
twice. They are what stops a future edit quietly turning a read-only mirror
into a calendar Coverage can write to, or folding the mail scope into the
calendar grant.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from capture import gcal_live, gmail_live
from capture.models import GmailConnection, GoogleCalendarConnection

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


@pytest.fixture
def student(db):
    return User.objects.create_user(email="cal-connect@example.com", password="x")


@pytest.fixture
def configured(settings):
    """Calendar's own switch ON, plus the three Gmail Live values it shares."""
    settings.GCAL_LIVE_ENABLED = True
    settings.GMAIL_LIVE_CLIENT_ID = "client-id"
    settings.GMAIL_LIVE_CLIENT_SECRET = "client-secret"
    settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
    return settings


def _fake_flow(refresh_token: str = "1//fake-calendar-refresh") -> MagicMock:
    flow = MagicMock()
    flow.credentials = MagicMock(refresh_token=refresh_token)
    return flow


def _fake_calendar_client() -> MagicMock:
    client = MagicMock()
    client.calendars.return_value.get.return_value.execute.return_value = {
        "id": "cal-connect@example.com",
        "summary": "cal-connect@example.com",
    }
    return client


# ---------------------------------------------------------------------------
class TestConfiguredGate:
    def test_the_flag_alone_is_what_turns_the_feature_on(self, configured):
        assert gcal_live.is_configured() is True
        configured.GCAL_LIVE_ENABLED = False
        # The shared credentials are all still set. The flag is the switch,
        # and it stays off until the consent screen carries the scope.
        assert gcal_live.is_configured() is False

    def test_the_flag_without_gmails_credentials_is_still_off(self, configured):
        configured.GMAIL_LIVE_CLIENT_ID = ""
        assert gcal_live.is_configured() is False

    def test_the_connect_view_404s_on_an_unconfigured_deploy(self, client, student, settings):
        settings.GCAL_LIVE_ENABLED = False
        client.force_login(student)
        assert client.get(reverse("capture:gcal_connect")).status_code == 404


# ---------------------------------------------------------------------------
class TestConnect:
    def test_a_first_connect_stores_the_grant(self, student, configured):
        with patch.object(gcal_live, "_flow", return_value=_fake_flow()), \
             patch("capture.gcal_live.build", return_value=_fake_calendar_client()):
            connection = gcal_live.connect_calendar(
                student, "code", "https://example.com/capture/calendar/callback/"
            )

        assert connection.google_email == "cal-connect@example.com"
        assert connection.calendar_id == "primary"
        assert connection.status == "active"
        # The token is stored ENCRYPTED and the row has no plaintext
        # accessor, same rule as GmailConnection.
        assert connection.refresh_token_encrypted != "1//fake-calendar-refresh"
        assert gmail_live.decrypt_token(connection.refresh_token_encrypted) == (
            "1//fake-calendar-refresh"
        )

    def test_no_refresh_token_is_a_readable_error_not_a_500(self, student, configured):
        with patch.object(gcal_live, "_flow", return_value=_fake_flow(refresh_token="")):
            with pytest.raises(gcal_live.GcalError) as exc:
                gcal_live.connect_calendar(student, "code", "https://example.com/cb/")

        assert "myaccount.google.com/permissions" in str(exc.value)

    def test_a_refused_calendar_api_names_the_console_setting(self, student, configured):
        """A 403 "Google Calendar API has not been used in project N" is an
        HttpError, not a GcalError, and would otherwise escape to the generic
        500 page for a problem the user can fix in a minute."""
        with patch.object(gcal_live, "_flow", return_value=_fake_flow()), \
             patch("capture.gcal_live.build", side_effect=Exception("403 API disabled")):
            with pytest.raises(gcal_live.GcalError) as exc:
                gcal_live.connect_calendar(student, "code", "https://example.com/cb/")

        assert "Google Calendar API" in str(exc.value)

    def test_a_reconnect_clears_the_stale_cursor(self, student, configured):
        existing = GoogleCalendarConnection.all_objects.create(
            user=student, google_email="cal-connect@example.com",
            refresh_token_encrypted="old", sync_token="stale-token", status="revoked",
        )

        with patch.object(gcal_live, "_flow", return_value=_fake_flow()), \
             patch("capture.gcal_live.build", return_value=_fake_calendar_client()):
            gcal_live.connect_calendar(student, "code", "https://example.com/cb/")

        existing.refresh_from_db()
        # The old cursor was issued against a grant that has been replaced,
        # and Google answers a stale one with a 410 anyway. Clearing it makes
        # the first sync after a reconnect a deliberate windowed read rather
        # than an error path.
        assert existing.sync_token == ""
        assert existing.status == "active"

    def test_connect_does_not_use_the_tenant_scoped_manager(self, student, configured):
        """The exact bug `connect_gmail` shipped with: writing through the
        tenant-SCOPED manager, which refuses to build a queryset unless the
        call goes through `.for_user()` first and raises `TenantScopeError`
        from `update_or_create`. Pinned here so the calendar path cannot
        repeat it."""
        with patch.object(gcal_live, "_flow", return_value=_fake_flow()), \
             patch("capture.gcal_live.build", return_value=_fake_calendar_client()):
            gcal_live.connect_calendar(student, "code", "https://example.com/cb/")

        assert GoogleCalendarConnection.all_objects.filter(user=student).count() == 1


# ---------------------------------------------------------------------------
class TestScopesStayApart:
    def test_the_calendar_scope_is_read_only(self, settings):
        assert settings.GCAL_LIVE_SCOPES == [
            "https://www.googleapis.com/auth/calendar.readonly"
        ]
        # Not `calendar`, not `calendar.events` — nothing that can write.
        for scope in settings.GCAL_LIVE_SCOPES:
            assert scope.endswith(".readonly")

    def test_gmails_scope_list_was_not_widened(self, settings):
        assert settings.GMAIL_LIVE_SCOPES == [
            "https://www.googleapis.com/auth/gmail.readonly"
        ]
        assert not any("calendar" in s for s in settings.GMAIL_LIVE_SCOPES)

    def test_the_calendar_consent_asks_for_the_calendar_scope_only(self, configured):
        url = gcal_live.build_auth_url("https://example.com/cb/", "state-1")
        assert "calendar.readonly" in url
        assert "gmail.readonly" not in url

    def test_the_consent_refuses_googles_incremental_scope_merge(self, configured):
        """`include_granted_scopes=false` is what keeps the grants apart at
        Google's end. With it true, connecting the calendar would hand back a
        token that also reads mail, and a Gmail reconnect would silently pick
        the calendar back up after a disconnect."""
        url = gcal_live.build_auth_url("https://example.com/cb/", "state-1")
        assert "include_granted_scopes=false" in url

    def test_the_calendar_flow_has_pkce_disabled(self, configured):
        """Same trap as `gmail_live._flow`: the authorization request and
        the token exchange are two HTTP requests, so a per-instance PKCE
        verifier never survives the redirect and every exchange fails with
        "invalid_grant: Missing code verifier"."""
        flow = gcal_live._flow("https://example.com/cb/")
        assert flow.code_verifier is None


# ---------------------------------------------------------------------------
class TestDisconnect:
    def test_disconnect_revokes_at_google_then_deletes_the_row(self, student, configured):
        GoogleCalendarConnection.all_objects.create(
            user=student, google_email="cal-connect@example.com",
            refresh_token_encrypted=gmail_live.encrypt_token("1//token"),
        )

        with patch("capture.google_revoke.revoke_token", return_value=True) as revoke:
            removed = gcal_live.disconnect(student)

        assert removed == 1
        revoke.assert_called_once_with("1//token")
        assert not GoogleCalendarConnection.all_objects.filter(user=student).exists()

    def test_disconnecting_the_calendar_leaves_gmail_connected(self, student, configured):
        """The whole reason these are two rows. A student who disconnects one
        must not lose the other."""
        GmailConnection.all_objects.create(
            user=student, gmail_address="cal-connect@example.com",
            refresh_token_encrypted=gmail_live.encrypt_token("1//mail"),
        )
        GoogleCalendarConnection.all_objects.create(
            user=student, google_email="cal-connect@example.com",
            refresh_token_encrypted=gmail_live.encrypt_token("1//cal"),
        )

        with patch("capture.google_revoke.revoke_token", return_value=True):
            gcal_live.disconnect(student)

        assert GmailConnection.all_objects.filter(user=student).exists()
        assert not GoogleCalendarConnection.all_objects.filter(user=student).exists()

    def test_disconnecting_gmail_leaves_the_calendar_connected(
        self, client, student, configured
    ):
        GmailConnection.all_objects.create(
            user=student, gmail_address="cal-connect@example.com",
            refresh_token_encrypted=gmail_live.encrypt_token("1//mail"),
        )
        GoogleCalendarConnection.all_objects.create(
            user=student, google_email="cal-connect@example.com",
            refresh_token_encrypted=gmail_live.encrypt_token("1//cal"),
        )
        client.force_login(student)

        with patch("capture.google_revoke.revoke_token", return_value=True):
            client.post(reverse("capture:gmail_disconnect"))

        assert not GmailConnection.all_objects.filter(user=student).exists()
        assert GoogleCalendarConnection.all_objects.filter(user=student).exists()

    def test_a_failing_revoke_still_removes_the_row(self, student, configured):
        """`google_revoke` is best-effort and never raises: refusing to
        disconnect because Google was slow is worse than a stale grant the
        user can still kill from their Google account."""
        GoogleCalendarConnection.all_objects.create(
            user=student, google_email="cal-connect@example.com",
            refresh_token_encrypted=gmail_live.encrypt_token("1//token"),
        )

        with patch("capture.google_revoke.revoke_token", return_value=False):
            gcal_live.disconnect(student)

        assert not GoogleCalendarConnection.all_objects.filter(user=student).exists()

    def test_the_disconnect_view_is_post_only(self, client, student, configured):
        client.force_login(student)
        assert client.get(reverse("capture:gcal_disconnect")).status_code == 405


# ---------------------------------------------------------------------------
class TestAccountDeletionHandsBackBothGrants:
    def test_deleting_an_account_revokes_the_calendar_grant_too(self, student, configured):
        """The privacy policy calls deletion immediate and complete. A
        calendar grant that outlives the account is exactly what
        `google_revoke` exists to stop."""
        from capture import google_revoke

        GmailConnection.all_objects.create(
            user=student, gmail_address="cal-connect@example.com",
            refresh_token_encrypted=gmail_live.encrypt_token("1//mail"),
        )
        GoogleCalendarConnection.all_objects.create(
            user=student, google_email="cal-connect@example.com",
            refresh_token_encrypted=gmail_live.encrypt_token("1//cal"),
        )

        with patch("capture.google_revoke.revoke_token", return_value=True) as revoke:
            count = google_revoke.revoke_all_for_user(student)

        assert count == 2
        assert {c.args[0] for c in revoke.call_args_list} == {"1//mail", "1//cal"}


# ---------------------------------------------------------------------------
class TestTheSettingsOption:
    def test_the_card_offers_connect_and_says_it_is_view_only(
        self, client, student, configured
    ):
        client.force_login(student)
        html = client.get(reverse("accounts:settings")).content.decode()

        assert 'id="google-calendar"' in html
        assert reverse("capture:gcal_connect") in html
        assert "Connect Calendar" in html
        assert "View only" in html

    def test_a_connected_calendar_offers_disconnect(self, client, student, configured):
        GoogleCalendarConnection.all_objects.create(
            user=student, google_email="cal-connect@example.com",
            refresh_token_encrypted="x",
        )
        client.force_login(student)
        html = client.get(reverse("accounts:settings")).content.decode()

        assert reverse("capture:gcal_disconnect") in html
        assert "Disconnect" in html

    def test_no_card_at_all_until_the_consent_screen_carries_the_scope(
        self, client, student, settings
    ):
        """A Connect button that sends a student to a Google page which
        refuses the scope is worse than no button: they read the refusal as
        Coverage being broken."""
        settings.GCAL_LIVE_ENABLED = False
        client.force_login(student)
        html = client.get(reverse("accounts:settings")).content.decode()

        assert 'id="google-calendar"' not in html

    def test_a_revoked_grant_is_visible_on_the_page_a_student_checks(
        self, client, student, configured
    ):
        GoogleCalendarConnection.all_objects.create(
            user=student, google_email="cal-connect@example.com",
            refresh_token_encrypted="x", status="revoked",
        )
        client.force_login(student)
        html = client.get(reverse("accounts:settings")).content.decode()

        assert "Access was revoked" in html

    def test_the_gmail_card_states_the_same_view_only_promise(
        self, client, student, configured
    ):
        """Both surfaces read as the same kind of switch. This also pins the
        correction: the Gmail card used to promise "never message bodies",
        which `gmail_live` has never honoured — it fetches `format="full"`
        and walks every text part."""
        configured.GMAIL_LIVE_PUBSUB_TOPIC = ""
        client.force_login(student)
        html = client.get(reverse("accounts:settings")).content.decode()

        assert "never message bodies" not in html
        assert html.count("View only") >= 2
