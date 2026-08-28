"""Gmail-findings capture provider — the daily sync's route into Coverage.

WHAT THIS IS
------------
`providers.py` promises that adding a source is "a new subclass, not a pipeline
change". This is that subclass for the founder's existing daily Gmail sync.

The sync itself is unchanged and lives outside this repo: an agent searches the
mailbox with the Gmail API and emits a list of per-contact *findings*. It has
always been a two-part design — the search runs somewhere else, and
`netdash/gmail_enrich.py` is "a pure apply layer" (its words) that writes those
findings into the single-user `campaign.db`. This module is the same apply
layer pointed at Coverage instead, so one search feeds both systems and the
founder's mailbox reaches the multi-tenant app without a second scan.

Deliberately NOT Gmail OAuth. Nothing here talks to Google; it consumes findings
someone else produced. The `gmail.*` restricted-scope decision (deploy.md §4,
build-plan §7 "Gmail-OAuth decision gate") stays open and untouched — this
provider is what makes it safe to defer, because it proves the capture loop with
real mailbox data while sign-in keeps only `openid`/`email`/`profile`.

THE PORTED RATCHET — the reason this is not three lines
--------------------------------------------------------
`coverage_domain.pipeline` deliberately excludes the caller-side thread ratchet
(see its docstring: "That caller-side ratchet is a separate port ... and is
intentionally NOT part of this package"). Without it, `apply_touch` will move
`thread_state` *backward*: a still-open thread re-found tomorrow at
`chat_scheduled`, after a `chat` already landed, regresses a contact who has
already had the conversation. That is the port, and it is what makes this safe
to run on a schedule rather than once.

Two dedup layers, both from the original:

- **With a `thread_id`** — every touch note carries a `[gmail:<id>]` marker, and
  a finding only logs if it ranks strictly ABOVE the highest stage already
  logged for that thread. A ratchet, not a seen/not-seen check, because one
  thread legitimately climbs reply -> scheduled -> chat over several days.
- **Without one** — no stable key exists, so a same-kind touch inside the last
  week counts as the same event. Otherwise an open thread re-logs daily, which
  silently resets `last_touch` and means the cadence engine's follow-up nudges
  never come due.

`outreach` is dedup'd per contact rather than per thread ("have I ever sent a
first note" is a per-contact fact), and is off the ladder entirely, so an
outreach marker never blocks a later reply on the same thread. A LATER
outbound send to a contact whose first note is on record logs as
`follow_up` — the kind branch 6 of the cadence engine counts toward
`max_cold_touches`, which no capture path used to write at all (so a cold
contact could never earn the park suggestion through capture). See
`_follow_up_action` for exactly what qualifies and what never does.

Every touch is written through `crm.services.log_touch`, so warmth advances via
the same ported ratchet as every other front end. No email body is ever stored:
`evidence` is a short caller-supplied summary, and §10's "no email bodies in
logs/notes" applies to it the same as anywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from analytics.events import record_event
from capture import appmail, discovery, mailfacts
from capture.providers import AmbiguousContactError, CaptureProvider, InteractionEvent
from coverage_domain.pipeline import BULK_RECEIVED_KIND
from crm import campaigns as crm_campaigns, services as crm_services
from crm.models import CalendarEvent, Contact, Touch
from directory.models import EmailPatternStats

EXTRACTION_VERSION = "gmail-findings-1"
TOUCH_CHANNEL = "email"  # every finding here comes from a Gmail thread

# The thread ladder. `outreach` is deliberately absent — it is not a stage.
# `bulk_received` is absent for a stronger reason: a mass email landing on a
# thread must never block the genuine reply that arrives on it later, and it
# must never be treated as a stage that a later finding has to outrank. It
# gets its own dedup instead (`_bulk_already_logged`).
THREAD_STAGE_RANK = {"reply_received": 1, "chat_scheduled": 2, "chat": 3}
_STAGE_NAME = {rank: kind for kind, rank in THREAD_STAGE_RANK.items()}

# Findings with no thread_id: treat a same-kind touch inside this window as the
# same event.
NO_THREAD_DEDUP_DAYS = 7

# Touch kinds that mean "a first note has gone out", for the per-contact
# outreach guard. Mirrors how the cadence engine itself decides whether first
# outreach is still due.
_OUTREACH_KINDS = ("outreach", "follow_up")


@dataclass
class SyncResult:
    """Per-run counters. Mirrors the original's summary dict closely enough
    that the two systems' daily reports can be read side by side."""

    findings: int = 0
    emails_backfilled: int = 0
    # A finding whose address DIFFERS from the one already on the contact.
    # Counted separately from `emails_backfilled` because it is a different
    # event with a different outcome: the alternate is written to the notes
    # and the primary address is left alone. Lumping the two together is
    # exactly how "backfill" quietly came to mean "overwrite".
    alternate_emails_noted: int = 0
    outreach_logged: int = 0
    # A SECOND outbound send to a contact whose first note is already on
    # record — logged as kind `follow_up`, the kind the cadence engine's
    # branch 6 counts toward `max_cold_touches` and nothing anywhere used
    # to log. Counted apart from `outreach_logged` because the two answer
    # different questions ("how many first notes" vs "how many bumps"),
    # and because this counter is the one that changes park behaviour:
    # every follow-up here moves a cold contact one step toward the park
    # threshold. See `_follow_up_action` for what qualifies.
    follow_ups_logged: int = 0
    touches_logged: int = 0
    # Inbound messages recorded as `bulk_received` — a mass invitation, a
    # newsletter, an out-of-office. Counted SEPARATELY from
    # `touches_logged` rather than folded into it: `touches_logged` is read
    # as "how much relationship progress did this run find", and a blast is
    # not progress. Reporting them together is how "we heard from 12 people
    # today" came to mean "12 lists mailed you".
    bulk_logged: int = 0
    # Bounced findings whose address was CLEARED off the contact (was
    # `archived_bounced`, which named an action no automated path performs
    # any more — see the bounce block in `apply_findings`).
    bounced_cleared: int = 0
    skipped_not_found: int = 0
    skipped_already_logged: int = 0
    skipped_unmatched: int = 0
    # Of the unmatched: senders the judgment chain (capture.discovery) found
    # worth tracking. A PROPOSAL row each — never a contact; the user's tap
    # on the Today page is what creates anything. Counted alongside
    # `skipped_unmatched` rather than instead of it: the finding still did
    # not land on a contact, and that stays visible.
    proposals_created: int = 0
    # Unmatched senders who matched an ARCHIVED contact: reported, never
    # proposed, never resurrected (capture_discover's rule, held here too).
    proposals_archived_match: int = 0
    # Pending outreach-evidence proposals upgraded in place because the
    # person wrote back later in the window. Not new rows — counted apart
    # from `proposals_created` so "N people found" stays true.
    proposals_upgraded: int = 0
    # Application-status mail (capture.appmail): an ATS saying a role moved.
    # Counted on its own axis rather than inside the touch counters because
    # it is about the APPLICATION pipeline, not about a relationship — and
    # like a contact proposal it writes only a pending row the user must
    # confirm. `unresolved` is the honest residue: mail we recognised as
    # application-status but could not pin to one role, which is REPORTED
    # rather than guessed (directory.applications' rule).
    app_events_proposed: int = 0
    app_events_unresolved: int = 0
    # Mail facts (capture.mailfacts): what an auto-reply or soft bounce
    # STATED about a person, acted on with a grounded quote. `applied` are
    # the safe-and-reversible automatic actions (address cleared for a
    # departure, follow-up snoozed to an OOO return date, routing address
    # noted); `surfaced` are the no-quote / nothing-to-change cards left for
    # the user; `referral` are the pending ContactProposals a "please
    # contact X at Y" wrote — counted apart from `proposals_created` because
    # the person never wrote to the user and the card must say so.
    mail_facts_applied: int = 0
    mail_facts_surfaced: int = 0
    referral_proposals: int = 0
    # Detected, matched, and already at or past the stage the mail claims —
    # the ATS's own reminder about something the board already knows.
    app_events_already: int = 0
    # Distinct from skipped_unmatched: the name matched MORE than one
    # contact (e.g. two "Michael Chen"s at different firms), so there is
    # no correct contact to log this finding against — not zero matches,
    # too many. Reported separately so it doesn't hide inside "unmatched".
    skipped_ambiguous: int = 0
    # Per-firm email-format evidence. A delivered message proves the address
    # format guessed for that firm works; a bounce proves it doesn't. The
    # counts live on the SHARED `EmailPatternStats` (keyed on firm, no user
    # column) because the aggregate helps everyone while the raw events stay
    # in the user's own private Touch rows — build-plan §2's split exactly.
    pattern_delivered: int = 0
    pattern_bounced: int = 0
    # Scheduled chats that carried a real datetime and landed on the calendar.
    chats_scheduled: int = 0
    details: list[str] = field(default_factory=list)

    def as_stats(self) -> dict:
        return {
            "findings": self.findings,
            "emails_backfilled": self.emails_backfilled,
            "alternate_emails_noted": self.alternate_emails_noted,
            "outreach_logged": self.outreach_logged,
            "follow_ups_logged": self.follow_ups_logged,
            "touches_logged": self.touches_logged,
            "bulk_logged": self.bulk_logged,
            "bounced_cleared": self.bounced_cleared,
            "skipped_not_found": self.skipped_not_found,
            "skipped_already_logged": self.skipped_already_logged,
            "skipped_unmatched": self.skipped_unmatched,
            "proposals_created": self.proposals_created,
            "proposals_archived_match": self.proposals_archived_match,
            "proposals_upgraded": self.proposals_upgraded,
            "app_events_proposed": self.app_events_proposed,
            "app_events_unresolved": self.app_events_unresolved,
            "mail_facts_applied": self.mail_facts_applied,
            "mail_facts_surfaced": self.mail_facts_surfaced,
            "referral_proposals": self.referral_proposals,
            "app_events_already": self.app_events_already,
            "skipped_ambiguous": self.skipped_ambiguous,
            "pattern_delivered": self.pattern_delivered,
            "pattern_bounced": self.pattern_bounced,
            "chats_scheduled": self.chats_scheduled,
        }


