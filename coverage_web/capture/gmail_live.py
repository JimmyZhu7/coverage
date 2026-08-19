"""Gmail Live — docs/build-plan.md §5's "v2": real Gmail API access.

WHY THIS FILE EXISTS SEPARATELY FROM gmail.py
----------------------------------------------
`capture.gmail.apply_findings` already IS the tested apply layer — ratchet,
dedup, calendar events, pattern stats, all of it. This module's only job is
to produce the SAME finding-dict shape `GmailFindingsProvider` already
consumes, but from a live Gmail API notification instead of a human-run
search agent. Every finding this module builds ends its life the same way a
finding from the daily manual sync does: a call to `apply_findings`. That is
deliberate — it means the one thing that has been running correctly in
production (the daily sync) stays the single source of truth for "what does
this evidence do to a contact," and this module's only responsibility is
"turn a Gmail message into evidence."

WHAT THIS MODULE DOES **NOT** DO, ON PURPOSE
---------------------------------------------
1. **No LLM classification.** §5 describes a "residue" path where ambiguous
   free text goes to an LLM classifier. This module only implements the
   DETERMINISTIC extractors from that same section: bounce-pattern matching,
   direction from headers, and calendar-invite (.ics) parsing for
   `chat_scheduled_at`. A message that only says "great chatting yesterday!"
   in prose, with no .ics anywhere, will NOT be recognised as a completed
   chat by this pipeline — that residue still needs the daily agent-run sync
   (or a future, explicitly-scoped LLM-residue pass) to catch. Silently
   guessing here is worse than leaving it for the sync that already exists.
2. **No new-contact creation.** Mirrors `capture_gmail` (Step 1 of the daily
   sync), not `capture_discover` (Step 2). `apply_findings` only logs
   touches against contacts ALREADY in Coverage; a message from someone not
   yet a contact is a harmless no-op (`skipped_unmatched`). Bringing
   USC-discovery-style new-contact creation onto a live, per-message trigger
   is a materially different scope decision (it write-creates data on every
   unknown sender) and was not asked for here.
3. **No push/webhook endpoint.** `users.watch()` needs a Pub/Sub topic, but
   the *subscription* on that topic is a PULL subscription
   (`gmail_pubsub_listen` below), not a push endpoint — deliberately, so this
   whole feature works before Coverage has a public HTTPS deploy at all.

SETUP THIS MODULE ASSUMES ALREADY HAPPENED (docs/gmail-live-setup.md)
-----------------------------------------------------------------------
A second Google Cloud OAuth client (never the login one — see §3 and the
settings module), the Gmail + Pub/Sub APIs enabled, one Pub/Sub topic granting
publish rights to `gmail-api-push@system.gserviceaccount.com`, and a pull
subscription on that topic. `is_configured()` is the runtime gate: every
entry point below no-ops (rather than 500s) until all four settings are set.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from email.utils import parseaddr

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db.models import Max, Q
from django.utils import timezone
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from billing import credits as billing_credits
from capture import gmail_residue
from capture.gmail import apply_findings
from capture.models import GmailConnection
from crm.models import Contact, Touch

# First-connect backfill (see backfill_connection): 365 days for a contact
# with zero prior touches, matching the daily sync's own capture_worklist
# precedent — a narrow window can't distinguish "never contacted" from
# "contacted before this contact was added to Coverage." A touched contact
# only needs back to last_touch (plus a week of overlap), capped so one
# very old contact can't turn the whole backfill into a full-mailbox scan.
BACKFILL_ZERO_TOUCH_DAYS = 365
BACKFILL_TOUCHED_WINDOW_DAYS = 90
BACKFILL_OVERLAP_DAYS = 7

logger = logging.getLogger(__name__)

GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"

# Same bounce-pattern vocabulary the manual daily sync's agents already use
# (see daily-networking-gmail-sync's SKILL.md) — kept identical so the two
# pipelines agree on what "bounced" means.
_BOUNCE_FROM_RE = re.compile(r"mailer-daemon|postmaster", re.IGNORECASE)
_BOUNCE_SUBJECT_RE = re.compile(
    r"delivery status notification|undeliverable|delivery has failed",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ICS_DTSTART_RE = re.compile(r"DTSTART(?:;TZID=([^:]+))?:(\d{8}T\d{6}Z?)")
_ICS_SUMMARY_RE = re.compile(r"SUMMARY:(.+)")


class GmailLiveError(Exception):
    """Raised for conditions the caller must react to (e.g. a revoked
    grant), as opposed to a single message this module just skips."""


def is_configured() -> bool:
    """The feature's actual off-switch — see the settings module's comment
    on why this gates rather than each entry point checking settings itself."""
    return bool(
        settings.GMAIL_LIVE_CLIENT_ID
        and settings.GMAIL_LIVE_CLIENT_SECRET
        and settings.GMAIL_LIVE_PUBSUB_TOPIC
        and settings.GMAIL_LIVE_TOKEN_KEY
    )


# ---------------------------------------------------------------------------
# Token encryption
# ---------------------------------------------------------------------------

def _fernet() -> Fernet:
    # `is_configured()` only checks this setting is non-EMPTY — it cannot
    # check it is a valid key without constructing a Fernet, which is what
    # this is. A key that isn't 32 url-safe-base64 bytes (truncated on a
    # copy-paste, generated with the wrong tool, quoted with the quotes
    # included) makes Fernet raise a bare `ValueError` from whichever call
    # site happened to encrypt or decrypt first — and the OAuth callback
    # only catches `GmailLiveError`, so that surfaces as a blank 500 page
    # rather than "your key is malformed."
    try:
        return Fernet(settings.GMAIL_LIVE_TOKEN_KEY.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise GmailLiveError(
            "GMAIL_LIVE_TOKEN_KEY is not a valid Fernet key — it must be the "
            "exact 44-character output of Fernet.generate_key(), with no "
            "quotes or truncation. Regenerate it and set it on every service "
            "that talks to Gmail."
        ) from exc


def encrypt_token(raw: str) -> str:
    return _fernet().encrypt(raw.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        # Wrong/rotated GMAIL_LIVE_TOKEN_KEY. Not a per-user problem — every
        # row is unreadable at once — so this is a config error, not
        # something a single reconnect fixes. Raise loudly rather than
        # silently treating one user as "revoked".
        raise GmailLiveError(
            "GMAIL_LIVE_TOKEN_KEY cannot decrypt a stored refresh token — "
            "has the key changed since it was encrypted?"
        ) from exc


# ---------------------------------------------------------------------------
# OAuth connect flow (a SEPARATE client from login — see §3)
# ---------------------------------------------------------------------------

def _flow(redirect_uri: str) -> Flow:
    """A fresh `Flow` per request — `build_auth_url` and `connect_gmail` each
    call this from a SEPARATE HTTP request (the browser round-trips through
    Google in between), so nothing on a `Flow` instance survives from one
    call to the other. `autogenerate_code_verifier` must stay off because of
    that: its default (True, as of google-auth-oauthlib's current release)
    has each fresh instance mint its own PKCE code_verifier, so the one
    `connect_gmail` generates for the token exchange never matches the
    code_challenge that `build_auth_url` already sent Google — every
    exchange then fails with "invalid_grant: Missing code verifier". PKCE
    exists to protect public clients that can't hold a secret; this is a
    confidential "web" client with a real client_secret, and CSRF is already
    covered by the `state` param, so there's nothing PKCE adds here worth
    the cost of persisting a verifier across the redirect."""
    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GMAIL_LIVE_CLIENT_ID,
                "client_secret": settings.GMAIL_LIVE_CLIENT_SECRET,
                "auth_uri": GMAIL_AUTH_URI,
                "token_uri": GMAIL_TOKEN_URI,
            }
        },
        scopes=settings.GMAIL_LIVE_SCOPES,
        redirect_uri=redirect_uri,
        autogenerate_code_verifier=False,
    )


def build_auth_url(redirect_uri: str, state: str) -> str:
    """The URL to send a user to for the Gmail-read consent screen.

    `access_type="offline"` + `prompt="consent"` is the pair that guarantees
    a refresh token comes back — Google only issues one on the FIRST consent
    unless you force the consent screen again, and a connect flow that
    silently gets no refresh token is a flow that silently stops working the
    moment the short-lived access token expires.
    """
    flow = _flow(redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
        include_granted_scopes="false",
    )
    return auth_url


def connect_gmail(user, code: str, redirect_uri: str) -> GmailConnection:
    """Exchange the consent code for tokens, register the watch, and store
    (or update) this user's `GmailConnection`.

    Raises `GmailLiveError` — and ONLY `GmailLiveError` — on anything that
    leaves the user without a working connection. That is not a stylistic
    preference: `capture.views.gmail_callback` catches exactly this type and
    nothing else, so any other exception escaping here renders the generic
    500 page ("Something broke on our side") for what is, every time, a
    fixable Google-console setting the user needs to be told about. Each
    `except` below exists because of one such escape.

    A failure to register the `users.watch()` is deliberately NOT fatal —
    see the comment at that call.
    """
    flow = _flow(redirect_uri)
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 - surfaced as GmailLiveError below
        raise GmailLiveError(f"Google rejected the consent code: {exc}") from exc

    creds = flow.credentials
    if not creds.refresh_token:
        # Happens if the user has an existing, un-revoked grant and Google
        # decided the "prompt=consent" above still didn't warrant reissuing
        # one. Nothing to store without it — reconnecting after revoking
        # access in the Google Account is the user-facing fix.
        raise GmailLiveError(
            "Google did not return a refresh token — revoke Coverage's "
            "access at https://myaccount.google.com/permissions and "
            "reconnect."
        )

    # The FIRST real call against the Gmail API on this project. It fails
    # loudly and specifically when a docs/gmail-live-setup.md step was
    # missed — most often a 403 "Gmail API has not been used in project N
    # before or it is disabled", which is a `HttpError`, not a
    # `GmailLiveError`, and so used to escape this function entirely and
    # render the generic 500 page. Translate it: the user's fix is a
    # console setting, and they can only apply it if they are told.
    try:
        gmail = build("gmail", "v1", credentials=creds)
        profile = gmail.users().getProfile(userId="me").execute()
    except Exception as exc:  # noqa: BLE001 - surfaced as GmailLiveError below
        raise GmailLiveError(
            f"Google refused to read the mailbox profile: {exc}. Check that "
            "the Gmail API is enabled on this OAuth client's Cloud project "
            "and that your account is on the app's test-user list."
        ) from exc

    # A completed backfill must not re-run just because the user
    # disconnected and reconnected the same mailbox — but a reconnect after
    # a REVOKED grant (backfill_status left at whatever it was, possibly
    # "failed" mid-run) should still get one. Only "done" is sticky.
    existing = GmailConnection.all_objects.filter(user=user).first()
    backfill_status = "done" if existing and existing.backfill_status == "done" else "pending"

    connection, _ = GmailConnection.all_objects.update_or_create(
        user=user,
        defaults={
            "gmail_address": profile["emailAddress"],
            "refresh_token_encrypted": encrypt_token(creds.refresh_token),
            "history_id": str(profile["historyId"]),
            "status": "active",
            "backfill_status": backfill_status,
        },
    )

    # The connection is now STORED and, on its own, complete: the refresh
    # token works, the backfill is queued, and the twice-daily agent sync is
    # unaffected. `users.watch()` only adds real-time push on top of that,
    # and it is the one piece of a connection that already repairs itself —
    # `renew_watches()` re-registers every active row whose
    # `watch_expiration` is null, daily.
    #
    # Real-time sync is Pro-only (docs/pricing-rebalance-plan.md §7): a Free
    # user's connection is already complete without it — the refresh token
    # works, the backfill above is queued, and Scan Now (capture/views.py::
    # gmail_rescan) is open to every plan. `renew_watches()` mirrors this
    # same `user.plan == "pro"` gate for every later daily tick, so a Free
    # connection simply never gets real-time turned on rather than having it
    # granted here and revoked later.
    #
    # For a Pro user, a watch failure must not be allowed to escape. There
    # are no ATOMIC_REQUESTS on this project, meaning the row above is
    # already committed by the time this runs: letting `register_watch`
    # raise produced the worst possible outcome — a generic 500 page for a
    # mailbox that WAS, in fact, connected. And `register_watch` re-raises
    # anything that isn't a 401/403, which covers the most likely
    # first-connect failure of all: a 400 "Error sending test message to
    # Cloud PubSub ... User not authorized" when the topic hasn't granted
    # publish rights to gmail-api-push@system.gserviceaccount.com, or a 404
    # when GMAIL_LIVE_PUBSUB_TOPIC names a topic that doesn't exist. Both are
    # config, both are fixable, and neither is a reason to throw the
    # connection away.
    if user.plan == "pro":
        try:
            register_watch(connection)
        except Exception:  # noqa: BLE001 - a watch is retried daily; a connect isn't
            logger.exception(
                "Gmail Live: connected %s but users.watch() failed — real-time "
                "notifications stay off until gmail_watch_renew succeeds. Check "
                "GMAIL_LIVE_PUBSUB_TOPIC exists and grants publish rights to "
                "gmail-api-push@system.gserviceaccount.com.",
                connection.gmail_address,
            )
    return connection


# ---------------------------------------------------------------------------
# Credentials + watch registration
# ---------------------------------------------------------------------------

def _credentials(connection: GmailConnection) -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=decrypt_token(connection.refresh_token_encrypted),
        token_uri=GMAIL_TOKEN_URI,
        client_id=settings.GMAIL_LIVE_CLIENT_ID,
        client_secret=settings.GMAIL_LIVE_CLIENT_SECRET,
        scopes=settings.GMAIL_LIVE_SCOPES,
    )
    creds.refresh(GoogleAuthRequest())
    return creds


def _gmail_client(connection: GmailConnection):
    return build("gmail", "v1", credentials=_credentials(connection))


def register_watch(connection: GmailConnection) -> None:
    """(Re-)register the 7-day `users.watch()` notification. Marks the
    connection `revoked` (rather than raising) when Google reports the grant
    itself is gone — the one failure mode that needs the USER to act, as
    opposed to a transient error `gmail_watch_renew` should just retry."""
    try:
        gmail = _gmail_client(connection)
        response = gmail.users().watch(
            userId="me",
            body={"topicName": settings.GMAIL_LIVE_PUBSUB_TOPIC, "labelIds": ["INBOX"]},
        ).execute()
    except HttpError as exc:
        if exc.resp.status in (401, 403):
            connection.status = "revoked"
            connection.save(update_fields=["status"])
            return
        raise

    # historyId as of watch registration — anchors the next history.list.
    connection.history_id = str(response["historyId"])
    connection.watch_expiration = datetime.fromtimestamp(
        int(response["expiration"]) / 1000, tz=dt_timezone.utc
    )
    connection.status = "active"
    connection.save(update_fields=["history_id", "watch_expiration", "status"])


def renew_watches() -> tuple[int, int]:
    """Re-registers any connection whose watch expires within a day (or has
    none yet). Returns (renewed, revoked) for the calling command to report.

    `user__plan="pro"` mirrors `connect_gmail`'s own gate: real-time sync is
    Pro-only (docs/pricing-rebalance-plan.md §7), so a Free connection is
    simply never picked up here. That is the whole mechanism by which a
    downgraded (or trial-expired) Pro connection loses real-time honestly —
    nothing deletes or disconnects it, this query just stops renewing its
    watch, and Google's own 7-day expiry does the rest.
    """
    soon = timezone.now() + timedelta(days=1)
    due = GmailConnection.all_objects.filter(status="active", user__plan="pro").filter(
        Q(watch_expiration__isnull=True) | Q(watch_expiration__lte=soon)
    )
    renewed = revoked = 0
    for connection in due:
        register_watch(connection)
        connection.refresh_from_db()
        if connection.status == "revoked":
            revoked += 1
        else:
            renewed += 1
    return renewed, revoked


# ---------------------------------------------------------------------------
# Notification -> findings -> apply_findings
# ---------------------------------------------------------------------------

def process_notification(gmail_address: str, published_history_id: str) -> None:
    """Entry point for one Pub/Sub notification. Looks up the connection by
    the mailbox address the notification names (Gmail's payload, not
    anything we control) and syncs it."""
    try:
        connection = GmailConnection.all_objects.select_related("user").get(
            gmail_address=gmail_address, status="active"
        )
    except GmailConnection.DoesNotExist:
        return  # disconnected or unknown mailbox — nothing to do

    # Defensive drop, not the primary gate: neither `connect_gmail` nor
    # `renew_watches` ever registers a watch for a non-Pro connection, so a
    # live notification arriving here for one means the plan changed AFTER
    # the watch was already live with Google (a trial that just expired, or
    # a manual downgrade) — Google's push can keep arriving for up to the
    # watch's remaining 7-day life. Drop rather than sync so a downgraded
    # account doesn't keep getting real-time coverage until the stale watch
    # itself finally expires.
    if connection.user.plan != "pro":
        return
    sync_connection(connection)


def sync_connection(connection: GmailConnection) -> None:
    gmail = _gmail_client(connection)
    start_id = connection.history_id or None

    try:
        message_ids, latest_history_id = _list_new_messages(gmail, start_id)
    except HttpError as exc:
        if exc.resp.status == 404:
            # startHistoryId too old (Gmail's own history retention window
            # passed, ~7 days). No correct incremental answer exists — the
            # gap is real lost coverage, but re-scanning "everything since
            # the beginning" on every notification would be worse. Re-anchor
            # to now; the twice-daily agent-run sync is the backstop for
            # whatever fell in the gap.
            profile = gmail.users().getProfile(userId="me").execute()
            connection.history_id = str(profile["historyId"])
            connection.save(update_fields=["history_id"])
            return
        raise

    findings = []
    for message_id in message_ids:
        message = gmail.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        finding = _classify_message(connection.gmail_address, message)
        if finding is not None:
            findings.append(finding)

    if findings:
        apply_findings(connection.user, findings)

    connection.history_id = latest_history_id or connection.history_id
    connection.last_notification_at = timezone.now()
    connection.save(update_fields=["history_id", "last_notification_at"])


def _list_new_messages(gmail, start_history_id) -> tuple[list[str], str | None]:
    """All `messagesAdded` ids since `start_history_id`, paging through
    `history.list`, plus the newest `historyId` to resume from next time."""
    if start_history_id is None:
        return [], None

    message_ids: list[str] = []
    latest = start_history_id
    page_token = None
    while True:
        response = gmail.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            pageToken=page_token,
        ).execute()
        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                message_ids.append(added["message"]["id"])
        latest = response.get("historyId", latest)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return message_ids, latest


def _header(message: dict, name: str) -> str:
    for header in message.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _walk_parts(payload: dict):
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _walk_parts(part)


def _decode_body(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", errors="replace"
        )
    except Exception:  # noqa: BLE001 - a malformed part is not fatal
        return ""


def _extract_ics_schedule(message: dict) -> tuple[str | None, str | None]:
    """(iso_datetime, summary) from the first `.ics`/`text/calendar` part
    found, or (None, None). Deterministic per §5 ("scheduling via .ics ...
    detection") — no language inference, just the invite's own DTSTART."""
    for part in _walk_parts(message.get("payload", {})):
        mime = part.get("mimeType", "")
        filename = part.get("filename", "")
        if mime != "text/calendar" and not filename.endswith(".ics"):
            continue
        text = _decode_body(part)
        dt_match = _ICS_DTSTART_RE.search(text)
        if not dt_match:
            continue
        tzid, raw = dt_match.groups()
        try:
            if raw.endswith("Z"):
                dt = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=dt_timezone.utc
                )
            else:
                # A floating or TZID-qualified time with no offset info we
                # can resolve without a full tz database lookup by name —
                # stored naive, same as the manual sync's own documented
                # fallback (anchors to the account owner's User.timezone).
                dt = datetime.strptime(raw, "%Y%m%dT%H%M%S")
        except ValueError:
            continue
        summary_match = _ICS_SUMMARY_RE.search(text)
        summary = summary_match.group(1).strip() if summary_match else ""
        return dt.isoformat(), summary
    return None, None


def _looks_like_bounce(from_addr: str, subject: str) -> bool:
    """True only from the FROM address or the SUBJECT — never from body text
    alone. A genuine, personal reply can easily QUOTE bounce-style wording
    ("I tried your old address and got 'recipient address rejected: 550
    5.1.1'...") about a completely different address, or about nothing at
    all still relevant; matching that against the body would misclassify
    the reply itself as a bounce of whichever address `_bounce_recipient`
    happens to find next in the text — including, in the worst case, the
    real contact's own working address, since nothing excludes the
    sender's own `From` from that scan. A real, system-generated bounce
    reliably carries a mailer-daemon/postmaster sender or a DSN-style
    subject, so requiring one of those two costs no real detections."""
    return bool(_BOUNCE_FROM_RE.search(from_addr) or _BOUNCE_SUBJECT_RE.search(subject))


def _bounce_recipient(message: dict, own_email: str) -> str | None:
    """Best-effort: the failed address a bounce body names, by scanning its
    text for the first address that isn't the account owner's or a mail-
    system address. Bounces vary a lot in format; this is the same
    reasonable heuristic the manual sync's agents already apply by reading
    the body, just as a regex instead of judgment.

    The snippet is always included in the text scanned, not only when a
    `text/*` part turns up — Gmail's own top-level payload frequently has no
    `mimeType` of its own at all (it sits on a nested part, or is absent for
    a message with no distinct body part), and a bounce with no text/* part
    found would otherwise never fall back to the one text Gmail always
    supplies.
    """
    own = own_email.lower()
    text_chunks = [message.get("snippet", "")]
    for part in _walk_parts(message.get("payload", {})):
        if part.get("mimeType", "").startswith("text/"):
            decoded = _decode_body(part)
            if decoded:
                text_chunks.append(decoded)
    for candidate in _EMAIL_RE.findall("\n".join(text_chunks)):
        low = candidate.lower()
        if low != own and not _BOUNCE_FROM_RE.search(low):
            return low
    return None


def _message_occurred_at(message: dict) -> str | None:
    """Gmail's own `internalDate` (epoch milliseconds, as a string — the
    API's actual format) as an ISO 8601 string, or None if absent/garbled.
    This is what makes a real message time flow through to `log_touch`
    instead of "whenever the sync happened to run" — load-bearing for the
    backfill command, which applies findings months after they occurred."""
    raw = message.get("internalDate")
    if not raw:
        return None
    try:
        millis = int(raw)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=dt_timezone.utc).isoformat()


