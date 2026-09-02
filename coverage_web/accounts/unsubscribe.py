"""One-click unsubscribe from the weekly digest, for a reader who is holding
an email and not a session.

WHY THIS LIVES IN `accounts/` AND NOT IN `crm/`, WHERE THE DIGEST IS BUILT.
The thing being turned off is `User.weekly_digest_opt_out`, an accounts
column, and it already has exactly one writer: `accounts.forms.
NotificationsForm`, whose docstring spells out the one translation the product
makes ("a checkbox next to Weekly Email Digest has to mean SEND IT when
checked ... this form is the one place that translation happens"). An
unsubscribe endpoint in `crm/` would be a second writer of that column in a
second app, which is precisely the "two definitions of one fact" this
codebase keeps getting bitten by (P5). It is one field, so it gets one owner,
and `apply_flag` below is the only line outside that form that sets it.

WHY A SIGNED TOKEN AND NOT A SESSION. An unsubscribe link is opened from an
inbox, often on a phone that has never signed in to Coverage. Requiring a
login to stop email is the pattern every student has been burned by. It is
also why the token carries nothing but the user id and a timestamp: it grants
one specific, reversible, non-destructive action and no read access to
anything.

WHY POST DOES THE WRITE. Mail clients, security scanners and link previewers
fetch every URL in a message. A GET that flipped the flag would silently
unsubscribe people whose corporate mail gateway is doing its job. So GET
renders a one-line confirm page and POST does the work, which is the same
"consequential actions get their own confirm page" rule accounts/views.py
already follows for sign-out-everywhere and account deletion.
"""

from __future__ import annotations

from django.core import signing

# The salt scopes these signatures to this one purpose: a token minted here
# cannot be replayed against any other signer in the project, and vice versa.
SALT = "accounts.digest-unsubscribe"

# Long on purpose. A digest arrives weekly and sits in an inbox; a student
# who finally gets round to unsubscribing from a mail sent last term should
# not meet an error page, because the alternative to a working link is
# marking Coverage as spam. 400 days covers a full recruiting cycle plus the
# gap to the next one. The token is still bounded, so a link scraped out of a
# forwarded mailbox does not stay live forever.
MAX_AGE_SECONDS = 400 * 24 * 60 * 60


class BadToken(Exception):
    """The token was tampered with, malformed, or older than MAX_AGE_SECONDS."""


def make_token(user) -> str:
    """A URL-safe token naming `user`, signed and timestamped."""
    return signing.TimestampSigner(salt=SALT).sign(str(user.pk))


def read_token(token: str) -> int:
    """The user id inside `token`, or `BadToken`.

    Every failure mode collapses to one exception on purpose: a caller that
    could tell "expired" from "forged" from "no such id" would be an oracle
    for probing the signing key.
    """
    try:
        raw = signing.TimestampSigner(salt=SALT).unsign(
            token, max_age=MAX_AGE_SECONDS
        )
        return int(raw)
    except (signing.BadSignature, signing.SignatureExpired, ValueError, TypeError) as exc:
        raise BadToken(str(exc)) from exc


def apply_flag(user) -> bool:
    """Turn the digest off for `user`. True if this call changed anything.

    Idempotent by construction: a second click on the same link (or a scanner
    that follows the confirm form) writes nothing and still lands the reader
    on the same "you are unsubscribed" page. Nothing about the outcome
    depends on how many times it ran.
    """
    if user.weekly_digest_opt_out:
        return False
    user.weekly_digest_opt_out = True
    user.save(update_fields=["weekly_digest_opt_out"])
    return True
