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
outreach marker never blocks a later reply on the same thread.

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
            # inbound; a never-answered sent note is outbound.
            direction="inbound" if touch_kind != "outreach" else "outbound",
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

    Raises:
        AmbiguousContactError: more than one contact shares the normalized
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
    name = (finding.get("name") or "").strip()
    if name:
        from capture.providers import normalize_name

        norm = normalize_name(name)
        matches = [
            contact for contact in scoped
            if contact.name and normalize_name(contact.name) == norm
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


def _upsert_scheduled_chat(user, contact: Contact, finding: dict) -> bool:
    """Put a scheduled chat on the calendar when the finding carries a time.

    `capture.extractors` has always pulled a real datetime off calendar
    invites (DTSTART) as `chat_scheduled_at`, and it had nowhere to go — the
    Today page's "Coming up" says so out loud, reporting when a chat was SET
    UP because "we do not store a chat datetime anywhere". This is that
    destination.

    Keyed on (user, thread_id) so the twice-daily sync updates the one row
    rather than stacking a duplicate every run, and so a rescheduled invite
    on the same thread MOVES the event instead of leaving two. A finding
    with no time is not an error — most are — it simply makes no event.
    """
    raw = (finding.get("chat_scheduled_at") or "").strip()
    thread_id = (finding.get("thread_id") or "").strip()
    if not raw or not thread_id:
        return False
    when = parse_datetime(raw)
    if when is None:
        return False
    if timezone.is_naive(when):
        # A naive timestamp means "this clock time, on the user's own clock".
        # It must be anchored to the USER's zone, not the process default:
        # this runs inside a management command, which never passes through
        # TimezoneMiddleware, so get_current_timezone() here is the server's
        # UTC — and a "10am" chat stored as 10:00 UTC reads as 6pm the moment
        # the user sets Asia/Hong_Kong in Settings. Same fallback discipline
        # as the middleware: a blank or unloadable zone name falls back to
        # the project default rather than crashing the sync.
        tzname = (getattr(user, "timezone", "") or "").strip()
        try:
            zone = ZoneInfo(tzname) if tzname else timezone.get_current_timezone()
        except (ZoneInfoNotFoundError, ValueError):
            zone = timezone.get_current_timezone()
        when = timezone.make_aware(when, zone)

    label = contact.name or "Coffee chat"
    CalendarEvent.all_objects.update_or_create(
        user=user, thread_id=thread_id[:128],
        defaults={
            "title": f"Chat with {label}",
            "starts_at": when,
            "all_day": False,
            "kind": CalendarEvent.KIND_CHAT,
            "source": CalendarEvent.SOURCE_CAPTURE,
            "contact": contact,
        },
    )
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

        thread_id = (finding.get("thread_id") or "").strip()
        marker = f"[gmail:{thread_id}] " if thread_id else ""
        evidence = (finding.get("evidence") or "").strip()

        # --- outreach: per-contact, off the ladder ------------------------ #
        if finding.get("outreach_sent"):
            already = (
                Touch.objects.for_user(user)
                .filter(contact=contact, kind__in=_OUTREACH_KINDS)
                .exists()
            )
            if already:
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
