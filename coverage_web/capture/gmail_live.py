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
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from email.utils import parseaddr

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from capture.gmail import apply_findings
from capture.models import GmailConnection

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
_BOUNCE_BODY_RE = re.compile(
    r"does not exist|550[ -]?5\.1\.1|recipient address rejected|"
    r"address not found|no such user",
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
    return Fernet(settings.GMAIL_LIVE_TOKEN_KEY.encode("utf-8"))


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
    (or update) this user's `GmailConnection`. Raises `GmailLiveError` on
    anything that leaves the user without a working connection."""
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

    gmail = build("gmail", "v1", credentials=creds)
    profile = gmail.users().getProfile(userId="me").execute()

    connection, _ = GmailConnection.objects.update_or_create(
        user=user,
        defaults={
            "gmail_address": profile["emailAddress"],
            "refresh_token_encrypted": encrypt_token(creds.refresh_token),
            "history_id": str(profile["historyId"]),
            "status": "active",
        },
    )
    register_watch(connection)
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
    none yet). Returns (renewed, revoked) for the calling command to report."""
    soon = timezone.now() + timedelta(days=1)
    due = GmailConnection.all_objects.filter(status="active").filter(
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
        connection = GmailConnection.all_objects.get(
            gmail_address=gmail_address, status="active"
        )
    except GmailConnection.DoesNotExist:
        return  # disconnected or unknown mailbox — nothing to do
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


def _looks_like_bounce(message: dict, from_addr: str, subject: str) -> bool:
    if _BOUNCE_FROM_RE.search(from_addr) or _BOUNCE_SUBJECT_RE.search(subject):
        return True
    snippet = message.get("snippet", "")
    return bool(_BOUNCE_BODY_RE.search(snippet))


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
        }

    if _looks_like_bounce(message, from_addr, subject):
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
    }
