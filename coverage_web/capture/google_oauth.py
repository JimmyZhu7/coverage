"""The two pieces of Google OAuth that Gmail Live and Calendar genuinely
share: building a `Flow` for a consent round-trip, and building refreshed
`Credentials` from a stored refresh token.

WHY THIS FILE IS SO SMALL. The temptation when adding a second Google
integration is to lift the whole of `gmail_live`'s credential half into a
"google" module and have both sides import it. That would move working,
heavily-commented code — token encryption, the connect flow's error
translation, the watch registration — for the sake of symmetry, and every
line moved is a line whose tests now cover a different file than the one
they were written against. So only two things live here, and they are the
two that are byte-for-byte identical between the integrations once the
scope list is a parameter.

WHAT DELIBERATELY DID NOT MOVE:

* **Token encryption.** `gmail_live.encrypt_token` / `decrypt_token` /
  `token_keys` stay where they are and Calendar imports them, which is
  exactly what `google_revoke.py` already does and says out loud
  ("Decryption is `gmail_live`'s, not a second implementation of it"). One
  Fernet key encrypts both grants; one function reads it.
* **Error types.** Nothing here raises. `decrypt_token` raises
  `GmailLiveError` and `capture.views.gmail_callback` catches exactly that
  type — a shared module raising a shared error class would either widen
  that catch or silently escape it. Each integration keeps its own
  exception and its own translation of Google's failures into sentences a
  student can act on.

PKCE IS OFF FOR BOTH, and the reasoning is `gmail_live._flow`'s: the
authorization request and the token exchange happen in two separate HTTP
requests with a browser round-trip through Google in between, so nothing
on a `Flow` instance survives from one to the other. With
`autogenerate_code_verifier` at its library default of True, each fresh
instance mints its own verifier and the exchange fails with
"invalid_grant: Missing code verifier". These are confidential web clients
holding a real secret, and CSRF is covered by `state`.
"""

from __future__ import annotations

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

# Google's own endpoints, not per-integration. Named here once so a second
# integration cannot quietly drift onto a different token URI than the
# first.
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"


def flow(*, client_id: str, client_secret: str, scopes: list[str], redirect_uri: str) -> Flow:
    """A fresh `Flow` for one consent round-trip. See the module docstring
    on why `autogenerate_code_verifier` must stay off."""
    return Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
            }
        },
        scopes=scopes,
        redirect_uri=redirect_uri,
        autogenerate_code_verifier=False,
    )


def auth_url(
    *, client_id: str, client_secret: str, scopes: list[str], redirect_uri: str, state: str
) -> str:
    """The URL to send a user to for a consent screen.

    `access_type="offline"` + `prompt="consent"` is the pair that guarantees
    a refresh token comes back. Google issues one only on the FIRST consent
    unless the screen is forced again, and a connect flow that silently gets
    no refresh token is one that silently stops working the moment its
    short-lived access token expires.

    `include_granted_scopes="false"` is what keeps the two grants apart.
    Google's incremental authorisation would otherwise fold every scope the
    user has already given this client into the new token — so connecting
    the calendar would hand back a credential that also reads mail, and a
    Gmail reconnect would silently pick the calendar back up after a
    disconnect. Each grant asks for its own scope and gets exactly that.
    """
    built = flow(
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        redirect_uri=redirect_uri,
    )
    url, _ = built.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
        include_granted_scopes="false",
    )
    return url


def credentials(
    *, client_id: str, client_secret: str, refresh_token: str, scopes: list[str]
) -> Credentials:
    """Exchange a stored refresh token for a live access token.

    `token=None` on purpose: access tokens are never stored by this project
    — they last an hour and a stored one is a secret with no upside — so
    every call starts from the refresh token and gets a fresh one. The
    `refresh()` here is what turns a revoked grant into a loud error at the
    top of a sync rather than a confusing 401 several calls in.
    """
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )
    creds.refresh(GoogleAuthRequest())
    return creds