class GmailFindingsProvider(CaptureProvider):
    """Turns one Gmail finding into an :class:`InteractionEvent`.

    Finding shape (unchanged from the daily sync's existing output)::

        {"contact_id": int|None, "name": str, "email": str|None, "found": bool,
         "bounced": bool, "outreach_sent": bool, "replied": bool,
         "chat_status": "none"|"scheduled"|"completed",
         "evidence": str|None, "thread_id": str|None,
         "bulk": bool, "bulk_reasons": str|None}

    ``bulk`` (optional, defaults False — every finding written before it
    existed keeps behaving exactly as it did) says the message is a mass or
    automated one: a programme invitation sent to a list, a newsletter, an
    out-of-office. It OVERRIDES ``replied``/``chat_status`` (see
    ``_touch_kind_for``) and produces a ``bulk_received`` touch, which moves
    neither warmth nor thread_state. ``bulk_reasons`` is the short,
    human-readable why — it goes in the touch note, because a demotion the
    user cannot see the reason for is a demotion they cannot argue with.
    `capture.inbound` produces both for the Gmail Live path; the daily
    agent-run sync may set them itself.

    ``contact_id`` is a *campaign.db* id and is not used for matching here —
    Coverage resolves by email, then name, so the two systems' id spaces never
    have to be kept in step.
    """

    name = "gmail"

    def build_event(
        self, finding: dict, user, touch_kind: str, *, occurred_at=None
    ) -> InteractionEvent:
        thread_id = (finding.get("thread_id") or "").strip()
        email = (finding.get("email") or "").strip()
        # Dedup key: the thread + the stage within it. Without a thread id
        # there is nothing stable to key on, so fall back to the counterparty
        # and let the time-window guard below do the deduping instead.
        ref = f"gmail:{thread_id}:{touch_kind}" if thread_id else f"gmail:{email or finding.get('name', '')}:{touch_kind}"
        return InteractionEvent(
            user_id=user.id,
            provider=self.name,
            provider_ref=ref[:255],
            # A finding describes a thread, not a single message. Reply and
            # chat evidence is what the counterparty did, so it is recorded
            # inbound; a never-answered sent note — the first one or a
            # follow-up — is outbound.
            direction="outbound" if touch_kind in _OUTREACH_KINDS else "inbound",
            counterparty_email=email,
            counterparty_name=(finding.get("name") or "").strip(),
            # `occurred_at` is when the finding's underlying message actually
            # happened, if the caller knows (the backfill command does; the
            # daily agent-run sync doesn't supply one and gets "now", exactly
            # its old behavior). See `_finding_occurred_at` for the clamp.
            occurred_at=occurred_at or timezone.now(),
            raw_ref=f"gmail-thread:{thread_id}" if thread_id else "",
            signals={
                "replied": bool(finding.get("replied")),
                "chat_status": finding.get("chat_status", "none"),
                "outreach_sent": bool(finding.get("outreach_sent")),
                "thread_id": thread_id,
            },
            extraction_version=EXTRACTION_VERSION,
            touch_kind=touch_kind,
        )


# --------------------------------------------------------------------------- #
# Matching + the ported dedup ratchet
# --------------------------------------------------------------------------- #

def _match_contact(user, finding: dict) -> Contact | None:
    """Resolve a finding to one of the user's existing contacts by email, then
    by name. Unlike ``capture.services.resolve_contact`` this deliberately does
    NOT auto-create: a finding is the result of searching a mailbox for people
    already being tracked, so an unmatched one means the two systems have
    drifted — worth reporting, not worth inventing a contact for.

    Shares its two conclusive identity rungs with
    ``capture.discovery._match_existing`` — ``discovery.routing_variant``
    (same localpart, one domain a routing extension of the other, e.g.
    Goldman's ``noah.bauld@ny.ibd.email.gs.com`` for ``noah.bauld@gs.com``)
    and ``discovery.names_equivalent`` (``Last, First`` inversion, diacritics,
    middle initials) — rather than the plain lowercase-and-strip
    ``normalize_name`` this used to name-match with, which could not see a
    corporate address book's inverted display form ("Nunley, Vanessa N").
    It stays a SEPARATE function rather than a call into
    ``_match_existing``, because the two have different contracts:
    ``_match_existing`` matches across every row including archived ones and
    never raises; this one is scoped to ACTIVE contacts only (a finding
    should not silently resurrect an archived row) and RAISES when a name
    resolves to more than one active contact rather than guessing. See
    ``discovery.py``'s module comment on the identity ladder for why the
    rungs live there and not here.

    Raises:
        AmbiguousContactError: more than one contact shares the equivalent
            name (e.g. two "Michael Chen"s at different firms) and there's
            no email on the finding to disambiguate with. Silently picking
            the first one found (the previous behavior) would land the
            touch on whichever row the queryset happened to yield first.
    """
    scoped = Contact.objects.for_user(user).filter(archived=False)
    email = (finding.get("email") or "").strip()
    if email:
        match = scoped.filter(email__iexact=email).first()
        if match:
            return match
        match = next(
            (c for c in scoped if c.email and discovery.routing_variant(email, c.email)),
            None,
        )
        if match:
            return match
    name = (finding.get("name") or "").strip()
    if name:
        matches = [
            contact for contact in scoped
            if contact.name and discovery.names_equivalent(contact.name, name)
        ]
        if len(matches) > 1:
            raise AmbiguousContactError(name, len(matches))
        if matches:
            return matches[0]
    return None


