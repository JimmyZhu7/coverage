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
   The genuine-reply test added in 2026-08 (`capture.inbound`) is held to
   the same rule: it reads RFC headers and the recipient shape, never prose.
2. **No new-contact creation.** Mirrors `capture_gmail` (Step 1 of the daily
   sync), not `capture_discover` (Step 2). `apply_findings` only logs
   touches against contacts ALREADY in Coverage; a message from someone not
   yet a contact is a harmless no-op (`skipped_unmatched`). Bringing
   USC-discovery-style new-contact creation onto a live, per-message trigger
   is a materially different scope decision (it write-creates data on every
   unknown sender) and was not asked for here.

   THE ONE SCOPE CHANGE SINCE (2026-08-22, and it is exactly the different
   decision this point anticipated): `apply_findings`' unmatched branch now
   also runs `capture.discovery.consider_finding`, which may write a
   `ContactProposal` — a pending, user-confirmable suggestion, judged by the
   deterministic chain in that module (bulk verdict, no-reply/role-account/
   ESP sender tests, firm-domain match or a genuine `In-Reply-To` of the
   user's own mail). It still never write-creates a Contact or a Touch:
   only the user's explicit accept on the Today page does, so the rule this
   point defends — no data created on every unknown sender — holds.
3. **No push/webhook endpoint.** `users.watch()` needs a Pub/Sub topic, but
   the *subscription* on that topic is a PULL subscription
   (`gmail_pubsub_listen` below), not a push endpoint — deliberately, so this
   whole feature works before Coverage has a public HTTPS deploy at all.

   AND PUB/SUB IS OPTIONAL ENTIRELY (2026-08-27). `sync_connection` builds
   its client from the stored OAuth refresh token and calls
   `users().history().list(...)` — no Pub/Sub is on that path, and no Google
   Cloud credential beyond the OAuth client in `.env`. Pulling FROM Pub/Sub
   is the one step needing Application Default Credentials, which is exactly
   what `gmail_pubsub_listen` cannot get on a network that blocks `gcloud`'s
   loopback hand-back or a Workspace tenant that forbids service-account
   keys. `gmail_poll` (capture/management/commands/gmail_poll.py) is the
   same sync on a timer instead of a doorbell: same `sync_connection`, same
   `apply_findings`, higher latency, zero Cloud credentials. See that
   command's docstring for the trade in full.

SETUP THIS MODULE ASSUMES ALREADY HAPPENED (docs/gmail-live-setup.md)
-----------------------------------------------------------------------
A second Google Cloud OAuth client (never the login one — see §3 and the
settings module) plus a Fernet token key. `is_configured()` is the runtime
gate for exactly that: every CONNECT-and-SYNC entry point below (the connect
button, `gmail_poll`, `gmail_backfill`) no-ops (rather than 500s) until those
three settings are set. A Pub/Sub topic (and, for `gmail_pubsub_listen`, a
pull subscription on it) is a SEPARATE, stricter gate — `is_push_configured()`
— held only by the pieces that genuinely need real-time push: `register_watch`,
`renew_watches`, and `gmail_pubsub_listen`. See "AND PUB/SUB IS OPTIONAL
ENTIRELY" above for why the split exists: a mailbox can connect and sync mail
with zero Google Cloud credentials beyond the OAuth client, and only turning
on real-time push needs the rest.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from email.utils import getaddresses, parseaddr

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db.models import Max, Q
from django.utils import timezone
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from accounts import trials as pro_trials
from billing import credits as billing_credits
from capture import gmail_residue, inbound
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

# The first-run SENT SWEEP (see `_sent_sweep_message_ids` and
# `backfill_connection`'s `sweep_sent`). How far back the one-time pass over
# the student's own sent mail reaches, and how many messages it will ever
# fetch.
#
# 180 days is one recruiting cycle. Sent mail older than that describes a
# relationship the cadence engine has nothing to do with any more, and every
# extra month is more proposals to read for less reason to read them.
#
# 500 is a hard ceiling on `messages.get` calls, applied newest-first, so a
# ten-year mailbox costs the same as a two-year one. It is a cost guard, not
# a policy: a student who really did send more than 500 mails in six months
# gets the most recent 500, and the live listener covers everything after
# the connection. Both are deliberately NOT env-tunable — a knob here would
# be a knob on how much of someone's mailbox Coverage reads.
#
# THE ONE PLACE THE CAP CAN COST SOMETHING, stated because it is not
# obvious. `discovery.BatchContext`'s merge guard counts distinct recipients
# per subject WITHIN the batch, so a blast the cap slices through is counted
# short. A blast's messages are contiguous in time and the cap is
# newest-first, so in practice a blast is either almost entirely in the
# batch or almost entirely out of it — measured on the founder's mailbox
# (2026-08-27, read-only) the cap dropped 308 of 808 matched messages and
# both of his real 100+ blasts still scored far past `MERGE_RECIPIENT_LIMIT`
# and proposed nobody. The residual case is a blast the cap boundary happens
# to leave three or fewer recipients of, and its backstop is the OTHER merge
# guard: a subject matching a DETECTED `Campaign` the user has not called
# their own recruiting is refused whatever the batch says.
SWEEP_SENT_DAYS = 180
SWEEP_MAX_MESSAGES = 500

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

# A bounce that does NOT mean "this address is wrong": the mailbox exists and
# is full, or the receiving server asked for a retry. Treating these as hard
# bounces is how a working address gets cleared off a contact over a full
# mailbox — verified on the founder's live mailbox (2026-08-24, read-only):
# Goldman's postmaster answered his note to a real banker with DSN status
# 5.2.2, "The recipient's mailbox is full and can't accept messages now.
# Please try resending your message later". The address works; the message
# didn't land TODAY. Markers, deterministic only: the mailbox-full/quota
# vocabulary (5.2.2 is nominally permanent but describes a full box, not a
# wrong address), the DSN's own "delayed"/retry language, and any 4.x.x
# status code (RFC 3464's transient class).
_SOFT_BOUNCE_RE = re.compile(
    r"mailbox (?:is )?full|over ?quota|quota ?exceeded"
    r"|try (?:re)?sending (?:your message )?(?:again )?later"
    r"|has been delayed|delivery will be (?:retried|attempted)"
    r"|will (?:keep trying|retry)|action:\s*delayed"
    r"|status:\s*4\.\d{1,3}\.\d{1,3}",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Labels that mean a message is NOT real correspondence and must never be
# classified. Measured on the founder's live mailbox (2026-08-27,
# read-only): 14 of 20 sampled `messagesAdded` history records were DRAFT
# autosaves — Gmail writes one per autosave while a mail is being composed.
# A draft classified as outbound logs an `outreach` touch for an email
# never sent (and the outbound discovery arm minting a "You wrote to them"
# proposal off it); SPAM lets a spoofed firm-domain sender reach the
# proposal ladder; TRASH is mail the user already threw away. Sending a
# draft mints a NEW message id with SENT, which arrives as its own
# `messagesAdded` record — skipping the DRAFT churn loses nothing.
_EXCLUDED_LABEL_IDS = frozenset({"DRAFT", "SPAM", "TRASH"})


def _excluded_by_labels(label_ids) -> bool:
    return bool(_EXCLUDED_LABEL_IDS.intersection(label_ids or ()))
_ICS_DTSTART_RE = re.compile(r"DTSTART(?:;TZID=([^:]+))?:(\d{8}T\d{6}Z?)")
_ICS_SUMMARY_RE = re.compile(r"SUMMARY:(.+)")
# Anchored to the start of a line, unlike the two above: `UID` is a substring
# of plenty of other property names (`X-MS-OLK-...UID`, `RECURRENCE-ID` in
# some exporters' custom fields), and picking one of those up would key the
# calendar on a value that is NOT stable across a reschedule.
_ICS_UID_RE = re.compile(r"^UID:(.+)$", re.MULTILINE)
# RFC 5545 line folding: a long property is split across lines, each
# continuation beginning with one space or tab. Google's UIDs routinely
# exceed the 75-octet limit and arrive folded, so the text has to be
# unfolded before any of the regexes above run — a half-read UID is worse
# than none, because it still looks like a key.
_ICS_FOLD_RE = re.compile(r"\r?\n[ \t]")


class GmailLiveError(Exception):
    """Raised for conditions the caller must react to (e.g. a revoked
    grant), as opposed to a single message this module just skips."""


def is_configured() -> bool:
    """Whether this deployment can connect a mailbox and sync mail at all —
    the connect button, `gmail_poll`, `gmail_backfill`'s backfill/rescan, all
    of it. Deliberately does NOT require `GMAIL_LIVE_PUBSUB_TOPIC`: none of
    those paths touch Pub/Sub — `connect_gmail` and `sync_connection` build
    their Gmail client straight from the stored OAuth refresh token and call
    `users().history().list(...)` directly (see the module docstring's "AND
    PUB/SUB IS OPTIONAL ENTIRELY"). See `is_push_configured()` for the
    stricter gate real-time push holds itself to instead."""
    return bool(
        settings.GMAIL_LIVE_CLIENT_ID
        and settings.GMAIL_LIVE_CLIENT_SECRET
        and settings.GMAIL_LIVE_TOKEN_KEY
    )


def is_push_configured() -> bool:
    """Whether this deployment can additionally register REAL-TIME push
    (`users.watch()`) on top of an already-working connection. Real-time push
    is the one piece of Gmail Live that needs a Google Cloud Pub/Sub topic —
    `register_watch`, `renew_watches`, and `gmail_pubsub_listen` hold
    themselves to this gate (never the base `is_configured()`), so a
    topicless deployment still connects and polls cleanly, and a genuine
    attempt at push refuses with a clear `GmailLiveError` instead of Google's
    own 400/404 for an empty or nonexistent topic name."""
    return is_configured() and bool(settings.GMAIL_LIVE_PUBSUB_TOPIC)


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

    # Pro trial (accounts.trials): a Free account's FIRST Gmail connect
    # starts a time-boxed trial, flipping `user.plan` to "pro" BEFORE the
    # plan gate right below sees it — so a trialing student's real-time sync
    # turns on in the same request that connected Gmail, which is the whole
    # point of the trial (the founder's own framing: "let them watch three
    # replies log themselves, then charge"). No-ops for anyone not eligible
    # (already Pro, already had a trial, or PRO_TRIAL_TRIGGER points
    # elsewhere) — see that module for what "eligible" means.
    if settings.PRO_TRIAL_TRIGGER == "gmail_connect":
        pro_trials.start_trial_if_eligible(user, trigger="gmail_connect")

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
    opposed to a transient error `gmail_watch_renew` should just retry.

    Raises `GmailLiveError` immediately, before any Google API call, when
    `GMAIL_LIVE_PUBSUB_TOPIC` is unset — otherwise this would reach Google
    with an empty `topicName` and come back as an opaque 400, which is
    exactly the "failing obscurely" this check exists to avoid. The one
    caller that treats this as non-fatal (`connect_gmail`, for a Pro user
    whose deployment has no topic yet) already catches broad `Exception`
    around this call, so the message just needs to be clear when it lands in
    that log line."""
    if not is_push_configured():
        raise GmailLiveError(
            "GMAIL_LIVE_PUBSUB_TOPIC is not set — real-time push needs a "
            "Pub/Sub topic. Connecting, Scan Now, and gmail_poll all work "
            "without one; see docs/gmail-live-setup.md §5 to add push."
        )
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

    Does not check `is_push_configured()` itself — the caller
    (`gmail_watch_renew`) already holds that gate before calling this at
    all, and this loop's own `register_watch` call raises the same clear
    `GmailLiveError` the moment any due connection is actually reached, so
    the refusal is never silent or obscure even for a caller that skips the
    gate.
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
# Free plan's "Scan Now" throttle (settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS)
# ---------------------------------------------------------------------------

def free_rescan_unlocks_at(connection: GmailConnection):
    """When THIS connection's next "Scan Now" becomes available on the Free
    plan, or `None` if one can run right now.

    Pro is never throttled here — including an active Pro trial, which is
    simply `user.plan == "pro"` (accounts.trials never introduces a third
    plan value, see that module). Real-time sync already gives Pro standing
    coverage; the throttle exists so Free can't reproduce that coverage for
    free by mashing the same button the gmail_backfill cron already polls
    every 15 minutes — see settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS's own
    comment for why that would gut the entire paid axis.

    Shared by `capture.views.gmail_rescan` (the server-side enforcement) and
    `accounts.views._gmail_live_context` (the Settings card's disabled
    button + unlock date), so the two can never quietly disagree about what
    "throttled" means.
    """
    if connection.user.plan == "pro":
        return None
    last_scan = connection.rescan_completed_at or connection.rescan_requested_at
    if last_scan is None:
        return None
    unlocks_at = last_scan + timedelta(days=settings.GMAIL_FREE_RESCAN_INTERVAL_DAYS)
    return unlocks_at if unlocks_at > timezone.now() else None


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
    except GmailConnection.MultipleObjectsReturned:
        # This is the ONE place in the app where a tenant is selected by a
        # value that arrives from outside it: `gmail_address` comes off
        # Google's Pub/Sub payload, and the docstring above says so. The
        # column is a plain EmailField with NO unique constraint (see
        # `capture.models.GmailConnection` — only `user` is unique, via the
        # OneToOneField), so two accounts connecting the same mailbox is a
        # shape the schema permits and this lookup cannot resolve.
        #
        # Refuse, loudly, rather than sync. `.get()` was already raising here
        # — uncaught, so a single ambiguous address broke every notification
        # the worker processed after it. Catching it stops that. What this
        # must never become is `.first()`: that reads as a tidy fix and turns
        # an ambiguous address into a silent, arbitrary choice of whose
        # mailbox to sync into whose CRM, which is a cross-tenant write
        # decided by row order. If this ever fires, the fix is a unique
        # constraint on the column, not a tiebreak here.
        logger.error(
            "gmail_live: %s active connections share gmail_address=%r — "
            "refusing to sync; a notification cannot say which tenant it is "
            "for. Add a unique constraint on GmailConnection.gmail_address.",
            GmailConnection.all_objects.filter(
                gmail_address=gmail_address, status="active"
            ).count(),
            gmail_address,
        )
        return

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
    # The shared per-mailbox lock (capture.locks) — the same one gmail_poll
    # and gmail_backfill take, because a push notification landing while a
    # poll pass or a backfill is mid-sync on the same mailbox is exactly
    # the interleaving the lock exists to end. Skip rather than wait: a
    # skipped notification costs nothing, since whoever holds the lock is
    # reading the same history cursor forward and the next notification
    # (or poll pass) picks up anything they missed.
    from capture import locks

    with locks.mailbox_lock(connection.pk) as acquired:
        if not acquired:
            return
        sync_connection(connection)


def sync_connection(connection: GmailConnection):
    """Sync one mailbox from its stored cursor. Returns the
    `capture.gmail.SyncResult` when findings were applied, else None.

    THE RETURN IS THE OBSERVABILITY FIX, not a convenience: this used to
    call `apply_findings(...)` and throw the report away, which meant every
    honesty valve that module maintains — `app_events_unresolved` ("we saw
    application mail and could not pin the role"), `skipped_ambiguous`,
    the mail-facts surfaced lines — was invisible on the ONE path that runs
    every two minutes. The next BofA-class deadline mail that gated in but
    could not resolve would have produced no row, no card, and no line
    anywhere. `gmail_poll` prints the non-zero counters and detail lines
    per pass; the Pub/Sub path gets the same facts through the log line
    below."""
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
            return None
        raise

    findings = []
    for message_id in message_ids:
        message = _fetch_message(gmail, message_id)
        if message is None:
            continue
        findings.extend(
            classify_message_findings(connection.gmail_address, message)
        )

    result = None
    if findings:
        result = apply_findings(connection.user, findings)
        for line in result.details:
            logger.info("Gmail Live %s: %s", connection.gmail_address, line)

    connection.history_id = latest_history_id or connection.history_id
    connection.last_notification_at = timezone.now()
    connection.save(update_fields=["history_id", "last_notification_at"])
    return result


def preview_sync(connection: GmailConnection) -> dict:
    """Read-only twin of `sync_connection`'s FIRST step, for `gmail_poll
    --dry-run`. Answers "what is waiting for this mailbox right now" without
    writing a single row: no findings applied, no `history_id` advanced, no
    `last_notification_at` stamped, no re-anchor on a 404.

    Returns `{"reanchor": bool, "message_ids": [...], "latest_history_id":
    str | None}`. `reanchor` True is the 404 case — Gmail's ~7-day history
    retention passed the stored cursor, and a REAL run would re-anchor to
    the mailbox's current `historyId` and accept the gap (see
    `sync_connection`). Reporting it instead of silently returning "nothing
    to sync" is the whole point: those two states look identical from the
    outside and mean completely different things.

    DELIBERATELY STOPS AFTER `history.list`. Classifying the messages would
    mean a `messages.get` for every one of them — the expensive half of a
    sync — to answer a question a poll's dry run isn't asking. "How much is
    queued, and is this mailbox still reachable" is what a scheduler's dry
    run needs; "what exactly would each message do to which contact" is
    `gmail_backfill --dry-run`'s job, and it already answers it per-contact
    on the shared `apply_findings(dry_run=True)` path.

    "Writes nothing" means nothing in Coverage and nothing in the mailbox.
    This DOES make live Gmail API calls — an OAuth token refresh plus one or
    more `history.list` pages — because a dry run that skipped the network
    could not tell a working connection from a revoked one, which is the
    single most useful thing it reports.
    """
    gmail = _gmail_client(connection)
    try:
        message_ids, latest = _list_new_messages(gmail, connection.history_id or None)
    except HttpError as exc:
        if exc.resp.status == 404:
            return {"reanchor": True, "message_ids": [], "latest_history_id": None}
        raise
    return {
        "reanchor": False,
        "message_ids": message_ids,
        "latest_history_id": latest,
    }


def _fetch_message(gmail, message_id: str) -> dict | None:
    """One full message, or None when it must not be classified.

    Two Nones, both load-bearing for the poll loop:

    - A 404. `history.list` faithfully reports `messagesAdded` for
      messages since PERMANENTLY DELETED — superseded draft autosaves are
      the canonical case (each autosave deletes its predecessor), and 4 of
      40 sampled ids on the founder's live stream were already gone. This
      loop used to have no handler, so the first such id raised out of
      `sync_connection` BEFORE the cursor advanced: every subsequent pass
      re-listed the same window and died on the same id, silently wedging
      the mailbox until Gmail's ~7-day retention re-anchored past the
      whole gap. A message that no longer exists is a skip, never an
      error.
    - An excluded label (see `_EXCLUDED_LABEL_IDS`): drafts, spam, trash.
    """
    try:
        message = gmail.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            return None
        raise
    if _excluded_by_labels(message.get("labelIds")):
        return None
    return message


def _list_new_messages(gmail, start_history_id) -> tuple[list[str], str | None]:
    """All `messagesAdded` ids since `start_history_id`, paging through
    `history.list`, plus the newest `historyId` to resume from next time.

    Records whose message already carries an excluded label (DRAFT/SPAM/
    TRASH — see `_EXCLUDED_LABEL_IDS`) are dropped here, before anyone
    pays a `messages.get` for them: on the founder's real stream most
    `messagesAdded` records are draft-autosave churn. `_fetch_message`
    re-checks after the fetch, because the history record's labels are a
    snapshot and the live message's labels are the truth."""
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
                message = added.get("message") or {}
                if _excluded_by_labels(message.get("labelIds")):
                    continue
                message_ids.append(message["id"])
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


def _extract_ics_schedule(message: dict) -> tuple[str | None, str | None, str | None]:
    """(iso_datetime, summary, uid) from the first `.ics`/`text/calendar` part
    found, or (None, None, None). Deterministic per §5 ("scheduling via .ics
    ... detection") — no language inference, just the invite's own DTSTART.

    THE UID IS THE STABLE IDENTITY. DTSTART answers "when", the UID answers
    "which event", and only the second one survives a reschedule. A real
    case from the founder's mailbox: an "Accepted: Jimmy <> Lily Coffee
    Chat" reply arrived on one Gmail thread and the counter-proposal ("New
    Time Proposed: ...") arrived on a DIFFERENT one, because Google starts a
    fresh thread for it. Keyed on the thread, that is two chats — one of
    them at a time nobody is turning up to. Keyed on the UID, which RFC 5545
    holds constant across REQUEST / REPLY / COUNTER / CANCEL for the same
    event, it is one chat that moved. See `capture.gmail._upsert_scheduled_chat`.
    """
    for part in _walk_parts(message.get("payload", {})):
        mime = part.get("mimeType", "")
        filename = part.get("filename", "")
        if mime != "text/calendar" and not filename.endswith(".ics"):
            continue
        text = _ICS_FOLD_RE.sub("", _decode_body(part)).replace("\r\n", "\n")
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
        uid_match = _ICS_UID_RE.search(text)
        uid = uid_match.group(1).strip() if uid_match else ""
        return dt.isoformat(), summary, uid or None
    return None, None, None


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


# The DSN's own machine-readable recipient fields (RFC 3464). Both real DSN
# shapes in the founder's mailbox (2026-08-27, read-only) carry these
# verbatim in the text this module scans: the Proofpoint/sendmail "Returned
# mail" transcript prints "Final-Recipient: RFC822; <addr>" inside its
# text/plain body, and the Exchange "Undeliverable:" report carries the same
# block in its message/delivery-status part (which `_bounce_text` now
# includes). When one of these is present it IS the answer — everything else
# in a DSN (quoted original headers, Received chains, signatures) is context
# that can name the wrong person.
_DSN_RECIPIENT_RE = re.compile(
    r"(?:final|original|x-actual)-recipient:\s*(?:rfc822;?\s*)?<?"
    r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})>?"
    r"|x-failed-recipients:\s*<?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})>?",
    re.IGNORECASE,
)

# Failure sentences that a human-readable bounce puts DIRECTLY in front of
# the failed address. Both live shapes lead with one of these; so do Gmail's
# own DSNs ("wasn't delivered to") and postfix's. An address found right
# after one of these phrases is being named AS the failure, not quoted in
# passing.
_BOUNCE_PHRASE_RE = re.compile(
    r"delivery has failed to these recipients or groups"
    r"|following addresses? had permanent fatal errors"
    r"|delivery to the following recipients? failed"
    r"|(?:was|were)n'?t delivered to"
    r"|could ?n[o']t be delivered to"
    r"|address not found"
    r"|your message to",
    re.IGNORECASE,
)
# How far past a failure phrase the named address must appear. Generous
# enough for a display name and a mailto: wrapper; short enough that the
# quoted original message's headers (hundreds of chars further down) can
# never be mistaken for the failure line.
_BOUNCE_PHRASE_WINDOW = 300


def _bounce_recipient(message: dict, own_email: str) -> str | None:
    """The failed address a bounce names, or None when it cannot be
    determined SAFELY. Downstream clears this address off a contact (the
    hard-bounce block in `apply_findings`) — a destructive write — so this
    refuses when the text is ambiguous rather than guessing.

    Three passes, most reliable first:

    1. RFC 3464 fields (`Final-Recipient:` / `Original-Recipient:` /
       `X-Failed-Recipients:`). Both real DSN shapes on the founder's live
       mailbox carry these; when present they are authoritative.
    2. An address within `_BOUNCE_PHRASE_WINDOW` chars after a failure
       phrase ("Delivery has failed to these recipients or groups", "The
       following addresses had permanent fatal errors", ...). The phrase is
       what makes the address the SUBJECT of the bounce.
    3. The old first-address-anywhere heuristic — but only when the whole
       text names exactly ONE candidate address. A DSN that quotes the
       original message (a Cc, an address in a signature) before naming the
       failed recipient used to hard-bounce-clear whichever address the
       scan met first; with several candidates and no anchor, the only safe
       answer is no answer. The daily agent-run sync remains the backstop
       for a refused DSN, and refusing costs one uncleaned address — the
       opposite mistake costs a working address on the wrong person.

    An address is never a candidate if it is the account owner's own or a
    mailer-daemon/postmaster address.
    """
    own = own_email.lower()

    def _ok(candidate: str) -> bool:
        return candidate != own and not _BOUNCE_FROM_RE.search(candidate)

    text = _bounce_text(message)

    for match in _DSN_RECIPIENT_RE.finditer(text):
        candidate = (match.group(1) or match.group(2) or "").lower()
        if _ok(candidate):
            return candidate

    for phrase in _BOUNCE_PHRASE_RE.finditer(text):
        window = text[phrase.end(): phrase.end() + _BOUNCE_PHRASE_WINDOW]
        for candidate in _EMAIL_RE.findall(window):
            low = candidate.lower()
            if _ok(low):
                return low

    candidates = []
    for candidate in _EMAIL_RE.findall(text):
        low = candidate.lower()
        if _ok(low) and low not in candidates:
            candidates.append(low)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _bounce_text(message: dict) -> str:
    """The text a bounce actually says its news in: Gmail's snippet plus
    every decoded `text/*` part, plus every `message/delivery-status` part.
    Shared by `_bounce_recipient` (finds the failed address) and the
    soft-bounce test in `_classify_message` (finds the "mailbox is full"/
    "delayed" vocabulary) so the two read the same words. The DECODED body
    matters beyond reach: Gmail's snippet for the founder's real Goldman
    DSN renders the routing address as "Noah. Bauld@ ny. ibd. email. gs.
    com" — spaces after every dot — so an address regex over the snippet
    alone misses what the body states cleanly.

    `message/delivery-status` is included because it is where an Exchange
    DSN keeps its `Final-Recipient:`/`Status:` block — the authoritative
    answer to "who failed" and the `4.x.x` transient class the soft-bounce
    test reads — and its body is plain RFC 3464 text despite the mimeType
    not starting with `text/`."""
    text_chunks = [message.get("snippet", "")]
    for part in _walk_parts(message.get("payload", {})):
        mime = part.get("mimeType", "")
        if mime.startswith("text/") or mime == "message/delivery-status":
            decoded = _decode_body(part)
            if decoded:
                text_chunks.append(decoded)
    return "\n".join(text_chunks)


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


def _outbound_recipients(own_email: str, message: dict) -> list[tuple[str, str]]:
    """Every distinct (name, address) the To: header names, own address
    excluded, order preserved. `getaddresses` rather than `parseaddr`:
    `parseaddr` silently takes only the FIRST mailbox of a multi-recipient
    header, which is how one email sent to three contacts logged outreach
    for one and lost the other two sends entirely."""
    seen: set[str] = set()
    recipients: list[tuple[str, str]] = []
    for to_name, to_addr in getaddresses([_header(message, "To")]):
        low = (to_addr or "").strip().lower()
        if not low or "@" not in low or low == own_email.lower() or low in seen:
            continue
        seen.add(low)
        recipients.append((to_name, low))
    return recipients


def classify_message_findings(own_email: str, message: dict) -> list[dict]:
    """Every finding one Gmail message honestly supports.

    For inbound mail this is `_classify_message`'s single finding (or
    none). For OUTBOUND mail it is one finding per distinct To: recipient:
    a note sent to three contacts is three sends, and collapsing it to the
    first recipient silently lost the other two (each un-logged send is a
    missing outreach touch, a wrong follow-up clock, and a hole in
    campaign detection's recipient count). Cc is deliberately excluded — a
    Cc received the mail but was not the person being written to, and
    "outreach" is a claim about who the note was FOR.

    The merge guard is unaffected in the right direction: each recipient's
    finding carries the same subject, so `discovery.BatchContext`'s
    fan-out count now sees the true recipient count — a single blast To:
    forty people counts as forty, which is exactly what the guard exists
    to notice.
    """
    from_name, from_addr = parseaddr(_header(message, "From"))
    if from_addr.lower() == own_email.lower():
        recipients = _outbound_recipients(own_email, message)
        return [
            _outbound_finding(message, to_name, to_addr)
            for to_name, to_addr in recipients
        ]
    finding = _classify_message(own_email, message)
    return [finding] if finding is not None else []


def _outbound_finding(message: dict, to_name: str, to_addr: str) -> dict:
    subject = _header(message, "Subject")
    ics_dt, ics_summary, ics_uid = _extract_ics_schedule(message)
    return {
        "name": to_name or to_addr.split("@")[0],
        "email": to_addr.lower(),
        "found": True,
        "bounced": False,
        "outreach_sent": True,
        "replied": False,
        "chat_status": "scheduled" if ics_dt else "none",
        "chat_scheduled_at": ics_dt,
        # The invite's own identity, stable across a reschedule that lands
        # on a different Gmail thread — see `_extract_ics_schedule`.
        "ics_uid": ics_uid,
        "evidence": (
            f"Calendar invite sent: {ics_summary or subject}"
            if ics_dt
            else f"Sent: {subject}"
        ),
        "thread_id": message.get("threadId", ""),
        # The Subject header, kept rather than only folded into the prose
        # of `evidence` above. `capture.gmail._stamp_subject` writes it to
        # `Touch.subject`, and `crm.campaigns` groups a mail merge on it —
        # 201 threads sharing one subject is the whole signal, and it used
        # to be read on the line above and discarded on the next.
        "subject": subject,
        "occurred_at": _message_occurred_at(message),
    }


def _classify_message(own_email: str, message: dict) -> dict | None:
    """One Gmail message -> one finding dict in `GmailFindingsProvider`'s
    shape, or None if there's nothing worth reporting (e.g. a message from
    the account owner to themselves, or an address this heuristic can't
    resolve at all).

    For an outbound message this reports the FIRST To: recipient only —
    callers that must not lose a multi-recipient send (the live sync, the
    backfill) go through `classify_message_findings`, which returns one
    finding per recipient."""
    from_name, from_addr = parseaddr(_header(message, "From"))
    subject = _header(message, "Subject")
    thread_id = message.get("threadId", "")
    ics_dt, ics_summary, ics_uid = _extract_ics_schedule(message)
    occurred_at = _message_occurred_at(message)

    is_outbound = from_addr.lower() == own_email.lower()

    if is_outbound:
        recipients = _outbound_recipients(own_email, message)
        if not recipients:
            return None
        return _outbound_finding(message, *recipients[0])

    if _looks_like_bounce(from_addr, subject):
        recipient = _bounce_recipient(message, own_email)
        if not recipient:
            return None
        # SOFT vs HARD, and the split is the whole point. A hard bounce means
        # "this address is wrong" and downstream clears it off the contact
        # (apply_findings' bounce block). A soft one — mailbox full, server
        # asked for a retry — means the OPPOSITE: the address works and the
        # message didn't land today. Filing it as `bounced: True` would clear
        # a working address; filing it as nothing (the old behavior — it fell
        # into the recipient's unmatched skip) discarded the one useful fact
        # the DSN carries, the expanded routing address the receiving system
        # names. So it becomes its own finding shape: never `bounced`, marked
        # `bulk` so no matcher can mistake the postmaster for a person
        # replying, and carrying the DSN's own text (from the decoded body,
        # not Gmail's dot-mangled snippet — see `_bounce_text`) for
        # `capture.mailfacts` to read and quote.
        text = _bounce_text(message)
        if _SOFT_BOUNCE_RE.search(f"{subject}\n{text}"):
            return {
                "name": recipient.split("@")[0],
                "email": recipient,
                "found": True,
                "bounced": False,
                "soft_bounce": True,
                "outreach_sent": False,
                "replied": False,
                "chat_status": "none",
                "chat_scheduled_at": None,
                "bulk": True,
                "bulk_reasons": "delivery deferred (soft bounce)",
                "snippet": text[:600],
                "evidence": f"Delivery deferred (not a bounce): {subject}",
                "thread_id": thread_id,
                "subject": subject,
                "occurred_at": occurred_at,
            }
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
            "subject": subject,
            "occurred_at": occurred_at,
        }

    if not from_addr:
        return None

    # THE GENUINE-REPLY TEST (capture.inbound). Everything inbound and
    # non-bouncing used to fall straight through to `replied: True` — which
    # is how a mass "Sophomore Series" invitation from a recruiter the
    # founder had never written to ratcheted her to warmth `replied` and put
    # a coffee-chat ask in his Today queue. A bulk message is still recorded
    # (it may carry a real deadline or event), just not as evidence that
    # anyone answered him.
    verdict = inbound.classify_inbound(own_email, message)
    if verdict.is_bulk:
        return {
            "name": from_name or from_addr.split("@")[0],
            "email": from_addr.lower(),
            "found": True,
            "bounced": False,
            "outreach_sent": False,
            "replied": False,
            # Deliberately NOT "scheduled" even when the blast carries an
            # .ics: a programme webinar on a list invitation is not a chat
            # with this person, and `_upsert_scheduled_chat` would put
            # "Chat with Caroline Baenen" on the calendar. The invite's own
            # summary still rides in `evidence` below, so the date is not
            # thrown away — it is just not claimed as a relationship.
            "chat_status": "none",
            "chat_scheduled_at": None,
            "bulk": True,
            "bulk_reasons": verdict.reason_text,
            # The counterparty's own mailbox answered by machine — RFC 3834
            # `Auto-Submitted`, an `X-Autoreply`, or the stock subject prefix
            # (see `capture.inbound`). This is `capture.mailfacts`' gate: an
            # auto-reply is the one bulk message whose body states facts
            # about the PERSON ("no longer with", "please contact X at Y",
            # "back on September 2"), so the flag rides along rather than
            # being re-derived from the reasons prose.
            "auto_reply": verdict.auto_submitted,
            # Gmail's own preview line, for `capture.appmail` only. Bulk
            # mail is where application-status mail lives, and a rejection's
            # subject is routinely the neutral "Your application to X" —
            # the decision sentence is in the body or nowhere. Read at
            # classification time and never stored: what an
            # `ApplicationEvent` keeps is the subject (§10), and every other
            # consumer of this dict ignores the key.
            "snippet": (message.get("snippet") or "")[:600],
            "evidence": (
                f"Bulk/automated email: {ics_summary or subject or ''}".strip()
                or "Bulk/automated email"
            ),
            "thread_id": thread_id,
            "subject": subject,
            "occurred_at": occurred_at,
        }

    return {
        "name": from_name or from_addr.split("@")[0],
        "email": from_addr.lower(),
        "found": True,
        "bounced": False,
        "outreach_sent": False,
        "replied": True,
        "chat_status": "scheduled" if ics_dt else "none",
        "chat_scheduled_at": ics_dt,
        # See the outbound branch above: this is what makes Lily's "New Time
        # Proposed" on a brand-new thread move the existing chat instead of
        # adding a second one at the old time.
        "ics_uid": ics_uid,
        "bulk": False,
        # For the discovery hook (capture.discovery, via apply_findings'
        # unmatched branch): whether this message carries a real RFC reply
        # pointer — the "the user emailed them first" evidence — and the
        # subject, which is the most a proposal card is allowed to show
        # (§10: subject at most, never a body). Ride-along facts on the same
        # finding shape; every existing consumer ignores them.
        "threaded_reply": verdict.threaded_reply,
        # Whether the sender actually put the user on To:/Cc: — the other
        # half of "someone wrote to YOU". A reply-all into a thread the user
        # was only Bcc'd or list-delivered into carries In-Reply-To and is
        # still not addressed to them; discovery refuses on an explicit
        # False and treats absence as unknown (older findings keep behaving
        # exactly as they did).
        "addressed_to_user": verdict.addressed_to_user,
        "subject": subject,
        # Same ride-along as the bulk branch above, for `capture.appmail`:
        # plenty of banks send confirmations from a `campus@firm.com`
        # address that carries no list headers at all.
        "snippet": (message.get("snippet") or "")[:600],
        "evidence": (
            f"Calendar invite received: {ics_summary or subject}"
            if ics_dt
            else message.get("snippet", "")[:300]
        ),
        "thread_id": thread_id,
        "subject": subject,
        "occurred_at": occurred_at,
    }


# ---------------------------------------------------------------------------
# First-connect historical backfill
# ---------------------------------------------------------------------------
#
# TWO SEARCHES, TWO QUESTIONS, ONE BATCH.
#
# The per-contact search is the original and still the default: scoped to
# `from:X OR to:X` for every contact already in Coverage, it can only ever
# return mail involving someone already tracked. It fills in HISTORY and, by
# construction, discovers nobody.
#
# That construction is exactly the cold start. A student who connects Gmail
# before they own a spreadsheet has no contacts, so the per-contact loop
# builds an empty query set, finds nothing, and the feature that makes
# Coverage feel alive does nothing on the one day it matters most. The
# onboarding copy papered over it by telling students to import first —
# which asks them to produce a CSV before they have seen a single thing the
# product does.
#
# So the first-connect pass now ALSO runs one SENT SWEEP
# (`_sent_sweep_message_ids`, opted into by `backfill_connection`'s
# `sweep_sent`): `in:sent` over the last `SWEEP_SENT_DAYS`, capped at
# `SWEEP_MAX_MESSAGES`. It answers the other question — "who have I already
# written to at a firm I care about" — and it is the only evidence a
# brand-new account actually has. Nothing downstream changes: the swept ids
# join the per-contact ids in ONE set, get the SAME `classify_message_findings`
# treatment, and go through the SAME single `apply_findings` call, which is
# what routes an unmatched recipient to `capture.discovery.consider_finding`
# and its refusal ladder. One `apply_findings` call is load-bearing rather
# than tidy: `discovery.BatchContext`'s mail-merge guard counts how many
# distinct recipients share a subject ACROSS the batch, so splitting the
# sweep into its own pass would blind the one guard that stops a 201-person
# club blast becoming 201 proposals.
#
# What the sweep does NOT do: create a contact (only a tap does — see
# capture/discovery.py), read inbound mail it was not already going to read
# (the sweep is `in:sent`; the whole-mailbox inbound firehose is newsletters
# and is not worth the read), or run more than once (`backfill_status` is
# sticky at `done`, and a proposal row for an address refuses forever after).
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


def _sent_sweep_message_ids(gmail, *, now) -> list[str]:
    """Message ids for the first-run sent sweep: the student's OWN sent mail
    over the last `SWEEP_SENT_DAYS`, newest first, at most
    `SWEEP_MAX_MESSAGES` of them.

    `in:sent` plus mailer-daemon/postmaster, and nothing else. Not
    `-in:sent` too, not `newer_than:` over the whole mailbox: the sweep's
    entire claim is "the student chose to write to this person", which is
    the one piece of evidence a brand-new account owns and the one Gmail can
    hand over without reading anything that was written TO them. Every
    judgment about whether a recipient is worth proposing happens later and
    elsewhere (`capture.discovery.consider_finding` — firm domain required,
    role accounts and ESPs and the user's own institution refused, merges
    refused). This function only decides which messages get read.

    THE BOUNCE CLAUSE IS NOT AN EXCEPTION TO THAT, IT IS WHAT KEEPS IT
    HONEST. `discovery.consider_finding` refuses to propose a recipient
    whose address bounced in the same batch ("the send provably did not
    reach a person"), and it can only see a bounce that is IN the batch. A
    bounce is inbound mail, so a strictly `in:sent` sweep would hand the
    guard a batch it cannot fire on and quietly propose every dead address
    in the student's outbox. Two `from:` terms restore it. They widen the
    read by exactly the machine senders `_looks_like_bounce` already
    recognises — no human's mail to the student is read by this function.

    Paging stops at the cap rather than truncating afterward, so the ceiling
    is on API calls, not just on the returned list. Gmail returns
    `messages.list` newest-first, so the cap drops the OLDEST mail, which is
    the right half to lose.
    """
    since = now - timedelta(days=SWEEP_SENT_DAYS)
    query = (
        f"(in:sent OR from:mailer-daemon OR from:postmaster) "
        f"after:{since:%Y/%m/%d}"
    )
    ids: list[str] = []
    page_token = None
    while True:
        response = gmail.users().messages().list(
            userId="me", q=query, pageToken=page_token
        ).execute()
        for item in response.get("messages", []) or []:
            ids.append(item["id"])
            if len(ids) >= SWEEP_MAX_MESSAGES:
                return ids
        page_token = response.get("nextPageToken")
        if not page_token:
            return ids


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
    sweep_sent: bool = False,
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

    `sweep_sent`: also read the student's own recent sent mail, whoever it
    went to (`_sent_sweep_message_ids`). False by default — the per-contact
    scan above is the whole job for the import-triggered scoped pass and for
    "Scan Now", both of which run against an account that already has
    contacts. Only the FIRST-CONNECT pass (`gmail_backfill`'s pending sweep)
    passes True, because that is the one moment where the answer to "who is
    already in Coverage" may be nobody and the mailbox is the only thing
    that knows anything. See the section header above for why the swept ids
    join this function's existing set rather than getting a pass of their
    own.

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

    if sweep_sent:
        # Union, not a second pass: an id the per-contact loop already found
        # is the same message, and the set is what keeps a note the student
        # sent to three people — two tracked, one not — from being fetched
        # twice and classified twice.
        message_ids.update(_sent_sweep_message_ids(gmail, now=now))

    findings = []
    for message_id in message_ids:
        # Same guard as the live path (`_fetch_message`): a per-contact
        # search matches the user's own DRAFTS to that contact, and a
        # message can be deleted between the list and the get. Neither is
        # correspondence, so neither reaches classification OR the residue
        # sink — a draft is not "mail we could not read".
        message = _fetch_message(gmail, message_id)
        if message is None:
            continue
        message_findings = classify_message_findings(own_email, message)
        if message_findings:
            findings.extend(message_findings)
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
        # Shared per-mailbox lock, best-effort semantics: this scan runs
        # inline in an import request and is documented as optional
        # enrichment, so "another writer has the mailbox" means skip, not
        # wait — the import succeeds either way, and the touches this
        # would have found arrive through the poll loop or a later scan.
        from capture import locks

        with locks.mailbox_lock(connection.pk) as acquired:
            if not acquired:
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
