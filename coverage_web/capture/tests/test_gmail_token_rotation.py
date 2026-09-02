"""Rotating the key that encrypts a stored Gmail refresh token.

`audit-security.md` finding 9: one static Fernet key, `is_configured()`
checking only that it is non-empty, and no rotation path — so rotating meant
writing a re-encrypt script that did not exist, and changing the key without
one makes every stored token unreadable at once. A key that cannot be rotated
is a key that cannot be retired if it leaks.

The failure mode was at least honest (`InvalidToken` raises loudly rather than
marking one user revoked), which is why this is prospective rather than an
incident. What follows is the whole lifecycle, in order, because the dangerous
step is the LAST one: dropping the old key before every row has moved.
"""

from __future__ import annotations

from io import StringIO

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command

from capture import gmail_live
from capture.models import GmailConnection

User = get_user_model()
pytestmark = pytest.mark.django_db

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


@pytest.fixture
def connection(settings):
    settings.GMAIL_LIVE_TOKEN_KEY = KEY_A
    user = User.objects.create_user(email="rotate@example.test", password="x")
    return GmailConnection.all_objects.create(
        user=user,
        gmail_address="rotate@example.test",
        refresh_token_encrypted=gmail_live.encrypt_token("the-refresh-token"),
    )


def _run(*args):
    out = StringIO()
    call_command("rotate_gmail_tokens", *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def test_the_whole_rotation(connection, settings):
    """THE test the item asks for, in one function because the steps only
    mean anything in sequence.

    Encrypt under A; move to [B, A] and assert the old ciphertext still
    decrypts; run the command; then assert it decrypts under B ALONE, which is
    the step that proves the old key can now be retired.
    """
    stored = connection.refresh_token_encrypted

    # Step 2: new key first, old key still present.
    settings.GMAIL_LIVE_TOKEN_KEY = f"{KEY_B},{KEY_A}"
    assert gmail_live.decrypt_token(stored) == "the-refresh-token", (
        "a row written under the old key must keep working while both keys "
        "are listed — otherwise the rotation is an outage"
    )

    # Step 3.
    _run()
    connection.refresh_from_db()
    assert connection.refresh_token_encrypted != stored

    # Step 4: the old key is gone and the token is still readable.
    settings.GMAIL_LIVE_TOKEN_KEY = KEY_B
    assert gmail_live.decrypt_token(
        connection.refresh_token_encrypted) == "the-refresh-token"


def test_dropping_the_old_key_too_early_is_the_thing_that_breaks(
    connection, settings
):
    """The reason `--check` exists, pinned. This is the mistake the procedure
    is written to prevent, and it must fail LOUDLY — a silent empty read here
    would look like "the student disconnected their Gmail"."""
    settings.GMAIL_LIVE_TOKEN_KEY = KEY_B  # step 4 without step 3
    with pytest.raises(gmail_live.GmailLiveError) as caught:
        gmail_live.decrypt_token(connection.refresh_token_encrypted)
    assert "rotat" in str(caught.value).lower(), (
        "the error must point at the rotation procedure; a bare "
        "'cannot decrypt' sends the reader to the wrong problem"
    )


def test_the_command_is_idempotent(connection, settings):
    settings.GMAIL_LIVE_TOKEN_KEY = f"{KEY_B},{KEY_A}"
    _run()
    once = GmailConnection.all_objects.get(pk=connection.pk).refresh_token_encrypted
    _run()
    twice = GmailConnection.all_objects.get(pk=connection.pk).refresh_token_encrypted
    # Re-encrypting produces new bytes each time (fresh IV and timestamp), so
    # idempotent means "still correct and still on the newest key", not
    # "byte-identical".
    assert once != twice
    settings.GMAIL_LIVE_TOKEN_KEY = KEY_B
    assert gmail_live.decrypt_token(twice) == "the-refresh-token"


def test_the_command_reports_the_row_count(connection, settings):
    settings.GMAIL_LIVE_TOKEN_KEY = f"{KEY_B},{KEY_A}"
    assert "re-encrypted 1 of 1 connection(s)" in _run()


def test_check_writes_nothing(connection, settings):
    settings.GMAIL_LIVE_TOKEN_KEY = f"{KEY_B},{KEY_A}"
    before = connection.refresh_token_encrypted
    assert "would re-encrypt 1 of 1" in _run("--check")
    connection.refresh_from_db()
    assert connection.refresh_token_encrypted == before


def test_it_writes_only_the_token_column(connection, settings):
    """A rotation must not look like a reconnect to anything else: no status
    change, no timestamp touch, no backfill re-queue."""
    settings.GMAIL_LIVE_TOKEN_KEY = f"{KEY_B},{KEY_A}"
    before = GmailConnection.all_objects.filter(pk=connection.pk).values().first()
    _run()
    after = GmailConnection.all_objects.filter(pk=connection.pk).values().first()
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"refresh_token_encrypted"}, changed


def test_a_single_key_configuration_behaves_exactly_as_before(settings):
    """P3, and the degradation clause of the item: nobody who is not rotating
    should be able to tell this landed."""
    settings.GMAIL_LIVE_TOKEN_KEY = KEY_A
    assert gmail_live.token_keys() == [KEY_A]
    assert gmail_live.decrypt_token(
        gmail_live.encrypt_token("plain")) == "plain"


def test_an_unreadable_row_is_reported_and_does_not_stop_the_others(settings):
    """A token encrypted under a key nobody has any more is already lost, and
    the answer is a reconnect. Aborting the rotation over it would strand
    every other row on the old key too."""
    settings.GMAIL_LIVE_TOKEN_KEY = KEY_A
    good = GmailConnection.all_objects.create(
        user=User.objects.create_user(email="good@example.test", password="x"),
        gmail_address="good@example.test",
        refresh_token_encrypted=gmail_live.encrypt_token("keep-me"),
    )
    lost = GmailConnection.all_objects.create(
        user=User.objects.create_user(email="lost@example.test", password="x"),
        gmail_address="lost@example.test",
        refresh_token_encrypted=Fernet(
            Fernet.generate_key()).encrypt(b"orphan").decode(),
    )

    settings.GMAIL_LIVE_TOKEN_KEY = f"{KEY_B},{KEY_A}"
    out = _run()
    assert "1 unreadable" in out

    settings.GMAIL_LIVE_TOKEN_KEY = KEY_B
    good.refresh_from_db()
    assert gmail_live.decrypt_token(good.refresh_token_encrypted) == "keep-me"
    lost.refresh_from_db()
    with pytest.raises(gmail_live.GmailLiveError):
        gmail_live.decrypt_token(lost.refresh_token_encrypted)


def test_an_empty_key_setting_says_so_rather_than_raising(settings):
    settings.GMAIL_LIVE_TOKEN_KEY = ""
    assert gmail_live.token_keys() == []
    assert "nothing to rotate" in _run()


def test_a_malformed_key_names_itself(settings):
    settings.GMAIL_LIVE_TOKEN_KEY = "not-a-fernet-key"
    with pytest.raises(gmail_live.GmailLiveError) as caught:
        gmail_live.encrypt_token("x")
    assert "GMAIL_LIVE_TOKEN_KEY" in str(caught.value)