def thread_stage_rank(user, contact: Contact, thread_id: str) -> int:
    """Highest ladder stage already logged for this contact from this thread.
    0 means nothing staged yet — an ``outreach`` touch can carry the same
    marker but is off the ladder, so it never blocks a reply or chat."""
    marker = f"[gmail:{thread_id}]"
    kinds = (
        Touch.objects.for_user(user)
        .filter(contact=contact, note__contains=marker)
        .values_list("kind", flat=True)
    )
    return max((THREAD_STAGE_RANK.get(kind, 0) for kind in kinds), default=0)


def _logged_recently(user, contact: Contact, kind: str, *, reference=None) -> bool:
    """Fallback dedup for findings carrying no thread_id (see module docstring).
    Unlike the original's local-naive-vs-UTC hazard, both sides here are aware
    datetimes, so the window is exactly seven days.

    `reference` is the finding's OWN time, not necessarily now — the backfill
    command applies findings months old, and "was there a same-kind touch
    within 7 days of NOW" is the wrong question for one of those (it would
    either never match, missing a real duplicate, or match against something
    that happened months apart). Defaults to now, which reproduces the
    original behavior exactly for the daily sync's undated findings. The
    window is symmetric around `reference` rather than a plain look-back:
    "recent" has to mean near the finding's own time in EITHER direction, or
    a live touch from today would look like a duplicate of a backfill
    finding from six months ago just because both are ">= cutoff" of a
    now-anchored window.
    """
    ref = reference or timezone.now()
    window = timedelta(days=NO_THREAD_DEDUP_DAYS)
    return (
        Touch.objects.for_user(user)
        .filter(contact=contact, kind=kind, ts__gte=ref - window, ts__lte=ref + window)
        .exists()
    )


# What `_follow_up_action` decided about a second outbound send.
FOLLOW_UP_LOG = "log"        # a real second note — log kind `follow_up`
FOLLOW_UP_MERGE = "merge"    # a mail-merge wave — never a follow-up
FOLLOW_UP_SKIP = "skip"      # this send is already on record


def _outbound_on_thread(user, contact: Contact, thread_id: str) -> bool:
    """Whether an outbound touch already carries this thread's marker —
    i.e. this thread's send has been recorded once."""
    marker = f"[gmail:{thread_id}]"
    return (
        Touch.objects.for_user(user)
        .filter(contact=contact, kind__in=_OUTREACH_KINDS, note__contains=marker)
        .exists()
    )


def _outbound_logged_near(user, contact: Contact, *, reference=None) -> bool:
    """An outreach/follow_up touch within ±NO_THREAD_DEDUP_DAYS of the
    finding's own time. The same symmetric window `_logged_recently` uses,
    widened to both outbound kinds: a touch near this send's own instant is
    overwhelmingly THIS send, recorded through another door (a hand-logged
    entry, the daily agent sync, an earlier scan) — not evidence of a
    second note."""
    ref = reference or timezone.now()
    window = timedelta(days=NO_THREAD_DEDUP_DAYS)
    return (
        Touch.objects.for_user(user)
        .filter(
            contact=contact, kind__in=_OUTREACH_KINDS,
            ts__gte=ref - window, ts__lte=ref + window,
        )
        .exists()
    )


def _merge_shaped_send(user, finding: dict, batch) -> bool:
    """Whether this outbound send is a mail-merge wave rather than a
    personal note — the same two tests `capture.discovery` applies before
    proposing from an outbound finding, mirrored here so a blast's second
    wave cannot become N follow-ups (each of which would march a contact
    toward the park threshold off a mass send):

      - more than MERGE_RECIPIENT_LIMIT distinct recipients share this
        normalized subject inside the batch in front of us;
      - the subject's signature matches a DETECTED campaign the user has
        not classified as their own recruiting. A campaign the user has
        explicitly called their recruiting is their outreach by their own
        word, so its second wave counts.
    """
    subject = (finding.get("subject") or "").strip()
    if not subject:
        return False
    signature = crm_campaigns.normalize_subject(subject)
    if not signature:
        return False
    if batch is not None and (
        batch.outbound_subject_recipients.get(signature, 0)
        > discovery.MERGE_RECIPIENT_LIMIT
    ):
        return True
    from crm.models import Campaign

    return (
        Campaign.objects.for_user(user)
        .filter(signature=signature)
        .exclude(kind=Campaign.KIND_RECRUITING)
        .exists()
    )


def _follow_up_action(
    user, contact: Contact, finding: dict, *, thread_id: str,
    finding_time, batch,
) -> str:
    """Decide what a SECOND outbound send to an already-contacted person is.

    The judgement this encodes (2026-08-27, closing the audit's biggest
    deferral): kind `follow_up` was never logged from any capture path, so
    the cadence engine's branch 6 saw `outbound` stuck at 1 forever — the
    "no reply after touch 1 — follow up" prompt re-rendered indefinitely,
    `max_cold_touches` was unreachable, and a cold contact could never earn
    the park suggestion through capture. The founder sends his follow-ups
    from Gmail (measured on his live mailbox: they are same-thread "just
    following up" replies), so capture is exactly where they must be
    recognised.

    What counts as what:

      - FIRST NOTE: no outreach/follow_up touch exists for the contact.
        Handled by the caller (logs `outreach`, unchanged).
      - RE-SEEN SEND: the same message coming past again (a rescan, the
        daily sync re-emitting a thread summary, a hand-logged duplicate).
        Never logged twice. Recognised two ways: the thread already carries
        an outbound marker and the finding has no message time of its own
        (a thread-level summary says nothing new), or an outbound touch
        already sits within ±NO_THREAD_DEDUP_DAYS of the finding's own
        time (that touch IS this send).
      - MERGE WAVE: `_merge_shaped_send` above — never a follow-up.
      - FOLLOW-UP: everything else — a dated send at least the dedup
        window after every recorded outbound touch (same thread or a new
        one), or an undated new-thread send from the daily sync (which the
        thread marker then guards from re-logging tomorrow).

    Two accepted costs, stated rather than hidden: a genuine second note
    sent within NO_THREAD_DEDUP_DAYS of the first is suppressed (the
    cadence's own follow-up window is 6 business days, so a real follow-up
    almost always clears it), and a threadless, undated finding never
    logs a follow-up at all (nothing could stop it re-logging weekly).
    """
    if finding.get("replied") or finding.get("bulk"):
        # A thread summary that says they replied has nothing to follow up;
        # a bulk send is not a note to this person.
        return FOLLOW_UP_SKIP
    if _merge_shaped_send(user, finding, batch):
        return FOLLOW_UP_MERGE
    if thread_id and _outbound_on_thread(user, contact, thread_id):
        # This thread's send is on record. Only a message carrying its own
        # real time can prove it is a DIFFERENT, later send on the same
        # thread (the founder's actual follow-up shape) — an undated
        # thread summary cannot, and treating it as one would re-log
        # weekly forever.
        if finding_time is None:
            return FOLLOW_UP_SKIP
        if _outbound_logged_near(user, contact, reference=finding_time):
            return FOLLOW_UP_SKIP
        return FOLLOW_UP_LOG
    if not thread_id and finding_time is None:
        # Nothing to dedup a re-emission against — the one shape that must
        # keep the old skip, or it logs a fresh follow-up every week the
        # thread stays in the search window.
        return FOLLOW_UP_SKIP
    if _outbound_logged_near(user, contact, reference=finding_time):
        return FOLLOW_UP_SKIP
    return FOLLOW_UP_LOG