def _classify_message(own_email: str, message: dict) -> dict | None:
    """One Gmail message -> one finding dict in `GmailFindingsProvider`'s
    shape, or None if there's nothing worth reporting (e.g. a message from
    the account owner to themselves, or an address this heuristic can't
    resolve at all)."""
    from_name, from_addr = parseaddr(_header(message, "From"))
    to_name, to_addr = parseaddr(_header(message, "To"))
    subject = _header(message, "Subject")
    thread_id = message.get("threadId", "")
    ics_dt, ics_summary = _extract_ics_schedule(message)
    occurred_at = _message_occurred_at(message)

    is_outbound = from_addr.lower() == own_email.lower()

    if is_outbound:
        if not to_addr:
            return None
        return {
            "name": to_name or to_addr.split("@")[0],
            "email": to_addr.lower(),
            "found": True,
            "bounced": False,
            "outreach_sent": True,
            "replied": False,
            "chat_status": "scheduled" if ics_dt else "none",
            "chat_scheduled_at": ics_dt,
            "evidence": (
                f"Calendar invite sent: {ics_summary or subject}"
                if ics_dt
                else f"Sent: {subject}"
            ),
            "thread_id": thread_id,
            "occurred_at": occurred_at,
        }

    if _looks_like_bounce(from_addr, subject):
        recipient = _bounce_recipient(message, own_email)
        if not recipient:
            return None
        return {
            "name": recipient.split("@")[0],
            "email": recipient,
            "found": True,
            "bounced": True,
            "outreach_sent": False,
            "replied": False,
            "chat_status": "none",
            "chat_scheduled_at": None,
            "evidence": f"Bounced: {subject}",
            "thread_id": thread_id,
            "occurred_at": occurred_at,
        }

    if not from_addr:
        return None
    return {
        "name": from_name or from_addr.split("@")[0],
        "email": from_addr.lower(),
        "found": True,
        "bounced": False,
        "outreach_sent": False,
        "replied": True,
        "chat_status": "scheduled" if ics_dt else "none",
        "chat_scheduled_at": ics_dt,
        "evidence": (
            f"Calendar invite received: {ics_summary or subject}"
            if ics_dt
            else message.get("snippet", "")[:300]
        ),
        "thread_id": thread_id,
        "occurred_at": occurred_at,
    }


