"""A revoked Gmail connection has to say so where the student is looking.

`todo-mined.md §4`, from `docs/gmail-live-setup.md §9`: when Google drops the
grant, `gmail_live` flips the row to `revoked` and the sync stops. Nothing
told the student. The only surface that knew was `/ops/health/gmail/`, which
is staff-only, so the student's experience was a mailbox that had quietly
stopped producing touches and a Settings page that looked fine.

Two things this pins, because both are silent failures rather than loud ones:

  * the Settings card renders the revoked state AND a control to fix it; and
  * a reconnect moves `connected_at`, which is what makes it a token-issuance
    timestamp rather than a first-seen one. It was `auto_now_add` and nothing
    else, so it never moved after the first connect, and D-17's seven-day
    expiry experiment has no clock to read without this.

The staff gate on `/ops/health/gmail/` is asserted here too
(`audit-security.md §18`): the point of this item is to give the student their
own view, not to widen the operator's.
"""

from __future__ import annotations

import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from capture import gmail_live
from capture.models import GmailConnection

pytestmark = pytest.mark.django_db

User = get_user_model()
SETTINGS = "accounts:settings"


@pytest.fixture
def student(db):
    return User.objects.create_user(email="revoked-student@example.com",
                                    password="x")


def _connection(student, **kw):
    kw.setdefault("gmail_address", "revoked-student@example.com")
    kw.setdefault("refresh_token_encrypted", "ciphertext")
    return GmailConnection.all_objects.create(user=student, **kw)


# ---------------------------------------------------------------------------
# What the student sees
# ---------------------------------------------------------------------------

def test_the_settings_card_says_the_access_was_revoked(client, student):
    _connection(student, status="revoked")
    client.force_login(student)
    body = client.get(reverse(SETTINGS)).content.decode()
    assert "Access was revoked" in body, (
        "a revoked connection renders as an ordinary one, so the student's "
        "only signal that mail stopped syncing is that it stopped"
    )


def test_the_revoked_card_offers_the_control_that_fixes_it(client, student):
    """Naming the problem without offering the fix is half a message. The
    reconnect control is the same `gmail_connect` entry point the first
    connect uses; what matters is that it is ON the card in the revoked
    state."""
    _connection(student, status="revoked")
    client.force_login(student)
    body = client.get(reverse(SETTINGS)).content.decode()
    connect_url = reverse("capture:gmail_connect")
    assert connect_url in body
    # The LABEL on the control, not just the word anywhere on the page: the
    # revoked copy already says "Reconnect below", so a bare substring check
    # would pass on a card with no button at all. Matched against the anchor's
    # own text with the template's whitespace collapsed.
    anchor = re.search(
        rf'<a[^>]*href="{re.escape(connect_url)}"[^>]*>(.*?)</a>', body, re.S)
    assert anchor, "the revoked card renders no link to the connect flow"
    assert "Reconnect" in " ".join(anchor.group(1).split())


def test_an_active_connection_does_not_wear_the_revoked_copy(client, student):
    """P3's shape: an untouched, working connection renders exactly as it did
    before this landed."""
    _connection(student, status="active")
    client.force_login(student)
    body = client.get(reverse(SETTINGS)).content.decode()
    assert "Access was revoked" not in body
    assert "revoked-student@example.com" in body


def test_a_student_with_no_connection_is_unaffected(client, student):
    client.force_login(student)
    body = client.get(reverse(SETTINGS)).content.decode()
    assert "Access was revoked" not in body
    assert "Connect Gmail" in body


# ---------------------------------------------------------------------------
# The timestamp D-17 needs
# ---------------------------------------------------------------------------

def _fake_gmail_client():
    client = MagicMock()
    client.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "revoked-student@example.com",
        "historyId": "1000",
    }
    client.users.return_value.watch.return_value.execute.return_value = {
        "historyId": "1001", "expiration": "9999999999999",
    }
    return client


def _reconnect(student):
    flow = MagicMock()
    flow.credentials = MagicMock(refresh_token="1//fresh-refresh-token")
    fake = _fake_gmail_client()
    with patch.object(gmail_live, "_flow", return_value=flow), \
         patch.object(gmail_live, "build", return_value=fake), \
         patch.object(gmail_live, "_gmail_client", return_value=fake):
        return gmail_live.connect_gmail(student, "auth-code", "https://x/callback")


def test_a_reconnect_moves_the_timestamp(student, settings):
    """THE fix. `connected_at` is `auto_now_add`, which fills a field on
    INSERT and never touches it again, so before this it recorded when the
    mailbox was first linked and said nothing about the token sitting beside
    it. A student who reconnected in September still read as connected in
    June, and the seven-day-expiry question (D-17) had no clock at all.
    """
    settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
    old = timezone.now() - timedelta(days=90)
    connection = _connection(student, status="revoked")
    GmailConnection.all_objects.filter(pk=connection.pk).update(connected_at=old)

    reconnected = _reconnect(student)

    assert reconnected.connected_at > old, (
        "connected_at did not move on reconnect, so it is still a first-seen "
        "date and cannot answer when the current token was issued"
    )
    assert (timezone.now() - reconnected.connected_at) < timedelta(minutes=1)
    assert reconnected.status == "active"


def test_a_first_connect_still_stamps_the_row(student, settings):
    settings.GMAIL_LIVE_TOKEN_KEY = gmail_live.Fernet.generate_key().decode()
    assert not GmailConnection.all_objects.filter(user=student).exists()
    connection = _reconnect(student)
    assert connection.connected_at is not None
    assert (timezone.now() - connection.connected_at) < timedelta(minutes=1)


# ---------------------------------------------------------------------------
# The operator's view stays the operator's
# ---------------------------------------------------------------------------

def test_the_ops_health_page_is_still_staff_only(client, student):
    """`audit-security.md §18`. This item gives the student their own view of
    a revoked connection; it must not turn the staff page into a second one.
    Both the signed-out and the signed-in-but-not-staff cases, because
    `staff_member_required` treats them differently and only one of them is
    the interesting refusal."""
    url = reverse("ops:health-gmail")

    anonymous = client.get(url)
    assert anonymous.status_code in (302, 403), anonymous.status_code

    client.force_login(student)
    assert student.is_staff is False
    signed_in = client.get(url)
    assert signed_in.status_code in (302, 403), signed_in.status_code
    if signed_in.status_code == 200:  # pragma: no cover - the failure branch
        raise AssertionError("a non-staff student can read every tenant's "
                             "Gmail addresses")
