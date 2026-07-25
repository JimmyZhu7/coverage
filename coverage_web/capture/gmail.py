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
from datetime import timedelta

from django.utils import timezone

from analytics.events import record_event
from capture.providers import CaptureProvider, InteractionEvent
from crm import services as crm_services
from crm.models import Contact, Touch

EXTRACTION_VERSION = "gmail-findings-1"
TOUCH_CHANNEL = "email"  # every finding here comes from a Gmail thread

# The thread ladder. `outreach` is deliberately absent — it is not a stage.
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
    outreach_logged: int = 0
    touches_logged: int = 0
    archived_bounced: int = 0
    skipped_not_found: int = 0
    skipped_already_logged: int = 0
    skipped_unmatched: int = 0
    details: list[str] = field(default_factory=list)

    def as_stats(self) -> dict:
        return {
            "findings": self.findings,
            "emails_backfilled": self.emails_backfilled,
            "outreach_logged": self.outreach_logged,
            "touches_logged": self.touches_logged,
            "archived_bounced": self.archived_bounced,
            "skipped_not_found": self.skipped_not_found,
            "skipped_already_logged": self.skipped_already_logged,
            "skipped_unmatched": self.skipped_unmatched,
        }


class GmailFindingsProvider(CaptureProvider):
    """Turns one Gmail finding into an :class:`InteractionEvent`.

    Finding shape (unchanged from the daily sync's existing output)::

        {"contact_id": int|None, "name": str, "email": str|None, "found": bool,
         "bounced": bool, "outreach_sent": bool, "replied": bool,
         "chat_status": "none"|"scheduled"|"completed",
         "evidence": str|None, "thread_id": str|None}

    ``contact_id`` is a *campaign.db* id and is not used for matching here —
    Coverage resolves by email, then name, so the two systems' id spaces never
    have to be kept in step.
    """

    name = "gmail"

    def build_event(self, finding: dict, user, touch_kind: str) -> InteractionEvent:
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
            occurred_at=timezone.now(),
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
    """
    scoped = Contact.objects.for_user(user).filter(archived=False)
    email = (finding.get("email") or "").strip()
    if email:
        match = scoped.filter(email__iexact=email).first()
        if match:
            return match
    name = (finding.get("name") or "").strip()
    if name:
        from capture import extractors

        norm = extractors.normalize_name(name)
        for contact in scoped:
            if contact.name and extractors.normalize_name(contact.name) == norm:
                return contact
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


def _logged_recently(user, contact: Contact, kind: str) -> bool:
    """Fallback dedup for findings carrying no thread_id (see module docstring).
    Unlike the original's local-naive-vs-UTC hazard, both sides here are aware
    datetimes, so the window is exactly seven days."""
    cutoff = timezone.now() - timedelta(days=NO_THREAD_DEDUP_DAYS)
    return (
        Touch.objects.for_user(user)
        .filter(contact=contact, kind=kind, ts__gte=cutoff)
        .exists()
    )


def _touch_kind_for(finding: dict) -> str | None:
    """The ladder stage a finding represents, or None if it shows no progress.
    Order matters: a completed chat outranks a mere reply on the same thread."""
    chat_status = finding.get("chat_status", "none")
    if chat_status == "completed":
        return "chat"
    if chat_status == "scheduled":
        return "chat_scheduled"
    if finding.get("replied"):
        return "reply_received"
    return None


# --------------------------------------------------------------------------- #
# The entry point
# --------------------------------------------------------------------------- #

def apply_findings(user, findings: list[dict], *, dry_run: bool = False) -> SyncResult:
    """Apply a batch of Gmail findings for one user. Safe to run daily.

    Ordering follows the original exactly, because each step guards the next:
    bounce check first (an address that does not exist has nothing to log),
    then email backfill, then outreach, then at most one ladder touch.

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

    for finding in findings:
        name = (finding.get("name") or "").strip() or "(unnamed)"

        if not finding.get("found"):
            result.skipped_not_found += 1
            continue

        contact = _match_contact(user, finding)
        if contact is None:
            result.skipped_unmatched += 1
            result.details.append(f"{name}: no matching contact in Coverage — skipped")
            continue

        # Bounced: the address does not exist. Archive (the soft-delete every
        # other write path uses) rather than delete — a bounce heuristic can
        # misfire on a reply that merely quotes failure language, and an
        # archived contact is trivially recoverable.
        if finding.get("bounced"):
            if not contact.archived:
                if not dry_run:
                    contact.archived = True
                    contact.save(update_fields=["archived"])
                result.archived_bounced += 1
                result.details.append(f"{name}: address bounced — archived")
            continue

        email = (finding.get("email") or "").strip()
        if email and email.lower() != (contact.email or "").lower():
            if not dry_run:
                contact.email = email[:254]
                contact.save(update_fields=["email"])
            result.emails_backfilled += 1

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
                    event = provider.build_event(finding, user, "outreach")
                    crm_services.log_touch(
                        user.id, contact.id, "outreach", TOUCH_CHANNEL,
                        note=f"{marker}{evidence}".strip() or None,
                    )
                    _record(user, contact, event)
                result.outreach_logged += 1
                result.details.append(f"{name}: outreach logged (a sent email was never recorded)")

        # --- the ladder: at most one stage per finding --------------------- #
        kind = _touch_kind_for(finding)
        if kind is None:
            continue

        if thread_id:
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
        elif _logged_recently(user, contact, kind):
            result.skipped_already_logged += 1
            result.details.append(
                f"{name}: {kind} logged within the last {NO_THREAD_DEDUP_DAYS} days and this "
                f"finding has no thread_id to distinguish it — skipped"
            )
            continue

        updates = {}
        if not dry_run:
            event = provider.build_event(finding, user, kind)
            updates = crm_services.log_touch(
                user.id, contact.id, kind, TOUCH_CHANNEL,
                note=f"{marker}{evidence}".strip() or None,
            )
            _record(user, contact, event, warmth_changed=bool(updates))
        result.touches_logged += 1
        result.details.append(
            f"{name}: {kind} logged" + (f" -> {updates}" if updates else " (warmth already at/above this stage)")
        )

    return result


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
