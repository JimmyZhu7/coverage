"""capture_discover — create contacts for people a mailbox scan newly found.

    python manage.py capture_discover --email you@example.com --findings people.json
    python manage.py capture_discover --email you@example.com --findings f.json --dry-run

WHY THIS IS A SEPARATE COMMAND FROM `capture_gmail`
---------------------------------------------------
`capture_gmail` deliberately never creates a contact. That rule is right for
what it does: it matches findings against people you are ALREADY tracking, so
an unmatched finding means the search drifted, and inventing a row would hide
the drift.

Discovery is the opposite intent — "here are people I found who are not on
your board yet" — and it needs its own door rather than a flag that softens
the other command's rule. Two commands, two contracts, neither weakening the
other.

WHAT IT REFUSES TO DO
---------------------
- It will not create someone who already exists, matched on email then on
  normalized name.
- It will not resurrect an ARCHIVED contact. Archiving is a deliberate user
  action; a scan finding that person again is not consent to undo it. The
  match is REPORTED so the user can unarchive by hand.
- It will not fabricate warmth. A discovered person starts cold with no
  touches unless the finding carries real evidence — outreach you sent, a
  reply, or a chat — in which case the touch is logged through the normal
  ratchet so the cadence engine sees the same history it would from any
  other source.

FINDINGS SHAPE
--------------
A JSON array of `{"name", "email", "role_guess", "firm", "outreach_sent",
"replied", "chat_status", "evidence", "bulk", "bulk_reasons"}`. `firm` is an
optional Coverage slug;
an unknown one is kept as free text rather than dropped, because
`Contact.firm_text` exists precisely so capture never blocks on directory
coverage.

`chat_status` is three-way — the same contract `capture_gmail` already asks
for, so both doors into the ratchet describe a conversation the same way:

- `"completed"` — the conversation ALREADY HAPPENED (a call, a coffee chat,
  a meeting). This is the only value that makes someone `chatted`.
- `"scheduled"` — a chat has been set up but has not happened yet. "Let's do
  Tuesday at noon" is scheduled, not completed.
- `"none"` — no chat either way. This is the default when the key is
  omitted, and it is what a warm email reply on its own earns, however
  enthusiastic the reply reads.

The evidence signals are a ladder, and at least one of them should be
present for anyone worth creating: you cannot discover a stranger. Strongest
first: `chat_status == "completed"`, then `chat_status == "scheduled"`, then
`replied`, then `outreach_sent`. If none is set the contact is still created
(someone met at an event and added by hand is real) but stays cold with no
touches.

`bulk` (optional, default False) sits OUTSIDE that ladder and overrides all
of it. It means "this inbound message was a mass or automated one" — a
programme invitation to a list, a newsletter, an application receipt, an
out-of-office. The contact is still created, and a `bulk_received` touch
still records the message, but warmth and thread_state do not move. See
`capture.inbound` for the deterministic header test the Gmail Live path uses
to set it, and `bulk_reasons` for the short why that ends up in the note.
"""

from __future__ import annotations

import json
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from capture.providers import normalize_email, normalize_name
from coverage_domain.pipeline import BULK_RECEIVED_KIND
from crm import services as crm_services
from crm.models import Contact
from directory.models import Firm


