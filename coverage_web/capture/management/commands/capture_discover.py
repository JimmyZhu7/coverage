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
"replied", "chat_status", "chat_scheduled_at", "evidence", "bulk",
"bulk_reasons", "thread_id"}`. `thread_id` is optional and is the Gmail
thread the evidence came from: supplying it stamps the touch with the same
`[gmail:<id>]` marker every other capture door writes, so the daily sync
re-reading that conversation dedups against it instead of logging the same
reply a second time. `firm` is an
optional Coverage slug;
an unknown one is kept as free text rather than dropped, because
`Contact.firm_text` exists precisely so capture never blocks on directory
coverage.

`chat_status` is three-way — the same contract `capture_gmail` already asks
for, so both doors into the ratchet describe a conversation the same way:

- `"completed"` — the conversation ALREADY HAPPENED (a call, a coffee chat,
  a meeting). This is the only value that makes someone `chatted`.
- `"scheduled"` — BOTH SIDES HAVE AGREED ON A TIME and the chat has not
  happened yet. A calendar invite either side accepted qualifies. So does
  "Tuesday at noon works, see you then" — an offer plus the other party's
  agreement to it.
- `"none"` — no chat either way. This is the default when the key is
  omitted, and it is what a warm email reply on its own earns, however
  enthusiastic the reply reads.

WHAT IS NOT `"scheduled"`: an offer nobody has accepted yet. "Happy to grab
coffee Tuesday at noon if that works" from them, with no reply from you, is a
REPLY — `replied: true`, `chat_status: "none"`. So is your own proposal they
have not answered. One party naming a time is a proposal; a booking takes
two. The date being specific does not make it agreed.

That distinction is the whole point of the value, and getting it backwards
has already shipped. Youqi Chen, live, 2026-08-31: a discovery run read
"coffee in HK, offered same-day meetup" as `"scheduled"`, which parked her at
`thread_state="chat_scheduled"` and put a permanent "did it happen? log the
chat or reschedule" card on Today about a meeting that was never booked. The
same shape as the Ellen Chung case below, one rung up the ladder.

`chat_scheduled_at` (ISO 8601) is the chat's own time, and it is what BOTH
chat values are CORROBORATED BY rather than a decoration on them. Not
optional in practice: `"completed"` and `"scheduled"` alike fall one rung
down the ladder without it. A chat that was held had a time and so did a
chat that was booked; a classifier that can tell you two people were warm at
each other but cannot tell you when they met is reading enthusiasm. See the
touch ladder in `handle` for the two live failures that set this bar, and why
it is the bar the live capture path already meets.

The evidence signals are a ladder, and at least one of them should be
present for anyone worth creating: you cannot discover a stranger. Strongest
first: `chat_status == "completed"`, then `chat_status == "scheduled"` (both
only when `chat_scheduled_at` corroborates them), then `replied`, then
`outreach_sent`. If none is set the contact is still created
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

from capture.providers import CHAT_CLAIMS, chat_status_of, chat_time_stated, normalize_email
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
        # Findings that claimed `chat_status: "scheduled"` and brought no time
        # to back it up. Counted and printed rather than silently downgraded:
        # a steady stream of these is a classifier that has not read the
        # contract, and the whole reason this defect reached a live card is
        # that nothing anywhere said a word while it happened.
        unconfirmed_chats = 0

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
            # person is recognised rather than duplicated. THE one matcher
            # (`capture.discovery._match_existing`) — this command used to
            # carry its own inline copy of the email-then-name rule, which
            # was a second opinion about who is a duplicate waiting to
            # drift; now every door asks the same question the same way.
            from capture import discovery

            match = discovery._match_existing(user, email, name)

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
            #
            # A DOCUMENTED CONTRACT IS NOT A GUARD, so `"scheduled"` now has
            # to bring a time with it. The contract above says a booking takes
            # two parties; nothing here could check that, and the classifier
            # that fills these findings runs OUTSIDE this repo, so tightening
            # the prose alone changes nothing about what arrives. Youqi Chen,
            # live 2026-08-31: "Replied to your email: coffee in HK, offered
            # same-day meetup" came in as `"scheduled"`, and an offer nobody
            # had accepted became `thread_state="chat_scheduled"` — a state
            # whose only exit is a human, and whose only behaviour is a daily
            # "did it happen? log the chat or reschedule" card about a meeting
            # that was never booked.
            #
            # THE BAR IS THE ONE THE LIVE PATH ALREADY ENFORCES. `capture.
            # gmail_live` emits `"chat_status": "scheduled" if ics_dt else
            # "none"` — twice, at both of its exits. It will not call a chat
            # scheduled without a real invite time. Discovery accepted a bare
            # language judgment with no corroboration at all, and the
            # asymmetry is what let this through. `chat_scheduled_at` is that
            # corroboration: a stated time, which an agreement produces and a
            # vibe does not.
            #
            # FLOORING AT `reply_received` IS THE SAFE DIRECTION and is not a
            # loss. They did write to you, so a reply is true; warmth still
            # moves to `replied`, the contact is still warm, still in the
            # queue, still re-checkable by every later run. What it does not
            # do is invent a booking. Compare the failure it replaces: the
            # comment above records that `capture_worklist` drops `chatted`
            # and `advocate` from every re-check, and `chat_scheduled` is the
            # same kind of trap one rung down — branch 2 of the cadence engine
            # `continue`s on it, so a wrongly-parked contact gets that one
            # card and nothing else, forever, until a human intervenes.
            # Under-reporting costs a rung that the next real signal restores.
            #
            # Reported, never silent: the summary line counts these so a run
            # that keeps producing them is visible as a classifier problem
            # rather than looking like a quiet day.
            # ONE RUNG DOWN, NOT OFF THE LADDER. The floor is `reply_received`
            # and not "fall through to whatever else is set", because a
            # `"scheduled"` finding is inbound evidence by construction —
            # somebody proposed a chat to this student — and a classifier that
            # sets the strongest rung has no reason to also set the weaker
            # `replied` beneath it. Dropping such a finding to no touch at all
            # would recreate the failure the `outreach_sent` comment above
            # documents: a contact created with zero touches, about whom Today
            # then says "added but never contacted" while their own notes
            # describe the exchange.
            #
            # The one exception is the shape `capture.discovery._evidence_kind`
            # already carves out: an outreach-only finding stays `outreach`
            # however it is labelled, because "an invite the user sent an
            # unknown person is still only the user's own act".
            #
            # `"completed"` IS HELD TO THE SAME BAR, and it is the rung that
            # matters most. The fix above covered `"scheduled"` and stopped
            # there, which left the STRONGER claim ungated: `"completed"`
            # logs kind `chat`, which sets warmth `chatted`, and
            # `capture_worklist.RECHECK_WARMTH` drops `chatted` from every
            # later re-check — so that one is not a card a human can argue
            # with, it is a contact no automated run will ever look at again.
            # Ellen Chung, live 2026-08-12: "Thanks for the email, filled out
            # the form!" came in as `"completed"`, and Coverage spent three
            # days asking the founder to debrief a conversation that never
            # happened. Patina Chu carries the same shape from 2026-08-02 and
            # was never corrected. Neither finding named a time; a chat that
            # happened had one.
            # Through `capture.providers` rather than re-spelled: this is one
            # of the three places `chat_status` is turned into a decision
            # (`capture.gmail._touch_kind_for` is another), and that module's
            # docstring names re-spelling the test as the exact way the
            # Youqi Chen shape could ship a second time. `chat_status_of` is
            # `chat_status` normalized the same way this line used to do it
            # inline (`str(...).strip().lower()`, default "none");
            # `chat_time_stated` is the `chat_scheduled_at` presence check.
            chat_status = chat_status_of(person)
            outbound_only = bool(person.get("outreach_sent")) and not person.get("replied")
            uncorroborated = (
                chat_status in CHAT_CLAIMS
                and not chat_time_stated(person)
            )
            if uncorroborated:
                unconfirmed_chats += 1
            kind = (BULK_RECEIVED_KIND if person.get("bulk")
                    else "outreach" if uncorroborated and outbound_only
                    else "reply_received" if uncorroborated
                    else "chat" if chat_status == "completed"
                    else "chat_scheduled" if chat_status == "scheduled"
                    else "reply_received" if person.get("replied")
                    else "outreach" if person.get("outreach_sent") else None)
            if kind:
                reasons = str(person.get("bulk_reasons") or "").strip()
                # THE THREAD MARKER, WHEN THE SCAN CAN NAME A THREAD. Every
                # other door into the ratchet stamps `[gmail:<id>]` on the
                # note, and `capture.gmail.thread_stage_rank` reads exactly
                # that string to decide whether a later finding on the same
                # thread is new evidence or the same message re-seen. A touch
                # written here without one ranks 0 on every thread forever, so
                # the daily sync re-finding that same conversation logs a
                # SECOND touch of the same kind — which states nothing false,
                # but resets `last_touch` and pushes the cadence engine's
                # follow-up nudge out by a week. `capture.discovery.accept`
                # already stamps the marker for the proposal door and says so
                # in its docstring; this door was the one exception.
                #
                # Optional, because the field is not part of the documented
                # findings shape and the scan does not always have a thread to
                # name. Absent, this behaves exactly as it did.
                thread_id = str(person.get("thread_id") or "").strip()
                marker = f"[gmail:{thread_id}] " if thread_id else ""
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
                    note=f"{marker}{note}", source="capture",
                )
                touched += 1

        if unconfirmed_chats:
            self.stdout.write(self.style.WARNING(
                f"{tag}{unconfirmed_chats} finding(s) claimed a chat with no "
                "chat_scheduled_at — logged one rung down. A chat has a time, "
                "booked or held; an offer nobody accepted is a reply."))
        self.stdout.write(self.style.SUCCESS(
            f"{tag}{created} created ({touched} with a touch logged), "
            f"{existing} already tracked, {archived_hits} archived-and-left-alone, "
            f"{skipped} unusable"))