def _finding_occurred_at(finding: dict):
    """The finding's own `occurred_at`, clamped to never exceed now — see
    `crm.services.log_touch`'s docstring: "Callers are responsible for
    clamping a forwarded/synced timestamp to not exceed 'now'... this
    function does not second-guess what it's given." Returns None (meaning
    "right now", `log_touch`'s own default) when the finding carries no
    parseable timestamp — every finding the daily agent-run sync has ever
    produced has no `occurred_at` field at all, and must keep behaving
    exactly as it always has.
    """
    raw = (finding.get("occurred_at") or "").strip()
    if not raw:
        return None
    when = parse_datetime(raw)
    if when is None:
        return None
    if timezone.is_naive(when):
        # `dt_timezone.utc`, not `timezone.utc`: Django 5 removed the
        # `django.utils.timezone.utc` alias, so the old spelling raised
        # AttributeError on the first naive timestamp — killing the whole
        # apply pass (and, on the poll loop, re-processing the same
        # messages every interval forever because the cursor never
        # advanced past them).
        when = timezone.make_aware(when, dt_timezone.utc)
    return min(when, timezone.now())


def _append_note(contact: Contact, line: str) -> None:
    """Append one dated line to `Contact.notes`.

    `notes` is an append-only journal everywhere else in this codebase
    (`crm.debrief._append_note` writes the same shape), so a fact discovered
    by the sync joins the record rather than replacing part of it. Does not
    save — the caller batches this with whatever column it is also changing,
    so one bounce is one UPDATE.
    """
    stamped = f"— {timezone.localdate():%b %d, %Y} — {line}"
    existing = (contact.notes or "").rstrip()
    contact.notes = f"{existing}\n{stamped}" if existing else stamped


def _note_bounce(contact: Contact, address: str) -> None:
    """Record the address that bounced before clearing the column, so the
    information is moved rather than destroyed — the user may recognise a
    typo, or want to try a variant of it."""
    _append_note(contact, f"{address} bounced (Gmail sync) — cleared from the contact.")


def _note_alternate_email(contact: Contact, address: str) -> None:
    """Record a second address the sync found, leaving the primary in place.
    Saves immediately: nothing else on the row is changing in this branch."""
    _append_note(contact, f"Gmail sync also saw this person at {address}.")
    contact.save(update_fields=["notes"])


def _record_pattern_evidence(
    contact: Contact, *, delivered: bool, dry_run: bool = False
) -> bool:
    """Bank one firm-level data point about whether its address format works.

    Called at most once per contact, ever: `Contact.email_pattern_recorded`
    is the guard. Without it a contact whose thread stays in the search
    window would re-bank the same evidence every single day, and a firm's
    confidence would climb on one real send.

    Returns True when the evidence was banked — or, under `dry_run`, when it
    WOULD have been. `dry_run` lives in here rather than at the call sites on
    purpose: both callers previously re-derived this eligibility rule
    themselves to keep their dry-run counters right, which is the same rule
    written three times and free to drift the moment the guard below changes.

    A contact with no firm has nothing to say about any firm's format.
    """
    if contact.firm_id is None or contact.email_pattern_recorded:
        return False
    if dry_run:
        return True

    stats, _ = EmailPatternStats.objects.get_or_create(firm_id=contact.firm_id)
    if delivered:
        stats.delivered = models.F("delivered") + 1
    else:
        stats.bounced = models.F("bounced") + 1
    stats.save(update_fields=["delivered" if delivered else "bounced", "last_updated"])

    contact.email_pattern_recorded = True
    contact.save(update_fields=["email_pattern_recorded"])
    return True