# ---------------------------------------------------------------------------
# First-connect historical backfill
# ---------------------------------------------------------------------------
#
# Deliberately mirrors capture_gmail (Step 1 of the daily sync) rather than
# capture_discover (Step 2): this fills in history for contacts ALREADY in
# Coverage and never creates new ones. Per-contact search is what makes that
# the free choice, not just the cautious one — a search scoped to
# `from:X OR to:X` can only ever return mail involving someone already
# tracked, so there is no scan-the-whole-mailbox cost to weigh against
# "could this discover someone new" (it can't, by construction).
#
# Runs as a scheduled command (gmail_backfill), never inline in the OAuth
# callback — a year of history is a multi-minute job, and connect_gmail's
# redirect must stay instant. See that command for the scheduling contract.

def _suppress_stale_bounces(findings: list[dict]) -> list[dict]:
    """Drop a bounce finding for an address that a LATER finding in this
    same batch proves is actually deliverable. Only the historical scan can
    see both sides of this at once — the live path only ever sees "right
    now," so it has nothing to compare a bounce against. Without this, a
    contact who bounced once in March and has been replying since June
    would have their working address cleared by the backfill applying the
    March bounce after the fact.
    """
    latest_good_by_email: dict[str, str] = {}
    for finding in findings:
        if finding.get("replied") or finding.get("chat_status") in ("scheduled", "completed"):
            email = (finding.get("email") or "").lower()
            ts = finding.get("occurred_at") or ""
            if email and ts > latest_good_by_email.get(email, ""):
                latest_good_by_email[email] = ts

    kept = []
    for finding in findings:
        if finding.get("bounced"):
            email = (finding.get("email") or "").lower()
            bounce_ts = finding.get("occurred_at") or ""
            later_proof = latest_good_by_email.get(email, "")
            if later_proof and later_proof > bounce_ts:
                continue
        kept.append(finding)
    return kept


