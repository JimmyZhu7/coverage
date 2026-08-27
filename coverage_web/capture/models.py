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
from django.db.models import Q

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

    REMEMBERED FOREVER IS NOT THE SAME AS UNRECOVERABLE. The memory exists to
    stop the SCAN re-asking; it was never meant to stop the USER changing
    their mind. `capture.discovery.restore` puts a dismissed row back to
    `pending` on an explicit tap (the Undo strip on Today, or the Dismissed
    card in Settings), and nothing automatic ever does — `consider_finding`
    still refuses on the mere existence of a row for that address, whatever
    its status. So the guarantee is intact and a mis-tap is no longer a
    person buried without a trace.

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
    # WHAT THIS PERSON REPLIED TO — the subject of the thread, reply/forward
    # prefixes stripped, and the single most decision-changing fact on the
    # card. Set only for a genuine threaded reply (`threaded_reply` on the
    # finding); blank everywhere else, and blank is honest.
    #
    # WHY IT IS A COLUMN AND NOT READ OFF `evidence`. The founder is also his
    # club's outreach lead and mail-merged 201 alumni with the subject "Fall
    # 2026 ICC Alumni Digital Panel Outreach" (see crm.models.Campaign). A
    # panelist's reply and a genuine banker's reply produce IDENTICAL cards
    # today: same name shape, same "Not in your network" badge, same
    # "Replied to your email" line. Campaign-aware suppression
    # (capture.discovery.consider_finding) only fires when the campaign was
    # DETECTED, and detection needs the outbound sends in the database — if
    # the merge predates the Gmail connection there is no campaign, no
    # suppression, and the club panelist walks into the CRM as a real
    # relationship on one tap. The subject alone makes that send self-evident,
    # so it gets its own field and its own line rather than being buried in
    # the prose of `evidence`, which a display change would then have to parse
    # back out.
    thread_subject = models.CharField(max_length=255, blank=True, default="")
    # Whether the message carried a real RFC reply pointer (In-Reply-To /
    # References of something the user sent) — `capture.inbound`'s
    # `threaded_reply`, the other half of the judgment chain's final gate.
    #
    # Stored so the card can tell "they replied and the subject line was
    # empty" apart from "they wrote to you first from a firm address". Those
    # need different sentences, and the only alternative was pattern-matching
    # the prose of `evidence`, which is a display string that a copy edit is
    # allowed to change. A fact the finding already knew, kept instead of
    # re-derived.
    threaded_reply = models.BooleanField(default=False)
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


