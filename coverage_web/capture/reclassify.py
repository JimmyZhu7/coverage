"""Re-judging `reply_received` touches that are already in the database.

WHY THIS IS A DIFFERENT PROBLEM FROM `capture.inbound`
------------------------------------------------------
`capture.inbound` decides what a message is from its own headers. It is
exact, and it is only available at capture time. A `reply_received` row
that is already in the database has no headers left: `Touch` deliberately
stores no email body and no header set (§10, "no email bodies in
logs/notes"), and Gmail Live is not even connected on the account this was
written for — those rows came from an agent-run mailbox sync that emitted
finding dicts, not messages.

So this module cannot re-apply the header test. It works from what the
database actually kept, which is weaker, and it is built to be wrong in
one direction only.

THE ONE DIRECTION
-----------------
Wrongly demoting a real banker's reply to "bulk" silences a real
relationship; wrongly leaving a blast as a reply produces visible noise.
The first is much worse. So a touch is demoted only when EVERY one of the
four conditions below holds, and everything else that merely looks
suspicious is REPORTED and left alone:

1. **The touch was written by the capture pipeline** (`source="capture"`).
   An `import` or `manual` row was curated by a person; this module has no
   standing to overrule one.
2. **The contact has no outbound touch of any kind, ever.** Not "no
   outbound on this thread" — thread-level was tried first and is wrong:
   Nick Tehle's genuine reply (J.P. Morgan, contact 481 on the founder's
   account) sits on thread `19f2e6ef0c479dc0` with no outreach touch
   carrying that thread's marker, because the outreach was logged
   separately by a discovery scan with no thread id. Contact-level asks
   the question that actually matters — "did he ever write to this
   person?" — and an inbound message from someone you have never written
   to cannot be a reply to you.
3. **The contact carries no `manual_override` touch.** A human correction
   is authoritative over any automated judgement, full stop. This is
   deliberately broad: it also protects contacts whose only override is a
   Park, which is not a correction of the reply at all. That is the
   conservative direction, and those contacts are still listed in the
   report so the owner can see what was held back.
4. **The evidence text is either absent or reads as a mass mailing.**
   "Absent" means the discovery scan's own placeholder — a row asserting
   "they replied" while recording nothing whatsoever about the message.

Condition 2 alone would have demoted 22 contacts on the founder's live
data, including Shelby Dibs, Brooke Baker, Bridget Doyle and Dan Frankel —
four campus recruiters who genuinely wrote back, whose outreach simply
predates Coverage and was never logged. Conditions 1 and 4 are what
separate those from Caroline Baenen. That measurement is the reason this
module has four conditions instead of one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from coverage_domain.pipeline import (
    BULK_RECEIVED_KIND,
    MANUAL_OVERRIDE_KIND,
    TOUCH_TRANSITIONS,
    WARMTH_RANK,
)
from crm.models import Contact, Touch

# Kinds that mean "I sent something to this person". Mirrors the cadence
# engine's own notion of outbound plus the two courtesy kinds, because the
# question here is only "has any message ever gone the other way".
OUTBOUND_KINDS = ("outreach", "follow_up", "thank_you", "reping", "maintain")

# The ladder kinds an inbound message can currently occupy.
LADDER_KINDS = ("reply_received", "chat_scheduled", "chat")

# `capture_discover`'s placeholder note. A row carrying ONLY this is
# asserting a reply while recording nothing at all about the message —
# there is no evidence here to overrule, because there is no evidence.
DISCOVERY_PLACEHOLDER = "discovered by mailbox scan"

# Mass-mailing register. Every phrase here is addressed to a COHORT rather
# than to a person: an invitation extended to whoever is on the list. These
# are matched against `Touch.note`, which for a capture row is the
# finding's short evidence summary (a subject line or a snippet), never a
# body — so this is a small, blunt vocabulary on purpose, and it is only
# ever consulted after conditions 1-3 have already passed.
_BULK_PHRASES = (
    r"as a participant in",
    r"we'?d love to invite you",
    r"we are excited to invite",
    r"you'?re invited",
    r"you are invited",
    r"register (?:now|here|today)",
    r"rsvp (?:here|now|by)",
    r"unsubscribe",
    r"view (?:this|it) in your browser",
    r"do not reply to this",
    r"this is an automated",
    r"out of (?:the )?office",
    r"we received your application",
    r"your application (?:has been|was) received",
    r"newsletter",
    r"webinar",
)
_BULK_RE = re.compile("|".join(_BULK_PHRASES), re.IGNORECASE)

_GMAIL_MARKER_RE = re.compile(r"\[gmail:([0-9a-zA-Z]+)\]")


def looks_like_bulk_evidence(note: str | None) -> tuple[bool, str]:
    """(is_bulk, why) for the text a capture touch kept about a message.

    Two separate cases, reported separately because they are different
    kinds of certainty:

      - no evidence at all (the discovery placeholder, or an empty note):
        there is nothing here that says a person wrote to you.
      - evidence written in mass-mailing register.
    """
    text = (note or "").strip()
    stripped = _GMAIL_MARKER_RE.sub("", text).strip()
    if not stripped or stripped.lower() == DISCOVERY_PLACEHOLDER:
        return True, "no evidence recorded beyond the scan's own placeholder"
    match = _BULK_RE.search(stripped)
    if match:
        return True, f"mass-mailing wording ({match.group(0)!r})"
    return False, ""


@dataclass
class TouchVerdict:
    touch: Touch
    contact: Contact
    demote: bool
    reason: str
    # True when the touch looks bulk but something held the demotion back
    # (a human correction, a curated source, an outbound history). These
    # are the rows the report asks the owner to eyeball.
    flagged: bool = False


@dataclass
class ContactPlan:
    contact: Contact
    demotions: list[TouchVerdict] = field(default_factory=list)
    # (warmth, thread_state) the contact should carry once the demoted
    # touches stop counting, or None when nothing about the contact
    # changes.
    new_warmth: str | None = None
    new_thread_state: str | None = None

    @property
    def state_changes(self) -> bool:
        return self.new_warmth is not None or self.new_thread_state is not None


@dataclass
class Report:
    plans: list[ContactPlan] = field(default_factory=list)
    flagged: list[TouchVerdict] = field(default_factory=list)
    reply_touches_seen: int = 0
    contacts_seen: int = 0

    @property
    def demoted_touches(self) -> int:
        return sum(len(p.demotions) for p in self.plans)

    @property
    def contacts_with_state_change(self) -> int:
        return sum(1 for p in self.plans if p.state_changes)


def _recomputed_state(kept: list[Touch]) -> tuple[str, str]:
    """The (warmth, thread_state) implied by the touches that REMAIN.

    Replays `TOUCH_TRANSITIONS` over them in time order — the same ladder
    `apply_touch` climbs, run forward from scratch rather than un-applied
    in reverse. Un-applying is not possible: the ratchet is a max, so
    "what would warmth have been without this touch" has no answer once
    other touches have landed on top. Replaying does have one.
    """
    warmth = "cold"
    thread_state = "no_reply"
    for touch in sorted(kept, key=lambda t: (t.ts, t.id)):
        transition = TOUCH_TRANSITIONS.get(touch.kind)
        if transition is None:
            continue  # manual_override and anything else off the ladder
        new_warmth, new_state = transition
        if new_warmth and WARMTH_RANK.get(new_warmth, 0) > WARMTH_RANK.get(warmth, 0):
            warmth = new_warmth
        if new_state and thread_state != "advocate":
            thread_state = new_state
    return warmth, thread_state


def build_report(user) -> Report:
    """Judge every `reply_received` touch this user has. Reads only."""
    report = Report()

    contacts = {
        c.id: c for c in Contact.objects.for_user(user).select_related("firm")
    }
    report.contacts_seen = len(contacts)

    touches_by_contact: dict[int, list[Touch]] = {}
    for touch in Touch.objects.for_user(user).order_by("ts", "id"):
        touches_by_contact.setdefault(touch.contact_id, []).append(touch)

    for contact_id, touches in sorted(touches_by_contact.items()):
        contact = contacts.get(contact_id)
        if contact is None:
            continue  # defensive: a touch whose contact is out of scope

        kinds = {t.kind for t in touches}
        has_outbound = bool(kinds & set(OUTBOUND_KINDS))
        has_override = MANUAL_OVERRIDE_KIND in kinds

        plan = ContactPlan(contact=contact)
        for touch in touches:
            if touch.kind != "reply_received":
                continue
            report.reply_touches_seen += 1

            is_bulk, why = looks_like_bulk_evidence(touch.note)
            if not is_bulk:
                continue

            # Conditions 1-3, each reported rather than silently skipped:
            # a held-back demotion is exactly the thing the owner most
            # needs to be able to see and overrule.
            if has_override:
                report.flagged.append(TouchVerdict(
                    touch, contact, demote=False, flagged=True,
                    reason=(f"{why}, but this contact carries a manual "
                            "correction — human judgement wins, left alone"),
                ))
                continue
            if touch.source != "capture":
                report.flagged.append(TouchVerdict(
                    touch, contact, demote=False, flagged=True,
                    reason=(f"{why}, but this touch came from '{touch.source}', "
                            "not the capture pipeline — left alone"),
                ))
                continue
            if has_outbound:
                report.flagged.append(TouchVerdict(
                    touch, contact, demote=False, flagged=True,
                    reason=(f"{why}, but you have written to this person — "
                            "an inbound message here can genuinely be a "
                            "reply, left alone"),
                ))
                continue

            plan.demotions.append(TouchVerdict(
                touch, contact, demote=True,
                reason=f"{why}; you have never written to this person",
            ))

        if not plan.demotions:
            continue

        demoted_ids = {v.touch.id for v in plan.demotions}
        kept = [t for t in touches if t.id not in demoted_ids]
        warmth, thread_state = _recomputed_state(kept)
        if warmth != contact.warmth:
            plan.new_warmth = warmth
        if thread_state != contact.thread_state:
            plan.new_thread_state = thread_state
        report.plans.append(plan)

    return report


def commit_plan(user, plan: ContactPlan) -> None:
    """Write one contact's plan. Import-local so a report-only run never
    even loads the write path."""
    from crm import services as crm_services

    for verdict in plan.demotions:
        touch = verdict.touch
        # The kind changes; the evidence does NOT. Appending the reason
        # rather than replacing the note keeps whatever the scan recorded
        # about the message — the whole posture here is that a bulk email
        # is real information, just not relationship evidence.
        note = (touch.note or "").rstrip()
        marker = f"[reclassified: bulk/automated, not a reply — {verdict.reason}]"
        if marker not in note:
            note = f"{note} {marker}".strip()
        touch.kind = BULK_RECEIVED_KIND
        touch.note = note
        touch.save(update_fields=["kind", "note"])

    if not plan.state_changes:
        return

    # Through `set_contact_state`, not a bare UPDATE: that is the only
    # path that can move warmth DOWN, and it writes its own audit touch so
    # the History shows why the contact cooled. That audit row is a
    # `manual_override`, which means a second run of this command will
    # skip this contact by condition 3 — correct, and idempotent by
    # accident rather than by design, so the note says plainly who did it.
    crm_services.set_contact_state(
        user.id,
        plan.contact.id,
        warmth=plan.new_warmth,
        thread_state=plan.new_thread_state,
        note=(
            f"Correction: {len(plan.demotions)} inbound message"
            f"{'s' if len(plan.demotions) != 1 else ''} recorded as a reply "
            "turned out to be bulk or automated mail. Status recalculated "
            "from the remaining evidence."
        ),
    )