def _user_aware(user, value: str | None):
    """`value` (an ISO string) as an aware datetime on the USER's clock, or
    None if absent/unparseable.

    A naive timestamp means "this clock time, on the user's own clock". It
    must be anchored to the USER's zone, not the process default: this runs
    inside a management command, which never passes through
    TimezoneMiddleware, so get_current_timezone() here is the server's UTC —
    and a "10am" chat stored as 10:00 UTC reads as 6pm the moment the user
    sets Asia/Hong_Kong in Settings. Same fallback discipline as the
    middleware: a blank or unloadable zone name falls back to the project
    default rather than crashing the sync.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = parse_datetime(raw)
    if parsed is None or timezone.is_aware(parsed):
        return parsed
    tzname = (getattr(user, "timezone", "") or "").strip()
    try:
        zone = ZoneInfo(tzname) if tzname else timezone.get_current_timezone()
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.get_current_timezone()
    return timezone.make_aware(parsed, zone)


def _upsert_scheduled_chat(user, contact: Contact, finding: dict) -> bool:
    """Put a scheduled chat on the calendar when the finding carries a time.

    `capture.extractors` has always pulled a real datetime off calendar
    invites (DTSTART) as `chat_scheduled_at`, and it had nowhere to go — the
    Today page's "Coming up" says so out loud, reporting when a chat was SET
    UP because "we do not store a chat datetime anywhere". This is that
    destination.

    THE KEY IS THE INVITE, NOT THE THREAD. This was keyed on (user,
    thread_id) alone, which holds for the twice-daily sync re-reading the
    same thread but breaks on the case it was written for. A real one from
    the founder's mailbox: Lily's "Accepted: Jimmy <> Lily Coffee Chat"
    arrived on thread `1a0346eb...`, and her counter-proposal, "New Time
    Proposed: Jimmy (USC) <> Lily Coffee Chat", arrived on a DIFFERENT
    thread, `1a038f4c...` — Google starts a fresh one for it. Two threads
    meant two rows, so the calendar showed the same chat twice and one of
    them at the time she had just moved away from. The .ics UID is held
    constant across REQUEST / REPLY / COUNTER / CANCEL for one event (RFC
    5545), so it is looked up FIRST and the thread is only the fallback for
    an invite whose UID could not be read.

    Two consequences of a UID hit, both deliberate:

    * Any OTHER captured chat sitting on the incoming thread is deleted.
      That row is the duplicate this reconciliation exists to remove — the
      second copy an earlier run already made — and leaving it would also
      collide with the (user, thread_id) constraint the moment the surviving
      row moves onto that thread. Only `source=capture` / `kind=chat` rows
      qualify: a hand-added event carries a blank thread_id and can never
      match.
    * The time only moves FORWARD in invite order. Findings are not sorted
      outside the backfill, so an older "Accepted:" applied after a newer
      "New Time Proposed:" would otherwise drag the chat back to the stale
      time — the same wrong answer as before, just with one row instead of
      two. `invite_sent_at` records which invite is currently speaking.

    A finding with no time is not an error — most are — it simply makes no
    event.
    """
    when = _user_aware(user, finding.get("chat_scheduled_at"))
    thread_id = (finding.get("thread_id") or "").strip()[:128]
    uid = (finding.get("ics_uid") or "").strip()[:255]
    if when is None or not (thread_id or uid):
        return False
    sent_at = _user_aware(user, finding.get("occurred_at"))

    event = None
    if uid:
        event = CalendarEvent.all_objects.filter(user=user, ics_uid=uid).first()
    if event is None and thread_id:
        event = CalendarEvent.all_objects.filter(user=user, thread_id=thread_id).first()

    label = contact.name or "Coffee chat"
    if event is None:
        CalendarEvent.all_objects.create(
            user=user, thread_id=thread_id, ics_uid=uid,
            title=f"Chat with {label}", starts_at=when, invite_sent_at=sent_at,
            all_day=False,
            kind=CalendarEvent.KIND_CHAT, source=CalendarEvent.SOURCE_CAPTURE,
            contact=contact,
        )
        return True

    # A RETIRED CHAT IS INERT TO ANY INVITE THAT CANNOT OUTDATE ITS
    # CANCELLATION, and this is what stops the retirement being undone by
    # accident. The sync re-reads a ROLLING WINDOW, so the original REQUEST
    # is still in the mailbox after the CANCEL lands; without this guard the
    # very next run would walk over it, rewrite the title back to "Chat with
    # Lily" and hand the student a cancelled meeting again — the resurrection
    # loop `_retire_cancelled_chat` rejected outright deletion to avoid,
    # arriving instead through the update path.
    #
    # Same rule as the recency guard below, applied to a different statement:
    # a cancellation is DATED, so only an invite that can prove it was sent
    # at or after it may speak over it. The re-read original never can — its
    # own send time predates the cancellation by definition — while a genuine
    # re-invite does, and revives the chat by clearing `cancelled_at` and
    # dropping the marker off the title.
    if event.cancelled_at is not None:
        if sent_at is None or sent_at < event.cancelled_at:
            return False
        event.cancelled_at = None

    if thread_id and event.thread_id != thread_id:
        CalendarEvent.all_objects.filter(
            user=user, thread_id=thread_id,
            source=CalendarEvent.SOURCE_CAPTURE, kind=CalendarEvent.KIND_CHAT,
        ).exclude(pk=event.pk).delete()

    event.title = f"Chat with {label}"
    event.all_day = False
    event.kind = CalendarEvent.KIND_CHAT
    event.source = CalendarEvent.SOURCE_CAPTURE
    event.contact = contact
    # Adopt the UID even when the row was found by thread: that is how a chat
    # first captured before any of this existed becomes reschedulable.
    event.ics_uid = uid or event.ics_uid
    # AN INVITE WE CANNOT DATE MAY NOT OVERWRITE A TIME A DATED INVITE SET.
    # `_message_occurred_at` returns None for an absent or garbled
    # `internalDate`, and the guard used to spell that case `sent_at is None ->
    # take the branch` — i.e. "if we can't date it, trust it most", which is
    # the exact reverse of what no timestamp means. An undateable invite
    # carries no evidence of being the NEWER one, so it walked straight in
    # through the guard's own null branch and dragged the chat back to its
    # DTSTART: the failure `test_the_older_invite_cannot_drag_the_chat_back`
    # exists to prevent, arriving by the one door that test left open.
    #
    # The rule, stated positively: the incoming invite speaks unless a DATED
    # invite already established this row's time and this one cannot say it is
    # newer. So all three of the other cases still move the row —
    #
    #   * dated over dated, newer wins (unchanged);
    #   * dated over a row with no recorded provenance — which is EVERY row
    #     written before `invite_sent_at` existed, so this is the branch that
    #     carries the live table, not a corner;
    #   * undateable over undateable, because neither side has evidence and
    #     refusing would freeze those same rows at whatever an old sync wrote.
    #
    # Only "undateable over dated" is refused. The CREATE path above is
    # deliberately not gated on this: a row that does not exist yet has no
    # time to protect, and some evidence beats none.
    if event.invite_sent_at is None or (
        sent_at is not None and sent_at >= event.invite_sent_at
    ):
        event.starts_at = when
        event.thread_id = thread_id or event.thread_id
        event.invite_sent_at = sent_at or event.invite_sent_at
    event.save(update_fields=[
        "title", "all_day", "kind", "source", "contact", "ics_uid",
        "starts_at", "thread_id", "invite_sent_at", "cancelled_at",
    ])
    return True


# The marker a retired chat wears on every surface that keeps it. A prefix,
# not a tense change: "Cancelled: Chat with Lily" against "Chat with Lily" is
# unmissable on a lock screen in a way that editing a verb is not.
_CANCELLED_PREFIX = "Cancelled: "


def _retire_cancelled_chat(user, finding: dict) -> bool:
    """Mark the chat a `METHOD:CANCEL` invite calls off, so it stops reading
    as scheduled. True when a row was actually retired.

    No `contact` argument, unlike its sibling above: the invite UID is the
    event's identity and the row is found by it alone. Narrowing to the
    contact the finding matched would only add a way to MISS — the original
    invite may well have been matched to a different card, and a cancellation
    that silently declines to act is the failure this function exists to end.

    THE HALF THAT WAS MISSING. `_extract_ics_schedule` learned to report no
    time from a cancellation, which stopped one arriving and re-asserting the
    meeting at its old DTSTART. It could not do anything about the row the
    ORIGINAL invite had already written: that row sat on the month grid, rode
    out to the subscribed .ics feed and onto a phone, and produced a prep card
    on Today telling a student to get ready for a chat nobody was attending.
    A cancellation that stops adding to a lie while leaving the lie standing
    is half a fix.

    RETIRED, NOT DELETED, and the third option is worse than both:

    * DELETE. It destroys the record of a chat that genuinely was on the
      books — but the disqualifying problem is mechanical, not sentimental.
      The sync re-reads a ROLLING WINDOW of the mailbox, so the original
      invite is still sitting in it; delete the row and the very next run
      walks over that invite and mints it again. The chat would resurrect
      itself twice a day, forever, and each resurrection looks exactly like a
      fresh booking. A delete here is not a fix, it is a loop.
    * RETITLE ONLY, the way the .ics feed retitles a posting the firm pulled
      (`crm.calendar_views._ics_body`). Right for that layer and not enough
      for this one: the loudest surface a cancelled chat reaches is Today's
      "Chats today" lane, and that card renders the CONTACT and the CLOCK —
      it never prints the event title at all (see `_cockpit.html`). A prefix
      nobody's most urgent surface displays is a fix you cannot see.

    So both, split by what the surface is FOR. `crm.today._schedule` drops
    cancelled rows outright, which empties them out of the prep lane and the
    day track: those answer "what is happening", and this is not happening.
    The month grid and the .ics feed keep the row, struck and retitled: those
    are a RECORD, and the feed especially — an entry that silently disappears
    off someone's phone is the failure mode the pulled-posting comment one
    layer down already argued through at length.

    ON PROPOSE-THEN-CONFIRM. Mail read on a student's behalf may propose, and
    only their own tap changes their record — so writing this without a tap
    needs an argument, and the argument is symmetry. Coverage created this row
    with no tap either: `_upsert_scheduled_chat` writes on the REQUEST because
    an .ics is a machine-readable statement from the organiser rather than
    something inferred out of prose. The CANCEL is the same statement from the
    same organiser in the same standard, retracting what it asserted. If the
    assertion needed no tap, the retraction of that same assertion cannot need
    one; requiring a tap to un-say something we said unasked is not caution,
    it is an asymmetry that can only ever leave the student holding the FALSE
    state. The posture guards against Coverage INFERRING things into someone's
    record, and nothing here is inferred.

    Two limits keep that argument honest. Only `source=capture` rows are
    touched — a hand-typed event is the student's own record and an inbound
    .ics has no standing over it, whatever UID it carries. And nothing is
    destroyed: the time, the contact and the identity all survive, so a
    re-invite on the same UID lands back in `_upsert_scheduled_chat` and
    revives it.

    IDEMPOTENT BY STRUCTURE. An already-cancelled row is left exactly alone
    rather than re-stamped, so the same cancellation on the second, tenth and
    hundredth sync is indistinguishable from the first — including the
    timestamp, which a `now()` re-stamp would walk forward on every run.
    """
    uid = (finding.get("ics_uid") or "").strip()[:255]
    thread_id = (finding.get("thread_id") or "").strip()[:128]
    if not (uid or thread_id):
        return False

    # Same lookup order as `_upsert_scheduled_chat`: the UID is the identity
    # that survives a reschedule, the thread is the fallback for an invite
    # whose UID would not parse. A cancellation typically arrives on a NEW
    # Gmail thread, so in practice the UID is the only key that finds
    # anything — which is exactly why the extractor now keeps it.
    rows = CalendarEvent.all_objects.filter(
        user=user,
        source=CalendarEvent.SOURCE_CAPTURE,
        kind=CalendarEvent.KIND_CHAT,
        cancelled_at__isnull=True,
    )
    event = rows.filter(ics_uid=uid).first() if uid else None
    if event is None and thread_id:
        event = rows.filter(thread_id=thread_id).first()
    if event is None:
        return False

    # The cancelling invite's own send time when it has one. Falling back to
    # "now" only when the message could not be dated keeps the column honest
    # about WHEN this was called off without ever leaving it null, which is
    # the value that means "not cancelled".
    event.cancelled_at = _user_aware(user, finding.get("occurred_at")) or timezone.now()
    if not event.title.startswith(_CANCELLED_PREFIX):
        # At the FRONT, and in the title rather than the description: the
        # .ics SUMMARY is the whole of what a phone notification shows. Same
        # argument, same wording shape, as "Closed:" on a pulled posting.
        event.title = f"{_CANCELLED_PREFIX}{event.title}"
    event.save(update_fields=["cancelled_at", "title"])
    return True


def _touch_kind_for(finding: dict) -> str | None:
    """The ladder stage a finding represents, or None if it shows no progress.
    Order matters: a completed chat outranks a mere reply on the same thread.

    `bulk` is checked FIRST and wins outright over `replied`/`chat_status`.
    A finding that says "this is a mass/automated message" is making a claim
    about what the message IS, not about how far along the relationship is,
    and the whole point of the flag is that no amount of enthusiasm in a
    blast should climb the ladder. The resulting kind is off the ladder
    entirely (see THREAD_STAGE_RANK) and its TOUCH_TRANSITIONS entry is
    `(None, None)`, so it moves neither warmth nor thread_state.
    """
    if finding.get("bulk"):
        return BULK_RECEIVED_KIND
    chat_status = finding.get("chat_status", "none")
    if chat_status == "completed":
        return "chat"
    if chat_status == "scheduled":
        return "chat_scheduled"
    if finding.get("replied"):
        return "reply_received"
    return None


def _bulk_already_logged(user, contact: Contact, thread_id: str, *, reference=None) -> bool:
    """Dedup for `bulk_received`, which the thread ladder cannot do for it.

    `thread_stage_rank` only knows the three ladder stages, so a bulk touch
    always ranks 0 there — which is exactly right for "a blast must never
    block a later genuine reply on the same thread", and exactly useless as
    a same-message guard. With a thread id, one bulk touch per thread is the
    rule (a recurring newsletter reuses its Gmail thread, and re-logging it
    every run would reset `last_touch` daily). Without one, fall back to the
    same seven-day same-kind window every other unthreaded finding uses.
    """
    if thread_id:
        return (
            Touch.objects.for_user(user)
            .filter(
                contact=contact,
                kind=BULK_RECEIVED_KIND,
                note__contains=f"[gmail:{thread_id}]",
            )
            .exists()
        )
    return _logged_recently(user, contact, BULK_RECEIVED_KIND, reference=reference)


# --------------------------------------------------------------------------- #
# The entry point
# --------------------------------------------------------------------------- #

def apply_findings(user, findings: list[dict], *, dry_run: bool = False) -> SyncResult:
    """Apply a batch of Gmail findings for one user. Safe to run daily.

    Ordering follows the original exactly, because each step guards the next:
    bounce check first (an address that does not exist has nothing to log),
    then email backfill, then outreach, then at most one ladder touch.

    Two things this function deliberately no longer does, both of which were
    automated decisions about a contact's LIFECYCLE rather than about the
    evidence in front of it: it never archives a contact (a bounce clears the
    address instead — archiving is a user action with a UI and an undo), and
    it never replaces a populated email (a differing address is noted, not
    substituted). See the two blocks below.

    ``dry_run`` reports what would happen and writes nothing. It is a flag on
    THIS function rather than a caller-side `transaction.atomic()` rollback,
    because a rollback cannot cover the writes: `crm.services.log_touch`
    deliberately opens its own psycopg connection and commits there (see that
    module's docstring), so it is invisible to Django's transaction management
    and would survive the unwind. Every matching, ratchet and dedup decision
    still runs on the one shared code path — only the three write sites are
    guarded, so the report cannot drift from the real behaviour.

    Note the resulting blind spot, which is inherent rather than an oversight:
    a dry run does not persist its own touches, so two findings that climb the
    same thread within one batch will both report as logging. A real run
    ratchets the second one away. Batches are per-thread-per-day in practice,
    so this stays theoretical.
    """
    result = SyncResult(findings=len(findings))
    provider = GmailFindingsProvider()
    # Firm-domain map for the discovery hook below — built lazily, at most
    # once per batch, and only if an unmatched finding actually reaches it.
    firm_domains = discovery.FirmDomains()
    # Batch-level facts the discovery hook needs per finding: which
    # addresses bounced in THIS batch, and how many distinct recipients
    # each outbound subject fanned out to. See discovery.BatchContext.
    batch_context = discovery.BatchContext(findings)
    # Same lazy shape, for the application-mail hook below: a batch with no
    # ATS mail in it builds nothing and queries nothing.
    appmail_resolver = appmail.Resolver(user)

    for finding in findings:
        name = (finding.get("name") or "").strip() or "(unnamed)"

        if not finding.get("found"):
            result.skipped_not_found += 1
            continue

        # THE APPLICATION-MAIL HOOK. Deliberately here — before contact
        # matching, and applied to EVERY finding — rather than beside the
        # discovery hook in the unmatched branch below.
        #
        # Discovery's question is "is this sender a person worth tracking",
        # so the unmatched branch is exactly its population. This one's
        # question is "did one of my applications move", which has nothing
        # to do with whether the sender is in the contact book: an ATS
        # sender usually isn't, but a firm that mails confirmations from the
        # same `campus@` address a student HAS saved would be the one case
        # this feature silently skipped. Writes at most one pending
        # `ApplicationEvent` and never touches the pipeline — see
        # capture/appmail.py.
        outcome = appmail.consider_finding(
            user, finding, resolver=appmail_resolver, dry_run=dry_run
        )
        if outcome.result == appmail.PROPOSED:
            result.app_events_proposed += 1
        elif outcome.result == appmail.UNRESOLVED:
            result.app_events_unresolved += 1
        elif outcome.result == appmail.ALREADY_AHEAD:
            result.app_events_already += 1
        if outcome.detail:
            result.details.append(outcome.detail)

        # THE MAIL-FACTS HOOK. Also before contact matching and for EVERY
        # finding, for the same reason the application hook is: whether the
        # sender is in the contact book has nothing to do with whether their
        # mailbox stated a fact — the departed sender is usually NOT a
        # contact (he wrote to them once, from outside Coverage), and the
        # postmaster never is. Reads auto-replies ("no longer with", "please
        # contact X at Y", "back on September 2") and soft bounces (the
        # routing address), acts only with a verbatim quote, and proposes —
        # never creates — people. See capture/mailfacts.py for the whole
        # contract, including where "act" ends and "propose" begins.
        facts = mailfacts.consider_finding(
            user, finding, firm_domains=firm_domains, dry_run=dry_run
        )
        result.mail_facts_applied += facts.applied
        result.mail_facts_surfaced += facts.surfaced
        result.referral_proposals += facts.referrals
        result.details.extend(facts.details)

        try:
            contact = _match_contact(user, finding)
        except AmbiguousContactError as exc:
            result.skipped_ambiguous += 1
            result.details.append(
                f"{name}: {exc.count} contacts share this name — needs manual "
                "review, skipped"
            )
            continue
        if contact is None:
            result.skipped_unmatched += 1
            result.details.append(f"{name}: no matching contact in Coverage — skipped")
            # THE DISCOVERY HOOK. An unmatched finding used to end here, full
            # stop — `_match_contact`'s docstring calls it drift, and for the
            # per-contact daily sync it usually is. But on the live listener
            # and the whole-mailbox paths it is also exactly where "someone
            # not in Coverage wrote to you" surfaces. `consider_finding` runs
            # the deterministic judgment chain and writes at most a PROPOSAL
            # (see capture/discovery.py) — this function still never creates
            # a contact, so its contract stands unchanged.
            outcome = discovery.consider_finding(
                user, finding, firm_domains=firm_domains, dry_run=dry_run,
                batch=batch_context,
            )
            if outcome == discovery.PROPOSED:
                result.proposals_created += 1
                result.details.append(
                    f"{name}: looks like a real contact — proposed for your confirm"
                )
            elif outcome == discovery.UPGRADED:
                result.proposals_upgraded += 1
                result.details.append(
                    f"{name}: they wrote back — pending proposal upgraded"
                )
            elif outcome == discovery.ARCHIVED_MATCH:
                result.proposals_archived_match += 1
                result.details.append(
                    f"{name}: matches an archived contact — left alone "
                    "(unarchive by hand if they should come back)"
                )
            continue

        # When this finding's underlying message actually happened — None
        # for the daily sync's findings (no such field), a real past instant
        # for the backfill command's. See `_finding_occurred_at`.
        finding_time = _finding_occurred_at(finding)

        # Bounced: the address does not exist. Clear the ADDRESS, keep the
        # PERSON.
        #
        # This used to archive the contact, on the reasoning that archiving is
        # a soft delete and "trivially recoverable". It isn't: `archived` had
        # no UI at all — no control to set or unset it, no view that lists
        # archived rows — and both resolvers filter `archived=False`, so a
        # later genuine reply from that person FORKED a second contact instead
        # of resurrecting the first. The bounce, which is a fact about one
        # string in one column, was quietly deciding the fate of the whole
        # relationship record. Clearing the email states exactly what is
        # known, keeps the person on the board where the user can fix the
        # address, and leaves archiving where it belongs: user-driven, from
        # the CRM, and reversible there (see `crm.views.contact_archive`).
        #
        # Idempotent by construction — a second bounce for the same person
        # finds the email already blank and does nothing, so the daily run
        # can't stack notes.
        if finding.get("bounced"):
            # Bank the evidence BEFORE clearing the address: the bounce is a
            # fact about the format the firm uses, and clearing wipes the
            # only copy of what was tried.
            if _record_pattern_evidence(contact, delivered=False, dry_run=dry_run):
                result.pattern_bounced += 1
            if contact.email:
                if not dry_run:
                    _note_bounce(contact, contact.email)
                    contact.email = ""
                    contact.save(update_fields=["email", "notes"])
                result.bounced_cleared += 1
                result.details.append(
                    f"{name}: address bounced — cleared from the contact "
                    "(kept in the notes)"
                )
            continue

        # Email: FILL a blank one, never REPLACE a populated one.
        #
        # The counter has always been called `emails_backfilled` and the
        # docstring has always said "backfill", but the code replaced any
        # differing value. Combined with name-only matching, a finding for
        # "John Smith" from a personal Gmail could overwrite the work address
        # on a contact matched by name alone — destroying the address the
        # student actually needs and detaching the person from their firm. A
        # second address is information; it is not a correction, and only the
        # user knows which one to write to. So the alternate goes in the notes
        # and the primary stands.
        email = (finding.get("email") or "").strip()
        current = (contact.email or "").strip()
        if email and not current:
            # ...unless a departed-fact stands against this exact address
            # (capture.mailfacts): the auto-reply that cleared it arrives in
            # the same batch (and again on every re-scan), name-matches this
            # contact, and would otherwise refill the address the firm's own
            # mail system said is dead.
            if mailfacts.address_is_departed(user, email):
                result.details.append(
                    f"{name}: not refilling {email} — their mailbox said "
                    "they have left the firm"
                )
            else:
                if not dry_run:
                    contact.email = email[:254]
                    contact.save(update_fields=["email"])
                result.emails_backfilled += 1
        elif email and email.lower() != current.lower():
            # Guarded on the note text, not on a flag: this runs daily, and
            # the same finding will resurface every day the thread stays in
            # the search window.
            if email.lower() not in (contact.notes or "").lower():
                if not dry_run:
                    _note_alternate_email(contact, email)
                result.alternate_emails_noted += 1
                result.details.append(
                    f"{name}: found a second address ({email}) — noted, "
                    f"primary ({current}) kept"
                )

        is_bulk = bool(finding.get("bulk"))

        # A reply is the strongest proof an address format works: the person
        # received the mail and answered. `outreach_sent` alone is weaker —
        # it proves the message left, not that it landed — so only a reply or
        # a chat banks a "delivered". Recorded once per contact ever, guarded
        # by `email_pattern_recorded`.
        #
        # A BULK message proves nothing about the format at all: a blast
        # arriving FROM a firm says their list software can reach the
        # student, not that the address the student guessed for that person
        # works. Banking it would let one newsletter raise a whole firm's
        # shared pattern confidence — and `email_pattern_recorded` means the
        # contact then never banks the real evidence later.
        if not is_bulk and (
            finding.get("replied") or finding.get("chat_status") in ("scheduled", "completed")
        ):
            if _record_pattern_evidence(contact, delivered=True, dry_run=dry_run):
                result.pattern_delivered += 1

        # A stated time turns a "chat_scheduled" into a dated calendar entry.
        # Never for a bulk finding: `gmail_live` already blanks `chat_status`
        # on one, and this second guard covers an externally-supplied finding
        # that sets both — a webinar on a mass invitation is not "Chat with
        # <person>" on the student's calendar.
        if not is_bulk and finding.get("chat_status") in ("scheduled", "completed"):
            if not dry_run:
                if _upsert_scheduled_chat(user, contact, finding):
                    result.chats_scheduled += 1
            elif (finding.get("chat_scheduled_at") or "").strip():
                result.chats_scheduled += 1

        # A cancellation is the only finding that reaches the calendar while
        # carrying no time — the extractor refuses to report one from a
        # cancellation, so it can never come through the branch above. Kept
        # off the `chats_scheduled` counter deliberately: retiring a chat is
        # not scheduling one, and folding it in would make the sync's own
        # summary line say the opposite of what happened. Same `is_bulk`
        # guard, for the same reason as above.
        if not is_bulk and finding.get("chat_cancelled") and not dry_run:
            _retire_cancelled_chat(user, finding)

        thread_id = (finding.get("thread_id") or "").strip()
        marker = f"[gmail:{thread_id}] " if thread_id else ""
        evidence = (finding.get("evidence") or "").strip()

        # --- outreach + follow-up: per-contact, off the ladder ------------ #
        if finding.get("outreach_sent"):
            already = (
                Touch.objects.for_user(user)
                .filter(contact=contact, kind__in=_OUTREACH_KINDS)
                .exists()
            )
            if already:
                # A first note is on record, so this send is either the
                # same event re-seen or the follow-up nothing used to log
                # (`_follow_up_action` is the whole judgement, including
                # why a merge wave and an undated re-emission never count).
                action = _follow_up_action(
                    user, contact, finding, thread_id=thread_id,
                    finding_time=finding_time, batch=batch_context,
                )
                if action == FOLLOW_UP_LOG:
                    if not dry_run:
                        event = provider.build_event(
                            finding, user, "follow_up", occurred_at=finding_time
                        )
                        logged = crm_services.log_touch(
                            user.id, contact.id, "follow_up", TOUCH_CHANNEL,
                            note=f"{marker}{evidence}".strip() or None,
                            now=finding_time,
                            source="capture",
                        )
                        _stamp_subject(user, logged, finding)
                        _record(user, contact, event)
                    result.follow_ups_logged += 1
                    result.details.append(
                        f"{name}: follow-up logged (second note, still no reply)"
                    )
                elif action == FOLLOW_UP_MERGE:
                    result.skipped_already_logged += 1
                    result.details.append(
                        f"{name}: outbound is a mail-merge wave — not logged "
                        "as a follow-up"
                    )
                else:
                    result.skipped_already_logged += 1
            else:
                if not dry_run:
                    event = provider.build_event(
                        finding, user, "outreach", occurred_at=finding_time
                    )
                    logged = crm_services.log_touch(
                        user.id, contact.id, "outreach", TOUCH_CHANNEL,
                        note=f"{marker}{evidence}".strip() or None,
                        now=finding_time,
                        source="capture",
                    )
                    _stamp_subject(user, logged, finding)
                    _record(user, contact, event)
                result.outreach_logged += 1
                result.details.append(f"{name}: outreach logged (a sent email was never recorded)")

        # --- the ladder: at most one stage per finding --------------------- #
        kind = _touch_kind_for(finding)
        if kind is None:
            continue

        if kind == BULK_RECEIVED_KIND:
            # Off the ladder — see `_bulk_already_logged` for why the thread
            # ratchet cannot dedup this kind, and why that is deliberate
            # rather than a gap.
            if _bulk_already_logged(user, contact, thread_id, reference=finding_time):
                result.skipped_already_logged += 1
                result.details.append(
                    f"{name}: bulk/automated email already recorded for this "
                    "thread — skipped"
                )
                continue
        elif thread_id:
            staged = thread_stage_rank(user, contact, thread_id)
            if THREAD_STAGE_RANK[kind] <= staged:
                result.skipped_already_logged += 1
                prior = _STAGE_NAME.get(staged, f"stage {staged}")
                reason = ("same stage, prior run" if THREAD_STAGE_RANK[kind] == staged
                          else "a later stage; refusing to regress")
                result.details.append(
                    f"{name}: {kind} not logged — this thread is already at '{prior}' ({reason})"
                )
                continue
        elif _logged_recently(user, contact, kind, reference=finding_time):
            result.skipped_already_logged += 1
            result.details.append(
                f"{name}: {kind} logged within the last {NO_THREAD_DEDUP_DAYS} days and this "
                f"finding has no thread_id to distinguish it — skipped"
            )
            continue

        # The note is the ONLY place a user ever sees why Coverage called a
        # message bulk, so the reasons ride along with the evidence rather
        # than staying in a log line nobody reads. Still no email body: the
        # reasons are header names, and `evidence` is the subject at most
        # (§10's "no email bodies in logs/notes").
        note_text = f"{marker}{evidence}".strip()
        if kind == BULK_RECEIVED_KIND and finding.get("bulk_reasons"):
            note_text = f"{note_text} [{finding['bulk_reasons']}]".strip()

        updates = {}
        if not dry_run:
            event = provider.build_event(finding, user, kind, occurred_at=finding_time)
            updates = crm_services.log_touch(
                user.id, contact.id, kind, TOUCH_CHANNEL,
                note=note_text or None,
                now=finding_time,
                source="capture",
            )
            _stamp_subject(user, updates, finding)
            _record(user, contact, event, warmth_changed=bool(updates))
        if kind == BULK_RECEIVED_KIND:
            result.bulk_logged += 1
            result.details.append(
                f"{name}: recorded as a bulk/automated email, not a reply "
                f"({finding.get('bulk_reasons') or 'flagged by the caller'}) — "
                "warmth and status unchanged"
            )
            continue
        result.touches_logged += 1
        result.details.append(
            f"{name}: {kind} logged" + (f" -> {updates}" if updates else " (warmth already at/above this stage)")
        )

    # Regroup this user's outbound mail into bulk sends now that the batch has
    # landed. Here rather than on a page render because it walks every outbound
    # touch, and here rather than in a nightly cron because a merge sent this
    # morning should be a question in Settings this afternoon, not tomorrow.
    #
    # Idempotent and additive by construction (see `crm.campaigns.detect`): it
    # never changes an answer the user has given and never removes anybody by
    # itself. Skipped on a dry run for the obvious reason, and never allowed to
    # fail the sync — the findings above are already committed, and losing a
    # whole night's capture because a grouping pass raised would be a far worse
    # trade than one late campaign card.
    if not dry_run:
        try:
            crm_campaigns.detect(user)
        except Exception as exc:  # noqa: BLE001 — see the comment above.
            result.details.append(f"campaign detection skipped: {exc}")

    return result


def _stamp_subject(user, touch_result, finding: dict) -> None:
    """Copy the finding's Subject header onto the touch row `log_touch` just
    created, when the provider supplied one.

    A SECOND STATEMENT rather than a wider INSERT, and deliberately so. The
    `touches` INSERT lives in `coverage_domain.pipeline.apply_touch`'s raw SQL,
    which is the pure package's contract with the ported ratchet; widening it
    to carry a column only the web app reads would push a view concern into the
    engine for no gain. `apply_touch` already returns the new row's id, so the
    capture layer can finish the row itself.

    WHY THE SUBJECT IS WORTH STORING AT ALL: a mail merge's one defining fact
    is that hundreds of threads share one subject line, and until now
    `gmail_live._classify_message` read that header and dropped it. See
    `crm/campaigns.py`.

    Silent when there is no subject, no touch id, or the provider is one that
    never sees headers (the manual findings sync). Never fatal — a missing
    subject costs one campaign the strong grouping key and falls back to the
    evidence note, which is the pre-existing behaviour, not a regression.
    """
    subject = (finding.get("subject") or "").strip()
    touch_id = getattr(touch_result, "touch_id", None)
    if not subject or not touch_id:
        return
    Touch.objects.for_user(user).filter(id=touch_id).update(subject=subject[:255])


def _record(user, contact: Contact, event: InteractionEvent, *, warmth_changed: bool = False) -> None:
    """Instrument through the same event name the BCC path uses, tagged with
    this provider — so §8's capture-mix metric (email vs manual) can tell a
    Gmail-sourced touch from a BCC'd one without a schema change."""
    record_event(
        "touch_logged",
        user=user,
        source="capture",
        provider=event.provider,
        kind=event.touch_kind,
        contact_id=contact.id,
        warmth_changed=warmth_changed,
    )