class ApplicationEvent(PrivateModel):
    """Something an ATS said about one of your applications, waiting for a tap.

    THE SAME DOOR `ContactProposal` OPENS, FOR THE OTHER HALF OF THE MAILBOX.
    Application mail is bulk mail — no-reply senders, `List-Unsubscribe`,
    campaign ids — which is exactly why `capture.inbound` can spot it and
    exactly why it never becomes a contact. What it IS good for is the
    pipeline: "Thank you for applying" is the fact My Applications was
    always waiting for, and `directory/management/commands/
    capture_applications.py` already proved the matching works. What that
    command lacks is a gate: it writes `UserOpportunity.applied_status`
    straight from a findings file. This row is that gate.

    NOTHING IS WRITTEN TO THE PIPELINE UNTIL THE USER ACCEPTS. That is not
    UX polish, it is the Limited Use posture the B2B plan rests on: mail
    read on a student's behalf may PROPOSE, and only the student's own tap
    may change their record. `capture.appmail.accept` is the only path from
    here to `UserOpportunity`, and it is reached from one POST.

    WHY `opportunity` IS NOT NULLABLE. A row here means "we know which role
    this is about" — `directory.applications.match_application` said so, by
    the same one-believable-candidate rule that command already refuses to
    bend. A confirmation we cannot pin to a role is counted and reported by
    the sync (see `SyncResult.app_events_unresolved`) and never carded: a
    card that names a firm but no role has nothing the user could accept.
    """

    # The vocabulary is the EMAIL's, not the pipeline's — see
    # `capture.appmail.TARGET_STATUS` for how each one maps onto the coarser
    # five-state funnel `directory.views._TRACK_STATES` owns, and why an
    # assessment invite deliberately does not claim "Interviewing".
    APPLIED = "applied"
    # "Congratulations on advancing in the Campus Insight Forum process." Its
    # own kind rather than a second flavour of APPLIED because the two mails
    # say different things and the dedup constraint below is keyed on the
    # kind: a firm that confirms an application and later announces an
    # advance has sent two facts, and a student should see both. Both still
    # claim `submitted` — see `capture.appmail.TARGET_STATUS` for the
    # under-claim and why it is the honest one.
    ADVANCED = "advanced"
    ASSESSMENT = "assessment"
    VIDEO_INTERVIEW = "video_interview"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    EVENT_CHOICES = [
        (APPLIED, "Application received"),
        (ADVANCED, "Advanced in the process"),
        (ASSESSMENT, "Online assessment invite"),
        (VIDEO_INTERVIEW, "Video interview invite"),
        (INTERVIEW, "Interview scheduling"),
        # Stored under its true name; NEVER rendered as one. See
        # `capture.appmail.EVENT_LABELS` — the card says "Not moving
        # forward" and the button says "Mark done", because a student
        # reading their own rejection back off a dashboard does not need
        # the product to say it twice.
        (REJECTED, "Decision received"),
        (OFFER, "Offer"),
    ]

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACCEPTED, "Accepted"),
        # Never deleted, exactly as on ContactProposal: the row IS the
        # "don't ask me about this again" memory.
        (STATUS_DISMISSED, "Dismissed"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # CASCADE, matching `analytics.UserOpportunity.opportunity`: this row is
    # about one posting and is meaningless without it, and the unique
    # constraint below is keyed on it.
    opportunity = models.ForeignKey(
        "directory.Opportunity", on_delete=models.CASCADE,
        related_name="application_events",
    )
    # Denormalized off the opportunity so the card renders (and the export
    # reads) without a second join, and so a merged firm doesn't silently
    # rewrite history. SET_NULL for the same reason ContactProposal.firm is.
    firm = models.ForeignKey(
        "directory.Firm", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="application_events",
    )
    # What the EMAIL called the firm, before matching — kept whether or not
    # `firm` resolved, because it is the observation and `firm` is the
    # conclusion.
    firm_text = models.CharField(max_length=255, blank=True, default="")
    event_type = models.CharField(max_length=32, choices=EVENT_CHOICES)
    # The `applied_status` an accept would write. Stored rather than derived
    # at accept time so a later change to the mapping cannot silently
    # re-point a card the user is already looking at.
    target_status = models.CharField(max_length=32)
    # One line of why: the SUBJECT, at most. §10's "no email bodies" rule,
    # held here the same way `capture.discovery._evidence_line` holds it —
    # and it binds the optional AI path too, whose grounded quote is used to
    # VERIFY an answer and then thrown away, never stored.
    evidence = models.CharField(max_length=300, blank=True, default="")
    # "rules" or "ai" — which layer produced the classification. The
    # deterministic layer is the product; the AI layer is the long tail, and
    # a founder auditing a wrong card needs to know which one to fix.
    detected_by = models.CharField(max_length=16, default="rules")
    # `directory.applications.Match.reason`, verbatim ("title match (83%)",
    # "the only role you had saved at this firm"). Shown on the card: a
    # match a student cannot check is a match they cannot trust.
    match_reason = models.CharField(max_length=200, blank=True, default="")
    thread_id = models.CharField(max_length=128, blank=True, default="")
    # When the message actually arrived — rides into `applied_at` on accept
    # so the funnel's dates are the mail's, not the tap's.
    occurred_at = models.DateTimeField(null=True, blank=True)
    # A deadline the MAIL stated, in words, with a year — "complete the
    # Program Preference Survey by August 30, 2026". Null is the common and
    # honest case: `capture.appmail._due_on` refuses numeric dates, refuses a
    # missing year, and refuses a date with no obligation word in front of it,
    # so nothing here is ever inferred.
    #
    # A DATE, DELIBERATELY, not a datetime. The mail said "11:59 PM EST" and
    # the founder's account is anchored to Asia/Hong_Kong; storing the clock
    # time would mean choosing a timezone for a fact whose timezone we read
    # off prose. The day is the part a student acts on.
    #
    # This is also the one thing on the row a STAGE cannot express, which is
    # why `consider_finding` will card an event that moves no stage but
    # carries a date still ahead of today.
    due_on = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    created = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta(PrivateModel.Meta):
        db_table = "application_events"
        ordering = ["created"]
        constraints = [
            # ONE row per (user, role, event kind), whatever its status.
            # This is both halves of the dedup the brief asks for: the same
            # message seen twice (a re-run, an overlapping history window)
            # resolves to the same triple, and so does the ATS's own
            # re-notification — Workday sends "we received your
            # application" and then a reminder about the same application,
            # and a student should be asked once.
            models.UniqueConstraint(
                fields=["user", "opportunity", "event_type"],
                name="uniq_application_event_user_opp_kind",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.firm_text or self.firm_id} {self.event_type} ({self.status})"


class MailFact(PrivateModel):
    """A fact one message STATED about a person, the verbatim sentence that
    states it, and what Coverage did about it.

    WHY THIS MODEL EXISTS. The pipeline used to understand four kinds of mail
    — a reply, a bounce, a blast, and silence — and flattened everything else
    to nothing. Two real costs on the founder's own mailbox (2026-08-24,
    read-only): an auto-reply saying "Somil Agarwal is no longer with Allen &
    Company. For matters with which Somil was involved, please contact Salima
    Vahabzadeh at salima@allenco.com" was filed as bulk noise, so Coverage
    kept proposing the departed Somil and never saw Salima; and Goldman's
    postmaster saying a real banker's "mailbox is full ... try resending your
    message later" had its routing address discarded. This row is where such
    a fact lands: what was stated, about whom, the QUOTE that grounds it, and
    the action taken — with the action's own undo bookkeeping.

    THE GROUNDING CONTRACT, non-negotiable (`capture.mailfacts`): no row is
    created, and no action is taken, without `quote` holding a verbatim
    sentence from the message that states the fact — the same rule
    `directory.ai_extract` holds for deadlines. A message that gates in but
    yields no quotable fact becomes a `review` row: surfaced, never acted on.

    ON §10 ("no email bodies in logs/notes"), read carefully rather than
    waved at: `quote` IS one sentence of body text, stored deliberately, and
    it is the ONLY place body text lands. It is the justification the user
    audits an automated action against — an action whose reason cannot be
    shown is an action that cannot be argued with — capped at one sentence,
    shown on the card, and never copied into `Contact.notes` (the notes get
    the FACT in Coverage's own words; the quote stays here).

    THE ACT/PROPOSE LINE (`capture.mailfacts` draws it, this model records
    it): actions that are safe and reversible run automatically and land
    `applied` with a visible undo — clearing a departed person's dead
    address (the bounce block's own precedent: address moved to notes, never
    the person archived), withdrawing the departed person's not-yet-accepted
    outreach proposal (restorable via the existing Settings restore path),
    snoozing a follow-up past a stated out-of-office return date, noting an
    alternate/routing address. Creating a PERSON stays propose-then-confirm:
    a referral writes a pending `ContactProposal` (evidence_kind
    "referral"), never a Contact.
    """

    KIND_DEPARTED = "departed"
    KIND_REFERRAL = "referral"
    KIND_OOO = "out_of_office"
    KIND_ADDRESS_CHANGE = "address_change"
    KIND_ROUTING = "routing_address"
    # Gated as an auto-reply, but no deterministic pattern (and no grounded
    # AI answer) could type it. Surfaced for the user, acted on never — the
    # "no quote, no action" fallback made visible instead of silent.
    KIND_REVIEW = "review"
    KIND_CHOICES = [
        (KIND_DEPARTED, "No longer at the firm"),
        (KIND_REFERRAL, "Referred you to someone"),
        (KIND_OOO, "Out of office"),
        (KIND_ADDRESS_CHANGE, "New email address"),
        (KIND_ROUTING, "Alternate routing address"),
        (KIND_REVIEW, "Auto-reply to review"),
    ]

    STATUS_PENDING = "pending"      # surfaced, waiting for the user's look
    STATUS_APPLIED = "applied"      # acted on automatically; undo offered
    STATUS_UNDONE = "undone"        # the user reversed the automated action
    STATUS_DISMISSED = "dismissed"  # the user waved the card away
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPLIED, "Applied"),
        (STATUS_UNDONE, "Undone"),
        (STATUS_DISMISSED, "Dismissed"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    # WHO the fact is about — the departed/OOO/moving person's address, or
    # the address the DSN says it could not deliver to. Normalized lowercase;
    # the dedup constraint below is keyed on it.
    about_email = models.EmailField()
    about_name = models.CharField(max_length=255, blank=True, default="")
    # The address the message NAMES: the referral's (salima@...), the routing
    # address a DSN expanded to, the "I've moved to" destination. Blank for
    # kinds that name none.
    new_email = models.EmailField(blank=True, default="")
    new_name = models.CharField(max_length=255, blank=True, default="")
    # The stated out-of-office return date. Only ever a DATE (same reasoning
    # as ApplicationEvent.due_on): the day is what a follow-up waits for.
    return_on = models.DateField(null=True, blank=True)
    # THE GROUNDING. One verbatim sentence from the message. Required for
    # every kind except `review`, whose whole meaning is "no quotable fact".
    quote = models.CharField(max_length=500, blank=True, default="")
    # The message's Subject — the same §10-safe evidence line every other
    # capture surface shows.
    subject = models.CharField(max_length=300, blank=True, default="")
    # "rules" or "ai" — which layer read the fact. The deterministic layer is
    # the product; the AI layer is the long tail, and an audit of a wrong
    # card needs to know which one to fix.
    detected_by = models.CharField(max_length=16, default="rules")
    thread_id = models.CharField(max_length=128, blank=True, default="")
    occurred_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    # One plain sentence saying what the automated action actually did
    # ("address cleared", "follow-up snoozed to Sep 2") — the card's second
    # line, written at apply time so a later copy change can't re-describe
    # history.
    action_note = models.CharField(max_length=300, blank=True, default="")

    # --- undo bookkeeping, per action --------------------------------- #
    contact = models.ForeignKey(
        "crm.Contact", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="mail_facts",
    )
    proposal = models.ForeignKey(
        ContactProposal, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="mail_facts",
    )
    # The address a `departed` apply cleared off the contact — undo puts it
    # back (only if the column is still blank, so a hand-typed correction is
    # never overwritten).
    prior_email = models.EmailField(blank=True, default="")
    # The exact `snoozed_until` an `out_of_office` apply wrote — undo clears
    # it only while it still holds this value, so a snooze the user set
    # themselves afterwards stands.
    snoozed_to = models.DateTimeField(null=True, blank=True)
    # The `snoozed_until` that stood BEFORE the apply moved it. `_extend_snooze`
    # only ever moves the clock forward, which means it can move it forward
    # OVER a snooze the user set themselves — and undo used to reset to None,
    # destroying the user's own earlier value instead of restoring it. Null
    # when there was nothing to preserve, in which case undo's None is exact.
    prior_snoozed_until = models.DateTimeField(null=True, blank=True)
    # The exact dated line appended to `Contact.notes` — undo removes this
    # line and nothing else.
    note_line = models.CharField(max_length=500, blank=True, default="")

    created = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta(PrivateModel.Meta):
        db_table = "mail_facts"
        ordering = ["created"]
        constraints = [
            # One row per (user, person, kind of fact) — the same
            # remembered-forever dedup ContactProposal keys on its address.
            # The same auto-responder firing on a re-scan (or on the user's
            # own follow-up) resolves to the row that already exists, so
            # nothing is re-applied and nothing re-asks.
            models.UniqueConstraint(
                fields=["user", "about_email", "kind"],
                name="uniq_mail_fact_user_about_kind",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind}: {self.about_name or self.about_email} ({self.status})"


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


class AutopilotRun(PrivateModel):
    """One reviewed batch of AI decisions over a scan's proposals — decided
    unattended, applied on the user's own tap, never both in one step.

    THE COMPLIANCE SHAPE, DECIDED FIRST. `ContactProposal` and
    `ApplicationEvent` both exist to keep one sentence true: "mail read on a
    student's behalf may PROPOSE, and only the student's own tap may change
    their record" — the Limited Use posture the whole B2B plan rests on
    (see ApplicationEvent's docstring, docs/gmail-live-setup.md §4). An
    autonomous pass that silently wrote fifty contacts out of mailbox
    content would void that posture in exactly the way it was designed to
    prevent. So this model splits the work the way the posture splits it:

      - DECIDE (unattended, cheap, grounded): the AI reads every pending
        proposal, decides accept-or-needs-you for each, and stores the
        verdicts here — with the verbatim quote each one stands on. No
        Contact, no Touch, no pipeline move. A run that dies mid-decide
        has written only opinions.
      - APPLY (one tap): the user taps once over the whole reviewed batch
        and `capture.autopilot.apply_run` executes every accept through
        the same `discovery.accept` / `appmail.accept` doors a card tap
        uses. One tap over a disclosed, quoted batch is the same
        compliance object as fifty card taps — and the hands-off
        experience is the difference between one tap and fifty.

    Status is the fail-closed mechanism: only a run that finished deciding
    (REVIEWED) can ever be applied. Apply is deliberately NOT one wrapping
    transaction — the warmth ratchet opens its own psycopg connection, so
    the guarantee is per-decision completeness plus idempotent resume (see
    `capture.autopilot.apply_run`); the run flips to APPLIED only after the
    whole batch. A run that died deciding stays RUNNING/FAILED forever and
    can touch nothing.
    """

    # QUEUED is the state a user-started run is born in: the button writes
    # the row and returns, and the worker (`capture_autopilot_worker`)
    # claims it. It exists as a ROW rather than as an in-process job for
    # the reason the rescan queue gives (capture.views.gmail_rescan): 52
    # sequential model calls is minutes of work, and no POST may wait on
    # it. It is also the re-entrancy guard — see `uniq_autopilot_active`
    # below: a second tap cannot write a second active row, so "two taps,
    # one run" is a database fact, not a race the view hopes to win.
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_REVIEWED = "reviewed"    # decided, waiting for the user's tap
    STATUS_APPLIED = "applied"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Deciding"),
        (STATUS_REVIEWED, "Reviewed — waiting for your tap"),
        (STATUS_APPLIED, "Applied"),
        (STATUS_FAILED, "Failed"),
    ]
    # The two states in which a run still owes the user an answer. One per
    # user at a time (the constraint below), and the pair the stale reaper
    # measures against.
    ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING
    )
    # Where the batch came from, for the audit trail — the findings file's
    # name or the sync's own label. Display only, never parsed.
    source_label = models.CharField(max_length=200, blank=True, default="")
    model = models.CharField(max_length=100, blank=True, default="")
    # The split, denormalized so the log page and the Today strip never
    # re-count: how many rows the AI said "add" to, how many it handed
    # back, how many it skipped without spending a call (already decided,
    # overridden, no longer pending), and how many it never reached
    # because a ceiling bound first (the fail-closed remainder — those
    # rows simply stay pending cards, exactly as if no run happened).
    accepts = models.IntegerField(default=0)
    escalations = models.IntegerField(default=0)
    skips = models.IntegerField(default=0)
    deferred = models.IntegerField(default=0)
    llm_calls = models.IntegerField(default=0)
    credits_spent = models.IntegerField(default=0)
    # What the decide pass could and could NOT see when it judged this
    # batch — written by `capture.autopilot.run_autopilot`, shown on the
    # ledger and on Today. It exists because the opposite (a run that
    # quietly judged 53 rows having been told "OTHER MAIL … none found")
    # produced a whole batch of wrong accepts and said nothing about it.
    # A run that reads less than the mailbox knows has to say so.
    evidence_note = models.CharField(max_length=300, blank=True, default="")
    # Why a FAILED run failed, in the user's language. A failed run that
    # renders identically to a running one is the "silently died" state
    # this field exists to make impossible.
    failure_reason = models.CharField(max_length=300, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)
    # When a worker actually claimed the row (NOT when it was queued —
    # `GmailConnection.rescan_started_at` carries the same distinction for
    # the same reason: staleness measured from the request reclaims runs
    # that were only slow to start, and charges twice for them).
    started_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta(PrivateModel.Meta):
        db_table = "autopilot_runs"
        ordering = ["-created"]
        constraints = [
            # "Two taps, one run", enforced by the database rather than by
            # the view's read-then-write. At most one queued-or-running run
            # per user; a second concurrent POST loses the insert with an
            # IntegrityError, which `queue_run` catches and reports as
            # "already running" — the same answer the pre-check gives, now
            # true even when both requests read an empty table.
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(status__in=["queued", "running"]),
                name="uniq_autopilot_active",
            ),
        ]

    def __str__(self) -> str:
        return f"AutopilotRun #{self.pk} ({self.status})"