class Command(BaseCommand):
    help = "Create contacts for newly discovered people from a mailbox scan."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Whose CRM to write to.")
        parser.add_argument("--findings", required=True, help="JSON array, or '-'.")
        parser.add_argument("--source", default="capture",
                            help="Provenance stamped on new contacts.")
        parser.add_argument("--region", default="",
                            help="Region for new contacts (hk/us/...). Blank = unknown, "
                                 "which the cadence engine treats as 'either'.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(email__iexact=opts["email"])
        except User.DoesNotExist as exc:
            raise CommandError(f"no user with email {opts['email']}") from exc

        raw = sys.stdin.read() if opts["findings"] == "-" else None
        if raw is None:
            try:
                raw = open(opts["findings"], encoding="utf-8").read()
            except OSError as exc:
                raise CommandError(f"cannot read findings: {exc}") from exc
        try:
            people = json.loads(raw)
        except ValueError as exc:
            raise CommandError(f"findings is not valid JSON: {exc}") from exc
        if not isinstance(people, list):
            raise CommandError("findings must be a JSON array")

        dry = opts["dry_run"]
        tag = "[dry-run] " if dry else ""
        firm_by_slug = {f.slug: f for f in Firm.objects.all()}

        created = existing = archived_hits = skipped = 0
        touched = 0

        for person in people:
            if not isinstance(person, dict):
                skipped += 1
                continue
            # Truncated to the column width, not passed through. An agent
            # writes these findings, and an agent that hands back a whole
            # email signature as a "name" would otherwise raise
            # `DataError: value too long for type character varying(255)`
            # mid-batch — killing the run and taking every VALID row after it
            # down too. Verified 2026-08-02: a 300-char name crashed the
            # command and the good contact behind it was never created.
            name = str(person.get("name", "")).strip()[:255]
            if not name:
                skipped += 1
                continue
            email = normalize_email(str(person.get("email", "")).strip())[:254]

            # Match against EVERY row including archived, so an archived
            # person is recognised rather than duplicated.
            everyone = Contact.objects.for_user(user)
            match = None
            if email:
                match = everyone.filter(email__iexact=email).first()
            if match is None:
                target = normalize_name(name)
                match = next(
                    (c for c in everyone if normalize_name(c.name) == target), None
                )

            if match is not None:
                if match.archived:
                    archived_hits += 1
                    self.stdout.write(
                        f"{tag}ARCHIVED {name} — already on the board but archived. "
                        "Reported, not resurrected: unarchive by hand if they should "
                        "come back."
                    )
                else:
                    existing += 1
                    self.stdout.write(f"{tag}HAVE     {name} — already tracked, skipped")
                continue

            firm = firm_by_slug.get(str(person.get("firm", "")).strip())
            created += 1
            self.stdout.write(
                f"{tag}NEW      {name}"
                + (f" <{email}>" if email else " (no address)")
                + (f" @ {firm.name}" if firm else "")
            )
            if dry:
                continue

            contact = Contact(
                user=user, name=name, email=email or "",
                firm=firm,
                firm_text="" if firm else str(person.get("firm", "")).strip()[:255],
                role=str(person.get("role_guess", "")).strip()[:255],
                region=opts["region"], source=opts["source"],
                notes=f"Discovered by mailbox scan · {timezone.localdate():%b %d, %Y}"
                      + (f"\n{person.get('evidence')}" if person.get("evidence") else ""),
            )
            contact.save()

            # Real evidence -> a real touch, through the same ratchet every
            # other source uses. Never invented: a person found on a CC line
            # with no reply stays cold, which is the truth about them.
            #
            # `outreach_sent` is the case this used to drop on the floor, and
            # dropping it produced a page that contradicted itself. The
            # discovery agent would find a thread where the user had emailed
            # someone who never wrote back, create the contact, and record
            # "Follow-up outreach sent … no reply yet" in the notes — while
            # logging no touch at all. The contact therefore had zero touches,
            # so the cadence engine's never-contacted branch fired and Today
            # said "Added but never contacted. Send the first note." about a
            # person whose own notes said the note had been sent. Observed on
            # live data 2026-08-05 (Jason Law, Christine Lee — both from the
            # ICC alumni panel thread) and reported by the owner.
            #
            # `chat_status` is three-way for the same reason `capture_gmail`
            # asks for three: a boolean "chatted" flag defined as "a real
            # conversation happened" is loose enough that a warm two-way
            # EMAIL reply satisfies it. On 2026-08-12 a discovery run marked
            # Ellen Chung `chatted` on the strength of "Thanks for the email,
            # filled out the form!" — no call, no meeting — which set
            # warmth='chatted'/thread_state='chat_done', nagged Today for a
            # debrief of a chat that never happened, and was UNRECOVERABLE by
            # any later run: `capture_worklist` drops anyone at `chatted` or
            # `advocate` from every future re-check, so the bad mark sits
            # there until a human fixes it by hand. A scheduled-but-not-yet-
            # held chat has the same failure mode in reverse, which is why it
            # gets its own rung rather than collapsing into "chat".
            #
            # Ordering is the ladder's, strongest evidence first: a chat that
            # happened outranks one that is merely booked, outranks a reply,
            # outranks outreach.
            # `bulk` short-circuits the whole ladder, ahead of `chat_status`
            # and `replied` alike. A discovery scan finds people the student
            # has never written to — which is precisely the population whose
            # inbound mail is most likely to be a blast, and the population
            # for whom "they replied" is least likely to be true. On
            # 2026-08-13 this branch logged `reply_received` for Caroline
            # Baenen (Manager, Talent Acquisition, West Monroe) off a mass
            # "Sophomore Series" invitation, warming a recruiter the founder
            # had never emailed and putting a coffee-chat ask in his queue.
            # A bulk finding still CREATES the contact (the person and their
            # firm are real information) — it just does not pretend they
            # answered him.
            chat_status = str(person.get("chat_status", "none") or "none").strip().lower()
            kind = (BULK_RECEIVED_KIND if person.get("bulk")
                    else "chat" if chat_status == "completed"
                    else "chat_scheduled" if chat_status == "scheduled"
                    else "reply_received" if person.get("replied")
                    else "outreach" if person.get("outreach_sent") else None)
            if kind:
                reasons = str(person.get("bulk_reasons") or "").strip()
                note = "Discovered by mailbox scan"
                if kind == BULK_RECEIVED_KIND:
                    note = (
                        "Discovered by mailbox scan — bulk/automated email, "
                        "not a reply"
                    )
                    if reasons:
                        note = f"{note} [{reasons}]"
                crm_services.log_touch(
                    user.id, contact.id, kind, "email",
                    note=note, source="capture",
                )
                touched += 1

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{created} created ({touched} with a touch logged), "
            f"{existing} already tracked, {archived_hits} archived-and-left-alone, "
            f"{skipped} unusable"))
