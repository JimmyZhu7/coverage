"""Gmail Live — per-user state for real Gmail API access.

This is docs/build-plan.md §5's "v2": the founder-dogfood, real-time upgrade
from the v1 BCC/forward pipeline. v1 needs zero Google review because it never
requests a mailbox scope; this does, which is why every field here exists to
keep that surface small and revocable rather than because the shape is fun.

ONE MODEL, ONE JOB: remember enough to (a) refresh an access token, (b) know
where in the mailbox's change history we last read, and (c) know when the
standing "notify me" registration needs renewing. Nothing else. In particular
this table stores no message content — that still flows through the same
`capture.gmail.apply_findings` finding-dict shape the manual daily sync has
always used (see gmail_live.py), so the ratchet/dedup/calendar-event logic
that pipeline already earned stays the single source of truth for "what does
a finding do to a contact." This model's only job is "can we still read this
mailbox, and from where."

The refresh token is a bearer credential to someone's Gmail — encrypted at
rest with Fernet (`settings.GMAIL_LIVE_TOKEN_KEY`), not because the app's
threat model changed, but because "we store a live mailbox key in plain text"
is exactly the kind of finding a security reviewer opens with.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from coverage_web.tenancy import PrivateModel


class ContactProposal(PrivateModel):
    """Someone the mailbox judged worth tracking, waiting for the user's tap.

    THE CONTRACT THIS MODEL EXISTS TO KEEP. `gmail_live.py`'s docstring
    (point 2) excluded new-contact creation from the live path because it
    "write-creates data on every unknown sender", and `capture_discover`
    insists discovery have "its own door". This row is that door made safe:
    detection writes a PROPOSAL — never a Contact, never a Touch — and only
    the user's explicit accept (capture.discovery.accept) creates anything.
    A dismissed proposal is remembered forever (the unique constraint below
    plus "dismissed rows are never deleted"), so the same stranger is not
    re-proposed every week the scan re-sees their thread.

    What it holds is exactly what was OBSERVED, nothing inferred: the display
    name the message carried, the address, the firm whose email domain
    matched (if one did), the one-line evidence, and the strongest touch kind
    the evidence honestly supports — so an accept can log real history
    through the normal ratchet rather than fabricating warmth.
    """

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACCEPTED, "Accepted"),
        # Never deleted. The row itself is the "do not re-propose" memory.
        (STATUS_DISMISSED, "Dismissed"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    # Always normalized lowercase (capture.providers.normalize_email) — the
    # unique constraint below is only a real dedup if the key has one spelling.
    email = models.EmailField()
    # The firm whose email domain matched the sender's, when one did. SET_NULL:
    # a firm leaving the directory does not invalidate the observation.
    firm = models.ForeignKey(
        "directory.Firm", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="contact_proposals",
    )
    # Parsed off the display name ("Jane Doe, Campus Recruiting" -> the tail),
    # never guessed from prose. Blank is the common, honest case.
    role_hint = models.CharField(max_length=255, blank=True, default="")
    # crm.relevance.is_recruiting_role over the role hint. Recruiters are
    # still proposed (worth tracking) — accepting sets
    # Contact.recruiting_contact so the queue never asks them for coffee.
    recruiting_hint = models.BooleanField(default=False)
    # One line of why: which message, in what shape. Subject at most — §10's
    # "no email bodies in logs/notes" applies here like everywhere else.
    evidence = models.CharField(max_length=300, blank=True, default="")
    # The strongest touch kind the evidence supports ("reply_received",
    # "chat_scheduled", "chat"). What accept logs through the ratchet, so the
    # created contact's warmth is earned history, not a gift.
    evidence_kind = models.CharField(max_length=32, default="reply_received")
    thread_id = models.CharField(max_length=128, blank=True, default="")
    # When the observed message actually happened — rides into log_touch's
    # `now` on accept so cadence math sees the real date, not the tap date.
    occurred_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    # Set on accept — the idempotency marker, same pattern as
    # ChatDebrief.intro_contact. SET_NULL: deleting the contact later must
    # not delete the memory that this address was already handled.
    contact = models.ForeignKey(
        "crm.Contact", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="proposals",
    )
    created = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta(PrivateModel.Meta):
        db_table = "contact_proposals"
        ordering = ["created"]
        constraints = [
            # One row per (user, address), whatever its status. This is the
            # whole "dismiss is permanent" mechanism: detection get-or-skips
            # on this key, so a dismissed person can never come back.
            models.UniqueConstraint(
                fields=["user", "email"], name="uniq_contact_proposal_user_email"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} <{self.email}> ({self.status})"


class GmailConnection(PrivateModel):
    STATUS_CHOICES = [
        ("active", "Active"),
        # Set when Google's refresh call itself reports the grant is gone
        # (revoked in the user's Google Account, or the OAuth app fell out of
        # the 100-tester allowlist). Distinct from a merely-expired watch,
        # which `gmail_watch_renew` fixes on its own — this needs the user to
        # reconnect, so it is surfaced rather than silently retried forever.
        ("revoked", "Revoked — needs reconnect"),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    gmail_address = models.EmailField()
    # Fernet ciphertext (bytes-as-text; Fernet output is already URL-safe
    # base64), never the raw refresh token. See `encrypt_token`/`decrypt_token`
    # in gmail_live.py — this model deliberately has no plaintext accessor.
    refresh_token_encrypted = models.TextField()
    # Gmail's own change cursor. `history.list(startHistoryId=...)` is how
    # every poll after the first one asks "what changed since last time" —
    # this is the ENTIRE reason a watch/notification-driven pipeline needs
    # per-user state at all, and losing it means falling back to a full
    # re-scan rather than an incremental one.
    history_id = models.CharField(max_length=32, blank=True, default="")
    # `users.watch()` registrations expire after 7 days (Google's own limit,
    # not ours) — `gmail_watch_renew` re-issues before this passes.
    watch_expiration = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="active")
    connected_at = models.DateTimeField(auto_now_add=True)
    last_notification_at = models.DateTimeField(null=True, blank=True)

    BACKFILL_CHOICES = [
        ("none", "Not started"),
        # Set by connect_gmail() right after register_watch() — live coverage
        # starts immediately either way; this just marks that a one-time
        # historical pass is owed. The next gmail_backfill tick picks it up.
        ("pending", "Queued"),
        ("running", "Running"),
        ("done", "Done"),
        # Distinct from "pending" so a reconnect after a revoked grant does
        # NOT silently skip the historical pass a failed run never finished —
        # see gmail_backfill.py's retry selection.
        ("failed", "Failed — will retry"),
    ]
    backfill_status = models.CharField(
        max_length=16, choices=BACKFILL_CHOICES, default="none"
    )
    # Set by gmail_backfill's command the moment it flips a row to
    # "running" — the ONLY thing that lets a stuck "running" row be told
    # apart from one that's genuinely still in progress. Without this, a
    # process killed mid-run (SIGKILL, OOM, a redeploy landing between the
    # status write and the try/except that would otherwise mark it
    # "failed") leaves the row parked at "running" forever: the command's
    # own selection query only ever looks for "pending"/"failed", so a
    # "running" row with no process behind it is invisible to every future
    # tick and the connection's first-connect backfill simply never
    # finishes. See gmail_backfill.py's STALE_RUNNING_AFTER.
    backfill_started_at = models.DateTimeField(null=True, blank=True)
    backfill_completed_at = models.DateTimeField(null=True, blank=True)
    # The SyncResult.as_stats() dict from the run that finished (or most
    # recently failed) — same shape capture_gmail's Import row stores, so a
    # human reading either has one shape to learn.
    backfill_stats = models.JSONField(default=dict, blank=True)

    # A user-triggered "Scan Now" rescan (Settings > Gmail Live) — a full
    # re-check of Gmail against ALL of the user's contacts, on demand,
    # repeatably. DELIBERATELY a separate set of fields from the
    # backfill_* ones above rather than reusing them: `backfill_status`
    # means specifically "has the ORIGINAL post-connect backfill ever
    # completed" and is sticky at "done" forever once true (see its own
    # comment) — a rescan is a different, repeatable action that must be
    # able to run again and again without disturbing that fact.
    RESCAN_CHOICES = [
        ("none", "Never run"),
        # Set the moment the "Scan Now" button is pressed; the same
        # gmail_backfill cron tick that picks up first-connect backfills
        # also picks up a "pending" rescan — see that command's docstring.
        ("pending", "Queued"),
        ("running", "Running"),
        ("done", "Done"),
        ("failed", "Failed — will retry"),
    ]
    rescan_status = models.CharField(
        max_length=16, choices=RESCAN_CHOICES, default="none"
    )
    # When "Scan Now" was pressed. Purely the in-flight-button guard (the
    # Settings view disables the button whenever `rescan_status` is
    # `pending`/`running`, so a user can't queue five rescans at once) — NOT
    # the staleness clock. A rescan can sit `pending` for a while before a
    # cron tick picks it up, so anchoring staleness here reclaimed (and
    # double-charged/double-logged) a rescan that was still genuinely
    # running, just slow — see `rescan_started_at` below for the field that
    # actually measures run time.
    rescan_requested_at = models.DateTimeField(null=True, blank=True)
    # When THIS run actually started — set the moment gmail_backfill.py
    # flips status to "running", mirroring backfill_started_at above. This,
    # not rescan_requested_at, is what STALE_RUNNING_AFTER measures against:
    # a process killed mid-run should be reclaimed based on how long it's
    # actually been running, not how long ago the user clicked the button.
    rescan_started_at = models.DateTimeField(null=True, blank=True)
    rescan_completed_at = models.DateTimeField(null=True, blank=True)
    # Same shape as backfill_stats, plus the Phase-3 AI residue stage's own
    # counters (see capture/gmail_residue.py) merged in under their own
    # keys — one JSON blob, one place a human reads "what did the last
    # rescan find".
    rescan_stats = models.JSONField(default=dict, blank=True)

    class Meta(PrivateModel.Meta):
        db_table = "gmail_connections"

    def __str__(self) -> str:
        return f"{self.gmail_address} ({self.status})"