class AutopilotDecision(PrivateModel):
    """One AI verdict about one pending row, and the quote it stands on.

    GROUNDED EVIDENCE OR NO ACTION — `directory/ai_extract.py`'s rule,
    inherited whole: `quote` is the exact substring of the evidence text
    the model was shown, verified by `capture.autopilot._grounded` before
    the decision is stored. A verdict whose quote failed verification is
    stored as an escalation ("needs you"), never as an accept — an
    ungrounded answer takes no action, it asks for a human.

    THE OVERRIDE IS PERMANENT — AND IT IS ONLY EVER THE USER'S WORD.
    `overridden=True` is set the moment the user undoes an applied decision
    (or the row it points at was resolved by their own hand before apply
    reached it), and `capture.autopilot.run_autopilot` refuses to ever
    re-decide a proposal that carries an overridden decision. The user's
    word outranks every future run, the same way a dismissed proposal
    outranks every future scan.

    A row resolved by AUTOMATION between decide and tap — a mail fact
    withdrawing a departed person's pending proposal (capture.mailfacts) —
    is NOT the user's word, so it must not wear the same flag: that
    decision is marked `superseded` instead (see STATUS_SUPERSEDED below),
    which blocks nothing. If the row ever comes back to pending (the user
    restores the withdrawn card), a future run decides it afresh — no
    human ever weighed in, so there is no override to honor. Before this
    split, an auto-withdrawal was stamped `overridden` and the row was
    locked out of every future run by a decision no person made.

    Exactly one of `proposal` / `app_event` is set — the two kinds of
    pending row a scan can leave behind. PROTECT, not CASCADE: a decision
    is the audit trail for something the AI did or asked; deleting the row
    it judged must not silently delete the record of the judgment.
    """

    DECIDE_ACCEPT = "accept"
    DECIDE_ESCALATE = "escalate"
    DECIDE_CHOICES = [
        (DECIDE_ACCEPT, "Add it"),
        (DECIDE_ESCALATE, "Needs you"),
    ]

    STATUS_PROPOSED = "proposed"    # decided, not yet applied
    STATUS_APPLIED = "applied"
    STATUS_UNDONE = "undone"
    # The row this decision judged was resolved by AUTOMATION (a mail-fact
    # withdrawal) between decide and tap, so the decision was never offered
    # and never applied. Deliberately distinct from `overridden`: that flag
    # is the user's word and permanent; this status is bookkeeping about a
    # machine action and blocks no future run. See the class docstring.
    STATUS_SUPERSEDED = "superseded"
    STATUS_CHOICES = [
        (STATUS_PROPOSED, "Proposed"),
        (STATUS_APPLIED, "Applied"),
        (STATUS_UNDONE, "Undone"),
        (STATUS_SUPERSEDED, "Superseded"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    run = models.ForeignKey(
        AutopilotRun, on_delete=models.CASCADE, related_name="decisions"
    )
    proposal = models.ForeignKey(
        ContactProposal, null=True, blank=True, on_delete=models.PROTECT,
        related_name="autopilot_decisions",
    )
    app_event = models.ForeignKey(
        ApplicationEvent, null=True, blank=True, on_delete=models.PROTECT,
        related_name="autopilot_decisions",
    )
    decision = models.CharField(max_length=16, choices=DECIDE_CHOICES)
    # 0.0–1.0 as the model reported it; the floor that turned it into
    # accept-or-escalate lives in capture.autopilot.ACCEPT_FLOOR, and a
    # deterministic decision (an app event already typed at capture) is
    # stored at 1.0 with detected_by="deterministic".
    confidence = models.FloatField(default=0.0)
    # The verbatim, verified substring of the evidence text this verdict
    # stands on. §10's "no email bodies" rule applies: the evidence text is
    # built from what the proposal/event rows already store (subjects,
    # evidence lines) plus counter-evidence snippets the scan surfaced —
    # never a full body.
    quote = models.CharField(max_length=500, blank=True, default="")
    reason = models.CharField(max_length=300, blank=True, default="")
    detected_by = models.CharField(max_length=32, default="ai")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PROPOSED
    )
    # The user's word, permanent — see the class docstring.
    overridden = models.BooleanField(default=False)
    # What apply actually did, for undo: the contact the accept resolved to,
    # and whether apply CREATED it (undo deletes it) or matched one that
    # already existed (undo removes only the touch it logged).
    contact = models.ForeignKey(
        "crm.Contact", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="autopilot_decisions",
    )
    created_contact = models.BooleanField(default=False)
    touch_id = models.IntegerField(null=True, blank=True)
    # For an app-event accept: the `UserOpportunity` fields as they stood
    # BEFORE apply ({"applied_status": ..., "applied_at": ..., "dismissed":
    # ...}), so undo can put back exactly what was there rather than
    # guessing at a rank ladder in reverse. Empty for proposal decisions.
    undo_state = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta(PrivateModel.Meta):
        db_table = "autopilot_decisions"
        ordering = ["created"]
        constraints = [
            # A decision is about exactly one pending row.
            models.CheckConstraint(
                condition=(
                    models.Q(proposal__isnull=False, app_event__isnull=True)
                    | models.Q(proposal__isnull=True, app_event__isnull=False)
                ),
                name="autopilot_decision_one_subject",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.decision} ({self.status})"