def _backfill_window_start(last_touch, *, now) -> "datetime":
    if last_touch is None:
        return now - timedelta(days=BACKFILL_ZERO_TOUCH_DAYS)
    floor = now - timedelta(days=BACKFILL_TOUCHED_WINDOW_DAYS)
    return max(last_touch - timedelta(days=BACKFILL_OVERLAP_DAYS), floor)


def backfill_connection(
    connection: GmailConnection,
    *,
    contacts=None,
    dry_run: bool = False,
    update_backfill_status: bool = True,
    residue_sink: list | None = None,
):
    """One historical pass over `contacts` (or, by default, every contact the
    user has). Searches per-contact (see module note above), classifies
    every message found with the SAME deterministic `_classify_message` the
    live path uses, then funnels everything through one `apply_findings`
    call — the identical ratchet/dedup contract the daily sync and the live
    listener both already rely on, so a message the live watcher already
    logged today is simply ratcheted away rather than double-counted.

    Findings are sorted ascending by `occurred_at` before applying, so the
    ladder climbs in the order things actually happened rather than
    whatever order the per-contact searches returned them in.

    `contacts`: an explicit iterable of `Contact` rows to scan, e.g. just the
    handful a CSV import created. Defaults to
    `Contact.objects.for_user(user).filter(archived=False).exclude(email="")`
    — every non-archived contact with an email — which is what the very
    first post-connect backfill (and a full "Scan Now" rescan) both want.

    `update_backfill_status`: whether this run should write
    `backfill_status`/`backfill_completed_at`/`backfill_stats` on
    `connection`. Defaults to True, which is exactly the original
    first-connect-backfill behavior (`gmail_backfill`'s pending/failed
    sweep). Callers running a narrower or repeatable scan — the
    import-triggered scoped scan, or a user-triggered "Scan Now" rescan —
    pass `False`, because `backfill_status` means specifically "has the
    ORIGINAL post-connect backfill ever completed" and must stay sticky at
    `done` regardless of how many other scans run afterward. Those callers
    are responsible for recording their own run's outcome wherever it
    belongs.

    `residue_sink`: when given a list, every message this pass could NOT
    confidently classify (`_classify_message` returned `None` — see
    `capture.gmail_residue`'s module docstring for exactly what that
    covers) is appended to it as `{"message": <raw message dict>,
    "thread_id": str}`. Defaults to `None`, which collects nothing — this
    is what keeps the free automatic first-connect backfill and Phase 1's
    import-triggered scan (neither passes a sink) entirely AI-free; only
    the "Scan Now" rescan command opts in, feeding the result to
    `gmail_residue.run_residue_stage` afterward.
    """
    gmail = _gmail_client(connection)
    user = connection.user
    own_email = connection.gmail_address.lower()
    now = timezone.now()

    if contacts is None:
        contacts = Contact.objects.for_user(user).filter(archived=False).exclude(email="")
    contacts = list(contacts)

    last_touch_by_contact = dict(
        Touch.objects.for_user(user)
        .values("contact_id")
        .annotate(last_ts=Max("ts"))
        .values_list("contact_id", "last_ts")
    )

    message_ids: set[str] = set()
    for contact in contacts:
        email = (contact.email or "").strip().lower()
        if not email or email == own_email:
            continue
        window_start = _backfill_window_start(
            last_touch_by_contact.get(contact.id), now=now
        )
        query = f"(from:{email} OR to:{email}) after:{window_start:%Y/%m/%d}"

        page_token = None
        while True:
            response = gmail.users().messages().list(
                userId="me", q=query, pageToken=page_token
            ).execute()
            for item in response.get("messages", []) or []:
                message_ids.add(item["id"])
            page_token = response.get("nextPageToken")
            if not page_token:
                break

    findings = []
    for message_id in message_ids:
        message = gmail.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        finding = _classify_message(own_email, message)
        if finding is not None:
            findings.append(finding)
        elif residue_sink is not None:
            residue_sink.append(
                {"message": message, "thread_id": message.get("threadId", "")}
            )

    findings = _suppress_stale_bounces(findings)
    findings.sort(key=lambda f: f.get("occurred_at") or "")

    result = apply_findings(user, findings, dry_run=dry_run)

    if not dry_run and update_backfill_status:
        connection.backfill_status = "done"
        connection.backfill_completed_at = timezone.now()
        connection.backfill_stats = result.as_stats()
        connection.save(
            update_fields=["backfill_status", "backfill_completed_at", "backfill_stats"]
        )

    return result


