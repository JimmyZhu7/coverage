"""Hand a Gmail grant back to Google when the user is done with it.

WHY THIS EXISTS. Disconnecting Gmail, and deleting a Coverage account
outright, both used to delete the encrypted refresh-token row and stop
there. `capture/views.py`'s own docstring argued the case: Google already
gives the user a better place to revoke (myaccount.google.com/permissions),
and a second call is a second thing that can silently fail. That reasoning
holds for a button in a settings page. It does not hold for what the two
actions PROMISE. "Disconnect" reads as "the grant is gone", and account
deletion is documented in the privacy policy as a hard delete with nothing
kept — while the OAuth grant stayed live at Google, indefinitely, for an
account that no longer exists on this side. A grant nobody can see and
nobody can use is exactly the kind of thing that surfaces years later.

BEST-EFFORT, DELIBERATELY. Every function here returns rather than raises.
The stored row is being deleted either way, and a network blip at Google
must not be able to leave a student unable to disconnect or unable to
delete their account. A failed revoke is logged and the user's own
myaccount.google.com control remains the backstop it always was — the
difference is that it is now the backstop rather than the only path.

NOTHING HERE LOGS A TOKEN. The revoke endpoint takes the token as a POST
body parameter, not a query string, so it does not reach an access log
either; the log lines below name the connection's id and the HTTP status
and nothing else.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

# Google's OAuth 2.0 revocation endpoint (RFC 7009). Revoking a REFRESH
# token invalidates the whole grant — every access token minted from it
# included — which is the point: the access tokens this app holds are
# short-lived and never stored, so revoking one of those would achieve
# nothing.
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Short on purpose. This runs inline in a request that the user is waiting
# on (a disconnect POST, an account deletion), and the work it guards is
# already done by the time it matters.
TIMEOUT_SECONDS = 5


def revoke_token(raw_token: str) -> bool:
    """POST one refresh token to Google's revoke endpoint. Never raises.

    Returns True when Google confirmed the revocation. Google answers 200
    for a token it revoked and 400 for one that is already invalid — both
    mean "this grant is not live any more", so both count as success here;
    a 400 is the ordinary answer for a token the user already revoked in
    their Google account, and treating that as a failure would log noise
    for the outcome we wanted.
    """
    if not raw_token:
        return False
    try:
        resp = requests.post(
            REVOKE_URL,
            data={"token": raw_token},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.warning("Gmail token revoke failed to reach Google: %s", exc)
        return False
    if resp.status_code in (200, 400):
        return True
    logger.warning("Gmail token revoke returned HTTP %s", resp.status_code)
    return False


def revoke_connection(connection) -> bool:
    """Revoke the grant behind one `capture.models.GmailConnection`.

    Decryption is `gmail_live`'s, not a second implementation of it — but
    it is imported here rather than at module scope so that importing this
    module costs nothing on a deploy with Gmail Live switched off, and so a
    malformed `GMAIL_LIVE_TOKEN_KEY` (which `decrypt_token` raises
    `GmailLiveError` for, loudly and correctly) cannot turn "disconnect" or
    "delete my account" into a 500.
    """
    ciphertext = getattr(connection, "refresh_token_encrypted", "") or ""
    if not ciphertext:
        return False
    from capture import gmail_live

    try:
        raw = gmail_live.decrypt_token(ciphertext)
    except Exception as exc:  # noqa: BLE001 — see the docstring: never fatal here
        logger.warning(
            "Gmail token revoke skipped for connection %s: token unreadable (%s)",
            getattr(connection, "pk", "?"),
            exc,
        )
        return False
    return revoke_token(raw)


def revoke_all_for_user(user) -> int:
    """Revoke every Gmail grant this user holds. Returns how many succeeded.

    `all_objects` with an explicit `user=` filter, the same shape every
    other worker-side read of this table uses: `GmailConnection` is a
    private-zone model and this is called from paths (account deletion)
    where the caller already holds the user object.
    """
    from capture.models import GmailConnection

    revoked = 0
    for connection in GmailConnection.all_objects.filter(user=user):
        if revoke_connection(connection):
            revoked += 1
    return revoked
