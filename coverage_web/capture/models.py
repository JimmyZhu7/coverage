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
    # When "Scan Now" was pressed. Doubles as two guards: the in-flight one
    # (the Settings view disables the button whenever `rescan_status` is
    # `pending`/`running`, so a user can't queue five rescans at once) and
    # the staleness one gmail_backfill.py's command uses to notice a
    # "running" row whose process died mid-run and pick it back up rather
    # than leaving it permanently stuck — see STALE_RUNNING_AFTER there.
    rescan_requested_at = models.DateTimeField(null=True, blank=True)
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