def backfill_new_contacts(user, contacts) -> "object | None":
    """Best-effort, zero-AI enrichment scan for contacts a CSV import just
    created (docs/build-plan.md §5's "Phase 1"). A student who imports
    people they've already emailed should not see 180 identical cold rows
    just because Coverage never checked Gmail — this runs the SAME
    deterministic `backfill_connection` pass `gmail_backfill` uses, scoped
    to only the handful of contacts this one import created, synchronously,
    right after the import (a handful of per-contact searches is not the
    multi-minute job a full-mailbox first-connect backfill is, so there is
    no need to defer this to the cron the way that one has to).

    Never raises — an import's own success/failure is independent of
    whether this enrichment scan ran, same posture as `crm.ai_summary`.
    Returns `None` whenever nothing happened (no contacts, Gmail Live not
    configured, no active connection, or the scan itself errored) and the
    `SyncResult` on an actual scan, mostly so tests can assert on it.

    `update_backfill_status=False`: this is a narrow, incidental scan, not
    the original first-connect backfill `GmailConnection.backfill_status`
    tracks — see `backfill_connection`'s docstring on why that field must
    stay untouched here.
    """
    contacts = list(contacts) if contacts else []
    if not contacts:
        return None
    try:
        if not is_configured():
            return None
        connection = GmailConnection.all_objects.filter(
            user=user, status="active"
        ).first()
        if connection is None:
            return None
        return backfill_connection(
            connection, contacts=contacts, update_backfill_status=False
        )
    except Exception:  # noqa: BLE001 — an import must never fail because enrichment did
        return None


