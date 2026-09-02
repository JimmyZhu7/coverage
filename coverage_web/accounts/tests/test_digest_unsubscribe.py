"""The unsubscribe link in the weekly digest.

settings-page.md's LATER section says the digest ships with "an unsubscribe
link in the email that writes the same flag" the Settings toggle writes. It
shipped without one, so the only way to stop the mail was a page you have to
be signed in to reach, from a mail you are reading on a phone.

The three properties that make this safe are each pinned below:

  SAME FLAG. `accounts.unsubscribe.apply_flag` writes
  `User.weekly_digest_opt_out`, the identical column
  `accounts.forms.NotificationsForm` writes, so the email and the toggle can
  never disagree about what "off" means (P5).

  A SIGNED TOKEN, NOT A SESSION. The reader is holding an email. The token
  carries the user id and a timestamp and nothing else, and a tampered one
  is refused rather than guessed at.

  POST WRITES, GET ASKS. Mail gateways, security scanners and link
  previewers fetch every URL in a message. A GET that flipped the flag would
  unsubscribe people whose corporate mail scanner is doing its job.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.urls import reverse

from accounts import unsubscribe as unsub

User = get_user_model()


@pytest.fixture
def reader(db):
    return User.objects.create_user(email="reader@example.com", password="pw12345!")


def _url(token: str) -> str:
    return reverse("accounts:digest_unsubscribe", args=[token])


# ---------------------------------------------------------------------------
# The token.
# ---------------------------------------------------------------------------

def test_a_token_round_trips_to_its_own_user(reader):
    assert unsub.read_token(unsub.make_token(reader)) == reader.pk


def test_a_tampered_token_is_refused(reader):
    token = unsub.make_token(reader)
    forged = token[:-1] + ("a" if token[-1] != "a" else "b")

    with pytest.raises(unsub.BadToken):
        unsub.read_token(forged)


def test_a_token_signed_with_another_salt_is_refused(reader):
    """The salt scopes these signatures to this one purpose. A token minted
    by any other signer in the project must not work here, and vice versa."""
    other = signing.TimestampSigner(salt="something.else").sign(str(reader.pk))

    with pytest.raises(unsub.BadToken):
        unsub.read_token(other)


def test_the_window_is_long_enough_to_survive_a_recruiting_cycle():
    """A digest sits in an inbox. A student unsubscribing from last term's
    mail should meet a working link, because the alternative to one is being
    marked as spam."""
    assert unsub.MAX_AGE_SECONDS >= 365 * 24 * 60 * 60


# ---------------------------------------------------------------------------
# The view.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_asks_and_writes_nothing(client, reader):
    response = client.get(_url(unsub.make_token(reader)))

    assert response.status_code == 200
    assert "Stop the weekly digest?" in response.content.decode()
    reader.refresh_from_db()
    assert reader.weekly_digest_opt_out is False, (
        "a mail gateway prefetching the link must not unsubscribe anyone"
    )


@pytest.mark.django_db
def test_post_unsubscribes_and_says_where_to_undo_it(client, reader):
    response = client.post(_url(unsub.make_token(reader)))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Unsubscribed." in body
    assert "Turn it back on in Settings." in body

    reader.refresh_from_db()
    assert reader.weekly_digest_opt_out is True


@pytest.mark.django_db
def test_the_page_does_not_wear_the_products_navigation(client, reader):
    """This URL lives under /welcome/, which is the prefix the site nav uses
    to light SETTINGS. Without suppressing it, someone arriving from an inbox
    met the whole app's navigation with the wrong tab highlighted, on a page
    that is not Settings and for a reader who may have no account open at
    all."""
    body = client.get(_url(unsub.make_token(reader))).content.decode()

    assert 'class="site-nav"' not in body
    assert 'class="wordmark"' in body, "the wordmark still says whose email this was"


@pytest.mark.django_db
def test_it_works_without_a_session(client, reader):
    """The whole point. The reader is in an inbox, on a phone that may never
    have signed in to Coverage."""
    assert "_auth_user_id" not in client.session
    client.post(_url(unsub.make_token(reader)))

    reader.refresh_from_db()
    assert reader.weekly_digest_opt_out is True


@pytest.mark.django_db
def test_unsubscribing_twice_is_a_no_op(client, reader):
    reader.weekly_digest_opt_out = True
    reader.save(update_fields=["weekly_digest_opt_out"])

    response = client.post(_url(unsub.make_token(reader)))

    assert response.status_code == 200
    assert "Unsubscribed." in response.content.decode()
    reader.refresh_from_db()
    assert reader.weekly_digest_opt_out is True


@pytest.mark.django_db
def test_a_tampered_link_is_a_400_not_a_silent_success(client, reader):
    token = unsub.make_token(reader)
    forged = token[:-1] + ("a" if token[-1] != "a" else "b")

    response = client.post(_url(forged))

    assert response.status_code == 400
    reader.refresh_from_db()
    assert reader.weekly_digest_opt_out is False


@pytest.mark.django_db
def test_a_link_for_a_deleted_account_is_refused_kindly(client, reader):
    token = unsub.make_token(reader)
    reader.delete()

    response = client.post(_url(token))

    assert response.status_code == 400
    assert "no longer exists" in response.content.decode()


@pytest.mark.django_db
def test_the_link_writes_the_flag_the_settings_toggle_reads(client, reader):
    """One column, one meaning. After an unsubscribe, the Settings checkbox
    has to render unticked, or the two surfaces are telling the student
    different things about the same preference."""
    from accounts.forms import NotificationsForm

    client.post(_url(unsub.make_token(reader)))
    reader.refresh_from_db()

    assert NotificationsForm.initial_for(reader) == {"weekly_digest_enabled": False}


@pytest.mark.django_db
def test_the_toggle_can_turn_it_back_on(client, reader):
    """"Turn it back on in Settings" has to be true."""
    from accounts.forms import NotificationsForm

    client.post(_url(unsub.make_token(reader)))
    reader.refresh_from_db()

    form = NotificationsForm({"section": "notifications", "weekly_digest_enabled": "on"})
    assert form.is_valid(), form.errors
    form.apply_to(reader)
    reader.refresh_from_db()
    assert reader.weekly_digest_opt_out is False