# ---------------------------------------------------------------------------
# "Scan Now" rescan (Settings > Gmail Live) — user-triggered, repeatable
# ---------------------------------------------------------------------------

def run_rescan(connection: GmailConnection, *, dry_run: bool = False) -> dict:
    """The full "Scan Now" pipeline for one connection: the deterministic
    `backfill_connection` pass over ALL of the user's contacts (repeatable,
    never touching `backfill_status` — see that function's docstring),
    followed by the capped Haiku residue stage (`capture.gmail_residue`)
    over whatever the deterministic pass couldn't classify.

    Two clearly separate stages composed here, not merged into one: the
    first is free and unlimited; the second is metered — clamped to
    whatever `billing.credits` says this user's ledger can afford right now
    (docs/credit-system-plan.md's enforcement point 2), which is itself
    clamped to `gmail_residue.MAX_RESIDUE_THREADS`, and only runs at all
    when `gmail_residue.is_configured()` (that module no-ops to zero-stats
    otherwise). A student with zero affordable credits still gets the free
    deterministic pass in full — the residue stage is simply skipped, and
    `stats["residue"]["credit_limited"]` says so honestly rather than
    silently under-reporting. This composition point is what the
    `gmail_backfill` command calls for a queued rescan — see that command's
    `--email` / rescan-selection logic. It runs from that cron command, not
    a request, which is exactly why `billing.credits` never assumes one.

    Returns one merged stats dict: the deterministic `SyncResult.as_stats()`
    keys at the top level, plus a nested `"residue"` key with
    `run_residue_stage`'s own stats. Callers store this whole dict as
    `GmailConnection.rescan_stats`.
    """
    residue: list[dict] = []
    result = backfill_connection(
        connection,
        update_backfill_status=False,
        dry_run=dry_run,
        residue_sink=residue,
    )
    stats = result.as_stats()
    distinct_threads = len({e["thread_id"] for e in residue if e.get("thread_id")})

    if dry_run:
        # A dry run must not spend a single metered AI call, and must not
        # touch the ledger either — report how much residue WOULD be handed
        # to Phase 3, but never call it and never debit for it.
        stats["residue"] = {
            "residue_threads_seen": distinct_threads,
            "residue_threads_processed": 0,
            "genuine_reply": 0,
            "auto_reply": 0,
            "ambiguous": 0,
            "touches_logged": 0,
            "credit_limited": False,
        }
        return stats

    # The credit clamp, applied BEFORE a single classification call —
    # `affordable` is threads, already floored at 0 (a student with no
    # credits left still completes the free deterministic pass above; this
    # just means the residue stage below processes nothing).
    affordable = billing_credits.affordable_residue_threads(connection.user, distinct_threads)
    residue_stats = gmail_residue.run_residue_stage(connection, residue, max_threads=affordable)
    billing_credits.spend_rescan(connection.user, residue_stats["residue_threads_processed"])
    # Honest labeling (docs/credit-system-plan.md §6): whenever fewer
    # threads got processed than were actually seen, say so, rather than
    # letting "seen 87 / processed 40" speak for itself in a stats dict
    # nobody but a template reads closely.
    residue_stats["credit_limited"] = (
        residue_stats["residue_threads_processed"] < residue_stats["residue_threads_seen"]
    )
    stats["residue"] = residue_stats
    return stats
